"""Unit tests for the feedback blueprint (blueprints/feedback/feedback_routes.py).

The feedback sidecar records thumbs-up/down votes, structured "more feedback"
details, per-step votes, helpful-skill picks and per-skill assessments, plus a
debug ``/recent`` reader. Every route validates client-supplied IDs against
``_SAFE_ID_RE = ^[A-Za-z0-9_-]{1,80}$`` before delegating to
``services.feedback_service``. Both ``feedback_service`` and ``gather_service``
are replaced with MagicMocks at the route module (point of use) in setUp, so
no JSONL feedback file or telemetry record is ever written.

This blueprint does not touch ``app_config`` (no silent_mode gating here).
"""
import unittest
import uuid
from unittest.mock import MagicMock, patch

from blueprints.feedback.feedback_routes import feedback_bp
from models.models import CaseContext
from unit_tests.base_test import BaseTestCase

ROUTES = "blueprints.feedback.feedback_routes"

CONV_ID = "conv_123"
TURN_ID = "turn-7"
SESSION_ID = "sess-abc"
FIXED_UUID = uuid.UUID("12345678-1234-5678-1234-567812345678")

# Each entry violates ^[A-Za-z0-9_-]{1,80}$ (path traversal, slash, space, too long).
UNSAFE_IDS = ["../etc", "a/b", "has space", "x" * 81]


class FeedbackTestCase(BaseTestCase):
    blueprint = feedback_bp

    def setUp(self):
        super().setUp()
        fs_patcher = patch(f"{ROUTES}.feedback_service")
        gs_patcher = patch(f"{ROUTES}.gather_service")
        self.feedback_service = fs_patcher.start()
        self.gather_service = gs_patcher.start()
        self.addCleanup(fs_patcher.stop)
        self.addCleanup(gs_patcher.stop)
        # Real allow-list from services/feedback_service.py
        self.feedback_service.SKILL_ASSESSMENT_VALUES = ("helpful", "wrong")
        self.feedback_service.record_vote.return_value = True
        self.feedback_service.record_detail.return_value = True
        self.feedback_service.record_step_vote.return_value = True
        self.feedback_service.record_helpful_skill.return_value = True
        self.feedback_service.record_skill_assessment.return_value = True
        self.feedback_service.get_recent_votes.return_value = [{"vote": 1}]

    def post_json(self, path, payload):
        return self.client.post(path, json=payload)

    def assert_bad_id(self, resp, field_name):
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.get_json(), {
            "success": False,
            "error": f"{field_name} must match [A-Za-z0-9_-]{{1,80}}",
        })


class TestFeedbackBlueprintStructure(FeedbackTestCase):

    def test_blueprint_registered_with_url_prefix(self):
        """Verify the blueprint wiring is correct.

        Input: the Flask app with ``feedback_bp`` registered (done in setUp).
        Expected: the blueprint is mounted under the ``/feedback`` prefix, the
        five write endpoints (vote, detail, step_vote, skill_helpful,
        skill_assessment) accept POST, and ``/feedback/recent`` accepts GET.
        """
        # Arrange / Act
        rules = {r.rule: r.methods for r in self.app.url_map.iter_rules()
                 if r.endpoint.startswith("feedback.")}

        # Assert
        self.assertIs(self.app.blueprints.get("feedback"), feedback_bp)
        self.assertEqual(feedback_bp.url_prefix, "/feedback")
        for path in ("/feedback/vote", "/feedback/detail", "/feedback/step_vote",
                     "/feedback/skill_helpful", "/feedback/skill_assessment"):
            self.assertIn(path, rules)
            self.assertIn("POST", rules[path])
        self.assertIn("GET", rules["/feedback/recent"])


