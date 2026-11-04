"""Unit tests for the nw_analysis blueprint
(blueprints/nw_analysis/nw_analysis_routes.py).

The blueprint drives a per-session ``NwAnalysisAgentSystem`` (NW/sleep-study
flavour) through JSON + Server-Sent-Event endpoints, plus skill-YAML
management and a sleep-study analyzer stream. Every external dependency is
patched at its point of use:

* ``NwAnalysisAgentSystem``  -> a MagicMock agent (no LLM client is built)
* ``gather_service``      -> MagicMock (no analytics files written)
* ``analyze_sleepstudy_stream`` -> scripted event generator
* ``tk`` / ``filedialog`` -> MagicMock (no native dialog ever opens)
* ``threading``           -> synchronous fake Thread (deterministic SSE order)
* skills loaders / YAML helpers / ``utils.helpers.get_load_path`` -> mocks
* ``os.path.exists`` / ``open`` -> in-memory fakes (no disk reads/writes)

The module-level ``_chatbot_instances`` store is cleared per test via
``patch.dict`` so agents never leak between tests.

Note: this blueprint does not consult ``app_config.silent_mode`` (it never
launches explorer / TextAnalysisTool), so the branch-specific flag has no
effect here.
"""
import io
import json
import os
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import ANY, MagicMock, patch

import blueprints.nw_analysis.nw_analysis_routes as nw_routes
from blueprints.nw_analysis.nw_analysis_routes import nw_analysis_bp
from configs.chatbot_ui import NW_UI
from tests.base_test import BaseTestCase, TESTS_DIR

ROUTES = "blueprints.nw_analysis.nw_analysis_routes"

LOG_PATH = r"C:\fake\logs\wifi_trace.etl.log"
ETL_PATH = r"C:\fake\logs\wifi_trace.etl"
SLEEP_PATH = r"C:\fake\sleepstudy-report.html"
SKILL_DESCRIPTIONS = [{"name": "roaming", "description": "Roaming analysis"}]

# Issue context derived from BaseTestCase.create_mock_case_context()
EXPECTED_CTX = {
    "case_nbr": "00123456",
    "subject": "Wi-Fi disconnects after resume",
    "description": "Wi-Fi drops every time the laptop resumes from Modern Standby.",
    "issue_type": "",
    "attachment_time": "",
}


class _SyncThread:
    """Drop-in for threading.Thread that runs the target inside start()."""

    def __init__(self, target=None, args=(), kwargs=None, daemon=None):
        self._target = target
        self._args = args
        self._kwargs = kwargs or {}

    def start(self):
        self._target(*self._args, **self._kwargs)

    def join(self, timeout=None):
        return None


def sse_events(resp):
    """Decode an SSE body into the list of JSON payloads."""
    body = resp.get_data(as_text=True)
    return [json.loads(chunk[len("data: "):])
            for chunk in body.split("\n\n") if chunk.startswith("data: ")]


class NwAnalysisTestCase(BaseTestCase):
    blueprint = nw_analysis_bp

    def setUp(self):
        super().setUp()
        instances_patcher = patch.dict(nw_routes._chatbot_instances, clear=True)
        instances_patcher.start()
        self.addCleanup(instances_patcher.stop)

        self.agent = MagicMock(name="nw_agent")
        self.agent.current_log_path = ""
        self.agent.model = "gpt-test"
        self.agent.last_turn_usage = {"total_tokens": 42}
        self.agent.get_skill_descriptions.return_value = SKILL_DESCRIPTIONS
        # No kept per-session skill file by default, so the skill-editor paths
        # seed from the latest user/cloud YAML rather than the agent session path.
        self.agent._session_skill_path = None
        agent_cls_patcher = patch(f"{ROUTES}.NwAnalysisAgentSystem", return_value=self.agent)
        self.mock_agent_cls = agent_cls_patcher.start()
        self.addCleanup(agent_cls_patcher.stop)

        self.base_agent = MagicMock(name="nw_base_agent", client="base-client",
                                    model="gpt-base", skills={"base": 1})
        self.app_config.nw_analysis_agent = self.base_agent

        gather_patcher = patch(f"{ROUTES}.gather_service")
        self.mock_gather = gather_patcher.start()
        self.addCleanup(gather_patcher.stop)

        # IPS gating is a shared-framework dependency added in the refactor:
        # blocking_state() gates chat/analyze SSE (428), prompt_state() is
        # merged into set_log responses, start_new_session() runs on reset.
        # Neutralise it so these routes behave as they did pre-gating.
        ips_patcher = patch(f"{ROUTES}.check_ips_service")
        self.mock_check_ips = ips_patcher.start()
        self.addCleanup(ips_patcher.stop)
        self.mock_check_ips.blocking_state.return_value = None
        self.mock_check_ips.prompt_state.return_value = {}
        self.mock_check_ips.start_new_session.return_value = None

        evt_patcher = patch(f"{ROUTES}.find_event_log_for_log", return_value="")
        self.mock_find_evt = evt_patcher.start()
        self.addCleanup(evt_patcher.stop)

    def make_agent_unavailable(self):
        """No base NW agent and no LLM helper -> _get_or_create_agent raises."""
        self.app_config.nw_analysis_agent = None
        self.app_config.llm_helper = None


# ======================================================================
# Structure
# ======================================================================
class TestNwAnalysisBlueprintStructure(NwAnalysisTestCase):

    def test_blueprint_registered_with_url_prefix(self):
        """Verify blueprint registration and all routes with methods.

        Input: the Flask app with ``nw_analysis_bp`` registered (via setUp).
        Expected: the blueprint is mounted under ``/nw_analysis`` and exposes
        every expected route with its listed HTTP methods.
        """
        # Arrange
        expected = {
            "/nw_analysis/": {"GET"},
            "/nw_analysis/browse": {"GET"},
            "/nw_analysis/set_log": {"POST"},
            "/nw_analysis/set_log_sleepstudy": {"POST"},
            "/nw_analysis/analyze_sleepstudy": {"POST"},
            "/nw_analysis/chat": {"POST"},
            "/nw_analysis/reset": {"POST"},
            "/nw_analysis/chat/stop": {"POST"},
            "/nw_analysis/prepare": {"POST"},
            "/nw_analysis/back_to_avatar": {"POST"},
            "/nw_analysis/browse_yaml": {"GET"},
            "/nw_analysis/load_skills_yaml": {"POST"},
            "/nw_analysis/append_skills_yaml": {"POST"},
            "/nw_analysis/reload_from_shared": {"POST"},
            "/nw_analysis/skills": {"GET"},
            "/nw_analysis/get_issue_context": {"GET"},
        }

        # Act
        rules = {r.rule: r.methods for r in self.app.url_map.iter_rules()
                 if r.endpoint.startswith("nw_analysis.")}

        # Assert
        self.assertIs(self.app.blueprints.get("nw_analysis"), nw_analysis_bp)
        self.assertEqual(nw_analysis_bp.url_prefix, "/nw_analysis")
        self.assertEqual(set(rules), set(expected))
        for rule, methods in expected.items():
            self.assertTrue(methods.issubset(rules[rule]), rule)


