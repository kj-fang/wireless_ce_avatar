from __future__ import annotations

from pathlib import Path

import pytest
from jinja2 import Environment, FileSystemLoader, select_autoescape

from configs.chatbot_ui import BT_UI, LOG_CHATBOT_UI, WIFI_UI


PROJECT_ROOT = Path(__file__).resolve().parents[1]
TEMPLATE_ROOT = PROJECT_ROOT / "templates"

PROFILES = {
    "bt": BT_UI,
    "wifi": LOG_CHATBOT_UI,
    "nw": WIFI_UI,
}


@pytest.fixture(scope="module")
def jinja_environment() -> Environment:
    return Environment(
        loader=FileSystemLoader(TEMPLATE_ROOT),
        autoescape=select_autoescape(("html", "xml")),
    )


@pytest.mark.parametrize("profile", ["bt", "wifi", "nw"])
def test_profile_has_complete_shared_shell_contract(profile: str) -> None:
    ui = PROFILES[profile]

    assert ui["api"].startswith("/")
    assert ui["domain"] in {"bt", "wifi"}
    assert ui["title"]
    assert ui["runtime_strategy_script"] or profile == "nw"
    assert set(ui["features"]) == {
        "sidebar_toggle",
        "issue_time",
        "feedback",
        "history",
        "skill_editor",
        "sleepstudy",
    }
    assert set(ui["issue_time"]) == {
        "strategy_script",
        "allow_time_only",
        "customer_timezone",
        "event_refinement",
        "multi_select",
        "prompt_title",
        "prompt_body",
    }
    assert set(ui["template_parts"]) == {
        "sidebar",
        "runtime",
    }
    assert isinstance(ui["stylesheets"], list)
    for stylesheet in ui["stylesheets"]:
        assert stylesheet.startswith("/static/")
        assert (PROJECT_ROOT / stylesheet.removeprefix("/")).is_file()
    assert ui["profile_script"].startswith("/static/")
    assert (PROJECT_ROOT / ui["profile_script"].removeprefix("/")).is_file()


@pytest.mark.parametrize("profile", ["bt", "wifi", "nw"])
def test_domain_template_renders_one_shared_chat_input(
    profile: str,
    jinja_environment: Environment,
) -> None:
    ui = PROFILES[profile]

    html = jinja_environment.get_template("chatbot/page.html").render(ui=ui)

    assert html.count('id="user-input"') == 1
    assert html.count("window.CHATBOT =") == 1
    assert f'data-domain="{ui["domain"]}"' in html
    assert "Reset Conversation" not in html
    assert ">Actions<" not in html


def test_bt_uses_bt_strategies_and_full_capabilities(
    jinja_environment: Environment,
) -> None:
    html = jinja_environment.get_template("chatbot/page.html").render(ui=BT_UI)

    assert "/static/chatbot/js/strategies/bt-issue-time.js" in html
    assert "/static/chatbot/js/strategies/bt-chat-runtime.js" in html
    assert "/static/chatbot/js/features/issue-time-controller.js" in html
    assert "/static/chatbot/js/controllers.js" in html
    assert 'id="sleepstudy-path-input"' not in html


def test_full_wifi_uses_wifi_strategies_and_full_capabilities(
    jinja_environment: Environment,
) -> None:
    html = jinja_environment.get_template("chatbot/page.html").render(ui=LOG_CHATBOT_UI)

    assert "/static/chatbot/js/strategies/wifi-issue-time.js" in html
    assert "/static/chatbot/js/strategies/wifi-chat-runtime.js" in html
    assert "/static/chatbot/js/features/issue-time-controller.js" in html
    assert "/static/chatbot/js/controllers.js" in html
    assert 'id="sleepstudy-path-input"' not in html


def test_nw_exposes_sleepstudy_without_full_agent_controllers(
    jinja_environment: Environment,
) -> None:
    html = jinja_environment.get_template("chatbot/page.html").render(ui=WIFI_UI)

    assert 'id="sleepstudy-path-input"' in html
    assert "/static/chatbot/js/features/issue-time-controller.js" not in html
    assert "/static/chatbot/js/controllers.js" not in html
    assert "/static/chatbot/js/strategies/" not in html


def test_feature_partials_do_not_branch_on_domain_name() -> None:
    feature_root = TEMPLATE_ROOT / "chatbot" / "features"
    source = "\n".join(
        path.read_text(encoding="utf-8")
        for path in sorted(feature_root.glob("*.html"))
    )

    assert "ui.domain" not in source
    assert "CHATBOT.domain" not in source


def test_legacy_domain_page_templates_are_removed() -> None:
    assert not (TEMPLATE_ROOT / "bt_chatbot.html").exists()
    assert not (TEMPLATE_ROOT / "log_chatbot.html").exists()
    assert not (TEMPLATE_ROOT / "NW_analysis.html").exists()
