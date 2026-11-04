"""Unit tests for the Wi-Fi log chatbot blueprint (blueprints/chatbot/chatbot_routes.py).

The blueprint is the Wi-Fi "Log Chatbot": a per-session WifiLogAgentSystem
that analyses a decoded .log, plus local conversation history, issue-time
helpers and a skills-YAML editor. Every collaborator is patched at its point
of use - the agent factory, chat_jobs / history_service / feedback_service /
gather_service, tkinter dialogs, the skills-YAML file helpers, os.path /
pathlib existence checks and shutil - so no LLM, GUI dialog, thread or
filesystem write is ever real.

Branch note: ``_extract_issue_context()`` builds the agent's issue context
from case_nbr / subject / description / classification / ai_ips_analysis /
attachment time only. It does NOT read ``case_context.env_detail`` (the IPS
Environment Details form that the handsfree flow ingests on this branch) -
``test_extract_issue_context_ignores_env_detail`` pins that down.
"""
import json
import queue
import unittest
from datetime import datetime, date
from pathlib import Path
from unittest.mock import ANY, MagicMock, patch

import blueprints.chatbot.chatbot_routes as routes
from blueprints.chatbot.chatbot_routes import log_chatbot_bp
from configs.chatbot_ui import LOG_CHATBOT_UI
from tests.base_test import BaseTestCase

# The three profile route modules were unified into one; the Wi-Fi handlers are
# now methods on a per-profile AgentRoutes instance built at import time.
ROUTES = "blueprints.chatbot.chatbot_routes"

LOG_PATH = r"C:\fake\logs\wifi.etl.log"
ETL_PATH = r"C:\fake\logs\wifi.etl"
SKILLS = [{"name": "Roaming", "description": "Roaming analysis"}]
ISSUE_DT = datetime(2025, 10, 28, 11, 25, 49)
LOG_FIRST = datetime(2025, 10, 28, 11, 0, 0)
LOG_LAST = datetime(2025, 10, 28, 12, 0, 0)
USER_YAML = Path(r"C:\fake\skills\user\skills_2026-10-03.yaml")
CLOUD_YAML = Path(r"C:\fake\skills\cloud\skills_2026-09-01.yaml")

# find_best_log is no longer part of a chatbot profile: it moved to
# blueprints/log_parser (POST /log_parser/find_best_log). The Wi-Fi profile
# also enables the prompt_editor capability, so it carries the /prompts/* group.
ALL_RULES = {
    "/log_chatbot/", "/log_chatbot/browse", "/log_chatbot/set_log",
    "/log_chatbot/suggest_issue_times", "/log_chatbot/chat", "/log_chatbot/reset",
    "/log_chatbot/chat/stop", "/log_chatbot/history/list", "/log_chatbot/history/stream",
    "/log_chatbot/history/get", "/log_chatbot/history/delete", "/log_chatbot/history/rename",
    "/log_chatbot/history/pin", "/log_chatbot/history/load", "/log_chatbot/back_to_avatar",
    "/log_chatbot/prepare", "/log_chatbot/browse_yaml",
    "/log_chatbot/load_skills_yaml",
    "/log_chatbot/append_skills_yaml", "/log_chatbot/reload_from_shared",
    "/log_chatbot/skills", "/log_chatbot/get_issue_context",
    "/log_chatbot/skills_yaml_status", "/log_chatbot/skills_yaml_use_cloud",
    "/log_chatbot/skills_yaml_use_user", "/log_chatbot/refresh_cloud_baseline",
    "/log_chatbot/load_local_skills_yaml", "/log_chatbot/save_local_skills_yaml",
    "/log_chatbot/delete_local_skill", "/log_chatbot/upload_modified_yaml",
    "/log_chatbot/prompts/status", "/log_chatbot/prompts/get", "/log_chatbot/prompts/save",
    "/log_chatbot/prompts/keep", "/log_chatbot/prompts/use_source", "/log_chatbot/prompts/revert",
    "/log_chatbot/prompts/refresh_shared", "/log_chatbot/prompts/upload",
}


class _SyncThread:
    """Drop-in for threading.Thread that runs the target inline on start()."""

    def __init__(self, target=None, args=(), kwargs=None, daemon=None):
        self._target, self._args, self._kwargs = target, args, kwargs or {}

    def start(self):
        self._target(*self._args, **self._kwargs)

    def join(self, timeout=None):
        pass


def make_agent(log_path=LOG_PATH):
    agent = MagicMock(name="agent")
    agent.current_log_path = log_path
    agent.issue_time = None
    agent.issue_context = {}
    agent.conversation_history = []
    agent.model = "gpt-test"
    agent.get_skill_descriptions.return_value = SKILLS
    agent.get_log_span_minutes.return_value = 30
    agent._log_has_date.return_value = True
    # No kept per-session skill file by default, so the skill-editor paths seed
    # from the latest user/cloud YAML rather than the agent's session path.
    agent._session_skill_path = None
    return agent


def sse_events(resp):
    body = resp.get_data(as_text=True)
    return [json.loads(chunk[len("data: "):]) for chunk in body.split("\n\n")
            if chunk.startswith("data: ")]


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


class _LogChatbotTestBase(BaseTestCase):
    blueprint = log_chatbot_bp
    main_stub_endpoints = ("index",)

    # Handlers are now methods on the per-profile AgentRoutes instance rather
    # than module-level functions, so these names are patched on that instance
    # (``self.routes``) instead of on the route module.
    INSTANCE_METHODS = frozenset({
        "_get_or_create_agent", "_resume_agent_for", "_issue_context_organized",
        "_get_configuration_agent", "_get_llm_client_model",
    })

    def setUp(self):
        super().setUp()
        # The blueprint's view functions are bound methods of the Wi-Fi
        # AgentRoutes instance; reach it through one to patch its per-session
        # store and its handler methods.
        self.routes = self.app.view_functions["log_chatbot.chat"].__self__
        self.start_patch(patch.dict(self.routes.session_agents, clear=True))
        self.chat_jobs = self.start_patch(patch(f"{ROUTES}.chat_jobs"))
        self.chat_jobs.get_job.return_value = None
        self.chat_jobs.active_summaries.return_value = []
        self.history_service = self.start_patch(patch(f"{ROUTES}.history_service"))
        self.history_service.get_context.return_value = None
        self.feedback_service = self.start_patch(patch(f"{ROUTES}.feedback_service"))
        self.gather_service = self.start_patch(patch(f"{ROUTES}.gather_service"))
        self.agent = make_agent()

        # IPS gating is a shared-framework dependency added in the refactor:
        # blocking_state() gates chat SSE (428), prompt_state() is merged into
        # set_log responses, start_new_session() runs on reset. Neutralise it so
        # these routes behave as they did pre-gating.
        self.mock_check_ips = self.start_patch(patch(f"{ROUTES}.check_ips_service"))
        self.mock_check_ips.blocking_state.return_value = None
        self.mock_check_ips.prompt_state.return_value = {}
        self.mock_check_ips.start_new_session.return_value = None

        # The System-Event-Log lookup now lives in utils.event_log_utils and is
        # imported into the routes module; default it to "no evtx found".
        self.mock_find_evt = self.start_patch(
            patch(f"{ROUTES}.find_event_log_for_log", return_value=""))

    def start_patch(self, patcher):
        started = patcher.start()
        self.addCleanup(patcher.stop)
        return started

    def patch_agent(self, agent=None):
        return self.patch_route("_get_or_create_agent", return_value=agent or self.agent)

    def patch_route(self, name, **kwargs):
        """Patch a route symbol, routing method names onto ``self.routes``."""
        if name in self.INSTANCE_METHODS:
            return self.start_patch(patch.object(self.routes, name, **kwargs))
        return self.start_patch(patch(f"{ROUTES}.{name}", **kwargs))

    def patch_target(self, target, **kwargs):
        return self.start_patch(patch(target, **kwargs))

    def fake_paths(self, func, *paths):
        """Patch ``os.path.<func>`` so ONLY ``paths`` are reported as present."""
        present = set(paths)
        return self.patch_route(f"os.path.{func}", side_effect=lambda p: p in present)

    # -- frozen-context overrides (skill-editor / append / shared handlers) ---
    def override_handler_ctx(self, handler_key, **overrides):
        """Swap callables on the frozen context a skill handler captured.

        Skill handlers are ``partial(fn, context)`` built per instance in
        ``AgentRoutes._build_handlers``; reach the context through the handler on
        ``self.routes.handlers`` and ``object.__setattr__`` past the frozen
        dataclass. ``append_skills_yaml_route`` carries its own narrower
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

        The unified module clones the per-session agent via ``type(base)(...)``;
        a base whose ``__new__`` returns a preset mock lets the factory (reset /
        get_skills) and shared handlers — which captured the original method at
        import — all hand out the same agent without patching the instance method.
        """
        agent = agent if agent is not None else self.agent

        class _CloneBase:
            client = "wifi-client"
            model = "wifi-model"
            skills = None
            ace_runner = None

            def __new__(cls, *args, **kwargs):
                return agent

        self.app_config.log_chatbot_agent = object.__new__(_CloneBase)
        return agent


# ======================================================================
# Structure
# ======================================================================
class TestLogChatbotBlueprintStructure(_LogChatbotTestBase):

    def test_blueprint_registered_with_url_prefix(self):
        """Verify blueprint registration and the full route set.

        Input: the Flask app with ``log_chatbot_bp`` registered (via setUp).
        Expected: the blueprint is mounted under ``/log_chatbot`` and its routes
        exactly match ALL_RULES.
        """
        # Arrange / Act
        rules = {r.rule for r in self.app.url_map.iter_rules()
                 if r.endpoint.startswith("log_chatbot.")}

        # Assert
        self.assertIs(self.app.blueprints.get("log_chatbot"), log_chatbot_bp)
        self.assertEqual(log_chatbot_bp.url_prefix, "/log_chatbot")
        self.assertEqual(rules, ALL_RULES)
        self.assertEqual(len(rules), 38)


# ======================================================================
# Pages + native dialogs
# ======================================================================
class TestIndexAndBrowse(_LogChatbotTestBase):

    @patch(f"{ROUTES}.render_template", return_value="rendered")
    def test_index_renders_with_suggested_log_and_description(self, mock_render):
        """The index page renders with the suggested log and issue description.

        Input: GET /log_chatbot/ with a last analyzed log and a seeded case
        session.
        Expected: 200; chatbot/page.html is rendered (with the LOG_CHATBOT_UI
        config) with the suggested log path and the case's issue description.
        """
        # Arrange
        self.app_config.last_analyzed_log_path = LOG_PATH
        self.seed_session()

        # Act
        resp = self.client.get("/log_chatbot/")

        # Assert
        self.assertEqual(resp.status_code, 200)
        mock_render.assert_called_once_with(
            "chatbot/page.html",
            ui=LOG_CHATBOT_UI,
            suggested_log=LOG_PATH,
            issue_description="Wi-Fi drops every time the laptop resumes from Modern Standby.",
        )

    @patch(f"{ROUTES}.render_template", return_value="rendered")
    def test_index_without_session_uses_empty_defaults(self, mock_render):
        """The index page renders with empty defaults when nothing is set.

        Input: GET /log_chatbot/ with no case context and no last analyzed log.
        Expected: 200; chatbot/page.html is rendered with empty suggested_log and
        issue_description.
        """
        # Arrange: no case_context, no last analysed log
        # Act
        resp = self.client.get("/log_chatbot/")

        # Assert
        self.assertEqual(resp.status_code, 200)
        mock_render.assert_called_once_with(
            "chatbot/page.html", ui=LOG_CHATBOT_UI, suggested_log="", issue_description="")

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

    def test_browse_returns_selected_log_path(self):
        """The browse dialog returns the picked log path.

        Input: GET /log_chatbot/browse where the file dialog returns a log path.
        Expected: {success, path}; the dialog is titled "Select log file" and the
        hidden Tk root is destroyed.
        """
        # Arrange
        tk, askopen = self._patch_browse_dialog(LOG_PATH)

        # Act
        resp = self.client.get("/log_chatbot/browse")

        # Assert
        self.assertEqual(resp.get_json(), {"success": True, "path": LOG_PATH})
        self.assertEqual(askopen.call_args.kwargs["title"], "Select log file")
        tk.return_value.destroy.assert_called_once()

    def test_browse_cancelled_returns_empty_path(self):
        """A cancelled browse dialog returns an empty path.

        Input: GET /log_chatbot/browse where the dialog returns "".
        Expected: {success: True, path: ""}.
        """
        # Arrange
        self._patch_browse_dialog("")

        # Act
        resp = self.client.get("/log_chatbot/browse")

        # Assert
        self.assertEqual(resp.get_json(), {"success": True, "path": ""})

    def test_browse_yaml_returns_selected_yaml(self):
        """The shared browse_yaml handler returns the picked YAML path.

        Input: GET /log_chatbot/browse_yaml where the factory dialog returns a
        YAML path.
        Expected: {success: True, path}.
        """
        # Arrange
        choose = self.patch_target("services.chatbot.factory.choose_skills_yaml",
                                   return_value=r"C:\fake\skills.yaml")

        # Act
        resp = self.client.get("/log_chatbot/browse_yaml")

        # Assert
        self.assertEqual(resp.get_json(), {"success": True, "path": r"C:\fake\skills.yaml"})
        choose.assert_called_once_with()