# ======================================================================
# Pages / native dialogs
# ======================================================================
class TestIndexAndBrowse(NwAnalysisTestCase):

    @patch(f"{ROUTES}.render_template", return_value="rendered")
    def test_index_renders_with_suggested_log_and_issue_description(self, mock_render):
        """The index page renders with the suggested log and issue description.

        Input: GET /nw_analysis/ with a last analyzed log and a seeded case
        session.
        Expected: 200; NW_analysis.html is rendered with the suggested log and
        the case's issue description.
        """
        # Arrange
        self.app_config.last_analyzed_log_path = LOG_PATH
        self.seed_session()

        # Act
        resp = self.client.get("/nw_analysis/")

        # Assert
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_data(as_text=True), "rendered")
        mock_render.assert_called_once_with(
            "chatbot/page.html",
            ui=NW_UI,
            suggested_log=LOG_PATH,
            issue_description=EXPECTED_CTX["description"],
        )

    @patch(f"{ROUTES}.render_template", return_value="rendered")
    def test_index_without_session_uses_empty_defaults(self, mock_render):
        """The index page renders with empty defaults when nothing is set.

        Input: GET /nw_analysis/ with no case context and no last analyzed log.
        Expected: 200; NW_analysis.html is rendered with empty suggested_log and
        issue_description.
        """
        # Arrange: no case context, no last analyzed log

        # Act
        resp = self.client.get("/nw_analysis/")

        # Assert
        self.assertEqual(resp.status_code, 200)
        mock_render.assert_called_once_with("chatbot/page.html", ui=NW_UI, suggested_log="", issue_description="")

    @patch(f"{ROUTES}.threading", new=MagicMock(Thread=_SyncThread))
    @patch(f"{ROUTES}.filedialog")
    @patch(f"{ROUTES}.tk")
    def test_browse_returns_selected_log_path(self, mock_tk, mock_dialog):
        """The browse dialog returns the picked log path.

        Input: GET /nw_analysis/browse where the file dialog returns a log path.
        Expected: {success, path}; the dialog is titled "Select log file" and the
        hidden Tk root is destroyed.
        """
        # Arrange
        mock_dialog.askopenfilename.return_value = LOG_PATH

        # Act
        resp = self.client.get("/nw_analysis/browse")

        # Assert
        self.assertEqual(resp.get_json(), {"success": True, "path": LOG_PATH})
        mock_tk.Tk.assert_called_once_with()
        self.assertEqual(mock_dialog.askopenfilename.call_args.kwargs["title"], "Select log file")
        mock_tk.Tk.return_value.destroy.assert_called_once_with()

    @patch(f"{ROUTES}.threading", new=MagicMock(Thread=_SyncThread))
    @patch(f"{ROUTES}.filedialog")
    @patch(f"{ROUTES}.tk")
    def test_browse_cancelled_dialog_returns_empty_path(self, mock_tk, mock_dialog):
        """A cancelled browse dialog returns an empty path.

        Input: GET /nw_analysis/browse where the dialog returns "".
        Expected: {success: True, path: ""}.
        """
        # Arrange
        mock_dialog.askopenfilename.return_value = ""

        # Act
        resp = self.client.get("/nw_analysis/browse")

        # Assert
        self.assertEqual(resp.get_json(), {"success": True, "path": ""})

    @patch("services.chatbot.factory.choose_skills_yaml", return_value=r"C:\fake\skills.yaml")
    def test_browse_yaml_returns_selected_yaml(self, mock_choose):
        """The shared browse_yaml handler returns the chosen YAML path.

        Input: GET /nw_analysis/browse_yaml. In the refactor this is the shared
        factory handler, which delegates to ``choose_skills_yaml()`` rather than
        opening a native dialog in the blueprint.
        Expected: {success, path} with the chosen YAML.
        """
        # Act
        resp = self.client.get("/nw_analysis/browse_yaml")

        # Assert
        self.assertEqual(resp.get_json(), {"success": True, "path": r"C:\fake\skills.yaml"})
        mock_choose.assert_called_once_with()


# ======================================================================
# set_log / set_log_sleepstudy
# ======================================================================
class TestSetLog(NwAnalysisTestCase):

    def test_set_log_requires_log_path(self):
        """set_log requires a non-blank log_path.

        Input: POST /set_log with a whitespace log_path.
        Expected: 400 with "log_path is required"; no agent is built.
        """
        # Act
        resp = self.client.post("/nw_analysis/set_log", json={"log_path": "   "})

        # Assert
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.get_json(), {"success": False, "error": "log_path is required"})
        self.mock_agent_cls.assert_not_called()

    def test_set_log_success_sets_path_resets_and_primes(self):
        """set_log sets the path, resets and primes the agent.

        Input: POST /set_log with a padded LOG_PATH and a seeded case session.
        Expected: 200 with success/skills; the agent gets the trimmed log path,
        resets and is primed with the issue context; the session stores the log
        path.
        """
        # Arrange
        self.seed_session()

        # Act
        resp = self.client.post("/nw_analysis/set_log", json={"log_path": f"  {LOG_PATH}  "})

        # Assert
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json(), {
            "success": True,
            "message": f"Log file set: {LOG_PATH}",
            "skills": SKILL_DESCRIPTIONS,
            "evtx_path": "",
        })
        self.assertEqual(self.agent.current_log_path, LOG_PATH)
        self.agent.reset_conversation.assert_called_once_with()
        self.agent.prime_with_context.assert_called_with(**EXPECTED_CTX)
        self.assertEqual(self.get_session()["chatbot_log_path"], LOG_PATH)

    def test_set_log_agent_error_returns_500(self):
        """A set_log agent error returns 500 and leaves the session untouched.

        Input: POST /set_log where reset_conversation raises.
        Expected: 500 with {success: False, error}; no chatbot_log_path in
        session.
        """
        # Arrange
        self.agent.reset_conversation.side_effect = RuntimeError("agent broke")

        # Act
        resp = self.client.post("/nw_analysis/set_log", json={"log_path": LOG_PATH})

        # Assert
        self.assertEqual(resp.status_code, 500)
        self.assertEqual(resp.get_json(), {"success": False, "error": "agent broke"})
        self.assertNotIn("chatbot_log_path", self.get_session())

    def test_set_log_sleepstudy_requires_log_path(self):
        """set_log_sleepstudy requires a log_path.

        Input: POST /set_log_sleepstudy with an empty body.
        Expected: 400 with "log_path is required".
        """
        # Act
        resp = self.client.post("/nw_analysis/set_log_sleepstudy", json={})

        # Assert
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.get_json()["error"], "log_path is required")

    def test_set_log_sleepstudy_success_has_no_session_write_or_skills(self):
        """set_log_sleepstudy sets the agent log but writes no session or skills.

        Input: POST /set_log_sleepstudy with a sleepstudy path and seeded case.
        Expected: 200 with just a success message; the agent gets the path,
        resets and is primed, but no chatbot_log_path is written to session.
        """
        # Arrange
        self.seed_session()

        # Act
        resp = self.client.post("/nw_analysis/set_log_sleepstudy", json={"log_path": SLEEP_PATH})

        # Assert
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json(), {"success": True, "message": f"Sleepstudy file set: {SLEEP_PATH}"})
        self.assertEqual(self.agent.current_log_path, SLEEP_PATH)
        self.agent.reset_conversation.assert_called_once_with()
        self.agent.prime_with_context.assert_called_with(**EXPECTED_CTX)
        self.assertNotIn("chatbot_log_path", self.get_session())

    def test_set_log_sleepstudy_agent_unavailable_returns_500(self):
        """set_log_sleepstudy returns 500 when the agent is unavailable.

        Input: POST /set_log_sleepstudy when no base agent or LLM helper exists.
        Expected: 500 with a "Log Chatbot Agent is not available" error.
        """
        # Arrange
        self.make_agent_unavailable()

        # Act
        resp = self.client.post("/nw_analysis/set_log_sleepstudy", json={"log_path": SLEEP_PATH})

        # Assert
        self.assertEqual(resp.status_code, 500)
        self.assertIn("Log Chatbot Agent is not available", resp.get_json()["error"])


