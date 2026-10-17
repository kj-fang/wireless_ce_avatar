"""Prompt and skill experiments must not bleed into the next conversation."""

from types import SimpleNamespace

from flask import Flask

from services.chatbot.engine.speclet_defaults import conversation_speclet, default_speclet
from services.chatbot.prompt_editor import PromptEditorContext, build_prompt_editor_handlers
from services.chatbot.session import ChatbotUseCases
from services.skill_editor.controller import (
    build_profile_yaml_helpers,
    delete_local_skill,
    refresh_cloud_baseline_route,
    save_local_skills_yaml,
    skills_yaml_use_cloud,
    skills_yaml_use_user,
)
from utils import speclets_utils as sp


class FakeAgent:
    def __init__(self, profile="linux"):
        self.capabilities = SimpleNamespace(profile=profile)
        self.prompt_overrides = {}
        self._initial_skills = {"original": object()}
        self.skills = dict(self._initial_skills)

    def reset_conversation(self):
        pass

    def apply_updated_skills(self, skills):
        self.skills = skills

    def get_skill_descriptions(self):
        return [{"name": name, "description": ""} for name in self.skills]


def test_prompt_bundle_is_live_only_in_the_current_conversation(tmp_path, monkeypatch):
    shared, user = tmp_path / "shared", tmp_path / "user"
    shared.mkdir()
    user.mkdir()
    monkeypatch.setattr(sp, "local_speclets_dir", lambda: shared)
    monkeypatch.setattr(sp, "local_user_speclets_dir", lambda: user)
    app = Flask(__name__)
    first, second = FakeAgent(), FakeAgent()
    handlers = build_prompt_editor_handlers(
        PromptEditorContext("linux", default_speclet, get_agent=lambda: first,
                            load_skills_from_yaml=lambda path: {"bluetooth": object()})
    )
    bundle = "## prompt\n\nBluetooth diagnosis\n\n## report\n\nBT report\n\n## suggest_time\n\nBT anchor"

    with app.test_request_context(json={"doc": "linux_agent", "text": bundle,
                                        "skill_mode": "replace", "skill_yaml_path": "bt.yaml"}):
        assert handlers["prompts_save"]().get_json()["success"]
    assert conversation_speclet(first, "prompt") == "Bluetooth diagnosis"
    assert conversation_speclet(first, "report") == "BT report"
    assert conversation_speclet(first, "suggest_time") == "BT anchor"
    assert conversation_speclet(second, "prompt") == default_speclet("linux", "prompt")
    assert list(first.skills) == ["bluetooth"]
    assert list(second.skills) == ["original"]

    with app.test_request_context("/prompts/status"):
        payload = handlers["prompts_status"]().get_json()
    assert payload["conversation_only"]
    assert len(payload["documents"]) == 4
    assert all(doc["profile"] == "linux" for doc in payload["documents"])

    with app.test_request_context("/prompts/get?doc=linux_agent"):
        assert "Bluetooth diagnosis" in handlers["prompts_get"]().get_json()["live"]
    with app.test_request_context("/prompts/get?doc=wifi_agent"):
        response, status = handlers["prompts_get"]()
        assert status == 400


def test_new_conversation_restores_original_prompt_and_skills():
    agent = FakeAgent()
    agent.prompt_overrides["prompt"] = "custom"
    agent.apply_updated_skills({"bluetooth": object()})
    result = ChatbotUseCases(lambda: agent).reset_conversation()
    assert result.payload["success"]
    assert conversation_speclet(agent, "prompt") == default_speclet("linux", "prompt")
    assert agent.skills == agent._initial_skills

    def busy():
        raise RuntimeError("Analysis running")

    context = SimpleNamespace(
        ensure_editable=busy,
        sanitise_skill_payload=lambda _: ({"one": {}}, None),
    )
    app = Flask(__name__)
    for handler in (skills_yaml_use_cloud, skills_yaml_use_user,
                    refresh_cloud_baseline_route, save_local_skills_yaml,
                    delete_local_skill):
        with app.test_request_context(json={"skill_key": "one", "skills": {"one": {}}}):
            response, status = handler(context)
            assert status == 409
            assert "Analysis running" in response.get_json()["error"]


def test_skill_file_activation_changes_only_session_agent(tmp_path):
    session_agent = FakeAgent()
    base_agent = FakeAgent()
    selected = {"bt": object()}
    yaml_file = tmp_path / "bluetooth.yaml"
    yaml_file.write_text("placeholder", encoding="utf-8")
    helpers = build_profile_yaml_helpers(
        user_local_dir=lambda: tmp_path,
        today_yaml_filename=lambda: "skills.yaml",
        user_yaml_prefix="skills_",
        latest_cloud_baseline=lambda: (None, None),
        latest_user_yaml=lambda: (None, None),
        write_yaml_file=lambda *args: None,
        load_skills_from_yaml=lambda path: selected,
        get_agent=lambda: session_agent,
        agent_config_attr="log_chatbot_agent",
    )
    helpers["activate_yaml"](yaml_file)
    assert session_agent.skills is selected
    assert base_agent.skills == base_agent._initial_skills
    assert session_agent._session_skill_path == yaml_file
