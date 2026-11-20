"""Core-flow unit tests for the generic_chatbot blueprint
(blueprints/chatbot/chatbot_routes.py, GENERIC_PROFILE).

The Generic chatbot is the domain-agnostic twin of log_chatbot/bt_chatbot/
linux_chatbot: a per-session GenericLogAgentSystem analyses a log of unknown
provenance and streams answers over SSE. Everything that would touch an LLM,
the network, the share folder or the disk is patched at its point of use in
the route module:

  * ``_get_or_create_agent`` / ``_resume_agent_for`` -> MagicMock agent
  * ``chat_jobs`` / ``history_service`` / ``feedback_service`` / ``gather_service``
  * ``threading`` (chat worker runs inline)
  * ``check_ips_service`` / ``find_event_log_for_log``

Generic-specific behavior pinned here (differs from its siblings):
  * every service call is tagged ``domain="generic"``;
  * the UI config is ``GENERIC_UI``, whose ``features`` dict is identical to
    ``BT_UI``'s, so the full route table matches bt_chatbot's shape 1:1 under
    the ``/generic_chatbot`` prefix (verified by TestGenericChatbotStructure,
    not assumed).

Scope note (docs/workpad/generic_log_chatbot/07_unit_tests.md): this module
covers blueprint registration/routes, /set_log, /chat, /reset, and
/history/list + /history/get only -- the owner chose "core flows" over full
parity with test_bt_chatbot_blueprint.py's breadth. The skills-editor,
prompts-editor, /suggest_issue_times, /browse*, /history/stream, /delete,
/rename, /pin, /load, /chat/stop, /prepare and index-page suites are
intentionally not duplicated here.
"""
import json
import queue
import unittest
from datetime import datetime
from unittest.mock import ANY, MagicMock, patch

import blueprints.chatbot.chatbot_routes as routes
from blueprints.chatbot.chatbot_routes import generic_chatbot_bp
from configs.chatbot_ui import GENERIC_UI
from unit_tests.base_test import BaseTestCase, TEST_CASE_NBR

ROUTES = "blueprints.chatbot.chatbot_routes"


class _SyncThread:
    """Thread stand-in that runs the target inline so the chat worker finishes
    within the request instead of on a real background thread."""

    def __init__(self, target=None, daemon=None, **kwargs):
        self._target = target

    def start(self):
        if self._target is not None:
            self._target()

    def join(self, timeout=None):
        pass


LOG_PATH = r"C:\fake\generic\case-2026-04-08\device.log"
OTHER_LOG = r"C:\fake\generic\other\boot.txt"
SKILLS = [{"name": "Whole_Log_Unfiltered", "description": "No-op filter over the full log"}]
CONV_ID = "conv-123"


def make_agent(**attrs):
    """MagicMock agent with the plain attributes the routes read/write."""
    agent = MagicMock(name="agent")
    agent.current_log_path = ""
    agent.conversation_history = []
    agent.issue_time = None
    agent.issue_context = {}
    agent.model = "test-model"
    agent.get_skill_descriptions.return_value = SKILLS
    agent.get_log_span_minutes.return_value = 30
    agent._log_has_date.return_value = True
    agent._session_skill_path = None
    for key, value in attrs.items():
        setattr(agent, key, value)
    return agent


def sse_events(resp):
    """Decode an SSE body into a list of JSON payloads."""
    body = resp.get_data(as_text=True)
    return [json.loads(chunk[len("data: "):]) for chunk in body.split("\n\n") if chunk.startswith("data: ")]