# ======================================================================
# analyze_sleepstudy (SSE)
# ======================================================================
class TestAnalyzeSleepstudy(NwAnalysisTestCase):

    @patch(f"{ROUTES}.analyze_sleepstudy_stream")
    def test_missing_log_path_streams_error(self, mock_stream):
        """analyze_sleepstudy streams an error when no log_path is given.

        Input: POST /analyze_sleepstudy with an empty body.
        Expected: an SSE error event "log_path is required"; the analyzer is not
        called.
        """
        # Act
        resp = self.client.post("/nw_analysis/analyze_sleepstudy", json={})

        # Assert
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.mimetype, "text/event-stream")
        self.assertEqual(sse_events(resp), [{"type": "error", "content": "log_path is required"}])
        mock_stream.assert_not_called()

    @patch(f"{ROUTES}.analyze_sleepstudy_stream")
    @patch(f"{ROUTES}.os.path.exists", return_value=False)
    def test_missing_file_streams_not_found(self, mock_exists, mock_stream):
        """analyze_sleepstudy streams a not-found error for a missing file.

        Input: POST /analyze_sleepstudy where the file does not exist.
        Expected: an SSE error "File not found: ..."; the analyzer is not called.
        """
        # Act
        resp = self.client.post("/nw_analysis/analyze_sleepstudy", json={"log_path": SLEEP_PATH})

        # Assert
        self.assertEqual(sse_events(resp), [{"type": "error", "content": f"File not found: {SLEEP_PATH}"}])
        mock_exists.assert_called_once_with(SLEEP_PATH)
        mock_stream.assert_not_called()

    @patch(f"{ROUTES}.analyze_sleepstudy_stream")
    @patch(f"{ROUTES}.os.path.exists", return_value=True)
    def test_stream_translates_events_and_calls_llm_adapter(self, mock_exists, mock_stream):
        """analyze_sleepstudy translates analyzer events and calls the LLM adapter.

        Input: POST /analyze_sleepstudy for an existing file where the analyzer
        yields step/unknown/done events and invokes the llm_call adapter.
        Expected: step and done events are re-emitted (unknown dropped); the LLM
        adapter returns the completion text; the create call uses the sleep
        model/params. (Known bug: the usage is never booked to Gather.)
        """
        # Arrange
        llm_client = MagicMock(name="llm_client")
        llm_client.chat.completions.create.return_value = SimpleNamespace(
            usage=SimpleNamespace(prompt_tokens=10, completion_tokens=5,
                                  cache_read_input_tokens=2, cache_creation_input_tokens=1),
            choices=[SimpleNamespace(message=SimpleNamespace(content="LLM answer"))],
        )
        self.app_config.llm_helper = MagicMock(client=llm_client, model="gpt-sleep")
        llm_replies = []

        def fake_stream(path, llm_call=None):
            llm_replies.append(llm_call("system prompt", "user message"))
            yield {"type": "step", "content": "Parsing report"}
            yield {"type": "unknown", "content": "ignored"}
            yield {"type": "done", "data": "## Report"}

        mock_stream.side_effect = fake_stream
        with self.client.session_transaction() as sess:
            sess["gather_workflow_id"] = "wf-1"

        # Act
        resp = self.client.post("/nw_analysis/analyze_sleepstudy", json={"log_path": SLEEP_PATH})
        events = sse_events(resp)

        # Assert
        self.assertEqual(resp.headers["Cache-Control"], "no-cache")
        self.assertEqual(events, [
            {"type": "step", "step": {"content": "Parsing report"}},
            {"type": "done", "result": {"type": "text", "data": "## Report"}},
        ])
        self.assertEqual(llm_replies, ["LLM answer"])
        create_kwargs = llm_client.chat.completions.create.call_args.kwargs
        self.assertEqual(create_kwargs["model"], "gpt-sleep")
        self.assertEqual(create_kwargs["temperature"], 0.2)
        self.assertEqual(create_kwargs["max_tokens"], 1500)
        # KNOWN BUG (documented, not endorsed): event_stream is a generator
        # wrapped in @copy_current_request_context, which only covers the
        # call that CREATES the generator. By the time Werkzeug iterates it
        # the request context is gone, session.get() raises RuntimeError in
        # the finally block and the bare `except: pass` swallows it - so the
        # sleep-study spend is never booked to Gather.
        self.mock_gather.record_feature_usage.assert_not_called()

    @patch(f"{ROUTES}.analyze_sleepstudy_stream")
    @patch(f"{ROUTES}.os.path.exists", return_value=True)
    def test_analyzer_error_event_streams_error_without_llm(self, mock_exists, mock_stream):
        """analyze_sleepstudy streams an analyzer error event without an LLM.

        Input: POST /analyze_sleepstudy with no LLM helper where the analyzer
        yields an error event.
        Expected: the error event is streamed and the analyzer's llm_call is None.
        """
        # Arrange: no LLM helper -> llm_call is None
        self.app_config.llm_helper = None
        mock_stream.return_value = iter([{"type": "error", "content": "bad report"}])

        # Act
        events = sse_events(self.client.post("/nw_analysis/analyze_sleepstudy", json={"log_path": SLEEP_PATH}))

        # Assert
        self.assertEqual(events, [{"type": "error", "content": "bad report"}])
        self.assertIsNone(mock_stream.call_args.kwargs["llm_call"])
        # Same known bug as above: the failed status is never booked.
        self.mock_gather.record_feature_usage.assert_not_called()

    @patch(f"{ROUTES}.analyze_sleepstudy_stream", side_effect=ValueError("parse exploded"))
    @patch(f"{ROUTES}.os.path.exists", return_value=True)
    def test_analyzer_exception_streams_error(self, mock_exists, mock_stream):
        """analyze_sleepstudy streams an error when the analyzer raises.

        Input: POST /analyze_sleepstudy where the analyzer raises ValueError.
        Expected: an SSE error event with the exception message.
        """
        # Act
        events = sse_events(self.client.post("/nw_analysis/analyze_sleepstudy", json={"log_path": SLEEP_PATH}))

        # Assert
        self.assertEqual(events, [{"type": "error", "content": "parse exploded"}])
        self.mock_gather.record_feature_usage.assert_not_called()  # known bug, see above


