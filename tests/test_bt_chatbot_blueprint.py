"""Unit tests for the bt_chatbot blueprint (blueprints/bt_chatbot/bt_chatbot_routes.py).

The BT chatbot is the Bluetooth twin of log_chatbot: a per-session agent
(``BtLogAgentSystem``, cloned via ``type(base)``) analyses a decoded BT HCI
log (``*.hci.txt``) and streams answers over SSE. Everything that would touch
an LLM, the network, the share folder, a native Tk dialog or the disk is
patched at its point of use in the route module:

  * ``_get_or_create_agent`` / ``_resume_agent_for`` -> MagicMock agent
  * ``chat_jobs`` / ``history_service`` / ``feedback_service`` / ``gather_service``
  * skills loaders and the ``utils.bt_skills_yaml_utils`` aliases
  * ``tk`` / ``filedialog`` (dialogs never open), ``threading`` (chat worker)
  * ``os.path.exists`` / ``isfile`` / ``isdir`` / ``os.listdir`` -> fixed fake sets

BT-specific behavior pinned here (differs from the Wi-Fi log_chatbot):
  * every service call is tagged ``domain="bt"``;
  * ``/history/stream`` and ``/history/load`` reject chat jobs whose
    ``domain`` is not ``"bt"`` (shared chat_jobs registry);
  * ``/prepare`` accepts a direct ``.hci.txt`` / ``.log`` path as-is;
  * ``/find_best_log`` falls back to the capture-folder timestamp
    (``_parse_path_timestamp``) because the decoded log does not exist yet
    at selection time, and ties are broken by explicit naming then file size.

This blueprint has no ``app_config.silent_mode`` gate (it never launches
explorer / TextAnalysisTool).
"""
import json
import os
import queue
import unittest
from datetime import date, datetime
from pathlib import Path
from unittest.mock import ANY, MagicMock, patch

from flask import session

import blueprints.chatbot.chatbot_routes as routes
from blueprints.chatbot.chatbot_routes import bt_chatbot_bp
from configs.chatbot_ui import BT_UI
from tests.base_test import BaseTestCase, TEST_CASE_NBR

# The three profile route modules were unified into one; the BT handlers are now
# methods on a per-profile AgentRoutes instance built at import time.
ROUTES = "blueprints.chatbot.chatbot_routes"


class _SyncThread:
    """Thread stand-in that runs the target inline so SSE/dialog workers finish
    within the request instead of on a real background thread."""

    def __init__(self, target=None, daemon=None, **kwargs):
        self._target = target

    def start(self):
        if self._target is not None:
            self._target()

    def join(self, timeout=None):
        pass


LOG_PATH = r"C:\fake\bt\DESKTOP-1-2026-04-08-00-16-16Z\ibtusb.etl.hci.txt"
OTHER_LOG = r"C:\fake\bt\other\ibtpci.etl.hci.txt"
SKILLS = [{"name": "A2DP", "description": "A2DP audio streaming"}]
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
    # No kept per-session skill file by default, so the skill-editor paths seed
    # from the latest user/cloud YAML rather than the agent's session path.
    agent._session_skill_path = None
    for key, value in attrs.items():
        setattr(agent, key, value)
    return agent


def sse_events(resp):
    """Decode an SSE body into a list of JSON payloads."""
    body = resp.get_data(as_text=True)
    return [json.loads(chunk[len("data: "):]) for chunk in body.split("\n\n") if chunk.startswith("data: ")]


class FakeAgent:
    """Concrete agent class so ``type(base)(...)`` cloning can be observed."""

    def __init__(self, client=None, model=None, skills=None):
        self.client = client
        self.model = model
        self.skills = skills
        self.ace_runner = None
        self.attached_ace = None
        self.current_log_path = ""
        self.primed_with = None

    def attach_ace(self, runner):
        self.attached_ace = runner

    def prime_with_context(self, **ctx):
        self.primed_with = ctx


class BtChatbotTestCase(BaseTestCase):
    blueprint = bt_chatbot_bp

    # Handlers are now methods on the per-profile AgentRoutes instance rather
    # than module-level functions, so these names are patched on that instance
    # (``self.routes``) instead of on the route module.
    INSTANCE_METHODS = frozenset({
        "_get_or_create_agent", "_resume_agent_for", "_issue_context_organized",
        "_get_configuration_agent", "_get_llm_client_model",
    })

    def setUp(self):
        super().setUp()
        # The blueprint's view functions are bound methods of the BT AgentRoutes
        # instance; reach it through one to patch its per-session store and
        # handler methods.
        self.routes = self.app.view_functions["bt_chatbot.chat"].__self__
        instances = patch.dict(self.routes.session_agents, clear=True)
        instances.start()
        self.addCleanup(instances.stop)

        # IPS gating is a shared-framework dependency added in the refactor:
        # blocking_state() gates chat SSE (428), prompt_state() is merged into
        # set_log responses, start_new_session() runs on reset. Neutralise it so
        # these routes behave as they did pre-gating.
        ips = patch(f"{ROUTES}.check_ips_service")
        self.mock_check_ips = ips.start()
        self.addCleanup(ips.stop)
        self.mock_check_ips.blocking_state.return_value = None
        self.mock_check_ips.prompt_state.return_value = {}
        self.mock_check_ips.start_new_session.return_value = None

        # The System-Event-Log lookup now lives in utils.event_log_utils and is
        # imported into the routes module; default it to "no evtx found".
        evt = patch(f"{ROUTES}.find_event_log_for_log", return_value="")
        self.mock_find_evt = evt.start()
        self.addCleanup(evt.stop)

    def override_handler_ctx(self, handler_key, **overrides):
        """Swap callables on the frozen context a skill handler captured.

        Skill handlers are ``partial(fn, context)`` bound at import time; reach
        the context through the handler and ``object.__setattr__`` past the
        frozen dataclass. ``append_skills_yaml_route`` carries its own narrower
        ``SkillAppendContext``; the editor endpoints share one ``SkillEditorContext``.
        """
        ctx = self.routes.handlers[handler_key].args[0]
        for name, value in overrides.items():
            original = getattr(ctx, name)
            object.__setattr__(ctx, name, value)
            self.addCleanup(object.__setattr__, ctx, name, original)
        return ctx

    def override_skill_editor_ctx(self, **overrides):
        return self.override_handler_ctx("skills_yaml_status", **overrides)

    def override_skill_append_ctx(self, **overrides):
        return self.override_handler_ctx("append_skills_yaml_route", **overrides)

    def shared_ctx(self):
        """Return the frozen SharedRouteContext the shared handlers closed over.

        ``build_shared_handlers`` returns closures (not partials), so the context
        is reached through a handler's closure cells rather than ``.args``.
        """
        from services.chatbot.shared_routes import SharedRouteContext
        for handler in self.routes.handlers.values():
            for cell in (getattr(handler, "__closure__", None) or ()):
                try:
                    value = cell.cell_contents
                except ValueError:
                    continue
                if isinstance(value, SharedRouteContext):
                    return value
        raise AssertionError("SharedRouteContext not found in routes.handlers")

    def override_shared_ctx(self, **overrides):
        ctx = self.shared_ctx()
        for name, value in overrides.items():
            original = getattr(ctx, name)
            object.__setattr__(ctx, name, value)
            self.addCleanup(object.__setattr__, ctx, name, original)
        return ctx

    def use_base_agent(self, agent=None):
        """Make the REAL ``_get_or_create_agent`` clone-return ``agent``.

        bt clones the per-session agent via ``type(base)(...)``; a base whose
        ``__new__`` returns a preset mock lets the factory (reset/get_skills) and
        shared handlers — which captured the original function at import — all
        hand out the same agent without patching the module symbol.
        """
        agent = agent if agent is not None else make_agent()

        class _CloneBase:
            client = "bt-client"
            model = "bt-model"
            skills = None
            ace_runner = None

            def __new__(cls, *args, **kwargs):
                return agent

        self.app_config.bt_chatbot_agent = object.__new__(_CloneBase)
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

    def fake_paths(self, func, *paths):
        """Patch ``os.path.<func>`` so ONLY ``paths`` are reported as present."""
        present = set(paths)
        return self.patch_route(f"os.path.{func}", side_effect=lambda p: p in present)

    def patch_services(self):
        self.feedback = self.patch_route("feedback_service")
        self.history = self.patch_route("history_service")
        self.gather = self.patch_route("gather_service")
        self.jobs = self.patch_route("chat_jobs")


# ======================================================================
# Structure
# ======================================================================
class TestBtChatbotStructure(BtChatbotTestCase):
    EXPECTED_RULES = {
        "/bt_chatbot/": {"GET"},
        "/bt_chatbot/browse": {"GET"},
        "/bt_chatbot/set_log": {"POST"},
        "/bt_chatbot/suggest_issue_times": {"POST"},
        "/bt_chatbot/chat": {"POST"},
        "/bt_chatbot/reset": {"POST"},
        "/bt_chatbot/chat/stop": {"POST"},
        "/bt_chatbot/history/list": {"GET"},
        "/bt_chatbot/history/stream": {"GET"},
        "/bt_chatbot/history/get": {"GET"},
        "/bt_chatbot/history/delete": {"POST"},
        "/bt_chatbot/history/rename": {"POST"},
        "/bt_chatbot/history/pin": {"POST"},
        "/bt_chatbot/history/load": {"POST"},
        "/bt_chatbot/back_to_avatar": {"POST"},
        "/bt_chatbot/prepare": {"POST"},
        "/bt_chatbot/browse_yaml": {"GET"},
        "/bt_chatbot/load_skills_yaml": {"POST"},
        "/bt_chatbot/append_skills_yaml": {"POST"},
        "/bt_chatbot/reload_from_shared": {"POST"},
        "/bt_chatbot/skills": {"GET"},
        "/bt_chatbot/get_issue_context": {"GET"},
        "/bt_chatbot/skills_yaml_status": {"GET"},
        "/bt_chatbot/skills_yaml_use_cloud": {"POST"},
        "/bt_chatbot/skills_yaml_use_user": {"POST"},
        "/bt_chatbot/refresh_cloud_baseline": {"POST"},
        "/bt_chatbot/load_local_skills_yaml": {"GET"},
        "/bt_chatbot/save_local_skills_yaml": {"POST"},
        "/bt_chatbot/delete_local_skill": {"POST"},
        "/bt_chatbot/upload_modified_yaml": {"POST"},
        "/bt_chatbot/prompts/status": {"GET"},
        "/bt_chatbot/prompts/get": {"GET"},
        "/bt_chatbot/prompts/save": {"POST"},
        "/bt_chatbot/prompts/keep": {"POST"},
        "/bt_chatbot/prompts/use_source": {"POST"},
        "/bt_chatbot/prompts/revert": {"POST"},
        "/bt_chatbot/prompts/refresh_shared": {"POST"},
        "/bt_chatbot/prompts/upload": {"POST"},
    }

    def test_blueprint_registered_with_url_prefix(self):
        """Verify the blueprint object and its URL prefix.

        Input: the Flask app with ``bt_chatbot_bp`` registered (via setUp).
        Expected: the registered "bt_chatbot" blueprint is this object and its
        url_prefix is "/bt_chatbot".
        """
        # Arrange / Act
        registered = self.app.blueprints.get("bt_chatbot")

        # Assert
        self.assertIs(registered, bt_chatbot_bp)
        self.assertEqual(bt_chatbot_bp.url_prefix, "/bt_chatbot")

    def test_all_31_routes_registered_with_methods(self):
        """All 38 bt_chatbot routes are registered with the right methods.

        Input: the app's URL map filtered to bt_chatbot.* endpoints.
        Expected: the rule set exactly matches EXPECTED_RULES (38 rules) and each
        rule supports its expected HTTP methods.
        """
        # Arrange / Act
        rules = {r.rule: r.methods for r in self.app.url_map.iter_rules()
                 if r.endpoint.startswith("bt_chatbot.")}

        # Assert
        self.assertEqual(set(rules), set(self.EXPECTED_RULES))
        self.assertEqual(len(rules), 38)
        for rule, methods in self.EXPECTED_RULES.items():
            self.assertTrue(methods.issubset(rules[rule]), rule)