class GenericChatbotTestCase(BaseTestCase):
    blueprint = generic_chatbot_bp

    # Handlers are methods on the per-profile AgentRoutes instance rather than
    # module-level functions, so these names are patched on that instance
    # (``self.routes``) instead of on the route module.
    INSTANCE_METHODS = frozenset({
        "_get_or_create_agent", "_resume_agent_for", "_issue_context_organized",
        "_get_configuration_agent", "_get_llm_client_model",
    })

    def setUp(self):
        super().setUp()
        self.routes = self.app.view_functions["generic_chatbot.chat"].__self__
        instances = patch.dict(self.routes.session_agents, clear=True)
        instances.start()
        self.addCleanup(instances.stop)

        # IPS gating: blocking_state() gates chat SSE (428), prompt_state() is
        # merged into set_log responses, start_new_session() runs on reset.
        ips = patch(f"{ROUTES}.check_ips_service")
        self.mock_check_ips = ips.start()
        self.addCleanup(ips.stop)
        self.mock_check_ips.blocking_state.return_value = None
        self.mock_check_ips.prompt_state.return_value = {}
        self.mock_check_ips.start_new_session.return_value = None

        evt = patch(f"{ROUTES}.find_event_log_for_log", return_value="")
        self.mock_find_evt = evt.start()
        self.addCleanup(evt.stop)

    def use_base_agent(self, agent=None):
        """Make the REAL ``_get_or_create_agent`` clone-return ``agent``.

        A base whose ``__new__`` returns a preset mock lets the factory
        (reset) hand out the same agent without patching the module symbol.
        """
        agent = agent if agent is not None else make_agent()

        class _CloneBase:
            client = "generic-client"
            model = "generic-model"
            skills = None
            ace_runner = None

            def __new__(cls, *args, **kwargs):
                return agent

        self.app_config.generic_chatbot_agent = object.__new__(_CloneBase)
        return agent

    def patch_route(self, name, **kwargs):
        """Patch a route symbol, routing method names onto ``self.routes``."""
        if name in self.INSTANCE_METHODS:
            patcher = patch.object(self.routes, name, **kwargs)
        else:
            patcher = patch(f"{ROUTES}.{name}", **kwargs)
        mocked = patcher.start()
        self.addCleanup(patcher.stop)
        return mocked

    def patch_target(self, target, **kwargs):
        patcher = patch(target, **kwargs)
        mocked = patcher.start()
        self.addCleanup(patcher.stop)
        return mocked

    def patch_services(self):
        self.feedback = self.patch_route("feedback_service")
        self.history = self.patch_route("history_service")
        self.gather = self.patch_route("gather_service")
        self.jobs = self.patch_route("chat_jobs")


# ======================================================================
# Structure
# ======================================================================
class TestGenericChatbotStructure(GenericChatbotTestCase):
    EXPECTED_RULES = {
        "/generic_chatbot/": {"GET"},
        "/generic_chatbot/browse": {"GET"},
        "/generic_chatbot/set_log": {"POST"},
        "/generic_chatbot/suggest_issue_times": {"POST"},
        "/generic_chatbot/chat": {"POST"},
        "/generic_chatbot/reset": {"POST"},
        "/generic_chatbot/chat/stop": {"POST"},
        "/generic_chatbot/history/list": {"GET"},
        "/generic_chatbot/history/stream": {"GET"},
        "/generic_chatbot/history/get": {"GET"},
        "/generic_chatbot/history/delete": {"POST"},
        "/generic_chatbot/history/rename": {"POST"},
        "/generic_chatbot/history/pin": {"POST"},
        "/generic_chatbot/history/load": {"POST"},
        "/generic_chatbot/back_to_avatar": {"POST"},
        "/generic_chatbot/prepare": {"POST"},
        "/generic_chatbot/browse_yaml": {"GET"},
        "/generic_chatbot/load_skills_yaml": {"POST"},
        "/generic_chatbot/append_skills_yaml": {"POST"},
        "/generic_chatbot/reload_from_shared": {"POST"},
        "/generic_chatbot/skills": {"GET"},
        "/generic_chatbot/get_issue_context": {"GET"},
        "/generic_chatbot/skills_yaml_status": {"GET"},
        "/generic_chatbot/skills_yaml_use_cloud": {"POST"},
        "/generic_chatbot/skills_yaml_use_user": {"POST"},
        "/generic_chatbot/refresh_cloud_baseline": {"POST"},
        "/generic_chatbot/load_local_skills_yaml": {"GET"},
        "/generic_chatbot/save_local_skills_yaml": {"POST"},
        "/generic_chatbot/delete_local_skill": {"POST"},
        "/generic_chatbot/upload_modified_yaml": {"POST"},
        "/generic_chatbot/prompts/status": {"GET"},
        "/generic_chatbot/prompts/get": {"GET"},
        "/generic_chatbot/prompts/save": {"POST"},
        "/generic_chatbot/prompts/keep": {"POST"},
        "/generic_chatbot/prompts/use_source": {"POST"},
        "/generic_chatbot/prompts/revert": {"POST"},
        "/generic_chatbot/prompts/refresh_shared": {"POST"},
        "/generic_chatbot/prompts/upload": {"POST"},
    }

    def test_blueprint_registered_with_url_prefix(self):
        """Verify the blueprint object and its URL prefix.

        Input: the Flask app with ``generic_chatbot_bp`` registered (via setUp).
        Expected: the registered "generic_chatbot" blueprint is this object and
        its url_prefix is "/generic_chatbot".
        """
        # Arrange / Act
        registered = self.app.blueprints.get("generic_chatbot")

        # Assert
        self.assertIs(registered, generic_chatbot_bp)
        self.assertEqual(generic_chatbot_bp.url_prefix, "/generic_chatbot")

    def test_all_38_routes_registered_with_methods(self):
        """All 38 generic_chatbot routes are registered with the right methods.

        Input: the app's URL map filtered to generic_chatbot.* endpoints.
        Expected: the rule set exactly matches EXPECTED_RULES (38 rules, the
        same shape bt_chatbot registers under its own prefix, since GENERIC_UI
        and BT_UI declare identical ``features``) and each rule supports its
        expected HTTP methods.
        """
        # Arrange / Act
        rules = {r.rule: r.methods for r in self.app.url_map.iter_rules()
                 if r.endpoint.startswith("generic_chatbot.")}

        # Assert
        self.assertEqual(set(rules), set(self.EXPECTED_RULES))
        self.assertEqual(len(rules), 38)
        for rule, methods in self.EXPECTED_RULES.items():
            self.assertTrue(methods.issubset(rules[rule]), rule)


