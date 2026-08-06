"""
Feedback Sidecar Blueprint

Independent endpoint for receiving user feedback (thumbs up / down) on
agent responses. Decoupled from the chatbot agent: failures here never
affect chat behaviour.
"""

import re
import uuid

from flask import Blueprint, request, jsonify, session

from models.models import CaseContext
from services import feedback_service, gather_service

feedback_bp = Blueprint("feedback", __name__, url_prefix="/feedback")


# ---- Defense-in-depth: validate client-supplied IDs at the route
# boundary before they reach feedback_service. The service layer ALSO
# sanitises (see feedback_service._safe_id), but route-level rejection
# is preferred because:
#   * fast-fail with a clear 400 to the client instead of silently
#     substituting "unknown" downstream,
#   * an obvious choke-point in case any future service function
#     forgets to call _safe_id on a path component,
#   * one consistent allow-list across both /feedback/* and
#     services.feedback_service so the schema is unambiguous.
_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,80}$")


def _bad_id_response(field_name: str):
    """Build the 400 response used when an ID fails the allow-list."""
    return jsonify({
        "success": False,
        "error": f"{field_name} must match [A-Za-z0-9_-]{{1,80}}",
    }), 400


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
        return jsonify({"success": False, "error": "vote must be 1, -1, or 0"}), 400
    if vote_val not in (1, -1, 0):
        return jsonify({"success": False, "error": "vote must be 1, -1, or 0"}), 400

    if not conversation_id or not turn_id:
        return jsonify({
            "success": False,
            "error": "conversation_id and turn_id are required",
        }), 400
    if not _SAFE_ID_RE.match(conversation_id):
        return _bad_id_response("conversation_id")
    if not _SAFE_ID_RE.match(turn_id):
        return _bad_id_response("turn_id")

    # Reuse the chatbot's anonymous session id; no login required.
    session_id = session.get("chatbot_session_id", "")

    # Optional context for weighting. The frontend sets `yaml_modified` to
    # true when the user edited the side-panel skill configuration earlier
    # in the session — those vote events are weighted "high" so reviewers
    # see them first.
    yaml_modified = bool(session.get("yaml_modified"))

    # Analysis domain ("" = wifi/default, "bt" = Bluetooth). The frontend
    # tags each request so feedback streams stay partitioned; the service
    # also inherits the conversation's recorded domain as a safety net.
    domain = (data.get("domain") or "").strip()

    ok = feedback_service.record_vote(
        session_id=session_id,
        conversation_id=conversation_id,
        turn_id=turn_id,
        vote=vote_val,
        yaml_modified=yaml_modified,
        domain=domain,
    )
    if not ok:
        return jsonify({"success": False, "error": "failed to record vote"}), 500

    # Surface the "modified YAML" hint so the client can show the
    # post-vote upload prompt without having to track this state itself.
    return jsonify({
        "success": True,
        "yaml_modified": yaml_modified,
        "yaml_modified_path": session.get("yaml_modified_path", "") or "",
    })


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
        ]
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
    if not _SAFE_ID_RE.match(conversation_id):
        return _bad_id_response("conversation_id")
    if not _SAFE_ID_RE.match(turn_id):
        return _bad_id_response("turn_id")

    raw_vote = data.get("vote")
    try:
        vote_val = int(raw_vote) if raw_vote not in (None, "") else None
    except (TypeError, ValueError):
        vote_val = None
    if vote_val not in (1, -1, None):
        vote_val = None

    session_id = session.get("chatbot_session_id", "")
    feedback_event_id = str(uuid.uuid4())
    # The legacy Wi-Fi frontend sends an empty domain; make the v6
    # agent_domain explicit while preserving feedback_service behaviour.
    agent_domain = (data.get("domain") or "wifi").strip().lower()

    # Skill-config attachment was removed from the feedback flow. We keep
    # the `yaml_modified` session flag only as a priority signal (it bumps
    # filled-form feedback to weight="high" in the review queue); no skill
    # YAML is ever copied or uploaded from here.
    yaml_modified = bool(session.get("yaml_modified"))

    # Log attachment is opt-in: only ship the session log when the user
    # explicitly ticked "Attach session log" in the modal. The log path is
    # resolved server-side from the session so the client cannot designate
    # an arbitrary file for upload.
    #
    # Spelled out as an explicit if/else (instead of
    # `session.get(...) or "" if attach_log else ""`) because the
    # one-liner relies on `or` binding tighter than the ternary, which
    # is correct today but reads as ambiguous and is easy to break in
    # future edits.
    attach_log = bool(data.get("attach_log"))
    if attach_log:
        log_path = session.get("chatbot_log_path", "") or ""
    else:
        log_path = ""

    # New high-ACE-value structured fields carry all feedback signal; the
    # old free-form `general_comment` / `expected_outcome` fields have been
    # retired and are no longer accepted or forwarded.
    raw_evidence = data.get("evidence_log_lines")
    if isinstance(raw_evidence, str):
        # Accept legacy textarea-as-string payloads too.
        raw_evidence = [
            ln.rstrip() for ln in raw_evidence.splitlines() if ln.strip()
        ]
    elif not isinstance(raw_evidence, list):
        raw_evidence = []

    ok = feedback_service.record_detail(
        session_id=session_id,
        conversation_id=conversation_id,
        turn_id=turn_id,
        vote=vote_val,
        issues=data.get("issues") or [],
        correct_root_cause=(data.get("correct_root_cause") or "").strip(),
        correct_conclusion_tag=(data.get("correct_conclusion_tag") or "").strip(),
        correct_skill=(data.get("correct_skill") or "").strip(),
        correct_approach=(data.get("correct_approach") or "").strip(),
        evidence_log_lines=raw_evidence,
        agent_workflow=(data.get("agent_workflow") or "").strip(),
        # Explicit dispatch lane from the wizard's Step-1 router
        # (skill | agent | both); empty falls back to field inference.
        feedback_layer=(data.get("feedback_layer") or "").strip(),
        # Per-skill verdicts (redundant / wrong) with attributed reason +
        # evidence, from the skill lane. List of dicts; service validates.
        skill_feedback=data.get("skill_feedback") or [],
        # Per-step verdicts (helpful / wrong) with attributed reason +
        # evidence, from the Agent-workflow lane. Same shape as
        # skill_feedback but keyed by reasoning step. Service validates.
        step_feedback=data.get("step_feedback") or [],
        issue_time_problem=(data.get("issue_time_problem") or "").strip(),
        correct_issue_time=(data.get("correct_issue_time") or "").strip(),
        used_issue_time=(data.get("used_issue_time") or "").strip(),
        log_has_date=bool(data.get("log_has_date", True)),
        yaml_modified=yaml_modified,
        log_path=log_path,
        attach_log=attach_log,
        domain=agent_domain,
        feedback_event_id=feedback_event_id,
    )
    if not ok:
        return jsonify({
            "success": False,
            "error": "nothing to record (all fields empty)",
        }), 400
    try:
        issue = CaseContext.from_session(session.get("case_context") or {}).to_dict()
    except Exception:
        issue = {}
    gather_service.record_feedback_submit(
        feedback_event_id=feedback_event_id,
        conversation_id=conversation_id,
        turn_id=turn_id,
        workflow_id=session.get("gather_workflow_id", ""),
        session_id=session_id,
        issue=issue,
        domain=agent_domain,
    )
    return jsonify({"success": True, "feedback_event_id": feedback_event_id})


