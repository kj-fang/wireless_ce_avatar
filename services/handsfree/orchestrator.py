"""
Beacon (handsfree replyer) orchestration: check-now runs and approve-and-post.

One check run at a time (module lock). All state the UI needs lives in
`run_state` (thread-safe copies via get_run_state) and the HandsfreeStore
queue/ledger on disk.

v1 posting policy: ONLY approve_and_post (a human click) ever posts.
The auto_post config flag is reserved for the future confidence-gated mode
and is deliberately not consulted anywhere yet.
"""

from __future__ import annotations

import copy
import re
import threading
import time
import traceback
from pathlib import Path
from typing import Optional

from .composer import compose, AI_MARKER, CHECKLIST_TAG
from .ips_client import IpsClient, PostUnsupported
from .queue import HandsfreeStore
from .runner import HandsfreeRunner


def _store() -> HandsfreeStore:
    from configs.global_configs import app_config
    return HandsfreeStore(Path(app_config.avatarfiles_dir) / "handsfree")


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
        if isinstance(s.get("case_details"), dict):
            s["case_details"] = copy.deepcopy(s["case_details"])
    try:
        from . import scheduler
        s["auto"] = scheduler.status()
    except Exception as e:
        s["auto"] = {"error": f"{type(e).__name__}: {e}"}
    return s

def _set_state(**fields) -> None:
    with _state_lock:
        _run_state.update(fields)


_MAX_CASE_DETAILS = 60


def _update_case_details(case_nbr: str, fields: dict) -> None:
    """Merge structured facts about one case of the current run into the run
    state (the page's live panel). Bounded: the oldest cases drop first."""
    with _state_lock:
        details = _run_state.setdefault("case_details", {})
        entry = details.get(str(case_nbr))
        if entry is None:
            entry = details[str(case_nbr)] = {"case_nbr": str(case_nbr),
                                              "started_at": time.time()}
        entry.update(fields)
        entry["updated_at"] = time.time()
        if len(details) > _MAX_CASE_DETAILS:
            for key in sorted(details, key=lambda k: details[k].get("started_at", 0))[
                    : len(details) - _MAX_CASE_DETAILS]:
                details.pop(key, None)


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
               events=[], cases=[], case_details={}, error=None)

    t = threading.Thread(target=_run_check, args=(owner, store, created_date),
                         daemon=True, name="handsfree-check")
    t.start()
    return {"ok": True, "owner": owner}


def _analyze_and_enqueue(store: HandsfreeStore, case_nbr: str,
                         case_id: str = "", subject: str = "",
                         owner_name: str = "",
                         trial: Optional[bool] = None,
                         case_ctx=None, source: str = "ips") -> Optional[dict]:
    """Shared per-case body for check runs and manual single-case runs.

    trial: the run's captured trial-mode flag (run_auto_scan fixes it once
    per round so a Settings change mid-run cannot turn the closed cases it
    selected into postable drafts); None = read the config now (manual runs).
    """
    def _progress(stage, detail, _c=case_nbr):
        _log_event(f"[{_c}] {stage}: {detail}")

    def _snapshot(fields, _c=case_nbr):
        _update_case_details(_c, fields)

    _update_case_details(case_nbr, {"case_id": case_id, "subject": subject,
                                    "owner_name": owner_name, "finished": False,
                                    "current_stage": "", "draft_id": "",
                                    "fr_draft_id": ""})
    runner = HandsfreeRunner(progress_cb=_progress, snapshot_cb=_snapshot)
    analysis = runner.analyze_case(case_nbr, case_ctx=case_ctx)
    _update_case_details(case_nbr, {"source": source})
    if analysis.mode == "unsupported":
        store.mark_skipped(case_nbr, analysis.error, owner_name=owner_name)
        _log_event(f"[{case_nbr}] skipped: {analysis.error}")
        _update_case_details(case_nbr, {"skipped": analysis.error})
        return None

    fr_rec = None
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
        # The runner's final snapshot predates the fill: push what the
        # checklist concluded (required logs provided / missing, the asks).
        _update_case_details(case_nbr, {
            "checklist_decision": dict(getattr(analysis, "checklist_decision", None) or {}),
            "issue_subcategory": analysis.issue_subcategory,
            "checklist_summary": f"{filled}/{total}"})
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

    draft = compose(analysis)
    rec = store.enqueue(source=source, 
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
    )
    _update_case_details(case_nbr, {"draft_id": rec["draft_id"]})
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
        fr_rec = store.enqueue(source=source, 
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
        )
        _update_case_details(case_nbr, {"fr_draft_id": fr_rec["draft_id"]})
        _log_event(f"[{case_nbr}] queued first-response checklist draft "
                   f"{fr_rec['draft_id']}")
    # Keep the live-panel history with the drafts, so opening a draft later
    # replays how that run went (Charles, 2026-10-20).
    try:
        live = _live_history(case_nbr)
        for d_id in [rec["draft_id"]] + ([fr_rec["draft_id"]] if fr_rec else []):
            store.update(d_id, live=live)
    except Exception as e:
        print(f"[handsfree] live history not saved (continuing): {e}")
    return rec


