"""
Scheduled sync: the Gather SMB share -> bronze + silver in PostgreSQL.

    python -m db.sync_share --dsn postgresql+psycopg://... [--full]
    python -m db.sync_share --dry-run                       # no database needed

Replaces the one-shot backfill. A full run and an incremental run are the same
code path: the only difference is whether a watermark is applied, so the
backfill is just the first incremental run.

Why it scans the way it does
----------------------------
Every per-file operation on this share is expensive, and metadata is not
cheaper than content. Measured from a laptop, 150 session files:

    os.scandir + DirEntry.stat    0.17 s/file      (mtime comes back with the
                                                    directory listing itself)
    Path.stat()                   1.53 s/file      (a second round trip)
    read_text()                   2.17 s/file

So the scan gets its mtimes from ``os.scandir`` — thirteen times cheaper than
reading — and only opens files newer than the watermark. On the live share that
is 15 files instead of 150 for a weekly window: ~36 s of listing plus ~33 s of
reading, against ~326 s to read everything.

``Path.rglob`` is avoided deliberately: it calls ``stat`` again per entry and
would cost more than reading the files.

Correctness of the watermark
----------------------------
The watermark is the previous run's *start* time minus an overlap, not its end
time. A record written while the previous scan was in progress would otherwise
fall in the gap between "already listed" and "newer than the end time". The
overlap re-reads a little, which is free: every event id is derived from the
record's own fields, so re-reading produces the same ids and the insert is a
no-op.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from typing import Iterator, Optional

from db.ingest import derive_event_id, ingest_batch

RECORD_KINDS = ("sessions", "workflows", "feedback_submissions")

# Re-read this far back beyond the last run. Cheap because ingestion is
# idempotent, and it absorbs both mid-scan writes and laptop clock skew.
OVERLAP = timedelta(hours=6)

_CASE_IN_PATH = re.compile(r"[\\/](0[01]\d{6})[\\/]")
_SIM_PREFIX = "SIM-"


# ------------------------------------------------------------------ scan --
def scan(root: str, since: Optional[datetime] = None) -> list[tuple[str, str, str, float]]:
    """Return (kind, path, file_name, mtime) for files newer than ``since``."""
    cut = since.timestamp() if since else 0.0
    out: list[tuple[str, str, str, float]] = []
    for kind in RECORD_KINDS:
        base = os.path.join(root, kind)
        if not os.path.isdir(base):
            continue
        for user_dir in os.scandir(base):
            if not user_dir.is_dir():
                continue
            for entry in os.scandir(user_dir.path):
                if not entry.name.endswith(".json"):
                    continue
                mt = entry.stat().st_mtime      # cached by the listing
                if mt > cut:
                    out.append((kind, entry.path, entry.name, mt))
    return out


# ------------------------------------------------- record -> event mapping --
def _environment(rec: dict, file_name: str) -> str:
    ids = (file_name, str(rec.get("conversation_id", "")),
           str(rec.get("workflow_id", "")), str(rec.get("feedback_event_id", "")))
    if any(i.startswith(_SIM_PREFIX) for i in ids):
        return "sim"
    if "format_check" in file_name:
        return "format_check"
    return "production"


def _case_ref(rec: dict) -> tuple[str, str]:
    nbr = str((rec.get("case") or {}).get("case_nbr") or "").strip()
    if nbr.startswith("local_upload"):
        return "", "absent"
    if nbr:
        return nbr, "explicit"
    hit = _CASE_IN_PATH.search(str(rec.get("log_path") or ""))
    return (hit.group(1), "derived_from_path") if hit else ("", "absent")


def _issue_time(raw) -> Optional[str]:
    s = str(raw or "").strip()
    if not s:
        return None
    for fmt in ("%m/%d/%Y-%H:%M:%S.%f", "%m/%d/%Y-%H:%M:%S", "%m/%d/%Y"):
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=timezone.utc).isoformat()
        except ValueError:
            continue
    return None


def _base(rec: dict, file_name: str, etype: str, eid, occurred) -> dict:
    return {
        "event_id": str(eid),
        "event_type": etype,
        "schema_version": int(rec.get("schema_version") or 1),
        "environment": _environment(rec, file_name),
        "occurred_at": occurred,
        "user_name": str(rec.get("user_name") or ""),
        "app_version": str(rec.get("app_version") or ""),
        "source_ref": file_name,
        "payload": {},
    }


def session_events(rec: dict, file_name: str) -> Iterator[dict]:
    conv = str(rec.get("conversation_id") or "").strip()
    if not conv:
        return
    updated = str(rec.get("updated_at") or rec.get("created_at") or "")
    created = rec.get("created_at") or updated
    case = rec.get("case") or {}
    case_nbr, case_src = _case_ref(rec)
    domain = rec.get("domain")

    ev = _base(rec, file_name, "conversation.started",
               derive_event_id("legacy", "chatbot_session", conv, updated), created)
    ev["payload"] = {
        "conversation_id": conv,
        "session_id": rec.get("session_id") or "",
        "workflow_id": rec.get("workflow_id") or None,
        "agent_domain": rec.get("agent_domain") or domain or "",
        # Pre-v6 records carry one overloaded `domain`. On a chatbot session it
        # meant the agent, and wifi_chatbot/bt_chatbot only ever ran on their
        # own technology, so for those the agent identifies the case too. `nw`
        # is genuinely ambiguous and stays unknown.
        "case_domain": rec.get("case_domain") or case.get("wifi_or_bt")
                       or (domain if domain in ("wifi", "bt") else ""),
        "case_nbr": case_nbr,
        "case_ref_source": case_src,
        "subject": case.get("subject") or "",
        "issue_type": case.get("issue_type") or "",
        "issue_time": _issue_time(rec.get("issue_time")),
        "issue_time_window_minutes": rec.get("issue_time_window_minutes"),
        "log_path": rec.get("log_path") or "",
    }
    yield ev

    # v4+ only. Pre-v4 records have no turns and none are invented for them.
    for i, t in enumerate(rec.get("turns") or []):
        if not isinstance(t, dict):
            continue
        tid = str(t.get("turn_id") or f"{conv}#{i}")
        tev = _base(rec, file_name, "turn.recorded",
                    derive_event_id("legacy", "turn", conv, tid),
                    t.get("started_at") or created)
        tev["payload"] = {
            "turn_id": tid,
            "conversation_id": conv,
            "seq": i,
            "status": t.get("status") or "started",
            "error_code": t.get("error_code") or "",
            "model": t.get("model") or rec.get("model") or "",
            "usage": t.get("usage") or {},
            "cost_usd": t.get("cost_usd"),
            "latency_ms": t.get("latency_ms"),
            "pricing_version": (rec.get("cost_usd") or {}).get("pricing_version", ""),
            "rate_input_per_mtok": (rec.get("cost_usd") or {}).get("rate_input_per_mtok"),
            "rate_output_per_mtok": (rec.get("cost_usd") or {}).get("rate_output_per_mtok"),
        }
        yield tev


def workflow_events(rec: dict, file_name: str) -> Iterator[dict]:
    wid = str(rec.get("workflow_id") or "").strip()
    if not wid:
        return
    updated = str(rec.get("updated_at") or rec.get("created_at") or "")
    created = rec.get("created_at") or updated
    case = rec.get("case") or {}

    ev = _base(rec, file_name, "workflow.started",
               derive_event_id("legacy", "workflow", wid, updated), created)
    ev["payload"] = {
        "workflow_id": wid,
        "case_nbr": str(case.get("case_nbr") or ""),
        "case_domain": rec.get("case_domain") or case.get("wifi_or_bt") or "",
        "subject": case.get("subject") or "",
        "issue_type": case.get("issue_type") or "",
        "agents_used": rec.get("agents_used") or [],
    }
    yield ev

    for inv in rec.get("ai_invocations") or []:
        if not isinstance(inv, dict):
            continue
        iid = str(inv.get("invocation_id") or "")
        if not iid:
            continue
        cost = inv.get("cost_usd") or {}
        iev = _base(rec, file_name, "invocation.recorded",
                    derive_event_id("legacy", "invocation", iid),
                    inv.get("ts") or created)
        iev["payload"] = {
            "invocation_id": iid,
            "workflow_id": wid,
            "conversation_id": inv.get("conversation_id") or None,
            "feature_code": inv.get("feature_code") or "unknown",
            "agent_domain": inv.get("agent_domain") or inv.get("domain") or "",
            "model": inv.get("model") or "",
            "usage": inv.get("usage") or {},
            "cost_usd": cost.get("total"),
            "unpriced_model": cost.get("unpriced_model") or "",
            "pricing_version": cost.get("pricing_version") or "",
            "status": inv.get("status") or "success",
            "error_code": inv.get("error_code") or "",
            "latency_ms": inv.get("latency_ms"),
        }
        yield iev

    audit = rec.get("attachment_audit")
    if isinstance(audit, dict):
        aev = _base(rec, file_name, "attachment.audited",
                    derive_event_id("legacy", "audit", wid, updated), created)
        aev["payload"] = {
            "workflow_id": wid,
            "declared_attached": audit.get("issue_declared_attached"),
            "files": [
                {"name": f.get("name") or f.get("file_name") or "",
                 "selected": bool(f.get("selected")),
                 "status": f.get("status") or f.get("download_status")
                           or "not_attempted",
                 "error_code": f.get("error_code") or ""}
                for f in (audit.get("files") or []) if isinstance(f, dict)
            ],
        }
        yield aev


def feedback_events(rec: dict, file_name: str) -> Iterator[dict]:
    fid = str(rec.get("feedback_event_id") or "").strip()
    if not fid:
        return
    submitted = rec.get("submitted_at") or rec.get("date") or ""
    ev = _base(rec, file_name, "feedback.submitted",
               derive_event_id("legacy", "feedback", fid), submitted)
    ev["payload"] = {
        "feedback_event_id": fid,
        "conversation_id": rec.get("conversation_id") or None,
        "turn_id": rec.get("turn_id") or None,
        "workflow_id": rec.get("workflow_id") or None,
        "case_nbr": str(rec.get("case_nbr") or ""),
    }
    yield ev


_EXPANDERS = {
    "sessions": session_events,
    "workflows": workflow_events,
    "feedback_submissions": feedback_events,
}


def build_events(root: str, since: Optional[datetime] = None) -> tuple[list[dict], dict]:
    t0 = time.time()
    files = scan(root, since)
    t_scan = time.time() - t0
    events, unread = [], 0
    t1 = time.time()
    for kind, path, name, _mt in files:
        try:
            rec = json.loads(open(path, encoding="utf-8").read())
        except Exception as e:
            print(f"  [skip] {name}: {e}", file=sys.stderr)
            unread += 1
            continue
        if isinstance(rec, dict):
            events.extend(_EXPANDERS[kind](rec, name))
    stats = {
        "files_seen": len(files),
        "files_unreadable": unread,
        "events": len(events),
        "scan_sec": round(t_scan, 1),
        "read_sec": round(time.time() - t1, 1),
    }
    return events, stats


# ------------------------------------------------------------ watermark --
def _read_watermark(conn, root: str) -> Optional[datetime]:
    from sqlalchemy import text
    row = conn.execute(
        text("SELECT last_run_started_at FROM bronze.sync_state WHERE source_root = :r"),
        {"r": root},
    ).fetchone()
    return row[0] if row else None


def _write_watermark(conn, root: str, started: datetime, stats: dict) -> None:
    from sqlalchemy import text
    conn.execute(text("""
        INSERT INTO bronze.sync_state
            (source_root, last_run_started_at, last_run_finished_at,
             files_seen, events_emitted)
        VALUES (:r, :s, now(), :f, :e)
        ON CONFLICT (source_root) DO UPDATE SET
            last_run_started_at  = EXCLUDED.last_run_started_at,
            last_run_finished_at = EXCLUDED.last_run_finished_at,
            files_seen           = EXCLUDED.files_seen,
            events_emitted       = EXCLUDED.events_emitted
    """), {"r": root, "s": started, "f": stats["files_seen"],
           "e": stats["events"]})


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--source", default=None, help="Gather share root")
    ap.add_argument("--dsn", default=None)
    ap.add_argument("--full", action="store_true",
                    help="ignore the watermark and re-read everything")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--batch", type=int, default=200)
    args = ap.parse_args()

    root = args.source
    if not root:
        from configs.path_configs import GATHER_DIR_prim
        root = GATHER_DIR_prim
    started = datetime.now(timezone.utc)

    if args.dry_run:
        events, stats = build_events(root, None)
        _summarise(events, stats)
        print("dry run — nothing written")
        return 0

    if not args.dsn:
        print("error: --dsn is required unless --dry-run", file=sys.stderr)
        return 2

    from sqlalchemy import create_engine
    engine = create_engine(args.dsn, future=True)
    with engine.begin() as conn:
        since = None if args.full else _read_watermark(conn, root)
        if since:
            since = since - OVERLAP
        print(f"watermark: {since.isoformat() if since else '(none — full read)'}")
        events, stats = build_events(root, since)
        _summarise(events, stats)

        totals = {"accepted": 0, "duplicate": 0, "rejected": 0}
        for i in range(0, len(events), args.batch):
            res = ingest_batch(conn, events[i:i + args.batch])
            totals["accepted"] += len(res.accepted)
            totals["duplicate"] += len(res.duplicate)
            totals["rejected"] += len(res.rejected)
            for eid, why in res.rejected.items():
                print(f"  [reject] {eid}: {why}", file=sys.stderr)
        # Only advance the watermark inside the same transaction that stored
        # the events. A crash here re-reads the window instead of skipping it.
        _write_watermark(conn, root, started, stats)

    print(f"accepted={totals['accepted']} duplicate={totals['duplicate']} "
          f"rejected={totals['rejected']}")
    return 1 if totals["rejected"] else 0


def _summarise(events: list[dict], stats: dict) -> None:
    from collections import Counter
    print(f"files={stats['files_seen']} unreadable={stats['files_unreadable']} "
          f"events={stats['events']}  scan={stats['scan_sec']}s read={stats['read_sec']}s")
    print(f"  by type     : {dict(Counter(e['event_type'] for e in events))}")
    print(f"  environment : {dict(Counter(e['environment'] for e in events))}")
    ids = {e["event_id"] for e in events}
    print(f"  distinct ids: {len(ids)}  (collisions: {len(events) - len(ids)})")


if __name__ == "__main__":
    sys.exit(main())
