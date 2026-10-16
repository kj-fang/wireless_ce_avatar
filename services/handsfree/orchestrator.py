"""
Handsfree Replyer orchestration: check-now runs and approve-and-post.

One check run at a time (module lock). All state the UI needs lives in
`run_state` (thread-safe copies via get_run_state) and the HandsfreeStore
queue/ledger on disk.

v1 posting policy: ONLY approve_and_post (a human click) ever posts.
The auto_post config flag is reserved for the future confidence-gated mode
and is deliberately not consulted anywhere yet.
"""

from __future__ import annotations

import re
import threading
import time
import traceback
from pathlib import Path
from typing import Optional

from .composer import compose, AI_MARKER, CHECKLIST_TAG
from .ips_client import IpsClient, PostUnsupported
from .queue import HandsfreeStore, open_store
from .runner import HandsfreeRunner


def _store() -> HandsfreeStore:
    return open_store()


_run_lock = threading.Lock()
_run_state: dict = {"status": "idle"}
_state_lock = threading.Lock()


def get_run_state() -> dict:
    with _state_lock:
        s = dict(_run_state)
        if isinstance(s.get("events"), list):
            s["events"] = [dict(e) for e in s["events"]]
        if isinstance(s.get("cases"), list):
            s["cases"] = [dict(c) for c in s["cases"]]
    try:
        from . import scheduler
        s["auto"] = scheduler.status()
    except Exception as e:
        s["auto"] = {"error": f"{type(e).__name__}: {e}"}
    return s

def _set_state(**fields) -> None:
    with _state_lock:
        _run_state.update(fields)


def _log_event(message: str) -> None:
    with _state_lock:
        events = _run_state.setdefault("events", [])
        events.append({"ts": time.strftime("%H:%M:%S"), "message": message})
        if len(events) > 200:
            del events[: len(events) - 200]
    print(f"[handsfree] {message}")


def start_check_now(owner_name: Optional[str] = None,
                    created_date: str = "TODAY") -> dict:
    """Kick off a background check run. Returns {ok, error?}."""
    if not _run_lock.acquire(blocking=False):
        return {"ok": False, "error": "a check run is already in progress"}
    store = _store()
    cfg = store.load_config()
    owner = (owner_name or "").strip() or cfg.get("owner_name") or ""
    if not owner:
        _run_lock.release()
        return {"ok": False, "error": "no owner name configured"}
    if owner_name and owner != cfg.get("owner_name"):
        store.save_config({"owner_name": owner})

    _set_state(status="running", owner=owner, created_date=created_date,
               started_at=time.time(),
               events=[], cases=[], error=None)

    t = threading.Thread(target=_run_check, args=(owner, store, created_date),
                         daemon=True, name="handsfree-check")
    t.start()
    return {"ok": True, "owner": owner}