# ======================================================================
# /set_log
# ======================================================================
class TestSetLog(GenericChatbotTestCase):

    def setUp(self):
        super().setUp()
        self.patch_services()
        self.agent = make_agent()
        self.get_agent = self.patch_route("_get_or_create_agent", return_value=self.agent)
        self.ctx = {"case_nbr": TEST_CASE_NBR, "subject": "Unexplained device crash"}
        self.patch_route("_extract_issue_context", return_value=self.ctx)
        self.read_range = self.patch_route(
            "read_log_time_range",
            return_value=(datetime(2026, 4, 8, 0, 0, 0), datetime(2026, 4, 8, 0, 30, 0)))
        self.find_evt = self.patch_route("find_event_log_for_log", return_value=r"C:\fake\generic\System.evtx")

    def test_missing_log_path_returns_400(self):
        """A blank log_path is rejected.

        Input: POST /set_log with log_path="  ".
        Expected: 400 with "log_path is required"; no agent is created.
        """
        # Act
        resp = self.client.post("/generic_chatbot/set_log", json={"log_path": "  "})

        # Assert
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.get_json(), {"success": False, "error": "log_path is required"})
        self.get_agent.assert_not_called()

    def test_first_load_sets_log_primes_agent_and_returns_metadata(self):
        """First-time log load primes the agent and returns full metadata.

        Input: POST /set_log with a fresh LOG_PATH and an empty session.
        Expected: 200; the agent gets the log path, resets, and is primed with
        the issue context; the response carries skills, span, last time,
        has-date, an empty evtx path (GENERIC_UI's windows_event_log is False,
        so find_event_log_for_log is never consulted -- unlike BT/Wi-Fi), and
        rotated=False with a new conversation id; the session stores the log
        path and a generic conversation is ensured.
        """
        # Act
        resp = self.client.post("/generic_chatbot/set_log", json={"log_path": LOG_PATH})

        # Assert
        body = resp.get_json()
        self.assertEqual(resp.status_code, 200)
        self.get_agent.assert_called_once_with(skip_prime=True)
        self.assertEqual(self.agent.current_log_path, LOG_PATH)
        self.agent.reset_conversation.assert_called_once_with()
        self.agent.prime_with_context.assert_called_once_with(**self.ctx)
        self.assertTrue(body["success"])
        self.assertEqual(body["message"], f"Log file set: {LOG_PATH}")
        self.assertEqual(body["skills"], SKILLS)
        self.assertEqual(body["log_span_minutes"], 30)
        self.assertEqual(body["log_last_time"], "04/08/2026-00:30:00.000")
        self.assertTrue(body["log_has_date"])
        self.assertEqual(body["evtx_path"], "")
        self.find_evt.assert_not_called()
        self.assertFalse(body["rotated"])
        self.assertEqual(body["previous_log_path"], "")
        sess = self.get_session()
        self.assertEqual(sess["chatbot_log_path"], LOG_PATH)
        self.assertEqual(body["new_conversation_id"], sess["feedback_conversation_id"])
        self.feedback.ensure_conversation.assert_called_once_with(
            conversation_id=body["new_conversation_id"], session_id="",
            issue=self.ctx, log_path=LOG_PATH, domain="generic")

    def test_switching_to_a_different_log_rotates_conversation(self):
        """Loading a different log rotates to a new conversation.

        Input: POST /set_log with LOG_PATH while the session already holds a
        different log and an old conversation id.
        Expected: rotated=True; the previous log/conversation are reported, a
        new conversation id is issued, and the agent conversation is reset.
        """
        # Arrange
        with self.client.session_transaction() as sess:
            sess["chatbot_log_path"] = OTHER_LOG
            sess["feedback_conversation_id"] = "old-conv"

        # Act
        body = self.client.post("/generic_chatbot/set_log", json={"log_path": LOG_PATH}).get_json()

        # Assert
        self.assertTrue(body["rotated"])
        self.assertEqual(body["previous_log_path"], OTHER_LOG)
        self.assertEqual(body["previous_conversation_id"], "old-conv")
        self.assertNotEqual(body["new_conversation_id"], "old-conv")
        self.agent.reset_conversation.assert_called_once_with()

    def test_reloading_same_log_keeps_conversation_and_history(self):
        """Reloading the same log keeps the existing conversation and history.

        Input: POST /set_log with the same LOG_PATH already in session; the
        agent still holds conversation history.
        Expected: rotated=False; the conversation id is unchanged; the agent is
        not reset, its history is preserved, and no stored context is
        re-fetched.
        """
        # Arrange
        history = [{"role": "user", "content": "q1"}, {"role": "assistant", "content": "a1"}]
        self.agent.conversation_history = list(history)
        with self.client.session_transaction() as sess:
            sess["chatbot_log_path"] = LOG_PATH
            sess["feedback_conversation_id"] = "same-conv"

        # Act
        body = self.client.post("/generic_chatbot/set_log", json={"log_path": LOG_PATH}).get_json()

        # Assert
        self.assertFalse(body["rotated"])
        self.assertEqual(body["new_conversation_id"], "same-conv")
        self.agent.reset_conversation.assert_not_called()
        self.assertEqual(self.agent.conversation_history, history)
        self.history.get_context.assert_not_called()

    def test_reloading_same_log_with_fresh_agent_restores_stored_context(self):
        """A fresh agent reloading the same log restores stored context.

        Input: POST /set_log with the same LOG_PATH in session but a new agent
        with empty history; history_service has stored context.
        Expected: the stored generic context is fetched and imported into the
        agent.
        """
        # Arrange
        self.history.get_context.return_value = [{"role": "user", "content": "stored"}]
        with self.client.session_transaction() as sess:
            sess["chatbot_log_path"] = LOG_PATH
            sess["feedback_conversation_id"] = "same-conv"

        # Act
        self.client.post("/generic_chatbot/set_log", json={"log_path": LOG_PATH})

        # Assert
        self.history.get_context.assert_called_once_with("same-conv", domain="generic")
        self.agent.import_conversation_context.assert_called_once_with([{"role": "user", "content": "stored"}])

    def test_optional_metadata_failures_fall_back_to_defaults(self):
        """Optional metadata failures degrade to safe defaults.

        Input: POST /set_log where span, has-date, time-range and evtx lookups
        all raise.
        Expected: 200 with success; span=0, last time "", has_date=True, evtx "".
        """
        # Arrange
        self.agent.get_log_span_minutes.side_effect = RuntimeError("x")
        self.agent._log_has_date.side_effect = RuntimeError("x")
        self.read_range.side_effect = OSError("x")
        self.find_evt.side_effect = RuntimeError("x")

        # Act
        body = self.client.post("/generic_chatbot/set_log", json={"log_path": LOG_PATH}).get_json()

        # Assert
        self.assertTrue(body["success"])
        self.assertEqual(body["log_span_minutes"], 0)
        self.assertEqual(body["log_last_time"], "")
        self.assertTrue(body["log_has_date"])
        self.assertEqual(body["evtx_path"], "")

    def test_agent_failure_returns_500(self):
        """An agent-creation failure returns 500.

        Input: POST /set_log where _get_or_create_agent raises.
        Expected: 500 with {success: False, error} carrying the failure message.
        """
        # Arrange
        self.get_agent.side_effect = RuntimeError("Log Chatbot Agent is not available.")

        # Act
        resp = self.client.post("/generic_chatbot/set_log", json={"log_path": LOG_PATH})

        # Assert
        self.assertEqual(resp.status_code, 500)
        self.assertEqual(resp.get_json(), {"success": False, "error": "Log Chatbot Agent is not available."})