# ======================================================================
# /set_log
# ======================================================================
class TestSetLog(_LogChatbotTestBase):

    def setUp(self):
        super().setUp()
        self.patch_agent()
        self.read_range = self.start_patch(
            patch(f"{ROUTES}.read_log_time_range", return_value=(LOG_FIRST, LOG_LAST)))
        self.agent.issue_time = ISSUE_DT

    def test_set_log_requires_log_path(self):
        """set_log requires a non-blank log_path.

        Input: POST /set_log with a whitespace log_path.
        Expected: 400 with "log_path is required".
        """
        # Act
        resp = self.client.post("/log_chatbot/set_log", json={"log_path": "   "})

        # Assert
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.get_json(), {"success": False, "error": "log_path is required"})

    def test_set_log_first_load_primes_agent_and_rotates_conversation(self):
        """First-time log load primes the agent and returns full metadata.

        Input: POST /set_log with a fresh LOG_PATH and a seeded case session.
        Expected: 200; the agent gets the log path, resets and is primed with the
        case context; the response carries skills, issue time, span, last time,
        has-date, rotated=False and a new conversation id; the session stores the
        log path and a conversation is ensured.
        """
        # Arrange
        self.seed_session()

        # Act
        resp = self.client.post("/log_chatbot/set_log", json={"log_path": LOG_PATH})

        # Assert
        body = resp.get_json()
        sess = self.get_session()
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(body["success"])
        self.assertEqual(body["message"], f"Log file set: {LOG_PATH}")
        self.assertEqual(body["skills"], SKILLS)
        self.assertEqual(body["issue_time"], "10/28/2025-11:25:49.000")
        self.assertEqual(body["log_span_minutes"], 30)
        self.assertEqual(body["log_last_time"], "10/28/2025-12:00:00.000")
        self.assertTrue(body["log_has_date"])
        self.assertFalse(body["rotated"])
        self.assertEqual(body["previous_log_path"], "")
        self.assertEqual(body["new_conversation_id"], sess["feedback_conversation_id"])
        self.assertEqual(sess["chatbot_log_path"], LOG_PATH)
        self.assertEqual(self.agent.current_log_path, LOG_PATH)
        self.agent.reset_conversation.assert_called_once_with()
        self.agent.prime_with_context.assert_called_once()
        self.assertEqual(self.agent.prime_with_context.call_args.kwargs["case_nbr"], "00123456")
        self.feedback_service.ensure_conversation.assert_called_once_with(
            conversation_id=body["new_conversation_id"], session_id=ANY, issue=ANY, log_path=LOG_PATH,
            domain="")

    def test_set_log_different_file_reports_rotation(self):
        """Loading a different log reports a conversation rotation.

        Input: POST /set_log with LOG_PATH while the session already holds a
        different log and old conversation id.
        Expected: rotated=True; the previous log/conversation are reported, a new
        conversation id is issued, and the agent is reset.
        """
        # Arrange
        self.seed_session(chatbot_log_path=r"C:\fake\logs\old.etl.log",
                          feedback_conversation_id="conv-old")

        # Act
        body = self.client.post("/log_chatbot/set_log", json={"log_path": LOG_PATH}).get_json()

        # Assert
        self.assertTrue(body["rotated"])
        self.assertEqual(body["previous_log_path"], r"C:\fake\logs\old.etl.log")
        self.assertEqual(body["previous_conversation_id"], "conv-old")
        self.assertNotEqual(body["new_conversation_id"], "conv-old")
        self.agent.reset_conversation.assert_called_once_with()

    def test_set_log_same_file_keeps_conversation_and_history(self):
        """Reloading the same log keeps the conversation and history.

        Input: POST /set_log with the same LOG_PATH already in session; the agent
        still holds history.
        Expected: rotated=False; the conversation id is unchanged, the agent is
        not reset, its history is preserved and no stored context is re-fetched.
        """
        # Arrange
        history = [{"role": "user", "content": "why did it drop?"}]
        self.agent.conversation_history = list(history)
        self.seed_session(chatbot_log_path=LOG_PATH, feedback_conversation_id="conv-same")

        # Act
        body = self.client.post("/log_chatbot/set_log", json={"log_path": LOG_PATH}).get_json()

        # Assert
        self.assertFalse(body["rotated"])
        self.assertEqual(body["new_conversation_id"], "conv-same")
        self.agent.reset_conversation.assert_not_called()
        self.assertEqual(self.agent.conversation_history, history)
        self.history_service.get_context.assert_not_called()

    def test_set_log_same_file_without_history_restores_stored_context(self):
        """Reloading the same log with a fresh agent restores stored context.

        Input: POST /set_log with the same LOG_PATH in session but a new agent;
        history_service has stored context.
        Expected: the stored context is fetched for that conversation and imported
        into the agent.
        """
        # Arrange
        stored = [{"role": "user", "content": "stored"}]
        self.history_service.get_context.return_value = stored
        self.seed_session(chatbot_log_path=LOG_PATH, feedback_conversation_id="conv-same")

        # Act
        self.client.post("/log_chatbot/set_log", json={"log_path": LOG_PATH})

        # Assert
        self.history_service.get_context.assert_called_once_with("conv-same", domain="")
        self.agent.import_conversation_context.assert_called_once_with(stored)

    def test_set_log_time_only_log_uses_raw_cache_for_last_time(self):
        """A dateless (DDD-style) log derives its last time from the raw cache.

        Input: POST /set_log where read_log_time_range yields no range and the
        log has no dated timestamps, but the raw cache has time-only lines.
        Expected: the raw cache is loaded; last time is the last cached timestamp
        and log_has_date is False.
        """
        # Arrange: DDD-style log - no dated timestamps
        self.read_range.return_value = (None, None)
        self.agent._log_has_date.return_value = False
        self.agent._raw_log_cache = ["10:00:00 first", "12:34:56.789123 last event", "no timestamp"]

        # Act
        body = self.client.post("/log_chatbot/set_log", json={"log_path": LOG_PATH}).get_json()

        # Assert
        self.agent._ensure_raw_log_cache.assert_called_once_with()
        self.assertEqual(body["log_last_time"], "12:34:56.789")
        self.assertFalse(body["log_has_date"])

    def test_set_log_agent_failure_returns_500(self):
        """An agent-creation failure during set_log returns 500.

        Input: POST /set_log where _get_or_create_agent raises.
        Expected: 500 with {success: False, error}; the agent factory was called
        with skip_prime=True.
        """
        # Arrange
        routes_get = self.patch_route("_get_or_create_agent",
                                      side_effect=RuntimeError("no api key"))

        # Act
        resp = self.client.post("/log_chatbot/set_log", json={"log_path": LOG_PATH})

        # Assert
        self.assertEqual(resp.status_code, 500)
        self.assertEqual(resp.get_json(), {"success": False, "error": "no api key"})
        routes_get.assert_called_once_with(skip_prime=True)


# ======================================================================
# /suggest_issue_times
# ======================================================================
class TestSuggestIssueTimes(_LogChatbotTestBase):

    def setUp(self):
        super().setUp()
        self.patch_agent()
        self.read_range = self.start_patch(
            patch(f"{ROUTES}.read_log_time_range", return_value=(LOG_FIRST, LOG_LAST)))
        self.start_patch(patch(f"{ROUTES}.get_effective_timezone", return_value="Pacific Standard Time"))
        self.start_patch(patch(f"{ROUTES}.format_tz_label", return_value="PST (UTC-08:00)"))
        self.build = self.start_patch(patch(f"{ROUTES}.build_issue_time_suggestions"))

    def test_no_text_and_no_log_returns_400(self):
        """No text and no log yields 400.

        Input: POST /suggest_issue_times with blank text and no loaded log/cache.
        Expected: 400 asking the user to type a description or load a log; the
        builder is not called.
        """
        # Arrange
        self.agent.current_log_path = ""
        self.agent._ensure_raw_log_cache.return_value = True  # cache unavailable

        # Act
        resp = self.client.post("/log_chatbot/suggest_issue_times", json={"text": "  "})

        # Assert
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.get_json()["error"], "Type a problem description or load a log first.")
        self.build.assert_not_called()

    def test_success_passes_log_context_to_builder(self):
        """Issue-time suggestion forwards log context to the builder.

        Input: POST /suggest_issue_times with text and a loaded log whose cache
        and range are available.
        Expected: 200 with suggestions; build_issue_time_suggestions is called
        with the text, cached log lines, first/last timestamps, LLM client/model,
        has-date and tz label.
        """
        # Arrange
        self.agent._ensure_raw_log_cache.return_value = False
        self.agent._raw_log_cache = ["line 1"]
        self.build.return_value = {"success": True, "suggestions": ["10/28/2025-11:25:49"]}

        # Act
        resp = self.client.post("/log_chatbot/suggest_issue_times", json={"text": "dropped at 11:25"})

        # Assert
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json()["suggestions"], ["10/28/2025-11:25:49"])
        self.build.assert_called_once_with(
            text="dropped at 11:25", log_lines=["line 1"], first_ts=LOG_FIRST, last_ts=LOG_LAST,
            llm_client=self.agent.client, llm_model="gpt-test", log_has_date=True,
            log_frame_first_ts=None, log_frame_last_ts=None, tz_label="PST (UTC-08:00)", brief=ANY,
        )
        self.read_range.assert_called_once_with(LOG_PATH)

    def test_builder_failure_returns_503(self):
        """An unsuccessful builder result maps to 503.

        Input: POST /suggest_issue_times where the builder returns success=False.
        Expected: 503 echoing the builder error.
        """
        # Arrange
        self.build.return_value = {"success": False, "error": "LLM unavailable"}

        # Act
        resp = self.client.post("/log_chatbot/suggest_issue_times", json={"text": "dropped"})

        # Assert
        self.assertEqual(resp.status_code, 503)
        self.assertEqual(resp.get_json()["error"], "LLM unavailable")

    def test_exception_returns_500(self):
        """A builder exception returns 500.

        Input: POST /suggest_issue_times where the builder raises ValueError.
        Expected: 500 with {success: False, error}.
        """
        # Arrange
        self.build.side_effect = ValueError("bad digest")

        # Act
        resp = self.client.post("/log_chatbot/suggest_issue_times", json={"text": "dropped"})

        # Assert
        self.assertEqual(resp.status_code, 500)
        self.assertEqual(resp.get_json(), {"success": False, "error": "bad digest"})