# ======================================================================
# chat (SSE)
# ======================================================================
class TestChat(NwAnalysisTestCase):

    def test_chat_requires_message(self):
        """chat requires a non-blank message.

        Input: POST /chat with a whitespace message.
        Expected: 400 with "message is required"; no send is recorded.
        """
        # Act
        resp = self.client.post("/nw_analysis/chat", json={"message": "  "})

        # Assert
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.get_json(), {"success": False, "error": "message is required"})
        self.mock_gather.record_send.assert_not_called()

    def test_chat_without_loaded_log_streams_error(self):
        """Chatting with no loaded log streams an error.

        Input: POST /chat while the agent has no current_log_path.
        Expected: an SSE error asking to set a log first; the agent never chats
        and no send is recorded.
        """
        # Arrange: agent.current_log_path == "" (setUp)

        # Act
        resp = self.client.post("/nw_analysis/chat", json={"message": "why disconnect?"})

        # Assert
        self.assertEqual(sse_events(resp), [
            {"type": "error", "content": "No log file loaded. Please set a log file first."}])
        self.agent.chat.assert_not_called()
        self.mock_gather.record_send.assert_not_called()

    @patch(f"{ROUTES}.threading", new=MagicMock(Thread=_SyncThread))
    def test_chat_simple_mode_returns_done_and_records_usage(self):
        """Simple chat returns a done event and records usage.

        Input: POST /chat with a message and string params and a seeded workflow.
        Expected: a single SSE done event; the agent chats with the parsed
        params; gather records a send (domain "nw") and a completed usage with
        the agent's token totals and model.
        """
        # Arrange
        self.agent.current_log_path = LOG_PATH
        self.agent.chat.return_value = {"type": "text", "data": "It roamed."}
        self.seed_session(gather_workflow_id="wf-9")

        # Act
        resp = self.client.post("/nw_analysis/chat", json={"message": " why? ", "temperature": "0.5"})

        # Assert
        self.assertEqual(sse_events(resp), [{"type": "done", "result": {"type": "text", "data": "It roamed."}}])
        self.agent.chat.assert_called_once_with("why?", max_steps=6, temperature=0.5, step_callback=ANY)
        sess = self.get_session()
        send = self.mock_gather.record_send.call_args.kwargs
        self.assertEqual(send["conversation_id"], sess["nw_conversation_id"])
        self.assertEqual(send["turn_id"], sess["nw_active_turn_id"])
        self.assertEqual(send["workflow_id"], "wf-9")
        self.assertEqual(send["user_message"], "why?")
        self.assertEqual(send["log_path"], LOG_PATH)
        self.assertEqual(send["issue"], EXPECTED_CTX)
        self.assertEqual(send["domain"], "nw")
        usage = self.mock_gather.record_usage.call_args.kwargs
        self.assertEqual(usage["status"], "completed")
        self.assertEqual(usage["usage"], {"total_tokens": 42})
        self.assertEqual(usage["model"], "gpt-test")

    @patch(f"{ROUTES}.threading", new=MagicMock(Thread=_SyncThread))
    def test_chat_clamps_out_of_range_parameters(self):
        """chat clamps out-of-range and non-numeric parameters.

        Input: two POST /chat calls with out-of-range then non-numeric
        temperature/max_steps.
        Expected: the first clamps to (temperature 1.0, max_steps 12) and the
        second falls back to the defaults (temperature 0.2, max_steps 6).
        """
        # Arrange
        self.agent.current_log_path = LOG_PATH
        self.agent.chat.return_value = {"type": "text", "data": "ok"}

        # Act
        self.client.post("/nw_analysis/chat", json={"message": "hi", "temperature": 5, "max_steps": 99})
        self.client.post("/nw_analysis/chat", json={"message": "hi", "temperature": "abc", "max_steps": "xyz"})

        # Assert
        first, second = self.agent.chat.call_args_list
        self.assertEqual((first.kwargs["temperature"], first.kwargs["max_steps"]), (1.0, 12))
        self.assertEqual((second.kwargs["temperature"], second.kwargs["max_steps"]), (0.2, 6))

    @patch(f"{ROUTES}.threading", new=MagicMock(Thread=_SyncThread))
    def test_chat_tools_mode_streams_steps_then_done(self):
        """Tools-mode chat streams steps then a done event.

        Input: POST /chat with clamped step/param values.
        Expected: the step callback's step is streamed, then the done result; the
        agent chats with max_steps clamped to 12 and temperature clamped to 0.0;
        a completed usage is recorded.
        """
        # Arrange
        self.agent.current_log_path = LOG_PATH

        def fake_chat(message, **kwargs):
            kwargs["step_callback"]({"tool": "grep", "summary": "found deauth"})
            return {"type": "text", "data": "Deauth reason 3"}

        self.agent.chat.side_effect = fake_chat

        # Act
        resp = self.client.post("/nw_analysis/chat", json={"message": "analyze",
                                                           "max_steps": 99, "temperature": -1})

        # Assert
        self.assertEqual(resp.headers["X-Accel-Buffering"], "no")
        self.assertEqual(sse_events(resp), [
            {"type": "step", "step": {"tool": "grep", "summary": "found deauth"}},
            {"type": "done", "result": {"type": "text", "data": "Deauth reason 3"}},
        ])
        kwargs = self.agent.chat.call_args.kwargs
        self.assertEqual((kwargs["max_steps"], kwargs["temperature"]), (12, 0.0))
        self.assertEqual(self.mock_gather.record_usage.call_args.kwargs["status"], "completed")

    @patch(f"{ROUTES}.threading", new=MagicMock(Thread=_SyncThread))
    def test_chat_tools_mode_agent_error_streams_error_and_books_failed_cost(self):
        """A tools-mode agent error streams an error and books a failed cost.

        Input: POST /chat with use_tools=True where the agent chat raises
        TimeoutError.
        Expected: an SSE error event; gather records a "failed" usage with error
        code "TimeoutError".
        """
        # Arrange
        self.agent.current_log_path = LOG_PATH
        self.agent.chat.side_effect = TimeoutError("LLM timeout")

        # Act
        resp = self.client.post("/nw_analysis/chat", json={"message": "analyze", "use_tools": True})

        # Assert
        self.assertEqual(sse_events(resp), [{"type": "error", "content": "LLM timeout"}])
        usage = self.mock_gather.record_usage.call_args.kwargs
        self.assertEqual(usage["status"], "failed")
        self.assertEqual(usage["error_code"], "TimeoutError")

    @patch(f"{ROUTES}.threading", new=MagicMock(Thread=_SyncThread))
    def test_chat_simple_mode_exception_streams_error_and_books_failed_cost(self):
        """A chat-worker exception streams an error and books a failed cost.

        Input: POST /chat where the agent chat raises RuntimeError.
        Expected: the worker catches it and streams an SSE error event with the
        message; gather books a "failed" usage (error code "RuntimeError").
        """
        # Arrange
        self.agent.current_log_path = LOG_PATH
        self.agent.chat.side_effect = RuntimeError("model down")

        # Act
        resp = self.client.post("/nw_analysis/chat", json={"message": "hi"})

        # Assert
        self.assertEqual(sse_events(resp), [{"type": "error", "content": "model down"}])
        usage = self.mock_gather.record_usage.call_args.kwargs
        self.assertEqual(usage["status"], "failed")
        self.assertEqual(usage["error_code"], "RuntimeError")

    def test_chat_agent_unavailable_streams_error_and_skips_turn_status(self):
        """An unavailable agent streams an error and records no turn status.

        Input: POST /chat when no agent can be created (raises before a
        conversation id is bound).
        Expected: the outer handler captures the message and streams a single
        SSE error event; no turn status is recorded and the agent never chats.
        """
        # Arrange: _get_or_create_agent raises BEFORE conversation_id is bound;
        # the except block's `if conversation_id and turn_id` then raises
        # UnboundLocalError, which its own try/except swallows.
        self.make_agent_unavailable()

        # Act
        resp = self.client.post("/nw_analysis/chat", json={"message": "hi"})

        # Assert
        events = sse_events(resp)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["type"], "error")
        self.assertIn("Log Chatbot Agent is not available", events[0]["content"])
        self.mock_gather.record_turn_status.assert_not_called()
        self.agent.chat.assert_not_called()


