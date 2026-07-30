"""Framework-independent application use cases for every chatbot profile."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable


@dataclass(frozen=True)
class UseCaseResult:
    payload: dict[str, Any]
    status: int = 200


class ChatbotUseCases:
    """Small application layer around an injected domain-agent provider.

    The class deliberately knows nothing about Flask, sessions, templates, or
    whether the agent handles BT, Wi-Fi, or NW logs. Domain policy remains in
    the adapter; only genuinely identical operations live here.
    """

    def __init__(self, get_agent: Callable[[], Any]) -> None:
        self._get_agent = get_agent

    def reset_conversation(self) -> UseCaseResult:
        try:
            self._get_agent().reset_conversation()
            return UseCaseResult({
                "success": True,
                "message": "Conversation reset.",
            })
        except Exception as exc:
            return UseCaseResult({
                "success": False,
                "error": str(exc),
            }, status=500)

    def get_skills(self) -> UseCaseResult:
        try:
            skills = self._get_agent().get_skill_descriptions()
            return UseCaseResult({
                "success": True,
                "skills": skills,
            })
        except Exception as exc:
            return UseCaseResult({
                "success": False,
                "error": str(exc),
            }, status=500)