# ======================================================================
# /chat
# ======================================================================
class TestChat(_LogChatbotTestBase):

    def setUp(self):
        super().setUp()
        # chat_jobs stays REAL (its in-memory job + _job_sse produce the SSE
        # frames); re-patch over the base mock with the real job runtime.
        import services.chatbot.job_runtime as real_chat_jobs
        self.chat_jobs = self.start_patch(patch(f"{ROUTES}.chat_jobs", new=real_chat_jobs))
        # The worker runs inline so the job is terminal before _job_sse streams.
        self.patch_route("threading", new=MagicMock(Thread=_SyncThread))
        self.agent = make_agent(LOG_PATH)
        self.agent.chat.return_value = {"answer": "Roaming failure at 11:25"}
        self.get_agent = self.patch_route("_get_or_create_agent", return_value=self.agent)
        # _resume_agent_for delegates to session.job_runtime.get_job; default
        # to "no prior job" so the real resume falls back to the session agent.
        self.session_jobs = self.patch_target("services.chatbot.session.job_runtime")
        self.session_jobs.get_job.return_value = None
        self.patch_route("_extract_issue_context", return_value={"case_nbr": "00123456"})

    def test_chat_requires_message(self):
        """chat requires a non-blank message.

        Input: POST /chat with a whitespace message.
        Expected: 400 with "message is required"; the agent is not called.
        """
        # Act
        resp = self.client.post("/log_chatbot/chat", json={"message": "  "})

        # Assert
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.get_json(), {"success": False, "error": "message is required"})
        self.agent.chat.assert_not_called()

    def test_ips_gate_blocks_with_428(self):
        """A log without a resolved case number is gated with 428.

        Input: POST /chat where check_ips_service reports a blocking state.
        Expected: 428 echoing the block payload; the agent never chats.
        """
        # Arrange
        self.seed_session(chatbot_session_id="sid-1")
        self.routes.session_agents["sid-1"] = self.agent
        self.mock_check_ips.blocking_state.return_value = {"needs_case": True}

        # Act
        resp = self.client.post("/log_chatbot/chat", json={"message": "q"})

        # Assert
        self.assertEqual(resp.status_code, 428)
        self.assertEqual(resp.get_json(), {"needs_case": True})
        self.agent.chat.assert_not_called()

    def test_chat_without_log_streams_error_event(self):
        """Chatting with no log streams an error event.

        Input: POST /chat where the agent has no log and none can be recovered.
        Expected: an SSE error event asking to set a log first; the agent never
        chats.
        """
        # Arrange: agent has no log, session/app_config have none to recover
        self.agent.current_log_path = ""

        # Act
        resp = self.client.post("/log_chatbot/chat", json={"message": "hi"})

        # Assert
        self.assertEqual(resp.mimetype, "text/event-stream")
        self.assertEqual(sse_events(resp),
                         [{"type": "error", "content": "No log file loaded. Please set a log file first."}])
        self.agent.chat.assert_not_called()

    def test_chat_recovers_log_path_from_session(self):
        """A missing agent log is recovered from the session.

        Input: POST /chat where the agent has no log but the session holds one.
        Expected: the agent's current_log_path is restored and the stream
        completes with a done event.
        """
        # Arrange
        self.agent.current_log_path = ""
        self.seed_session(chatbot_log_path=LOG_PATH)

        # Act
        resp = self.client.post("/log_chatbot/chat", json={"message": "hi"})

        # Assert
        self.assertEqual(self.agent.current_log_path, LOG_PATH)
        self.assertEqual(sse_events(resp)[-1]["type"], "done")

    def test_chat_streams_steps_then_done_and_records_wifi_turn(self):
        """Chat streams the worker's steps then a done event and records a turn.

        Input: POST /chat whose agent emits one step before returning.
        Expected: an SSE stream of that step then a done event carrying the
        result; the agent chats with default max_steps/temperature and a step
        callback; gather/feedback/history record the turn tagged domain="wifi".
        """
        # Arrange
        def fake_chat(message, **kwargs):
            kwargs["step_callback"]({"skill": "Roaming", "summary": "found drop"})
            return {"answer": "Roaming failure at 11:25"}

        self.agent.chat.side_effect = fake_chat
        self.seed_session()

        # Act
        resp = self.client.post("/log_chatbot/chat",
                                json={"message": "why disconnect?", "parent_message_id": "p-1"})

        # Assert
        self.assertEqual(resp.mimetype, "text/event-stream")
        events = sse_events(resp)
        self.assertEqual(events[0], {"type": "step", "step": {"skill": "Roaming", "summary": "found drop"}})
        self.assertEqual(events[-1]["type"], "done")
        self.assertEqual(events[-1]["result"], {"answer": "Roaming failure at 11:25"})
        call = self.agent.chat.call_args
        self.assertEqual(call.args, ("why disconnect?",))
        self.assertEqual((call.kwargs["max_steps"], call.kwargs["temperature"]), (6, 0.2))
        self.assertIn("step_callback", call.kwargs)
        self.assertEqual(self.gather_service.record_send.call_args.kwargs["domain"], "wifi")
        self.assertEqual(self.gather_service.record_send.call_args.kwargs["log_path"], LOG_PATH)
        fb = self.feedback_service.record_turn.call_args.kwargs
        self.assertEqual((fb["mode"], fb["parent_message_id"]), ("tools", "p-1"))
        hist = self.history_service.record_turn.call_args.kwargs
        self.assertEqual((hist["mode"], hist["log_path"]), ("tools", LOG_PATH))
        self.gather_service.record_usage.assert_called_once()

    def test_numeric_parameters_are_clamped(self):
        """Out-of-range numeric params are clamped to valid bounds.

        Input: POST /chat with temperature=5, max_steps=99,
        issue_time_window_minutes=5000.
        Expected: the agent chats with temperature=1.0 and max_steps=12; the
        issue-time window is clamped to 1440.
        """
        # Act
        self.client.post("/log_chatbot/chat", json={
            "message": "q", "temperature": 5, "max_steps": 99, "issue_time_window_minutes": 5000})

        # Assert
        kwargs = self.agent.chat.call_args.kwargs
        self.assertEqual((kwargs["temperature"], kwargs["max_steps"]), (1.0, 12))
        self.assertEqual(self.agent.issue_time_window_minutes, 1440)

    def test_invalid_numeric_parameters_fall_back_to_defaults(self):
        """Non-numeric params fall back to defaults.

        Input: POST /chat with temperature="hot", max_steps="lots",
        issue_time_window_minutes="x".
        Expected: the agent chats with the default temperature 0.2 and
        max_steps 6; the issue-time window attribute is left untouched.
        """
        # Act
        self.client.post("/log_chatbot/chat", json={
            "message": "q", "temperature": "hot", "max_steps": "lots", "issue_time_window_minutes": "x"})

        # Assert
        kwargs = self.agent.chat.call_args.kwargs
        self.assertEqual((kwargs["temperature"], kwargs["max_steps"]), (0.2, 6))
        self.assertNotIsInstance(self.agent.issue_time_window_minutes, int)  # untouched MagicMock attr

    @patch(f"{ROUTES}.get_effective_timezone", return_value="")
    def test_sidebar_issue_time_overrides_agent_issue_time(self, mock_tz):
        """A sidebar issue_time overrides the agent's own issue time.

        Input: POST /chat with an explicit issue_time while the agent context has
        an attachment_time.
        Expected: the agent's issue_time becomes the parsed datetime, the
        time-only flag and customer fields are cleared, attachment_time is
        dropped, and the timezone is resolved from the log.
        """
        # Arrange
        self.agent.issue_context = {"attachment_time": "01/01/2025-00:00:00"}

        # Act
        self.client.post("/log_chatbot/chat", json={"message": "hi", "issue_time": "10/28/2025-11:25:49"})

        # Assert
        self.assertEqual(self.agent.issue_time, ISSUE_DT)
        self.assertFalse(self.agent._issue_time_time_only)
        self.assertNotIn("attachment_time", self.agent.issue_context)
        self.assertEqual(self.agent.issue_time_tz, "")
        self.assertIsNone(self.agent.issue_time_customer)
        mock_tz.assert_called_once_with(LOG_PATH)

    def test_explicitly_cleared_issue_time_strips_timestamps(self):
        """An explicitly cleared issue_time strips timestamps from context text.

        Input: POST /chat with issue_time="" and issue_time_cleared=True while the
        context has timestamps in its description/subject.
        Expected: the agent's issue_time becomes None and all timestamps are
        stripped from the description and subject.
        """
        # Arrange
        self.agent.issue_time = ISSUE_DT
        self.agent.issue_context = {
            "attachment_time": "x",
            "description": "drop at 10/28/2025-11:25:49 again",
            "subject": "drop 12:00:00",
        }

        # Act
        self.client.post("/log_chatbot/chat", json={
            "message": "hi", "issue_time": "", "issue_time_cleared": True})

        # Assert
        self.assertIsNone(self.agent.issue_time)
        self.assertEqual(self.agent.issue_context, {"description": "drop at again", "subject": "drop"})

    def test_empty_issue_time_without_clear_flag_keeps_agent_time(self):
        """An empty issue_time without the clear flag keeps the agent's time.

        Input: POST /chat with issue_time="" and no issue_time_cleared flag.
        Expected: the agent's existing issue_time is left unchanged.
        """
        # Arrange
        sentinel = datetime(1, 1, 1, 14, 50, 51)
        self.agent.issue_time = sentinel

        # Act
        self.client.post("/log_chatbot/chat", json={"message": "hi", "issue_time": ""})

        # Assert
        self.assertIs(self.agent.issue_time, sentinel)

    def test_issue_time_window_is_clamped(self):
        """An over-large issue-time window is clamped.

        Input: POST /chat with issue_time_window_minutes=5000.
        Expected: the agent's window is clamped to 1440 minutes.
        """
        # Act
        self.client.post("/log_chatbot/chat", json={"message": "hi", "issue_time_window_minutes": 5000})

        # Assert
        self.assertEqual(self.agent.issue_time_window_minutes, 1440)

    def test_worker_detaches_agent_and_starts_background_job(self):
        """The chat worker detaches the session agent and starts a wifi job.

        Input: POST /chat with a session-bound agent.
        Expected: a chat job is started (title, agent, domain="") and the agent
        is popped from its session slot so a concurrent switch can't corrupt it.
        """
        # Arrange: re-patch chat_jobs to a mock so start_job can be asserted,
        # and stub _job_sse so the request returns without iterating a real job.
        jobs = self.patch_route("chat_jobs")
        jobs.start_job.return_value = MagicMock(turn_id="t-1", conversation_id="c-1")
        self.patch_route("_job_sse", return_value=iter(["data: {}\n\n"]))
        self.seed_session(chatbot_session_id="sid-1")
        self.routes.session_agents["sid-1"] = self.agent

        # Act
        self.client.post("/log_chatbot/chat", json={"message": "analyse"})

        # Assert
        kwargs = jobs.start_job.call_args.kwargs
        self.assertEqual((kwargs["title"], kwargs["agent"], kwargs["domain"]), ("analyse", self.agent, ""))
        self.assertNotIn("sid-1", self.routes.session_agents)  # detached from session slot

    def test_worker_failure_streams_error_and_books_failed_status(self):
        """A failing chat worker streams an error and books a failed status.

        Input: POST /chat where the agent chat raises RuntimeError.
        Expected: an SSE error event; gather records a "failed" turn status with
        error code "RuntimeError" and domain="wifi".
        """
        # Arrange
        self.agent.chat.side_effect = RuntimeError("LLM down")

        # Act
        events = sse_events(self.client.post("/log_chatbot/chat", json={"message": "analyze"}))

        # Assert
        self.assertEqual(events[-1], {"type": "error", "content": "LLM down"})
        status = self.gather_service.record_turn_status.call_args.kwargs
        self.assertEqual((status["status"], status["error_code"], status["domain"]),
                         ("failed", "RuntimeError", "wifi"))
        self.feedback_service.record_turn.assert_not_called()

    def test_agent_resolution_error_streams_error_event(self):
        """An agent-resolution error streams a single SSE error event.

        Input: POST /chat where _resume_agent_for raises before a turn id is
        bound.
        Expected: the outer handler captures the message and streams one SSE
        error event; no turn status is recorded (turn id not yet bound).
        """
        # Arrange
        self.patch_route("_resume_agent_for", side_effect=RuntimeError("Log Chatbot Agent is not available."))

        # Act
        events = sse_events(self.client.post("/log_chatbot/chat", json={"message": "q"}))

        # Assert
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["type"], "error")
        self.assertIn("Log Chatbot Agent is not available", events[0]["content"])
        self.gather_service.record_turn_status.assert_not_called()

    def test_finished_job_agent_is_adopted_for_follow_up(self):
        """A finished job's agent is adopted for a follow-up chat.

        Input: POST /chat for a conversation whose job finished, with a
        session-bound slot.
        Expected: the finished job's agent answers (not the session agent); the
        factory is not called, and the adopted agent is detached into its own
        background job (popped from the session slot).
        """
        # Arrange
        job_agent = make_agent(LOG_PATH)
        job_agent.chat.return_value = {"answer": "follow-up"}
        self.session_jobs.get_job.return_value = MagicMock(status="done", agent=job_agent)
        self.seed_session(feedback_conversation_id="conv-1", chatbot_session_id="sid-1")

        # Act
        resp = self.client.post("/log_chatbot/chat", json={"message": "and then?"})

        # Assert
        self.assertEqual(sse_events(resp)[-1]["result"], {"answer": "follow-up"})
        self.session_jobs.get_job.assert_called_once_with("conv-1")
        self.get_agent.assert_not_called()
        self.agent.chat.assert_not_called()
        job_agent.chat.assert_called_once()
        self.assertNotIn("sid-1", self.routes.session_agents)  # detached into the job




# ======================================================================
# /reset, /chat/stop, /skills
# ======================================================================
class TestResetStopSkills(_LogChatbotTestBase):

    def setUp(self):
        super().setUp()
        # reset/skills are factory handlers (capture the real _get_or_create_agent);
        # chat_stop is a shared handler using shared_routes-level services.
        self.use_base_agent()
        self.chat_jobs = self.start_patch(patch("services.chatbot.shared_routes.chat_jobs"))
        self.chat_jobs.get_job.return_value = None
        self.gather_service = self.start_patch(patch("services.chatbot.shared_routes.gather_service"))
        self.start_patch(patch("services.chatbot.shared_routes.extract_issue_context", return_value={}))

    def test_reset_resets_agent_conversation(self):
        """reset clears the agent conversation.

        Input: POST /reset.
        Expected: {success, message}; the agent's conversation is reset once.
        """
        # Act
        resp = self.client.post("/log_chatbot/reset")

        # Assert
        self.assertEqual(resp.get_json(), {"success": True, "message": "Conversation reset."})
        self.agent.reset_conversation.assert_called_once_with()

    def test_reset_failure_returns_500(self):
        """A reset failure returns 500.

        Input: POST /reset where no agent can be built.
        Expected: 500 with success False.
        """
        # Arrange: no base agent and no usable llm client -> agent build raises
        self.app_config.log_chatbot_agent = None
        self.app_config.llm_helper = MagicMock(client=None)

        # Act
        resp = self.client.post("/log_chatbot/reset")

        # Assert
        self.assertEqual(resp.status_code, 500)
        self.assertFalse(resp.get_json()["success"])

    def test_chat_stop_uses_session_conversation_and_records_cancel(self):
        """chat/stop cancels the session conversation and records the cancel.

        Input: POST /chat/stop with no body while the session holds a
        conversation whose job is running and cancellable.
        Expected: {success, stopped: True}; cancel runs on the session
        conversation and gather records a "cancelled" turn status.
        """
        # Arrange
        self.chat_jobs.get_job.return_value = MagicMock(turn_id="turn-9")
        self.chat_jobs.request_cancel.return_value = True
        self.seed_session(feedback_conversation_id="conv-1")

        # Act
        resp = self.client.post("/log_chatbot/chat/stop", json={})

        # Assert
        self.assertEqual(resp.get_json(), {"success": True, "stopped": True})
        self.chat_jobs.request_cancel.assert_called_once_with("conv-1")
        kwargs = self.gather_service.record_turn_status.call_args.kwargs
        self.assertEqual((kwargs["conversation_id"], kwargs["turn_id"], kwargs["status"]),
                         ("conv-1", "turn-9", "cancelled"))

    def test_chat_stop_without_conversation_is_noop(self):
        """chat/stop with no conversation is a no-op.

        Input: POST /chat/stop with no body and no session conversation.
        Expected: {success, stopped: False}; cancel is never attempted.
        """
        # Act
        resp = self.client.post("/log_chatbot/chat/stop", json={})

        # Assert
        self.assertEqual(resp.get_json(), {"success": True, "stopped": False})
        self.chat_jobs.request_cancel.assert_not_called()

    def test_chat_stop_nothing_running_does_not_record(self):
        """chat/stop records nothing when no job is running.

        Input: POST /chat/stop with a conversation id whose cancel returns False.
        Expected: {success, stopped: False}; cancel is attempted but no status is
        recorded.
        """
        # Arrange
        self.chat_jobs.request_cancel.return_value = False

        # Act
        resp = self.client.post("/log_chatbot/chat/stop", json={"conversation_id": "conv-2"})

        # Assert
        self.assertEqual(resp.get_json(), {"success": True, "stopped": False})
        self.chat_jobs.request_cancel.assert_called_once_with("conv-2")
        self.gather_service.record_turn_status.assert_not_called()

    def test_skills_returns_agent_skill_descriptions(self):
        """GET /skills returns the agent's skill descriptions.

        Input: GET /log_chatbot/skills.
        Expected: {success, skills} with the agent skill descriptions.
        """
        # Act
        resp = self.client.get("/log_chatbot/skills")

        # Assert
        self.assertEqual(resp.get_json(), {"success": True, "skills": SKILLS})

    def test_skills_failure_returns_500(self):
        """A GET /skills failure returns 500.

        Input: GET /skills where no agent can be built.
        Expected: 500 with success False.
        """
        # Arrange: no base agent and no usable llm client -> agent build raises
        self.app_config.log_chatbot_agent = None
        self.app_config.llm_helper = MagicMock(client=None)

        # Act
        resp = self.client.get("/log_chatbot/skills")

        # Assert
        self.assertEqual(resp.status_code, 500)
        self.assertFalse(resp.get_json()["success"])


