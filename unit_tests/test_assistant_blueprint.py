"""Unit tests for the Muse assistant blueprint
(blueprints/assistant/assistant_routes.py).

The model is a scripted fake, the history store and the usage recorder are
patched at point of use, and no connector reaches outside the process: no
LLM, no IPS, no Outlook, no Echo, nothing written anywhere.

What is pinned here is the contract of the page's API and, above all, the
guard: the assistant reads the user's mail and cases while the app listens on
every network interface, so each route - reads included - must refuse a
request that is not this PC's own browser on this app's own page.
"""
import json
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from blueprints.assistant import assistant_routes as routes
from blueprints.assistant.assistant_routes import assistant_bp
from services.assistant.agent import AssistantAgent
from services.assistant.registry import Connector, Registry, object_schema
from unit_tests.base_test import BaseTestCase

CONV = "11111111-2222-3333-4444-555555555555"


def _reply(text=None, calls=()):
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=text,
                                                         tool_calls=list(calls) or None))],
        usage=SimpleNamespace(prompt_tokens=10, completion_tokens=5, total_tokens=15))


def _call(name, args=None, call_id="call_1"):
    return SimpleNamespace(id=call_id, function=SimpleNamespace(
        name=name, arguments=json.dumps(args or {})))


class _FakeLLM:
    def __init__(self, script):
        self.script = list(script)
        self.requests = []
        self.chat = SimpleNamespace(completions=self)

    def create(self, **kwargs):
        self.requests.append(kwargs)
        item = self.script.pop(0) if len(self.script) > 1 else self.script[0]
        if isinstance(item, BaseException):
            raise item
        return item


def _events(response):
    """The data frames of a Server-Sent-Events response, parsed."""
    out = []
    for frame in response.get_data(as_text=True).split("\n\n"):
        for line in frame.split("\n"):
            if line.startswith("data: "):
                out.append(json.loads(line[6:]))
    return out


class AssistantTestCase(BaseTestCase):
    blueprint = assistant_bp

    def setUp(self):
        super().setUp()
        routes._agents.clear()
        # The job registry is process-wide and every test reuses CONV.
        with routes.chat_jobs._registry_lock:
            routes.chat_jobs._jobs.clear()
        self.recorded_turns = []
        self.gather_sends = []
        self.gather_usage = []
        for target, replacement in (
            (patch.object(routes.history_service, "record_turn",
                          side_effect=lambda **kw: self.recorded_turns.append(kw)), None),
            (patch.object(routes.history_service, "get_context", return_value=[]), None),
            (patch.object(routes.history_service, "get_conversation", return_value=None), None),
            (patch.object(routes.history_service, "list_conversations", return_value=[]), None),
            (patch.object(routes.history_service, "delete_conversation", return_value=True), None),
            (patch.object(routes.gather_service, "record_send",
                          side_effect=lambda **kw: self.gather_sends.append(kw)), None),
            (patch.object(routes.gather_service, "record_usage",
                          side_effect=lambda **kw: self.gather_usage.append(kw)), None),
            (patch.object(routes.runner, "start_silent_sign_in"), None),
            (patch.object(routes.runner, "identity", return_value={
                "owner_name": "Test Engineer", "mailbox": "t@example.com", "role": "client",
                "signed_in": True, "sign_in_state": "signed_in"}), None),
        ):
            setattr(self, "_p", target.start())
            self.addCleanup(target.stop)

    def use_script(self, script, registry=None):
        """Every new agent answers from `script`. Returns the fake model."""
        llm = _FakeLLM(script)
        registry = registry or Registry([Connector(
            "lookup", "Look a key up.", object_schema({"key": {"type": "string"}}, ["key"]),
            lambda args, ctx: {"value": "v-" + args["key"]})])

        def _new_agent(headless=False):
            return AssistantAgent(llm, "fake-model", registry, headless=headless,
                                  identity=routes.runner.identity)
        p = patch.object(routes.runner, "new_agent", side_effect=_new_agent)
        p.start()
        self.addCleanup(p.stop)
        return llm

    def wait_idle(self, conversation_id, timeout=5.0):
        job = routes.chat_jobs.get_job(conversation_id)
        deadline = time.monotonic() + timeout
        while job is not None and job.worker_thread is not None and job.worker_thread.is_alive():
            if time.monotonic() > deadline:
                self.fail("the turn's worker did not finish")
            time.sleep(0.02)