def _analyze_and_enqueue(store: HandsfreeStore, case_nbr: str,
                         case_id: str = "", subject: str = "",
                         owner_name: str = "",
                         trial: Optional[bool] = None) -> Optional[dict]:
    """Shared per-case body for check runs and manual single-case runs.

    trial: the run's captured trial-mode flag (run_auto_scan fixes it once
    per round so a Settings change mid-run cannot turn the closed cases it
    selected into postable drafts); None = read the config now (manual runs).
    """
    def _progress(stage, detail, _c=case_nbr):
        _log_event(f"[{_c}] {stage}: {detail}")

    runner = HandsfreeRunner(progress_cb=_progress)
    analysis = runner.analyze_case(case_nbr)
    if analysis.mode == "unsupported":
        store.mark_skipped(case_nbr, analysis.error, owner_name=owner_name)
        _log_event(f"[{case_nbr}] skipped: {analysis.error}")
        return None

    # Pre-fill the first-response checklist: pipeline facts first, then one
    # LLM pass over the rest. Failure never blocks the drafts.
    try:
        from .checklist import build_fills
        from configs.global_configs import app_config
        analysis.checklist_fills = build_fills(
            analysis, llm=getattr(app_config, "llm_helper", None))
        filled = sum(1 for sec in analysis.checklist_fills.values()
                     for e in sec if e.get("provided"))
        total = sum(len(sec) for sec in analysis.checklist_fills.values())
        _log_event(f"[{case_nbr}] first response: domain "
                   f"'{analysis.issue_domain or 'Others'}', pre-filled "
                   f"{filled}/{total} checklist items")
    except Exception as e:
        print(f"[handsfree] checklist fill failed (continuing): {e}")

    # The customer-facing overview checklist goes out once per case: later
    # rounds (customer replied, new logs) get targeted asks only.
    analysis.first_response_done = store.first_response_posted(case_nbr)

    # Trial mode (Settings): tuning on historical / closed cases — every
    # draft is flagged and can never be posted.
    if trial is None:
        trial = bool(store.load_config().get("trial_mode"))
    trial = bool(trial)

    shared_log_path = _share_decoded_log(store, case_nbr, analysis.log_path)

    draft = compose(analysis)
    rec = store.enqueue(
        case_nbr=case_nbr,
        case_id=case_id or analysis.case_id,
        subject=subject or analysis.subject,
        draft_plain=draft["plain"],
        draft_html=draft["html"],
        confidence=draft["confidence"],
        mode=analysis.mode,
        analysis=analysis.to_dict(),
        owner_name=owner_name,
        trial=trial,
        shared_log_path=shared_log_path,
    )
    _log_event(
        f"[{case_nbr}] queued draft {rec['draft_id']} "
        f"(mode={analysis.mode}, confidence={draft['confidence']}"
        + (", TRIAL — posting disabled" if trial else "") + ")")

    # Every case also gets a first-response checklist reply — except when the
    # primary draft already IS the first response (the request modes render
    # the same pre-filled checklist themselves), the case could not even be
    # fetched (mode "error": nothing to base a customer reply on), Intel is
    # waiting on the customer (nothing to ask), or the overview was already
    # sent in an earlier round.
    if (analysis.mode not in ("request_logs", "request_info", "error",
                              "waiting_customer")
            and not analysis.first_response_done):
        from dataclasses import replace
        fr = replace(analysis, mode="first_response")
        fr_draft = compose(fr)
        fr_rec = store.enqueue(
            case_nbr=case_nbr,
            case_id=case_id or analysis.case_id,
            subject=subject or analysis.subject,
            draft_plain=fr_draft["plain"],
            draft_html=fr_draft["html"],
            confidence=None,
            mode="first_response",
            # fr, not analysis: the review UI reads analysis.mode for the
            # "public reply to customer" visibility chip.
            analysis=fr.to_dict(),
            trial=trial,
            owner_name=owner_name,
            shared_log_path=shared_log_path,
        )
        _log_event(f"[{case_nbr}] queued first-response checklist draft "
                   f"{fr_rec['draft_id']}")
    return rec


def _share_decoded_log(store: HandsfreeStore, case_nbr: str, log_path: str) -> str:
    """Shared copy of the decoded log, or "" (no log, or the copy failed —
    the draft then simply has no Open-folder button)."""
    import os
    if not log_path or not os.path.isfile(log_path):
        return ""
    try:
        shared = store.share_log(case_nbr, log_path)
        _log_event(f"[{case_nbr}] decoded log shared: {shared}")
        return shared
    except Exception as e:
        _log_event(f"[{case_nbr}] could not share the decoded log: {type(e).__name__}: {e}")
        return ""


