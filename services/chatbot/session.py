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

_session_agent_stores: dict[str, MutableMapping[str, Any]] = {}


def register_session_agent_store(profile: str, instances: MutableMapping[str, Any]) -> None:
    """Register a profile-owned store without making services import routes."""
    _session_agent_stores[profile] = instances


def agents_for_session(session_id: str) -> list[Any]:
    """Existing clones only: never create an agent or reset its conversation."""
    agents = []
    if session_id:
        for instances in _session_agent_stores.values():
            agent = instances.get(session_id)
            if agent is not None and all(agent is not held for held in agents):
                agents.append(agent)
    return agents


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


def _kept_skills_for(agent):
    """``(skills, source, filename)`` kept for this agent's profile, or None.

    Only the explicit "keep for next time" counts. The skill editor's own
    active source lives on the conversation's agent and is meant to die with
    it; this is the one thing on disk that a fresh conversation adopts, and
    only because someone ticked a box saying so. Never raises: a missing or
    unreadable file just means the built-in skills, which is what every
    conversation used before keeping existed.
    """
    from pathlib import Path

    from utils import speclets_utils

    try:
        profile = getattr(getattr(agent, "capabilities", None), "profile", "")
        path = speclets_utils.kept_skills(profile) if profile else None
        if not path:
            return None
        from services.chatbot.engine.system import load_skills_from_yaml

        skills = load_skills_from_yaml(path)
        return (skills, "user", Path(path).name) if skills else None
    except Exception:
        print("[skills] could not load the kept skill set; "
              "this conversation uses the built-in one.")
        return None


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
            agent = self._get_agent()
            agent.reset_conversation()
            # /reset is a new conversation, unlike an internal context reset
            # during set_log/prepare. Do not carry experimental prompts or
            # skill imports into it.
            if hasattr(agent, "prompt_overrides"):
                agent.prompt_overrides.clear()
            if hasattr(agent, "_initial_skills"):
                # "The skills this conversation starts from" is the kept set
                # when there is one, and the built-in set otherwise -- the same
                # order conversation_speclet() uses for the prompt, and for the
                # same reason: a kept prompt names its skills, and fetch_
                # filtered_logs takes an enum of the ones actually loaded, so
                # resetting to the built-in set here would leave the agent
                # holding instructions to call skills it no longer has.
                skills, source, filename = (
                    dict(agent._initial_skills), "cloud", "")
                kept = _kept_skills_for(agent)
                if kept:
                    skills, source, filename = kept
                agent.apply_updated_skills(skills)
                agent._session_skill_source = source
                agent._session_skill_filename = filename
                agent._session_skill_path = None
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