@feedback_bp.route("/step_vote", methods=["POST"])
def step_vote():
    """
    Per-step thumbs from the live conversation view. Each click on a
    reasoning step's mini 👍/👎 fires one request.

    Request JSON:
      { "conversation_id": "...", "turn_id": "...",
        "step_index": 3, "vote": 1 | -1 }
    """
    data = request.get_json(silent=True) or {}
    conversation_id = (data.get("conversation_id") or "").strip()
    turn_id = (data.get("turn_id") or "").strip()
    if not conversation_id or not turn_id:
        return jsonify({
            "success": False,
            "error": "conversation_id and turn_id are required",
        }), 400
    if not _SAFE_ID_RE.match(conversation_id):
        return _bad_id_response("conversation_id")
    if not _SAFE_ID_RE.match(turn_id):
        return _bad_id_response("turn_id")

    try:
        step_index = int(data.get("step_index"))
        vote = int(data.get("vote"))
    except (TypeError, ValueError):
        return jsonify({
            "success": False,
            "error": "step_index and vote must be integers",
        }), 400
    if vote not in (1, -1):
        return jsonify({"success": False, "error": "vote must be 1 or -1"}), 400

    session_id = session.get("chatbot_session_id", "")
    ok = feedback_service.record_step_vote(
        session_id=session_id,
        conversation_id=conversation_id,
        turn_id=turn_id,
        step_index=step_index,
        vote=vote,
        domain=(data.get("domain") or "").strip(),
    )
    if not ok:
        return jsonify({"success": False, "error": "failed to record"}), 500
    return jsonify({"success": True})