def _run_check(owner: str, store: HandsfreeStore,
               created_date: str = "TODAY") -> None:
    try:
        cfg = store.load_config()
        max_cases = int(cfg.get("max_cases_per_run") or 3)

        _log_event(f"querying IPS for cases assigned to '{owner}' "
               f"created {created_date.lower()}…")
        ips = IpsClient()
        refs = ips.find_new_cases(owner, created_date=created_date)
        _log_event(f"found {len(refs)} case(s) created {created_date.lower()} "
               "for this owner")

        fresh = [r for r in refs if not store.is_processed(r.case_nbr)]
        skipped = len(refs) - len(fresh)
        if skipped:
            _log_event(f"skipping {skipped} already-processed case(s)")
        fresh = fresh[:max_cases]
        _set_state(cases=[r.to_dict() for r in fresh])

        for ref in fresh:
            _log_event(f"analyzing case {ref.case_nbr} — {ref.subject[:60]}")
            _analyze_and_enqueue(store, ref.case_nbr,
                                 case_id=ref.case_id, subject=ref.subject,
                                 owner_name=owner)

        _set_state(status="done", finished_at=time.time())
        _log_event("check run complete — review the queue below")
    except Exception as e:
        print(f"[handsfree] check run failed:\n{traceback.format_exc()}")
        _set_state(status="error", error=f"{type(e).__name__}: {e}",
                   finished_at=time.time())
        _log_event(f"check run FAILED: {e}")
    finally:
        _run_lock.release()


# ---------------------------------------------------------------------------
# automatic scan: new cases + customer updates on open cases
# ---------------------------------------------------------------------------

def _parse_iso(s: str):
    """Salesforce ('2026-10-05T10:07:12.000+0000') and our own local ISO
    stamps -> aware datetime; None when unparseable."""
    from datetime import datetime, timezone
    raw = str(s or "").strip()
    if not raw:
        return None
    raw = re.sub(r"([+-]\d{2})(\d{2})$", r"\1:\2", raw.replace("Z", "+00:00"))
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def select_auto_work(open_cases: list, updates: dict, ledger_lookup,
                     since_iso: str) -> list[tuple]:
    """Which open cases need a round, newest first. Pure (unit-testable).

    open_cases:    [CaseRef]           updates: {case_id: latest customer
    comment ISO}   ledger_lookup(case_nbr) -> ledger entry dict
    Returns [(CaseRef, reason, update_iso)] — a case qualifies on EITHER
    condition (new case OR new customer comment), not both:
      * a case created after `since_iso` that was never analyzed -> new case
      * a customer comment newer than the case's last analysis (and not the
        update already run for) -> customer update
    """
    since = _parse_iso(since_iso)
    work = []
    for ref in open_cases:
        entry = ledger_lookup(ref.case_nbr) or {}
        upd = updates.get(ref.case_id, "")
        analyzed = _parse_iso(entry.get("analyzed_at", ""))
        if upd and upd != entry.get("last_customer_update"):
            upd_dt = _parse_iso(upd)
            if analyzed is None or (upd_dt and upd_dt > analyzed):
                work.append((ref, f"customer update at {upd}", upd))
                continue
        created = _parse_iso(ref.created)
        if not entry and created and since and created >= since:
            work.append((ref, "new case", ref.created))
    work.sort(key=lambda w: w[2], reverse=True)
    return work


def _is_closed(ref) -> bool:
    """Closed state of a CaseRef: IsClosed when the client could query it,
    else the Status text."""
    closed = getattr(ref, "closed", None)
    if closed is not None:
        return bool(closed)
    return str(getattr(ref, "status", "") or "").strip().lower().startswith("closed")


def select_list_view_work(refs: list, updates: dict, ledger_lookup,
                          include_closed: bool) -> tuple:
    """Which cases of an IPS list view need a round, in list order. Pure.

    A case qualifies when it was never analyzed (new to the list) or a
    customer comment is newer than its last analysis (same rule as the
    owner source). Closed cases are skipped unless include_closed (trial
    mode in Settings); the number skipped comes back too so the run log can
    say why nothing ran. Returns ([(CaseRef, reason, stamp)], skipped_closed).
    """
    work, skipped_closed = [], 0
    for ref in refs:
        if not include_closed and _is_closed(ref):
            skipped_closed += 1
            continue
        entry = ledger_lookup(ref.case_nbr) or {}
        if not entry:
            work.append((ref, "not analyzed yet", ref.created))
            continue
        upd = updates.get(ref.case_id, "")
        if upd and upd != entry.get("last_customer_update"):
            upd_dt = _parse_iso(upd)
            analyzed = _parse_iso(entry.get("analyzed_at", ""))
            if analyzed is None or (upd_dt and upd_dt > analyzed):
                work.append((ref, f"customer update at {upd}", upd))
    return work, skipped_closed


