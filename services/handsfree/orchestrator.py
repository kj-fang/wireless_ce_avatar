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


def start_check_now(owner_name: Optional[str] = None) -> dict:
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

    _set_state(status="running", owner=owner, started_at=time.time(),
               events=[], cases=[], error=None)

    t = threading.Thread(target=_run_check, args=(owner, store),
                         daemon=True, name="handsfree-check")
    t.start()
    return {"ok": True, "owner": owner}


def _analyze_and_enqueue(store: HandsfreeStore, case_nbr: str,
                         case_id: str = "", subject: str = "") -> dict:
    """Shared per-case body for check runs and manual single-case runs."""
    def _progress(stage, detail, _c=case_nbr):
        _log_event(f"[{_c}] {stage}: {detail}")

    runner = HandsfreeRunner(progress_cb=_progress)
    analysis = runner.analyze_case(case_nbr)

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
    )
    _log_event(
        f"[{case_nbr}] queued draft {rec['draft_id']} "
        f"(mode={analysis.mode}, confidence={draft['confidence']})")

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
        )
        _log_event(f"[{case_nbr}] queued first-response checklist draft "
                   f"{fr_rec['draft_id']}")
    return rec


def _run_check(owner: str, store: HandsfreeStore) -> None:
    try:
        cfg = store.load_config()
        max_cases = int(cfg.get("max_cases_per_run") or 3)

        _log_event(f"querying IPS for cases assigned to '{owner}' today…")
        ips = IpsClient()
        refs = ips.find_new_cases(owner)
        _log_event(f"found {len(refs)} case(s) created today for this owner")

        fresh = [r for r in refs if not store.is_processed(r.case_nbr)]
        skipped = len(refs) - len(fresh)
        if skipped:
            _log_event(f"skipping {skipped} already-processed case(s)")
        fresh = fresh[:max_cases]
        _set_state(cases=[r.to_dict() for r in fresh])

        for ref in fresh:
            _log_event(f"analyzing case {ref.case_nbr} — {ref.subject[:60]}")
            _analyze_and_enqueue(store, ref.case_nbr,
                                 case_id=ref.case_id, subject=ref.subject)

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


def run_auto_scan(store: HandsfreeStore, owner: str, max_cases: int = 10,
                  trigger: str = "scheduled") -> dict:
    """One automatic round: find the owner's open cases with new customer
    activity since the last scan, run the per-case pipeline for each (drafts
    land in the review queue — nothing posts), remember the scan time.
    Returns a summary; {"ok": False} when a run is already in progress."""
    from datetime import datetime, timedelta, timezone

    if not _run_lock.acquire(blocking=False):
        return {"ok": False, "error": "a run is already in progress"}
    started = datetime.now(timezone.utc)
    summary: dict = {"ok": True, "trigger": trigger, "started_at": started.isoformat(),
                     "cases": []}
    try:
        _set_state(status="running", owner=owner, mode=f"auto-scan ({trigger})",
                   started_at=time.time(), events=[], cases=[], error=None)
        cfg = store.load_config()
        since_iso = cfg.get("auto_last_scan_at") or (
            started - timedelta(hours=24)).strftime("%Y-%m-%dT%H:%M:%SZ")
        _log_event(f"auto-scan ({trigger}): open cases of '{owner}' with customer "
                   f"activity since {since_iso}")
        ips = IpsClient()
        open_cases = ips.find_open_cases(owner)
        updates = ips.find_customer_updates([c.case_id for c in open_cases], since_iso)
        work = select_auto_work(open_cases, updates, store.ledger_entry, since_iso)
        _log_event(f"{len(open_cases)} open case(s), {len(updates)} with customer "
                   f"comments since then, {len(work)} to run")
        if len(work) > max_cases:
            _log_event(f"capped to {max_cases} case(s) this round (newest first)")
            work = work[:max_cases]
        _set_state(cases=[w[0].to_dict() for w in work])
        for ref, reason, upd in work:
            _log_event(f"analyzing case {ref.case_nbr} — {reason} — {ref.subject[:60]}")
            try:
                rec = _analyze_and_enqueue(store, ref.case_nbr,
                                           case_id=ref.case_id, subject=ref.subject)
                if upd and reason.startswith("customer update"):
                    store.mark_customer_update(ref.case_nbr, upd)
                summary["cases"].append({"case_nbr": ref.case_nbr, "reason": reason,
                                         "mode": rec.get("mode")})
            except Exception as e:
                print(f"[handsfree] auto-scan case {ref.case_nbr} failed:\n"
                      f"{traceback.format_exc()}")
                _log_event(f"[{ref.case_nbr}] FAILED: {type(e).__name__}: {e}")
                summary["cases"].append({"case_nbr": ref.case_nbr, "reason": reason,
                                         "mode": "error"})
        store.save_config({"auto_last_scan_at": started.strftime("%Y-%m-%dT%H:%M:%SZ"),
                           "auto_last_result": f"{len(work)} case(s) run"})
        _set_state(status="done", finished_at=time.time())
        _log_event("auto-scan complete — review the queue below")
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
    case_nbr = "".join(ch for ch in str(case_nbr or "").strip() if ch.isalnum())
    if not case_nbr:
        return {"ok": False, "error": "empty case number"}
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
    if rec.get("mode") == "first_response":
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
        if sent:
            store.update(draft_id, status="posted",
                         post_result={"ok": False, "backend": "none",
                                      "error": "first response already sent on this case"})
            return {"ok": False,
                    "error": "the first-response overview was already sent on this "
                             "case (it goes out once); marked as posted",
                    "draft": store.get(draft_id)}

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
