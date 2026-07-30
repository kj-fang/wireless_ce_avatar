from __future__ import annotations

import ast
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SERVICE_PATH = (
    PROJECT_ROOT / "services" / "chatbot" / "agent" / "system.py"
)
MIXIN_ROOT = PROJECT_ROOT / "services" / "chatbot" / "agent"
MIXIN_MODULES = (
    "conversation.py",
    "log_scope.py",
    "report_quality.py",
    "skill_analysis.py",
    "tool_execution.py",
)

EXPECTED_MIXINS = {
    "LogScopeMixin": {
        "_ensure_raw_log_cache",
        "_log_has_date",
        "_merge_wrapped_lines_if_needed",
        "get_log_span_minutes",
        "_preprocess_raw_log_context",
        "_export_scoped_log_file",
        "_export_assembled_log_file",
        "_strip_line_number_prefix",
        "_extract_lines_from_filtered_blob",
        "_normalize_time_message",
        "_merge_lines_into_assembled_log",
        "_build_assembled_log_report",
        "_build_skill_focus_payload",
        "get_assembled_log_snapshot",
        "get_final_state_snapshot",
        "_resolve_context_span",
        "_clip_for_prompt",
        "_get_filtered_log_lines",
    },
    "SkillAnalysisMixin": {
        "_analyze_with_skill_prompt",
        "fetch_filtered_logs",
        "analyze_with_skill",
        "query_log_detail",
    },
    "ConversationMixin": {
        "chat",
        "_chat_simple",
        "_msg_role",
        "_msg_tool_call_ids",
        "_msg_tool_call_id",
        "_repair_tool_use_consistency",
        "_history_skeleton",
        "_chat_with_tools",
    },
    "ReportQualityMixin": {
        "_extract_issue_time",
        "_precheck_report_quality",
        "_review_report_quality",
        "_get_assembled_log_tail",
        "_build_outcome_injection",
        "_build_ace_workflow_block",
        "_build_ace_domain_block",
    },
    "ToolExecutionMixin": {
        "_build_analyze_system_prompt",
        "_invoke_tool",
        "_append_tool_message",
        "_handle_submit_tool_call",
        "_handle_fetch_tool_call",
        "_handle_snapshot_tool_call",
        "_handle_final_state_tool_call",
        "_handle_detail_tool_call",
        "_inject_analysis_into_history",
        "_build_tools",
    },
}

EXPECTED_AGENT_METHODS = {
    "_normalize_markers",
    "_line_matches_any",
    "__init__",
    "attach_ace",
    "adapt_from_feedback",
    "get_skill_names",
    "get_skill_descriptions",
    "reset_conversation",
    "apply_updated_skills",
    "prime_with_context",
}


def _classes(path: Path) -> dict[str, ast.ClassDef]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    return {
        node.name: node
        for node in tree.body
        if isinstance(node, ast.ClassDef)
    }


def _method_names(node: ast.ClassDef) -> set[str]:
    return {
        child.name
        for child in node.body
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef))
    }


def test_agent_composes_behavior_mixins_without_duplicate_ownership() -> None:
    service_classes = _classes(SERVICE_PATH)
    agent = service_classes["WifiLogAgentSystem"]
    base_names = {
        base.id
        for base in agent.bases
        if isinstance(base, ast.Name)
    }

    assert base_names == set(EXPECTED_MIXINS)
    assert _method_names(agent) == EXPECTED_AGENT_METHODS

    owned_methods = set(EXPECTED_AGENT_METHODS)
    for module_name in MIXIN_MODULES:
        module_path = MIXIN_ROOT / module_name
        for class_name, class_node in _classes(module_path).items():
            expected = EXPECTED_MIXINS[class_name]
            actual = _method_names(class_node)
            assert actual == expected
            assert owned_methods.isdisjoint(actual)
            owned_methods.update(actual)

    assert len(owned_methods) == 57


def test_message_accessors_remain_static_methods() -> None:
    conversation = _classes(MIXIN_ROOT / "conversation.py")["ConversationMixin"]
    static_methods = {
        node.name
        for node in conversation.body
        if isinstance(node, ast.FunctionDef)
        and any(
            isinstance(decorator, ast.Name) and decorator.id == "staticmethod"
            for decorator in node.decorator_list
        )
    }

    assert static_methods == {
        "_msg_role",
        "_msg_tool_call_ids",
        "_msg_tool_call_id",
    }
