"""
In-memory registry of running / recently-finished chat analyses ("jobs").

Why this exists
---------------
A tools-mode analysis runs on a background thread and streams steps over SSE.
Originally the stream was tied 1:1 to the /chat HTTP request and to the single
per-session agent, so navigating away (e.g. clicking a History item) reset the
shared agent mid-run and there was no way to re-attach to the live progress.

A ``ChatJob`` decouples the running analysis from any one HTTP connection:

  * The job owns its own agent instance (detached from the session slot at
    start) so later session mutations can't corrupt the in-flight run.
  * Every step is appended to ``job.steps`` (a buffer) AND fanned out to any
    number of live subscribers. A reconnecting client replays the buffer and
    then follows new steps until the job reaches a terminal state.
  * The job survives the original request ending — the worker thread writes to
    the buffer regardless of whether anyone is currently reading.

Design mirrors the rest of the chatbot services: files/DB-free, never raises
out of the public helpers, per-job locking so concurrent SSE threads don't
interleave.
"""

from __future__ import annotations

import queue as _queue
import threading
from datetime import datetime, timedelta
from typing import Any, Optional


# Keep at most this many steps per job buffer (a single analysis caps its own
# reasoning steps far below this; this is just a runaway guard).
_MAX_STEPS = 1000

# How long a finished/errored job lingers so a late reconnect still sees its
# result, and how many jobs total we retain before pruning oldest terminals.
_TERMINAL_TTL = timedelta(minutes=30)
_MAX_JOBS = 50


class ChatJob:
    """One running or recently-finished analysis, keyed by conversation_id."""

    __slots__ = (
        "conversation_id", "turn_id", "title", "status",
        "steps", "result", "error", "agent",
        "created_at", "updated_at", "_subscribers", "lock",
    )

    def __init__(self, conversation_id: str, turn_id: str, title: str, agent: Any):
        self.conversation_id = conversation_id
        self.turn_id = turn_id
        self.title = (title or "").strip()
        self.status = "running"          # running | done | error
        self.steps: list = []
        self.result: Any = None
        self.error: str = ""
        self.agent = agent               # detached per-conversation agent
        self.created_at = datetime.now()
        self.updated_at = self.created_at
        self._subscribers: set = set()   # set[queue.Queue]
        self.lock = threading.Lock()

    def summary(self) -> dict:
        return {
            "conversation_id": self.conversation_id,
            "turn_id": self.turn_id,
            "title": self.title or "New conversation",
            "status": self.status,
            "running": self.status == "running",
            "step_count": len(self.steps),
        }


# --- Registry ------------------------------------------------------------
_jobs: dict[str, ChatJob] = {}
_registry_lock = threading.Lock()


def _prune_locked() -> None:
    """Drop expired terminal jobs / cap total. Caller holds _registry_lock."""
    now = datetime.now()
    # Remove terminal jobs past their TTL.
    expired = [
        cid for cid, j in _jobs.items()
        if j.status != "running" and (now - j.updated_at) > _TERMINAL_TTL
    ]
    for cid in expired:
        _jobs.pop(cid, None)
    # If still over cap, drop oldest terminal jobs (never running ones).
    if len(_jobs) > _MAX_JOBS:
        terminals = sorted(
            (j for j in _jobs.values() if j.status != "running"),
            key=lambda j: j.updated_at,
        )
        for j in terminals[: len(_jobs) - _MAX_JOBS]:
            _jobs.pop(j.conversation_id, None)


def start_job(*, conversation_id: str, turn_id: str, title: str, agent: Any) -> ChatJob:
    """Register a fresh job for a conversation, replacing any prior one."""
    job = ChatJob(conversation_id, turn_id, title, agent)
    with _registry_lock:
        _prune_locked()
        _jobs[conversation_id] = job
    return job


def get_job(conversation_id: str) -> Optional[ChatJob]:
    if not conversation_id:
        return None
    with _registry_lock:
        return _jobs.get(conversation_id)


def publish_step(job: ChatJob, step: Any) -> None:
    """Append a step to the buffer and fan it out to live subscribers."""
    if job is None:
        return
    try:
        with job.lock:
            if len(job.steps) < _MAX_STEPS:
                job.steps.append(step)
            job.updated_at = datetime.now()
            subs = list(job._subscribers)
        for q in subs:
            try:
                q.put_nowait(("step", step))
            except Exception:
                pass
    except Exception:
        pass


def finish_job(job: ChatJob, result: Any) -> None:
    _terminate(job, "done", result=result)


def fail_job(job: ChatJob, error: str) -> None:
    _terminate(job, "error", error=error or "Unknown error")


def _terminate(job: ChatJob, status: str, *, result: Any = None, error: str = "") -> None:
    if job is None:
        return
    try:
        with job.lock:
            job.status = status
            job.result = result
            job.error = error
            job.updated_at = datetime.now()
            subs = list(job._subscribers)
            job._subscribers.clear()
        payload = result if status == "done" else error
        for q in subs:
            try:
                q.put_nowait((status, payload))
            except Exception:
                pass
    except Exception:
        pass


def subscribe(job: ChatJob):
    """
    Atomically snapshot the steps-so-far and register a live subscriber.

    Returns ``(queue, replay_steps, terminal)`` where:
      * replay_steps — steps already buffered (caller emits these first),
      * terminal     — ``(status, payload)`` if the job already finished, in
        which case ``queue`` is unused and the caller emits the terminal event
        right after the replay (no live follow needed),
      * otherwise terminal is None and the caller drains ``queue`` until it
        yields a ('done'|'error', payload) item.
    """
    with job.lock:
        replay = list(job.steps)
        if job.status == "done":
            return None, replay, ("done", job.result)
        if job.status == "error":
            return None, replay, ("error", job.error)
        q: _queue.Queue = _queue.Queue()
        job._subscribers.add(q)
        return q, replay, None


def unsubscribe(job: ChatJob, q) -> None:
    if job is None or q is None:
        return
    try:
        with job.lock:
            job._subscribers.discard(q)
    except Exception:
        pass


def active_summaries() -> list[dict]:
    """Summaries of all currently-running jobs (for the History sidebar)."""
    with _registry_lock:
        return [j.summary() for j in _jobs.values() if j.status == "running"]
