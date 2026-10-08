"""
Handsfree Replyer routes — manual trigger + review queue.

v1 rules enforced here and in the orchestrator:
  * Posting happens ONLY via the explicit Approve endpoint.
  * A dry_run config flag disables posting entirely.
"""

from __future__ import annotations

import os
import subprocess

from flask import Blueprint, jsonify, render_template, request

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


@handsfree_bp.route("/trial_run", methods=["POST"])
def trial_run():
    """Tuning batch on an IPS list view (open or closed cases); needs trial
    mode in Settings. Drafts are flagged trial and can never be posted."""
    body = request.get_json(silent=True) or {}
    return jsonify(orchestrator.start_trial_run(
        list_view=str(body.get("list_view") or ""),
        max_cases=int(body.get("max_cases") or 0),
        skip_analyzed=bool(body.get("skip_analyzed", True))))


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
            max_cases=body.get("max_cases")))
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400


@handsfree_bp.route("/auto/run_now", methods=["POST"])
def auto_run_now():
    """One automatic round right away (same scan the schedule performs)."""
    import threading
    from services.handsfree import scheduler
    if orchestrator._run_lock.locked():
        return jsonify({"ok": False, "error": "a run is already in progress"})
    cfg = _store().load_config()
    if not (cfg.get("owner_name") or "").strip():
        return jsonify({"ok": False, "error": "no owner name configured"})
    threading.Thread(target=scheduler.run_once, kwargs={"trigger": "manual"},
                     daemon=True, name="handsfree-auto-now").start()
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


# ---------- config + REST field discovery ----------

@handsfree_bp.route("/config", methods=["GET", "POST"])
def config():
    store = _store()
    if request.method == "GET":
        return jsonify(store.load_config())
    body = request.get_json(silent=True) or {}
    allowed = {"owner_name", "post_backend", "max_cases_per_run",
               "dry_run", "rest_field_map", "ui_locators",
               "trial_mode", "trial_list_view", "trial_max_cases"}
    updates = {k: v for k, v in body.items() if k in allowed}
    return jsonify(store.save_config(updates))


@handsfree_bp.route("/discover_fields", methods=["POST"])
def discover_fields():
    try:
        return jsonify(orchestrator.discover_rest_fields())
    except Exception as e:
        return jsonify({"error": f"{type(e).__name__}: {e}"}), 500