# ======================================================================
# reset / chat_stop
# ======================================================================
class TestResetAndStop(NwAnalysisTestCase):

    def test_reset_resets_agent_and_rotates_conversation_id(self):
        """reset resets the agent and rotates the conversation id.

        Input: POST /reset with an existing conversation id in session.
        Expected: 200; the agent conversation is reset and a new, non-empty
        conversation id replaces the old one.
        """
        # Arrange
        self.seed_session(nw_conversation_id="old-conv-id")

        # Act
        resp = self.client.post("/nw_analysis/reset")

        # Assert
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json(), {"success": True, "message": "Conversation reset."})
        self.agent.reset_conversation.assert_called_once_with()
        new_id = self.get_session()["nw_conversation_id"]
        self.assertNotEqual(new_id, "old-conv-id")
        self.assertTrue(new_id)

    def test_reset_agent_unavailable_returns_500(self):
        """reset returns 500 when the agent is unavailable.

        Input: POST /reset when no agent can be created.
        Expected: 500 with success False.
        """
        # Arrange
        self.make_agent_unavailable()

        # Act
        resp = self.client.post("/nw_analysis/reset")

        # Assert
        self.assertEqual(resp.status_code, 500)
        self.assertFalse(resp.get_json()["success"])

    def test_chat_stop_without_running_agent_is_noop(self):
        """chat/stop with no running agent is a no-op.

        Input: POST /chat/stop with no session-bound agent.
        Expected: {success, stopped: False}; no turn status is recorded.
        """
        # Act
        resp = self.client.post("/nw_analysis/chat/stop")

        # Assert
        self.assertEqual(resp.get_json(), {"success": True, "stopped": False})
        self.mock_gather.record_turn_status.assert_not_called()

    def test_chat_stop_sets_cancel_event_and_records_cancelled(self):
        """chat/stop sets the cancel event and records a cancelled status.

        Input: POST /chat/stop with a session-bound agent and active
        conversation/turn ids.
        Expected: {success, stopped: True}; the agent cancel event is set and
        gather records a "cancelled" turn status (domain "nw").
        """
        # Arrange
        nw_routes._chatbot_instances["sid-1"] = self.agent
        self.seed_session(chatbot_session_id="sid-1", nw_active_conversation_id="conv-1",
                          nw_active_turn_id="turn-1", gather_workflow_id="wf-1")

        # Act
        resp = self.client.post("/nw_analysis/chat/stop")

        # Assert
        self.assertEqual(resp.get_json(), {"success": True, "stopped": True})
        self.agent.cancel_event.set.assert_called_once_with()
        self.mock_gather.record_turn_status.assert_called_once_with(
            conversation_id="conv-1", turn_id="turn-1", status="cancelled",
            workflow_id="wf-1", issue=EXPECTED_CTX, domain="nw",
        )

    def test_chat_stop_cancel_failure_returns_500(self):
        """A chat/stop cancel failure returns 500.

        Input: POST /chat/stop where setting the cancel event raises.
        Expected: 500 with {success: False, error}.
        """
        # Arrange
        self.agent.cancel_event.set.side_effect = RuntimeError("cannot cancel")
        nw_routes._chatbot_instances["sid-1"] = self.agent
        self.seed_session(chatbot_session_id="sid-1")

        # Act
        resp = self.client.post("/nw_analysis/chat/stop")

        # Assert
        self.assertEqual(resp.status_code, 500)
        self.assertEqual(resp.get_json(), {"success": False, "error": "cannot cancel"})


# ======================================================================
# prepare
# ======================================================================
class TestPrepare(NwAnalysisTestCase):

    def test_prepare_requires_etl_path(self):
        """prepare requires an etl_path.

        Input: POST /prepare with an empty body.
        Expected: 400 with "etl_path is required".
        """
        # Act
        resp = self.client.post("/nw_analysis/prepare", json={})

        # Assert
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.get_json(), {"success": False, "error": "etl_path is required"})

    @patch(f"{ROUTES}.os.path.exists", return_value=False)
    def test_prepare_missing_log_returns_404(self, mock_exists):
        """prepare returns 404 when the decoded log is missing.

        Input: POST /prepare with an ETL path whose sibling .log does not exist.
        Expected: 404 naming the expected "<etl>.log"; no agent is built.
        """
        # Act
        resp = self.client.post("/nw_analysis/prepare", json={"etl_path": ETL_PATH})

        # Assert
        self.assertEqual(resp.status_code, 404)
        self.assertEqual(resp.get_json()["error"], f".log file not found: {ETL_PATH}.log")
        mock_exists.assert_called_once_with(ETL_PATH + ".log")
        self.mock_agent_cls.assert_not_called()

    @patch(f"{ROUTES}.os.path.exists", return_value=True)
    def test_prepare_success_sets_shared_log_path_and_primes_agent(self, mock_exists):
        """prepare sets the shared log path and primes the agent.

        Input: POST /prepare with an ETL path whose "<etl>.log" exists.
        Expected: 200; the decoded log becomes the last analyzed log and the
        agent's current log, and the agent is primed with the issue context.
        """
        # Arrange
        self.seed_session()

        # Act
        resp = self.client.post("/nw_analysis/prepare", json={"etl_path": ETL_PATH})

        # Assert
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json(), {"success": True})
        self.assertEqual(self.app_config.last_analyzed_log_path, ETL_PATH + ".log")
        self.assertEqual(self.agent.current_log_path, ETL_PATH + ".log")
        self.agent.prime_with_context.assert_called_with(**EXPECTED_CTX)

    @patch(f"{ROUTES}.os.path.exists", return_value=True)
    def test_prepare_agent_error_returns_500(self, mock_exists):
        """A prepare agent error returns 500.

        Input: POST /prepare with an existing log where no agent can be created.
        Expected: 500 with a "Log Chatbot Agent is not available" error.
        """
        # Arrange
        self.make_agent_unavailable()

        # Act
        resp = self.client.post("/nw_analysis/prepare", json={"etl_path": ETL_PATH})

        # Assert
        self.assertEqual(resp.status_code, 500)
        self.assertIn("Log Chatbot Agent is not available", resp.get_json()["error"])


