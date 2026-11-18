"""
Connectors: what the assistant can call, and how a call is run.

A connector is a plain function with a JSON-schema description. The model
picks one by name (native function calling); `run_connector` executes it on a
short-lived thread so the chat loop stays responsive: Stop takes effect within
half a second and a connector that hangs is abandoned at its timeout instead
of holding the turn (Echo, for one, runs `asyncio.run` and cannot be
interrupted - it takes about 70 seconds on a good day).

Results are dicts and travel back to the model as JSON, capped in size. A
connector never raises out of `run_connector`: a failure is a result that says
what went wrong, which the model can explain to the user.
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Optional

MAX_RESULT_CHARS = 6000        # one connector result as the model sees it
POLL_S = 0.5                   # how often a running call checks Stop / timeout


@dataclass(frozen=True)
class Connector:
    """One thing the assistant can do.

    kind        "read" looks something up; "act" changes something (starts an
                analysis, schedules a task). Acts are never offered to a
                headless run that must not have them (see `headless_ok`).
    internal    the result is Intel-internal (Echo): the answer is labelled
                so, and must never be passed on to a customer as is.
    untrusted   the result carries text written by third parties (mail, case
                comments). It is data for the model, never instructions.
    headless_ok may be used by a scheduled task running with nobody watching.
    """

    name: str
    description: str
    parameters: dict
    run: Callable[[dict, "Ctx"], dict]
    kind: str = "read"
    timeout_s: int = 45
    internal: bool = False
    untrusted: bool = False
    headless_ok: bool = True
    result_chars: int = MAX_RESULT_CHARS      # a list connector may need more room

    def schema(self) -> dict:
        """The OpenAI-style function schema the LLM client takes."""
        return {"type": "function",
                "function": {"name": self.name, "description": self.description,
                             "parameters": self.parameters}}


@dataclass
class Ctx:
    """What a connector may know about the turn it runs in."""

    user_text: str = ""                       # the user's own words this turn
    owner_name: str = ""                      # signed-in IPS user ("" when not signed in)
    mailbox: str = ""                         # this PC's mail address
    role: str = "client"                      # Beacon role of this process
    headless: bool = False                    # a scheduled task, nobody watching
    cancel_event: threading.Event = field(default_factory=threading.Event)
    # Short ids the model sees ("m3") -> the real reference behind them
    # (an Outlook entry id is ~140 characters and means nothing to a model).
    handles: dict = field(default_factory=dict)
    # Actions a connector decided the USER must confirm (see connectors/cases).
    pending_actions: list = field(default_factory=list)
    # Progress lines a slow connector wants shown while it works.
    emit: Callable[[str], None] = lambda text: None
    now: Callable[[], datetime] = lambda: datetime.now().astimezone()
    internal_used: bool = False               # an internal connector answered this turn
    # Other people's writing (mail, case comments) is in front of the model in
    # this conversation. From then on an action is proposed for the user's
    # click instead of run: the request for it may not be the user's own.
    untrusted_seen: bool = False


def object_schema(properties: Optional[dict] = None, required: Optional[list] = None) -> dict:
    """A JSON-schema object with no extra properties allowed."""
    return {"type": "object", "properties": dict(properties or {}),
            "required": list(required or []), "additionalProperties": False}


def clean_args(conn: Connector, raw: Any) -> tuple[dict, str]:
    """(arguments, problem). Keeps only the declared properties, applies a
    light type coercion (models send "40" for 40 and "true" for true), and
    names the first required argument that is missing."""
    args = raw if isinstance(raw, dict) else {}
    props = (conn.parameters or {}).get("properties") or {}
    out: dict = {}
    for key, spec in props.items():
        if key not in args or args[key] is None:
            continue
        value, kind = args[key], (spec or {}).get("type")
        try:
            if kind == "integer" and not isinstance(value, bool):
                value = int(value)
            elif kind == "number" and not isinstance(value, bool):
                value = float(value)
            elif kind == "boolean" and not isinstance(value, bool):
                value = str(value).strip().lower() in ("1", "true", "yes", "y")
            elif kind == "string" and not isinstance(value, str):
                value = str(value)
        except (TypeError, ValueError):
            return {}, f"argument '{key}' must be {kind}"
        out[key] = value
    for key in (conn.parameters or {}).get("required") or []:
        if key not in out or (isinstance(out[key], str) and not out[key].strip()):
            return {}, f"argument '{key}' is required"
    return out, ""


def run_connector(conn: Connector, raw_args: Any, ctx: Ctx) -> dict:
    """Run one connector call and return its result dict. Never raises.

    {"error": ...} when the arguments are wrong, the connector failed, the
    user pressed Stop ("cancelled") or the call outlived `conn.timeout_s`. In
    the last two cases the worker thread is left to finish on its own and its
    late result is dropped."""
    args, problem = clean_args(conn, raw_args)
    if problem:
        return {"error": problem}
    box: dict = {}

    def _work() -> None:
        try:
            box["result"] = conn.run(args, ctx)
        except Exception as e:                       # a connector bug is an answer, not a crash
            box["error"] = f"{type(e).__name__}: {e}"

    worker = threading.Thread(target=_work, daemon=True, name=f"assistant-connector-{conn.name}")
    worker.start()
    deadline = time.monotonic() + max(1, int(conn.timeout_s))
    while worker.is_alive():
        worker.join(POLL_S)
        if not worker.is_alive():
            break
        if ctx.cancel_event.is_set():
            return {"error": "cancelled"}
        if time.monotonic() >= deadline:
            return {"error": f"{conn.name} did not answer within {conn.timeout_s} s"}
    if "error" in box:
        return {"error": box["error"]}
    result = box.get("result")
    if not isinstance(result, dict):
        result = {"result": result}
    if conn.internal and not result.get("error"):
        ctx.internal_used = True
    return result


def to_model_text(result: dict, limit: int = MAX_RESULT_CHARS) -> str:
    """A connector result as the tool message the model reads: compact JSON,
    cut at `limit` with a note saying so (a cut-off JSON document would look
    complete to a model that cannot see the end)."""
    try:
        text = json.dumps(result, ensure_ascii=False, separators=(",", ":"), default=str)
    except Exception:
        text = str(result)
    if len(text) <= limit:
        return text
    return text[:limit] + f' ...[cut: {len(text) - limit} more characters not shown; narrow the request]'


class Registry:
    """The connectors one agent may use, by name."""

    def __init__(self, connectors=()):
        self._by_name: dict[str, Connector] = {}
        for conn in connectors:
            self.add(conn)

    def add(self, conn: Connector) -> None:
        if conn.name in self._by_name:
            raise ValueError(f"connector {conn.name!r} is registered twice")
        self._by_name[conn.name] = conn

    def get(self, name: str) -> Optional[Connector]:
        return self._by_name.get(str(name or ""))

    def available(self, headless: bool = False) -> list[Connector]:
        return [c for c in self._by_name.values() if c.headless_ok or not headless]

    def schemas(self, headless: bool = False) -> list[dict]:
        return [c.schema() for c in self.available(headless)]

    def describe(self, headless: bool = False) -> list[dict]:
        """For the page's sidebar and GET /assistant/connectors."""
        return [{"name": c.name, "description": c.description, "kind": c.kind,
                 "internal": c.internal} for c in self.available(headless)]
