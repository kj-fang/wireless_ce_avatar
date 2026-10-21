"""
Beacon (handsfree replyer) routes — manual trigger + review queue.

v1 rules enforced here and in the orchestrator:
  * Posting happens ONLY via the explicit Approve endpoint.
  * A dry_run config flag disables posting entirely.
"""

from __future__ import annotations

import os
import subprocess

from flask import Blueprint, jsonify, render_template, request, session

from services.handsfree import orchestrator
from services.handsfree.queue import HandsfreeStore, decoded_log_dir

handsfree_bp = Blueprint("handsfree", __name__, url_prefix="/handsfree")


def _store() -> HandsfreeStore:
    from configs.global_configs import app_config
    from pathlib import Path
    return HandsfreeStore(Path(app_config.avatarfiles_dir) / "handsfree")


@handsfree_bp.route("/")
def index():
    cfg = _store().load_config()
    return render_template("handsfree.html", config=cfg)


# ---------- run ----------

@handsfree_bp.route("/check_now", methods=["POST"])
def check_now():
    body = request.get_json(silent=True) or {}
    return jsonify(orchestrator.start_check_now(body.get("owner_name")))


@handsfree_bp.route("/run_case", methods=["POST"])
def run_case():
    """Manual trigger for one explicitly chosen IPS case number."""
    body = request.get_json(silent=True) or {}
    return jsonify(orchestrator.start_case_run(body.get("case_nbr")))


@handsfree_bp.route("/status")
def status():
    return jsonify(orchestrator.get_run_state())


# ---------- automatic analysis (scheduler) ----------

@handsfree_bp.record_once
def _resume_scheduler(_state):
    """After an app restart, resume a previously enabled schedule without
    waiting for someone to open the page. The thread reads the config on
    every tick, so a not-yet-booted app_config just means a skipped tick."""
    try:
        from services.handsfree import scheduler
        scheduler.ensure_started()
    except Exception as e:
        print(f"[handsfree] scheduler not started: {e}")


@handsfree_bp.route("/auto", methods=["GET", "POST"])
def auto_check():
    from services.handsfree import scheduler
    if request.method == "GET":
        return jsonify(scheduler.status())
    body = request.get_json(silent=True) or {}
    try:
        return jsonify(scheduler.configure(
            enabled=bool(body.get("enabled")),
            mode=str(body.get("mode") or "nightly"),
            max_cases=body.get("max_cases"),
            source=body.get("source"),
            list_view=body.get("list_view"),
            list_views=body.get("list_views")))
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400


@handsfree_bp.route("/list_views")
def list_views():
    """Case list views visible to the current IPS login, for the automatic-
    analysis source dropdown. Never raises: {"items": [...]} or {"error": ...}."""
    try:
        from services.handsfree.ips_client import IpsClient
        return jsonify({"items": IpsClient().list_case_views()})
    except Exception as e:
        return jsonify({"error": f"{type(e).__name__}: {e}"})


@handsfree_bp.route("/auto/run_now", methods=["POST"])
def auto_run_now():
    """One automatic round right away (same scan the schedule performs),
    with the round settings from the page (source, list view, max cases)
    saved first — the schedule itself is left alone."""
    import threading
    from services.handsfree import scheduler
    if orchestrator._run_lock.locked():
        return jsonify({"ok": False, "error": "a run is already in progress"})
    body = request.get_json(silent=True) or {}
    cfg = scheduler.save_settings(max_cases=body.get("max_cases"),
                                  source=body.get("source"),
                                  list_view=body.get("list_view"),
                                  list_views=body.get("list_views"))
    target = scheduler.scan_target(cfg)
    if target["error"]:
        return jsonify({"ok": False, "error": target["error"]})
    # Handshake: answer only once the worker holds the run lock and the
    # status says "running" — the page polls right away and would otherwise
    # read the previous run's "done" and stop polling before this one starts.
    started = threading.Event()
    outcome: dict = {}

    def _worker():
        try:
            outcome["res"] = scheduler.run_once(trigger="manual", on_start=started.set)
        finally:
            started.set()
    threading.Thread(target=_worker, daemon=True, name="handsfree-auto-now").start()
    started.wait(timeout=15)
    res = outcome.get("res")
    if res is not None and not res.get("ok"):
        return jsonify({"ok": False, "error": res.get("error") or "run did not start"})
    return jsonify({"ok": True})


# ---------- queue ----------