# ======================================================================
# Skills: load_skills_yaml / reload_from_shared / skills
# ======================================================================
class TestSkillsRoutes(NwAnalysisTestCase):

    def test_load_skills_yaml_requires_path(self):
        """load_skills_yaml requires a yaml_path.

        Input: POST /load_skills_yaml with an empty yaml_path.
        Expected: 400 with "yaml_path is required.".
        """
        # Act
        resp = self.client.post("/nw_analysis/load_skills_yaml", json={"yaml_path": ""})

        # Assert
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.get_json(), {"success": False, "error": "yaml_path is required."})

    @patch(f"{ROUTES}.load_skills_from_yaml", return_value={"s1": 1, "s2": 2, "s3": 3})
    def test_load_skills_yaml_success_updates_all_agents(self, mock_load):
        """load_skills_yaml loads skills from a YAML into all agents.

        Input: POST /load_skills_yaml with a YAML path.
        Expected: skills are loaded and shared to the session and base agents;
        the response reports the count, source and agent skills.
        """
        # Act
        resp = self.client.post("/nw_analysis/load_skills_yaml", json={"yaml_path": r"C:\fake\skills.yaml"})

        # Assert
        self.assertEqual(resp.get_json(), {
            "success": True,
            "message": "3 skills loaded from YAML",
            "source": r"C:\fake\skills.yaml",
            "skills": SKILL_DESCRIPTIONS,
        })
        mock_load.assert_called_once_with(r"C:\fake\skills.yaml")
        self.assertEqual(self.agent.skills, {"s1": 1, "s2": 2, "s3": 3})
        self.assertEqual(self.base_agent.skills, {"s1": 1, "s2": 2, "s3": 3})

    @patch(f"{ROUTES}.load_skills_from_yaml", side_effect=ValueError("bad yaml"))
    def test_load_skills_yaml_parse_error_returns_500(self, mock_load):
        """A load_skills_yaml parse error returns 500.

        Input: POST /load_skills_yaml where the loader raises ValueError.
        Expected: 500 echoing the error.
        """
        # Act
        resp = self.client.post("/nw_analysis/load_skills_yaml", json={"yaml_path": r"C:\fake\skills.yaml"})

        # Assert
        self.assertEqual(resp.status_code, 500)
        self.assertEqual(resp.get_json(), {"success": False, "error": "bad yaml"})

    @patch(f"{ROUTES}.load_skills_from_yaml")
    @patch("utils.helpers.get_load_path", return_value=None)
    def test_reload_from_shared_not_found_returns_400(self, mock_get_path, mock_load):
        """reload_from_shared returns 400 when no shared YAML exists.

        Input: POST /reload_from_shared where get_load_path returns None.
        Expected: 400 with a "Shared YAML not found" error; skills are not loaded.
        """
        # Act
        resp = self.client.post("/nw_analysis/reload_from_shared")

        # Assert
        self.assertEqual(resp.status_code, 400)
        self.assertIn("Shared YAML not found", resp.get_json()["error"])
        mock_load.assert_not_called()

    @patch(f"{ROUTES}.load_skills_from_yaml", return_value={"shared": 1})
    @patch("utils.helpers.get_load_path", return_value=os.path.abspath(__file__))
    def test_reload_from_shared_success(self, mock_get_path, mock_load):
        """reload_from_shared reloads skills from the shared folder.

        Input: POST /reload_from_shared where get_load_path returns a file.
        Expected: skills are loaded from it and shared to the base agent; the
        response reports the reloaded count and source.
        """
        # Arrange: get_load_path returns this (existing) test file as the "shared YAML"
        shared = os.path.abspath(__file__)

        # Act
        resp = self.client.post("/nw_analysis/reload_from_shared")

        # Assert
        self.assertEqual(resp.get_json(), {
            "success": True,
            "message": "1 skills reloaded from shared folder",
            "source": shared,
            "skills": SKILL_DESCRIPTIONS,
        })
        mock_load.assert_called_once_with(shared)
        self.assertEqual(self.base_agent.skills, {"shared": 1})

    @patch(f"{ROUTES}.load_skills_from_yaml", side_effect=OSError("share offline"))
    @patch("utils.helpers.get_load_path", return_value=os.path.abspath(__file__))
    def test_reload_from_shared_load_error_returns_500(self, mock_get_path, mock_load):
        """A reload_from_shared load error returns 500.

        Input: POST /reload_from_shared where loading raises OSError.
        Expected: 500 echoing the error.
        """
        # Act
        resp = self.client.post("/nw_analysis/reload_from_shared")

        # Assert
        self.assertEqual(resp.status_code, 500)
        self.assertEqual(resp.get_json(), {"success": False, "error": "share offline"})

    def test_get_skills_returns_agent_skill_descriptions(self):
        """GET /skills returns the agent's skill descriptions.

        Input: GET /nw_analysis/skills.
        Expected: {success, skills} with the agent skill descriptions.
        """
        # Act
        resp = self.client.get("/nw_analysis/skills")

        # Assert
        self.assertEqual(resp.get_json(), {"success": True, "skills": SKILL_DESCRIPTIONS})

    def test_get_skills_agent_unavailable_returns_500(self):
        """GET /skills returns 500 when the agent is unavailable.

        Input: GET /skills when no agent can be created.
        Expected: 500 with success False.
        """
        # Arrange
        self.make_agent_unavailable()

        # Act
        resp = self.client.get("/nw_analysis/skills")

        # Assert
        self.assertEqual(resp.status_code, 500)
        self.assertFalse(resp.get_json()["success"])


