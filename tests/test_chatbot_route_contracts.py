from __future__ import annotations

import pytest
from flask import Flask

from configs.chatbot_ui import BT_UI, LOG_CHATBOT_UI, WIFI_UI
from services.chatbot.factory import (
    ChatbotBlueprintConfig,
    create_chatbot_blueprint,
    enabled_route_specs,
    route_contract,
)

FULL_AGENT_ROUTES = {
    "/": {"GET"},
    "/back_to_avatar": {"GET"},
    "/browse": {"GET"},
    "/browse_dir": {"GET"},
    "/browse_yaml": {"GET"},
    "/chat": {"POST"},
    "/chat/stop": {"POST"},
    "/delete_local_skill": {"POST"},
    "/find_best_log": {"POST"},
    "/get_issue_context": {"GET"},
    "/history/delete": {"POST"},
    "/history/get": {"GET"},
    "/history/list": {"GET"},
    "/history/load": {"POST"},
    "/history/pin": {"POST"},
    "/history/rename": {"POST"},
    "/history/stream": {"GET"},
    "/load_local_skills_yaml": {"GET"},
    "/load_skills_yaml": {"POST"},
    "/prepare": {"POST"},
    "/refresh_cloud_baseline": {"POST"},
    "/reload_from_shared": {"POST"},
    "/reload_skills": {"POST"},
    "/reset": {"POST"},
    "/save_local_skills_yaml": {"POST"},
    "/set_log": {"POST"},
    "/skills": {"GET"},
    "/skills_yaml_status": {"GET"},
    "/skills_yaml_use_cloud": {"POST"},
    "/skills_yaml_use_user": {"POST"},
    "/suggest_issue_times": {"POST"},
    "/upload_modified_yaml": {"POST"},
}

NW_AGENT_ROUTES = {
    "/": {"GET"},
    "/analyze_sleepstudy": {"POST"},
    "/browse": {"GET"},
    "/browse_dir": {"GET"},
    "/browse_yaml": {"GET"},
    "/chat": {"POST"},
    "/chat/stop": {"POST"},
    "/find_best_log": {"POST"},
    "/get_issue_context": {"GET"},
    "/load_skills_yaml": {"POST"},
    "/prepare": {"POST"},
    "/reload_from_shared": {"POST"},
    "/reload_skills": {"POST"},
    "/reset": {"POST"},
    "/set_log": {"POST"},
    "/set_log_sleepstudy": {"POST"},
    "/skills": {"GET"},
}

PROFILE_CONTRACTS = {
    "bt": (BT_UI, "/bt_chatbot", FULL_AGENT_ROUTES),
    "wifi": (LOG_CHATBOT_UI, "/log_chatbot", FULL_AGENT_ROUTES),
    "nw": (WIFI_UI, "/nw_analysis", NW_AGENT_ROUTES),
}


@pytest.mark.parametrize("profile", ["bt", "wifi", "nw"])
def test_profile_route_inventory_is_stable(profile: str) -> None:
    ui, _, expected_routes = PROFILE_CONTRACTS[profile]
    capabilities = {
        key for key, enabled in ui["features"].items() if enabled
    }

    assert route_contract(capabilities) == expected_routes


def test_bt_and_wifi_full_agents_keep_the_same_public_route_contract() -> None:
    bt_routes = route_contract({
        key for key, enabled in BT_UI["features"].items() if enabled
    })
    wifi_routes = route_contract({
        key for key, enabled in LOG_CHATBOT_UI["features"].items() if enabled
    })

    assert bt_routes == wifi_routes


def test_nw_sleepstudy_routes_are_an_explicit_capability() -> None:
    full_routes = route_contract({
        key for key, enabled in LOG_CHATBOT_UI["features"].items() if enabled
    })
    nw_routes = route_contract({
        key for key, enabled in WIFI_UI["features"].items() if enabled
    })

    assert {"/set_log_sleepstudy", "/analyze_sleepstudy"} <= nw_routes.keys()
    assert {"/set_log_sleepstudy", "/analyze_sleepstudy"}.isdisjoint(full_routes)


def test_factory_registers_contract_and_shared_use_cases() -> None:
    class FakeAgent:
        def __init__(self) -> None:
            self.was_reset = False

        def reset_conversation(self) -> None:
            self.was_reset = True

        def get_skill_descriptions(self):
            return [{"id": "shared-test-skill"}]

    agent = FakeAgent()
    capabilities: set[str] = set()
    handlers = {}
    for spec in enabled_route_specs(capabilities):
        if spec.endpoint in {"reset", "get_skills"}:
            continue

        def view():
            return {"success": True}

        view.__name__ = f"test_{spec.endpoint}"
        handlers[spec.endpoint] = view

    blueprint = create_chatbot_blueprint(ChatbotBlueprintConfig(
        name="contract_test",
        import_name=__name__,
        url_prefix="/contract",
        capabilities=capabilities,
        get_agent=lambda: agent,
        handlers=handlers,
    ))
    app = Flask(__name__)
    app.register_blueprint(blueprint)
    app.config["TESTING"] = True

    rules = {
        rule.rule.removeprefix("/contract"): (
            set(rule.methods) - {"HEAD", "OPTIONS"}
        )
        for rule in app.url_map.iter_rules()
        if rule.rule.startswith("/contract")
    }
    assert rules == route_contract(capabilities)

    client = app.test_client()
    reset_response = client.post("/contract/reset")
    skills_response = client.get("/contract/skills")

    assert reset_response.status_code == 200
    assert reset_response.get_json() == {
        "success": True,
        "message": "Conversation reset.",
    }
    assert agent.was_reset is True
    assert skills_response.status_code == 200
    assert skills_response.get_json() == {
        "success": True,
        "skills": [{"id": "shared-test-skill"}],
    }


def test_shared_use_case_error_schema_is_stable() -> None:
    def failing_agent():
        raise RuntimeError("agent unavailable")

    capabilities: set[str] = set()
    handlers = {}
    for spec in enabled_route_specs(capabilities):
        if spec.endpoint in {"reset", "get_skills"}:
            continue

        def view():
            return {"success": True}

        view.__name__ = f"test_error_{spec.endpoint}"
        handlers[spec.endpoint] = view

    blueprint = create_chatbot_blueprint(ChatbotBlueprintConfig(
        name="contract_error_test",
        import_name=__name__,
        url_prefix="/contract-error",
        capabilities=capabilities,
        get_agent=failing_agent,
        handlers=handlers,
    ))
    app = Flask(__name__)
    app.register_blueprint(blueprint)
    app.config["TESTING"] = True
    client = app.test_client()

    for method, path in (
        (client.post, "/contract-error/reset"),
        (client.get, "/contract-error/skills"),
    ):
        response = method(path)
        assert response.status_code == 500
        assert response.get_json() == {
            "success": False,
            "error": "agent unavailable",
        }