class TestVote(FeedbackTestCase):

    def test_vote_success_records_with_session_context(self):
        """Happy-path thumbs-up vote with a populated session.

        Input: POST /feedback/vote with vote=1, whitespace-padded
        conversation_id/domain, and a session carrying session id + yaml-modified
        flag/path.
        Expected: 200 with success + the yaml-modified flag/path echoed back; the
        service receives trimmed ids/domain, vote=1, and yaml_modified=True.
        """
        # Arrange
        with self.client.session_transaction() as sess:
            sess["chatbot_session_id"] = SESSION_ID
            sess["yaml_modified"] = True
            sess["yaml_modified_path"] = r"C:\fake\skills.yaml"

        # Act
        resp = self.post_json("/feedback/vote", {
            "conversation_id": f" {CONV_ID} ", "turn_id": TURN_ID,
            "vote": 1, "domain": " bt ",
        })

        # Assert
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json(), {
            "success": True, "yaml_modified": True,
            "yaml_modified_path": r"C:\fake\skills.yaml",
        })
        self.feedback_service.record_vote.assert_called_once_with(
            session_id=SESSION_ID, conversation_id=CONV_ID, turn_id=TURN_ID,
            vote=1, yaml_modified=True, domain="bt",
        )

    def test_vote_zero_string_accepted_with_empty_session_defaults(self):
        """A vote of "0" (clear) given as a string, with no session.

        Input: POST /feedback/vote with vote="0" and no session keys set.
        Expected: 200 with success and empty yaml defaults; the service is called
        with vote coerced to int 0, empty session_id/domain, yaml_modified=False.
        """
        # Arrange: no session keys; vote "0" (clear) as a string

        # Act
        resp = self.post_json("/feedback/vote", {
            "conversation_id": CONV_ID, "turn_id": TURN_ID, "vote": "0",
        })

        # Assert
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json(), {
            "success": True, "yaml_modified": False, "yaml_modified_path": "",
        })
        self.feedback_service.record_vote.assert_called_once_with(
            session_id="", conversation_id=CONV_ID, turn_id=TURN_ID,
            vote=0, yaml_modified=False, domain="",
        )

    def test_vote_invalid_values_return_400(self):
        """Reject vote values outside the allowed {1, -1, 0} set.

        Input: POST /feedback/vote with vote being None, "up", 2, or -2.
        Expected: each returns 400 with "vote must be 1, -1, or 0" and the
        service is never called.
        """
        for bad_vote in (None, "up", 2, -2):
            with self.subTest(vote=bad_vote):
                # Act
                resp = self.post_json("/feedback/vote", {
                    "conversation_id": CONV_ID, "turn_id": TURN_ID, "vote": bad_vote,
                })

                # Assert
                self.assertEqual(resp.status_code, 400)
                self.assertEqual(resp.get_json()["error"], "vote must be 1, -1, or 0")
        self.feedback_service.record_vote.assert_not_called()

    def test_vote_non_json_body_fails_vote_check_first(self):
        """A non-JSON body is treated as empty, failing vote validation first.

        Input: POST /feedback/vote with a plain-text (non-JSON) body.
        Expected: 400 with "vote must be 1, -1, or 0" (vote is checked before the
        id presence check) and the service is never called.
        """
        # Arrange: no JSON at all -> data == {}; vote is validated before ids

        # Act
        resp = self.client.post("/feedback/vote", data="not json")

        # Assert
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.get_json()["error"], "vote must be 1, -1, or 0")
        self.feedback_service.record_vote.assert_not_called()

    def test_vote_missing_ids_return_400(self):
        """Require both conversation_id and turn_id to be present and non-blank.

        Input: valid vote but a payload missing one id, or with a whitespace-only
        conversation_id.
        Expected: each returns 400 with "conversation_id and turn_id are required"
        and the service is never called.
        """
        for payload in ({"turn_id": TURN_ID}, {"conversation_id": CONV_ID},
                        {"conversation_id": "   ", "turn_id": TURN_ID}):
            with self.subTest(payload=payload):
                # Act
                resp = self.post_json("/feedback/vote", {**payload, "vote": 1})

                # Assert
                self.assertEqual(resp.status_code, 400)
                self.assertEqual(resp.get_json()["error"],
                                 "conversation_id and turn_id are required")
        self.feedback_service.record_vote.assert_not_called()

    def test_vote_rejects_unsafe_ids(self):
        """Reject ids that violate the ^[A-Za-z0-9_-]{1,80}$ safe-id pattern.

        Input: POST /feedback/vote with an unsafe conversation_id or turn_id
        (path traversal, slash, space, over-length).
        Expected: each returns 400 with the safe-id error naming the offending
        field and the service is never called.
        """
        for bad in UNSAFE_IDS:
            with self.subTest(conversation_id=bad):
                resp = self.post_json("/feedback/vote",
                                      {"conversation_id": bad, "turn_id": TURN_ID, "vote": 1})
                self.assert_bad_id(resp, "conversation_id")
            with self.subTest(turn_id=bad):
                resp = self.post_json("/feedback/vote",
                                      {"conversation_id": CONV_ID, "turn_id": bad, "vote": 1})
                self.assert_bad_id(resp, "turn_id")
        self.feedback_service.record_vote.assert_not_called()

    def test_vote_service_failure_returns_500(self):
        """Surface a service-level failure as a 500.

        Input: a valid vote request, but record_vote returns False.
        Expected: 500 with {"success": False, "error": "failed to record vote"}.
        """
        # Arrange
        self.feedback_service.record_vote.return_value = False

        # Act
        resp = self.post_json("/feedback/vote",
                              {"conversation_id": CONV_ID, "turn_id": TURN_ID, "vote": -1})

        # Assert
        self.assertEqual(resp.status_code, 500)
        self.assertEqual(resp.get_json(), {"success": False, "error": "failed to record vote"})

    def test_vote_service_exception_is_not_caught(self):
        """Document that the route does not swallow service exceptions.

        Input: a valid vote request, but record_vote raises RuntimeError.
        Expected: the exception propagates out of the route (becomes a 500 in
        production) rather than being caught and reshaped.
        """
        # Arrange: the route has no try/except around the service call, so a
        # raising service surfaces as an unhandled error (500 in production).
        self.feedback_service.record_vote.side_effect = RuntimeError("disk full")

        # Act / Assert
        with self.assertRaises(RuntimeError):
            self.post_json("/feedback/vote",
                           {"conversation_id": CONV_ID, "turn_id": TURN_ID, "vote": 1})