# ======================================================================
# Pages + native dialogs
# ======================================================================
class TestIndexAndDialogs(BtChatbotTestCase):

    def setUp(self):
        super().setUp()
        self.render = self.patch_route("render_template", return_value="rendered")
        self.read_range = self.patch_route("read_log_time_range", return_value=(None, None))
        self.organize = self.patch_route("_issue_context_organized")
        self.extract = self.patch_route("_extract_issue_context", return_value={"description": "Headset drops"})

    def test_index_renders_with_suggested_log_and_prewarms_issue_cache(self):
        """The index page suggests the last log and pre-warms the issue cache.

        Input: GET /bt_chatbot/ with app_config.last_analyzed_log_path set and a
        cold issue cache.
        Expected: 200; bt_chatbot.html rendered with the suggested log and the
        extracted issue description; the log time range is read and the issue
        context is organized once to warm the cache.
        """
        # Arrange
        self.app_config.last_analyzed_log_path = LOG_PATH

        # Act
        resp = self.client.get("/bt_chatbot/")

        # Assert
        self.assertEqual(resp.status_code, 200)
        self.render.assert_called_once_with("chatbot/page.html", ui=BT_UI, suggested_log=LOG_PATH,
                                            issue_description="Headset drops")
        self.read_range.assert_called_once_with(LOG_PATH)
        self.organize.assert_called_once_with("Headset drops", None, None)

    def test_index_skips_prewarm_when_cache_is_hot(self):
        """The index page skips pre-warming when the issue cache is already hot.

        Input: GET /bt_chatbot/ with a suggested log but a populated
        ``_issue_ai_quick`` session cache.
        Expected: 200; _issue_context_organized is not called again.
        """
        # Arrange
        self.app_config.last_analyzed_log_path = LOG_PATH
        with self.client.session_transaction() as sess:
            sess["_issue_ai_quick"] = {"data": {}}

        # Act
        resp = self.client.get("/bt_chatbot/")

        # Assert
        self.assertEqual(resp.status_code, 200)
        self.organize.assert_not_called()

    def test_index_without_log_and_with_failing_context_still_renders(self):
        """The index page renders even with no log and a failing context lookup.

        Input: GET /bt_chatbot/ with no suggested log and _extract_issue_context
        raising.
        Expected: 200; bt_chatbot.html rendered with empty suggested_log and
        empty issue_description; the log time range is never read.
        """
        # Arrange
        self.extract.side_effect = RuntimeError("no session")

        # Act
        resp = self.client.get("/bt_chatbot/")

        # Assert
        self.assertEqual(resp.status_code, 200)
        self.render.assert_called_once_with("chatbot/page.html", ui=BT_UI, suggested_log="", issue_description="")
        self.read_range.assert_not_called()

    def test_index_prewarm_failure_is_swallowed(self):
        """A pre-warm failure does not break the index page.

        Input: GET /bt_chatbot/ with a suggested log where read_log_time_range
        raises OSError.
        Expected: 200; the page still renders once.
        """
        # Arrange
        self.app_config.last_analyzed_log_path = LOG_PATH
        self.read_range.side_effect = OSError("locked")

        # Act
        resp = self.client.get("/bt_chatbot/")

        # Assert
        self.assertEqual(resp.status_code, 200)
        self.render.assert_called_once()

    def _patch_browse_dialog(self, path):
        """Patch the shared browse handler's native dialog + worker thread.

        The shared ``browse`` handler imports tkinter locally and runs the
        dialog on ``shared_routes.threading``; patch both so no window opens.
        """
        self.patch_target("services.chatbot.shared_routes.threading",
                          new=MagicMock(Thread=_SyncThread))
        tk = self.patch_target("tkinter.Tk")
        askopen = self.patch_target("tkinter.filedialog.askopenfilename", return_value=path)
        return tk, askopen

    def test_browse_returns_selected_hci_path(self):
        """The browse dialog returns the picked .hci.txt path.

        Input: GET /bt_chatbot/browse where the native file dialog returns a log
        path.
        Expected: {success, path}; the dialog is titled "Select log file" with an
        hci.txt filter; the hidden Tk root is destroyed.
        """
        # Arrange
        tk, askopen = self._patch_browse_dialog(LOG_PATH)

        # Act
        resp = self.client.get("/bt_chatbot/browse")

        # Assert
        self.assertEqual(resp.get_json(), {"success": True, "path": LOG_PATH})
        kwargs = askopen.call_args.kwargs
        self.assertEqual(kwargs["title"], "Select log file")
        self.assertEqual(kwargs["filetypes"][0], ("hci.txt files", "*.hci.txt"))
        tk.return_value.destroy.assert_called_once()

    def test_browse_cancelled_dialog_returns_empty_path(self):
        """A cancelled browse dialog returns an empty path.

        Input: GET /bt_chatbot/browse where the dialog returns "".
        Expected: {success: True, path: ""}.
        """
        # Arrange
        self._patch_browse_dialog("")

        # Act
        resp = self.client.get("/bt_chatbot/browse")

        # Assert
        self.assertEqual(resp.get_json(), {"success": True, "path": ""})

    def test_browse_yaml_returns_selected_yaml(self):
        """The shared browse_yaml handler returns the chosen YAML path.

        Input: GET /bt_chatbot/browse_yaml where the factory dialog returns a
        YAML path.
        Expected: {success: True, path}.
        """
        # Arrange
        choose = self.patch_target("services.chatbot.factory.choose_skills_yaml",
                                   return_value=r"C:\fake\bt_skills.yaml")

        # Act
        resp = self.client.get("/bt_chatbot/browse_yaml")

        # Assert
        self.assertEqual(resp.get_json(), {"success": True, "path": r"C:\fake\bt_skills.yaml"})
        choose.assert_called_once_with()