# ======================================================================
# /history/*
# ======================================================================
class TestHistory(_LogChatbotTestBase):

    def setUp(self):
        super().setUp()
        # History endpoints are shared handlers using shared_routes-level services.
        self.history_service = self.start_patch(patch("services.chatbot.shared_routes.history_service"))
        self.chat_jobs = self.start_patch(patch("services.chatbot.shared_routes.chat_jobs"))
        self.chat_jobs.get_job.return_value = None
        self.chat_jobs.active_summaries.return_value = []

    def test_history_list_merges_running_jobs_and_sorts(self):
        """The history list merges running jobs and sorts pinned/new/old.

        Input: GET /history/list with two stored conversations (one pinned) and
        two active jobs (one overlapping, one new).
        Expected: order is pinned, new running, then old; the overlapping conv is
        flagged running and the new job is merged in with its title/turn count.
        """
        # Arrange
        self.history_service.list_conversations.return_value = [
            {"conversation_id": "a", "updated_at": "2025-01-01T00:00:00", "pinned": False},
            {"conversation_id": "b", "updated_at": "2025-02-01T00:00:00", "pinned": True},
        ]
        self.chat_jobs.active_summaries.return_value = [
            {"conversation_id": "a", "title": "A", "step_count": 2},
            {"conversation_id": "new", "title": "New run", "step_count": 3},
        ]

        # Act
        body = self.client.get("/log_chatbot/history/list").get_json()

        # Assert
        convs = body["conversations"]
        self.assertTrue(body["success"])
        self.assertEqual([c["conversation_id"] for c in convs], ["b", "new", "a"])
        self.assertTrue(convs[2]["running"])
        self.assertEqual(convs[1]["title"], "New run")
        self.assertEqual(convs[1]["turn_count"], 3)
        self.chat_jobs.active_summaries.assert_called_once_with(domain="")

    def test_history_list_failure_returns_500(self):
        """A history-list failure returns 500.

        Input: GET /history/list where list_conversations raises OSError.
        Expected: 500 with {success: False, error}.
        """
        # Arrange
        self.history_service.list_conversations.side_effect = OSError("disk")

        # Act
        resp = self.client.get("/log_chatbot/history/list")

        # Assert
        self.assertEqual(resp.status_code, 500)
        self.assertEqual(resp.get_json(), {"success": False, "error": "disk"})

    def test_history_stream_without_job_emits_idle(self):
        """Streaming a conversation with no live job emits idle.

        Input: GET /history/stream?conversation_id=conv-1 with no job.
        Expected: a single SSE "idle" event.
        """
        # Act
        resp = self.client.get("/log_chatbot/history/stream?conversation_id=conv-1")

        # Assert
        self.assertEqual(resp.mimetype, "text/event-stream")
        self.assertEqual(sse_events(resp), [{"type": "idle"}])
        self.chat_jobs.get_job.assert_called_once_with("conv-1")

    def test_history_stream_follows_live_job_to_terminal_event(self):
        """Streaming follows a live job to its terminal event.

        Input: GET /history/stream for a job whose live queue yields a step then
        an error.
        Expected: the SSE stream is the step then the error; the subscription is
        cleaned up.
        """
        # Arrange: nothing buffered yet; one live step then an error arrive on the queue
        live = queue.Queue()
        live.put(("step", {"skill": "Roaming"}))
        live.put(("error", "agent crashed"))
        job = MagicMock(turn_id="t", conversation_id="conv-1", domain="")
        self.chat_jobs.get_job.return_value = job
        self.start_patch(patch("services.chatbot.job_runtime.subscribe", return_value=(live, [], None)))
        unsubscribe = self.start_patch(patch("services.chatbot.job_runtime.unsubscribe"))

        # Act
        resp = self.client.get("/log_chatbot/history/stream?conversation_id=conv-1")

        # Assert
        self.assertEqual(sse_events(resp), [
            {"type": "step", "step": {"skill": "Roaming"}},
            {"type": "error", "content": "agent crashed"},
        ])
        unsubscribe.assert_called_once_with(job, live)

    def test_history_get_requires_id(self):
        """history/get requires a conversation_id.

        Input: GET /history/get with no conversation_id.
        Expected: 400 with "conversation_id is required".
        """
        resp = self.client.get("/log_chatbot/history/get")
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.get_json()["error"], "conversation_id is required")

    def test_history_get_not_found_returns_404(self):
        """history/get returns 404 for an unknown conversation.

        Input: GET /history/get where get_conversation returns None.
        Expected: 404 with "Conversation not found".
        """
        # Arrange
        self.history_service.get_conversation.return_value = None

        # Act
        resp = self.client.get("/log_chatbot/history/get?conversation_id=missing")

        # Assert
        self.assertEqual(resp.status_code, 404)
        self.assertEqual(resp.get_json()["error"], "Conversation not found")

    def test_history_get_returns_conversation_with_steps(self):
        """history/get returns the conversation including steps.

        Input: GET /history/get with a known conversation_id.
        Expected: {success, conversation}; get_conversation is called with
        with_steps=True.
        """
        # Arrange
        self.history_service.get_conversation.return_value = {"title": "T", "turns": []}

        # Act
        resp = self.client.get("/log_chatbot/history/get?conversation_id=conv-1")

        # Assert
        self.assertEqual(resp.get_json(), {"success": True, "conversation": {"title": "T", "turns": []}})
        self.history_service.get_conversation.assert_called_once_with("conv-1", domain="", with_steps=True)

    def test_history_delete_requires_id(self):
        """history/delete requires a conversation_id.

        Input: POST /history/delete with an empty body.
        Expected: 400; nothing is deleted.
        """
        resp = self.client.post("/log_chatbot/history/delete", json={})
        self.assertEqual(resp.status_code, 400)
        self.history_service.delete_conversation.assert_not_called()

    def test_history_delete_active_conversation_clears_session_pointer(self):
        """Deleting the active conversation clears the session pointer.

        Input: POST /history/delete for the conversation stored in session;
        delete succeeds.
        Expected: {success: True}; the session's feedback_conversation_id is
        removed.
        """
        # Arrange
        self.history_service.delete_conversation.return_value = True
        self.seed_session(feedback_conversation_id="conv-1")

        # Act
        resp = self.client.post("/log_chatbot/history/delete", json={"conversation_id": "conv-1"})

        # Assert
        self.assertEqual(resp.get_json(), {"success": True})
        self.assertNotIn("feedback_conversation_id", self.get_session())

    def test_history_delete_failure_keeps_session_pointer(self):
        """A failed delete keeps the session pointer.

        Input: POST /history/delete where delete returns False.
        Expected: {success: False}; the session conversation id is kept.
        """
        # Arrange
        self.history_service.delete_conversation.return_value = False
        self.seed_session(feedback_conversation_id="conv-1")

        # Act
        resp = self.client.post("/log_chatbot/history/delete", json={"conversation_id": "conv-1"})

        # Assert
        self.assertEqual(resp.get_json(), {"success": False})
        self.assertEqual(self.get_session()["feedback_conversation_id"], "conv-1")

    def test_history_rename_validates_id_and_title(self):
        """history/rename validates both id and title.

        Input: POST /history/rename missing the id, then missing the title.
        Expected: both return 400 with the matching required-field error; nothing
        is renamed.
        """
        # Act
        no_id = self.client.post("/log_chatbot/history/rename", json={"title": "x"})
        no_title = self.client.post("/log_chatbot/history/rename", json={"conversation_id": "c"})

        # Assert
        self.assertEqual((no_id.status_code, no_id.get_json()["error"]), (400, "conversation_id is required"))
        self.assertEqual((no_title.status_code, no_title.get_json()["error"]), (400, "title is required"))
        self.history_service.rename_conversation.assert_not_called()

    def test_history_rename_success(self):
        """history/rename trims and applies a new title.

        Input: POST /history/rename with a padded title.
        Expected: {success: True}; rename_conversation is called with the trimmed
        title.
        """
        # Arrange
        self.history_service.rename_conversation.return_value = True

        # Act
        resp = self.client.post("/log_chatbot/history/rename",
                                json={"conversation_id": "c", "title": " Roaming case "})

        # Assert
        self.assertEqual(resp.get_json(), {"success": True})
        self.history_service.rename_conversation.assert_called_once_with("c", "Roaming case", domain="")

    def test_history_pin_requires_id(self):
        """history/pin requires a conversation_id.

        Input: POST /history/pin with only a pinned flag.
        Expected: 400; set_pinned is never called.
        """
        resp = self.client.post("/log_chatbot/history/pin", json={"pinned": True})
        self.assertEqual(resp.status_code, 400)
        self.history_service.set_pinned.assert_not_called()

    def test_history_pin_success(self):
        """history/pin coerces the flag and persists it.

        Input: POST /history/pin with pinned=1.
        Expected: {success, pinned: True}; set_pinned is called with True.
        """
        # Arrange
        self.history_service.set_pinned.return_value = True

        # Act
        resp = self.client.post("/log_chatbot/history/pin", json={"conversation_id": "c", "pinned": 1})

        # Assert
        self.assertEqual(resp.get_json(), {"success": True, "pinned": True})
        self.history_service.set_pinned.assert_called_once_with("c", True, domain="")