def _live_history(case_nbr: str) -> dict:
    """Snapshot of the run state for one case: its case_details entry and
    its stage events (what renderLivePanel needs to replay the run)."""
    with _state_lock:
        details = copy.deepcopy((_run_state.get("case_details") or {}).get(str(case_nbr)) or {})
        prefix = f"[{case_nbr}]"
        events = [dict(e) for e in (_run_state.get("events") or [])
                  if str(e.get("message", "")).startswith(prefix)]
        mode = _run_state.get("mode") or ""
        started = _run_state.get("started_at")
    details.setdefault("finished", True)
    return {"saved_at": time.strftime("%Y-%m-%dT%H:%M:%S"), "run_mode": mode,
            "run_started_at": started, "case_details": {str(case_nbr): details},
            "events": events[-200:]}


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
                   started_at=time.time(), events=[], cases=[], case_details={}, error=None)
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
               events=[], cases=[], case_details={}, error=None)
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
# Outlook intake (2026-10-22): mail that names an IPS case
# ---------------------------------------------------------------------------

def run_email_scan(store: HandsfreeStore, *, folder: str = "Inbox", max_cases: int = 3,
                   lookback_days: int = 7, trigger: str = "manual",
                   reply_all: bool = True, on_start=None,
                   own_email: Optional[str] = None) -> dict:
    """One Outlook round (stage 1 of the email intake). New mail received in
    the last `lookback_days` in the folder that names an IPS case → the
    case's EMAIL THREAD (every mail naming it, Inbox + Sent Items) becomes
    the case: earliest mail = description, later mails = comments, their
    files = the uploads (saved under <avatarfiles>/email_<case>), and the
    whole pipeline runs on that, not on IPS. One run per case per round,
    newest mail first. The drafts carry `email` (the newest customer mail,
    the reply target) and source "email", so Approve saves an Outlook reply
    draft and the IPS ledger stays untouched. Mail without a case number is
    remembered and listed as skipped."""
    from datetime import datetime, timedelta
    from . import outlook_source as ol
    from . import email_case as ec

    if not _run_lock.acquire(blocking=False):
        return {"ok": False, "error": "a run is already in progress"}
    source = f"Outlook folder '{folder or 'Inbox'}'"
    started = datetime.now().astimezone()
    summary: dict = {"ok": True, "trigger": trigger, "source": source,
                     "started_at": started.isoformat(timespec="seconds"),
                     "cases": [], "skipped": []}
    try:
        _set_state(status="running", owner=None, mode=f"email-scan ({trigger})",
                   source=source, started_at=time.time(), events=[], cases=[],
                   case_details={}, error=None)
        if on_start is not None:
            on_start()
        since = started - timedelta(days=max(1, int(lookback_days or 7)))
        own = own_email
        if own is None:                            # the signed-in user's UPN
            own = ""
            try:
                from utils.helpers import detect_user_email
                own = detect_user_email() or ""
            except Exception:
                pass
        _log_event(f"email-scan ({trigger}): {source}, mail received since "
                   f"{since.strftime('%Y-%m-%d %H:%M')}")
        with ol.Outlook() as app:
            refs = app.scan(folder or "Inbox", since, own_email=own)
            state = ol.load_state(store.root)
            new = [r for r in refs if not ol.is_processed(state, r.entry_id)]
            triggers = []
            for r in new:
                if not r.case_nbrs:
                    why = "no IPS case number"
                elif ec.is_automated(r.sender_email, r.sender_name, r.subject):
                    why = "automated mail (IPS/Jira notification, auto-reply)"
                elif ec.is_digest(r.case_nbrs):
                    why = f"names {len(set(r.case_nbrs))} cases — a status digest, not a case mail"
                else:
                    triggers.append(r)
                    continue
                summary["skipped"].append({"subject": r.subject[:80], "sender": r.sender_name,
                                           "why": why})
                ol.mark_processed(store.root, r.entry_id)
            work: list = []
            dupes: list = []            # older mails naming a case a newer mail already runs
            seen: set = set()
            for r in triggers:                         # newest first
                for nbr in r.case_nbrs:
                    if nbr not in seen:
                        seen.add(nbr)
                        work.append((r, nbr))
                    else:
                        dupes.append((r, nbr))
            _log_event(f"{len(refs)} mail(s) in the window, {len(new)} new, "
                       f"{len(summary['skipped'])} skipped (no case number / automated / digest), "
                       f"{len(work)} case(s) to run")
            capped = len(work) > max_cases
            if capped:
                _log_event(f"capped to {max_cases} case(s) this round (newest mail first)")
                work = work[:max_cases]
            _set_state(cases=[{"case_nbr": nbr, "subject": r.subject, "email": True}
                              for r, nbr in work])
            failed = 0
            done_cases: set = set()
            thread_root = Path(store.root).parent
            for r, nbr in work:
                _log_event(f"[{nbr}] collecting the email thread — triggered by the mail from "
                           f"{r.sender_name or r.sender_email}: {r.subject[:60]}")
                try:
                    msgs = app.thread(nbr, own_email=own)
                    if not msgs:
                        raise RuntimeError("the mail naming the case was not found again in Outlook")
                    thread_dir = str(thread_root / f"email_{nbr}")
                    for m in msgs:
                        app.save_attachments(m, thread_dir)
                    ctx = ec.build_case_context(nbr, msgs, thread_dir)
                    target = ec.reply_target(msgs, trigger_entry_id=r.entry_id) or msgs[-1]
                    email = {"entry_id": target.entry_id, "store_id": target.store_id,
                             "subject": target.subject, "sender_name": target.sender_name,
                             "sender_email": target.sender_email, "received": target.received,
                             "reply_all": bool(reply_all), "thread_size": len(msgs),
                             "folder": folder or "Inbox"}
                    _update_case_details(nbr, {"email": email, "source": "email",
                                               "subject": ctx.subject})
                    _log_event(f"[{nbr}] email thread: {len(msgs)} mail(s), "
                               f"{len(ctx.attachment_list)} file attachment(s), "
                               f"{len(ctx.image_files)} image(s); reply goes to "
                               f"{target.sender_name or target.sender_email}")
                    rec = _analyze_and_enqueue(store, nbr, subject=ctx.subject,
                                               case_ctx=ctx, source="email")
                    if rec is not None and rec.get("mode") == "error":
                        failed += 1
                        _log_event(f"[{nbr}] FAILED: "
                                   f"{(rec.get('analysis') or {}).get('error') or 'analysis failed'}"
                                   " — the mail is retried next round")
                        summary["cases"].append({"case_nbr": nbr, "subject": r.subject,
                                                 "sender": r.sender_name, "mode": "error"})
                        continue
                    draft_ids = []
                    if rec is not None:
                        draft_ids.append(rec["draft_id"])
                        with _state_lock:
                            fr_id = ((_run_state.get("case_details") or {}).get(nbr) or {}).get("fr_draft_id")
                        if fr_id:
                            draft_ids.append(fr_id)
                        for d_id in draft_ids:
                            store.update(d_id, email=email)
                    ol.mark_processed(store.root, r.entry_id, nbr, draft_ids[0] if draft_ids else "")
                    done_cases.add(nbr)
                    summary["cases"].append({"case_nbr": nbr, "subject": r.subject,
                                             "sender": r.sender_name, "thread": len(msgs),
                                             "mode": rec.get("mode") if rec else "unsupported"})
                except Exception as e:
                    print(f"[handsfree] email-scan case {nbr} failed:\n{traceback.format_exc()}")
                    _log_event(f"[{nbr}] FAILED: {type(e).__name__}: {e}")
                    failed += 1
                    summary["cases"].append({"case_nbr": nbr, "subject": r.subject,
                                             "sender": r.sender_name, "mode": "error"})
        # The case got its analysis from the newest mail (the reply draft
        # answers that one); the older mails naming it are handled too.
        for r, nbr in dupes:
            if nbr in done_cases:
                ol.mark_processed(store.root, r.entry_id, nbr)
        result = f"{len(work)} case(s) run from {len(work)} mail(s)" if work else "no new mail naming a case"
        if capped or failed:
            result += " (capped/failed — retried next round)"
        state = ol.load_state(store.root)
        state["last_scan_at"] = started.isoformat(timespec="seconds")
        ol.save_state(store.root, state)
        store.save_config({"email_last_scan_at": started.isoformat(timespec="seconds"),
                           "email_last_result": result})
        _set_state(status="done", finished_at=time.time())
        _log_event("email-scan complete — review the queue below; Approve saves the reply "
                   "as an Outlook draft")
    except ol.OutlookUnavailable as e:
        summary.update(ok=False, error=str(e))
        store.save_config({"email_last_result": f"failed: {e}"})
        _set_state(status="error", error=str(e), finished_at=time.time())
        _log_event(f"email-scan FAILED: {e}")
    except Exception as e:
        print(f"[handsfree] email-scan failed:\n{traceback.format_exc()}")
        summary.update(ok=False, error=f"{type(e).__name__}: {e}")
        store.save_config({"email_last_result": f"failed: {type(e).__name__}: {e}"})
        _set_state(status="error", error=f"{type(e).__name__}: {e}", finished_at=time.time())
        _log_event(f"email-scan FAILED: {e}")
    finally:
        _run_lock.release()
    return summary