# ======================================================================
# /set_log
# ======================================================================
class TestSetLog(BtChatbotTestCase):

    def setUp(self):
        super().setUp()
        self.patch_services()
        self.agent = make_agent()
        self.get_agent = self.patch_route("_get_or_create_agent", return_value=self.agent)
        self.ctx = {"case_nbr": TEST_CASE_NBR, "subject": "BT headset drops"}
        self.patch_route("_extract_issue_context", return_value=self.ctx)
        self.read_range = self.patch_route(
            "read_log_time_range",
            return_value=(datetime(2026, 4, 8, 0, 0, 0), datetime(2026, 4, 8, 0, 30, 0)))
        self.find_evt = self.patch_route("find_event_log_for_log", return_value=r"C:\fake\bt\System.evtx")

    def test_missing_log_path_returns_400(self):
        """A blank log_path is rejected.

        Input: POST /set_log with log_path="  ".
        Expected: 400 with "log_path is required"; no agent is created.
        """
        # Act
        resp = self.client.post("/bt_chatbot/set_log", json={"log_path": "  "})

        # Assert
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.get_json(), {"success": False, "error": "log_path is required"})
        self.get_agent.assert_not_called()

    def test_first_load_sets_log_primes_agent_and_returns_metadata(self):
        """First-time log load primes the agent and returns full metadata.

        Input: POST /set_log with a fresh LOG_PATH and an empty session.
        Expected: 200; the agent gets the log path, resets, and is primed with
        the issue context; the response carries skills, span, last time,
        has-date, evtx path, rotated=False, and a new conversation id; the
        session stores the log path and a BT conversation is ensured.
        """
        # Act
        resp = self.client.post("/bt_chatbot/set_log", json={"log_path": LOG_PATH})

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
        self.assertEqual(body["evtx_path"], r"C:\fake\bt\System.evtx")
        self.assertFalse(body["rotated"])
        self.assertEqual(body["previous_log_path"], "")
        sess = self.get_session()
        self.assertEqual(sess["chatbot_log_path"], LOG_PATH)
        self.assertEqual(body["new_conversation_id"], sess["feedback_conversation_id"])
        self.feedback.ensure_conversation.assert_called_once_with(
            conversation_id=body["new_conversation_id"], session_id="",
            issue=self.ctx, log_path=LOG_PATH, domain="bt")

    def test_switching_to_a_different_log_rotates_conversation(self):
        """Loading a different log rotates to a new conversation.

        Input: POST /set_log with LOG_PATH while the session already holds a
        different log and an old conversation id.
        Expected: rotated=True; the previous log/conversation are reported, a new
        conversation id is issued, and the agent conversation is reset.
        """
        # Arrange
        with self.client.session_transaction() as sess:
            sess["chatbot_log_path"] = OTHER_LOG
            sess["feedback_conversation_id"] = "old-conv"

        # Act
        body = self.client.post("/bt_chatbot/set_log", json={"log_path": LOG_PATH}).get_json()

        # Assert
        self.assertTrue(body["rotated"])
        self.assertEqual(body["previous_log_path"], OTHER_LOG)
        self.assertEqual(body["previous_conversation_id"], "old-conv")
        self.assertNotEqual(body["new_conversation_id"], "old-conv")
        self.agent.reset_conversation.assert_called_once_with()

    def test_reloading_same_log_keeps_conversation_and_history(self):
        """Reloading the same log keeps the existing conversation and history.

        Input: POST /set_log with the same LOG_PATH already in session; the agent
        still holds conversation history.
        Expected: rotated=False; the conversation id is unchanged; the agent is
        not reset, its history is preserved, and no stored context is re-fetched.
        """
        # Arrange
        history = [{"role": "user", "content": "q1"}, {"role": "assistant", "content": "a1"}]
        self.agent.conversation_history = list(history)
        with self.client.session_transaction() as sess:
            sess["chatbot_log_path"] = LOG_PATH
            sess["feedback_conversation_id"] = "same-conv"

        # Act
        body = self.client.post("/bt_chatbot/set_log", json={"log_path": LOG_PATH}).get_json()

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
        Expected: the stored BT context is fetched and imported into the agent.
        """
        # Arrange
        self.history.get_context.return_value = [{"role": "user", "content": "stored"}]
        with self.client.session_transaction() as sess:
            sess["chatbot_log_path"] = LOG_PATH
            sess["feedback_conversation_id"] = "same-conv"

        # Act
        self.client.post("/bt_chatbot/set_log", json={"log_path": LOG_PATH})

        # Assert
        self.history.get_context.assert_called_once_with("same-conv", domain="bt")
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
        body = self.client.post("/bt_chatbot/set_log", json={"log_path": LOG_PATH}).get_json()

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
        resp = self.client.post("/bt_chatbot/set_log", json={"log_path": LOG_PATH})

        # Assert
        self.assertEqual(resp.status_code, 500)
        self.assertEqual(resp.get_json(), {"success": False, "error": "Log Chatbot Agent is not available."})


# ======================================================================
# /suggest_issue_times
# ======================================================================
class TestSuggestIssueTimes(BtChatbotTestCase):

    def setUp(self):
        super().setUp()
        self.agent = make_agent(current_log_path=LOG_PATH, client="llm-client", _raw_log_cache=["l1", "l2"])
        self.agent._ensure_raw_log_cache.return_value = None
        self.get_agent = self.patch_route("_get_or_create_agent", return_value=self.agent)
        self.first, self.last = datetime(2026, 4, 8, 0, 0, 0), datetime(2026, 4, 8, 1, 0, 0)
        self.patch_route("read_log_time_range", return_value=(self.first, self.last))
        self.find_evt = self.patch_route("find_event_log_for_log", return_value="")
        self.build = self.patch_route("build_issue_time_suggestions")
        self.nearest = self.patch_route("find_nearest_event_error")

    def test_success_passes_log_lines_and_range_to_ai(self):
        """Issue-time suggestion forwards cached log lines and range to the AI.

        Input: POST /suggest_issue_times with text " drops at noon " and a loaded
        log with a cached range.
        Expected: 200 with the AI payload; build_issue_time_suggestions is called
        with trimmed text, the cached log lines, the first/last timestamps, the
        LLM client/model, and an empty event list.
        """
        # Arrange
        self.build.return_value = {"success": True, "suggestions": []}

        # Act
        resp = self.client.post("/bt_chatbot/suggest_issue_times", json={"text": " drops at noon "})

        # Assert
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json(), {"success": True, "suggestions": []})
        self.build.assert_called_once_with(
            text="drops at noon", log_lines=["l1", "l2"], first_ts=self.first, last_ts=self.last,
            llm_client="llm-client", llm_model="test-model", brief=ANY, event_log_events=[])

    def test_event_log_anchor_is_fetched_and_nearest_error_attached(self):
        """Event-log events are fetched and nearest errors linked to suggestions.

        Input: POST /suggest_issue_times with a level_filter and an available
        evtx; suggestions include a full-date time and a time-only entry.
        Expected: get_paged_events is queried with the filter; the events feed
        the AI; the full-date suggestion gets a nearest_error while the time-only
        one does not.
        """
        # Arrange
        self.find_evt.return_value = r"C:\fake\bt\System.evtx"
        events = [{"level": "Error", "time": "04/08/2026-00:10:00"}]
        get_paged = self.patch_target("services.event_log_service.get_paged_events",
                                      return_value={"events": events})
        self.build.return_value = {"success": True,
                                   "suggestions": [{"issue_time": "04/08/2026-00:12:00"},
                                                   {"issue_time": "00:12:00"}]}
        self.nearest.return_value = {"time": "04/08/2026-00:10:00"}

        # Act
        body = self.client.post("/bt_chatbot/suggest_issue_times",
                                json={"text": "x", "level_filter": "error"}).get_json()

        # Assert
        get_paged.assert_called_once_with(r"C:\fake\bt\System.evtx", offset=0, limit=500,
                                          source_filter="all", level_filter="error")
        self.assertEqual(self.build.call_args.kwargs["event_log_events"], events)
        self.nearest.assert_called_once_with(events, datetime(2026, 4, 8, 0, 12, 0))
        self.assertEqual(body["suggestions"][0]["nearest_error"], {"time": "04/08/2026-00:10:00"})
        self.assertNotIn("nearest_error", body["suggestions"][1])  # time-only: no date distance

    def test_user_explicit_time_skips_nearest_error_linking(self):
        """An explicit user time skips nearest-error linking.

        Input: POST /suggest_issue_times where the AI marks the result
        user_explicit with a dated suggestion.
        Expected: find_nearest_event_error is not called.
        """
        # Arrange
        self.find_evt.return_value = r"C:\fake\bt\System.evtx"
        self.patch_target("services.event_log_service.get_paged_events",
                          return_value={"events": [{"level": "Error"}]})
        self.build.return_value = {"success": True, "user_explicit": True,
                                   "suggestions": [{"issue_time": "04/08/2026-00:12:00"}]}

        # Act
        self.client.post("/bt_chatbot/suggest_issue_times", json={"text": "at 04/08/2026-00:12:00"})

        # Assert
        self.nearest.assert_not_called()

    def test_no_text_and_no_log_lines_returns_400(self):
        """No text and no log lines yields 400.

        Input: POST /suggest_issue_times with empty text and no loaded log/cache.
        Expected: 400 asking the user to type a description or load a log; the AI
        is not called.
        """
        # Arrange
        self.agent.current_log_path = ""
        self.agent._ensure_raw_log_cache.return_value = "no log"

        # Act
        resp = self.client.post("/bt_chatbot/suggest_issue_times", json={"text": ""})

        # Assert
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.get_json()["error"], "Type a problem description or load a log first.")
        self.build.assert_not_called()

    def test_unsuccessful_ai_payload_returns_503(self):
        """An unsuccessful AI payload maps to 503.

        Input: POST /suggest_issue_times where the AI returns success=False.
        Expected: 503 echoing the AI error.
        """
        # Arrange
        self.build.return_value = {"success": False, "error": "LLM unavailable"}

        # Act
        resp = self.client.post("/bt_chatbot/suggest_issue_times", json={"text": "x"})

        # Assert
        self.assertEqual(resp.status_code, 503)
        self.assertEqual(resp.get_json()["error"], "LLM unavailable")

    def test_agent_failure_returns_500(self):
        """An agent failure returns 500.

        Input: POST /suggest_issue_times where _get_or_create_agent raises.
        Expected: 500 with {success: False, error}.
        """
        # Arrange
        self.get_agent.side_effect = RuntimeError("no agent")

        # Act
        resp = self.client.post("/bt_chatbot/suggest_issue_times", json={"text": "x"})

        # Assert
        self.assertEqual(resp.status_code, 500)
        self.assertEqual(resp.get_json(), {"success": False, "error": "no agent"})


# ======================================================================
# /chat
# ======================================================================
class TestChat(BtChatbotTestCase):

    def setUp(self):
        super().setUp()
        # chat_jobs stays REAL (its in-memory job + job_sse produce the SSE
        # frames); only the side-car services are mocked.
        self.feedback = self.patch_route("feedback_service")
        self.history = self.patch_route("history_service")
        self.gather = self.patch_route("gather_service")
        # The worker runs inline so the job is terminal before job_sse streams.
        self.patch_route("threading", new=MagicMock(Thread=_SyncThread))
        self.agent = make_agent(current_log_path=LOG_PATH)
        self.agent.chat.return_value = {"answer": "A2DP stream dropped"}
        self.resume = self.patch_route("_resume_agent_for", return_value=self.agent)
        self.issue_ctx = {"case_nbr": TEST_CASE_NBR}
        self.patch_route("_extract_issue_context", return_value=self.issue_ctx)

    def test_empty_message_returns_400(self):
        """A blank chat message is rejected.

        Input: POST /chat with message="   ".
        Expected: 400 with "message is required"; the agent is not resumed.
        """
        # Act
        resp = self.client.post("/bt_chatbot/chat", json={"message": "   "})

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
        resp = self.client.post("/bt_chatbot/chat", json={"message": "q"})

        # Assert
        self.assertEqual(resp.status_code, 428)
        self.assertEqual(resp.get_json(), {"needs_case": True})
        self.agent.chat.assert_not_called()

    def test_chat_streams_steps_then_done_and_records_bt_turn(self):
        """Chat streams the worker's steps then a done event and records a turn.

        Input: POST /chat whose agent emits one step before returning.
        Expected: an SSE stream of that step then a done event carrying the
        result; the agent chats with default max_steps/temperature and a step
        callback; gather/feedback/history record the turn tagged domain="bt".
        """
        # Arrange
        def fake_chat(message, **kwargs):
            kwargs["step_callback"]({"tool": "grep", "summary": "found drop"})
            return {"answer": "A2DP stream dropped"}

        self.agent.chat.side_effect = fake_chat

        # Act
        resp = self.client.post("/bt_chatbot/chat",
                                json={"message": "why drop?", "parent_message_id": "p-1"})

        # Assert
        self.assertEqual(resp.mimetype, "text/event-stream")
        events = sse_events(resp)
        self.assertEqual(events[0], {"type": "step", "step": {"tool": "grep", "summary": "found drop"}})
        self.assertEqual(events[-1]["type"], "done")
        self.assertEqual(events[-1]["result"], {"answer": "A2DP stream dropped"})
        call = self.agent.chat.call_args
        self.assertEqual(call.args, ("why drop?",))
        self.assertEqual((call.kwargs["max_steps"], call.kwargs["temperature"]), (6, 0.2))
        self.assertIn("step_callback", call.kwargs)
        self.assertEqual(self.gather.record_send.call_args.kwargs["domain"], "bt")
        fb = self.feedback.record_turn.call_args.kwargs
        self.assertEqual((fb["mode"], fb["domain"], fb["parent_message_id"]), ("tools", "bt", "p-1"))
        hist = self.history.record_turn.call_args.kwargs
        self.assertEqual((hist["mode"], hist["domain"], hist["log_path"]), ("tools", "bt", LOG_PATH))
        self.gather.record_usage.assert_called_once()

    def test_numeric_parameters_are_clamped(self):
        """Out-of-range numeric params are clamped to valid bounds.

        Input: POST /chat with temperature=5, max_steps=99,
        issue_time_window_minutes=-3.
        Expected: the agent chats with temperature=1.0 and max_steps=12; the
        issue-time window is clamped to 0.
        """
        # Act
        self.client.post("/bt_chatbot/chat", json={
            "message": "q", "temperature": 5, "max_steps": 99, "issue_time_window_minutes": -3})

        # Assert
        kwargs = self.agent.chat.call_args.kwargs
        self.assertEqual((kwargs["temperature"], kwargs["max_steps"]), (1.0, 12))
        self.assertEqual(self.agent.issue_time_window_minutes, 0)

    def test_invalid_numeric_parameters_fall_back_to_defaults(self):
        """Non-numeric params fall back to defaults.

        Input: POST /chat with temperature="hot", max_steps="lots",
        issue_time_window_minutes="x".
        Expected: the agent chats with the default temperature 0.2 and
        max_steps 6; the issue-time window attribute is left untouched.
        """
        # Act
        self.client.post("/bt_chatbot/chat", json={
            "message": "q", "temperature": "hot", "max_steps": "lots", "issue_time_window_minutes": "x"})

        # Assert
        kwargs = self.agent.chat.call_args.kwargs
        self.assertEqual((kwargs["temperature"], kwargs["max_steps"]), (0.2, 6))
        self.assertNotIsInstance(self.agent.issue_time_window_minutes, int)  # untouched MagicMock attr

    def test_no_log_loaded_streams_error(self):
        """Chatting with no log loaded streams an error event.

        Input: POST /chat where the agent has no current_log_path and the session
        has none either.
        Expected: a single SSE error event asking to set a log first; the agent
        never chats.
        """
        # Arrange
        self.agent.current_log_path = ""

        # Act
        events = sse_events(self.client.post("/bt_chatbot/chat", json={"message": "q"}))

        # Assert
        self.assertEqual(events, [{"type": "error", "content": "No log file loaded. Please set a log file first."}])
        self.agent.chat.assert_not_called()

    def test_missing_agent_log_is_recovered_from_session(self):
        """A missing agent log is recovered from the session.

        Input: POST /chat where the agent has no log but the session holds
        OTHER_LOG.
        Expected: the agent's current_log_path is set to OTHER_LOG and the stream
        completes with a done event.
        """
        # Arrange
        self.agent.current_log_path = ""
        with self.client.session_transaction() as sess:
            sess["chatbot_log_path"] = OTHER_LOG

        # Act
        events = sse_events(self.client.post("/bt_chatbot/chat", json={"message": "q"}))

        # Assert
        self.assertEqual(self.agent.current_log_path, OTHER_LOG)
        self.assertEqual(events[-1]["type"], "done")

    def test_sidebar_issue_time_overrides_agent_issue_time(self):
        """A sidebar issue_time overrides the agent's own issue time.

        Input: POST /chat with issue_time="10/28/2025-11:25:49" while the agent
        context holds an attachment_time.
        Expected: the agent's issue_time becomes the parsed datetime, the
        time-only flag is cleared, and attachment_time is dropped from context.
        """
        # Arrange
        self.agent.issue_context = {"attachment_time": "01/01/2026-00:00:00"}

        # Act
        self.client.post("/bt_chatbot/chat", json={"message": "q", "issue_time": "10/28/2025-11:25:49"})

        # Assert
        self.assertEqual(self.agent.issue_time, datetime(2025, 10, 28, 11, 25, 49))
        self.assertFalse(self.agent._issue_time_time_only)
        self.assertNotIn("attachment_time", self.agent.issue_context)

    def test_explicitly_cleared_issue_time_strips_timestamps_from_context(self):
        """An explicitly cleared issue_time strips timestamps from context text.

        Input: POST /chat with issue_time="" and issue_time_cleared=True while
        the context has timestamps embedded in description/subject.
        Expected: the agent's issue_time becomes None and all timestamps are
        removed from the description and subject.
        """
        # Arrange
        self.agent.issue_time = datetime(2025, 1, 1)
        self.agent.issue_context = {"attachment_time": "x",
                                    "description": "dropped at 10/28/2025-11:25:49 then 12:00:01 again",
                                    "subject": "BT 2025-10-28 11:25:49 drop"}

        # Act
        self.client.post("/bt_chatbot/chat", json={"message": "q", "issue_time": "", "issue_time_cleared": True})

        # Assert
        self.assertIsNone(self.agent.issue_time)
        self.assertEqual(self.agent.issue_context, {"description": "dropped at then again", "subject": "BT drop"})

    def test_empty_issue_time_without_clear_flag_keeps_agent_time(self):
        """An empty issue_time without the clear flag keeps the agent's time.

        Input: POST /chat with issue_time="" and no issue_time_cleared flag.
        Expected: the agent's existing issue_time is left unchanged.
        """
        # Arrange
        sentinel = datetime(1, 1, 1, 14, 50, 51)
        self.agent.issue_time = sentinel

        # Act
        self.client.post("/bt_chatbot/chat", json={"message": "q", "issue_time": ""})

        # Assert
        self.assertIs(self.agent.issue_time, sentinel)

    def test_worker_detaches_agent_and_starts_background_job(self):
        """The chat worker detaches the session agent and starts a BT job.

        Input: POST /chat with a session-bound agent.
        Expected: a chat job is started (title, agent, domain="bt") and the agent
        is popped from its session slot so a concurrent switch can't corrupt it.
        """
        # Arrange
        start_job = self.patch_route("chat_jobs")
        start_job.start_job.return_value = MagicMock(turn_id="t-1", conversation_id="c-1")
        self.patch_route("_job_sse", return_value=iter(["data: {}\n\n"]))
        with self.client.session_transaction() as sess:
            sess["chatbot_session_id"] = "sid-1"
        self.routes.session_agents["sid-1"] = self.agent

        # Act
        self.client.post("/bt_chatbot/chat", json={"message": "analyse"})

        # Assert
        kwargs = start_job.start_job.call_args.kwargs
        self.assertEqual((kwargs["title"], kwargs["agent"], kwargs["domain"]), ("analyse", self.agent, "bt"))
        self.assertNotIn("sid-1", self.routes.session_agents)  # detached from session slot

    def test_worker_failure_streams_error_and_books_failed_status(self):
        """A failing chat worker streams an error and books a failed status.

        Input: POST /chat where the agent chat raises TimeoutError.
        Expected: an SSE error event; gather records a "failed" turn status with
        error code "TimeoutError" and domain="bt".
        """
        # Arrange
        self.agent.chat.side_effect = TimeoutError("LLM timeout")

        # Act
        events = sse_events(self.client.post("/bt_chatbot/chat", json={"message": "q"}))

        # Assert
        self.assertEqual(events[-1], {"type": "error", "content": "LLM timeout"})
        status = self.gather.record_turn_status.call_args.kwargs
        self.assertEqual((status["status"], status["error_code"], status["domain"]),
                         ("failed", "TimeoutError", "bt"))

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
        events = sse_events(self.client.post("/bt_chatbot/chat", json={"message": "q"}))

        # Assert
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["type"], "error")
        self.assertIn("Log Chatbot Agent is not available", events[0]["content"])
        self.gather.record_turn_status.assert_not_called()


# ======================================================================
# /reset, /chat/stop
# ======================================================================
class TestResetAndStop(BtChatbotTestCase):

    def setUp(self):
        super().setUp()
        # reset is a factory handler (captures the real _get_or_create_agent);
        # chat_stop is a shared handler using shared_routes-level services.
        self.agent = self.use_base_agent(make_agent())
        self.jobs = self.patch_target("services.chatbot.shared_routes.chat_jobs")
        self.gather = self.patch_target("services.chatbot.shared_routes.gather_service")
        self.patch_target("services.chatbot.shared_routes.extract_issue_context",
                          return_value={"case_nbr": TEST_CASE_NBR})

    def test_reset_clears_conversation(self):
        """Reset clears the agent conversation.

        Input: POST /reset.
        Expected: {success, message}; the agent's conversation is reset once.
        """
        # Act
        resp = self.client.post("/bt_chatbot/reset")

        # Assert
        self.assertEqual(resp.get_json(), {"success": True, "message": "Conversation reset."})
        self.agent.reset_conversation.assert_called_once_with()

    def test_reset_failure_returns_500(self):
        """A reset failure returns 500.

        Input: POST /reset where no agent can be built.
        Expected: 500 with {success: False, error}.
        """
        # Arrange: no base agent and no usable llm client -> agent build raises
        self.app_config.bt_chatbot_agent = None
        self.app_config.llm_helper = MagicMock(client=None)

        # Act
        resp = self.client.post("/bt_chatbot/reset")

        # Assert
        self.assertEqual(resp.status_code, 500)
        self.assertFalse(resp.get_json()["success"])

    def test_stop_with_explicit_conversation_cancels_and_records_status(self):
        """Stopping an explicit conversation cancels it and records the status.

        Input: POST /chat/stop with a conversation_id whose job is running and is
        successfully cancelled.
        Expected: {success, stopped: True}; the job is cancelled by id and gather
        records a "cancelled" turn status tagged domain="bt".
        """
        # Arrange
        self.jobs.get_job.return_value = MagicMock(turn_id="t-9")
        self.jobs.request_cancel.return_value = True

        # Act
        resp = self.client.post("/bt_chatbot/chat/stop", json={"conversation_id": CONV_ID})

        # Assert
        self.assertEqual(resp.get_json(), {"success": True, "stopped": True})
        self.jobs.request_cancel.assert_called_once_with(CONV_ID)
        kwargs = self.gather.record_turn_status.call_args.kwargs
        self.assertEqual((kwargs["conversation_id"], kwargs["turn_id"], kwargs["status"], kwargs["domain"]),
                         (CONV_ID, "t-9", "cancelled", "bt"))

    def test_stop_falls_back_to_session_conversation(self):
        """Stop falls back to the session conversation when none is given.

        Input: POST /chat/stop with an empty body while the session holds a
        conversation id; cancel finds no running job.
        Expected: {success, stopped: False}; cancel is attempted on the session
        conversation; no status is recorded.
        """
        # Arrange
        self.jobs.request_cancel.return_value = False
        with self.client.session_transaction() as sess:
            sess["feedback_conversation_id"] = "session-conv"

        # Act
        resp = self.client.post("/bt_chatbot/chat/stop", json={})

        # Assert
        self.assertEqual(resp.get_json(), {"success": True, "stopped": False})
        self.jobs.request_cancel.assert_called_once_with("session-conv")
        self.gather.record_turn_status.assert_not_called()

    def test_stop_without_any_conversation_is_noop(self):
        """Stop with no conversation anywhere is a no-op.

        Input: POST /chat/stop with no body and no session conversation.
        Expected: {success, stopped: False}; cancel is never attempted.
        """
        # Act
        resp = self.client.post("/bt_chatbot/chat/stop")

        # Assert
        self.assertEqual(resp.get_json(), {"success": True, "stopped": False})
        self.jobs.request_cancel.assert_not_called()

    def test_stop_failure_returns_500(self):
        """A stop failure returns 500.

        Input: POST /chat/stop where the job registry lookup raises.
        Expected: 500 echoing the error message.
        """
        # Arrange
        self.jobs.get_job.side_effect = RuntimeError("registry broken")

        # Act
        resp = self.client.post("/bt_chatbot/chat/stop", json={"conversation_id": CONV_ID})

        # Assert
        self.assertEqual(resp.status_code, 500)
        self.assertEqual(resp.get_json()["error"], "registry broken")


# ======================================================================
# History endpoints
# ======================================================================
class TestHistoryEndpoints(BtChatbotTestCase):

    def setUp(self):
        super().setUp()
        # History endpoints are shared handlers using shared_routes-level services.
        self.history = self.patch_target("services.chatbot.shared_routes.history_service")
        self.jobs = self.patch_target("services.chatbot.shared_routes.chat_jobs")

    def test_history_list_merges_running_jobs_and_orders_groups(self):
        """The history list merges running jobs and orders pinned/new/old groups.

        Input: GET /history/list with two stored conversations (one pinned) and
        two active jobs (one overlapping a stored conv, one brand new).
        Expected: BT-scoped list/summary queries; order is pinned, new running,
        then old; overlapping convs are flagged running and new jobs are merged
        in with their title/turn count.
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
        body = self.client.get("/bt_chatbot/history/list").get_json()

        # Assert
        self.history.list_conversations.assert_called_once_with(domain="bt")
        self.jobs.active_summaries.assert_called_once_with(domain="bt")
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
        resp = self.client.get("/bt_chatbot/history/list")

        # Assert
        self.assertEqual(resp.status_code, 500)
        self.assertEqual(resp.get_json(), {"success": False, "error": "disk"})

    def test_history_stream_without_job_emits_idle(self):
        """Streaming a conversation with no live job emits idle.

        Input: GET /history/stream?conversation_id=... where no job exists.
        Expected: a single SSE "idle" event with a no-cache header.
        """
        # Arrange
        self.jobs.get_job.return_value = None

        # Act
        resp = self.client.get(f"/bt_chatbot/history/stream?conversation_id={CONV_ID}")

        # Assert
        self.assertEqual(sse_events(resp), [{"type": "idle"}])
        self.assertEqual(resp.headers["Cache-Control"], "no-cache")

    def test_history_stream_rejects_wifi_domain_job(self):
        """Streaming rejects a job from the wifi domain.

        Input: GET /history/stream where the matching job has domain="wifi".
        Expected: a single "idle" event; the shared registry is never subscribed.
        """
        # Arrange
        self.jobs.get_job.return_value = MagicMock(domain="wifi")

        # Act
        resp = self.client.get(f"/bt_chatbot/history/stream?conversation_id={CONV_ID}")

        # Assert
        self.assertEqual(sse_events(resp), [{"type": "idle"}])

    def test_history_stream_follows_live_bt_job_until_terminal(self):
        """Streaming follows a live BT job, replaying then live events.

        Input: GET /history/stream for a BT job whose live queue yields a step
        then an error, with one replayed step buffered.
        Expected: SSE events are the replayed step, the live step, then the error;
        the subscription is cleaned up afterward.
        """
        # Arrange: job_sse (real) resolves subscribe/unsubscribe in job_runtime.
        job = MagicMock(domain="bt", turn_id="t-2", conversation_id=CONV_ID)
        live = queue.Queue()
        live.put(("step", {"text": "live step"}))
        live.put(("error", "agent crashed"))
        self.jobs.get_job.return_value = job
        self.patch_target("services.chatbot.job_runtime.subscribe",
                          return_value=(live, [{"text": "replayed"}], None))
        unsubscribe = self.patch_target("services.chatbot.job_runtime.unsubscribe")

        # Act
        events = sse_events(self.client.get(f"/bt_chatbot/history/stream?conversation_id={CONV_ID}"))

        # Assert
        self.assertEqual(events, [
            {"type": "step", "step": {"text": "replayed"}},
            {"type": "step", "step": {"text": "live step"}},
            {"type": "error", "content": "agent crashed"},
        ])
        unsubscribe.assert_called_once_with(job, live)

    def test_history_get_requires_conversation_id(self):
        """history/get requires a conversation_id.

        Input: GET /history/get with no conversation_id.
        Expected: 400; the conversation is never fetched.
        """
        # Act
        resp = self.client.get("/bt_chatbot/history/get")

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
        resp = self.client.get(f"/bt_chatbot/history/get?conversation_id={CONV_ID}")

        # Assert
        self.assertEqual(resp.status_code, 404)
        self.assertEqual(resp.get_json(), {"success": False, "error": "Conversation not found"})

    def test_history_get_returns_bt_conversation_with_steps(self):
        """history/get returns the BT conversation including steps.

        Input: GET /history/get with a known conversation_id.
        Expected: {success, conversation}; get_conversation is called BT-scoped
        with with_steps=True.
        """
        # Arrange
        self.history.get_conversation.return_value = {"conversation_id": CONV_ID, "turns": []}

        # Act
        body = self.client.get(f"/bt_chatbot/history/get?conversation_id={CONV_ID}").get_json()

        # Assert
        self.assertEqual(body, {"success": True, "conversation": {"conversation_id": CONV_ID, "turns": []}})
        self.history.get_conversation.assert_called_once_with(CONV_ID, domain="bt", with_steps=True)

    def test_history_delete_requires_conversation_id(self):
        """history/delete requires a conversation_id.

        Input: POST /history/delete with an empty body.
        Expected: 400; nothing is deleted.
        """
        # Act
        resp = self.client.post("/bt_chatbot/history/delete", json={})

        # Assert
        self.assertEqual(resp.status_code, 400)
        self.history.delete_conversation.assert_not_called()

    def test_history_delete_active_conversation_drops_session_pointer(self):
        """Deleting the active conversation also clears the session pointer.

        Input: POST /history/delete for the conversation currently stored in
        session; delete succeeds.
        Expected: {success: True}; BT-scoped delete runs and the session's
        feedback_conversation_id is removed.
        """
        # Arrange
        self.history.delete_conversation.return_value = True
        with self.client.session_transaction() as sess:
            sess["feedback_conversation_id"] = CONV_ID

        # Act
        body = self.client.post("/bt_chatbot/history/delete", json={"conversation_id": CONV_ID}).get_json()

        # Assert
        self.assertEqual(body, {"success": True})
        self.history.delete_conversation.assert_called_once_with(CONV_ID, domain="bt")
        self.assertNotIn("feedback_conversation_id", self.get_session())

    def test_history_delete_missing_conversation_keeps_session(self):
        """Deleting a missing conversation leaves the session pointer intact.

        Input: POST /history/delete where delete returns False.
        Expected: {success: False}; the session's conversation id is kept.
        """
        # Arrange
        self.history.delete_conversation.return_value = False
        with self.client.session_transaction() as sess:
            sess["feedback_conversation_id"] = CONV_ID

        # Act
        body = self.client.post("/bt_chatbot/history/delete", json={"conversation_id": CONV_ID}).get_json()

        # Assert
        self.assertEqual(body, {"success": False})
        self.assertEqual(self.get_session()["feedback_conversation_id"], CONV_ID)

    def test_history_rename_validates_id_and_title(self):
        """history/rename validates both id and title.

        Input: POST /history/rename missing the id, then missing a non-blank
        title.
        Expected: both return 400 with the matching required-field error; nothing
        is renamed.
        """
        # Act
        no_id = self.client.post("/bt_chatbot/history/rename", json={"title": "T"})
        no_title = self.client.post("/bt_chatbot/history/rename", json={"conversation_id": CONV_ID, "title": " "})

        # Assert
        self.assertEqual((no_id.status_code, no_id.get_json()["error"]), (400, "conversation_id is required"))
        self.assertEqual((no_title.status_code, no_title.get_json()["error"]), (400, "title is required"))
        self.history.rename_conversation.assert_not_called()

    def test_history_rename_success(self):
        """history/rename trims and applies a new title.

        Input: POST /history/rename with a padded title " New title ".
        Expected: {success: True}; rename_conversation is called BT-scoped with
        the trimmed title.
        """
        # Arrange
        self.history.rename_conversation.return_value = True

        # Act
        body = self.client.post("/bt_chatbot/history/rename",
                                json={"conversation_id": CONV_ID, "title": " New title "}).get_json()

        # Assert
        self.assertEqual(body, {"success": True})
        self.history.rename_conversation.assert_called_once_with(CONV_ID, "New title", domain="bt")

    def test_history_pin_requires_conversation_id(self):
        """history/pin requires a conversation_id.

        Input: POST /history/pin with only a pinned flag.
        Expected: 400; set_pinned is never called.
        """
        # Act
        resp = self.client.post("/bt_chatbot/history/pin", json={"pinned": True})

        # Assert
        self.assertEqual(resp.status_code, 400)
        self.history.set_pinned.assert_not_called()

    def test_history_pin_success(self):
        """history/pin coerces the pinned flag and persists it.

        Input: POST /history/pin with pinned=1.
        Expected: {success, pinned: True}; set_pinned is called BT-scoped with
        True.
        """
        # Arrange
        self.history.set_pinned.return_value = True

        # Act
        body = self.client.post("/bt_chatbot/history/pin",
                                json={"conversation_id": CONV_ID, "pinned": 1}).get_json()

        # Assert
        self.assertEqual(body, {"success": True, "pinned": True})
        self.history.set_pinned.assert_called_once_with(CONV_ID, True, domain="bt")