# ======================================================================
# append_skills_yaml
# ======================================================================
class TestAppendSkillsYaml(NwAnalysisTestCase):
    TARGET = Path(r"C:\fake\user\skills_2026-10-03.yaml")

    def _override_append_context(self, **overrides):
        """Swap callables on the frozen SkillAppendContext the route captured.

        The append handler is built once at import via build_skill_append_handlers
        and bound into the registered view as a partial, so its context's
        callables cannot be reached through module-level patches.
        """
        ctx = nw_routes._SKILL_APPEND_HANDLERS["append_skills_yaml_route"].args[0]
        for name, value in overrides.items():
            original = getattr(ctx, name)
            object.__setattr__(ctx, name, value)
            self.addCleanup(object.__setattr__, ctx, name, original)
        return ctx

    def test_append_requires_yaml_path(self):
        """append_skills_yaml requires a yaml_path.

        Input: POST /append_skills_yaml with an empty body.
        Expected: 400 with "yaml_path is required.".
        """
        # Act
        resp = self.client.post("/nw_analysis/append_skills_yaml", json={})

        # Assert
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.get_json(), {"success": False, "error": "yaml_path is required."})

    @patch("utils.skill_import_utils.load_and_filter_source", side_effect=FileNotFoundError("no such file"))
    def test_append_missing_source_returns_404(self, mock_load_src):
        """append_skills_yaml returns 404 for a missing source file.

        Input: POST /append_skills_yaml where the source loader raises
        FileNotFoundError.
        Expected: 404 echoing the error.
        """
        # Act
        resp = self.client.post("/nw_analysis/append_skills_yaml", json={"yaml_path": r"C:\fake\in.yaml"})

        # Assert
        self.assertEqual(resp.status_code, 404)
        self.assertEqual(resp.get_json(), {"success": False, "error": "no such file"})

    @patch("utils.skill_import_utils.load_and_filter_source", side_effect=ValueError("mapping values"))
    def test_append_unparseable_source_returns_400(self, mock_load_src):
        """append_skills_yaml returns 400 for an unparseable source.

        Input: POST /append_skills_yaml where the source loader raises ValueError.
        Expected: 400 with a "Cannot parse YAML: ..." message.
        """
        # Act
        resp = self.client.post("/nw_analysis/append_skills_yaml", json={"yaml_path": r"C:\fake\in.yaml"})

        # Assert
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.get_json()["error"], "Cannot parse YAML: mapping values")

    @patch("utils.skill_import_utils.load_and_filter_source", return_value=({}, {"bad": "missing name"}))
    def test_append_without_importable_skills_returns_400(self, mock_load_src):
        """append_skills_yaml returns 400 when nothing is importable.

        Input: POST /append_skills_yaml where the source yields no valid skills,
        only skipped ones.
        Expected: 400 echoing the skipped map.
        """
        # Act
        resp = self.client.post("/nw_analysis/append_skills_yaml", json={"yaml_path": r"C:\fake\in.yaml"})

        # Assert
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.get_json(), {
            "success": False,
            "error": "No appendable skills found in the selected YAML.",
            "skipped": {"bad": "missing name"},
        })

    def test_append_success_seeds_from_cloud_and_activates_user_yaml(self):
        """append_skills_yaml seeds from the cloud baseline and activates user.

        Input: POST /append_skills_yaml with importable skills; no user YAML
        exists but a cloud baseline does.
        Expected: 200; the import is merged onto the cloud baseline, persisted as
        a user snapshot, the source switches to "user", and the live agent is
        reloaded; the session records the modified path and the message reports
        the counts.
        """
        # Arrange
        valid = {"new": {"name": "new"}}
        merged = {"old": {"name": "old"}, "new": {"name": "new"}}
        mock_read = MagicMock(return_value={"old": {"name": "old"}})
        mock_persist = MagicMock(return_value=self.TARGET)
        mock_set_source = MagicMock()
        mock_activate = MagicMock(return_value=SKILL_DESCRIPTIONS)
        self._override_append_context(
            latest_user_yaml=MagicMock(return_value=(None, None)),
            latest_cloud_baseline=MagicMock(return_value=(Path(r"C:\fake\cloud.yaml"), "cloud")),
            read_yaml_file=mock_read,
            persist_user_yaml_snapshot=mock_persist,
            set_active_source=mock_set_source,
            activate_yaml=mock_activate,
        )

        # Act
        with patch("utils.skill_import_utils.load_and_filter_source",
                   return_value=(valid, {"junk": "no name"})), \
             patch("utils.skill_import_utils.merge_overwrite",
                   return_value=(merged, ["new"], [])) as mock_merge:
            resp = self.client.post("/nw_analysis/append_skills_yaml", json={"yaml_path": r"C:\fake\import.yaml"})

        # Assert
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json(), {
            "success": True,
            "active_source": "user",
            "mode": "append",
            "target_path": str(self.TARGET),
            "filename": self.TARGET.name,
            "appended": ["new"],
            "overwritten": [],
            "dropped": [],
            "skipped": {"junk": "no name"},
            "message": "Imported from import.yaml: appended 1, skipped 1.",
            "skills": SKILL_DESCRIPTIONS,
        })
        mock_read.assert_called_once_with(Path(r"C:\fake\cloud.yaml"))
        mock_merge.assert_called_once_with({"old": {"name": "old"}}, valid)
        mock_persist.assert_called_once_with(merged)
        mock_set_source.assert_called_once_with("user")
        mock_activate.assert_called_once_with(self.TARGET)
        sess = self.get_session()
        self.assertTrue(sess["yaml_modified"])
        self.assertEqual(sess["yaml_modified_path"], str(self.TARGET))

    def test_append_persist_failure_returns_500(self):
        """An append_skills_yaml persist failure returns 500.

        Input: POST /append_skills_yaml where persisting the snapshot raises
        PermissionError.
        Expected: 500 echoing the error; the source is not switched and the
        session is not marked modified.
        """
        # Arrange
        mock_read = MagicMock(return_value={})
        mock_persist = MagicMock(side_effect=PermissionError("read-only"))
        mock_set_source = MagicMock()
        self._override_append_context(
            latest_cloud_baseline=MagicMock(return_value=(Path(r"C:\fake\user.yaml"), "cloud")),
            read_yaml_file=mock_read,
            persist_user_yaml_snapshot=mock_persist,
            set_active_source=mock_set_source,
        )

        # Act
        with patch("utils.skill_import_utils.load_and_filter_source", return_value=({"x": {}}, {})), \
             patch("utils.skill_import_utils.merge_overwrite", return_value=({}, [], ["x"])):
            resp = self.client.post("/nw_analysis/append_skills_yaml", json={"yaml_path": r"C:\fake\import.yaml"})

        # Assert
        self.assertEqual(resp.status_code, 500)
        self.assertEqual(resp.get_json(), {"success": False, "error": "read-only"})
        mock_read.assert_called_once_with(Path(r"C:\fake\user.yaml"))
        mock_set_source.assert_not_called()
        self.assertNotIn("yaml_modified", self.get_session())