class TestAssistantStructure(AssistantTestCase):
    def test_blueprint_registered_with_its_routes(self):
        """The blueprint is mounted under /assistant with exactly the page's API."""
        rules = {}
        for r in self.app.url_map.iter_rules():
            if r.endpoint.startswith("assistant."):
                rules.setdefault(r.rule, set()).update(r.methods)
        self.assertIs(self.app.blueprints.get("assistant"), assistant_bp)
        self.assertEqual(assistant_bp.url_prefix, "/assistant")
        self.assertEqual(set(rules), {
            "/assistant/", "/assistant/state", "/assistant/sign_in", "/assistant/connectors",
            "/assistant/chat", "/assistant/chat/stop", "/assistant/reset",
            "/assistant/history/list", "/assistant/history/get", "/assistant/history/stream",
            "/assistant/history/load", "/assistant/history/rename", "/assistant/history/pin",
            "/assistant/history/delete",
            # actions the user confirms, scheduled tasks, notifications
            "/assistant/actions/confirm", "/assistant/actions/dismiss",
            "/assistant/tasks", "/assistant/tasks/add", "/assistant/tasks/<action>",
            "/assistant/notifications", "/assistant/notifications/read",
            # voice input
            "/assistant/voice/status", "/assistant/voice/prepare", "/assistant/transcribe",
        })
        for rule in ("/assistant/chat", "/assistant/chat/stop", "/assistant/reset",
                     "/assistant/sign_in", "/assistant/history/load",
                     "/assistant/history/delete", "/assistant/history/rename",
                     "/assistant/history/pin", "/assistant/actions/confirm",
                     "/assistant/actions/dismiss", "/assistant/tasks/add",
                     "/assistant/tasks/<action>", "/assistant/notifications/read"):
            self.assertNotIn("GET", rules[rule], rule)

    def test_assistant_has_its_own_history_domain(self):
        """Conversations are filed apart from every log chatbot's."""
        from configs import chat_domains
        self.assertEqual(chat_domains.normalize("assistant"), "assistant")
        self.assertEqual(chat_domains.history_folder("assistant"), "assistant_history")
        self.assertEqual(chat_domains.history_prefix("assistant"), "assistant-")
        self.assertTrue(chat_domains.is_agent_domain("assistant"))
        folders = [d.history_folder for d in chat_domains.DOMAINS]
        prefixes = [d.history_prefix for d in chat_domains.DOMAINS]
        self.assertEqual(len(folders), len(set(folders)))
        self.assertEqual(len(prefixes), len(set(prefixes)))

    def test_page_renders_without_touching_the_log_chatbots(self):
        """The page is its own template and loads only its own script."""
        with patch.object(routes, "render_template", return_value="page") as render:
            response = self.client.get("/assistant/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(render.call_args.args[0], "assistant/page.html")
        routes.runner.start_silent_sign_in.assert_called_once_with()
        self.assertNotIn("chatbot_session_id", self.get_session())


class TestAssistantGuard(AssistantTestCase):
    """Every route answers this PC's own browser on this app's own page only."""

    READS = ("/assistant/state", "/assistant/connectors", "/assistant/history/list",
             "/assistant/tasks", "/assistant/notifications",
             "/assistant/history/get?conversation_id=" + CONV,
             "/assistant/history/stream?conversation_id=" + CONV)

    def test_another_machine_is_refused_even_for_reads(self):
        for path in self.READS + ("/assistant/",):
            response = self.client.get(path, environ_base={"REMOTE_ADDR": "10.20.30.40"})
            self.assertEqual(response.status_code, 403, path)
        response = self.client.post("/assistant/chat", json={"message": "hi"},
                                    environ_base={"REMOTE_ADDR": "10.20.30.40"})
        self.assertEqual(response.status_code, 403)

    def test_another_sites_page_is_refused_even_for_reads(self):
        for path in self.READS:
            response = self.client.get(path, headers={"Origin": "http://evil.example"})
            self.assertEqual(response.status_code, 403, path)
            response = self.client.get(path, headers={"Sec-Fetch-Site": "cross-site"})
            self.assertEqual(response.status_code, 403, path)
            response = self.client.get(path, headers={"Sec-Fetch-Site": "same-site"})
            self.assertEqual(response.status_code, 403, path)
        response = self.client.post("/assistant/chat", json={"message": "hi"},
                                    headers={"Origin": "null"})
        self.assertEqual(response.status_code, 403)

    def test_a_rebound_host_name_is_refused(self):
        """DNS rebinding: the page's own name now points at 127.0.0.1, so the
        request is same-origin and comes from loopback - only the Host header
        gives it away."""
        for path in self.READS:
            response = self.client.get(path, headers={"Host": "evil.example:48600"})
            self.assertEqual(response.status_code, 403, path)

    def test_this_pcs_own_page_is_served(self):
        for host in ("127.0.0.1:48600", "localhost:48600", "[::1]:48600"):
            response = self.client.get(
                "/assistant/state",
                headers={"Host": host, "Origin": "http://" + host, "Sec-Fetch-Site": "same-origin"})
            self.assertEqual(response.status_code, 200, host)

    def test_following_a_link_to_the_page_is_allowed_but_not_to_its_data(self):
        nav = {"Sec-Fetch-Site": "cross-site", "Sec-Fetch-Mode": "navigate"}
        with patch.object(routes, "render_template", return_value="page"):
            self.assertEqual(self.client.get("/assistant/", headers=nav).status_code, 200)
        self.assertEqual(self.client.get("/assistant/history/list", headers=nav).status_code, 403)
        self.assertEqual(self.client.get("/assistant/state", headers=nav).status_code, 403)


class TestAssistantChat(AssistantTestCase):
    def test_chat_streams_steps_then_the_answer(self):
        llm = self.use_script([_reply(None, [_call("lookup", {"key": "k"})]),
                               _reply("The value is v-k.")])
        response = self.client.post("/assistant/chat",
                                    json={"message": "what is k?", "conversation_id": CONV})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.mimetype == "text/event-stream")
        events = _events(response)
        self.wait_idle(CONV)
        steps = [e["step"]["content"] for e in events if e["type"] == "step"]
        self.assertTrue(any(s.startswith("🔧 Tool call: `lookup`") for s in steps), steps)
        self.assertTrue(all(e["step"]["role"] == "agent" and "ts_ms" in e["step"]
                            for e in events if e["type"] == "step"))
        done = events[-1]
        self.assertEqual(done["type"], "done")
        self.assertEqual(done["conversation_id"], CONV)
        self.assertEqual(done["result"]["data"], "The value is v-k.")
        self.assertEqual(done["result"]["connectors_used"], ["lookup"])
        self.assertFalse(done["result"]["internal"])
        self.assertEqual(len(llm.requests), 2)

    def test_turn_is_saved_under_the_assistant_domain_with_steps_and_context(self):
        self.use_script([_reply("Hello.")])
        _events(self.client.post("/assistant/chat",
                                 json={"message": "hi", "conversation_id": CONV}))
        self.wait_idle(CONV)
        self.assertEqual(len(self.recorded_turns), 1)
        saved = self.recorded_turns[0]
        self.assertEqual(saved["domain"], "assistant")
        self.assertEqual(saved["conversation_id"], CONV)
        self.assertEqual(saved["user_message"], "hi")
        self.assertEqual(saved["agent_result"]["data"], "Hello.")
        self.assertTrue(saved["steps"])
        self.assertEqual([m["role"] for m in saved["agent_context"]], ["user", "assistant"])

    def test_usage_analytics_get_a_label_never_the_message(self):
        """The first question of a conversation lands on the team's shared
        analytics folder; an assistant question can quote a mail."""
        secret = "customer ACME says their CEO's laptop fails - see mail from bob@acme.example"
        self.use_script([_reply("ok")])
        _events(self.client.post("/assistant/chat",
                                 json={"message": secret, "conversation_id": CONV}))
        self.wait_idle(CONV)
        self.assertEqual(len(self.gather_sends), 1)
        self.assertEqual(self.gather_sends[0]["domain"], "assistant")
        self.assertNotIn("ACME", json.dumps(self.gather_sends[0], default=str))
        self.assertEqual(self.gather_sends[0]["user_message"], "[assistant chat]")
        self.assertEqual(len(self.gather_usage), 1)
        self.assertEqual(self.gather_usage[0]["status"], "completed")
        self.assertEqual(self.gather_usage[0]["model"], "fake-model")
        self.assertNotIn("ACME", json.dumps(self.gather_usage[0], default=str))

    def test_conversation_id_is_created_and_kept_in_the_assistants_own_session_key(self):
        self.use_script([_reply("one"), _reply("two")])
        first = _events(self.client.post("/assistant/chat", json={"message": "a"}))[-1]
        conversation_id = first["conversation_id"]
        self.wait_idle(conversation_id)
        session = self.get_session()
        self.assertEqual(session.get("assistant_conversation_id"), conversation_id)
        self.assertNotIn("feedback_conversation_id", session)
        self.assertNotIn("chatbot_session_id", session)
        second = _events(self.client.post("/assistant/chat", json={"message": "b"}))[-1]
        self.wait_idle(conversation_id)
        self.assertEqual(second["conversation_id"], conversation_id)
        self.assertEqual(len(routes._agents), 1)
        self.assertEqual(len(routes._agents[conversation_id].conversation_history), 4)

    def test_log_chatbot_session_state_is_left_alone(self):
        """Opening the assistant must not take the Wi-Fi chatbot's case away."""
        self.seed_session(chatbot_session_id="wifi-sid", feedback_conversation_id="wifi-conv",
                          chatbot_log_path="C:/logs/a.log")
        self.use_script([_reply("ok")])
        done = _events(self.client.post("/assistant/chat", json={"message": "a"}))[-1]
        self.wait_idle(done["conversation_id"])
        self.client.post("/assistant/chat/stop", json={})
        self.client.post("/assistant/reset", json={})
        session = self.get_session()
        self.assertEqual(session["chatbot_session_id"], "wifi-sid")
        self.assertEqual(session["feedback_conversation_id"], "wifi-conv")
        self.assertEqual(session["chatbot_log_path"], "C:/logs/a.log")
        self.assertIn("case_context", session)
        self.assertNotIn("assistant_conversation_id", session)

    def test_bad_requests_are_refused_before_any_work(self):
        llm = self.use_script([_reply("ok")])
        self.assertEqual(self.client.post("/assistant/chat", json={"message": "  "}).status_code, 400)
        self.assertEqual(self.client.post("/assistant/chat", json={}).status_code, 400)
        self.assertEqual(self.client.post("/assistant/chat",
                                          json={"message": "x" * 8001}).status_code, 400)
        self.assertEqual(llm.requests, [])
        self.assertEqual(self.gather_sends, [])

    def test_a_strange_conversation_id_is_replaced_not_used_as_a_file_name(self):
        self.use_script([_reply("ok")])
        done = _events(self.client.post(
            "/assistant/chat", json={"message": "a", "conversation_id": "../../etc/passwd"}))[-1]
        self.wait_idle(done["conversation_id"])
        self.assertRegex(done["conversation_id"], r"^[0-9a-f-]{36}$")

    def test_model_failure_is_an_error_event_and_is_booked_as_failed(self):
        self.use_script([RuntimeError("gateway 503")])
        events = _events(self.client.post("/assistant/chat",
                                          json={"message": "hi", "conversation_id": CONV}))
        self.wait_idle(CONV)
        self.assertEqual(events[-1]["type"], "error")
        self.assertIn("gateway 503", events[-1]["content"])
        self.assertEqual(self.recorded_turns, [])
        self.assertEqual(self.gather_usage[0]["status"], "failed")
        self.assertEqual(self.gather_usage[0]["error_code"], "RuntimeError")

    def test_no_api_key_is_explained_in_the_stream(self):
        with patch.object(routes.runner, "new_agent",
                          side_effect=routes.runner.AssistantUnavailable("no API key here")):
            events = _events(self.client.post("/assistant/chat",
                                              json={"message": "hi", "conversation_id": CONV}))
        self.assertEqual(events, [{"type": "error", "content": "no API key here"}])
        self.assertIsNone(routes.chat_jobs.get_job(CONV))

    def test_stop_cancels_this_conversations_turn_only(self):
        release = threading.Event()
        registry = Registry([Connector("slow", "Slow.", object_schema(),
                                       lambda args, ctx: (release.wait(10), {"late": True})[1])])
        self.use_script([_reply(None, [_call("slow")]), _reply("never")], registry)
        stopped = {}

        def press_stop():
            time.sleep(0.4)
            with self.app.test_client() as other:
                stopped["wrong"] = other.post("/assistant/chat/stop",
                                              json={"conversation_id": "x" * 12}).get_json()
                stopped["right"] = other.post("/assistant/chat/stop",
                                              json={"conversation_id": CONV}).get_json()

        threading.Thread(target=press_stop, daemon=True).start()
        events = _events(self.client.post("/assistant/chat",
                                          json={"message": "go", "conversation_id": CONV}))
        release.set()
        self.wait_idle(CONV)
        self.assertEqual(stopped["wrong"], {"success": True, "stopped": False})
        self.assertEqual(stopped["right"], {"success": True, "stopped": True})
        self.assertEqual(events[-1]["type"], "done")
        self.assertTrue(events[-1]["result"]["stopped"])
        self.assertEqual(self.gather_usage[0]["status"], "cancelled")

    def test_stop_ignores_a_log_chatbots_job(self):
        """A job of another bot with the same id must not be cancellable here."""
        agent = SimpleNamespace(cancel_event=threading.Event())
        job = routes.chat_jobs.start_job(conversation_id=CONV, turn_id="t", title="wifi",
                                         agent=agent, domain="")
        try:
            body = self.client.post("/assistant/chat/stop",
                                    json={"conversation_id": CONV}).get_json()
            self.assertEqual(body, {"success": True, "stopped": False})
            self.assertFalse(agent.cancel_event.is_set())
        finally:
            routes.chat_jobs.finish_job(job, {})

    def test_second_message_while_answering_is_refused(self):
        agent = SimpleNamespace(cancel_event=threading.Event())
        job = routes.chat_jobs.start_job(conversation_id=CONV, turn_id="t", title="busy",
                                         agent=agent, domain="assistant")
        try:
            self.use_script([_reply("ok")])
            response = self.client.post("/assistant/chat",
                                        json={"message": "again", "conversation_id": CONV})
            self.assertEqual(response.status_code, 409)
        finally:
            routes.chat_jobs.finish_job(job, {})


