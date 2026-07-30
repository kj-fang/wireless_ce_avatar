"""Flask session coordination shared by full BT and Wi-Fi agents."""

from __future__ import annotations

import uuid
from collections.abc import Callable, MutableMapping
from typing import Any

from flask import session

from services.chatbot import job_runtime


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