class TestHistoryLoad(BtChatbotTestCase):

    def setUp(self):
        super().setUp()
        self.patch_services()
        self.agent = make_agent()
        self.get_agent = self.patch_route("_get_or_create_agent", return_value=self.agent)
        self.jobs.get_job.return_value = None
        self.history.get_context.return_value = None
        self.stored = {
            "title": "Headset drop",
            "log_path": LOG_PATH,
            "issue": {"case_nbr": TEST_CASE_NBR, "subject": "BT drop", "bogus": "x", "issue_type": 7},
            "issue_time": "10/28/2025-11:25:49",
            "turns": [{"user_message": "why?", "result": {"answer": "because"}}],
        }

    def _load(self, conv_id=CONV_ID):
        return self.client.post("/bt_chatbot/history/load", json={"conversation_id": conv_id})

    def test_missing_conversation_id_returns_400(self):
        """history/load requires a conversation_id.

        Input: POST /history/load with an empty body.
        Expected: 400.
        """
        # Act
        resp = self.client.post("/bt_chatbot/history/load", json={})

        # Assert
        self.assertEqual(resp.status_code, 400)

    def test_unknown_conversation_returns_404(self):
        """history/load returns 404 for an unknown conversation.

        Input: POST /history/load where get_conversation returns None and no job
        exists.
        Expected: 404 with {success: False, error: "Conversation not found"}.
        """
        # Arrange
        self.history.get_conversation.return_value = None

        # Act
        resp = self._load()

        # Assert
        self.assertEqual(resp.status_code, 404)
        self.assertEqual(resp.get_json(), {"success": False, "error": "Conversation not found"})

    def test_wifi_job_is_not_adopted(self):
        """history/load does not adopt a wifi-domain job.

        Input: POST /history/load where the live job is wifi and no stored
        conversation exists.
        Expected: 404 (the wifi job is ignored by this BT blueprint).
        """
        # Arrange
        self.jobs.get_job.return_value = MagicMock(domain="wifi", agent=make_agent())
        self.history.get_conversation.return_value = None

        # Act
        resp = self._load()

        # Assert
        self.assertEqual(resp.status_code, 404)

    def test_stored_conversation_restores_log_issue_time_and_context(self):
        """Loading a stored conversation restores log, issue time and context.

        Input: POST /history/load for a stored BT conversation whose log exists;
        history_service returns stored agent context.
        Expected: a fresh agent is primed with the filtered issue fields, the log
        path and parsed issue_time are set, stored context is imported (not
        rebuilt from text), and the response reports title/skills/span/issue_time
        with the session pointing at the conversation and log.
        """
        # Arrange
        self.history.get_conversation.return_value = self.stored
        self.history.get_context.return_value = [{"role": "user", "content": "ctx"}]
        self.agent.import_conversation_context.return_value = 4
        self.fake_paths("exists", LOG_PATH)

        # Act
        body = self._load().get_json()

        # Assert
        self.history.get_conversation.assert_called_once_with(CONV_ID, domain="bt", with_steps=True)
        self.get_agent.assert_called_once_with(skip_prime=True)
        self.agent.reset_conversation.assert_called_once_with()
        self.assertEqual(self.agent.current_log_path, LOG_PATH)
        self.agent.prime_with_context.assert_called_once_with(case_nbr=TEST_CASE_NBR, subject="BT drop")
        self.assertEqual(self.agent.issue_time, datetime(2025, 10, 28, 11, 25, 49))
        self.history.get_context.assert_called_once_with(CONV_ID, domain="bt")
        self.assertEqual(self.agent.conversation_history, [])  # not rebuilt from text
        self.assertEqual(body["context_restored"], 4)
        self.assertEqual((body["title"], body["log_exists"], body["running"]), ("Headset drop", True, False))
        self.assertEqual((body["skills"], body["log_span_minutes"], body["issue_time"]),
                         (SKILLS, 30, "10/28/2025-11:25:49"))
        sess = self.get_session()
        self.assertEqual((sess["feedback_conversation_id"], sess["chatbot_log_path"]), (CONV_ID, LOG_PATH))

    def test_missing_log_and_no_stored_context_rebuilds_text_history(self):
        """A missing log with no stored context rebuilds history from text.

        Input: POST /history/load for a stored conversation whose log is absent
        and which has no stored agent context.
        Expected: the agent conversation is rebuilt from the turn text;
        prime_with_context is skipped; log_exists is False, skills empty, context
        restored 0, and the session has no chatbot_log_path.
        """
        # Arrange
        self.history.get_conversation.return_value = self.stored
        self.history.assistant_text_from_result.return_value = "because"
        self.fake_paths("exists")

        # Act
        body = self._load().get_json()

        # Assert
        self.assertEqual(self.agent.conversation_history,
                         [{"role": "user", "content": "why?"}, {"role": "assistant", "content": "because"}])
        self.agent.prime_with_context.assert_not_called()
        self.assertEqual((body["log_exists"], body["skills"], body["context_restored"]), (False, [], 0))
        self.assertNotIn("chatbot_log_path", self.get_session())

    def test_finished_bt_job_agent_is_adopted(self):
        """A finished BT job's agent is adopted into the session.

        Input: POST /history/load where a finished BT job carries an agent with a
        log and issue time, and no stored conversation exists.
        Expected: no new agent is created; the job agent is bound to the session
        slot; no stored context is fetched and no re-priming occurs; the response
        reports the job title, log path, running=False and formatted issue_time.
        """
        # Arrange
        job_agent = make_agent(current_log_path=LOG_PATH, issue_time=datetime(2026, 4, 8, 0, 5, 0))
        self.jobs.get_job.return_value = MagicMock(domain="bt", status="done", agent=job_agent, title="Q")
        self.history.get_conversation.return_value = None
        self.fake_paths("exists", LOG_PATH)
        with self.client.session_transaction() as sess:
            sess["chatbot_session_id"] = "sid-7"

        # Act
        body = self._load().get_json()

        # Assert
        self.get_agent.assert_not_called()
        self.assertIs(self.routes.session_agents["sid-7"], job_agent)
        self.history.get_context.assert_not_called()
        job_agent.prime_with_context.assert_not_called()
        self.assertEqual((body["title"], body["log_path"], body["running"]), ("Q", LOG_PATH, False))
        self.assertEqual(body["issue_time"], "04/08/2026-00:05:00.000")

    def test_running_bt_job_returns_buffered_steps_without_adopting(self):
        """A running BT job returns buffered steps without adopting its agent.

        Input: POST /history/load where a running BT job has buffered steps.
        Expected: running=True with the running user message and steps returned;
        the agent is not adopted into the session and the job agent is untouched.
        """
        # Arrange
        job_agent = make_agent(current_log_path=LOG_PATH)
        job = MagicMock(domain="bt", status="running", agent=job_agent, title="Analyse", steps=[{"s": 1}])
        self.jobs.get_job.return_value = job
        self.history.get_conversation.return_value = None
        self.fake_paths("exists", LOG_PATH)

        # Act
        body = self._load().get_json()

        # Assert
        self.assertTrue(body["running"])
        self.assertEqual((body["running_user_message"], body["steps"]), ("Analyse", [{"s": 1}]))
        self.assertEqual(self.routes.session_agents, {})
        job_agent._log_has_date.assert_not_called()

    def test_agent_failure_returns_500(self):
        """An agent failure during load returns 500.

        Input: POST /history/load for a stored conversation where
        _get_or_create_agent raises.
        Expected: 500 with {success: False, error}.
        """
        # Arrange
        self.history.get_conversation.return_value = self.stored
        self.get_agent.side_effect = RuntimeError("no key")

        # Act
        resp = self._load()

        # Assert
        self.assertEqual(resp.status_code, 500)
        self.assertEqual(resp.get_json(), {"success": False, "error": "no key"})


