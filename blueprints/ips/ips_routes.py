"""
Attaching a case number to a log that arrived without one.

Analysing a log and then chatting about it is the work the case is billed
against, so a session that never names a case cannot be traced back to one
afterwards. These endpoints back the prompt that asks for the number before
the conversation starts.
"""

from flask import Blueprint, jsonify, request

from services import ips_service

ips_bp = Blueprint("ips", __name__, url_prefix="/api/ips")


@ips_bp.route("/candidates", methods=["GET"])
def candidates():
    """Case numbers worth pre-filling for a log, best guess first."""
    log_path = (request.args.get("log_path") or "").strip()
    return jsonify({"success": True, **ips_service.prompt_state(log_path)})


@ips_bp.route("/resolve", methods=["POST"])
def resolve():
    """
    Record the answer to the prompt.

    ``skip`` is a deliberate statement that the log has no case, not a way to
    dismiss the dialog, so it is only honoured when the client also sends
    ``confirmed``. Without that the user could clear the prompt by accident and
    the session would go untraceable in exactly the silent way this exists to
    prevent.
    """
    data = request.get_json(silent=True) or {}
    log_path = str(data.get("log_path") or "").strip()
    action = str(data.get("action") or "attach").strip().lower()

    if action == "skip":
        if not bool(data.get("confirmed")):
            return jsonify({
                "success": False,
                "error": "Confirm there is no case number for this log before skipping.",
            }), 400
        ips_service.attach("", ips_service.SKIPPED, log_path)
        return jsonify({
            "success": True,
            "case_nbr": "",
            "case_ref_source": ips_service.SKIPPED,
            "message": "Recorded as having no case number.",
        })

    source = str(data.get("source") or ips_service.EXPLICIT).strip().lower()
    if source not in (ips_service.EXPLICIT, ips_service.DERIVED_FROM_PATH):
        source = ips_service.EXPLICIT

    try:
        canonical = ips_service.attach(data.get("case_nbr"), source, log_path)
    except ValueError as e:
        return jsonify({"success": False, "error": str(e)}), 400

    return jsonify({
        "success": True,
        "case_nbr": canonical,
        "case_ref_source": source,
        "message": f"Case {canonical} attached.",
    })