class TestDetail(FeedbackTestCase):

    @patch(f"{ROUTES}.uuid.uuid4", return_value=FIXED_UUID)
    def test_detail_success_forwards_all_fields_and_records_gather(self, _mock_uuid):
        """Full "more feedback" submission forwards every field downstream.

        Input: POST /feedback/detail with all optional fields populated (issues,
        correction fields, evidence lines, skill/step feedback, issue-time fields,
        attach_log=True, padded domain) plus a session with log path + workflow id.
        Expected: 200 with success + the generated feedback_event_id; record_detail
        receives all fields trimmed/normalised, and gather_service records a
        feedback-submit event tied to the same event id, workflow, and issue.
        """
        # Arrange
        self.seed_session(
            chatbot_session_id=SESSION_ID, yaml_modified=True,
            chatbot_log_path=r"C:\fake\session.log", gather_workflow_id="wf-1",
        )
        issues = [{"scope": "overall", "category": "bad_output", "comment": "wrong"}]
        skill_feedback = [{"skill_id": "Connection Flow", "verdict": "wrong"}]
        step_feedback = [{"step_index": 2, "verdict": "helpful"}]

        # Act
        resp = self.post_json("/feedback/detail", {
            "conversation_id": CONV_ID, "turn_id": TURN_ID, "vote": "-1",
            "issues": issues,
            "correct_root_cause": " AP deauth ", "correct_conclusion_tag": " deauth ",
            "correct_skill": " Roaming ", "correct_approach": " check 802.11 mgmt ",
            "evidence_log_lines": ["line 1", "line 2"],
            "agent_workflow": " wf ", "feedback_layer": " skill ",
            "skill_feedback": skill_feedback, "step_feedback": step_feedback,
            "issue_time_problem": " wrong_time ", "correct_issue_time": " 10:00 ",
            "used_issue_time": " 09:00 ", "log_has_date": False,
            "attach_log": True, "domain": " BT ",
        })

        # Assert
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json(), {"success": True, "feedback_event_id": str(FIXED_UUID)})
        self.feedback_service.record_detail.assert_called_once_with(
            session_id=SESSION_ID, conversation_id=CONV_ID, turn_id=TURN_ID, vote=-1,
            issues=issues, correct_root_cause="AP deauth", correct_conclusion_tag="deauth",
            correct_skill="Roaming", correct_approach="check 802.11 mgmt",
            evidence_log_lines=["line 1", "line 2"], agent_workflow="wf",
            feedback_layer="skill", skill_feedback=skill_feedback, step_feedback=step_feedback,
            issue_time_problem="wrong_time", correct_issue_time="10:00", used_issue_time="09:00",
            log_has_date=False, yaml_modified=True, log_path=r"C:\fake\session.log",
            attach_log=True, domain="bt", feedback_event_id=str(FIXED_UUID),
        )
        self.gather_service.record_feedback_submit.assert_called_once_with(
            feedback_event_id=str(FIXED_UUID), conversation_id=CONV_ID, turn_id=TURN_ID,
            workflow_id="wf-1", session_id=SESSION_ID,
            issue=self.create_mock_case_context(), domain="bt",
        )

    @patch(f"{ROUTES}.uuid.uuid4", return_value=FIXED_UUID)
    def test_detail_minimal_payload_uses_defaults_and_ignores_log_without_opt_in(self, _mock_uuid):
        """A bare submission falls back to defaults and never attaches the log.

        Input: POST /feedback/detail with only the ids; the session has a log path
        but attach_log is omitted.
        Expected: 200; record_detail gets vote=None, empty list defaults,
        log_has_date=True, attach_log=False, empty log_path, domain="wifi", empty
        session_id; gather_service records a default CaseContext and empty workflow.
        """
        # Arrange: a log path exists in session, but attach_log is not set
        with self.client.session_transaction() as sess:
            sess["chatbot_log_path"] = r"C:\fake\session.log"

        # Act
        resp = self.post_json("/feedback/detail", {"conversation_id": CONV_ID, "turn_id": TURN_ID})

        # Assert
        self.assertEqual(resp.status_code, 200)
        kwargs = self.feedback_service.record_detail.call_args.kwargs
        self.assertIsNone(kwargs["vote"])
        self.assertEqual(kwargs["issues"], [])
        self.assertEqual(kwargs["evidence_log_lines"], [])
        self.assertEqual(kwargs["skill_feedback"], [])
        self.assertEqual(kwargs["step_feedback"], [])
        self.assertIs(kwargs["log_has_date"], True)
        self.assertIs(kwargs["attach_log"], False)
        self.assertEqual(kwargs["log_path"], "")
        self.assertEqual(kwargs["domain"], "wifi")
        self.assertEqual(kwargs["session_id"], "")
        gather_kwargs = self.gather_service.record_feedback_submit.call_args.kwargs
        # No case_context in session -> from_session({}) -> default CaseContext
        self.assertEqual(gather_kwargs["issue"], CaseContext().to_dict())
        self.assertEqual(gather_kwargs["workflow_id"], "")
        self.assertEqual(gather_kwargs["domain"], "wifi")

    def test_detail_invalid_vote_values_are_coerced_to_none(self):
        """Unparseable/out-of-range votes become None rather than erroring.

        Input: POST /feedback/detail with vote being "", "abc", 5, 0, or [1].
        Expected: each returns 200 and record_detail is called with vote=None
        (detail accepts any vote; only 1/-1 are meaningful).
        """
        for raw_vote in ("", "abc", 5, 0, [1]):
            with self.subTest(vote=raw_vote):
                # Act
                resp = self.post_json("/feedback/detail", {
                    "conversation_id": CONV_ID, "turn_id": TURN_ID, "vote": raw_vote,
                })

                # Assert
                self.assertEqual(resp.status_code, 200)
                self.assertIsNone(self.feedback_service.record_detail.call_args.kwargs["vote"])

    def test_detail_evidence_string_is_split_into_lines(self):
        """A multiline evidence string is split into trimmed, non-empty lines.

        Input: POST /feedback/detail with evidence_log_lines as a single string
        containing blank/whitespace lines.
        Expected: 200; record_detail receives ["first line", "second line"] with
        blank lines dropped and surrounding whitespace stripped.
        """
        # Act
        resp = self.post_json("/feedback/detail", {
            "conversation_id": CONV_ID, "turn_id": TURN_ID,
            "evidence_log_lines": "first line  \n\n   \nsecond line\n",
        })

        # Assert
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(
            self.feedback_service.record_detail.call_args.kwargs["evidence_log_lines"],
            ["first line", "second line"],
        )

    def test_detail_evidence_of_other_type_becomes_empty_list(self):
        """Evidence of an unsupported type degrades to an empty list.

        Input: POST /feedback/detail with evidence_log_lines=42 (not a list/str).
        Expected: 200; record_detail receives evidence_log_lines=[].
        """
        # Act
        resp = self.post_json("/feedback/detail", {
            "conversation_id": CONV_ID, "turn_id": TURN_ID, "evidence_log_lines": 42,
        })

        # Assert
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self.feedback_service.record_detail.call_args.kwargs["evidence_log_lines"], [])

    def test_detail_missing_ids_return_400(self):
        """Require both ids on the detail endpoint.

        Input: POST /feedback/detail missing one or both of the ids.
        Expected: each returns 400 with "conversation_id and turn_id are
        required"; neither the feedback nor gather service is called.
        """
        for payload in ({}, {"conversation_id": CONV_ID}, {"turn_id": TURN_ID}):
            with self.subTest(payload=payload):
                resp = self.post_json("/feedback/detail", payload)
                self.assertEqual(resp.status_code, 400)
                self.assertEqual(resp.get_json()["error"], "conversation_id and turn_id are required")
        self.feedback_service.record_detail.assert_not_called()
        self.gather_service.record_feedback_submit.assert_not_called()

    def test_detail_rejects_unsafe_ids(self):
        """Reject unsafe ids on the detail endpoint.

        Input: POST /feedback/detail with an unsafe conversation_id or turn_id.
        Expected: each returns 400 with the safe-id error for the offending field
        and record_detail is never called.
        """
        for bad in UNSAFE_IDS:
            with self.subTest(conversation_id=bad):
                self.assert_bad_id(self.post_json("/feedback/detail",
                                                  {"conversation_id": bad, "turn_id": TURN_ID}),
                                   "conversation_id")
            with self.subTest(turn_id=bad):
                self.assert_bad_id(self.post_json("/feedback/detail",
                                                  {"conversation_id": CONV_ID, "turn_id": bad}),
                                   "turn_id")
        self.feedback_service.record_detail.assert_not_called()

    def test_detail_nothing_recorded_returns_400_and_skips_gather(self):
        """An all-empty submission is rejected and does not log a gather event.

        Input: POST /feedback/detail with only ids; record_detail returns False
        (nothing worth recording).
        Expected: 400 with "nothing to record (all fields empty)" and
        gather_service.record_feedback_submit is not called.
        """
        # Arrange
        self.feedback_service.record_detail.return_value = False

        # Act
        resp = self.post_json("/feedback/detail", {"conversation_id": CONV_ID, "turn_id": TURN_ID})

        # Assert
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.get_json(), {"success": False, "error": "nothing to record (all fields empty)"})
        self.gather_service.record_feedback_submit.assert_not_called()