# ======================================================================
# /chat
# ======================================================================
class TestChat(GenericChatbotTestCase):

    def setUp(self):
        super().setUp()
        self.feedback = self.patch_route("feedback_service")
        self.history = self.patch_route("history_service")
        self.gather = self.patch_route("gather_service")
        # The worker runs inline so the job is terminal before job_sse streams.
        self.patch_route("threading", new=MagicMock(Thread=_SyncThread))
        self.agent = make_agent(current_log_path=LOG_PATH)
        self.agent.chat.return_value = {"answer": "Device reset at boot"}
        self.resume = self.patch_route("_resume_agent_for", return_value=self.agent)
        self.issue_ctx = {"case_nbr": TEST_CASE_NBR}
        self.patch_route("_extract_issue_context", return_value=self.issue_ctx)

    def test_empty_message_returns_400(self):
        """A blank chat message is rejected.

        Input: POST /chat with message="   ".
        Expected: 400 with "message is required"; the agent is not resumed.
        """
        # Act
        resp = self.client.post("/generic_chatbot/chat", json={"message": "   "})

        # Assert
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.get_json(), {"success": False, "error": "message is required"})
        self.resume.assert_not_called()

    def test_ips_gate_blocks_with_428(self):
        """A log without a resolved case number is gated with 428.

        Input: POST /chat where check_ips_service reports a blocking state.
        Expected: 428 echoing the block payload; the agent never chats.
        """
        # Arrange
        with self.client.session_transaction() as sess:
            sess["chatbot_session_id"] = "sid-1"
        self.routes.session_agents["sid-1"] = self.agent
        self.mock_check_ips.blocking_state.return_value = {"needs_case": True}

        # Act
        resp = self.client.post("/generic_chatbot/chat", json={"message": "q"})

        # Assert
        self.assertEqual(resp.status_code, 428)
        self.assertEqual(resp.get_json(), {"needs_case": True})
        self.agent.chat.assert_not_called()

    def test_chat_streams_steps_then_done_and_records_generic_turn(self):
        """Chat streams the worker's steps then a done event and records a turn.

        Input: POST /chat whose agent emits one step before returning.
        Expected: an SSE stream of that step then a done event carrying the
        result; the agent chats with default max_steps/temperature and a step
        callback; gather/feedback/history record the turn tagged
        domain="generic".
        """
        # Arrange
        def fake_chat(message, **kwargs):
            kwargs["step_callback"]({"tool": "grep", "summary": "found reset"})
            return {"answer": "Device reset at boot"}

        self.agent.chat.side_effect = fake_chat

        # Act
        resp = self.client.post("/generic_chatbot/chat",
                                 json={"message": "why reset?", "parent_message_id": "p-1"})

        # Assert
        self.assertEqual(resp.mimetype, "text/event-stream")
        events = sse_events(resp)
        self.assertEqual(events[0], {"type": "step", "step": {"tool": "grep", "summary": "found reset"}})
        self.assertEqual(events[-1]["type"], "done")
        self.assertEqual(events[-1]["result"], {"answer": "Device reset at boot"})
        call = self.agent.chat.call_args
        self.assertEqual(call.args, ("why reset?",))
        self.assertEqual((call.kwargs["max_steps"], call.kwargs["temperature"]), (6, 0.2))
        self.assertIn("step_callback", call.kwargs)
        self.assertEqual(self.gather.record_send.call_args.kwargs["domain"], "generic")
        fb = self.feedback.record_turn.call_args.kwargs
        self.assertEqual((fb["mode"], fb["domain"], fb["parent_message_id"]), ("tools", "generic", "p-1"))
        hist = self.history.record_turn.call_args.kwargs
        self.assertEqual((hist["mode"], hist["domain"], hist["log_path"]), ("tools", "generic", LOG_PATH))
        self.gather.record_usage.assert_called_once()

    def test_numeric_parameters_are_clamped(self):
        """Out-of-range numeric params are clamped to valid bounds.

        Input: POST /chat with temperature=5, max_steps=99,
        issue_time_window_minutes=-3.
        Expected: the agent chats with temperature=1.0 and max_steps=12; the
        issue-time window is clamped to 0.
        """
        # Act
        self.client.post("/generic_chatbot/chat", json={
            "message": "q", "temperature": 5, "max_steps": 99, "issue_time_window_minutes": -3})

        # Assert
        kwargs = self.agent.chat.call_args.kwargs
        self.assertEqual((kwargs["temperature"], kwargs["max_steps"]), (1.0, 12))
        self.assertEqual(self.agent.issue_time_window_minutes, 0)

    def test_no_log_loaded_streams_error(self):
        """Chatting with no log loaded streams an error event.

        Input: POST /chat where the agent has no current_log_path and the
        session has none either.
        Expected: a single SSE error event asking to set a log first; the
        agent never chats.
        """
        # Arrange
        self.agent.current_log_path = ""

        # Act
        events = sse_events(self.client.post("/generic_chatbot/chat", json={"message": "q"}))

        # Assert
        self.assertEqual(events, [{"type": "error", "content": "No log file loaded. Please set a log file first."}])
        self.agent.chat.assert_not_called()

    def test_missing_agent_log_is_recovered_from_session(self):
        """A missing agent log is recovered from the session.

        Input: POST /chat where the agent has no log but the session holds
        OTHER_LOG.
        Expected: the agent's current_log_path is set to OTHER_LOG and the
        stream completes with a done event.
        """
        # Arrange
        self.agent.current_log_path = ""
        with self.client.session_transaction() as sess:
            sess["chatbot_log_path"] = OTHER_LOG

        # Act
        events = sse_events(self.client.post("/generic_chatbot/chat", json={"message": "q"}))

        # Assert
        self.assertEqual(self.agent.current_log_path, OTHER_LOG)
        self.assertEqual(events[-1]["type"], "done")

    def test_worker_failure_streams_error_and_books_failed_status(self):
        """A failing chat worker streams an error and books a failed status.

        Input: POST /chat where the agent chat raises TimeoutError.
        Expected: an SSE error event; gather records a "failed" turn status
        with error code "TimeoutError" and domain="generic".
        """
        # Arrange
        self.agent.chat.side_effect = TimeoutError("LLM timeout")

        # Act
        events = sse_events(self.client.post("/generic_chatbot/chat", json={"message": "q"}))

        # Assert
        self.assertEqual(events[-1], {"type": "error", "content": "LLM timeout"})
        status = self.gather.record_turn_status.call_args.kwargs
        self.assertEqual((status["status"], status["error_code"], status["domain"]),
                          ("failed", "TimeoutError", "generic"))

    def test_agent_resolution_error_streams_error_event(self):
        """An agent-resolution error streams a single SSE error event.

        Input: POST /chat where _resume_agent_for raises before a turn id is
        bound.
        Expected: the outer handler captures the message and streams one SSE
        error event; no turn status is recorded (turn id not yet bound).
        """
        # Arrange
        self.resume.side_effect = RuntimeError("Log Chatbot Agent is not available.")

        # Act
        events = sse_events(self.client.post("/generic_chatbot/chat", json={"message": "q"}))

        # Assert
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["type"], "error")
        self.assertIn("Log Chatbot Agent is not available", events[0]["content"])
        self.gather.record_turn_status.assert_not_called()