# ======================================================================
# /back_to_avatar, /prepare
# ======================================================================
class TestBackToAvatar(BtChatbotTestCase):
    main_stub_endpoints = ("index",)

    def test_back_to_avatar_clears_session_and_redirects_to_main(self):
        """back_to_avatar clears chatbot session state and redirects to main.

        Input: GET /back_to_avatar with a populated chatbot session plus one
        unrelated key.
        Expected: 302 to main.index; the session agent is dropped, the last log
        path is cleared, all chatbot-related session keys are removed, and the
        unrelated key is preserved.
        """
        # Arrange
        self.routes.session_agents["sid-1"] = make_agent()
        self.app_config.last_analyzed_log_path = LOG_PATH
        self.seed_session(chatbot_session_id="sid-1", chatbot_log_path=LOG_PATH, classification={"x": 1},
                          _issue_ai_quick={"data": {}}, feedback_conversation_id=CONV_ID,
                          unrelated_key="kept")

        # Act
        resp = self.client.post("/bt_chatbot/back_to_avatar")

        # Assert
        self.assertEqual(resp.status_code, 302)
        self.assertTrue(resp.headers["Location"].endswith(self.main_stub_path("index")))
        self.assertNotIn("sid-1", self.routes.session_agents)
        self.assertEqual(self.app_config.last_analyzed_log_path, "")
        sess = self.get_session()
        for key in ("chatbot_session_id", "chatbot_log_path", "case_context", "classification",
                    "_issue_ai_quick", "feedback_conversation_id"):
            self.assertNotIn(key, sess)
        self.assertEqual(sess["unrelated_key"], "kept")


class TestPrepare(BtChatbotTestCase):
    ETL = r"C:\fake\bt\cap\ibtusb.etl"

    def setUp(self):
        super().setUp()
        self.patch_services()
        self.agent = make_agent()
        self.get_agent = self.patch_route("_get_or_create_agent", return_value=self.agent)
        self.ctx = {"case_nbr": TEST_CASE_NBR, "description": "headset drops"}
        self.extract = self.patch_route("_extract_issue_context", return_value=self.ctx)
        self.read_range = self.patch_route("read_log_time_range", return_value=(None, None))
        self.organize = self.patch_route("_issue_context_organized")

    def test_missing_etl_path_returns_400(self):
        """prepare requires an etl_path.

        Input: POST /prepare with an empty body.
        Expected: 400 with "etl_path is required".
        """
        # Act
        resp = self.client.post("/bt_chatbot/prepare", json={})

        # Assert
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.get_json(), {"success": False, "error": "etl_path is required"})

    def test_missing_log_file_returns_404(self):
        """prepare returns 404 when the decoded log is missing.

        Input: POST /prepare with an ETL path whose sibling .log does not exist.
        Expected: 404 naming the expected "<etl>.log"; no agent is created.
        """
        # Arrange
        self.fake_paths("exists")

        # Act
        resp = self.client.post("/bt_chatbot/prepare", json={"etl_path": self.ETL})

        # Assert
        self.assertEqual(resp.status_code, 404)
        self.assertEqual(resp.get_json()["error"], f"Log file not found: {self.ETL}.log")
        self.get_agent.assert_not_called()

    def test_direct_hci_txt_path_is_used_as_is(self):
        """A direct .hci.txt path is used without remapping.

        Input: POST /prepare with an existing LOG_PATH (.hci.txt).
        Expected: {success: True}; the path becomes the last analyzed log and the
        agent's current log; the agent is primed and the issue context organized.
        """
        # Arrange
        self.fake_paths("isfile", LOG_PATH)

        # Act
        resp = self.client.post("/bt_chatbot/prepare", json={"etl_path": LOG_PATH})

        # Assert
        self.assertEqual(resp.get_json(), {"success": True})
        self.assertEqual(self.app_config.last_analyzed_log_path, LOG_PATH)
        self.assertEqual(self.agent.current_log_path, LOG_PATH)
        self.get_agent.assert_called_once_with(skip_prime=True)
        self.agent.prime_with_context.assert_called_once_with(**self.ctx)
        self.organize.assert_called_once_with("headset drops", None, None)

    def test_etl_path_maps_to_sibling_log_and_rotates_conversation(self):
        """An ETL path maps to its sibling .log and rotates the conversation.

        Input: POST /prepare with an ETL path whose "<etl>.log" exists, while the
        session holds an old conversation id.
        Expected: 200; the agent uses the sibling log, a new conversation id is
        issued, and a BT conversation is ensured for it.
        """
        # Arrange
        self.fake_paths("isfile", self.ETL, self.ETL + ".log")
        with self.client.session_transaction() as sess:
            sess["feedback_conversation_id"] = "old"

        # Act
        resp = self.client.post("/bt_chatbot/prepare", json={"etl_path": self.ETL})

        # Assert
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self.agent.current_log_path, self.ETL + ".log")
        new_id = self.get_session()["feedback_conversation_id"]
        self.assertNotEqual(new_id, "old")
        self.feedback.ensure_conversation.assert_called_once_with(
            conversation_id=new_id, session_id="", issue=self.ctx, log_path=self.ETL + ".log", domain="bt")

    def test_organize_failure_does_not_fail_prepare(self):
        """An issue-organize failure does not fail prepare.

        Input: POST /prepare with an existing log where read_log_time_range
        raises.
        Expected: {success: True}; _issue_context_organized is skipped.
        """
        # Arrange
        self.fake_paths("isfile", LOG_PATH)
        self.read_range.side_effect = OSError("locked")

        # Act
        resp = self.client.post("/bt_chatbot/prepare", json={"etl_path": LOG_PATH})

        # Assert
        self.assertEqual(resp.get_json(), {"success": True})
        self.organize.assert_not_called()

    def test_context_failure_returns_500(self):
        """A context-extraction failure returns 500.

        Input: POST /prepare with an existing log where _extract_issue_context
        raises.
        Expected: 500 with {success: False, error}.
        """
        # Arrange
        self.fake_paths("isfile", LOG_PATH)
        self.extract.side_effect = RuntimeError("bad session")

        # Act
        resp = self.client.post("/bt_chatbot/prepare", json={"etl_path": LOG_PATH})

        # Assert
        self.assertEqual(resp.status_code, 500)
        self.assertEqual(resp.get_json(), {"success": False, "error": "bad session"})


# ======================================================================
# Skill (re)loading endpoints
# ======================================================================
class TestSkillLoading(BtChatbotTestCase):
    NEW_SKILLS = {"A2DP": {}, "HFP": {}}

    def setUp(self):
        super().setUp()
        self.agent = self.use_base_agent(make_agent())
        self.app_config.bt_chatbot_agent.skills = None
        self.app_config.llm_helper = MagicMock(name="llm_helper")
        self.from_yaml = MagicMock(return_value=self.NEW_SKILLS)
        self.override_shared_ctx(load_skills_from_yaml=self.from_yaml)

    def test_load_skills_yaml_requires_path(self):
        """load_skills_yaml requires a yaml_path.

        Input: POST /load_skills_yaml with an empty yaml_path.
        Expected: 400 with "yaml_path is required."; the loader is not called.
        """
        # Act
        resp = self.client.post("/bt_chatbot/load_skills_yaml", json={"yaml_path": ""})

        # Assert
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.get_json()["error"], "yaml_path is required.")
        self.from_yaml.assert_not_called()

    def test_load_skills_yaml_success(self):
        """load_skills_yaml loads and applies skills from a YAML file.

        Input: POST /load_skills_yaml with a YAML path.
        Expected: skills are loaded from the file and applied to the agent; the
        response reports the count, source and agent skills.
        """
        # Act
        body = self.client.post("/bt_chatbot/load_skills_yaml",
                                json={"yaml_path": r"C:\fake\bt.yaml"}).get_json()

        # Assert
        self.from_yaml.assert_called_once_with(r"C:\fake\bt.yaml")
        self.agent.apply_updated_skills.assert_called_once_with(self.NEW_SKILLS)
        self.assertEqual(body, {"success": True, "message": "2 skills loaded from YAML",
                                "source": r"C:\fake\bt.yaml", "skills": SKILLS})

    def test_load_skills_yaml_failure_returns_500(self):
        """A load_skills_yaml parse failure returns 500.

        Input: POST /load_skills_yaml where the loader raises ValueError.
        Expected: 500 echoing the error.
        """
        # Arrange
        self.from_yaml.side_effect = ValueError("bad yaml")

        # Act
        resp = self.client.post("/bt_chatbot/load_skills_yaml", json={"yaml_path": r"C:\fake\bt.yaml"})

        # Assert
        self.assertEqual(resp.status_code, 500)
        self.assertEqual(resp.get_json()["error"], "bad yaml")

    def test_reload_from_shared_not_found_returns_400(self):
        """reload_from_shared returns 400 when no shared YAML exists.

        Input: POST /reload_from_shared where get_load_path returns None.
        Expected: 400 with "Shared YAML not found"; the loader is not called.
        """
        # Arrange
        self.patch_target("utils.helpers.get_load_path", return_value=None)

        # Act
        resp = self.client.post("/bt_chatbot/reload_from_shared")

        # Assert
        self.assertEqual(resp.status_code, 400)
        self.assertIn("Shared YAML not found", resp.get_json()["error"])
        self.from_yaml.assert_not_called()

    def test_reload_from_shared_success(self):
        """reload_from_shared reloads skills from the shared folder.

        Input: POST /reload_from_shared where get_load_path returns a file.
        Expected: skills are loaded from it and applied; the response reports the
        reloaded count and source.
        """
        # Arrange: any existing file stands in for the shared YAML (read-only)
        shared = os.path.abspath(__file__)
        self.patch_target("utils.helpers.get_load_path", return_value=shared)

        # Act
        body = self.client.post("/bt_chatbot/reload_from_shared").get_json()

        # Assert
        self.from_yaml.assert_called_once_with(shared)
        self.agent.apply_updated_skills.assert_called_once_with(self.NEW_SKILLS)
        self.assertEqual(body["message"], "2 skills reloaded from shared folder")
        self.assertEqual(body["source"], shared)

    def test_reload_from_shared_failure_returns_500(self):
        """A reload_from_shared failure returns 500.

        Input: POST /reload_from_shared where get_load_path raises TimeoutError.
        Expected: 500 echoing the error.
        """
        # Arrange
        self.patch_target("utils.helpers.get_load_path", side_effect=TimeoutError("share offline"))

        # Act
        resp = self.client.post("/bt_chatbot/reload_from_shared")

        # Assert
        self.assertEqual(resp.status_code, 500)
        self.assertEqual(resp.get_json()["error"], "share offline")

    def test_get_skills_returns_agent_skills(self):
        """GET /skills returns the agent's current skills.

        Input: GET /bt_chatbot/skills.
        Expected: {success, skills} with the agent skill descriptions.
        """
        # Act
        body = self.client.get("/bt_chatbot/skills").get_json()

        # Assert
        self.assertEqual(body, {"success": True, "skills": SKILLS})

    def test_get_skills_failure_returns_500(self):
        """A GET /skills failure returns 500.

        Input: GET /skills where the agent cannot be built.
        Expected: 500.
        """
        # Arrange: no base agent and no usable llm client -> agent build raises
        self.app_config.bt_chatbot_agent = None
        self.app_config.llm_helper = MagicMock(client=None)

        # Act
        resp = self.client.get("/bt_chatbot/skills")

        # Assert
        self.assertEqual(resp.status_code, 500)