class TestStepVote(FeedbackTestCase):

    def test_step_vote_success(self):
        """Happy-path per-step vote.

        Input: POST /feedback/step_vote with step_index="3" (string), vote=-1,
        padded domain, and a session id.
        Expected: 200 with success; record_step_vote receives step_index coerced
        to int 3, vote=-1, and the trimmed domain.
        """
        # Arrange
        with self.client.session_transaction() as sess:
            sess["chatbot_session_id"] = SESSION_ID

        # Act
        resp = self.post_json("/feedback/step_vote", {
            "conversation_id": CONV_ID, "turn_id": TURN_ID,
            "step_index": "3", "vote": -1, "domain": " bt ",
        })

        # Assert
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json(), {"success": True})
        self.feedback_service.record_step_vote.assert_called_once_with(
            session_id=SESSION_ID, conversation_id=CONV_ID, turn_id=TURN_ID,
            step_index=3, vote=-1, domain="bt",
        )

    def test_step_vote_missing_ids_return_400(self):
        """Require both ids on the step-vote endpoint.

        Input: POST /feedback/step_vote missing conversation_id.
        Expected: 400 with "conversation_id and turn_id are required" and the
        service is never called.
        """
        # Act
        resp = self.post_json("/feedback/step_vote", {"turn_id": TURN_ID, "step_index": 1, "vote": 1})

        # Assert
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.get_json()["error"], "conversation_id and turn_id are required")
        self.feedback_service.record_step_vote.assert_not_called()

    def test_step_vote_rejects_unsafe_ids(self):
        """Reject unsafe ids on the step-vote endpoint.

        Input: POST /feedback/step_vote with an unsafe conversation_id or turn_id.
        Expected: each returns 400 with the safe-id error for the offending field
        and the service is never called.
        """
        base = {"step_index": 1, "vote": 1}
        for bad in UNSAFE_IDS:
            with self.subTest(conversation_id=bad):
                self.assert_bad_id(self.post_json("/feedback/step_vote",
                                                  {**base, "conversation_id": bad, "turn_id": TURN_ID}),
                                   "conversation_id")
            with self.subTest(turn_id=bad):
                self.assert_bad_id(self.post_json("/feedback/step_vote",
                                                  {**base, "conversation_id": CONV_ID, "turn_id": bad}),
                                   "turn_id")
        self.feedback_service.record_step_vote.assert_not_called()

    def test_step_vote_non_integer_fields_return_400(self):
        """Require step_index and vote to be integer-coercible.

        Input: POST /feedback/step_vote with a non-numeric step_index, a None
        vote, or a missing step_index.
        Expected: each returns 400 with "step_index and vote must be integers"
        and the service is never called.
        """
        for payload in ({"step_index": "x", "vote": 1}, {"step_index": 1, "vote": None},
                        {"vote": 1}):
            with self.subTest(payload=payload):
                resp = self.post_json("/feedback/step_vote",
                                      {"conversation_id": CONV_ID, "turn_id": TURN_ID, **payload})
                self.assertEqual(resp.status_code, 400)
                self.assertEqual(resp.get_json()["error"], "step_index and vote must be integers")
        self.feedback_service.record_step_vote.assert_not_called()

    def test_step_vote_zero_vote_rejected(self):
        """A zero (clear) vote is not allowed per step.

        Input: POST /feedback/step_vote with vote=0.
        Expected: 400 with "vote must be 1 or -1" (unlike /vote, step votes
        cannot be cleared) and the service is never called.
        """
        # Act: unlike /vote, 0 (clear) is NOT accepted per step
        resp = self.post_json("/feedback/step_vote",
                              {"conversation_id": CONV_ID, "turn_id": TURN_ID, "step_index": 1, "vote": 0})

        # Assert
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.get_json(), {"success": False, "error": "vote must be 1 or -1"})
        self.feedback_service.record_step_vote.assert_not_called()

    def test_step_vote_service_failure_returns_500(self):
        """Surface a step-vote service failure as a 500.

        Input: a valid step-vote request, but record_step_vote returns False.
        Expected: 500 with {"success": False, "error": "failed to record"}.
        """
        # Arrange
        self.feedback_service.record_step_vote.return_value = False

        # Act
        resp = self.post_json("/feedback/step_vote",
                              {"conversation_id": CONV_ID, "turn_id": TURN_ID, "step_index": 1, "vote": 1})

        # Assert
        self.assertEqual(resp.status_code, 500)
        self.assertEqual(resp.get_json(), {"success": False, "error": "failed to record"})


