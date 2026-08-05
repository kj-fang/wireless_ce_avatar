from __future__ import annotations

import json
import uuid

from flask import Flask, session

from services.chatbot import job_runtime
from services.chatbot.session import resume_agent_for


def _decode_sse(frame: str) -> dict:
    assert frame.startswith("data: ")
    return json.loads(frame.removeprefix("data: ").strip())


def test_completed_job_replays_steps_before_the_terminal_result() -> None:
    conversation_id = str(uuid.uuid4())
    job = job_runtime.start_job(
        conversation_id=conversation_id,
        turn_id="turn-1",
        title="Connectivity issue",
        agent=object(),
        domain="wifi",
    )
    job_runtime.publish_step(job, {"message": "Inspecting log"})
    job_runtime.finish_job(job, {"summary": "Completed"})

    events = [_decode_sse(frame) for frame in job_runtime.job_sse(job)]

    assert events == [
        {
            "type": "step",
            "step": {"message": "Inspecting log"},
        },
        {
            "type": "done",
            "turn_id": "turn-1",
            "conversation_id": conversation_id,
            "result": {"summary": "Completed"},
        },
    ]


def test_completed_job_agent_is_adopted_by_the_flask_session() -> None:
    app = Flask(__name__)
    app.secret_key = "job-runtime-contract"
    conversation_id = str(uuid.uuid4())
    completed_agent = object()
    fallback_agent = object()
    instances: dict[str, object] = {}
    job = job_runtime.start_job(
        conversation_id=conversation_id,
        turn_id="turn-2",
        title="Bluetooth issue",
        agent=completed_agent,
        domain="bt",
    )
    job_runtime.finish_job(job, {"summary": "Completed"})

    with app.test_request_context("/"):
        adopted = resume_agent_for(
            conversation_id,
            instances,
            lambda: fallback_agent,
        )
        session_id = session["chatbot_session_id"]

    assert adopted is completed_agent
    assert instances[session_id] is completed_agent