class TestAppendSkillsYaml(BtChatbotTestCase):
    TARGET = Path(r"C:\fake\user\bt_skills_2026-10-03.yaml")

    def setUp(self):
        super().setUp()
        self.load_filter = self.patch_target("utils.skill_import_utils.load_and_filter_source")
        self.merge = self.patch_target("utils.skill_import_utils.merge_overwrite")
        self.latest_user = MagicMock(return_value=(None, None))
        self.latest_cloud = MagicMock(return_value=(None, None))
        self.read_yaml = MagicMock(return_value={"A2DP": {}})
        self.persist = MagicMock(return_value=self.TARGET)
        self.set_source = MagicMock()
        self.activate = MagicMock(return_value=SKILLS)
        self.override_skill_append_ctx(
            latest_user_yaml=self.latest_user,
            latest_cloud_baseline=self.latest_cloud,
            read_yaml_file=self.read_yaml,
            persist_user_yaml_snapshot=self.persist,
            set_active_source=self.set_source,
            activate_yaml=self.activate,
        )

    def _post(self, path=r"C:\in\import.yaml"):
        return self.client.post("/bt_chatbot/append_skills_yaml", json={"yaml_path": path})

    def test_requires_yaml_path(self):
        """append_skills_yaml requires a yaml_path.

        Input: POST /append_skills_yaml with an empty yaml_path.
        Expected: 400; the source loader is not called.
        """
        # Act
        resp = self._post("")

        # Assert
        self.assertEqual(resp.status_code, 400)
        self.load_filter.assert_not_called()

    def test_missing_source_file_returns_404(self):
        """append_skills_yaml returns 404 for a missing source file.

        Input: POST /append_skills_yaml where load_and_filter_source raises
        FileNotFoundError.
        Expected: 404 echoing the error.
        """
        # Arrange
        self.load_filter.side_effect = FileNotFoundError("no such file")

        # Act
        resp = self._post()

        # Assert
        self.assertEqual(resp.status_code, 404)
        self.assertEqual(resp.get_json()["error"], "no such file")

    def test_unparseable_source_returns_400(self):
        """append_skills_yaml returns 400 for an unparseable source.

        Input: POST /append_skills_yaml where load_and_filter_source raises
        ValueError.
        Expected: 400 with a "Cannot parse YAML: ..." message.
        """
        # Arrange
        self.load_filter.side_effect = ValueError("mapping expected")

        # Act
        resp = self._post()

        # Assert
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.get_json()["error"], "Cannot parse YAML: mapping expected")

    def test_no_importable_skills_returns_400_with_skipped(self):
        """append_skills_yaml returns 400 when nothing is importable.

        Input: POST /append_skills_yaml where the source yields no valid skills,
        only skipped ones.
        Expected: 400 echoing the skipped map; nothing is persisted.
        """
        # Arrange
        self.load_filter.return_value = ({}, {"Bad": "missing name"})

        # Act
        resp = self._post()

        # Assert
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.get_json()["skipped"], {"Bad": "missing name"})
        self.persist.assert_not_called()

    def test_merges_into_cloud_baseline_when_no_user_file(self):
        """append_skills_yaml merges into the cloud baseline when no user file.

        Input: POST /append_skills_yaml with one importable skill; no user YAML
        exists but a cloud baseline does.
        Expected: the import is merged onto the cloud baseline, persisted as a
        user snapshot, the active source switches to "user", the new file is
        activated, and the session records the modified path.
        """
        # Arrange
        cloud = Path(r"C:\fake\cloud\bt_skills_2026-09-01.yaml")
        self.latest_cloud.return_value = (cloud, date(2026, 9, 1))
        self.load_filter.return_value = ({"HFP": {"name": "HFP"}}, {"Bad": "x"})
        self.merge.return_value = ({"A2DP": {}, "HFP": {}}, ["HFP"], [])

        # Act
        body = self._post().get_json()

        # Assert
        self.read_yaml.assert_called_once_with(cloud)
        self.merge.assert_called_once_with({"A2DP": {}}, {"HFP": {"name": "HFP"}})
        self.persist.assert_called_once_with({"A2DP": {}, "HFP": {}})
        self.set_source.assert_called_once_with("user")
        self.activate.assert_called_once_with(self.TARGET)
        self.assertEqual(body["message"], "Imported from import.yaml: appended 1, skipped 1.")
        self.assertEqual((body["filename"], body["active_source"], body["skills"]),
                         (self.TARGET.name, "user", SKILLS))
        sess = self.get_session()
        self.assertTrue(sess["yaml_modified"])
        self.assertEqual(sess["yaml_modified_path"], str(self.TARGET))

    def test_no_base_file_merges_into_empty_dict(self):
        """append_skills_yaml merges into an empty base when nothing exists.

        Input: POST /append_skills_yaml with one importable skill and no user or
        cloud base file.
        Expected: the merge uses an empty dict base (no file read); the response
        reports the overwritten count.
        """
        # Arrange
        self.load_filter.return_value = ({"HFP": {}}, {})
        self.merge.return_value = ({"HFP": {}}, [], ["HFP"])

        # Act
        body = self._post().get_json()

        # Assert
        self.read_yaml.assert_not_called()
        self.merge.assert_called_once_with({}, {"HFP": {}})
        self.assertEqual(body["message"], "Imported from import.yaml: overwrote 1.")

    def test_persist_failure_returns_500(self):
        """A persist failure in append_skills_yaml returns 500.

        Input: POST /append_skills_yaml where _persist_user_yaml_snapshot raises
        PermissionError.
        Expected: 500 echoing the error.
        """
        # Arrange
        self.load_filter.return_value = ({"HFP": {}}, {})
        self.merge.return_value = ({"HFP": {}}, ["HFP"], [])
        self.persist.side_effect = PermissionError("read-only")

        # Act
        resp = self._post()

        # Assert
        self.assertEqual(resp.status_code, 500)
        self.assertEqual(resp.get_json()["error"], "read-only")


# ======================================================================
# /get_issue_context
# ======================================================================
class TestGetIssueContext(BtChatbotTestCase):

    def setUp(self):
        super().setUp()
        self.extract = self.patch_route("_extract_issue_context", return_value={
            "subject": "[BT] Headset drops", "description": "raw desc", "attachment_time": "04/08/2026-00:10:00"})
        self.read_range = self.patch_route("read_log_time_range", return_value=(None, None))
        self.organized = self.patch_route("_issue_context_organized")
        self.resolved = self.patch_route("_resolved_issue_time_for", return_value="04/08/2026-00:10:00.000")
        # Keep auto-filled times unchanged so assertions read the source values
        # rather than a realigned reformat (BT is not a customer-timezone profile).
        self.patch_route("realign_times_to_log", side_effect=lambda times, *a, **k: list(times))

    def test_returns_organized_description_and_first_issue_time(self):
        """get_issue_context returns the organized description and first time.

        Input: GET /get_issue_context with a loaded log and organized context
        yielding a clean description and two issue times.
        Expected: the log range is read and context organized; the response
        carries the clean description, the attachment time, the first issue time,
        all issue times, and the interpretation (no resolved-time fallback).
        """
        # Arrange
        self.organized.return_value = {"clean_description": "Headset drops during call",
                                       "issue_times": ["04/08/2026-00:12:00", "04/08/2026-00:20:00"],
                                       "interpretation": "two drops"}
        with self.client.session_transaction() as sess:
            sess["chatbot_log_path"] = LOG_PATH

        # Act
        body = self.client.get("/bt_chatbot/get_issue_context").get_json()

        # Assert
        self.read_range.assert_any_call(LOG_PATH)
        self.organized.assert_called_once_with("raw desc", None, None, "")
        self.resolved.assert_not_called()
        self.assertEqual(body["description"], "Headset drops during call")
        self.assertEqual(body["attachment_time"], "04/08/2026-00:10:00")
        self.assertEqual(body["issue_time"], "04/08/2026-00:12:00")
        self.assertEqual(body["issue_times"], ["04/08/2026-00:12:00", "04/08/2026-00:20:00"])
        self.assertEqual(body["interpretation"], "two drops")
        self.assertFalse(body["issue_time_blocked"])

    def test_without_organized_times_falls_back_to_resolved_time_and_composer(self):
        """get_issue_context falls back to resolved time and composed description.

        Input: GET /get_issue_context where organized context has no times; the
        last analyzed log is used for resolution.
        Expected: the resolved issue time is computed and returned, the
        description is composed around it, and interpretation is empty.
        """
        # Arrange
        self.organized.return_value = {"clean_description": "", "issue_times": []}
        self.app_config.last_analyzed_log_path = OTHER_LOG

        # Act
        body = self.client.get("/bt_chatbot/get_issue_context").get_json()

        # Assert
        self.resolved.assert_called_once_with(OTHER_LOG, "04/08/2026-00:10:00")
        self.assertEqual(body["issue_time"], "04/08/2026-00:10:00.000")
        self.assertEqual(body["description"], "Headset drops at around 04/08/2026-00:10:00")
        self.assertEqual(body["interpretation"], "")

    def test_context_failure_and_no_log_still_return_payload(self):
        """get_issue_context still returns a payload on failure with no log.

        Input: GET /get_issue_context where _extract_issue_context raises, there
        is no log, and resolution yields nothing.
        Expected: no log range read; context organized on empty input; empty
        attachment_time and a default "Perform full multi-skill log analysis"
        description.
        """
        # Arrange
        self.extract.side_effect = RuntimeError("no session")
        self.organized.return_value = {"clean_description": "", "issue_times": []}
        self.resolved.return_value = ""

        # Act
        body = self.client.get("/bt_chatbot/get_issue_context").get_json()

        # Assert
        self.read_range.assert_not_called()
        self.organized.assert_called_once_with("", None, None, "")
        self.assertEqual(body["attachment_time"], "")
        self.assertEqual(body["description"], "Perform full multi-skill log analysis")