class TestHistoryLoad(_LogChatbotTestBase):

    def setUp(self):
        super().setUp()
        self.get_agent = self.patch_agent()
        self.history_service.assistant_text_from_result.return_value = "answer text"

    def _saved_conversation(self):
        return {
            "title": "Saved",
            "turns": [{"user_message": "why?", "result": {"answer": "x"}}],
            "log_path": LOG_PATH,
            "issue": {"case_nbr": "00123456", "subject": "drop", "bogus": "ignored", "issue_type": 7},
            "issue_time": "10/28/2025-11:25:49",
        }

    def test_history_load_requires_id(self):
        """history/load requires a conversation_id.

        Input: POST /history/load with an empty body.
        Expected: 400.
        """
        resp = self.client.post("/log_chatbot/history/load", json={})
        self.assertEqual(resp.status_code, 400)

    def test_history_load_unknown_conversation_returns_404(self):
        """history/load returns 404 for an unknown conversation.

        Input: POST /history/load where get_conversation returns None.
        Expected: 404 with "Conversation not found".
        """
        # Arrange
        self.history_service.get_conversation.return_value = None

        # Act
        resp = self.client.post("/log_chatbot/history/load", json={"conversation_id": "nope"})

        # Assert
        self.assertEqual(resp.status_code, 404)
        self.assertEqual(resp.get_json()["error"], "Conversation not found")

    def test_history_load_rebuilds_agent_from_saved_turns(self):
        """history/load rebuilds the agent from saved turns.

        Input: POST /history/load for a saved conversation whose log exists and
        which has no stored model context.
        Expected: a fresh agent is reset and primed with the filtered issue
        fields, the log path and issue time are set, the conversation history is
        rebuilt from the turn text, and the session points at the conversation.
        """
        # Arrange
        self.history_service.get_conversation.return_value = self._saved_conversation()

        # Act
        with patch(f"{ROUTES}.os.path.exists", return_value=True):
            resp = self.client.post("/log_chatbot/history/load", json={"conversation_id": "conv-1"})

        # Assert
        body = resp.get_json()
        sess = self.get_session()
        self.assertTrue(body["success"])
        self.assertEqual(body["title"], "Saved")
        self.assertEqual(body["context_restored"], 0)
        self.assertTrue(body["log_exists"])
        self.assertFalse(body["running"])
        self.assertEqual(body["issue_time"], "10/28/2025-11:25:49")
        self.assertEqual(body["skills"], SKILLS)
        self.get_agent.assert_called_once_with(skip_prime=True)
        self.agent.reset_conversation.assert_called_once_with()
        self.agent.prime_with_context.assert_called_once_with(case_nbr="00123456", subject="drop")
        self.assertEqual(self.agent.issue_time, ISSUE_DT)
        self.assertEqual(self.agent.conversation_history, [
            {"role": "user", "content": "why?"},
            {"role": "assistant", "content": "answer text"},
        ])
        self.assertEqual(sess["feedback_conversation_id"], "conv-1")
        self.assertEqual(sess["chatbot_log_path"], LOG_PATH)

    def test_history_load_prefers_stored_model_context(self):
        """history/load prefers stored model context over rebuilt text.

        Input: POST /history/load for a saved conversation with stored model
        context.
        Expected: the stored context is imported (context_restored reflects the
        count), the text history is not rebuilt.
        """
        # Arrange
        self.history_service.get_conversation.return_value = self._saved_conversation()
        self.history_service.get_context.return_value = [{"role": "user", "content": "ctx"}]
        self.agent.import_conversation_context.return_value = 5

        # Act
        with patch(f"{ROUTES}.os.path.exists", return_value=True):
            body = self.client.post("/log_chatbot/history/load",
                                    json={"conversation_id": "conv-1"}).get_json()

        # Assert
        self.assertEqual(body["context_restored"], 5)
        self.assertEqual(self.agent.conversation_history, [])
        self.history_service.assistant_text_from_result.assert_not_called()

    def test_history_load_running_job_is_not_adopted_into_session(self):
        """A running job is surfaced but not adopted into the session.

        Input: POST /history/load where the first analysis is still running and
        nothing is on disk yet.
        Expected: running=True with the running user message and buffered steps;
        the agent is not adopted, the factory is not called, and no stored context
        is fetched.
        """
        # Arrange: first analysis still running, nothing on disk yet
        job_agent = make_agent()
        job_agent.issue_time = ISSUE_DT
        self.history_service.get_conversation.return_value = None
        self.chat_jobs.get_job.return_value = MagicMock(
            status="running", agent=job_agent, title="Analyzing", steps=[{"n": 1}], domain="")

        # Act
        with patch(f"{ROUTES}.os.path.exists", return_value=False):
            body = self.client.post("/log_chatbot/history/load",
                                    json={"conversation_id": "conv-1"}).get_json()

        # Assert
        self.assertTrue(body["running"])
        self.assertEqual(body["title"], "Analyzing")
        self.assertEqual(body["running_user_message"], "Analyzing")
        self.assertEqual(body["steps"], [{"n": 1}])
        self.assertEqual(body["log_path"], LOG_PATH)
        self.assertFalse(body["log_exists"])
        self.assertEqual(body["issue_time"], "10/28/2025-11:25:49.000")
        self.get_agent.assert_not_called()
        self.assertEqual(self.routes.session_agents, {})
        self.history_service.get_context.assert_not_called()

    def test_history_load_finished_job_agent_is_reinstated(self):
        """A finished job's agent is reinstated into the session.

        Input: POST /history/load for a saved conversation whose finished job
        carries an agent.
        Expected: running=False; the job agent is bound to the session slot, is
        not re-primed, and the factory is not called.
        """
        # Arrange
        job_agent = make_agent()
        self.history_service.get_conversation.return_value = self._saved_conversation()
        self.chat_jobs.get_job.return_value = MagicMock(status="done", agent=job_agent, domain="")
        self.seed_session(chatbot_session_id="sid-1")

        # Act
        with patch(f"{ROUTES}.os.path.exists", return_value=True):
            body = self.client.post("/log_chatbot/history/load",
                                    json={"conversation_id": "conv-1"}).get_json()

        # Assert
        self.assertFalse(body["running"])
        self.assertIs(self.routes.session_agents["sid-1"], job_agent)
        job_agent.prime_with_context.assert_not_called()
        self.get_agent.assert_not_called()


# ======================================================================
# /back_to_avatar + /prepare
# ======================================================================
class TestBackToAvatarAndPrepare(_LogChatbotTestBase):

    def test_back_to_avatar_clears_session_and_redirects_to_main_index(self):
        """back_to_avatar clears chatbot session state and redirects to main.

        Input: GET /back_to_avatar with a populated chatbot session plus one
        unrelated key.
        Expected: 302 to main.index; the session agent is dropped, the last log
        path is cleared, all chatbot-related session keys are removed, and the
        unrelated key is preserved.
        """
        # Arrange
        self.routes.session_agents["sid-1"] = self.agent
        self.app_config.last_analyzed_log_path = LOG_PATH
        self.seed_session(chatbot_session_id="sid-1", chatbot_log_path=LOG_PATH,
                          classification={"issue_type": "Roaming"}, selected_files=[],
                          feedback_conversation_id="conv-1", unrelated_key="kept")

        # Act
        resp = self.client.post("/log_chatbot/back_to_avatar")

        # Assert
        sess = self.get_session()
        self.assertEqual(resp.status_code, 302)
        self.assertTrue(resp.headers["Location"].endswith(self.main_stub_path("index")))
        for key in ("chatbot_session_id", "chatbot_log_path", "case_context", "classification",
                    "selected_files", "feedback_conversation_id"):
            self.assertNotIn(key, sess)
        self.assertEqual(sess["unrelated_key"], "kept")
        self.assertNotIn("sid-1", self.routes.session_agents)
        self.assertEqual(self.app_config.last_analyzed_log_path, "")

    def test_prepare_requires_etl_path(self):
        """prepare requires an etl_path.

        Input: POST /prepare with an empty body.
        Expected: 400 with "etl_path is required".
        """
        resp = self.client.post("/log_chatbot/prepare", json={})
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.get_json(), {"success": False, "error": "etl_path is required"})

    def test_prepare_missing_log_returns_404(self):
        """prepare returns 404 when the decoded log is missing.

        Input: POST /prepare with an ETL path whose sibling .log does not exist.
        Expected: 404 naming the expected "<etl>.log"; existence is checked on
        that path.
        """
        # Act
        with patch(f"{ROUTES}.os.path.isfile", return_value=False) as mock_isfile:
            resp = self.client.post("/log_chatbot/prepare", json={"etl_path": ETL_PATH})

        # Assert
        self.assertEqual(resp.status_code, 404)
        self.assertEqual(resp.get_json()["error"], f".log file not found: {ETL_PATH}.log")
        mock_isfile.assert_called_once_with(ETL_PATH + ".log")

    @patch(f"{ROUTES}.read_log_time_range", return_value=(LOG_FIRST, LOG_LAST))
    def test_prepare_primes_agent_and_invalidates_stale_caches(self, mock_range):
        """prepare primes the agent and invalidates stale issue-time caches.

        Input: POST /prepare with an existing decoded log while the session holds
        stale issue caches.
        Expected: 200; the stale caches are cleared and the attachment cache
        recomputed; the agent uses the new log, resets and is primed; the issue
        context is organized and a conversation is ensured.
        """
        # Arrange
        self.patch_agent()
        mock_organize = self.patch_route("_issue_context_organized")
        self.seed_session(_issue_ai_quick={"data": {"issue_times": ["stale"]}},
                          _carried_issue_time="stale", _attachment_time_cache="stale")

        # Act
        with patch(f"{ROUTES}.os.path.isfile", return_value=True):
            resp = self.client.post("/log_chatbot/prepare", json={"etl_path": ETL_PATH})

        # Assert
        sess = self.get_session()
        self.assertEqual(resp.get_json(), {"success": True})
        self.assertNotIn("_issue_ai_quick", sess)
        self.assertNotIn("_carried_issue_time", sess)
        self.assertEqual(sess["_attachment_time_cache"], "")  # recomputed from the fresh case
        self.assertEqual(self.app_config.last_analyzed_log_path, LOG_PATH)
        self.assertEqual(self.agent.current_log_path, LOG_PATH)
        self.agent.reset_conversation.assert_called_once_with()
        self.agent.prime_with_context.assert_called_once()
        mock_organize.assert_called_once_with(
            "Wi-Fi drops every time the laptop resumes from Modern Standby.", LOG_FIRST, LOG_LAST)
        self.feedback_service.ensure_conversation.assert_called_once_with(
            conversation_id=sess["feedback_conversation_id"], session_id=ANY, issue=ANY, log_path=LOG_PATH,
            domain="")

    @patch(f"{ROUTES}.read_log_time_range", return_value=(LOG_FIRST, LOG_LAST))
    @patch(f"{ROUTES}.validate_issue_time_in_log_range", return_value=(ISSUE_DT, LOG_FIRST, LOG_LAST, ""))
    def test_prepare_carries_valid_issue_time(self, mock_validate, mock_range):
        """prepare carries a valid in-range issue time forward.

        Input: POST /prepare with an issue_time that validates inside the log
        range.
        Expected: the session records the carried issue time (present, value, no
        warning).
        """
        # Arrange
        self.patch_agent()
        self.patch_route("_issue_context_organized")

        # Act
        with patch(f"{ROUTES}.os.path.isfile", return_value=True):
            self.client.post("/log_chatbot/prepare",
                             json={"etl_path": ETL_PATH, "issue_time": "10/28/2025-11:25:49"})

        # Assert
        sess = self.get_session()
        mock_validate.assert_called_once_with("10/28/2025-11:25:49", LOG_PATH, log_in_customer_clock=False)
        self.assertTrue(sess["_carried_issue_time_present"])
        self.assertEqual(sess["_carried_issue_time"], "10/28/2025-11:25:49.000")
        self.assertEqual(sess["_carried_issue_time_warning"], "")

    @patch(f"{ROUTES}.read_log_time_range", return_value=(LOG_FIRST, LOG_LAST))
    @patch(f"{ROUTES}.validate_issue_time_in_log_range",
           return_value=(None, LOG_FIRST, LOG_LAST, "Outside log range."))
    def test_prepare_rejects_out_of_range_issue_time_with_warning(self, mock_validate, mock_range):
        """prepare rejects an out-of-range issue time with a warning.

        Input: POST /prepare with an issue_time that validates outside the log
        range.
        Expected: the carried issue time is cleared and a descriptive warning is
        stored in the session.
        """
        # Arrange
        self.patch_agent()
        self.patch_route("_issue_context_organized")

        # Act
        with patch(f"{ROUTES}.os.path.isfile", return_value=True):
            self.client.post("/log_chatbot/prepare",
                             json={"etl_path": ETL_PATH, "issue_time": "10/29/2025-01:00:00"})

        # Assert
        sess = self.get_session()
        self.assertEqual(sess["_carried_issue_time"], "")
        self.assertEqual(
            sess["_carried_issue_time_warning"],
            "Auto-detected issue time 10/29/2025-01:00:00 was not used: Outside log range. "
            "Log range: 10/28/2025-11:00:00.000 to 10/28/2025-12:00:00.000. Please confirm the issue time.",
        )

    def test_prepare_agent_failure_returns_500(self):
        """A prepare agent failure returns 500.

        Input: POST /prepare with an existing log where the agent factory raises.
        Expected: 500 with {success: False, error}.
        """
        # Arrange
        self.patch_route("_get_or_create_agent", side_effect=RuntimeError("no key"))

        # Act
        with patch(f"{ROUTES}.os.path.isfile", return_value=True):
            resp = self.client.post("/log_chatbot/prepare", json={"etl_path": ETL_PATH})

        # Assert
        self.assertEqual(resp.status_code, 500)
        self.assertEqual(resp.get_json(), {"success": False, "error": "no key"})


