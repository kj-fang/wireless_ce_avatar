"""Flask-session helpers, framework-free use cases, and local file dialogs.

Three tiny concerns that only ever serve the chatbot blueprints, kept in one
module rather than three files across two packages.
"""

from __future__ import annotations

import threading
import uuid
from collections.abc import Callable, MutableMapping
from dataclasses import dataclass
from typing import Any

from flask import session

from services.chatbot import job_runtime


# --------------------------------------------------------------------------
# Flask session coordination shared by the full BT and Wi-Fi agents
# --------------------------------------------------------------------------


def ensure_feedback_conversation_id(*, rotate: bool = False) -> str:
    if rotate or not session.get("feedback_conversation_id"):
        session["feedback_conversation_id"] = str(uuid.uuid4())
    return session["feedback_conversation_id"]


def resume_agent_for(
    conversation_id: str,
    instances: MutableMapping[str, Any],
    get_agent: Callable[[], Any],
) -> Any:
    """Adopt a completed job's detached agent, or return the session agent."""
    job = job_runtime.get_job(conversation_id)
    if (
        job is not None
        and getattr(job, "agent", None) is not None
        and job.status != "running"
    ):
        session_id = session.get("chatbot_session_id")
        if not session_id:
            session_id = str(uuid.uuid4())
            session["chatbot_session_id"] = session_id
        instances[session_id] = job.agent
        return job.agent
    return get_agent()


# --------------------------------------------------------------------------
# Framework-independent application use cases for every chatbot profile
# --------------------------------------------------------------------------


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


# --------------------------------------------------------------------------
# Tkinter file-dialog adapter for local desktop chatbot profiles
# --------------------------------------------------------------------------


def _run_dialog(select: Callable[[], str]) -> str:
    import tkinter as tk

    result = {"path": ""}

    def open_dialog() -> None:
        root = tk.Tk()
        root.withdraw()
        root.wm_attributes("-topmost", True)
        try:
            result["path"] = select() or ""
        finally:
            root.destroy()

    thread = threading.Thread(target=open_dialog)
    thread.start()
    thread.join(timeout=60)
    return result["path"]


def choose_skills_yaml() -> str:
    from tkinter import filedialog

    return _run_dialog(
        lambda: filedialog.askopenfilename(
            title="Select skills YAML file",
            filetypes=[
                ("YAML files", "*.yaml *.yml"),
                ("All files", "*.*"),
            ],
        )
    )
