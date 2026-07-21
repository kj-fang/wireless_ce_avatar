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

import threading
import time
import traceback
from pathlib import Path
from typing import Optional

from .composer import compose, AI_MARKER
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
        return dict(_run_state)


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

def approve_and_post(draft_id: str, edited_plain: Optional[str] = None) -> dict:
    """Human clicked Approve. Post the (possibly edited) draft to IPS as a
    Private-to-Intel comment. Returns the updated draft record + result."""
    store = _store()
    cfg = store.load_config()
    rec = store.get(draft_id)
    if rec is None:
        return {"ok": False, "error": f"draft not found: {draft_id}"}
    if rec.get("status") == "posted":
        return {"ok": False, "error": "draft already posted", "draft": rec}
    if cfg.get("dry_run"):
        return {"ok": False, "error": "dry_run is enabled in config — posting disabled"}

    # Apply reviewer edits.
    if edited_plain is not None and edited_plain.strip():
        from .composer import compose_html
        plain = edited_plain
        if AI_MARKER not in plain:
            plain = AI_MARKER + "\n\n" + plain
        rec = store.update(draft_id, draft_plain=plain,
                           draft_html=compose_html(plain))

    ips = IpsClient()

    # Never double-comment: scan existing comments for our marker.
    try:
        for c in ips.get_case_comments(rec["case_id"]):
            body = str(c.get(IpsClient.FIELD_RICH_BODY) or "")
            if AI_MARKER in body:
                store.update(draft_id, status="posted",
                             post_result={"ok": False, "backend": "none",
                                          "error": "AI comment already present on case"})
                store.mark_posted(rec["case_nbr"], comment_id=c.get("Id", ""))
                return {"ok": False,
                        "error": "an AI-Avatar comment already exists on this case; "
                                 "marked as posted to avoid a duplicate",
                        "draft": store.get(draft_id)}
    except Exception as e:
        print(f"[handsfree] prior-comment scan failed (continuing): {e}")

    backend = (cfg.get("post_backend") or "auto").lower()
    result = None

    if backend in ("rest", "auto"):
        try:
            result = ips.post_comment(rec["case_id"], rec["draft_html"],
                                      field_map=cfg.get("rest_field_map"),
                                      private=True)
        except PostUnsupported as e:
            print(f"[handsfree] REST posting unsupported: {e}")
            if backend == "rest":
                store.update(draft_id, status="post_failed",
                             post_result={"ok": False, "backend": "rest", "error": str(e)})
                return {"ok": False, "error": str(e), "draft": store.get(draft_id)}
            result = None   # fall through to UI

    if result is None or (not result.ok and backend == "auto"):
        from .ui_commenter import UiCommenter
        ui = UiCommenter(locators=cfg.get("ui_locators"))
        result = ui.post_comment(rec["case_id"], rec["draft_plain"])

    if result.ok:
        store.update(draft_id, status="posted", post_result=result.to_dict())
        store.mark_posted(rec["case_nbr"], comment_id=result.comment_id)
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