# ======================================================================
# /reset
# ======================================================================
class TestReset(GenericChatbotTestCase):

    def setUp(self):
        super().setUp()
        # reset is a factory handler (captures the real _get_or_create_agent).
        self.agent = self.use_base_agent(make_agent())

    def test_reset_clears_conversation(self):
        """Reset clears the agent conversation.

        Input: POST /reset.
        Expected: {success, message}; the agent's conversation is reset once.
        """
        # Act
        resp = self.client.post("/generic_chatbot/reset")

        # Assert
        self.assertEqual(resp.get_json(), {"success": True, "message": "Conversation reset."})
        self.agent.reset_conversation.assert_called_once_with()

    def test_reset_failure_returns_500(self):
        """A reset failure returns 500.

        Input: POST /reset where no agent can be built.
        Expected: 500 with {success: False, error}.
        """
        # Arrange: no base agent and no usable llm client -> agent build raises
        self.app_config.generic_chatbot_agent = None
        self.app_config.llm_helper = MagicMock(client=None)

        # Act
        resp = self.client.post("/generic_chatbot/reset")

        # Assert
        self.assertEqual(resp.status_code, 500)
        self.assertFalse(resp.get_json()["success"])


# ======================================================================
# /history/list, /history/get
# ======================================================================
class TestHistoryEndpoints(GenericChatbotTestCase):

    def setUp(self):
        super().setUp()
        # History endpoints are shared handlers using shared_routes-level services.
        self.history = self.patch_target("services.chatbot.shared_routes.history_service")
        self.jobs = self.patch_target("services.chatbot.shared_routes.chat_jobs")

    def test_history_list_merges_running_jobs_and_orders_groups(self):
        """The history list merges running jobs and orders pinned/new/old groups.

        Input: GET /history/list with two stored conversations (one pinned) and
        two active jobs (one overlapping a stored conv, one brand new).
        Expected: generic-scoped list/summary queries; order is pinned, new
        running, then old; overlapping convs are flagged running and new jobs
        are merged in with their title/turn count.
        """
        # Arrange
        self.history.list_conversations.return_value = [
            {"conversation_id": "c1", "updated_at": "2026-01-01T00:00:00", "pinned": False},
            {"conversation_id": "c2", "updated_at": "2025-01-01T00:00:00", "pinned": True},
        ]
        self.jobs.active_summaries.return_value = [
            {"conversation_id": "c1", "title": "running old"},
            {"conversation_id": "c3", "title": "brand new", "step_count": 2},
        ]

        # Act
        body = self.client.get("/generic_chatbot/history/list").get_json()

        # Assert
        self.history.list_conversations.assert_called_once_with(domain="generic")
        self.jobs.active_summaries.assert_called_once_with(domain="generic")
        convs = body["conversations"]
        self.assertEqual([c["conversation_id"] for c in convs], ["c2", "c3", "c1"])
        self.assertTrue(convs[2]["running"])
        self.assertEqual((convs[1]["title"], convs[1]["turn_count"], convs[1]["running"]), ("brand new", 2, True))

    def test_history_list_failure_returns_500(self):
        """A history-list failure returns 500.

        Input: GET /history/list where list_conversations raises OSError.
        Expected: 500 with {success: False, error}.
        """
        # Arrange
        self.history.list_conversations.side_effect = OSError("disk")

        # Act
        resp = self.client.get("/generic_chatbot/history/list")

        # Assert
        self.assertEqual(resp.status_code, 500)
        self.assertEqual(resp.get_json(), {"success": False, "error": "disk"})

    def test_history_get_requires_conversation_id(self):
        """history/get requires a conversation_id.

        Input: GET /history/get with no conversation_id.
        Expected: 400; the conversation is never fetched.
        """
        # Act
        resp = self.client.get("/generic_chatbot/history/get")

        # Assert
        self.assertEqual(resp.status_code, 400)
        self.history.get_conversation.assert_not_called()

    def test_history_get_not_found_returns_404(self):
        """history/get returns 404 for an unknown conversation.

        Input: GET /history/get where get_conversation returns None.
        Expected: 404 with {success: False, error: "Conversation not found"}.
        """
        # Arrange
        self.history.get_conversation.return_value = None

        # Act
        resp = self.client.get(f"/generic_chatbot/history/get?conversation_id={CONV_ID}")

        # Assert
        self.assertEqual(resp.status_code, 404)
        self.assertEqual(resp.get_json(), {"success": False, "error": "Conversation not found"})

    def test_history_get_returns_generic_conversation_with_steps(self):
        """history/get returns the generic conversation including steps.

        Input: GET /history/get with a known conversation_id.
        Expected: {success, conversation}; get_conversation is called
        generic-scoped with with_steps=True.
        """
        # Arrange
        self.history.get_conversation.return_value = {"conversation_id": CONV_ID, "turns": []}

        # Act
        body = self.client.get(f"/generic_chatbot/history/get?conversation_id={CONV_ID}").get_json()

        # Assert
        self.assertEqual(body, {"success": True, "conversation": {"conversation_id": CONV_ID, "turns": []}})
        self.history.get_conversation.assert_called_once_with(CONV_ID, domain="generic", with_steps=True)


if __name__ == "__main__":
    unittest.main()
