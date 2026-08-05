from __future__ import annotations

import ast
from datetime import datetime
from pathlib import Path

from services.chatbot.agent.network_experience import NwAnalysisAgentSystem
from services.chatbot.agent.bluetooth import BtLogAgentSystem
from services.chatbot.agent.system import NW_AGENT_POLICY, Skill, WifiLogAgentSystem


PROJECT_ROOT = Path(__file__).resolve().parents[1]
NW_AGENT_PATH = PROJECT_ROOT / "services" / "chatbot" / "agent" / "network_experience.py"


def _agent() -> NwAnalysisAgentSystem:
    skill = Skill(
        name="Connectivity",
        description="Connectivity diagnostics",
        keywords=["connect"],
        tat_path=None,
        expert_rules="Inspect connectivity evidence.",
    )
    return NwAnalysisAgentSystem(client=object(), skills={skill.name: skill})


def _tool_functions(agent: NwAnalysisAgentSystem) -> dict[str, dict]:
    return {
        tool["function"]["name"]: tool["function"]
        for tool in agent._build_tools()
    }


def test_nw_tool_contract_excludes_wifi_softap_and_ace_report_fields() -> None:
    tools = _tool_functions(_agent())

    assert set(tools) == {
        "fetch_filtered_logs",
        "lookup_assert_code",
        "submit_final_report",
    }
    report_properties = tools["submit_final_report"]["parameters"]["properties"]
    assert "applied_bullet_ids" not in report_properties
    assert "flagged_bullet_ids" not in report_properties


def test_three_backend_profiles_expose_only_their_capability_tools() -> None:
    skill = _agent().skills
    profiles = {
        "wifi": WifiLogAgentSystem(client=object(), skills=skill),
        "nw": NwAnalysisAgentSystem(client=object(), skills=skill),
        "bt": BtLogAgentSystem(client=object(), skills=skill),
    }
    expected = {
        "wifi": {
            "fetch_filtered_logs",
            "lookup_assert_code",
            "softAP_supported_channel",
            "submit_final_report",
        },
        "nw": {
            "fetch_filtered_logs",
            "lookup_assert_code",
            "submit_final_report",
        },
        "bt": {"fetch_filtered_logs", "submit_final_report"},
    }

    for profile, agent in profiles.items():
        assert set(_tool_functions(agent)) == expected[profile]


def test_legacy_nw_service_module_has_been_removed() -> None:
    assert not (PROJECT_ROOT / "services" / "nw_analysis_service.py").exists()


def test_nw_agent_is_a_thin_shared_engine_profile() -> None:
    assert issubclass(NwAnalysisAgentSystem, WifiLogAgentSystem)
    assert NwAnalysisAgentSystem.CAPABILITY_POLICY is NW_AGENT_POLICY

    tree = ast.parse(NW_AGENT_PATH.read_text(encoding="utf-8"))
    class_node = next(
        node for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "NwAnalysisAgentSystem"
    )
    owned_methods = {
        node.name for node in class_node.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    assert owned_methods == {
        "fetch_filtered_logs",
        "_build_analyze_system_prompt",
        "prime_with_context",
    }


def test_nw_policy_owns_large_engine_differences() -> None:
    policy = NW_AGENT_POLICY
    assert policy.disabled_tools == frozenset({"softAP_supported_channel"})
    assert policy.ace_playbooks is False
    assert policy.cooperative_cancellation is False
    assert policy.repair_tool_history is False
    assert policy.emit_step_token_usage is True
    assert policy.emit_fetch_previews is True
    assert policy.primed_issue_time_source == "attachment_time"
    assert policy.compact_issue_time_notice is True
    assert policy.diagnose_history_on_llm_error is False
    assert policy.scope_time_only_logs is False
    assert policy.configurable_issue_window is False
    assert policy.full_scope_for_undated_logs is False


def test_nw_prescan_keeps_its_fixed_five_minute_window(tmp_path) -> None:
    log_path = tmp_path / "dated.log"
    log_path.write_text(
        "\n".join(
            [
                "08/04/2026-11:59:59.000 before",
                "08/04/2026-12:04:00.000 inside",
                "08/04/2026-12:06:00.000 outside",
            ]
        ),
        encoding="utf-8",
    )
    agent = _agent()
    agent.current_log_path = str(log_path)
    agent._raw_log_cache = log_path.read_text(encoding="utf-8").splitlines()
    agent._raw_log_cache_path = str(log_path)
    agent.issue_time = datetime(2026, 8, 4, 12, 0, 0)
    agent.issue_time_window_minutes = 1

    agent._preprocess_raw_log_context()

    assert any("inside" in line for line in agent._issue_time_window_lines)
    assert all("outside" not in line for line in agent._issue_time_window_lines)


def test_nw_prescan_does_not_expand_undated_logs_to_full_scope(tmp_path) -> None:
    log_path = tmp_path / "undated.log"
    log_path.write_text("09:59:00 first\n10:00:00 second", encoding="utf-8")
    agent = _agent()
    agent.current_log_path = str(log_path)
    agent._raw_log_cache = log_path.read_text(encoding="utf-8").splitlines()
    agent._raw_log_cache_path = str(log_path)
    agent.issue_time = datetime(2026, 8, 4, 10, 0, 0)

    agent._preprocess_raw_log_context()

    assert agent._scoped_log_lines == []


def test_nw_prime_contract_parses_time_only_without_timezone_conversion() -> None:
    agent = _agent()

    agent.prime_with_context(attachment_time="16:45:30")

    assert agent.issue_time is not None
    assert agent.issue_time.time() == datetime.strptime("16:45:30", "%H:%M:%S").time()
    assert agent._issue_time_time_only is True