def run_auto_scan(store: HandsfreeStore, owner: str = "", max_cases: int = 10,
                  trigger: str = "scheduled", list_view: str = "",
                  on_start=None, list_views: Optional[list] = None) -> dict:
    """One automatic round. Source: the owner's open cases with new customer
    activity since the last scan (default), or — when list_views (or the
    single list_view) is given — the union of the cases of those IPS list
    views (in list order, each case once) not yet analyzed or with new
    customer comments (closed ones only in trial mode). The per-case pipeline runs
    for each; drafts land in the review queue — nothing posts. Remembers the
    scan time per source. Returns a summary; {"ok": False} when a run is
    already in progress. on_start() is called once the run holds the lock
    and the state says "running" (the UI's Run-now handshake)."""
    from datetime import datetime, timedelta, timezone

    if not _run_lock.acquire(blocking=False):
        return {"ok": False, "error": "a run is already in progress"}
    started = datetime.now(timezone.utc)
    owner = (owner or "").strip()
    views: list = []
    for v in list(list_views or []) + [list_view]:
        name = str(v or "").strip()
        if name and name not in views:
            views.append(name)
    list_view = views[0] if views else ""          # truthy = list-view source
    source = (("IPS list view " if len(views) == 1 else "IPS list views ")
              + ", ".join(f"'{v}'" for v in views)) if views else f"open cases of '{owner}'"
    summary: dict = {"ok": True, "trigger": trigger, "started_at": started.isoformat(),
                     "source": source, "cases": []}
    try:
        _set_state(status="running", owner=owner or None, mode=f"auto-scan ({trigger})",
                   started_at=time.time(), events=[], cases=[], error=None)
        if on_start is not None:
            on_start()
        cfg = store.load_config()
        trial = bool(cfg.get("trial_mode"))
        scan_times = dict(cfg.get("auto_last_scan_by_owner") or {})
        scan_key = ("listview:" + "|".join(sorted(v.casefold() for v in views))
                    if views else owner.casefold())
        # Older configs kept one stamp, valid only for the configured owner.
        legacy_since = (cfg.get("auto_last_scan_at") or ""
                        if not list_view
                        and owner.casefold() == str(cfg.get("owner_name") or "").casefold()
                        else "")
        since_iso = scan_times.get(scan_key) or legacy_since or (
            started - timedelta(hours=24)).strftime("%Y-%m-%dT%H:%M:%SZ")
        _log_event(f"auto-scan ({trigger}): {source}, customer activity since {since_iso}")
        ips = IpsClient()
        if list_view:
            refs, seen_nbrs = [], set()
            for view in views:
                view_refs = ips.list_view_cases(view)
                fresh = [r for r in view_refs if r.case_nbr not in seen_nbrs]
                seen_nbrs.update(r.case_nbr for r in fresh)
                refs.extend(fresh)
                if len(views) > 1:
                    _log_event(f"list view '{view}': {len(view_refs)} case(s), "
                               f"{len(fresh)} not already listed by an earlier view")
            updates = ips.find_customer_updates([c.case_id for c in refs], since_iso)
            work, skipped_closed = select_list_view_work(
                refs, updates, store.ledger_entry, include_closed=trial)
            line = (f"{len(refs)} case(s) in "
                    + ("the list view" if len(views) == 1 else f"{len(views)} list views")
                    + f", {len(updates)} with customer comments since then, {len(work)} to run")
            if skipped_closed:
                line += (f" — {skipped_closed} closed case(s) skipped (enable trial mode "
                         "in Settings to include closed cases)")
            if trial:
                line += " — trial mode: closed cases included, drafts cannot be posted"
            _log_event(line)
        else:
            open_cases = ips.find_open_cases(owner)
            updates = ips.find_customer_updates([c.case_id for c in open_cases], since_iso)
            work = select_auto_work(open_cases, updates, store.ledger_entry, since_iso)
            _log_event(f"{len(open_cases)} open case(s), {len(updates)} with customer "
                       f"comments since then, {len(work)} to run")
        capped = len(work) > max_cases
        if capped:
            _log_event(f"capped to {max_cases} case(s) this round"
                       + ("" if list_view else " (newest first)"))
            work = work[:max_cases]
        _set_state(cases=[w[0].to_dict() for w in work])
        failed = 0
        for ref, reason, upd in work:
            _log_event(f"analyzing case {ref.case_nbr} — {reason} — {ref.subject[:60]}")
            try:
                rec = _analyze_and_enqueue(store, ref.case_nbr,
                                           case_id=ref.case_id, subject=ref.subject,
                                           owner_name=owner or getattr(ref, "owner_name", ""),
                                           trial=trial)
                if rec is not None and rec.get("mode") == "error":
                    # The runner could not even fetch the case (IPS/Snowflake
                    # hiccup): the draft only tells the reviewer. The update
                    # stays unconsumed and the ledger untouched (enqueue skips
                    # it for error drafts), so the next round retries.
                    failed += 1
                    _log_event(f"[{ref.case_nbr}] FAILED: "
                               f"{(rec.get('analysis') or {}).get('error') or 'case fetch failed'}"
                               " — retried next round")
                    summary["cases"].append({"case_nbr": ref.case_nbr, "reason": reason,
                                             "mode": "error"})
                    continue
                if upd and reason.startswith("customer update"):
                    store.mark_customer_update(ref.case_nbr, upd)
                summary["cases"].append({"case_nbr": ref.case_nbr, "reason": reason,
                                         "mode": rec.get("mode") if rec else "unsupported"})
            except Exception as e:
                print(f"[handsfree] auto-scan case {ref.case_nbr} failed:\n"
                      f"{traceback.format_exc()}")
                _log_event(f"[{ref.case_nbr}] FAILED: {type(e).__name__}: {e}")
                failed += 1
                summary["cases"].append({"case_nbr": ref.case_nbr, "reason": reason,
                                         "mode": "error"})
        result = f"{len(work)} case(s) run"
        if capped or failed:
            # Keep the window: the next round re-queries it and the ledger skips finished cases.
            result += " (capped/failed — window kept for the next round)"
            store.save_config({"auto_last_result": result})
        else:
            stamp = started.strftime("%Y-%m-%dT%H:%M:%SZ")
            scan_times = dict(store.load_config().get("auto_last_scan_by_owner") or {})
            scan_times[scan_key] = stamp
            updates_cfg = {"auto_last_scan_by_owner": scan_times, "auto_last_result": result}
            if not list_view:
                updates_cfg["auto_last_scan_at"] = stamp
            store.save_config(updates_cfg)
        _set_state(status="done", finished_at=time.time())
        _log_event("auto-scan complete — review the queue below"
                   + (" (trial drafts cannot be posted)" if trial else ""))
    except Exception as e:
        print(f"[handsfree] auto-scan failed:\n{traceback.format_exc()}")
        summary.update(ok=False, error=f"{type(e).__name__}: {e}")
        store.save_config({"auto_last_result": f"failed: {type(e).__name__}: {e}"})
        _set_state(status="error", error=f"{type(e).__name__}: {e}",
                   finished_at=time.time())
        _log_event(f"auto-scan FAILED: {e}")
    finally:
        _run_lock.release()
    return summary