@handsfree_bp.route("/queue")
def list_queue():
    include_closed = request.args.get("all", "").lower() in ("1", "true", "yes")
    owner_name = request.args.get("owner", "").strip()
    return jsonify({"items": _store().list_drafts(
        include_closed=include_closed, owner_name=owner_name)})


@handsfree_bp.route("/queue/<draft_id>")
def get_draft(draft_id: str):
    rec = _store().get(draft_id)
    if rec is None:
        return jsonify({"error": "not found"}), 404
    return jsonify(rec)


@handsfree_bp.route("/queue/<draft_id>", methods=["PUT", "POST"])
def save_draft(draft_id: str):
    body = request.get_json(silent=True) or {}
    plain = (body.get("draft_plain") or "").strip()
    if not plain:
        return jsonify({"ok": False, "error": "empty draft"}), 400
    from services.handsfree.composer import compose_html, AI_MARKER
    if AI_MARKER not in plain:
        plain = AI_MARKER + "\n\n" + plain
    rec = _store().update(draft_id, draft_plain=plain,
                          draft_html=compose_html(plain))
    if rec is None:
        return jsonify({"ok": False, "error": "not found"}), 404
    return jsonify({"ok": True, "draft": rec})


@handsfree_bp.route("/queue/<draft_id>/approve", methods=["POST"])
def approve(draft_id: str):
    body = request.get_json(silent=True) or {}
    return jsonify(orchestrator.approve_and_post(
        draft_id, edited_plain=body.get("draft_plain")))


@handsfree_bp.route("/queue/<draft_id>/reject", methods=["POST"])
def reject(draft_id: str):
    body = request.get_json(silent=True) or {}
    return jsonify(orchestrator.reject(draft_id, reason=body.get("reason", "")))


@handsfree_bp.route("/queue/<draft_id>/open_log_folder", methods=["POST"])
def open_log_folder(draft_id: str):
    """Open the folder of the case's decoded WRT log in Explorer (the app
    runs on the reviewer's own PC), with the .log selected. The path comes
    from the stored draft record, never from the request."""
    if request.remote_addr not in ("127.0.0.1", "::1"):
        return jsonify({"ok": False, "error": "localhost only"}), 403
    rec = _store().get(draft_id)
    if rec is None:
        return jsonify({"ok": False, "error": "not found"}), 404
    folder = decoded_log_dir(rec)
    if not folder:
        return jsonify({"ok": False,
                        "error": "this case has no decoded log"}), 404
    if not os.path.isdir(folder):
        return jsonify({"ok": False,
                        "error": f"folder no longer exists: {folder}"}), 404
    log_path = os.path.normpath(rec["analysis"]["log_path"])
    if os.path.isfile(log_path):
        subprocess.Popen(["explorer", "/select,", log_path])
    else:
        subprocess.Popen(["explorer", folder])
    return jsonify({"ok": True, "folder": folder})


# ---------- per-case history + hand-off to Avatar's manual analysis ----------

def _localhost_only():
    if request.remote_addr not in ("127.0.0.1", "::1"):
        return jsonify({"ok": False, "error": "localhost only"}), 403
    return None


@handsfree_bp.route("/case/<case_nbr>/reset", methods=["POST"])
def reset_case(case_nbr: str):
    """Forget the case's handsfree history (drafts archived, ledger analysis
    marks dropped, BSOD submissions forgotten) so the next run starts fresh,
    e.g. after the customer replaced a wrong log upload. Posted drafts and
    the record of what was posted are kept."""
    denied = _localhost_only()
    if denied:
        return denied
    from services.handsfree import bsod
    from utils import check_ips_utils
    canonical = check_ips_utils.normalise_ips(str(case_nbr or ""))
    if not canonical:
        return jsonify({"ok": False, "error": f"not a case number: {case_nbr}"}), 400
    body = request.get_json(silent=True) or {}
    case_dir = ""
    try:
        from configs.global_configs import app_config
        case_dir = os.path.join(app_config.avatarfiles_dir, canonical)
    except Exception:
        pass
    res = _store().reset_case(canonical, purge_downloads=bool(body.get("purge_downloads")),
                              case_dir=case_dir)
    res["bsod_forgotten"] = bsod.forget_case(canonical)
    res["ok"] = True
    return jsonify(res)