class TestAssistantHistory(AssistantTestCase):
    SAVED = {"title": "My cases", "turns": [
        {"turn_id": "t1", "user_message": "list my cases",
         "result": {"type": "text", "data": "You have 2 cases."},
         "steps": [{"role": "agent", "content": "🧠 Reasoning Step 1"}]}]}

    def test_load_returns_the_turns_and_points_the_session_at_the_conversation(self):
        routes.history_service.get_conversation.return_value = dict(self.SAVED)
        body = self.client.post("/assistant/history/load",
                                json={"conversation_id": CONV}).get_json()
        self.assertTrue(body["success"])
        self.assertEqual(body["title"], "My cases")
        self.assertEqual(body["turns"][0]["steps"][0]["content"], "🧠 Reasoning Step 1")
        self.assertFalse(body["running"])
        self.assertEqual(self.get_session()["assistant_conversation_id"], CONV)
        routes.history_service.get_conversation.assert_called_with(
            CONV, domain="assistant", with_steps=True)

    def test_load_of_an_unknown_conversation_is_404(self):
        response = self.client.post("/assistant/history/load", json={"conversation_id": CONV})
        self.assertEqual(response.status_code, 404)
        self.assertNotIn("assistant_conversation_id", self.get_session())
        self.assertEqual(self.client.post("/assistant/history/load", json={}).status_code, 400)

    def test_a_reopened_conversation_is_continued_from_its_saved_turns(self):
        """After an app restart the agent is rebuilt from the saved turns."""
        routes.history_service.get_conversation.return_value = dict(self.SAVED)
        llm = self.use_script([_reply("Still 2.")])
        _events(self.client.post("/assistant/chat",
                                 json={"message": "still?", "conversation_id": CONV}))
        self.wait_idle(CONV)
        sent = [m.get("content") for m in llm.requests[0]["messages"]]
        self.assertIn("list my cases", sent)
        self.assertIn("You have 2 cases.", sent)
        self.assertEqual(sent[-1], "still?")

    def test_list_is_scoped_to_the_assistant(self):
        routes.history_service.list_conversations.return_value = [
            {"conversation_id": CONV, "title": "My cases", "updated_at": "2026-11-18T09:00:00"}]
        body = self.client.get("/assistant/history/list").get_json()
        self.assertEqual([c["conversation_id"] for c in body["conversations"]], [CONV])
        routes.history_service.list_conversations.assert_called_with(domain="assistant")

    def test_delete_forgets_the_conversation_and_its_agent(self):
        self.use_script([_reply("ok")])
        _events(self.client.post("/assistant/chat",
                                 json={"message": "a", "conversation_id": CONV}))
        self.wait_idle(CONV)
        self.assertIn(CONV, routes._agents)
        body = self.client.post("/assistant/history/delete",
                                json={"conversation_id": CONV}).get_json()
        self.assertEqual(body, {"success": True})
        routes.history_service.delete_conversation.assert_called_with(CONV, domain="assistant")
        self.assertNotIn(CONV, routes._agents)
        self.assertNotIn("assistant_conversation_id", self.get_session())

    def test_a_running_conversation_cannot_be_deleted(self):
        agent = SimpleNamespace(cancel_event=threading.Event())
        job = routes.chat_jobs.start_job(conversation_id=CONV, turn_id="t", title="busy",
                                         agent=agent, domain="assistant")
        try:
            response = self.client.post("/assistant/history/delete",
                                        json={"conversation_id": CONV})
            self.assertEqual(response.status_code, 409)
            routes.history_service.delete_conversation.assert_not_called()
            body = self.client.post("/assistant/history/load",
                                    json={"conversation_id": CONV}).get_json()
            self.assertTrue(body["running"])
            self.assertEqual(body["running_message"], "busy")
        finally:
            routes.chat_jobs.finish_job(job, {})

    def test_stream_of_another_bots_job_is_idle(self):
        agent = SimpleNamespace(cancel_event=threading.Event())
        job = routes.chat_jobs.start_job(conversation_id=CONV, turn_id="t", title="wifi",
                                         agent=agent, domain="")
        try:
            routes.chat_jobs.publish_step(job, {"role": "agent", "content": "wifi secret"})
            events = _events(self.client.get("/assistant/history/stream?conversation_id=" + CONV))
            self.assertEqual(events, [{"type": "idle"}])
        finally:
            routes.chat_jobs.finish_job(job, {})