def start_case_run(case_nbr: str) -> dict:
    """Manual trigger: analyze ONE explicitly chosen case number.

    Bypasses owner/today detection AND the processed-case ledger (an explicit
    request means the user wants a fresh analysis even if the case was seen
    before) — duplicate-POST protection still applies at approve time.
    """
    from utils.check_ips_utils import normalise_ips
    raw = str(case_nbr or "").strip()
    case_nbr = normalise_ips(raw)
    if not case_nbr:
        return {"ok": False,
                "error": (f"{raw!r} is not an IPS case number (six to eight digits, "
                          "e.g. 01009610)") if raw else "empty case number"}
    if not _run_lock.acquire(blocking=False):
        return {"ok": False, "error": "a run is already in progress"}

    store = _store()
    _set_state(status="running", owner=None, mode="single-case",
               case_nbr=case_nbr, started_at=time.time(),
               events=[], cases=[], error=None)
    if store.is_processed(case_nbr):
        _log_event(f"note: case {case_nbr} was analyzed before — re-running "
                   "on explicit request (approve-time duplicate guard still active)")

    def _run():
        try:
            _log_event(f"manual run: analyzing case {case_nbr}")
            _analyze_and_enqueue(store, case_nbr)
            _set_state(status="done", finished_at=time.time())
            _log_event("manual run complete — review the queue below")
        except Exception as e:
            print(f"[handsfree] manual run failed:\n{traceback.format_exc()}")
            _set_state(status="error", error=f"{type(e).__name__}: {e}",
                       finished_at=time.time())
            _log_event(f"manual run FAILED: {e}")
        finally:
            _run_lock.release()

    threading.Thread(target=_run, daemon=True, name="handsfree-case").start()
    return {"ok": True, "case_nbr": case_nbr}


