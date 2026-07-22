"""
Handsfree Replyer routes — manual trigger + review queue.

v1 rules enforced here and in the orchestrator:
  * Posting happens ONLY via the explicit Approve endpoint.
  * A dry_run config flag disables posting entirely.
"""

from __future__ import annotations

from flask import Blueprint, jsonify, render_template, request

from services.handsfree import orchestrator
from services.handsfree.queue import HandsfreeStore

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


# ---------- queue ----------

@handsfree_bp.route("/queue")
def list_queue():
    include_closed = request.args.get("all", "").lower() in ("1", "true", "yes")
    return jsonify({"items": _store().list_drafts(include_closed=include_closed)})


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


# ---------- config + REST field discovery ----------

@handsfree_bp.route("/config", methods=["GET", "POST"])
def config():
    store = _store()
    if request.method == "GET":
        return jsonify(store.load_config())
    body = request.get_json(silent=True) or {}
    allowed = {"owner_name", "post_backend", "max_cases_per_run",
               "dry_run", "rest_field_map", "ui_locators"}
    updates = {k: v for k, v in body.items() if k in allowed}
    return jsonify(store.save_config(updates))


@handsfree_bp.route("/discover_fields", methods=["POST"])
def discover_fields():
    try:
        return jsonify(orchestrator.discover_rest_fields())
    except Exception as e:
        return jsonify({"error": f"{type(e).__name__}: {e}"}), 500
