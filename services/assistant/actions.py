"""
Actions that wait for the user's click.

The assistant reads other people's writing (mail, case comments) and it can
start things. A sentence in a mail must never be able to start one. So an
"act" connector runs straight away only when the user's own message names its
target; in every other case it PROPOSES the action here, the answer shows it
with a Confirm button, and only that click (POST /assistant/actions/confirm)
runs it.

What an action does is fixed here on the server when it is proposed: the page
sends back nothing but the id.
"""

from __future__ import annotations

import threading
import time
import uuid
from typing import Callable

EXPIRES_S = 30 * 60
MAX_PENDING = 100

_lock = threading.Lock()
_pending: dict[str, dict] = {}
# kind -> function(params) -> result dict; registered by the connector module
# that owns the action.
_runners: dict[str, Callable[[dict], dict]] = {}


def register(kind: str, run: Callable[[dict], dict]) -> None:
    _runners[kind] = run


def _prune_locked(now: float) -> None:
    for action_id in [k for k, v in _pending.items() if now - v["created"] > EXPIRES_S]:
        _pending.pop(action_id, None)
    while len(_pending) > MAX_PENDING:
        _pending.pop(min(_pending, key=lambda k: _pending[k]["created"]), None)


def propose(kind: str, label: str, params: dict, confirm_label: str = "Confirm") -> dict:
    """Remember an action and return what the page needs to show it:
    {"id", "kind", "label", "confirm_label"}."""
    if kind not in _runners:
        raise ValueError(f"unknown action kind: {kind!r}")
    action_id = uuid.uuid4().hex
    now = time.time()
    with _lock:
        _prune_locked(now)
        _pending[action_id] = {"kind": kind, "label": label, "params": dict(params),
                               "created": now}
    return {"id": action_id, "kind": kind, "label": label, "confirm_label": confirm_label}


def confirm(action_id: str) -> dict:
    """Run a proposed action once. {"success": False, "error"} when the id is
    unknown, already used or older than EXPIRES_S."""
    now = time.time()
    with _lock:
        _prune_locked(now)
        action = _pending.pop(str(action_id or ""), None)
    if action is None:
        return {"success": False,
                "error": "This action has expired or was already used. Ask again to get a new one."}
    try:
        result = _runners[action["kind"]](action["params"]) or {}
    except Exception as e:
        return {"success": False, "error": f"{type(e).__name__}: {e}"}
    ok = not result.get("error")
    return {"success": ok, "message": str(result.get("message") or ""),
            "error": str(result.get("error") or ""), "result": result}


def dismiss(action_id: str) -> bool:
    with _lock:
        return _pending.pop(str(action_id or ""), None) is not None


def pending_count() -> int:
    with _lock:
        _prune_locked(time.time())
        return len(_pending)


def clear() -> None:
    with _lock:
        _pending.clear()
