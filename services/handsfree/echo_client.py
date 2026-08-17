"""
Echo knowledge-base MCP client + assert/yellow-bang evidence detection.

Echo is the Wireless Connectivity Knowledge Assistant
(https://echo-backend.intel.com/welcome/#mcp-setup). This module speaks MCP
over SSE to the FastMCP server in **knowledge-base-only mode** (Option A on
the welcome page): no credentials, no X-Service-Connections header — only the
grounded documentation Q&A tool `echo_chat`.

Used by the handsfree runner's `echo_kb` stage: when the analysis surfaced a
firmware assert or yellow-bang evidence, the assert is resolved to its driver
header entry via utils.assert_code_utils.lookup_assert_code and Echo is asked
for the known root cause of that assert. Echo being down must never affect
the analysis pipeline — every failure raises EchoUnavailable, and
collect_echo_insights() converts per-question failures into error entries
instead of propagating.

Verified against mcp==2.0.0 / echo-backend on 2026-08-17:
  * echo_chat's required arg is `question` (schema introspected as fallback);
  * the SSE context manager can raise an ExceptionGroup during TEARDOWN even
    after a successful tool call — hence the capture-box pattern in
    ask_echo_kb, which keeps a result obtained before the noise.
"""

from __future__ import annotations

import asyncio
import os
import re
from typing import Callable, Optional

DEFAULT_ECHO_SSE_URL = "https://echo-backend.intel.com/mcp/sse"
DEFAULT_TIMEOUT_S = 90          # Azure OpenAI file search can take a while
MAX_ASSERTS_PER_CASE = 3        # bound tokens/latency per case


class EchoUnavailable(RuntimeError):
    """Echo could not be reached or did not return a usable answer."""


def echo_url() -> str:
    return os.environ.get("ECHO_MCP_URL") or DEFAULT_ECHO_SSE_URL


async def _ask_async(question: str, url: str, timeout: float, box: dict) -> None:
    from mcp import ClientSession
    from mcp.client.sse import sse_client

    async with sse_client(url, timeout=15, sse_read_timeout=timeout) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            spec = next((t for t in tools.tools if t.name == "echo_chat"), None)
            if spec is None:
                raise EchoUnavailable("server does not offer the echo_chat tool")
            # Arg name from the schema (mcp 2.0 uses input_schema); "question"
            # is the verified name today — introspection guards against renames.
            arg = "question"
            schema = (getattr(spec, "input_schema", None)
                      or getattr(spec, "inputSchema", None) or {})
            required = schema.get("required") or []
            props = schema.get("properties") or {}
            for name in required:
                if (props.get(name) or {}).get("type") == "string":
                    arg = name
                    break
            res = await session.call_tool("echo_chat", {arg: question})
            parts = [c.text for c in (getattr(res, "content", None) or [])
                     if getattr(c, "text", None)]
            text = "\n".join(parts).strip()
            if getattr(res, "is_error", False) or getattr(res, "isError", False):
                raise EchoUnavailable(f"echo_chat error: {text[:300]}")
            if not text or text.startswith("Error executing tool"):
                raise EchoUnavailable(f"echo_chat unusable reply: {text[:300]}")
            box["answer"] = text


def ask_echo_kb(question: str, *, url: Optional[str] = None,
                timeout: float = DEFAULT_TIMEOUT_S) -> str:
    """Blocking knowledge-base question -> Echo's grounded answer.

    Raises EchoUnavailable on connection failure, tool error, or timeout.
    Safe to call from the handsfree worker thread (owns its own event loop).
    """
    box: dict = {}
    try:
        asyncio.run(asyncio.wait_for(
            _ask_async(question, url or echo_url(), timeout, box),
            timeout=timeout + 30))
    except EchoUnavailable:
        raise
    except BaseException as e:  # incl. teardown ExceptionGroups (see docstring)
        if "answer" not in box:
            leaf = e
            while hasattr(leaf, "exceptions") and leaf.exceptions:
                leaf = leaf.exceptions[0]
            raise EchoUnavailable(f"{type(leaf).__name__}: {leaf}") from None
    if "answer" not in box:
        raise EchoUnavailable("no answer returned")
    return box["answer"]


# ---------------------------------------------------------------------------
# Evidence detection over a CaseAnalysis
# ---------------------------------------------------------------------------