# ======================================================================
# Skill (re)loading: /reload_skills, /load_skills_yaml, /append_skills_yaml, /reload_from_shared
# ======================================================================
class TestSkillLoading(_LogChatbotTestBase):

    def setUp(self):
        super().setUp()
        # load_skills_yaml / reload_from_shared are shared handlers using the
        # SharedRouteContext loader and the real _get_or_create_agent.
        self.use_base_agent()
        self.app_config.llm_helper = MagicMock(name="llm_helper")
        self.from_yaml = MagicMock(return_value={"A": {}, "B": {}})
        self.override_shared_ctx(load_skills_from_yaml=self.from_yaml)

    def test_load_skills_yaml_requires_path(self):
        """load_skills_yaml requires a yaml_path.

        Input: POST /load_skills_yaml with an empty body.
        Expected: 400 with "yaml_path is required.".
        """
        resp = self.client.post("/log_chatbot/load_skills_yaml", json={})
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.get_json()["error"], "yaml_path is required.")

    def test_load_skills_yaml_success(self):
        """load_skills_yaml loads and applies skills from a YAML file.

        Input: POST /load_skills_yaml with a YAML path.
        Expected: skills are loaded and applied to the agent; the response reports
        the count, source and agent skills.
        """
        # Act
        body = self.client.post("/log_chatbot/load_skills_yaml",
                                json={"yaml_path": r"C:\fake\skills.yaml"}).get_json()

        # Assert
        self.assertEqual(body["message"], "2 skills loaded from YAML")
        self.assertEqual(body["source"], r"C:\fake\skills.yaml")
        self.assertEqual(body["skills"], SKILLS)
        self.from_yaml.assert_called_once_with(r"C:\fake\skills.yaml")
        self.agent.apply_updated_skills.assert_called_once_with({"A": {}, "B": {}})

    def test_load_skills_yaml_parse_error_returns_500(self):
        """A load_skills_yaml parse error returns 500.

        Input: POST /load_skills_yaml where the loader raises ValueError.
        Expected: 500 echoing the error.
        """
        self.from_yaml.side_effect = ValueError("bad yaml")
        resp = self.client.post("/log_chatbot/load_skills_yaml", json={"yaml_path": "x.yaml"})
        self.assertEqual(resp.status_code, 500)
        self.assertEqual(resp.get_json()["error"], "bad yaml")

    def test_append_skills_yaml_requires_path(self):
        """append_skills_yaml requires a yaml_path.

        Input: POST /append_skills_yaml with an empty body.
        Expected: 400.
        """
        resp = self.client.post("/log_chatbot/append_skills_yaml", json={})
        self.assertEqual(resp.status_code, 400)

    @patch("utils.skill_import_utils.load_and_filter_source", side_effect=FileNotFoundError("YAML file not found: x"))
    def test_append_skills_yaml_missing_file_returns_404(self, mock_src):
        """append_skills_yaml returns 404 for a missing source file.

        Input: POST /append_skills_yaml where the source loader raises
        FileNotFoundError.
        Expected: 404 echoing the error.
        """
        resp = self.client.post("/log_chatbot/append_skills_yaml", json={"yaml_path": "x.yaml"})
        self.assertEqual(resp.status_code, 404)
        self.assertEqual(resp.get_json()["error"], "YAML file not found: x")

    @patch("utils.skill_import_utils.load_and_filter_source", side_effect=ValueError("bad indent"))
    def test_append_skills_yaml_parse_error_returns_400(self, mock_src):
        """append_skills_yaml returns 400 for an unparseable source.

        Input: POST /append_skills_yaml where the source loader raises ValueError.
        Expected: 400 with a "Cannot parse YAML: ..." message.
        """
        resp = self.client.post("/log_chatbot/append_skills_yaml", json={"yaml_path": "x.yaml"})
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.get_json()["error"], "Cannot parse YAML: bad indent")

    @patch("utils.skill_import_utils.load_and_filter_source", return_value=({}, {"Bad": "no expert_rules"}))
    def test_append_skills_yaml_nothing_appendable_returns_400(self, mock_src):
        """append_skills_yaml returns 400 when nothing is importable.

        Input: POST /append_skills_yaml where the source yields no valid skills,
        only skipped ones.
        Expected: 400 echoing the skipped map.
        """
        resp = self.client.post("/log_chatbot/append_skills_yaml", json={"yaml_path": "x.yaml"})
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.get_json()["skipped"], {"Bad": "no expert_rules"})

    @patch("utils.skill_import_utils.merge_overwrite")
    @patch("utils.skill_import_utils.load_and_filter_source")
    def test_append_skills_yaml_merges_into_cloud_seed_and_activates_user(self, mock_src, mock_merge):
        """append_skills_yaml merges into the cloud seed and activates user.

        Input: POST /append_skills_yaml with importable skills; no user YAML
        exists but a cloud baseline does.
        Expected: the import is merged onto the cloud baseline, persisted as a
        user snapshot, the source switches to "user", the file is activated, and
        the session records the modified path; the message reports counts.
        """
        # Arrange
        mock_read = MagicMock(return_value={"Roaming": {"name": "Roaming"}})
        mock_persist = MagicMock(return_value=USER_YAML)
        mock_set = MagicMock()
        mock_activate = MagicMock(return_value=SKILLS)
        self.override_skill_append_ctx(
            latest_user_yaml=MagicMock(return_value=(None, None)),
            latest_cloud_baseline=MagicMock(return_value=(CLOUD_YAML, date(2026, 9, 1))),
            read_yaml_file=mock_read,
            persist_user_yaml_snapshot=mock_persist,
            set_active_source=mock_set,
            activate_yaml=mock_activate,
        )
        mock_src.return_value = ({"Scan": {"name": "Scan"}}, {"Bad": "reason"})
        merged = {"Roaming": {"name": "Roaming"}, "Scan": {"name": "Scan"}}
        mock_merge.return_value = (merged, ["Scan"], ["Roaming"])

        # Act
        body = self.client.post("/log_chatbot/append_skills_yaml",
                                json={"yaml_path": r"C:\fake\new.yaml"}).get_json()

        # Assert
        self.assertTrue(body["success"])
        self.assertEqual(body["message"], "Imported from new.yaml: appended 1, overwrote 1, skipped 1.")
        self.assertEqual(body["filename"], USER_YAML.name)
        mock_read.assert_called_once_with(CLOUD_YAML)
        mock_merge.assert_called_once_with({"Roaming": {"name": "Roaming"}}, {"Scan": {"name": "Scan"}})
        mock_persist.assert_called_once_with(merged)
        mock_set.assert_called_once_with("user")
        mock_activate.assert_called_once_with(USER_YAML)
        self.assertTrue(self.get_session()["yaml_modified"])
        self.assertEqual(self.get_session()["yaml_modified_path"], str(USER_YAML))

    @patch("utils.helpers.get_load_path", return_value=None)
    def test_reload_from_shared_not_found_returns_400(self, mock_load_path):
        """reload_from_shared returns 400 when no shared YAML exists.

        Input: POST /reload_from_shared where get_load_path returns None.
        Expected: 400 with a "Shared YAML not found" message; skills are not
        applied.
        """
        resp = self.client.post("/log_chatbot/reload_from_shared")
        self.assertEqual(resp.status_code, 400)
        self.assertTrue(resp.get_json()["error"].startswith("Shared YAML not found at "))
        self.agent.apply_updated_skills.assert_not_called()

    @patch("utils.helpers.get_load_path", return_value=r"\\share\skills_config\skills.yaml")
    def test_reload_from_shared_success(self, mock_load_path):
        """reload_from_shared reloads skills from the shared folder.

        Input: POST /reload_from_shared where get_load_path returns a file.
        Expected: skills are loaded from it and applied; the response reports the
        reloaded count and source.
        """
        # Arrange
        self.from_yaml.return_value = {"A": {}}

        # Act
        with patch("pathlib.Path.exists", return_value=True):
            body = self.client.post("/log_chatbot/reload_from_shared").get_json()

        # Assert
        self.assertEqual(body["message"], "1 skills reloaded from shared folder")
        self.assertEqual(body["source"], r"\\share\skills_config\skills.yaml")
        self.from_yaml.assert_called_once_with(r"\\share\skills_config\skills.yaml")
        self.agent.apply_updated_skills.assert_called_once_with({"A": {}})


# ======================================================================
# /get_issue_context
# ======================================================================
class TestGetIssueContext(_LogChatbotTestBase):

    def setUp(self):
        super().setUp()
        self.organize = self.patch_route("_issue_context_organized")
        self.read_range = self.start_patch(patch(f"{ROUTES}.read_log_time_range"))
        self.start_patch(patch(f"{ROUTES}._resolved_issue_time_for", return_value=""))

    def test_without_log_returns_organized_description(self):
        """get_issue_context returns an organized description without a log.

        Input: GET /get_issue_context with a seeded case but no loaded log.
        Expected: the organized clean description and interpretation are returned
        with empty issue time(s); the log range is not read.
        """
        # Arrange
        self.organize.return_value = {"clean_description": "Clean desc", "issue_times": [],
                                      "interpretation": "user saw drop"}
        self.seed_session()

        # Act
        body = self.client.get("/log_chatbot/get_issue_context").get_json()

        # Assert
        self.assertEqual(body["description"], "Clean desc")
        self.assertEqual(body["interpretation"], "user saw drop")
        self.assertEqual(body["issue_time"], "")
        self.assertEqual(body["issue_times"], [])
        self.assertFalse(body["issue_time_blocked"])
        self.assertEqual(body["log_first_time"], "")
        self.read_range.assert_not_called()
        self.organize.assert_called_once_with(
            "Wi-Fi drops every time the laptop resumes from Modern Standby.", None, None, "")

    @patch(f"{ROUTES}.to_iana_timezone", return_value="America/Los_Angeles")
    @patch("utils.issue_time_ai.determine_issue_time_frames")
    def test_in_range_time_is_kept_with_customer_annotation(self, mock_frames, mock_iana):
        """An in-range issue time is kept and annotated with the customer time.

        Input: GET /get_issue_context with a loaded log and an organized time
        inside the log range; frames resolve a customer timezone.
        Expected: the issue time is kept, a customer tz/iana is reported, a
        customer-time annotation is attached, and the time is not blocked.
        """
        # Arrange
        self.read_range.return_value = (LOG_FIRST, LOG_LAST)
        self.organize.return_value = {"clean_description": "Clean", "issue_times": ["10/28/2025-11:25:49"]}
        mock_frames.side_effect = lambda parsed, paths, **kw: {
            "log_frame": parsed, "customer_frame": datetime(2025, 10, 27, 20, 25, 49),
            "customer_tz": "Pacific Standard Time"}
        self.seed_session(chatbot_log_path=LOG_PATH)

        # Act
        body = self.client.get("/log_chatbot/get_issue_context").get_json()

        # Assert
        self.assertEqual(body["issue_time"], "10/28/2025-11:25:49")
        self.assertEqual(body["issue_times"], ["10/28/2025-11:25:49"])
        self.assertEqual(body["customer_tz"], "Pacific Standard Time")
        self.assertEqual(body["customer_iana"], "America/Los_Angeles")
        self.assertEqual(body["customer_annotations"], {"10/28/2025-11:25:49": "10/27/2025-20:25:49.000"})
        self.assertFalse(body["issue_time_blocked"])
        self.assertEqual(body["log_first_time"], "10/28/2025-11:00:00.000")
        self.assertEqual(body["log_last_time"], "10/28/2025-12:00:00.000")
        self.read_range.assert_any_call(LOG_PATH)

    @patch("utils.issue_time_ai.determine_issue_time_frames")
    def test_out_of_range_time_is_blocked_with_warning(self, mock_frames):
        """An out-of-range issue time is blocked with a warning.

        Input: GET /get_issue_context with an organized time outside the log
        range.
        Expected: the issue time is cleared, issue_time_blocked is True, and a
        descriptive warning naming the log range is returned.
        """
        # Arrange
        self.read_range.return_value = (LOG_FIRST, LOG_LAST)
        self.organize.return_value = {"clean_description": "Clean", "issue_times": ["10/28/2025-13:30:00"]}
        mock_frames.side_effect = lambda parsed, paths, **kw: {"log_frame": parsed}
        self.seed_session(chatbot_log_path=LOG_PATH)

        # Act
        body = self.client.get("/log_chatbot/get_issue_context").get_json()

        # Assert
        self.assertEqual(body["issue_time"], "")
        self.assertEqual(body["issue_times"], [])
        self.assertTrue(body["issue_time_blocked"])
        self.assertEqual(
            body["issue_time_warning"],
            "The auto-detected issue time was not used because it is outside the selected log range "
            "(10/28/2025-11:00:00.000 to 10/28/2025-12:00:00.000). Please confirm the issue time.",
        )

    def test_failed_carried_hand_off_blocks_issue_time(self):
        """A failed carried hand-off blocks the issue time.

        Input: GET /get_issue_context where the session carried a hand-off that
        failed (present but empty, with a warning).
        Expected: issue times are cleared, issue_time_blocked is True, and the
        carried warning is surfaced.
        """
        # Arrange
        self.organize.return_value = {"clean_description": "Clean", "issue_times": ["10/28/2025-11:25:49"]}
        self.seed_session(_carried_issue_time_present=True, _carried_issue_time="",
                          _carried_issue_time_warning="Auto-detected issue time was not used")

        # Act
        body = self.client.get("/log_chatbot/get_issue_context").get_json()

        # Assert
        self.assertEqual(body["issue_times"], [])
        self.assertEqual(body["issue_time"], "")
        self.assertTrue(body["issue_time_blocked"])
        self.assertEqual(body["issue_time_warning"], "Auto-detected issue time was not used")

    def test_carried_issue_time_overrides_organized_times(self):
        """A carried issue time overrides the organized times.

        Input: GET /get_issue_context where the session carried a valid issue
        time different from the organized one.
        Expected: the carried time replaces the organized times and attachment
        time, and the time is not blocked.
        """
        # Arrange
        self.organize.return_value = {"clean_description": "Clean", "issue_times": ["01/01/2025-00:00:00"]}
        self.seed_session(_carried_issue_time_present=True, _carried_issue_time="10/28/2025-11:25:49.000")

        # Act
        body = self.client.get("/log_chatbot/get_issue_context").get_json()

        # Assert
        self.assertEqual(body["issue_times"], ["10/28/2025-11:25:49.000"])
        self.assertEqual(body["issue_time"], "10/28/2025-11:25:49.000")
        self.assertEqual(body["attachment_time"], "10/28/2025-11:25:49.000")
        self.assertFalse(body["issue_time_blocked"])