# ---------------------------------------------------------------------------
# retention
# ---------------------------------------------------------------------------

# A log folder this fresh may belong to a draft being queued right now on
# another machine, so it is never removed as "orphaned".
_LOG_GRACE_S = 24 * 3600


def cleanup_queue(store: HandsfreeStore) -> dict:
    """Remove drafts older than retention_days, every draft of a case IPS
    reports closed, and shared log folders no remaining draft uses. The
    ledger is kept, so a cleaned case is never re-analyzed as "new"; a
    removed draft can always be produced again by re-running the case."""
    from datetime import datetime, timedelta
    from .ips_client import CaseRef

    days = int(store.load_config().get("retention_days") or 30)
    cutoff = datetime.now().astimezone() - timedelta(days=days)
    drafts = store.all_drafts()

    closed: set = set()
    refs = {d["case_id"]: CaseRef(case_nbr=str(d["case_nbr"]), case_id=d["case_id"])
            for d in drafts if d.get("case_id") and d.get("case_nbr")}
    if refs:
        try:
            IpsClient()._fill_case_states(list(refs.values()))
            closed = {r.case_nbr for r in refs.values() if r.closed}
        except Exception as e:
            # Without the IPS state only the age rule applies this time.
            print(f"[handsfree] cleanup: case state lookup failed ({e}); age rule only")

    removed, kept_cases = 0, set()
    for d in drafts:
        created = _parse_iso(d.get("created_at", ""))
        if str(d.get("case_nbr")) in closed or (created and created < cutoff):
            store.delete_draft(str(d.get("draft_id") or ""))
            removed += 1
        else:
            kept_cases.add(str(d.get("case_nbr")))

    logs_removed = 0
    for case_dir in store.case_log_dirs():
        fresh = time.time() - case_dir.stat().st_mtime < _LOG_GRACE_S
        if case_dir.name not in kept_cases and (case_dir.name in closed or not fresh):
            store.delete_case_logs(case_dir.name)
            logs_removed += 1
    return {"drafts_removed": removed, "log_folders_removed": logs_removed,
            "closed_cases": sorted(closed), "retention_days": days}


# ---------------------------------------------------------------------------
# posting
# ---------------------------------------------------------------------------

_post_lock = threading.Lock()

# Customer-facing first-response family (posted PUBLIC, tagged CHECKLIST_TAG).
_CHECKLIST_MODES = ("request_logs", "request_info", "first_response")