# ======================================================================
# Skills YAML lifecycle endpoints
# ======================================================================
class TestSkillsYamlLifecycle(BtChatbotTestCase):
    CLOUD = Path(r"C:\fake\cloud\bt_skills_2026-09-01.yaml")
    USER = Path(r"C:\fake\user\bt_skills_2026-10-01.yaml")
    TODAY = Path(r"C:\fake\user\bt_skills_2026-10-03.yaml")

    def setUp(self):
        super().setUp()
        self.agent = make_agent()
        # The skill-editor endpoints are built from a frozen SkillEditorContext
        # bound at import time; override its callables rather than the (now gone)
        # routes-module helpers the old tests patched.
        self.get_agent = MagicMock(return_value=self.agent)
        self.status = MagicMock()
        self.latest_cloud = MagicMock(return_value=(None, None))
        self.latest_user = MagicMock(return_value=(None, None))
        self.set_source = MagicMock()
        self.get_source = MagicMock(return_value="cloud")
        self.activate = MagicMock(return_value=SKILLS)
        self.refresh = MagicMock()
        self.read_yaml = MagicMock()
        self.persist = MagicMock(return_value=self.TODAY)
        self.override_skill_editor_ctx(
            get_or_create_agent=self.get_agent,
            skills_yaml_status_payload=self.status,
            latest_cloud_baseline=self.latest_cloud,
            latest_user_yaml=self.latest_user,
            set_active_source=self.set_source,
            get_active_source=self.get_source,
            activate_yaml=self.activate,
            refresh_cloud_baseline=self.refresh,
            read_yaml_file=self.read_yaml,
            persist_user_yaml_snapshot=self.persist,
        )

    # ---- /skills_yaml_status
    def test_status_returns_payload(self):
        """skills_yaml_status returns {success, **payload}.

        Input: GET /skills_yaml_status.
        Expected: the status payload fields merged under success=True. The
        refactored handler no longer attaches agent skills.
        """
        # Arrange
        self.status.return_value = {"active_source": "cloud", "effective_source": "cloud"}

        # Act
        body = self.client.get("/bt_chatbot/skills_yaml_status").get_json()

        # Assert
        self.assertEqual(body, {"success": True, "active_source": "cloud",
                                "effective_source": "cloud"})

    def test_status_failure_returns_500(self):
        """A skills_yaml_status failure returns 500.

        Input: GET /skills_yaml_status where the status payload raises OSError.
        Expected: 500.
        """
        # Arrange
        self.status.side_effect = OSError("share")

        # Act
        resp = self.client.get("/bt_chatbot/skills_yaml_status")

        # Assert
        self.assertEqual(resp.status_code, 500)

    # ---- /skills_yaml_use_cloud
    def test_use_cloud_without_baseline_returns_404(self):
        """skills_yaml_use_cloud returns 404 without a cloud baseline.

        Input: POST /skills_yaml_use_cloud with no cloud baseline present.
        Expected: 404 with "No cloud baseline found"; the source is not switched.
        """
        # Act
        resp = self.client.post("/bt_chatbot/skills_yaml_use_cloud")

        # Assert
        self.assertEqual(resp.status_code, 404)
        self.assertIn("No cloud baseline found", resp.get_json()["error"])
        self.set_source.assert_not_called()

    def test_use_cloud_switches_source_and_activates(self):
        """skills_yaml_use_cloud switches to the cloud source and activates it.

        Input: POST /skills_yaml_use_cloud with a cloud baseline present.
        Expected: the source is set to "cloud" and the baseline activated; the
        response reports source, local date, filename and skills.
        """
        # Arrange
        self.latest_cloud.return_value = (self.CLOUD, date(2026, 9, 1))

        # Act
        body = self.client.post("/bt_chatbot/skills_yaml_use_cloud").get_json()

        # Assert
        self.set_source.assert_called_once_with("cloud")
        self.activate.assert_called_once_with(self.CLOUD)
        self.assertEqual((body["active_source"], body["local_date"], body["filename"], body["skills"]),
                         ("cloud", "2026-09-01", self.CLOUD.name, SKILLS))

    def test_use_cloud_failure_returns_500(self):
        """A skills_yaml_use_cloud failure returns 500.

        Input: POST /skills_yaml_use_cloud where the baseline lookup raises.
        Expected: 500.
        """
        # Arrange
        self.latest_cloud.side_effect = OSError("x")

        # Act / Assert
        self.assertEqual(self.client.post("/bt_chatbot/skills_yaml_use_cloud").status_code, 500)

    # ---- /skills_yaml_use_user
    def test_use_user_without_overrides_returns_404(self):
        """skills_yaml_use_user returns 404 without a user override.

        Input: POST /skills_yaml_use_user with no user YAML present.
        Expected: 404 with "No customised configuration found yet".
        """
        # Act
        resp = self.client.post("/bt_chatbot/skills_yaml_use_user")

        # Assert
        self.assertEqual(resp.status_code, 404)
        self.assertIn("No customised configuration found yet", resp.get_json()["error"])

    def test_use_user_switches_source_and_activates(self):
        """skills_yaml_use_user switches to the user source and activates it.

        Input: POST /skills_yaml_use_user with a user YAML present.
        Expected: the source is set to "user" and the user file activated; the
        response reports source, local date and filename.
        """
        # Arrange
        self.latest_user.return_value = (self.USER, None)

        # Act
        body = self.client.post("/bt_chatbot/skills_yaml_use_user").get_json()

        # Assert
        self.set_source.assert_called_once_with("user")
        self.activate.assert_called_once_with(self.USER)
        self.assertEqual((body["active_source"], body["local_date"], body["filename"]),
                         ("user", None, self.USER.name))

    def test_use_user_failure_returns_500(self):
        """A skills_yaml_use_user failure returns 500.

        Input: POST /skills_yaml_use_user where the user lookup raises.
        Expected: 500.
        """
        # Arrange
        self.latest_user.side_effect = OSError("x")

        # Act / Assert
        self.assertEqual(self.client.post("/bt_chatbot/skills_yaml_use_user").status_code, 500)

    # ---- /refresh_cloud_baseline
    def test_refresh_unreachable_share_returns_503(self):
        """refresh_cloud_baseline returns 503 when the share is unreachable.

        Input: POST /refresh_cloud_baseline where refresh returns (None, None).
        Expected: 503 asking the user to retry on VPN.
        """
        # Arrange
        self.refresh.return_value = (None, None)

        # Act
        resp = self.client.post("/bt_chatbot/refresh_cloud_baseline")

        # Assert
        self.assertEqual(resp.status_code, 503)
        self.assertEqual(resp.get_json()["error"], "Share folder is unreachable; please retry on VPN.")

    def test_refresh_reactivates_when_cloud_is_active(self):
        """refresh_cloud_baseline reactivates when cloud is the active source.

        Input: POST /refresh_cloud_baseline that fetches a new baseline while the
        active source is cloud.
        Expected: the new baseline is activated; the response reports the
        filename, date and skills.
        """
        # Arrange
        self.refresh.return_value = (self.CLOUD, date(2026, 9, 1))

        # Act
        body = self.client.post("/bt_chatbot/refresh_cloud_baseline").get_json()

        # Assert
        self.activate.assert_called_once_with(self.CLOUD)
        self.assertEqual((body["filename"], body["local_date"], body["skills"]),
                         (self.CLOUD.name, "2026-09-01", SKILLS))

    def test_refresh_does_not_reactivate_when_user_is_active(self):
        """refresh_cloud_baseline does not reactivate when user is active.

        Input: POST /refresh_cloud_baseline that fetches a baseline while the
        active source is user.
        Expected: the baseline is not activated; the response is still success.
        """
        # Arrange
        self.refresh.return_value = (self.CLOUD, None)
        self.get_source.return_value = "user"

        # Act
        body = self.client.post("/bt_chatbot/refresh_cloud_baseline").get_json()

        # Assert
        self.activate.assert_not_called()
        self.assertTrue(body["success"])

    def test_refresh_failure_returns_500(self):
        """A refresh_cloud_baseline failure returns 500.

        Input: POST /refresh_cloud_baseline where refresh raises.
        Expected: 500.
        """
        # Arrange
        self.refresh.side_effect = OSError("x")

        # Act / Assert
        self.assertEqual(self.client.post("/bt_chatbot/refresh_cloud_baseline").status_code, 500)

    # ---- /load_local_skills_yaml
    def test_load_local_user_missing_returns_404(self):
        """load_local_skills_yaml returns 404 for a missing user source.

        Input: GET /load_local_skills_yaml?source=user with no user YAML.
        Expected: 404 with "No customised configuration on disk yet.".
        """
        # Act
        resp = self.client.get("/bt_chatbot/load_local_skills_yaml?source=user")

        # Assert
        self.assertEqual(resp.status_code, 404)
        self.assertEqual(resp.get_json(), {"success": False, "error": "No customised configuration on disk yet.",
                                           "source": "user"})

    def test_load_local_cloud_missing_returns_404(self):
        """load_local_skills_yaml returns 404 for a missing cloud source.

        Input: GET /load_local_skills_yaml?source=cloud with no local baseline.
        Expected: 404 with "Cloud baseline not present locally".
        """
        # Act
        resp = self.client.get("/bt_chatbot/load_local_skills_yaml?source=cloud")

        # Assert
        self.assertEqual(resp.status_code, 404)
        self.assertIn("Cloud baseline not present locally", resp.get_json()["error"])

    def test_load_local_defaults_to_active_source(self):
        """load_local_skills_yaml defaults to the active source.

        Input: GET /load_local_skills_yaml with no source param while the active
        source is cloud and a baseline exists.
        Expected: the active source is consulted, the cloud file read, and the
        response reports source, path, date, filename and skills.
        """
        # Arrange
        self.latest_cloud.return_value = (self.CLOUD, date(2026, 9, 1))
        self.read_yaml.return_value = {"A2DP": {"name": "A2DP"}}

        # Act
        body = self.client.get("/bt_chatbot/load_local_skills_yaml").get_json()

        # Assert
        self.get_source.assert_called_once_with()
        self.read_yaml.assert_called_once_with(self.CLOUD)
        self.assertEqual(body, {"success": True, "source": "cloud", "local_path": str(self.CLOUD),
                                "local_date": "2026-09-01", "filename": self.CLOUD.name,
                                "skills": {"A2DP": {"name": "A2DP"}}})

    def test_load_local_unknown_source_is_treated_as_cloud(self):
        """load_local_skills_yaml treats an unknown source as cloud.

        Input: GET /load_local_skills_yaml?source=bogus.
        Expected: the source is resolved as "cloud"; the user lookup is skipped.
        """
        # Arrange
        self.latest_cloud.return_value = (self.CLOUD, None)
        self.read_yaml.return_value = {}

        # Act
        body = self.client.get("/bt_chatbot/load_local_skills_yaml?source=bogus").get_json()

        # Assert
        self.assertEqual(body["source"], "cloud")
        self.latest_user.assert_not_called()

    def test_load_local_read_failure_returns_500(self):
        """A load_local_skills_yaml read failure returns 500.

        Input: GET /load_local_skills_yaml?source=user where reading the YAML
        raises ValueError.
        Expected: 500 echoing the error.
        """
        # Arrange
        self.latest_user.return_value = (self.USER, None)
        self.read_yaml.side_effect = ValueError("Invalid YAML structure")

        # Act
        resp = self.client.get("/bt_chatbot/load_local_skills_yaml?source=user")

        # Assert
        self.assertEqual(resp.status_code, 500)
        self.assertEqual(resp.get_json()["error"], "Invalid YAML structure")

    # ---- /save_local_skills_yaml
    def test_save_invalid_payload_returns_400(self):
        """save_local_skills_yaml rejects an empty skills payload.

        Input: POST /save_local_skills_yaml with an empty skills object.
        Expected: 400 requiring a non-empty 'skills' object; nothing is persisted.
        """
        # Act
        resp = self.client.post("/bt_chatbot/save_local_skills_yaml", json={"skills": {}})

        # Assert
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.get_json()["error"], "Request body must contain a non-empty 'skills' object.")
        self.persist.assert_not_called()

    def test_save_valid_payload_persists_and_activates_user_source(self):
        """save_local_skills_yaml persists a valid payload and activates user.

        Input: POST /save_local_skills_yaml with one well-formed skill.
        Expected: the normalized skill is persisted (expert rules newline-
        terminated), the source switches to "user" and the new file is activated;
        the response reports the saved count and the session records the path.
        """
        # Arrange
        payload = {"A2DP": {"name": "A2DP", "description": "audio", "expert_rules": "1. check codec"}}

        # Act
        body = self.client.post("/bt_chatbot/save_local_skills_yaml", json={"skills": payload}).get_json()

        # Assert
        self.persist.assert_called_once_with(
            {"A2DP": {"name": "A2DP", "description": "audio", "expert_rules": "1. check codec\n"}})
        self.set_source.assert_called_once_with("user")
        self.activate.assert_called_once_with(self.TODAY)
        self.assertEqual(body["message"], f"Saved 1 skill(s) to {self.TODAY.name}.")
        self.assertEqual(self.get_session()["yaml_modified_path"], str(self.TODAY))

    def test_save_persist_failure_returns_500(self):
        """A save_local_skills_yaml persist failure returns 500.

        Input: POST /save_local_skills_yaml where persistence raises
        PermissionError.
        Expected: 500.
        """
        # Arrange
        self.persist.side_effect = PermissionError("locked")
        payload = {"A2DP": {"name": "A2DP", "description": "audio", "expert_rules": "x"}}

        # Act
        resp = self.client.post("/bt_chatbot/save_local_skills_yaml", json={"skills": payload})

        # Assert
        self.assertEqual(resp.status_code, 500)

    # ---- /delete_local_skill
    def test_delete_requires_skill_key(self):
        """delete_local_skill requires a skill_key.

        Input: POST /delete_local_skill with a blank skill_key.
        Expected: 400 with "skill_key is required.".
        """
        # Act
        resp = self.client.post("/bt_chatbot/delete_local_skill", json={"skill_key": " "})

        # Assert
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.get_json()["error"], "skill_key is required.")

    def test_delete_without_any_local_yaml_returns_404(self):
        """delete_local_skill returns 404 with no local YAML to edit.

        Input: POST /delete_local_skill with a skill_key but no user/cloud YAML.
        Expected: 404 with "No local skill YAML to edit.".
        """
        # Act
        resp = self.client.post("/bt_chatbot/delete_local_skill", json={"skill_key": "A2DP"})

        # Assert
        self.assertEqual(resp.status_code, 404)
        self.assertEqual(resp.get_json()["error"], "No local skill YAML to edit.")

    def test_delete_unknown_skill_returns_404(self):
        """delete_local_skill returns 404 for an unknown skill.

        Input: POST /delete_local_skill for "A2DP" where the user YAML only has
        "HFP".
        Expected: 404 noting the key is not present; nothing is persisted.
        """
        # Arrange
        self.latest_user.return_value = (self.USER, None)
        self.read_yaml.return_value = {"HFP": {}}

        # Act
        resp = self.client.post("/bt_chatbot/delete_local_skill", json={"skill_key": "A2DP"})

        # Assert
        self.assertEqual(resp.status_code, 404)
        self.assertIn("'A2DP' is not present", resp.get_json()["error"])
        self.persist.assert_not_called()

    def test_delete_from_cloud_baseline_writes_user_copy(self):
        """delete_local_skill from the cloud baseline writes a user copy.

        Input: POST /delete_local_skill for "A2DP" where only a cloud baseline
        exists with A2DP and HFP.
        Expected: the remaining skills are persisted as a user snapshot, the
        source switches to "user", and the session is flagged yaml_modified.
        """
        # Arrange
        self.latest_cloud.return_value = (self.CLOUD, None)
        self.read_yaml.return_value = {"A2DP": {}, "HFP": {}}

        # Act
        body = self.client.post("/bt_chatbot/delete_local_skill", json={"skill_key": "A2DP"}).get_json()

        # Assert
        self.read_yaml.assert_called_once_with(self.CLOUD)
        self.persist.assert_called_once_with({"HFP": {}})
        self.set_source.assert_called_once_with("user")
        self.assertEqual(body["message"], "Removed skill 'A2DP'.")
        self.assertTrue(self.get_session()["yaml_modified"])

    def test_delete_failure_returns_500(self):
        """A delete_local_skill failure returns 500.

        Input: POST /delete_local_skill where the user lookup raises.
        Expected: 500.
        """
        # Arrange
        self.latest_user.side_effect = OSError("x")

        # Act / Assert
        self.assertEqual(self.client.post("/bt_chatbot/delete_local_skill",
                                          json={"skill_key": "A2DP"}).status_code, 500)


class TestUploadModifiedYaml(BtChatbotTestCase):
    SHARE = r"\\fake-share\skills_config"

    def setUp(self):
        super().setUp()
        self.latest_user = MagicMock(return_value=(None, None))
        self.cloud_dir = MagicMock(return_value=self.SHARE)
        self.override_skill_editor_ctx(
            latest_user_yaml=self.latest_user,
            resolve_cloud_skills_dir=self.cloud_dir,
        )
        self.mkdir = self.patch_target("pathlib.Path.mkdir")
        self.copy2 = self.patch_target("shutil.copy2")
        self.patch_target("getpass.getuser", return_value="tester")
        # Any existing file stands in for the user's modified YAML (never written).
        self.local = os.path.abspath(__file__)

    def test_no_modified_yaml_returns_404(self):
        """upload_modified_yaml returns 404 when nothing was modified.

        Input: POST /upload_modified_yaml with no session or user YAML.
        Expected: 404 with "No customised skill YAML was found to upload."; no
        copy occurs.
        """
        # Act
        resp = self.client.post("/bt_chatbot/upload_modified_yaml")

        # Assert
        self.assertEqual(resp.status_code, 404)
        self.assertEqual(resp.get_json()["error"], "No customised skill YAML was found to upload.")
        self.copy2.assert_not_called()

    def test_unreachable_share_returns_503(self):
        """upload_modified_yaml returns 503 when the share is unreachable.

        Input: POST /upload_modified_yaml with a modified file but an empty cloud
        dir.
        Expected: 503; no copy occurs.
        """
        # Arrange
        self.cloud_dir.return_value = ""
        with self.client.session_transaction() as sess:
            sess["yaml_modified_path"] = self.local

        # Act
        resp = self.client.post("/bt_chatbot/upload_modified_yaml")

        # Assert
        self.assertEqual(resp.status_code, 503)
        self.copy2.assert_not_called()

    def test_upload_copies_into_contributions_folder(self):
        """upload_modified_yaml copies into the user_contributions folder.

        Input: POST /upload_modified_yaml with a session-modified YAML and a
        reachable share.
        Expected: the contributions folder is created and the file copied in
        under a ``<user>__<name>`` path; the response reports where it went.
        """
        # Arrange
        with self.client.session_transaction() as sess:
            sess["yaml_modified_path"] = self.local
        expected = Path(self.SHARE) / "user_contributions" / f"tester__{os.path.basename(self.local)}"

        # Act
        body = self.client.post("/bt_chatbot/upload_modified_yaml").get_json()

        # Assert
        self.mkdir.assert_called_once_with(parents=True, exist_ok=True)
        self.copy2.assert_called_once_with(self.local, str(expected))
        self.assertEqual(body["uploaded_to"], str(expected))
        self.assertTrue(body["success"])

    def test_falls_back_to_latest_user_yaml_when_session_path_missing(self):
        """upload_modified_yaml falls back to the latest user YAML.

        Input: POST /upload_modified_yaml with no session path but a latest user
        YAML available.
        Expected: the latest user YAML is used and the upload succeeds.
        """
        # Arrange
        self.latest_user.return_value = (Path(self.local), None)

        # Act
        body = self.client.post("/bt_chatbot/upload_modified_yaml").get_json()

        # Assert
        self.latest_user.assert_called_once_with()
        self.assertTrue(body["success"])

    def test_contributions_folder_creation_failure_returns_500(self):
        """A contributions-folder creation failure returns 500.

        Input: POST /upload_modified_yaml where mkdir raises OSError.
        Expected: 500 explaining the folder could not be created; no copy occurs.
        """
        # Arrange
        self.latest_user.return_value = (Path(self.local), None)
        self.mkdir.side_effect = OSError("access denied")

        # Act
        resp = self.client.post("/bt_chatbot/upload_modified_yaml")

        # Assert
        self.assertEqual(resp.status_code, 500)
        self.assertEqual(resp.get_json()["error"], "Cannot create contributions folder on share: access denied")
        self.copy2.assert_not_called()