class TestAssistantState(AssistantTestCase):
    def test_state_reports_identity_conversation_and_missing_api_key(self):
        with patch.object(routes.runner, "llm", return_value=(object(), "m")):
            body = self.client.get("/assistant/state").get_json()
        self.assertEqual(body["identity"]["owner_name"], "Test Engineer")
        self.assertEqual(body["conversation_id"], "")
        self.assertFalse(body["running"])
        self.assertEqual(body["llm_error"], "")
        with patch.object(routes.runner, "llm",
                          side_effect=routes.runner.AssistantUnavailable("no key")):
            body = self.client.get("/assistant/state").get_json()
        self.assertEqual(body["llm_error"], "no key")

    def test_sign_in_retry_is_silent(self):
        self.client.post("/assistant/sign_in", json={})
        routes.runner.start_silent_sign_in.assert_called_once_with(retry_failed=True)

    def test_connectors_lists_what_the_assistant_can_reach(self):
        body = self.client.get("/assistant/connectors").get_json()
        names = {c["name"]: c for c in body["connectors"]}
        self.assertIn("ask_echo", names)
        self.assertTrue(names["ask_echo"]["internal"])
        self.assertEqual(names["ask_echo"]["kind"], "read")


class TestAssistantTasksAndActions(AssistantTestCase):
    """Scheduled tasks, notifications and confirmed actions. The task files
    go to a temporary folder that is removed after each test."""

    def setUp(self):
        super().setUp()
        import tempfile
        from pathlib import Path
        from services.assistant import actions, tasks
        self.tasks, self.actions = tasks, actions
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        base = Path(self._tmp.name) / "assistant"
        p = patch.object(tasks, "_base_dir", return_value=base)
        p.start()
        self.addCleanup(p.stop)
        actions.clear()
        self.addCleanup(actions.clear)

    def test_importing_the_blueprint_does_not_start_the_task_clock(self):
        """Only the running app starts it (configs/set_up_app.set_up)."""
        self.assertFalse(self.tasks.ticker_running())
        body = self.client.get("/assistant/tasks").get_json()
        self.assertEqual(body, {"success": True, "open": [], "recent": [], "ticker_running": False})

    def test_the_sidebar_form_schedules_and_refuses_bad_times(self):
        ok = self.client.post("/assistant/tasks/add",
                              json={"when": "in 30 minutes", "instruction": "check my mail"})
        self.assertEqual(ok.status_code, 200)
        task = ok.get_json()["task"]
        self.assertEqual(task["status"], "scheduled")
        self.assertEqual(task["instruction"], "check my mail")
        listed = self.client.get("/assistant/tasks").get_json()
        self.assertEqual([t["id"] for t in listed["open"]], [task["id"]])

        for when, code in (("whenever", "unreadable_time"), ("2020-01-01T10:00", "time_in_past"),
                           ("", "unreadable_time")):
            bad = self.client.post("/assistant/tasks/add", json={"when": when, "instruction": "x"})
            self.assertEqual(bad.status_code, 400, when)
            self.assertEqual(bad.get_json()["code"], code, when)
        empty = self.client.post("/assistant/tasks/add", json={"when": "in 5 minutes"})
        self.assertEqual(empty.get_json()["code"], "empty_instruction")
        self.assertEqual(len(self.tasks.list_tasks()), 1)

    def test_cancel_skip_and_unknown_actions(self):
        task = self.client.post("/assistant/tasks/add",
                                json={"when": "in 30 minutes", "instruction": "x"}).get_json()["task"]
        self.assertEqual(self.client.post("/assistant/tasks/skip",
                                          json={"id": task["id"]}).status_code, 409)
        self.assertEqual(self.client.post("/assistant/tasks/explode",
                                          json={"id": task["id"]}).status_code, 404)
        self.assertEqual(self.client.post("/assistant/tasks/cancel",
                                          json={"id": "nope"}).status_code, 409)
        self.assertEqual(self.client.post("/assistant/tasks/cancel",
                                          json={"id": task["id"]}).get_json(), {"success": True})
        self.assertEqual(self.tasks.get_task(task["id"])["status"], "cancelled")
        self.assertEqual(self.client.post("/assistant/tasks/cancel",
                                          json={"id": task["id"]}).status_code, 409)

    def test_run_now_runs_a_task_on_its_own_thread(self):
        task = self.client.post("/assistant/tasks/add",
                                json={"when": "in 30 minutes", "instruction": "x"}).get_json()["task"]
        ran = threading.Event()
        with patch.object(self.tasks, "run_task", side_effect=lambda task_id: ran.set()) as run:
            self.assertEqual(self.client.post("/assistant/tasks/run_now",
                                              json={"id": task["id"]}).get_json(), {"success": True})
            self.assertTrue(ran.wait(3))
            run.assert_called_once_with(task["id"])
            self.assertEqual(self.client.post("/assistant/tasks/run_now",
                                              json={"id": "nope"}).status_code, 409)

    def test_notifications_are_listed_and_marked_read(self):
        first = self.tasks.notify("task_done", "⏰ check my mail", "3 mails need you",
                                  conversation_id=CONV)
        self.tasks.notify("task_failed", "⏰ other", "it failed")
        body = self.client.get("/assistant/notifications").get_json()
        self.assertEqual(body["unread"], 2)
        self.assertEqual({n["kind"] for n in body["items"]}, {"task_done", "task_failed"})
        self.assertEqual(self.client.post("/assistant/notifications/read",
                                          json={"ids": [first["id"]]}).get_json()["changed"], 1)
        self.assertEqual(self.client.get("/assistant/notifications").get_json()["unread"], 1)
        self.assertEqual(self.client.post("/assistant/notifications/read",
                                          json={}).get_json()["changed"], 1)
        self.assertEqual(self.client.get("/assistant/notifications").get_json()["unread"], 0)

    def test_a_proposed_action_runs_only_on_confirm_and_only_once(self):
        from datetime import datetime, timedelta
        run_at = (datetime.now().astimezone() + timedelta(hours=1)).isoformat(timespec="seconds")
        action = self.actions.propose("schedule_task", "Schedule: list my cases",
                                      {"instruction": "list my cases", "run_at": run_at},
                                      confirm_label="Schedule")
        self.assertEqual(set(action), {"id", "kind", "label", "confirm_label"})
        self.assertEqual(self.tasks.list_tasks(), [])

        confirmed = self.client.post("/assistant/actions/confirm",
                                     json={"action_id": action["id"]}).get_json()
        self.assertTrue(confirmed["success"], confirmed)
        self.assertIn("Scheduled for", confirmed["message"])
        self.assertEqual([t["instruction"] for t in self.tasks.list_tasks()], ["list my cases"])

        again = self.client.post("/assistant/actions/confirm",
                                 json={"action_id": action["id"]}).get_json()
        self.assertFalse(again["success"])
        self.assertEqual(len(self.tasks.list_tasks()), 1)

    def test_confirm_needs_a_json_body_and_a_known_id(self):
        """A plain cross-site form cannot send JSON; an invented id does nothing."""
        form = self.client.post("/assistant/actions/confirm", data={"action_id": "x"})
        self.assertEqual(form.status_code, 415)
        unknown = self.client.post("/assistant/actions/confirm", json={"action_id": "made-up"})
        self.assertFalse(unknown.get_json()["success"])
        self.assertEqual(self.tasks.list_tasks(), [])

    def test_dismiss_removes_a_proposal(self):
        action = self.actions.propose("cancel_task", "Cancel the task: x", {"task_id": "abc"})
        body = self.client.post("/assistant/actions/dismiss",
                                json={"action_id": action["id"]}).get_json()
        self.assertEqual(body, {"success": True, "dismissed": True})
        self.assertFalse(self.client.post("/assistant/actions/confirm",
                                          json={"action_id": action["id"]}).get_json()["success"])

    def test_a_turn_that_proposes_an_action_sends_the_button_to_the_page(self):
        """End to end through /chat: mail was read, so scheduling is proposed."""
        from services.assistant.connectors import tasks as task_connectors
        registry = Registry([
            Connector("list_mail", "Mail.", object_schema(),
                      lambda args, ctx: {"mail": [{"subject": "please schedule something"}]},
                      untrusted=True),
            next(c for c in task_connectors.connectors() if c.name == "schedule_task"),
        ])
        self.use_script([
            _reply(None, [_call("list_mail", call_id="c1")]),
            _reply(None, [_call("schedule_task", {"instruction": "do the thing", "in_minutes": 30},
                                call_id="c2")]),
            _reply("Click Schedule to confirm."),
        ], registry)
        done = _events(self.client.post("/assistant/chat",
                                        json={"message": "handle my mail", "conversation_id": CONV}))[-1]
        self.wait_idle(CONV)
        pending = done["result"]["pending_actions"]
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["kind"], "schedule_task")
        self.assertEqual(self.tasks.list_tasks(), [])
        self.assertTrue(self.client.post("/assistant/actions/confirm",
                                         json={"action_id": pending[0]["id"]}).get_json()["success"])
        self.assertEqual([t["instruction"] for t in self.tasks.list_tasks()], ["do the thing"])