# ======================================================================
# get_issue_context
# ======================================================================
class TestGetIssueContext(NwAnalysisTestCase):

    def test_issue_context_without_session_uses_generic_prompt(self):
        """get_issue_context uses a generic prompt without a session.

        Input: GET /get_issue_context with no case context.
        Expected: a default "Perform full multi-skill log analysis" description
        and empty attachment time.
        """
        # Act
        resp = self.client.get("/nw_analysis/get_issue_context")

        # Assert
        self.assertEqual(resp.get_json(), {"description": "Perform full multi-skill log analysis",
                                           "attachment_time": ""})

    def test_issue_context_strips_tags_and_appends_text_timestamp(self):
        """get_issue_context strips subject tags and appends a text timestamp.

        Input: GET /get_issue_context with a tagged subject and a timestamp in the
        description.
        Expected: the cleaned subject with an " at around <time>" suffix and empty
        attachment time.
        """
        # Arrange
        with self.client.session_transaction() as sess:
            sess["case_context"] = self.create_mock_case_context(
                subject="[ADL][Win11] 6G Weak Signal disconnected (F/R：1/1u,40/200C)",
                description="Customer saw drop at 10/28/2025-11:25:49 on site.",
            )

        # Act
        resp = self.client.get("/nw_analysis/get_issue_context")

        # Assert
        self.assertEqual(resp.get_json(), {
            "description": "6G Weak Signal disconnected at around 10/28/2025-11:25:49",
            "attachment_time": "",
        })

    @patch(f"{ROUTES}.extract_time_from_description", return_value=datetime(2025, 10, 28, 11, 25, 49))
    def test_issue_context_prefers_selected_attachment_time(self, mock_extract):
        """get_issue_context prefers the selected attachment's time.

        Input: GET /get_issue_context with two attachments where only the
        selected one carries a parseable time.
        Expected: the description is composed around the selected attachment's
        time and attachment_time reflects it; the unselected note is not parsed.
        """
        # Arrange
        attachments = [
            ["other.zip", "url-1", ["2025-10-01", "uploaded early"]],
            ["chosen.zip", "url-2", ["2025-10-28", "issue at 10/28/2025 11:25:49"]],
        ]
        with self.client.session_transaction() as sess:
            sess["case_context"] = self.create_mock_case_context(attachment_list=attachments)
            sess["selected_files"] = [["chosen.zip", "url-2"]]

        # Act
        resp = self.client.get("/nw_analysis/get_issue_context")

        # Assert
        self.assertEqual(resp.get_json(), {
            "description": "Wi-Fi disconnects after resume at around 10/28/2025-11:25:49",
            "attachment_time": "10/28/2025-11:25:49",
        })
        mock_extract.assert_any_call("issue at 10/28/2025 11:25:49")
        self.assertNotIn(unittest.mock.call("uploaded early"), mock_extract.call_args_list)


# ======================================================================
# find_best_log moved to blueprints/log_parser (POST /log_parser/find_best_log);
# it is covered by the log_parser test suite now, not here.
# ======================================================================


# ======================================================================
# Core logic: _get_or_create_agent and pure helpers
# ======================================================================
class TestCoreLogic(NwAnalysisTestCase):

    def test_get_or_create_agent_builds_from_base_and_reuses_instance(self):
        """_get_or_create_agent builds from the base agent and reuses it.

        Input: two calls to _get_or_create_agent with a last analyzed log.
        Expected: the agent is built once from the base client/model/skills,
        cached per session, given the last log, and reused on the second call
        (nothing to prime for an empty session).
        """
        # Arrange
        self.app_config.last_analyzed_log_path = LOG_PATH

        # Act
        with self.app.test_request_context("/"):
            first = nw_routes._get_or_create_agent()
            second = nw_routes._get_or_create_agent()
            sid = nw_routes.session["chatbot_session_id"]

        # Assert
        self.assertIs(first, self.agent)
        self.assertIs(second, first)
        self.mock_agent_cls.assert_called_once_with(client="base-client", model="gpt-base", skills={"base": 1})
        self.assertIs(nw_routes._chatbot_instances[sid], self.agent)
        self.assertEqual(self.agent.current_log_path, LOG_PATH)
        self.agent.prime_with_context.assert_not_called()  # empty session -> nothing to prime

    def test_get_or_create_agent_falls_back_to_llm_helper(self):
        """_get_or_create_agent falls back to the llm_helper.

        Input: _get_or_create_agent with no base NW agent but a configured
        llm_helper.
        Expected: the agent is built from the helper client/model/skills (a base
        then the session clone).
        """
        # Arrange
        self.app_config.nw_analysis_agent = None
        self.app_config.llm_helper = MagicMock(client="helper-client", model="gpt-helper", skills={"h": 1})
        fallback_base = MagicMock(client="helper-client", model="gpt-helper", skills={"h": 1})
        self.mock_agent_cls.side_effect = [fallback_base, self.agent]

        # Act
        with self.app.test_request_context("/"):
            agent = nw_routes._get_or_create_agent()

        # Assert
        self.assertIs(agent, self.agent)
        self.assertEqual(self.mock_agent_cls.call_args_list, [
            unittest.mock.call(client="helper-client", model="gpt-helper", skills={"h": 1}),
            unittest.mock.call(client="helper-client", model="gpt-helper", skills={"h": 1}),
        ])

    def test_get_or_create_agent_raises_without_any_llm(self):
        """_get_or_create_agent raises when no LLM is available.

        Input: _get_or_create_agent with no base agent and no llm_helper.
        Expected: a RuntimeError is raised and no agent is built.
        """
        # Arrange
        self.make_agent_unavailable()

        # Act / Assert
        with self.app.test_request_context("/"):
            with self.assertRaises(RuntimeError):
                nw_routes._get_or_create_agent()
        self.mock_agent_cls.assert_not_called()

    def test_ensure_conversation_id_is_stable_until_rotated(self):
        """_ensure_nw_conversation_id is stable until explicitly rotated.

        Input: two plain calls then one with rotate=True.
        Expected: the first two return the same id; the rotate call returns a
        different one.
        """
        # Act
        with self.app.test_request_context("/"):
            first = nw_routes._ensure_nw_conversation_id()
            again = nw_routes._ensure_nw_conversation_id()
            rotated = nw_routes._ensure_nw_conversation_id(rotate=True)

        # Assert
        self.assertEqual(first, again)
        self.assertNotEqual(first, rotated)

    def test_extract_issue_context_merges_ai_analysis_and_classification(self):
        """_extract_issue_context merges AI analysis and classification.

        Input: a session with a case context, classification and AI IPS analysis.
        Expected: the issue type comes from the classification and the description
        is composed from the case description plus AI summary/symptoms.
        """
        # Arrange
        with self.app.test_request_context("/"):
            nw_routes.session["case_context"] = self.create_mock_case_context()
            nw_routes.session["classification"] = {"issue_type": "Disconnect", "confidence": 90}
            nw_routes.session["ai_ips_analysis"] = {
                "Classification": {"issue_type": "ignored"},
                "Summary": " Drops on resume ",
                "Symptoms": ["no IP", "", "deauth"],
                "Blank": "  ",
            }

            # Act
            ctx = nw_routes._extract_issue_context()

        # Assert
        self.assertEqual(ctx["issue_type"], "Disconnect")
        self.assertEqual(ctx["description"],
                         "Wi-Fi drops every time the laptop resumes from Modern Standby.\n"
                         "Summary: Drops on resume\n"
                         "Symptoms: no IP; deauth")

    def test_extract_disconnect_time_scans_sources_in_order(self):
        """_extract_disconnect_time scans its sources in order.

        Input: combinations of subject/attachment/description strings with and
        without timestamps.
        Expected: the first recognized timestamp yields an " at around <time>"
        suffix, and no timestamp yields "".
        """
        # Act / Assert
        self.assertEqual(nw_routes._extract_disconnect_time("", None, "drop 10/28/2025 11:25:49.123 ok"),
                         " at around 10/28/2025 11:25:49.123")
        self.assertEqual(nw_routes._extract_disconnect_time("seen 2025-10-28T11:25:49", "10/01/2025-01:00:00"),
                         " at around 2025-10-28T11:25:49")
        self.assertEqual(nw_routes._extract_disconnect_time("no time here"), "")


if __name__ == "__main__":
    unittest.main()