# ======================================================================
# Core logic: agent lifecycle + issue-context helpers
# ======================================================================
class TestAgentLifecycle(BtChatbotTestCase):

    def test_no_base_agent_and_no_llm_client_raises(self):
        """_get_or_create_agent raises with no base agent and no LLM client.

        Input: calling _get_or_create_agent when both the BT base agent and the
        llm_helper client are absent.
        Expected: RuntimeError "Log Chatbot Agent is not available".
        """
        # Arrange
        self.app_config.llm_helper = MagicMock(client=None)

        # Act / Assert
        with self.app.test_request_context():
            with self.assertRaises(RuntimeError) as cm:
                self.routes._get_or_create_agent()
        self.assertIn("Log Chatbot Agent is not available", str(cm.exception))

    def test_clone_preserves_base_type_ace_runner_and_session_log(self):
        """Cloning preserves the base type, ACE runner and session log.

        Input: _get_or_create_agent twice with a base FakeAgent and a session log.
        Expected: a new FakeAgent (not the base) with the base's client/model/
        skills/ACE runner and the session log; the second call returns the same
        cached per-session instance.
        """
        # Arrange
        base = FakeAgent(client="client", model="m", skills={"A2DP": {}})
        base.ace_runner = "ace"
        self.app_config.bt_chatbot_agent = base
        extract = self.patch_route("_extract_issue_context")

        # Act
        with self.app.test_request_context():
            session["chatbot_log_path"] = LOG_PATH
            agent = self.routes._get_or_create_agent(skip_prime=True)
            again = self.routes._get_or_create_agent(skip_prime=True)
            sid = session["chatbot_session_id"]

        # Assert
        self.assertIs(type(agent), FakeAgent)
        self.assertIsNot(agent, base)
        self.assertEqual((agent.client, agent.model, agent.skills), ("client", "m", {"A2DP": {}}))
        self.assertEqual(agent.attached_ace, "ace")
        self.assertEqual(agent.current_log_path, LOG_PATH)
        self.assertIs(again, agent)
        self.assertIs(self.routes.session_agents[sid], agent)
        extract.assert_not_called()

    def test_fallback_builds_bt_agent_from_llm_helper_and_primes(self):
        """The fallback builds a BT agent from the llm_helper and primes it.

        Input: _get_or_create_agent with no base agent but a configured
        llm_helper and a last analyzed log.
        Expected: a new BT agent built from the helper client/model, with the
        last log set and primed with the extracted issue context.
        """
        # Arrange: the fallback builds the base via profile.agent_class
        original_cls = self.routes.profile.agent_class
        object.__setattr__(self.routes.profile, "agent_class", FakeAgent)
        self.addCleanup(object.__setattr__, self.routes.profile, "agent_class", original_cls)
        self.app_config.llm_helper = MagicMock(client="helper-client", model="gpt-x", skills=None)
        self.app_config.last_analyzed_log_path = OTHER_LOG
        self.patch_route("_extract_issue_context", return_value={"case_nbr": TEST_CASE_NBR, "subject": ""})

        # Act
        with self.app.test_request_context():
            agent = self.routes._get_or_create_agent()

        # Assert
        self.assertIsInstance(agent, FakeAgent)
        self.assertEqual((agent.client, agent.model), ("helper-client", "gpt-x"))
        self.assertEqual(agent.current_log_path, OTHER_LOG)
        self.assertEqual(agent.primed_with, {"case_nbr": TEST_CASE_NBR, "subject": ""})

    def test_resume_adopts_finished_job_agent(self):
        """_resume_agent_for adopts a finished job's agent.

        Input: _resume_agent_for a conversation whose job is finished.
        Expected: the job agent is returned and bound to the session slot;
        _get_or_create_agent is not called.
        """
        # Arrange
        job_agent = make_agent()
        jobs = self.patch_target("services.chatbot.session.job_runtime")
        jobs.get_job.return_value = MagicMock(agent=job_agent, status="done")
        get_agent = self.patch_route("_get_or_create_agent")

        # Act
        with self.app.test_request_context():
            agent = self.routes._resume_agent_for(CONV_ID)
            sid = session["chatbot_session_id"]

        # Assert
        self.assertIs(agent, job_agent)
        self.assertIs(self.routes.session_agents[sid], job_agent)
        get_agent.assert_not_called()

    def test_resume_running_job_falls_back_to_session_agent(self):
        """_resume_agent_for falls back to the session agent for a running job.

        Input: _resume_agent_for a conversation whose job is still running.
        Expected: the running job's agent is not adopted; the session agent from
        _get_or_create_agent is returned instead.
        """
        # Arrange
        jobs = self.patch_target("services.chatbot.session.job_runtime")
        jobs.get_job.return_value = MagicMock(agent=make_agent(), status="running")
        fresh = make_agent()
        self.patch_route("_get_or_create_agent", return_value=fresh)

        # Act
        with self.app.test_request_context():
            agent = self.routes._resume_agent_for(CONV_ID)

        # Assert
        self.assertIs(agent, fresh)

    def test_get_llm_client_model_priority(self):
        """_get_llm_client_model follows base-agent > helper > none priority.

        Input: successive configurations of bt_chatbot_agent and llm_helper.
        Expected: the base agent's client/model wins; then the helper's; then
        (None, None) when nothing is configured.
        """
        # Arrange / Act / Assert: base agent wins
        self.app_config.bt_chatbot_agent = MagicMock(client="base-client", model="base-model")
        self.assertEqual(self.routes._get_llm_client_model(), ("base-client", "base-model"))
        # llm_helper next
        self.app_config.bt_chatbot_agent = None
        self.app_config.llm_helper = MagicMock(client="helper-client", model="helper-model")
        self.assertEqual(self.routes._get_llm_client_model(), ("helper-client", "helper-model"))
        # nothing configured
        self.app_config.llm_helper = None
        self.assertEqual(self.routes._get_llm_client_model(), (None, None))


class TestIssueContextHelpers(BtChatbotTestCase):

    def test_extract_issue_context_merges_case_ai_and_attachment_time(self):
        """_extract_issue_context merges case, AI analysis and attachment time.

        Input: a session with a case context, classification, AI IPS analysis,
        and an attachment whose note carries a time.
        Expected: the returned context merges case number, subject, a composed
        description (with AI summary/symptoms), issue type, and the extracted
        attachment time; the attachment time is cached in session.
        """
        # Arrange
        extract_time = self.patch_target("services.chatbot.issue_context.extract_time_from_description",
                                         return_value=datetime(2026, 4, 8, 0, 12, 0))
        att = [["bt_logs.zip", "url", ["2026-04-08", "Issue happened around 00:12"]]]
        ctx = self.create_mock_case_context(attachment_list=att, description="BT drops")

        # Act
        with self.app.test_request_context():
            session["case_context"] = ctx
            session["classification"] = {"issue_type": "A2DP"}
            session["ai_ips_analysis"] = {"Classification": {"x": 1}, "Summary": " Headset ",
                                          "Symptoms": ["drop", "", "noise"], "Score": 3}
            result = routes._extract_issue_context()
            cached = session["_attachment_time_cache"]

        # Assert
        extract_time.assert_called_once_with("Issue happened around 00:12")
        self.assertEqual(result, {"case_nbr": TEST_CASE_NBR, "subject": "Wi-Fi disconnects after resume",
                                  "description": "BT drops\nSummary: Headset\nSymptoms: drop; noise",
                                  "issue_type": "A2DP", "attachment_time": "04/08/2026-00:12:00"})
        self.assertEqual(cached, "04/08/2026-00:12:00")

    def test_extract_issue_context_uses_cached_attachment_time(self):
        """_extract_issue_context reuses a cached attachment time.

        Input: a session already holding ``_attachment_time_cache``.
        Expected: extract_time_from_description is not called; the cached time is
        returned with empty case fields.
        """
        # Arrange
        extract_time = self.patch_target("services.chatbot.issue_context.extract_time_from_description")

        # Act
        with self.app.test_request_context():
            session["_attachment_time_cache"] = "cached-time"
            result = routes._extract_issue_context()

        # Assert
        extract_time.assert_not_called()
        self.assertEqual(result, {"case_nbr": "", "subject": "", "description": "",
                                  "issue_type": "", "attachment_time": "cached-time"})

    def test_extract_disconnect_time_formats(self):
        """_extract_disconnect_time formats recognized time phrases.

        Input: various subject/description strings with and without timestamps.
        Expected: a " at around <time>" suffix for recognized times and "" when
        none is present.
        """
        # Act / Assert
        from services.chatbot.issue_context import extract_disconnect_time
        self.assertEqual(extract_disconnect_time("", "drop at 10/28/2025-11:25:49.123 x"),
                         " at around 10/28/2025-11:25:49.123")
        self.assertEqual(extract_disconnect_time("at 2025-10-28 11:25:49"), " at around 2025-10-28 11:25:49")
        self.assertEqual(extract_disconnect_time("no time here", None), "")

    def test_compose_concise_description_variants(self):
        """_compose_concise_description handles several context shapes.

        Input: contexts with tags/(F/R) in the subject plus an attachment time,
        a time embedded in the description, and an empty context.
        Expected: tags are stripped and the attachment time preferred; otherwise
        the description time is used; an empty context yields the default
        "Perform full multi-skill log analysis".
        """
        # Act / Assert: tags + (F/R...) stripped, attachment time preferred
        self.assertEqual(routes._compose_concise_description(
            {"subject": "[BT][HP] Headset drops (F/R: 1/1)", "description": "", "attachment_time": "T1"}),
            "Headset drops at around T1")
        # time extracted from description text
        self.assertEqual(routes._compose_concise_description(
            {"subject": "", "description": "Drops at 10/28/2025 11:25:49", "attachment_time": ""}),
            "Drops at 10/28/2025 11:25:49 at around 10/28/2025 11:25:49")
        # nothing usable
        self.assertEqual(routes._compose_concise_description({}), "Perform full multi-skill log analysis")

    def test_compose_concise_description_context_failure(self):
        """_compose_concise_description tolerates a context-extraction failure.

        Input: calling it with no args where _extract_issue_context raises.
        Expected: the default "Perform full multi-skill log analysis".
        """
        # Arrange
        self.patch_route("_extract_issue_context", side_effect=RuntimeError("x"))

        # Act / Assert
        self.assertEqual(routes._compose_concise_description(), "Perform full multi-skill log analysis")

    def test_issue_context_organized_delegates_to_shared_service(self):
        """_issue_context_organized delegates to the shared issue-context service.

        Input: a call with raw text and a log range.
        Expected: it forwards to the shared organized_issue_context with the BT
        LLM client/model provider and domain="bt", returning its result.
        """
        # Arrange
        organized = self.patch_route("_organized_issue_context",
                                     return_value={"clean_description": "clean", "issue_times": []})

        # Act
        with self.app.test_request_context():
            result = self.routes._issue_context_organized("raw", "F", "L", "logp")

        # Assert
        organized.assert_called_once_with("raw", "F", "L", "logp",
                                          llm_client_model=self.routes._get_llm_client_model, domain="bt")
        self.assertEqual(result, {"clean_description": "clean", "issue_times": []})

    def test_resolved_issue_time_is_cached_per_log_path(self):
        """_resolved_issue_time_for caches the resolved time per log path.

        Input: two successive calls for the same log path.
        Expected: resolve_issue_time runs only once; both calls return the same
        formatted time.
        """
        # Arrange
        resolve = self.patch_target("services.chatbot.issue_context.resolve_issue_time",
                                    return_value=(datetime(2026, 4, 8, 0, 12, 0), "src"))

        # Act
        with self.app.test_request_context():
            first = routes._resolved_issue_time_for(LOG_PATH, "")
            second = routes._resolved_issue_time_for(LOG_PATH, "")

        # Assert
        resolve.assert_called_once_with("", LOG_PATH)
        self.assertEqual(first, "04/08/2026-00:12:00.000")
        self.assertEqual(second, first)


# ======================================================================
# Core logic: BT log-selection helpers (pure functions)
# ======================================================================
# find_best_log and its selection helpers moved to
# blueprints/log_parser/log_parser_routes.py; they are covered by the
# log_parser test suite now, not here.


# ======================================================================
# Core logic: skills-editor payload validation + disabled-comment round trip
# ======================================================================
class TestSkillPayloadHelpers(unittest.TestCase):

    def _ok(self, **row):
        base = {"name": "A2DP", "description": "audio", "expert_rules": "1. x"}
        base.update(row)
        return {"A2DP": base}

    def test_sanitise_rejects_invalid_payloads(self):
        """_sanitise_skill_payload rejects malformed skill payloads.

        Input: a series of invalid payloads (None, blank id, non-object skill,
        missing name/description/expert rules).
        Expected: each returns the matching validation error message.
        """
        # Act / Assert
        self.assertEqual(routes._sanitise_skill_payload(None)[1],
                         "Request body must contain a non-empty 'skills' object.")
        self.assertEqual(routes._sanitise_skill_payload({" ": {}})[1], "Skill ID is required.")
        self.assertEqual(routes._sanitise_skill_payload({"A": "x"})[1], "Skill 'A' must be an object.")
        self.assertEqual(routes._sanitise_skill_payload({"A": {"description": "d"}})[1],
                         "Skill 'A': display name is required.")
        self.assertEqual(routes._sanitise_skill_payload({"A": {"name": "n"}})[1],
                         "Skill 'A': description is required.")
        self.assertEqual(routes._sanitise_skill_payload({"A": {"name": "n", "description": "d"}})[1],
                         "Skill 'A': expert rules are required.")

    def test_sanitise_cleans_lists_and_drops_unknown_keys(self):
        """_sanitise_skill_payload trims list entries and drops unknown keys.

        Input: a valid skill with padded/empty keyword entries, an empty
        exclusive list and an unknown key.
        Expected: keywords are trimmed and emptied entries removed, unknown keys
        dropped, and expert_rules newline-terminated.
        """
        # Act
        cleaned, err = routes._sanitise_skill_payload(self._ok(
            keywords=[" A2DP_START  ", "", "x"], exclusive=[], unknown="drop me"))

        # Assert
        self.assertEqual(err, "")
        self.assertEqual(cleaned, {"A2DP": {"name": "A2DP", "description": "audio",
                                            "keywords": [" A2DP_START", "x"], "expert_rules": "1. x\n"}})

    def test_sanitise_structured_expert_rules_numbering(self):
        """_sanitise_skill_payload renders structured expert rules with numbering.

        Input: expert_rules given as a structured preamble + items (with prefixes,
        plain strings and blanks).
        Expected: a single numbered text block preserving explicit prefixes and
        auto-numbering the rest.
        """
        # Act
        cleaned, _ = routes._sanitise_skill_payload(self._ok(expert_rules={
            "preamble": "Check:", "items": [{"prefix": "1", "text": "a"}, {"prefix": "2-1", "text": "b"}, "c", " "]}))

        # Assert
        self.assertEqual(cleaned["A2DP"]["expert_rules"], "Check:\n1. a\n2-1. b\n2. c\n")

    def test_sanitise_list_expert_rules_are_numbered(self):
        """_sanitise_skill_payload numbers list-form expert rules.

        Input: expert_rules given as a list with a blank entry.
        Expected: non-blank entries are numbered sequentially into one text block.
        """
        # Act
        cleaned, _ = routes._sanitise_skill_payload(self._ok(expert_rules=["first", " ", "second"]))

        # Assert
        self.assertEqual(cleaned["A2DP"]["expert_rules"], "1. first\n2. second\n")

    def test_scan_and_inject_disabled_comments_round_trip(self):
        """Disabled-comment scan/inject round-trips commented YAML entries.

        Input: an original YAML with a commented-out keyword and a re-rendered
        version without it.
        Expected: _scan_disabled_comments captures the disabled entry and
        _inject_disabled_comments restores the original; empty inputs are no-ops.
        """
        # Arrange
        from services.skill_editor.yaml_service import inject_disabled_comments, scan_disabled_comments
        original = ('A2DP:\n  name: "A2DP"\n  keywords:\n    - "KW1"\n    # - "OLD"\n'
                    '  expert_rules: |\n    1. x\n')
        rendered = 'A2DP:\n  name: "A2DP"\n  keywords:\n    - "KW1"\n  expert_rules: |\n    1. x\n'

        # Act
        disabled = scan_disabled_comments(original)
        restored = inject_disabled_comments(rendered, disabled)

        # Assert
        self.assertEqual(disabled, {"A2DP": {"keywords": ["OLD"]}})
        self.assertEqual(restored, original)
        self.assertEqual(scan_disabled_comments(""), {})
        self.assertEqual(inject_disabled_comments(rendered, {}), rendered)


if __name__ == "__main__":
    unittest.main()