def start_email_scan(**settings) -> dict:
    """UI 'Check Outlook now': the round in a worker thread; answers once the
    worker holds the run lock (same handshake as the automatic Run now)."""
    from . import scheduler
    if _run_lock.locked():
        return {"ok": False, "error": "a run is already in progress"}
    store = _store()
    cfg = store.load_config()
    opts = scheduler.email_settings(cfg)
    started = threading.Event()
    outcome: dict = {}

    def _worker():
        try:
            outcome["res"] = run_email_scan(store, trigger="manual", on_start=started.set, **opts)
        finally:
            started.set()

    threading.Thread(target=_worker, daemon=True, name="handsfree-email-now").start()
    started.wait(timeout=15)
    res = outcome.get("res")
    if res is not None and not res.get("ok"):
        return {"ok": False, "error": res.get("error") or "run did not start"}
    return {"ok": True}


def _save_outlook_reply(store: HandsfreeStore, cfg: dict, draft_id: str,
                        rec: dict, email: dict) -> dict:
    """Approve for a mail-triggered draft: the text becomes a reply DRAFT in
    Outlook (nothing is sent, nothing goes to IPS, the IPS ledger is left
    alone)."""
    from . import outlook_source as ol
    from .composer import compose_html
    from .ips_client import PostResult
    body_html = rec.get("draft_html") or compose_html(rec.get("draft_plain") or "")
    reply_all = bool(email.get("reply_all", cfg.get("email_reply_all", True)))
    try:
        with ol.Outlook() as app:
            saved = app.save_reply_draft(email, ol.reply_fragment(body_html),
                                         reply_all=reply_all,
                                         display=bool(cfg.get("email_display_draft")))
    except ol.OutlookUnavailable as e:
        err = str(e)
    except Exception as e:
        err = f"{type(e).__name__}: {e}"
    else:
        result = PostResult(ok=True, backend="outlook", comment_id=saved.get("entry_id", ""),
                            detail={"folder": "Drafts", "to": saved.get("to", ""),
                                    "subject": saved.get("subject", ""),
                                    "reply_all": reply_all,
                                    "in_reply_to": email.get("subject", "")})
        store.update(draft_id, status="posted", post_result=result.to_dict())
        return {"ok": True, "draft": store.get(draft_id), "result": result.to_dict()}
    store.update(draft_id, status="post_failed",
                 post_result={"ok": False, "backend": "outlook", "error": err})
    return {"ok": False, "error": err, "draft": store.get(draft_id)}


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

    Serialized by _post_lock: the duplicate guard below is check-then-act
    (scan existing comments, then insert), so two near-simultaneous approvals
    interleaving could both pass the scan and double-post. Posting is a
    human-click-rate operation — a coarse lock costs nothing."""
    with _post_lock:
        return _approve_and_post_locked(draft_id, edited_plain)


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

    # A mail-triggered draft replies to the mail, in Outlook, as a draft.
    if rec.get("email"):
        return _save_outlook_reply(store, cfg, draft_id, rec, rec["email"])

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