class TestSkillHelpful(FeedbackTestCase):

    def test_skill_helpful_success_allows_spaces_in_skill_id(self):
        """Mark a skill helpful; skill_id may contain internal spaces.

        Input: POST /feedback/skill_helpful with a padded skill_id containing a
        space (" Connection Flow ") and a session id.
        Expected: 200 with success; record_helpful_skill receives the skill_id
        trimmed at the ends but with the internal space preserved.
        """
        # Arrange
        with self.client.session_transaction() as sess:
            sess["chatbot_session_id"] = SESSION_ID

        # Act
        resp = self.post_json("/feedback/skill_helpful", {
            "conversation_id": CONV_ID, "turn_id": TURN_ID,
            "skill_id": " Connection Flow ", "domain": "bt",
        })

        # Assert
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json(), {"success": True})
        self.feedback_service.record_helpful_skill.assert_called_once_with(
            session_id=SESSION_ID, conversation_id=CONV_ID, turn_id=TURN_ID,
            skill_id="Connection Flow", domain="bt",
        )

    def test_skill_helpful_missing_fields_return_400(self):
        """Require conversation_id, turn_id, and skill_id together.

        Input: POST /feedback/skill_helpful missing any one of the three fields.
        Expected: each returns 400 with "conversation_id, turn_id, and skill_id
        are required" and the service is never called.
        """
        for payload in ({"conversation_id": CONV_ID, "turn_id": TURN_ID},
                        {"conversation_id": CONV_ID, "skill_id": "S"},
                        {"turn_id": TURN_ID, "skill_id": "S"}):
            with self.subTest(payload=payload):
                resp = self.post_json("/feedback/skill_helpful", payload)
                self.assertEqual(resp.status_code, 400)
                self.assertEqual(resp.get_json()["error"],
                                 "conversation_id, turn_id, and skill_id are required")
        self.feedback_service.record_helpful_skill.assert_not_called()

    def test_skill_helpful_rejects_unsafe_ids(self):
        """Reject unsafe ids on the skill-helpful endpoint.

        Input: POST /feedback/skill_helpful with an unsafe conversation_id or
        turn_id.
        Expected: each returns 400 with the safe-id error for the offending field
        and the service is never called.
        """
        for bad in UNSAFE_IDS:
            with self.subTest(conversation_id=bad):
                self.assert_bad_id(self.post_json("/feedback/skill_helpful",
                                                  {"conversation_id": bad, "turn_id": TURN_ID, "skill_id": "S"}),
                                   "conversation_id")
            with self.subTest(turn_id=bad):
                self.assert_bad_id(self.post_json("/feedback/skill_helpful",
                                                  {"conversation_id": CONV_ID, "turn_id": bad, "skill_id": "S"}),
                                   "turn_id")
        self.feedback_service.record_helpful_skill.assert_not_called()

    def test_skill_helpful_service_failure_returns_500(self):
        """Surface a skill-helpful service failure as a 500.

        Input: a valid request, but record_helpful_skill returns False.
        Expected: 500 with {"success": False, "error": "failed to record"}.
        """
        # Arrange
        self.feedback_service.record_helpful_skill.return_value = False

        # Act
        resp = self.post_json("/feedback/skill_helpful",
                              {"conversation_id": CONV_ID, "turn_id": TURN_ID, "skill_id": "S"})

        # Assert
        self.assertEqual(resp.status_code, 500)
        self.assertEqual(resp.get_json(), {"success": False, "error": "failed to record"})


