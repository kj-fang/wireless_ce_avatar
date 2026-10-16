"""
Handsfree Replyer routes — manual trigger + review queue.

v1 rules enforced here and in the orchestrator:
  * Posting happens ONLY via the explicit Approve endpoint.
  * A dry_run config flag disables posting entirely.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

from flask import Blueprint, jsonify, render_template, request

from services.handsfree import orchestrator
from services.handsfree.queue import (HandsfreeShareUnavailable, HandsfreeStore,
                                      decoded_log_dir, decoded_log_file,
                                      describe_share_error, open_store)

handsfree_bp = Blueprint("handsfree", __name__, url_prefix="/handsfree")


def _store() -> HandsfreeStore:
    return open_store()


@handsfree_bp.errorhandler(HandsfreeShareUnavailable)
def _share_unavailable(e):
    return jsonify({"ok": False, "error": str(e)}), 503


@handsfree_bp.errorhandler(OSError)
def _file_error(e):
    # Permission or network trouble after the startup check passed.
    where = f" ({e.filename})" if getattr(e, "filename", None) else ""
    return jsonify({"ok": False,
                    "error": f"file access failed{where}: {describe_share_error(e)}"}), 503


@handsfree_bp.route("/")
def index():
    try:
        cfg = _store().load_config()
        share_error = ""
    except HandsfreeShareUnavailable as e:
        cfg, share_error = dict(HandsfreeStore.DEFAULT_CONFIG), str(e)
    return render_template("handsfree.html", config=cfg, share_error=share_error,
                           my_owner=_my_owner())


# ---------- this reviewer (kept on this PC, not on the shared folder) ----------

def _me_path() -> Path:
    from configs.global_configs import app_config
    return Path(app_config.avatarfiles_dir) / "handsfree_user.json"


def _my_owner() -> str:
    try:
        return str(json.loads(_me_path().read_text(encoding="utf-8")).get("owner_name") or "")
    except Exception:
        return ""


@handsfree_bp.route("/me", methods=["POST"])
def set_me():
    """Remember whose cases this reviewer's queue opens on."""
    name = str((request.get_json(silent=True) or {}).get("owner_name") or "").strip()
    _me_path().write_text(json.dumps({"owner_name": name}), encoding="utf-8")
    return jsonify({"ok": True, "owner_name": name})


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


# ---------- automatic analysis ----------
# Scheduled rounds run on the Handsfree server (nightly CLI) only. The app
# keeps the round settings and "Run now" (a one-off manual round), but never
# starts the in-app scheduler: the config is shared by every reviewer's PC.

@handsfree_bp.route("/auto", methods=["GET", "POST"])
def auto_check():
    from services.handsfree import scheduler
    if request.method == "GET":
        return jsonify(scheduler.status())
    body = request.get_json(silent=True) or {}
    if body.get("enabled"):
        return jsonify({"ok": False, "error": "scheduled analysis runs on the Handsfree "
                        "server (nightly CLI) and cannot be started from the app"}), 403
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
    """Open the folder of the case's decoded log in Explorer (the app runs on
    the reviewer's own PC), with the log selected — the shared copy on the
    Handsfree folder, so it works whichever machine ran the analysis. The
    path comes from the stored draft record, never from the request."""
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
    log_path = os.path.normpath(decoded_log_file(rec))
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
               "dry_run", "rest_field_map", "ui_locators", "trial_mode"}
    updates = {k: v for k, v in body.items() if k in allowed}
    return jsonify(store.save_config(updates))


@handsfree_bp.route("/discover_fields", methods=["POST"])
def discover_fields():
    try:
        return jsonify(orchestrator.discover_rest_fields())
    except Exception as e:
        return jsonify({"error": f"{type(e).__name__}: {e}"}), 500
