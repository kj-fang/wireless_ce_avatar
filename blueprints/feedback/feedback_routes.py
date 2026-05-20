"""
Feedback Sidecar Blueprint

Independent endpoint for receiving user feedback (thumbs up / down) on
agent responses. Decoupled from the chatbot agent: failures here never
affect chat behaviour.
"""

from flask import Blueprint, request, jsonify, session

from services import feedback_service

feedback_bp = Blueprint("feedback", __name__, url_prefix="/feedback")


@feedback_bp.route("/vote", methods=["POST"])
def vote():
    """
    Record a thumbs up / down on a specific agent turn.

    Request JSON:
      {
        "conversation_id": "...",   # required
        "turn_id":         "...",   # required
        "vote":            1 | -1   # required
      }

    session_id is taken from the existing chatbot session — no auth needed.
    """
    data = request.get_json(silent=True) or {}
    conversation_id = (data.get("conversation_id") or "").strip()
    turn_id = (data.get("turn_id") or "").strip()

    raw_vote = data.get("vote")
    try:
        vote_val = int(raw_vote)
    except (TypeError, ValueError):
        return jsonify({"success": False, "error": "vote must be 1 or -1"}), 400
    if vote_val not in (1, -1):
        return jsonify({"success": False, "error": "vote must be 1 or -1"}), 400

    if not conversation_id or not turn_id:
        return jsonify({
            "success": False,
            "error": "conversation_id and turn_id are required",
        }), 400

    # Reuse the chatbot's anonymous session id; no login required.
    session_id = session.get("chatbot_session_id", "")

    ok = feedback_service.record_vote(
        session_id=session_id,
        conversation_id=conversation_id,
        turn_id=turn_id,
        vote=vote_val,
    )
    if not ok:
        return jsonify({"success": False, "error": "failed to record vote"}), 500
    return jsonify({"success": True})


@feedback_bp.route("/detail", methods=["POST"])
def detail():
    """
    Record structured "more feedback" details for a specific agent turn.

    Layer 2 of the feedback pipeline — the user has already cast a 👍/👎
    via /feedback/vote and is now opening the modal to say *why* / *where*.

    Request JSON (all fields optional except conversation_id + turn_id):
      {
        "conversation_id": "...",
        "turn_id":         "...",
        "vote":            1 | -1 | null,         # carry-over for context
        "issues": [
          {
            "scope":      "overall" | "skill" | "step",
            "skill_id":   "...",                  # when scope=skill
            "step_index": 3,                      # when scope=step
            "step_label": "Step 3 — Invoking driver_init",
            "category":   "wrong_skill" | "wrong_input" | "wrong_order"
                          | "bad_output" | "missing_step" | "stuck" | "other",
            "should_be":  "...",                  # only for wrong_skill
            "comment":    "..."
          },
          ...
        ],
        "general_comment": "..."
      }
    """
    data = request.get_json(silent=True) or {}
    conversation_id = (data.get("conversation_id") or "").strip()
    turn_id = (data.get("turn_id") or "").strip()
    if not conversation_id or not turn_id:
        return jsonify({
            "success": False,
            "error": "conversation_id and turn_id are required",
        }), 400

    raw_vote = data.get("vote")
    try:
        vote_val = int(raw_vote) if raw_vote not in (None, "") else None
    except (TypeError, ValueError):
        vote_val = None
    if vote_val not in (1, -1, None):
        vote_val = None

    session_id = session.get("chatbot_session_id", "")

    ok = feedback_service.record_detail(
        session_id=session_id,
        conversation_id=conversation_id,
        turn_id=turn_id,
        vote=vote_val,
        issues=data.get("issues") or [],
        general_comment=(data.get("general_comment") or "").strip(),
    )
    if not ok:
        return jsonify({
            "success": False,
            "error": "nothing to record (all fields empty)",
        }), 400
    return jsonify({"success": True})


@feedback_bp.route("/recent", methods=["GET"])
def recent():
    """
    Debug helper: return the last N feedback events.
    Useful during MVP development to verify writes are happening.
    """
    try:
        limit = int(request.args.get("limit", "50"))
    except ValueError:
        limit = 50
    limit = max(1, min(500, limit))
    return jsonify({
        "success": True,
        "events": feedback_service.get_recent_votes(limit),
    })
