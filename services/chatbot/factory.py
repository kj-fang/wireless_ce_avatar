"""Capability-driven Flask Blueprint factory for chatbot profiles."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Collection, Mapping

from flask import Blueprint, jsonify

from services.chatbot.session import (
    ChatbotUseCases,
    UseCaseResult,
    choose_skills_yaml,
)


ViewHandler = Callable[..., Any]


@dataclass(frozen=True)
class RouteSpec:
    rule: str
    endpoint: str
    methods: tuple[str, ...]
    capability: str | None = None


# One canonical public HTTP contract. A profile enables optional groups through
# data in configs/chatbot_ui.py; route modules no longer repeat decorators.
ROUTE_SPECS: tuple[RouteSpec, ...] = (
    RouteSpec("/", "index", ("GET",)),
    RouteSpec("/browse", "browse", ("GET",)),
    RouteSpec("/set_log", "set_log", ("POST",)),
    RouteSpec("/chat", "chat", ("POST",)),
    RouteSpec("/chat/stop", "chat_stop", ("POST",)),
    RouteSpec("/reset", "reset", ("POST",)),
    RouteSpec("/prepare", "prepare", ("POST",)),
    RouteSpec("/browse_yaml", "browse_yaml", ("GET",)),
    RouteSpec("/load_skills_yaml", "load_skills_yaml_route", ("POST",)),
    RouteSpec("/reload_from_shared", "reload_from_shared", ("POST",)),
    RouteSpec("/skills", "get_skills", ("GET",)),
    RouteSpec("/get_issue_context", "get_issue_context", ("GET",)),
    RouteSpec("/find_best_log", "find_best_log", ("POST",)),
    RouteSpec(
        "/suggest_issue_times",
        "suggest_issue_times",
        ("POST",),
        "issue_time",
    ),
    RouteSpec("/history/list", "history_list", ("GET",), "history"),
    RouteSpec("/history/stream", "history_stream", ("GET",), "history"),
    RouteSpec("/history/get", "history_get", ("GET",), "history"),
    RouteSpec("/history/delete", "history_delete", ("POST",), "history"),
    RouteSpec("/history/rename", "history_rename", ("POST",), "history"),
    RouteSpec("/history/pin", "history_pin", ("POST",), "history"),
    RouteSpec("/history/load", "history_load", ("POST",), "history"),
    # Session teardown, not a history feature: every profile needs it so a
    # page reloaded after "Back to Avatar" starts clean.
    RouteSpec("/back_to_avatar", "back_to_avatar", ("GET",)),
    RouteSpec(
        "/skills_yaml_status",
        "skills_yaml_status",
        ("GET",),
        "skill_editor",
    ),
    RouteSpec(
        "/skills_yaml_use_cloud",
        "skills_yaml_use_cloud",
        ("POST",),
        "skill_editor",
    ),
    RouteSpec(
        "/skills_yaml_use_user",
        "skills_yaml_use_user",
        ("POST",),
        "skill_editor",
    ),
    RouteSpec(
        "/refresh_cloud_baseline",
        "refresh_cloud_baseline_route",
        ("POST",),
        "skill_editor",
    ),
    RouteSpec(
        "/load_local_skills_yaml",
        "load_local_skills_yaml",
        ("GET",),
        "skill_editor",
    ),
    RouteSpec(
        "/save_local_skills_yaml",
        "save_local_skills_yaml",
        ("POST",),
        "skill_editor",
    ),
    RouteSpec(
        "/delete_local_skill",
        "delete_local_skill",
        ("POST",),
        "skill_editor",
    ),
    RouteSpec(
        "/upload_modified_yaml",
        "upload_modified_yaml",
        ("POST",),
        "skill_editor",
    ),
    RouteSpec(
        "/set_log_sleepstudy",
        "set_log_sleepstudy",
        ("POST",),
        "sleepstudy",
    ),
    RouteSpec(
        "/analyze_sleepstudy",
        "analyze_sleepstudy",
        ("POST",),
        "sleepstudy",
    ),
)


@dataclass(frozen=True)
class ChatbotBlueprintConfig:
    name: str
    import_name: str
    url_prefix: str
    capabilities: Collection[str]
    get_agent: Callable[[], Any]
    handlers: Mapping[str, ViewHandler]


def enabled_route_specs(capabilities: Collection[str]) -> tuple[RouteSpec, ...]:
    enabled = set(capabilities)
    return tuple(
        spec
        for spec in ROUTE_SPECS
        if spec.capability is None or spec.capability in enabled
    )


def route_contract(
    capabilities: Collection[str],
) -> dict[str, set[str]]:
    """Expose the generated route contract for tests and diagnostics."""
    return {
        spec.rule: set(spec.methods)
        for spec in enabled_route_specs(capabilities)
    }


def handler_map(
    namespace: Mapping[str, Any],
    capabilities: Collection[str],
) -> dict[str, ViewHandler]:
    """Build and validate an adapter map from a domain module namespace."""
    shared_endpoints = {
        "reset",
        "get_skills",
        "browse_yaml",
    }
    required = {
        spec.endpoint
        for spec in enabled_route_specs(capabilities)
        if spec.endpoint not in shared_endpoints
    }
    missing = sorted(name for name in required if not callable(namespace.get(name)))
    if missing:
        raise RuntimeError(
            "Chatbot adapter is missing route handlers: " + ", ".join(missing)
        )
    return {name: namespace[name] for name in required}


def _json_result(result: UseCaseResult):
    response = jsonify(result.payload)
    response.status_code = result.status
    return response


def create_chatbot_blueprint(config: ChatbotBlueprintConfig) -> Blueprint:
    """Create one Blueprint from a route contract and a thin domain adapter."""
    blueprint = Blueprint(
        config.name,
        config.import_name,
        url_prefix=config.url_prefix,
    )
    use_cases = ChatbotUseCases(config.get_agent)

    def reset():
        return _json_result(use_cases.reset_conversation())

    def get_skills():
        return _json_result(use_cases.get_skills())

    def browse_yaml():
        return jsonify({
            "success": True,
            "path": choose_skills_yaml(),
        })

    shared_handlers: dict[str, ViewHandler] = {
        "reset": reset,
        "get_skills": get_skills,
        "browse_yaml": browse_yaml,
    }

    for spec in enabled_route_specs(config.capabilities):
        view = shared_handlers.get(spec.endpoint) or config.handlers.get(spec.endpoint)
        if view is None:
            raise RuntimeError(
                f"Profile '{config.name}' has no handler for '{spec.endpoint}'."
            )
        blueprint.add_url_rule(
            spec.rule,
            endpoint=spec.endpoint,
            view_func=view,
            methods=list(spec.methods),
        )

    return blueprint