@feedback_bp.route("/skill_helpful", methods=["POST"])
def skill_helpful():
    """
    Post-thumbs-up quick prompt: the user names which skill drove the
    correct answer. Increments the ACE `helpful_count` for that skill
    when aggregated downstream.

    Request JSON: { "conversation_id": "...", "turn_id": "...",
                    "skill_id": "Connection Flow" }
    """
    data = request.get_json(silent=True) or {}
    conversation_id = (data.get("conversation_id") or "").strip()
    turn_id = (data.get("turn_id") or "").strip()
    skill_id = (data.get("skill_id") or "").strip()
    if not conversation_id or not turn_id or not skill_id:
        return jsonify({
            "success": False,
            "error": "conversation_id, turn_id, and skill_id are required",
        }), 400
    if not _SAFE_ID_RE.match(conversation_id):
        return _bad_id_response("conversation_id")
    if not _SAFE_ID_RE.match(turn_id):
        return _bad_id_response("turn_id")
    # skill_id can legitimately contain spaces or punctuation ("Connection Flow"),
    # so don't enforce _SAFE_ID_RE — it never reaches a filesystem path.

    session_id = session.get("chatbot_session_id", "")
    ok = feedback_service.record_helpful_skill(
        session_id=session_id,
        conversation_id=conversation_id,
        turn_id=turn_id,
        skill_id=skill_id,
        domain=(data.get("domain") or "").strip(),
    )
    if not ok:
        return jsonify({"success": False, "error": "failed to record"}), 500
    return jsonify({"success": True})


@feedback_bp.route("/skill_assessment", methods=["POST"])
def skill_assessment():
    """
    Per-skill chip click from the in-line response view. Two states:
    `helpful`, `wrong` (a KNOWLEDGE verdict on the skill's output). Sending
    an empty assessment clears the chip. Each click overwrites any previous
    assessment for the same (turn_id, skill_id) pair. Redundancy is judged
    AI-side by the ACE pipeline, not marked by the user.

    Request JSON:
      { "conversation_id": "...", "turn_id": "...",
        "skill_id": "Connection Flow",
        "assessment": "helpful" | "wrong" | "" }
    """
    data = request.get_json(silent=True) or {}
    conversation_id = (data.get("conversation_id") or "").strip()
    turn_id = (data.get("turn_id") or "").strip()
    skill_id = (data.get("skill_id") or "").strip()
    assessment = (data.get("assessment") or "").strip().lower()

    if not conversation_id or not turn_id or not skill_id:
        return jsonify({
            "success": False,
            "error": "conversation_id, turn_id, and skill_id are required",
        }), 400
    if not _SAFE_ID_RE.match(conversation_id):
        return _bad_id_response("conversation_id")
    if not _SAFE_ID_RE.match(turn_id):
        return _bad_id_response("turn_id")
    if assessment and assessment not in feedback_service.SKILL_ASSESSMENT_VALUES:
        return jsonify({
            "success": False,
            "error": f"assessment must be one of {feedback_service.SKILL_ASSESSMENT_VALUES} or empty",
        }), 400

    session_id = session.get("chatbot_session_id", "")
    ok = feedback_service.record_skill_assessment(
        session_id=session_id,
        conversation_id=conversation_id,
        turn_id=turn_id,
        skill_id=skill_id,
        assessment=assessment,
    )
    if not ok:
        return jsonify({"success": False, "error": "failed to record"}), 500
    return jsonify({"success": True})


@feedback_bp.route("/recent", methods=["GET"])
def recent():
    """
    Debug helper: return the last N feedback events.
    Useful during MVP development to verify writes are happening.

    Query params:
      limit  — max events to return (1..500, default 50)
      domain — which feedback stream to read:
                 ""/"wifi" → feedback.jsonl       (default)
                 "bt"      → bt_feedback.jsonl
               e.g. /feedback/recent?domain=bt to confirm BT writes landed.
    """
    try:
        limit = int(request.args.get("limit", "50"))
    except ValueError:
        limit = 50
    limit = max(1, min(500, limit))
    domain = (request.args.get("domain") or "").strip()
    return jsonify({
        "success": True,
        "domain": domain or "wifi",
        "events": feedback_service.get_recent_votes(limit, domain=domain),
    })