def _handoff_source(case_nbr: str, draft_id: str) -> dict:
    """What the chatbot needs, from the draft record or (while a run is in
    flight) the run state: log_path, subject, description, ... ."""
    store = _store()
    if draft_id:
        rec = store.get(draft_id)
        if rec and str(rec.get("case_nbr") or "") == case_nbr:
            a = rec.get("analysis") or {}
            incs = a.get("incidents") or []
            sent = (incs[0].get("sent_for_analysis") if incs and isinstance(incs[0], dict)
                    else None) or {}
            return {"log_path": a.get("log_path") or "", "case_id": a.get("case_id") or "",
                    "subject": a.get("subject") or "", "wifi_or_bt": a.get("wifi_or_bt") or "wifi",
                    "issue_type": a.get("issue_type") or "",
                    "description": a.get("clean_description") or a.get("description") or "",
                    "issue_time_log": sent.get("issue_time_log") or ""}
    live = (orchestrator.get_run_state().get("case_details") or {}).get(case_nbr) or {}
    sent = (live.get("sent_for_analysis") or [{}])[0]
    return {"log_path": live.get("log_path") or "", "case_id": live.get("case_id") or "",
            "subject": live.get("subject") or "", "wifi_or_bt": live.get("wifi_or_bt") or "wifi",
            "issue_type": live.get("issue_type") or "",
            "description": live.get("clean_description") or "",
            "issue_time_log": sent.get("issue_time_log") or ""}


@handsfree_bp.route("/chatbot_handoff", methods=["POST"])
def chatbot_handoff():
    """Prepare THIS browser session so Avatar's Log Chatbot opens on the
    case's decoded log, primed with the case and the issue time (in the log's
    clock), for a manual analysis. Returns the URL to open; the page opens it
    in a new tab from the click handler."""
    denied = _localhost_only()
    if denied:
        return denied
    from urllib.parse import urlencode
    from configs.global_configs import app_config
    from models.models import CaseContext
    from services import check_ips_service
    from utils import check_ips_utils
    body = request.get_json(silent=True) or {}
    case_nbr = check_ips_utils.normalise_ips(str(body.get("case_nbr") or ""))
    if not case_nbr:
        return jsonify({"ok": False, "error": "case number required"}), 400
    src = _handoff_source(case_nbr, str(body.get("draft_id") or ""))
    log_path = src["log_path"]
    if not log_path or not os.path.isfile(log_path):
        return jsonify({"ok": False, "error": "this case has no decoded log to hand off"}), 404
    try:
        # 1. the chatbot's case-number gate
        check_ips_service.attach(case_nbr, check_ips_service.EXPLICIT, log_path)
        # 2. the case as the chatbot's issue context (no refetch)
        ctx = CaseContext(case_nbr=case_nbr, id=src["case_id"], subject=src["subject"],
                          description=src["description"], wifi_or_bt=src["wifi_or_bt"],
                          case_download_dir=os.path.join(app_config.avatarfiles_dir, case_nbr),
                          case_ref_source=check_ips_service.EXPLICIT)
        session["case_context"] = ctx.to_session()
        session["classification"] = {"issue_type": src["issue_type"]} if src["issue_type"] else {}
        session.pop("ai_ips_analysis", None)
        # 3. the log + the issue time, the way /log_chatbot/prepare seeds them
        for key in ("_attachment_time_cache", "_issue_ai_quick", "chatbot_session_id",
                    "feedback_conversation_id"):
            session.pop(key, None)
        session["chatbot_log_path"] = log_path
        session["_carried_issue_time_present"] = bool(src["issue_time_log"])
        session["_carried_issue_time"] = src["issue_time_log"]
        session["_carried_issue_time_warning"] = ""
        app_config.last_analyzed_log_path = log_path
    except Exception as e:
        return jsonify({"ok": False, "error": f"{type(e).__name__}: {e}"}), 500
    params = {"auto_run": "analyze_all"}
    if src["issue_time_log"]:
        params["issue_time"] = src["issue_time_log"]
    return jsonify({"ok": True, "url": "/log_chatbot/?" + urlencode(params),
                    "log_path": log_path, "issue_time": src["issue_time_log"]})


# ---------- config + REST field discovery ----------

@handsfree_bp.route("/config", methods=["GET", "POST"])
def config():
    store = _store()
    if request.method == "GET":
        return jsonify(store.load_config())
    body = request.get_json(silent=True) or {}
    allowed = {"owner_name", "post_backend", "max_cases_per_run",
               "dry_run", "rest_field_map", "ui_locators", "trial_mode"}
    updates = {k: v for k, v in body.items() if k in allowed}
    return jsonify(store.save_config(updates))


@handsfree_bp.route("/discover_fields", methods=["POST"])
def discover_fields():
    try:
        return jsonify(orchestrator.discover_rest_fields())
    except Exception as e:
        return jsonify({"error": f"{type(e).__name__}: {e}"}), 500