# "ASSERT ... 0x02001234" style — code within 120 chars after an assert word.
_ASSERT_CODE_RE = re.compile(r"(?i)\bassert\w*\b[^\n]{0,120}?(0x[0-9A-Fa-f]{4,10})")
# lookup_assert_code() tool output already captured in the agent steps.
_LOOKUP_BLOCK_RE = re.compile(r"=== Assert Code Lookup: (0x[0-9A-Fa-f]+)")
_YELLOW_BANG_RE = re.compile(
    r"(?i)yellow[ _-]?bang|\bYB\b|device (?:lost|drop)|\bCode 10\b")


def _analysis_text_blob(analysis) -> str:
    """All the text the agent produced for this case, concatenated."""
    texts: list[str] = []
    for inc in getattr(analysis, "incidents", None) or []:
        texts.append(str(getattr(inc, "report_text", "") or ""))
        rep = getattr(inc, "report", None) or {}
        texts.append(str(rep.get("root_cause_summary") or ""))
        texts.append(str(rep.get("markdown_summary") or ""))
        for step in getattr(inc, "steps", None) or []:
            texts.append(str((step or {}).get("content") or ""))
    return "\n".join(texts)


def find_assert_evidence(analysis) -> dict:
    """Scan the analysis for firmware assert codes and yellow-bang evidence.

    Returns {"assert_codes": [str, ...], "yellow_bang": bool}; codes are
    deduped case-insensitively, order preserved, capped at
    MAX_ASSERTS_PER_CASE.
    """
    blob = _analysis_text_blob(analysis)
    codes: list[str] = []
    for regex in (_ASSERT_CODE_RE, _LOOKUP_BLOCK_RE):
        codes.extend(m.group(1) for m in regex.finditer(blob))
    seen: set = set()
    uniq: list[str] = []
    for c in codes:
        if c.lower() not in seen:
            seen.add(c.lower())
            uniq.append(c)
    yellow = bool(_YELLOW_BANG_RE.search(blob)
                  or _YELLOW_BANG_RE.search(str(getattr(analysis, "issue_type", "") or "")))
    return {"assert_codes": uniq[:MAX_ASSERTS_PER_CASE], "yellow_bang": yellow}


def build_assert_question(code: str, lookup_text: str, context: str = "") -> str:
    parts = [
        "A WiFi firmware assert was hit in an Intel wireless driver customer "
        f"case (IPS). Assert code: {code}.",
        "",
        "Assert entry resolved from the driver headers "
        "(assertLmac.h / assertUmac.h):",
        lookup_text.strip(),
    ]
    if context.strip():
        parts += ["", "Case symptom described by the customer:",
                  context.strip()[:800]]
    parts += ["",
              "From the WiFi driver/firmware documentation: what is the known "
              "root cause of this assert, and what are the recommended next "
              "debug steps?"]
    return "\n".join(parts)


def build_yellow_bang_question(context: str = "") -> str:
    parts = ["An Intel WiFi adapter hit a yellow bang (device error / device "
             "lost) in an IPS customer case."]
    if context.strip():
        parts += ["", "Case symptom described by the customer:",
                  context.strip()[:800]]
    parts += ["",
              "From the WiFi driver documentation: what are the common root "
              "causes of a WiFi yellow bang / device-lost failure, and what "
              "are the recommended next debug steps?"]
    return "\n".join(parts)


def collect_echo_insights(analysis,
                          ask: Optional[Callable[[str], str]] = None) -> list[dict]:
    """Query Echo for every assert (and yellow bang) found in the analysis.

    Never raises: a per-question EchoUnavailable becomes an entry with
    "error" set and "answer" None, so one outage can't lose other answers
    (and the pipeline stage stays green — the insight list records the miss).
    """
    from utils.assert_code_utils import lookup_assert_code

    ask = ask or ask_echo_kb
    evidence = find_assert_evidence(analysis)
    context = str(getattr(analysis, "clean_description", "") or "")
    insights: list[dict] = []

    for code in evidence["assert_codes"]:
        lookup = lookup_assert_code(code)
        question = build_assert_question(code, lookup, context)
        entry = {"kind": "assert", "code": code, "lookup": lookup,
                 "question": question, "answer": None, "error": None}
        try:
            entry["answer"] = ask(question)
        except EchoUnavailable as e:
            entry["error"] = str(e)
        insights.append(entry)

    # Yellow bang without an assert code still deserves a KB answer; with
    # asserts present the assert questions already carry the case context.
    if evidence["yellow_bang"] and not evidence["assert_codes"]:
        question = build_yellow_bang_question(context)
        entry = {"kind": "yellow_bang", "code": None, "lookup": None,
                 "question": question, "answer": None, "error": None}
        try:
            entry["answer"] = ask(question)
        except EchoUnavailable as e:
            entry["error"] = str(e)
        insights.append(entry)

    return insights
