"""
Gather Service — usage-analytics sidecar.

Captures, on the first Send of a chatbot session, the session context the user
entered the chatbot with (Windows user name, date, CASE NUMBER and a short
summary of the case) plus the first question they asked. One tidy JSON file
per conversation is written to a shared "Gather" folder for later ingestion
into a backend database, so we can tally:

  * how many distinct people use the tool,
  * which CASE NUMBERs they worked on,
  * roughly what those cases were about.

Design goals (mirrors feedback_service):
  * Independent of the chatbot agent — never blocks chat, never raises.
  * No DB. Flat JSON files only ("bronze" layer for a future DB load).
  * Writes happen on a background thread so the HTTP response isn't stalled
    by a slow / off-VPN SMB share.
  * Each record carries an integer `schema_version` so downstream ETL knows
    which parsing rules to apply when fields are added later.

Storage resolution order (cached for the process lifetime):
  1. Shared primary  (configs.path_configs.GATHER_DIR_prim)
  2. Shared backup   (configs.path_configs.GATHER_DIR_bkup)
  3. Local fallback  ``<avatarfiles_dir>/Gather``   (off-VPN / share down)

The shared UNCs are not written out in this file or logged in full — see
path_configs and ``_redact_path`` below.

Layout:  <root>/sessions/<user>/<conversation_id>.json
A per-user subfolder makes "how many distinct people" a simple folder count,
while attribution also travels inside every record (user_name field) so a flat
DB load can ignore the folder structure entirely.
"""

from __future__ import annotations

import getpass
import json
import os
import re
import sys
import threading
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Optional

from configs.global_configs import app_config
from configs.path_configs import GATHER_DIR_prim, GATHER_DIR_bkup
from utils import helpers


# Bump when the record structure changes (downstream ETL keys off this).
#   v1 - Initial usage record (user_name, date, case, log_path, issue_time,
#        messages[]).
#   v2 - Added send_count (incremented on every Send) + silver aggregate files
#        in <root>/aggregates/ (summary, users, cases, daily).
#   v3 - Added issue_time_window_minutes (the ±minutes window around issue_time
#        used for the Segment2 log slice; default 5).
GATHER_SCHEMA_VERSION = 3

_MAX_DESC_CHARS = 2000        # keep the case summary compact for the DB
_MAX_MSG_CHARS = 4000         # cap the stored first question

# Minimum seconds between async aggregate rebuilds (per process). Each write
# schedules a rebuild but only one runs per cooldown window, so a burst of
# Sends doesn't fan out into a burst of full-scan rebuilds.
_AGG_COOLDOWN_SEC = 60

_root_cache: Optional[Path] = None
_root_lock = threading.Lock()

_locks_guard = threading.Lock()
_path_locks: dict[str, threading.Lock] = {}

_agg_lock = threading.Lock()
_last_agg_at: float = 0.0


def _redact_path(p: Any) -> str:
    """Hide the full SMB UNC in any log line: keep just ``\\<host>\\…\\<leaf>``."""
    s = str(p or "")
    if s.startswith("\\\\"):
        parts = s.lstrip("\\").split("\\")
        if len(parts) >= 2:
            return rf"\\{parts[0]}\…\{parts[-1]}"
    return s


def _current_user() -> str:
    """Best-effort Windows username; sanitised so it can be a folder name."""
    try:
        u = getpass.getuser() or os.environ.get("USERNAME", "") or "anon"
    except Exception:
        u = os.environ.get("USERNAME", "") or "anon"
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", u).strip("._-") or "anon"
    return safe


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _today() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def _safe_id(value: Any) -> str:
    """Filesystem-safe conversation id (defends against path traversal)."""
    s = str(value or "").strip()
    s = re.sub(r"[^A-Za-z0-9_.-]+", "_", s)
    return s[:128] or "unknown"


def _resolve_root() -> Path:
    """Resolve the Gather root once and cache it (8 s share probe per path)."""
    global _root_cache
    if _root_cache is not None:
        return _root_cache
    with _root_lock:
        if _root_cache is not None:
            return _root_cache

        share = helpers.get_load_path(GATHER_DIR_prim, GATHER_DIR_bkup)
        if share:
            try:
                root = Path(share)
                (root / "sessions").mkdir(parents=True, exist_ok=True)
                _root_cache = root
                print(f"[gather] using shared root: {_redact_path(root)}")
                return root
            except Exception as e:
                print(f"[gather] shared root unwritable ({_redact_path(share)}): {e} — falling back to local")

        base = getattr(app_config, "avatarfiles_dir", None)
        root = Path(base) / "Gather" if base else Path.cwd() / "data" / "Gather"
        (root / "sessions").mkdir(parents=True, exist_ok=True)
        _root_cache = root
        print(f"[gather] using local root: {_redact_path(root)}")
        return root