class TestSkillAssessment(FeedbackTestCase):

    def test_skill_assessment_success_normalises_case(self):
        """Record a per-skill assessment, lowercasing/trimming the value.

        Input: POST /feedback/skill_assessment with assessment=" Helpful " and a
        session id.
        Expected: 200 with success; record_skill_assessment receives the value
        normalised to "helpful".
        """
        # Arrange
        with self.client.session_transaction() as sess:
            sess["chatbot_session_id"] = SESSION_ID

        # Act
        resp = self.post_json("/feedback/skill_assessment", {
            "conversation_id": CONV_ID, "turn_id": TURN_ID,
            "skill_id": "Connection Flow", "assessment": " Helpful ",
        })

        # Assert
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json(), {"success": True})
        self.feedback_service.record_skill_assessment.assert_called_once_with(
            session_id=SESSION_ID, conversation_id=CONV_ID, turn_id=TURN_ID,
            skill_id="Connection Flow", assessment="helpful",
        )

    def test_skill_assessment_empty_value_clears_chip(self):
        """An omitted assessment clears the skill's assessment chip.

        Input: POST /feedback/skill_assessment with no assessment field.
        Expected: 200; record_skill_assessment is called with assessment=""
        (empty is allowed and means "clear").
        """
        # Act
        resp = self.post_json("/feedback/skill_assessment",
                              {"conversation_id": CONV_ID, "turn_id": TURN_ID, "skill_id": "S"})

        # Assert
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self.feedback_service.record_skill_assessment.call_args.kwargs["assessment"], "")

    def test_skill_assessment_invalid_value_returns_400(self):
        """Reject assessment values outside the allow-list.

        Input: POST /feedback/skill_assessment with assessment="redundant" (not in
        SKILL_ASSESSMENT_VALUES = ("helpful", "wrong")).
        Expected: 400 with "assessment must be one of ('helpful', 'wrong') or
        empty" and the service is never called.
        """
        # Act
        resp = self.post_json("/feedback/skill_assessment", {
            "conversation_id": CONV_ID, "turn_id": TURN_ID, "skill_id": "S", "assessment": "redundant",
        })

        # Assert
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.get_json()["error"],
                         "assessment must be one of ('helpful', 'wrong') or empty")
        self.feedback_service.record_skill_assessment.assert_not_called()

    def test_skill_assessment_missing_fields_return_400(self):
        """Require conversation_id, turn_id, and skill_id together.

        Input: POST /feedback/skill_assessment missing skill_id.
        Expected: 400 with "conversation_id, turn_id, and skill_id are required"
        and the service is never called.
        """
        # Act
        resp = self.post_json("/feedback/skill_assessment",
                              {"conversation_id": CONV_ID, "turn_id": TURN_ID, "assessment": "wrong"})

        # Assert
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.get_json()["error"], "conversation_id, turn_id, and skill_id are required")
        self.feedback_service.record_skill_assessment.assert_not_called()

    def test_skill_assessment_rejects_unsafe_ids(self):
        """Reject unsafe ids on the skill-assessment endpoint.

        Input: POST /feedback/skill_assessment with an unsafe conversation_id or
        turn_id.
        Expected: each returns 400 with the safe-id error for the offending field
        and the service is never called.
        """
        for bad in UNSAFE_IDS:
            with self.subTest(conversation_id=bad):
                self.assert_bad_id(self.post_json("/feedback/skill_assessment",
                                                  {"conversation_id": bad, "turn_id": TURN_ID, "skill_id": "S"}),
                                   "conversation_id")
            with self.subTest(turn_id=bad):
                self.assert_bad_id(self.post_json("/feedback/skill_assessment",
                                                  {"conversation_id": CONV_ID, "turn_id": bad, "skill_id": "S"}),
                                   "turn_id")
        self.feedback_service.record_skill_assessment.assert_not_called()

    def test_skill_assessment_service_failure_returns_500(self):
        """Surface a skill-assessment service failure as a 500.

        Input: a valid request, but record_skill_assessment returns False.
        Expected: 500 with {"success": False, "error": "failed to record"}.
        """
        # Arrange
        self.feedback_service.record_skill_assessment.return_value = False

        # Act
        resp = self.post_json("/feedback/skill_assessment", {
            "conversation_id": CONV_ID, "turn_id": TURN_ID, "skill_id": "S", "assessment": "wrong",
        })

        # Assert
        self.assertEqual(resp.status_code, 500)
        self.assertEqual(resp.get_json(), {"success": False, "error": "failed to record"})