# ======================================================================
# Skills YAML lifecycle (status / use_cloud / use_user / refresh / load / save / delete / upload)
# ======================================================================
class TestSkillsYamlLifecycle(_LogChatbotTestBase):

    def setUp(self):
        super().setUp()
        # Skill-editor endpoints read a frozen SkillEditorContext bound at import;
        # override its callables (not the gone routes-module helpers).
        field_map = {
            "_latest_user_yaml": ("latest_user_yaml", (None, None)),
            "_latest_cloud_baseline": ("latest_cloud_baseline", (None, None)),
            "_get_active_source": ("get_active_source", "cloud"),
            "_set_active_source": ("set_active_source", None),
            "_activate_yaml": ("activate_yaml", SKILLS),
            "_read_yaml_file": ("read_yaml_file", {}),
            "_persist_user_yaml_snapshot": ("persist_user_yaml_snapshot", USER_YAML),
            "_refresh_cloud_baseline": ("refresh_cloud_baseline", (None, None)),
            "_resolve_cloud_skills_dir": ("resolve_cloud_skills_dir", ""),
            "_skills_yaml_status_payload": ("skills_yaml_status_payload", {}),
        }
        self.y = {}
        overrides = {"get_or_create_agent": MagicMock(return_value=self.agent)}
        for old, (field, value) in field_map.items():
            mock = MagicMock(return_value=value)
            self.y[old] = mock
            overrides[field] = mock
        self.override_skill_editor_ctx(**overrides)

    def test_skills_yaml_status_returns_payload(self):
        """skills_yaml_status returns the status payload.

        Input: GET /skills_yaml_status.
        Expected: {success} merged with the status payload fields.
        """
        # Arrange
        self.y["_skills_yaml_status_payload"].return_value = {"active_source": "user", "effective_source": "user"}

        # Act
        body = self.client.get("/log_chatbot/skills_yaml_status").get_json()

        # Assert
        self.assertEqual(body, {"success": True, "active_source": "user", "effective_source": "user"})

    def test_skills_yaml_status_failure_returns_500(self):
        """A skills_yaml_status failure returns 500.

        Input: GET /skills_yaml_status where the payload raises OSError.
        Expected: 500 with {success: False, error}.
        """
        self.y["_skills_yaml_status_payload"].side_effect = OSError("share down")
        resp = self.client.get("/log_chatbot/skills_yaml_status")
        self.assertEqual(resp.status_code, 500)
        self.assertEqual(resp.get_json(), {"success": False, "error": "share down"})

    def test_use_cloud_without_baseline_returns_404(self):
        """skills_yaml_use_cloud returns 404 without a baseline.

        Input: POST /skills_yaml_use_cloud with no cloud baseline.
        Expected: 404 with "No cloud baseline found."; the source is not switched.
        """
        resp = self.client.post("/log_chatbot/skills_yaml_use_cloud")
        self.assertEqual(resp.status_code, 404)
        self.assertTrue(resp.get_json()["error"].startswith("No cloud baseline found."))
        self.y["_set_active_source"].assert_not_called()

    def test_use_cloud_switches_source_and_reloads(self):
        """skills_yaml_use_cloud switches to the cloud source and reloads.

        Input: POST /skills_yaml_use_cloud with a cloud baseline present.
        Expected: the source is set to "cloud" and the baseline activated; the
        response reports source, date, filename and skills.
        """
        # Arrange
        self.y["_latest_cloud_baseline"].return_value = (CLOUD_YAML, date(2026, 9, 1))

        # Act
        body = self.client.post("/log_chatbot/skills_yaml_use_cloud").get_json()

        # Assert
        self.assertEqual(body["active_source"], "cloud")
        self.assertEqual(body["local_date"], "2026-09-01")
        self.assertEqual(body["filename"], CLOUD_YAML.name)
        self.assertEqual(body["skills"], SKILLS)
        self.y["_set_active_source"].assert_called_once_with("cloud")
        self.y["_activate_yaml"].assert_called_once_with(CLOUD_YAML)

    def test_use_user_without_overrides_returns_404(self):
        """skills_yaml_use_user returns 404 without a user override.

        Input: POST /skills_yaml_use_user with no user YAML.
        Expected: 404 with "No customised configuration found yet.".
        """
        resp = self.client.post("/log_chatbot/skills_yaml_use_user")
        self.assertEqual(resp.status_code, 404)
        self.assertTrue(resp.get_json()["error"].startswith("No customised configuration found yet."))

    def test_use_user_switches_source_and_reloads(self):
        """skills_yaml_use_user switches to the user source and reloads.

        Input: POST /skills_yaml_use_user with a user YAML present.
        Expected: the source is set to "user" and the user file activated; the
        response reports source and filename.
        """
        # Arrange
        self.y["_latest_user_yaml"].return_value = (USER_YAML, None)

        # Act
        body = self.client.post("/log_chatbot/skills_yaml_use_user").get_json()

        # Assert
        self.assertEqual(body["active_source"], "user")
        self.assertIsNone(body["local_date"])
        self.y["_set_active_source"].assert_called_once_with("user")
        self.y["_activate_yaml"].assert_called_once_with(USER_YAML)

    def test_refresh_cloud_baseline_unreachable_returns_503(self):
        """refresh_cloud_baseline returns 503 when the share is unreachable.

        Input: POST /refresh_cloud_baseline where refresh returns (None, None).
        Expected: 503 asking the user to retry on VPN.
        """
        resp = self.client.post("/log_chatbot/refresh_cloud_baseline")
        self.assertEqual(resp.status_code, 503)
        self.assertEqual(resp.get_json()["error"], "Share folder is unreachable; please retry on VPN.")

    def test_refresh_cloud_baseline_reloads_when_cloud_active(self):
        """refresh_cloud_baseline reloads when cloud is the active source.

        Input: POST /refresh_cloud_baseline that fetches a new baseline while
        cloud is active.
        Expected: the new baseline is activated and the response reports its date
        and skills.
        """
        # Arrange
        self.y["_refresh_cloud_baseline"].return_value = (CLOUD_YAML, date(2026, 9, 1))

        # Act
        body = self.client.post("/log_chatbot/refresh_cloud_baseline").get_json()

        # Assert
        self.assertTrue(body["success"])
        self.assertEqual(body["local_date"], "2026-09-01")
        self.assertEqual(body["skills"], SKILLS)
        self.y["_activate_yaml"].assert_called_once_with(CLOUD_YAML)

    def test_refresh_cloud_baseline_does_not_reload_when_user_active(self):
        """refresh_cloud_baseline does not reload when user is active.

        Input: POST /refresh_cloud_baseline that fetches a baseline while the
        active source is user.
        Expected: the baseline is not activated; the response is still success.
        """
        # Arrange
        self.y["_refresh_cloud_baseline"].return_value = (CLOUD_YAML, None)
        self.y["_get_active_source"].return_value = "user"

        # Act
        body = self.client.post("/log_chatbot/refresh_cloud_baseline").get_json()

        # Assert
        self.assertTrue(body["success"])
        self.y["_activate_yaml"].assert_not_called()

    def test_load_local_skills_yaml_defaults_to_active_source(self):
        """load_local_skills_yaml defaults to the active source.

        Input: GET /load_local_skills_yaml with no source param while the active
        source is user with a user YAML present.
        Expected: the user file is read; the response reports source, date and
        skills.
        """
        # Arrange
        self.y["_get_active_source"].return_value = "user"
        self.y["_latest_user_yaml"].return_value = (USER_YAML, date(2026, 10, 3))
        self.y["_read_yaml_file"].return_value = {"Roaming": {"name": "Roaming"}}

        # Act
        body = self.client.get("/log_chatbot/load_local_skills_yaml").get_json()

        # Assert
        self.assertEqual(body["source"], "user")
        self.assertEqual(body["local_date"], "2026-10-03")
        self.assertEqual(body["skills"], {"Roaming": {"name": "Roaming"}})
        self.y["_read_yaml_file"].assert_called_once_with(USER_YAML)

    def test_load_local_skills_yaml_unknown_source_falls_back_to_cloud_404(self):
        """load_local_skills_yaml treats an unknown source as cloud and 404s.

        Input: GET /load_local_skills_yaml?source=bogus with no cloud baseline.
        Expected: 404 resolved as source "cloud" with a "Cloud baseline not
        present locally" message.
        """
        # Act
        resp = self.client.get("/log_chatbot/load_local_skills_yaml?source=bogus")

        # Assert
        self.assertEqual(resp.status_code, 404)
        self.assertEqual(resp.get_json()["source"], "cloud")
        self.assertTrue(resp.get_json()["error"].startswith("Cloud baseline not present locally."))

    def test_load_local_skills_yaml_user_missing_returns_404(self):
        """load_local_skills_yaml returns 404 for a missing user source.

        Input: GET /load_local_skills_yaml?source=user with no user YAML.
        Expected: 404 with "No customised configuration on disk yet.".
        """
        resp = self.client.get("/log_chatbot/load_local_skills_yaml?source=user")
        self.assertEqual(resp.status_code, 404)
        self.assertEqual(resp.get_json()["error"], "No customised configuration on disk yet.")

    def test_load_local_skills_yaml_read_error_returns_500(self):
        """A load_local_skills_yaml read error returns 500.

        Input: GET /load_local_skills_yaml?source=cloud where reading the YAML
        raises ValueError.
        Expected: 500 echoing the error.
        """
        # Arrange
        self.y["_latest_cloud_baseline"].return_value = (CLOUD_YAML, None)
        self.y["_read_yaml_file"].side_effect = ValueError("Invalid YAML structure")

        # Act
        resp = self.client.get("/log_chatbot/load_local_skills_yaml?source=cloud")

        # Assert
        self.assertEqual(resp.status_code, 500)
        self.assertEqual(resp.get_json()["error"], "Invalid YAML structure")

    def test_save_local_skills_yaml_rejects_empty_payload(self):
        """save_local_skills_yaml rejects an empty skills payload.

        Input: POST /save_local_skills_yaml with an empty skills object.
        Expected: 400 requiring a non-empty 'skills' object; nothing is persisted.
        """
        resp = self.client.post("/log_chatbot/save_local_skills_yaml", json={"skills": {}})
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.get_json()["error"], "Request body must contain a non-empty 'skills' object.")
        self.y["_persist_user_yaml_snapshot"].assert_not_called()

    def test_save_local_skills_yaml_rejects_missing_description(self):
        """save_local_skills_yaml rejects a skill missing a description.

        Input: POST /save_local_skills_yaml with a skill lacking a description.
        Expected: 400 naming the skill's missing description.
        """
        resp = self.client.post("/log_chatbot/save_local_skills_yaml",
                                json={"skills": {"Roaming": {"name": "Roaming", "expert_rules": "1. x"}}})
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.get_json()["error"], "Skill 'Roaming': description is required.")

    def test_save_local_skills_yaml_rejects_empty_expert_rules(self):
        """save_local_skills_yaml rejects empty expert rules.

        Input: POST /save_local_skills_yaml with a skill whose expert_rules items
        are all blank.
        Expected: 400 naming the skill's required expert rules.
        """
        resp = self.client.post("/log_chatbot/save_local_skills_yaml", json={"skills": {
            "Roaming": {"name": "Roaming", "description": "d", "expert_rules": {"items": ["  "]}}}})
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.get_json()["error"], "Skill 'Roaming': expert rules are required.")

    def test_save_local_skills_yaml_sanitises_and_persists(self):
        """save_local_skills_yaml sanitises the payload and persists it.

        Input: POST /save_local_skills_yaml with a skill carrying padded fields,
        unknown keys, blank keywords and structured expert rules.
        Expected: the skill is trimmed, unknown keys dropped, keywords cleaned and
        expert rules numbered; it is persisted, the source switches to "user" and
        the session records the path.
        """
        # Arrange
        payload = {"skills": {" Roaming ": {
            "name": " Roaming ", "description": " Roam analysis ", "extra": "dropped",
            "keywords": [" CNCT_FLOW  ", "", "ROAM"], "exclusive": [],
            "expert_rules": {"preamble": "Check roam", "items": [
                "first", {"prefix": "2-1", "text": "sub step"}, "last"]},
        }}}

        # Act
        body = self.client.post("/log_chatbot/save_local_skills_yaml", json=payload).get_json()

        # Assert
        self.assertTrue(body["success"])
        self.assertEqual(body["message"], f"Saved 1 skill(s) to {USER_YAML.name}.")
        self.y["_persist_user_yaml_snapshot"].assert_called_once_with({"Roaming": {
            "name": "Roaming", "description": "Roam analysis",
            "keywords": [" CNCT_FLOW", "ROAM"],
            "expert_rules": "Check roam\n1. first\n2-1. sub step\n2. last\n",
        }})
        self.y["_set_active_source"].assert_called_once_with("user")
        self.assertEqual(self.get_session()["yaml_modified_path"], str(USER_YAML))

    def test_delete_local_skill_requires_key(self):
        """delete_local_skill requires a skill_key.

        Input: POST /delete_local_skill with an empty body.
        Expected: 400 with "skill_key is required.".
        """
        resp = self.client.post("/log_chatbot/delete_local_skill", json={})
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.get_json()["error"], "skill_key is required.")

    def test_delete_local_skill_without_any_yaml_returns_404(self):
        """delete_local_skill returns 404 with no local YAML to edit.

        Input: POST /delete_local_skill with a key but no user/cloud YAML.
        Expected: 404 with "No local skill YAML to edit.".
        """
        resp = self.client.post("/log_chatbot/delete_local_skill", json={"skill_key": "Roaming"})
        self.assertEqual(resp.status_code, 404)
        self.assertEqual(resp.get_json()["error"], "No local skill YAML to edit.")

    def test_delete_local_skill_unknown_key_returns_404(self):
        """delete_local_skill returns 404 for an unknown key.

        Input: POST /delete_local_skill for "Roaming" where the user YAML only
        has "Scan".
        Expected: 404 noting the key is not present; nothing is persisted.
        """
        # Arrange
        self.y["_latest_user_yaml"].return_value = (USER_YAML, None)
        self.y["_read_yaml_file"].return_value = {"Scan": {}}

        # Act
        resp = self.client.post("/log_chatbot/delete_local_skill", json={"skill_key": "Roaming"})

        # Assert
        self.assertEqual(resp.status_code, 404)
        self.assertEqual(resp.get_json()["error"], "Skill 'Roaming' is not present in the active configuration.")
        self.y["_persist_user_yaml_snapshot"].assert_not_called()

    def test_delete_local_skill_removes_key_from_cloud_seed(self):
        """delete_local_skill removes a key from the cloud seed into a user copy.

        Input: POST /delete_local_skill for "Roaming" where only a cloud baseline
        exists with Roaming and Scan.
        Expected: the remaining skills are persisted as a user snapshot and the
        source switches to "user".
        """
        # Arrange
        self.y["_latest_cloud_baseline"].return_value = (CLOUD_YAML, None)
        self.y["_read_yaml_file"].return_value = {"Roaming": {}, "Scan": {}}

        # Act
        body = self.client.post("/log_chatbot/delete_local_skill", json={"skill_key": "Roaming"}).get_json()

        # Assert
        self.assertEqual(body["message"], "Removed skill 'Roaming'.")
        self.y["_read_yaml_file"].assert_called_once_with(CLOUD_YAML)
        self.y["_persist_user_yaml_snapshot"].assert_called_once_with({"Scan": {}})
        self.y["_set_active_source"].assert_called_once_with("user")

    def test_upload_modified_yaml_without_user_yaml_returns_404(self):
        """upload_modified_yaml returns 404 with no user YAML.

        Input: POST /upload_modified_yaml with no modified/user YAML.
        Expected: 404 with "No customised skill YAML was found to upload.".
        """
        resp = self.client.post("/log_chatbot/upload_modified_yaml")
        self.assertEqual(resp.status_code, 404)
        self.assertEqual(resp.get_json()["error"], "No customised skill YAML was found to upload.")

    def test_upload_modified_yaml_share_unreachable_returns_503(self):
        """upload_modified_yaml returns 503 when the share is unreachable.

        Input: POST /upload_modified_yaml with a modified file but an
        unresolvable cloud dir.
        Expected: 503 asking the user to retry on VPN.
        """
        # Arrange
        self.seed_session(yaml_modified_path=str(USER_YAML))

        # Act
        with patch("pathlib.Path.exists", return_value=True):
            resp = self.client.post("/log_chatbot/upload_modified_yaml")

        # Assert
        self.assertEqual(resp.status_code, 503)
        self.assertEqual(resp.get_json()["error"], "Shared skill folder is unreachable; please retry on VPN.")

    @patch("getpass.getuser", return_value="test user")
    @patch("shutil.copy2")
    @patch("pathlib.Path.mkdir")
    def test_upload_modified_yaml_copies_to_contributions(self, mock_mkdir, mock_copy, mock_user):
        """upload_modified_yaml copies the YAML into the contributions folder.

        Input: POST /upload_modified_yaml with a session-modified YAML and a
        reachable share.
        Expected: the contributions folder is created and the file copied in
        under a ``<user>__<name>`` path; the response reports where it went.
        """
        # Arrange
        share = r"\\share\skills_config"
        self.y["_resolve_cloud_skills_dir"].return_value = share
        self.seed_session(yaml_modified_path=str(USER_YAML))
        expected = Path(share) / "user_contributions" / f"test_user__{USER_YAML.name}"

        # Act
        with patch("pathlib.Path.exists", return_value=True):
            body = self.client.post("/log_chatbot/upload_modified_yaml").get_json()

        # Assert
        self.assertTrue(body["success"])
        self.assertEqual(body["uploaded_to"], str(expected))
        mock_mkdir.assert_called_once_with(parents=True, exist_ok=True)
        mock_copy.assert_called_once_with(str(USER_YAML), str(expected))

    @patch("shutil.copy2")
    @patch("pathlib.Path.mkdir", side_effect=OSError("access denied"))
    def test_upload_modified_yaml_mkdir_failure_returns_500(self, mock_mkdir, mock_copy):
        """An upload_modified_yaml mkdir failure returns 500.

        Input: POST /upload_modified_yaml where mkdir raises OSError.
        Expected: 500 explaining the folder could not be created; no copy occurs.
        """
        # Arrange
        self.y["_resolve_cloud_skills_dir"].return_value = r"\\share\skills_config"
        self.seed_session(yaml_modified_path=str(USER_YAML))

        # Act
        with patch("pathlib.Path.exists", return_value=True):
            resp = self.client.post("/log_chatbot/upload_modified_yaml")

        # Assert
        self.assertEqual(resp.status_code, 500)
        self.assertEqual(resp.get_json()["error"], "Cannot create contributions folder on share: access denied")
        mock_copy.assert_not_called()