def _lock_for(path: Path) -> threading.Lock:
    key = str(path)
    with _locks_guard:
        lock = _path_locks.get(key)
        if lock is None:
            lock = threading.Lock()
            _path_locks[key] = lock
        return lock


def _conversation_path(conversation_id: str, user: str) -> Path:
    root = _resolve_root()
    user_dir = root / "sessions" / _safe_id(user)
    user_dir.mkdir(parents=True, exist_ok=True)
    return user_dir / f"{_safe_id(conversation_id)}.json"


def _clean_case(issue: Optional[dict]) -> dict:
    """Pull a tidy, compact case summary out of the issue context dict."""
    issue = issue if isinstance(issue, dict) else {}
    desc = str(issue.get("description") or "").strip()
    if len(desc) > _MAX_DESC_CHARS:
        desc = desc[:_MAX_DESC_CHARS] + "…"
    return {
        "case_nbr": str(issue.get("case_nbr") or "").strip(),
        "subject": str(issue.get("subject") or "").strip(),
        "issue_type": str(issue.get("issue_type") or "").strip(),
        "description": desc,
    }


def _new_record(
    conversation_id: str,
    session_id: str,
    user: str,
    issue: Optional[dict],
    log_path: str,
    issue_time: str,
    issue_time_window_minutes: Optional[int],
    domain: str,
) -> dict:
    now = _now_iso()
    return {
        "schema_version": GATHER_SCHEMA_VERSION,
        "record_type": "chatbot_session",
        "conversation_id": _safe_id(conversation_id),
        "session_id": session_id or "",
        "user_name": user,                  # required attribution
        "date": _today(),                   # required local date (YYYY-MM-DD)
        "created_at": now,
        "updated_at": now,
        "domain": domain or "wifi",
        "case": _clean_case(issue),
        "log_path": log_path or "",
        "issue_time": issue_time or "",
        # ±minutes window around issue_time used for the Segment2 log slice
        # (sidebar-adjustable; default 5). None when not supplied.
        "issue_time_window_minutes": issue_time_window_minutes,
        "messages": [],
        "message_count": 0,    # number of question texts stored (<=1)
        "send_count": 0,       # total Sends on this conversation (for stats)
    }