class TestRecent(FeedbackTestCase):

    def test_recent_defaults_to_50_and_wifi(self):
        """The debug reader defaults to 50 events and the wifi domain.

        Input: GET /feedback/recent with no query params.
        Expected: 200 with the mocked events and domain="wifi"; get_recent_votes
        is called with limit=50 and domain="".
        """
        # Act
        resp = self.client.get("/feedback/recent")

        # Assert
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json(), {"success": True, "domain": "wifi", "events": [{"vote": 1}]})
        self.feedback_service.get_recent_votes.assert_called_once_with(50, domain="")

    def test_recent_bt_domain_and_explicit_limit(self):
        """Honour explicit limit and domain query params.

        Input: GET /feedback/recent?limit=10&domain=bt.
        Expected: response domain is "bt"; get_recent_votes is called with
        limit=10 and domain="bt".
        """
        # Act
        resp = self.client.get("/feedback/recent?limit=10&domain=bt")

        # Assert
        self.assertEqual(resp.get_json()["domain"], "bt")
        self.feedback_service.get_recent_votes.assert_called_once_with(10, domain="bt")

    def test_recent_limit_is_clamped_and_non_numeric_falls_back(self):
        """Clamp the limit to [1, 500] and fall back to 50 for non-numeric input.

        Input: GET /feedback/recent?limit=<raw> for raw in "0", "-5", "9999",
        "abc".
        Expected: get_recent_votes is called with the clamped value 1, 1, 500,
        and the default 50 respectively.
        """
        for raw, expected in (("0", 1), ("-5", 1), ("9999", 500), ("abc", 50)):
            with self.subTest(limit=raw):
                # Arrange
                self.feedback_service.get_recent_votes.reset_mock()

                # Act
                resp = self.client.get(f"/feedback/recent?limit={raw}")

                # Assert
                self.assertEqual(resp.status_code, 200)
                self.feedback_service.get_recent_votes.assert_called_once_with(expected, domain="")


if __name__ == "__main__":
    unittest.main()