# request_logs / request_info replies posted before CHECKLIST_TAG existed
# carry only AI_MARKER — recognized by their fixed template sentences.
_LEGACY_REQUEST_PHRASES = (
    "we need the Intel wireless driver WRT logs covering",
    "To start the analysis we need some additional information about the",
)


def _is_checklist_comment(body: str) -> bool:
    """Is this posted AI comment of the first-response family (vs analysis)?"""
    if CHECKLIST_TAG in body:
        return True
    return any(p in body for p in _LEGACY_REQUEST_PHRASES)


def approve_and_post(draft_id: str, edited_plain: Optional[str] = None) -> dict:
    """Human clicked Approve. Post the (possibly edited) draft to IPS.
    Returns the updated draft record + result.

    The duplicate guard below is check-then-act (scan existing comments,
    then insert), so two near-simultaneous approvals interleaving could both
    pass the scan and double-post. _post_lock serializes this process; the
    case claim on the shared folder serializes reviewers on other machines."""
    with _post_lock:
        store = _store()
        rec = store.get(draft_id)
        if rec is None:
            return {"ok": False, "error": f"draft not found: {draft_id}"}
        case_nbr = rec["case_nbr"]
        holder = store.claim_case(case_nbr, _reviewer_id())
        if holder:
            return {"ok": False, "error": f"{holder} is posting to case {case_nbr} "
                                          "right now — refresh and try again in a moment"}
        try:
            return _approve_and_post_locked(draft_id, edited_plain)
        finally:
            store.release_case(case_nbr)


def _reviewer_id() -> str:
    import getpass
    import socket
    try:
        user = getpass.getuser()
    except Exception:
        user = "unknown"
    return f"{user}@{socket.gethostname()}"


