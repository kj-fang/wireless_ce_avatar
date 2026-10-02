"""
Attaching a case number to a log that arrived without one.

Analysing a log and then chatting about it is the work the case is billed
against, so a session that never names a case cannot be traced back to one
afterwards. These endpoints back the prompt that asks for the number before
the conversation starts.
"""

from flask import Blueprint, jsonify, request

from services import check_ips_service

check_ips_bp = Blueprint("check_ips", __name__, url_prefix="/api/ips")


@check_ips_bp.route("/candidates", methods=["GET"])
def candidates():
    """Case numbers worth pre-filling for a log, best guess first."""
    log_path = (request.args.get("log_path") or "").strip()
    return jsonify({"success": True, **check_ips_service.prompt_state(log_path)})


@check_ips_bp.route("/resolve", methods=["POST"])
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
        # The literal boolean only. bool() of the JSON string "false" is True,
        # which let a caller record a skip without confirming anything.
        if data.get("confirmed") is not True:
            return jsonify({
                "success": False,
                "error": "Confirm there is no case number for this log before skipping.",
            }), 400
        check_ips_service.attach("", check_ips_service.SKIPPED, log_path)
        return jsonify({
            "success": True,
            "case_nbr": "",
            "case_ref_source": check_ips_service.SKIPPED,
            "message": "Recorded as having no case number.",
        })

    # The client says where it thinks the number came from; the server decides.
    # A claim of derived_from_path only stands for a number that really is a
    # case folder in this log's path, and a remembered answer keeps its own.
    claimed = str(data.get("source") or check_ips_service.EXPLICIT).strip().lower()
    source = check_ips_service.source_for_answer(data.get("case_nbr"), claimed, log_path)

    try:
        canonical = check_ips_service.attach(data.get("case_nbr"), source, log_path)
    except ValueError as e:
        return jsonify({"success": False, "error": str(e)}), 400

    # Look the case up and classify it, as the case search does, so a number
    # given here is no longer left "Unclassified" with no description. After
    # the answer is recorded, and never a reason to refuse it.
    try:
        check_ips_service.enrich_attached_case(canonical)
    except Exception as e:
        print(f"[ips] enrich after resolve failed: {e}")

    return jsonify({
        "success": True,
        "case_nbr": canonical,
        "case_ref_source": source,
        "message": f"Case {canonical} attached.",
    })