# ======================================================================
# Core logic: agent factory + issue-context helpers
# ======================================================================
class TestCoreLogic(_LogChatbotTestBase):

    def test_agent_factory_without_llm_returns_500(self):
        """The agent factory errors when no LLM is available.

        Input: GET /skills when there is no boot-time agent and no API client.
        Expected: 500 with a "Log Chatbot Agent is not available." error.
        """
        # Arrange: no boot-time agent and no API client
        self.app_config.llm_helper.client = None

        # Act
        resp = self.client.get("/log_chatbot/skills")

        # Assert
        self.assertEqual(resp.status_code, 500)
        self.assertTrue(resp.get_json()["error"].startswith("Log Chatbot Agent is not available."))

    def test_agent_factory_clones_base_agent_once_per_session(self):
        """The agent factory clones the base agent once per session.

        Input: _get_or_create_agent twice with a base FakeAgent and a session log.
        Expected: a new FakeAgent (not the base) carrying the base's client/model/
        skills and ACE runner plus the session log; the second call returns the
        same cached per-session instance.
        """
        # Arrange
        base = FakeAgent(client="base-client", model="base-model", skills={"A": {}})
        base.ace_runner = "ace"
        self.app_config.log_chatbot_agent = base
        extract = self.patch_route("_extract_issue_context")

        # Act
        with self.app.test_request_context():
            from flask import session
            session["chatbot_log_path"] = LOG_PATH
            agent = self.routes._get_or_create_agent(skip_prime=True)
            again = self.routes._get_or_create_agent(skip_prime=True)
            sid = session["chatbot_session_id"]

        # Assert
        self.assertIs(type(agent), FakeAgent)
        self.assertIsNot(agent, base)
        self.assertEqual((agent.client, agent.model, agent.skills), ("base-client", "base-model", {"A": {}}))
        self.assertEqual(agent.attached_ace, "ace")
        self.assertEqual(agent.current_log_path, LOG_PATH)
        self.assertIs(again, agent)
        self.assertIs(self.routes.session_agents[sid], agent)
        extract.assert_not_called()

    def test_agent_factory_falls_back_to_llm_helper(self):
        """The agent factory falls back to the llm_helper when no base agent.

        Input: _get_or_create_agent with no base agent but a configured
        llm_helper and a last analyzed log.
        Expected: a new Wi-Fi agent built from the helper client/model, with the
        last log set and primed with the extracted issue context.
        """
        # Arrange: the fallback builds the base via profile.agent_class
        original_cls = self.routes.profile.agent_class
        object.__setattr__(self.routes.profile, "agent_class", FakeAgent)
        self.addCleanup(object.__setattr__, self.routes.profile, "agent_class", original_cls)
        helper = self.app_config.llm_helper
        helper.client, helper.model, helper.skills = "helper-client", "gpt-x", {"B": {}}
        self.app_config.log_chatbot_agent = None
        self.app_config.last_analyzed_log_path = LOG_PATH
        self.patch_route("_extract_issue_context", return_value={"case_nbr": "00123456"})

        # Act
        with self.app.test_request_context():
            agent = self.routes._get_or_create_agent()

        # Assert
        self.assertIsInstance(agent, FakeAgent)
        self.assertEqual((agent.client, agent.model), ("helper-client", "gpt-x"))
        self.assertEqual(agent.current_log_path, LOG_PATH)
        self.assertEqual(agent.primed_with, {"case_nbr": "00123456"})

    @patch("services.chatbot.issue_context.extract_time_from_description", return_value=ISSUE_DT)
    def test_extract_issue_context_merges_sources_and_selected_attachment_time(self, mock_extract):
        """_extract_issue_context merges case, AI and selected-attachment time.

        Input: a session with a case context, classification, AI analysis and a
        selected attachment whose note carries a time.
        Expected: the returned context merges case number, subject, a composed
        description (with AI summary/keywords), issue type and the extracted
        attachment time (time-of-day parsed from the note); that time is cached.
        """
        # Arrange
        ctx = self.create_mock_case_context(attachment_list=[
            ["other.zip", "u1", ["10/01/2025", "no time here"]],
            ["log.zip", "u2", ["10/28/2025", "Issue at 10/28 11:25:49"]],
        ])

        # Act
        with self.app.test_request_context():
            from flask import session
            session["case_context"] = ctx
            session["classification"] = {"issue_type": "Disconnect"}
            session["ai_ips_analysis"] = {"Summary": " Drops on resume ", "Classification": {"x": 1},
                                         "Keywords": ["roam", "", "deauth"]}
            session["selected_files"] = [["log.zip", "u2", ["10/28/2025", "Issue at 10/28 11:25:49"]]]
            result = routes._extract_issue_context()
            cached = session["_attachment_time_cache"]

        # Assert
        self.assertEqual(result, {
            "case_nbr": "00123456",
            "subject": "Wi-Fi disconnects after resume",
            "description": "Wi-Fi drops every time the laptop resumes from Modern Standby.\n"
                           "Summary: Drops on resume\nKeywords: roam; deauth",
            "issue_type": "Disconnect",
            "attachment_time": "10/28/2025-11:25:49",
        })
        self.assertEqual(cached, "10/28/2025-11:25:49")

    @patch("services.chatbot.issue_context.extract_time_from_description")
    def test_extract_issue_context_uses_cached_attachment_time(self, mock_extract):
        """_extract_issue_context reuses a cached attachment time.

        Input: a session already holding ``_attachment_time_cache``.
        Expected: extract_time_from_description is not called and the cached time
        is returned.
        """
        # Act
        with self.app.test_request_context():
            from flask import session
            session["case_context"] = self.create_mock_case_context()
            session["_attachment_time_cache"] = "10/28/2025-11:25:49"
            result = routes._extract_issue_context()

        # Assert
        self.assertEqual(result["attachment_time"], "10/28/2025-11:25:49")
        mock_extract.assert_not_called()

    def test_extract_issue_context_ignores_env_detail(self):
        """_extract_issue_context ignores the IPS env_detail form.

        Input: a session case context that includes env_detail.
        Expected: env_detail content is absent from the result, whose keys are
        exactly case_nbr/subject/description/issue_type/attachment_time (the log
        chatbot does not consume env_detail on this branch).
        """
        # Branch note: env_detail (IPS Environment Details form) is consumed by
        # the handsfree reader on this branch, but NOT by the log chatbot.
        with self.app.test_request_context():
            from flask import session
            session["case_context"] = self.create_mock_case_context()
            result = routes._extract_issue_context()

        self.assertNotIn("TestModel X1", json.dumps(result))
        self.assertEqual(set(result), {"case_nbr", "subject", "description", "issue_type", "attachment_time"})

    def test_compose_concise_description_strips_tags_and_appends_time(self):
        """_compose_concise_description strips subject tags and appends the time.

        Input: a context whose subject has [tags] and (F/R...) noise plus an
        attachment time.
        Expected: the cleaned subject with an " at around <time>" suffix.
        """
        result = routes._compose_concise_description({
            "subject": "[IPS][WiFi] 6G Weak Signal disconnected (F/R：1/1u,40/200C)",
            "description": "", "attachment_time": "10/28/2025-11:25:49"})
        self.assertEqual(result, "6G Weak Signal disconnected at around 10/28/2025-11:25:49")

    def test_compose_concise_description_falls_back_to_description_time(self):
        """_compose_concise_description falls back to a description time.

        Input: a context with no subject/attachment time but a time embedded in
        the description.
        Expected: the description text with an " at around <time>" suffix.
        """
        result = routes._compose_concise_description({
            "subject": "", "description": "Drop seen 2025-10-28 11:25:49 in log", "attachment_time": ""})
        self.assertEqual(result, "Drop seen 2025-10-28 11:25:49 in log at around 2025-10-28 11:25:49")

    def test_compose_concise_description_default_when_empty(self):
        """_compose_concise_description returns a default for an empty context.

        Input: an empty context dict.
        Expected: "Perform full multi-skill log analysis".
        """
        self.assertEqual(routes._compose_concise_description({}), "Perform full multi-skill log analysis")


if __name__ == "__main__":
    unittest.main()