def _approve_and_post_locked(draft_id: str, edited_plain: Optional[str]) -> dict:
    store = _store()
    cfg = store.load_config()
    rec = store.get(draft_id)
    if rec is None:
        return {"ok": False, "error": f"draft not found: {draft_id}"}
    if rec.get("status") == "posted":
        return {"ok": False, "error": "draft already posted", "draft": rec}
    if cfg.get("dry_run"):
        return {"ok": False, "error": "dry_run is enabled in config — posting disabled"}
    if rec.get("trial"):
        return {"ok": False, "error": "trial draft (tuning run on a historical case) — "
                                      "posting is disabled for trial drafts", "draft": rec}

    draft_is_checklist = rec.get("mode") in _CHECKLIST_MODES

    # Apply reviewer edits. Both markers are restored when edited away: the
    # duplicate scan below (and every later one) tells the comment families
    # apart by them.
    if edited_plain is not None and edited_plain.strip():
        from .composer import compose_html
        plain = edited_plain
        if AI_MARKER not in plain:
            plain = AI_MARKER + "\n\n" + plain
        if draft_is_checklist and CHECKLIST_TAG not in plain:
            plain = plain.replace(AI_MARKER, AI_MARKER + "\n" + CHECKLIST_TAG, 1)
        rec = store.update(draft_id, draft_plain=plain,
                           draft_html=compose_html(plain))

    ips = IpsClient()

    # Duplicate policy (2026-10-06): analyses may post once per round (a
    # case gets a new round whenever the customer replies), request replies
    # are per-round asks — neither is blocked. The customer-facing OVERVIEW
    # (first_response) goes out once per case, tracked in our own ledger
    # rather than by the body markers (reviewers edit the text). The IPS
    # scan for the family tag only backfills the ledger for cases whose
    # overview was posted before the ledger tracked it; a failed scan does
    # not block.
    # A request draft composed while no first response was recorded embeds
    # the overview too (analysis.first_response_done False at compose time).
    # Later-round request drafts (True) carry only their targeted ask.
    embeds_overview = (rec.get("mode") == "first_response"
                       or (draft_is_checklist
                           and not (rec.get("analysis") or {}).get("first_response_done")))
    if embeds_overview:
        sent = store.first_response_posted(rec["case_nbr"])
        if not sent:
            try:
                for c in ips.get_case_comments(rec["case_id"]):
                    body = str(c.get(IpsClient.FIELD_RICH_BODY) or "")
                    if AI_MARKER in body and _is_checklist_comment(body):
                        store.mark_posted(rec["case_nbr"], comment_id=c.get("Id", ""),
                                          first_response=True)
                        sent = True
                        break
            except Exception as e:
                print(f"[handsfree] first-response scan skipped ({type(e).__name__}: {e})")
        if sent and rec.get("mode") == "first_response":
            store.update(draft_id, status="posted",
                         post_result={"ok": False, "backend": "none",
                                      "error": "first response already sent on this case"})
            return {"ok": False,
                    "error": "the first-response overview was already sent on this "
                             "case (it goes out once); marked as posted",
                    "draft": store.get(draft_id)}
        if sent:
            # Stale first-round request draft (e.g. a manual rerun queued two
            # before either posted): posting it would send the overview a
            # second time. A fresh round composes the targeted ask only.
            err = ("the first-response overview was already sent on this case and "
                   "this first-round request draft embeds it again — rejected; "
                   "re-run the case for a targeted request")
            store.update(draft_id, status="rejected",
                         post_result={"ok": False, "backend": "none", "error": err})
            return {"ok": False, "error": err, "draft": store.get(draft_id)}

    backend = (cfg.get("post_backend") or "auto").lower()
    result = None

    # First-response-family drafts are customer-facing: post PUBLIC
    # (visible to the customer). Everything else stays Private-to-Intel.
    is_public_reply = draft_is_checklist

    if backend in ("rest", "auto"):
        try:
            result = ips.post_comment(rec["case_id"], rec["draft_html"],
                                      plain_body=rec.get("draft_plain") or "",
                                      field_map=cfg.get("rest_field_map"),
                                      private=not is_public_reply)
        except PostUnsupported as e:
            print(f"[handsfree] REST posting unsupported: {e}")
            if backend == "rest" or is_public_reply:
                store.update(draft_id, status="post_failed",
                             post_result={"ok": False, "backend": "rest", "error": str(e)})
                return {"ok": False, "error": str(e), "draft": store.get(draft_id)}
            result = None   # fall through to UI

    if result is None or (not result.ok and backend == "auto"):
        if is_public_reply:
            # The Selenium fallback drives the Private-to-Intel UI flow — it
            # cannot post a public reply. REST-only for customer-facing posts.
            err = (result.error if result else
                   "REST backend unavailable — public replies post via REST only")
            store.update(draft_id, status="post_failed",
                         post_result={"ok": False, "backend": "rest", "error": err})
            return {"ok": False, "error": err, "draft": store.get(draft_id)}
        from .ui_commenter import UiCommenter
        ui = UiCommenter(locators=cfg.get("ui_locators"))
        result = ui.post_comment(rec["case_id"], rec["draft_plain"])

    if result.ok:
        store.update(draft_id, status="posted", post_result=result.to_dict())
        # A request reply carries the overview checklist only on the first
        # round, so it counts as the first response exactly when no first
        # response was recorded before (mark_posted keeps the first record).
        store.mark_posted(rec["case_nbr"], comment_id=result.comment_id,
                          first_response=draft_is_checklist)
        return {"ok": True, "draft": store.get(draft_id), "result": result.to_dict()}

    store.update(draft_id, status="post_failed", post_result=result.to_dict())
    return {"ok": False, "error": result.error, "draft": store.get(draft_id)}


def reject(draft_id: str, reason: str = "") -> dict:
    store = _store()
    rec = store.update(draft_id, status="rejected",
                       post_result={"ok": False, "backend": "none",
                                    "error": f"rejected by reviewer: {reason}"})
    if rec is None:
        return {"ok": False, "error": f"draft not found: {draft_id}"}
    return {"ok": True, "draft": rec}


def discover_rest_fields() -> dict:
    """Run the one-time describe so the engineer can pick the Private-to-Intel
    field; the choice is saved to config by the route."""
    ips = IpsClient()
    return ips.describe_comment_fields()