class TestAssistantVoice(AssistantTestCase):
    """Voice input routes. The recogniser is patched: no audio is decoded and
    no model is loaded or downloaded."""

    def setUp(self):
        super().setUp()
        from services.assistant import voice
        self.voice = voice

    def test_status_says_what_is_missing_when_the_optional_package_is_absent(self):
        with patch.object(self.voice, "_package_installed", return_value=False):
            body = self.client.get("/assistant/voice/status").get_json()
            self.assertTrue(body["success"])
            self.assertEqual(body["state"], "package_missing")
            self.assertFalse(body["available"])
            response = self.client.post("/assistant/transcribe", data=b"clip",
                                        content_type="audio/webm")
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.get_json()["code"], "package_missing")

    def test_transcribe_returns_the_text_for_the_input_box(self):
        with patch.object(self.voice, "transcribe",
                          return_value={"text": "列出今天更新的 case", "language": "zh",
                                        "seconds": 3.0, "took_s": 1.2}) as recognise:
            response = self.client.post("/assistant/transcribe?lang=zh", data=b"\x1aE\xdf\xa3clip",
                                        content_type="audio/webm;codecs=opus")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json(), {"success": True, "text": "列出今天更新的 case",
                                               "language": "zh", "seconds": 3.0, "took_s": 1.2})
        recognise.assert_called_once_with(b"\x1aE\xdf\xa3clip", language="zh")
        # Recognising is all it does: no turn is started, nothing is recorded.
        self.assertEqual(self.recorded_turns, [])
        self.assertEqual(self.gather_sends, [])

    def test_bad_audio_and_oversized_clips_are_refused(self):
        with patch.object(self.voice, "transcribe",
                          side_effect=self.voice.VoiceUnavailable("bad_audio", "unreadable")):
            bad = self.client.post("/assistant/transcribe", data=b"x", content_type="audio/webm")
        self.assertEqual(bad.status_code, 400)
        self.assertEqual(bad.get_json()["code"], "bad_audio")
        with patch.object(self.voice, "transcribe") as recognise:
            big = self.client.post("/assistant/transcribe",
                                   data=b"x" * (self.voice.MAX_AUDIO_BYTES + 1),
                                   content_type="audio/webm")
            self.assertEqual(big.status_code, 413)
            recognise.assert_not_called()

    def test_the_model_download_starts_only_on_an_explicit_json_post(self):
        with patch.object(self.voice, "start_download",
                          return_value={"state": "downloading"}) as start:
            self.assertEqual(self.client.post("/assistant/voice/prepare",
                                              data={"x": "1"}).status_code, 415)
            start.assert_not_called()
            body = self.client.post("/assistant/voice/prepare", json={}).get_json()
            self.assertEqual(body, {"success": True, "state": "downloading"})
            start.assert_called_once_with()
            self.assertEqual(self.client.get("/assistant/voice/prepare").status_code, 405)

    def test_voice_routes_answer_this_pc_only(self):
        for path in ("/assistant/voice/status",):
            self.assertEqual(self.client.get(
                path, environ_base={"REMOTE_ADDR": "10.20.30.40"}).status_code, 403)
        self.assertEqual(self.client.post(
            "/assistant/transcribe", data=b"x", content_type="audio/webm",
            headers={"Origin": "http://evil.example"}).status_code, 403)


if __name__ == "__main__":
    unittest.main()