def _do_record(
    conversation_id: str,
    session_id: str,
    user_message: str,
    issue: Optional[dict],
    log_path: str,
    issue_time: str,
    issue_time_window_minutes: Optional[int],
    domain: str,
) -> None:
    """Worker-side: create or update the per-conversation usage record."""
    user = _current_user()
    try:
        path = _conversation_path(conversation_id, user)
    except Exception as e:
        print(f"[gather] could not resolve path (conv={conversation_id}): {e}")
        return

    with _lock_for(path):
        record = None
        existed = path.exists()
        if existed:
            try:
                record = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                record = None
        first_send = not isinstance(record, dict)
        if first_send:
            record = _new_record(
                conversation_id, session_id, user, issue, log_path,
                issue_time, issue_time_window_minutes, domain
            )

        # Refresh latest context (the user may have loaded a log / set a time
        # after the conversation started).
        if issue:
            record["case"] = _clean_case(issue)
        if log_path:
            record["log_path"] = log_path
        if issue_time:
            record["issue_time"] = issue_time
        if issue_time_window_minutes is not None:
            record["issue_time_window_minutes"] = issue_time_window_minutes
        record["updated_at"] = _now_iso()

        # Only the FIRST Send of a conversation records the question — later
        # sends just refresh context (no message accumulation).
        if first_send:
            msg = str(user_message or "").strip()
            if msg:
                if len(msg) > _MAX_MSG_CHARS:
                    msg = msg[:_MAX_MSG_CHARS] + "…"
                record["messages"] = [{"ts": _now_iso(), "text": msg}]
                record["message_count"] = 1

        # Send counter increments on every call so aggregates can distinguish
        # "how many conversations" from "how many questions sent".
        record["send_count"] = int(record.get("send_count") or 0) + 1
        # Forward-fill schema_version on existing v1 records we touch.
        record["schema_version"] = GATHER_SCHEMA_VERSION

        try:
            tmp = path.with_suffix(".json.tmp")
            tmp.write_text(
                json.dumps(record, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            tmp.replace(path)
        except Exception as e:
            print(f"[gather] write failed (conv={conversation_id}): {e}")
            return

    # Refresh silver aggregates in the background (debounced).
    _maybe_rebuild_aggregates_async()


def record_send(
    *,
    conversation_id: str,
    session_id: str = "",
    user_message: str = "",
    issue: Optional[dict] = None,
    log_path: str = "",
    issue_time: str = "",
    issue_time_window_minutes: Optional[int] = None,
    domain: str = "",
) -> None:
    """
    Record one Send into the shared Gather folder for usage analytics.

    Captures the entry session (user name, date, case context) and the first
    question on the first Send of a conversation. Subsequent Sends only
    refresh context (latest case info / log path / issue time / issue-time
    window / updated_at) on the same file — questions are NOT accumulated.
    Runs on a background thread and never raises, so the chat path is never
    blocked or broken.
    """
    if not conversation_id:
        return
    # Only the FROZEN (packaged release) build records usage analytics. In
    # DEVELOP mode (running from source, sys.frozen is False) we skip the
    # Gather write entirely so developer testing doesn't pollute the shared
    # stats on the network share.
    if not getattr(sys, "frozen", False):
        return
    # Normalise the window to a plain int (or None) so the stored record is
    # JSON-clean regardless of what the caller passed.
    try:
        window = int(issue_time_window_minutes) if issue_time_window_minutes is not None else None
    except (TypeError, ValueError):
        window = None
    try:
        t = threading.Thread(
            target=_do_record,
            args=(conversation_id, session_id, user_message, issue, log_path,
                  issue_time, window, domain),
            daemon=True,
        )
        t.start()
    except Exception as e:
        print(f"[gather] record_send dispatch failed: {e}")


# ---------------------------------------------------------------------------
# Silver layer: aggregate rollups derived from the bronze per-conversation
# records. Files live at <root>/aggregates/*.json and are fully regenerated
# from the bronze records, so they're always consistent with the source of
# truth (no fragile cross-machine counter increments on the SMB share).
# ---------------------------------------------------------------------------

def iter_records() -> Iterator[dict]:
    """Yield every bronze record under <root>/sessions/<user>/*.json."""
    try:
        root = _resolve_root()
    except Exception:
        return
    sess_dir = root / "sessions"
    if not sess_dir.exists():
        return
    for user_dir in sess_dir.iterdir():
        if not user_dir.is_dir():
            continue
        for f in user_dir.glob("*.json"):
            try:
                rec = json.loads(f.read_text(encoding="utf-8"))
            except Exception:
                continue
            if isinstance(rec, dict):
                yield rec


def _min_iso(a: Optional[str], b: Optional[str]) -> Optional[str]:
    if not a:
        return b
    if not b:
        return a
    return a if a <= b else b


def _max_iso(a: Optional[str], b: Optional[str]) -> Optional[str]:
    if not a:
        return b
    if not b:
        return a
    return a if a >= b else b


def compute_aggregates() -> dict:
    """Scan all bronze records and return summary + per-dimension rollups."""
    users: dict[str, dict] = defaultdict(lambda: {
        "sessions": 0,
        "sends": 0,
        "first_seen": None,
        "last_seen": None,
        "cases": Counter(),     # case_nbr -> session count
    })
    cases: dict[str, dict] = defaultdict(lambda: {
        "sessions": 0,
        "sends": 0,
        "first_seen": None,
        "last_seen": None,
        "subject": "",
        "issue_type": "",
        "users": Counter(),     # user_name -> session count
    })
    daily: dict[str, dict] = defaultdict(lambda: {
        "sessions": 0,
        "sends": 0,
        "users": set(),
        "cases": set(),
    })

    total_sessions = 0
    total_sends = 0
    all_users: set[str] = set()
    all_cases: set[str] = set()

    for rec in iter_records():
        u = (rec.get("user_name") or "anon").strip() or "anon"
        case = rec.get("case") or {}
        c = str(case.get("case_nbr") or "").strip()
        subj = str(case.get("subject") or "").strip()
        itype = str(case.get("issue_type") or "").strip()
        date = (rec.get("date") or "") or (rec.get("created_at") or "")[:10]
        # send_count is v2+; fall back to message_count (always 0 or 1) for v1.
        sends = int(rec.get("send_count") or rec.get("message_count") or 0) or 1
        created = rec.get("created_at") or ""
        updated = rec.get("updated_at") or created

        total_sessions += 1
        total_sends += sends
        all_users.add(u)
        if c:
            all_cases.add(c)

        ub = users[u]
        ub["sessions"] += 1
        ub["sends"] += sends
        ub["first_seen"] = _min_iso(ub["first_seen"], created)
        ub["last_seen"] = _max_iso(ub["last_seen"], updated)
        if c:
            ub["cases"][c] += 1

        if c:
            cb = cases[c]
            cb["sessions"] += 1
            cb["sends"] += sends
            cb["first_seen"] = _min_iso(cb["first_seen"], created)
            cb["last_seen"] = _max_iso(cb["last_seen"], updated)
            if subj and not cb["subject"]:
                cb["subject"] = subj
            if itype and not cb["issue_type"]:
                cb["issue_type"] = itype
            cb["users"][u] += 1

        if date:
            db = daily[date]
            db["sessions"] += 1
            db["sends"] += sends
            db["users"].add(u)
            if c:
                db["cases"].add(c)

    users_out = {
        u: {
            "sessions": v["sessions"],
            "sends": v["sends"],
            "distinct_cases": len(v["cases"]),
            "first_seen": v["first_seen"],
            "last_seen": v["last_seen"],
            "cases": dict(v["cases"].most_common()),
        }
        for u, v in users.items()
    }
    cases_out = {
        c: {
            "sessions": v["sessions"],
            "sends": v["sends"],
            "distinct_users": len(v["users"]),
            "first_seen": v["first_seen"],
            "last_seen": v["last_seen"],
            "subject": v["subject"],
            "issue_type": v["issue_type"],
            "users": dict(v["users"].most_common()),
        }
        for c, v in cases.items()
    }
    daily_out = {
        d: {
            "sessions": v["sessions"],
            "sends": v["sends"],
            "distinct_users": len(v["users"]),
            "distinct_cases": len(v["cases"]),
        }
        for d, v in sorted(daily.items())
    }

    top_users = sorted(
        (
            {"user_name": u, "sessions": d["sessions"], "sends": d["sends"],
             "distinct_cases": d["distinct_cases"]}
            for u, d in users_out.items()
        ),
        key=lambda x: (x["sessions"], x["sends"]),
        reverse=True,
    )[:20]
    top_cases = sorted(
        (
            {"case_nbr": c, "sessions": d["sessions"], "sends": d["sends"],
             "distinct_users": d["distinct_users"], "subject": d["subject"]}
            for c, d in cases_out.items()
        ),
        key=lambda x: (x["sessions"], x["distinct_users"]),
        reverse=True,
    )[:20]

    summary = {
        "schema_version": GATHER_SCHEMA_VERSION,
        "generated_at": _now_iso(),
        "total_sessions": total_sessions,
        "total_sends": total_sends,
        "distinct_users": len(all_users),
        "distinct_cases": len(all_cases),
        "top_users": top_users,
        "top_cases": top_cases,
    }
    return {
        "summary": summary,
        "users": users_out,
        "cases": cases_out,
        "daily": daily_out,
    }


def _write_json_atomic(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def rebuild_aggregates() -> dict:
    """Regenerate <root>/aggregates/*.json from the bronze layer.

    Returns the summary dict (useful as an HTTP response body).
    """
    aggs = compute_aggregates()
    try:
        root = _resolve_root()
    except Exception as e:
        print(f"[gather] aggregate rebuild: cannot resolve root: {e}")
        return aggs["summary"]
    out_dir = root / "aggregates"
    for name, data in aggs.items():
        try:
            _write_json_atomic(out_dir / f"{name}.json", data)
        except Exception as e:
            print(f"[gather] aggregate write failed ({name}): {e}")
    return aggs["summary"]


def _maybe_rebuild_aggregates_async() -> None:
    """Schedule a background aggregate rebuild, debounced by ``_AGG_COOLDOWN_SEC``."""
    global _last_agg_at
    now = time.time()
    with _agg_lock:
        if now - _last_agg_at < _AGG_COOLDOWN_SEC:
            return
        _last_agg_at = now
    try:
        threading.Thread(target=_safe_rebuild, daemon=True).start()
    except Exception as e:
        print(f"[gather] aggregate dispatch failed: {e}")


def _safe_rebuild() -> None:
    try:
        rebuild_aggregates()
    except Exception as e:
        print(f"[gather] aggregate rebuild failed: {e}")
