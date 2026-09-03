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


def _make_httpx_client_factory():
    """httpx client factory for the MCP transport, hardened for this network:

    * trust_env=False — Echo is an INTRANET host. The corporate proxy
      (proxy-dmz) answers 403 for internal destinations, and inside the app
      process the proxy env vars are unreliable anyway: snowflake_service
      re-asserts NO_PROXY to the Snowflake host only (dropping intel.com) and
      DriverManager wipes HTTP(S)_PROXY around Chrome startup. Verified from
      the app venv: direct -> 200, via proxy-dmz -> 403.
    * verify=truststore — the Echo cert chains to Intel's corporate CA which
      lives in the Windows cert store, not in certifi. truststore uses the
      OS store (this is also what mcp's vendored httpx2 does by default).
    """
    import ssl
    try:
        import truststore
        verify = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    except Exception:
        verify = True   # fall back to certifi (may fail on corp CA)

    def factory(headers=None, timeout=None, auth=None):
        # mcp passes its own vendored httpx module's Timeout/Auth objects;
        # build the client from that same module so the types match.
        from mcp.shared import _httpx_utils as u
        hx = u.httpx2 if hasattr(u, "httpx2") else __import__("httpx")
        kwargs = {"follow_redirects": True, "trust_env": False, "verify": verify}
        kwargs["timeout"] = timeout if timeout is not None else hx.Timeout(30.0, read=300.0)
        if headers:
            kwargs["headers"] = headers
        if auth is not None:
            kwargs["auth"] = auth
        return hx.AsyncClient(**kwargs)

    return factory


async def _ask_async(question: str, url: str, timeout: float, box: dict) -> None:
    from mcp import ClientSession
    from mcp.client.sse import sse_client

    async with sse_client(url, timeout=15, sse_read_timeout=timeout,
                          httpx_client_factory=_make_httpx_client_factory()) as (read, write):
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

# --- PRIMARY: the driver's own assert line in the decoded WRT .log ----------
# e.g. "...FATAL_ERROR: uCode ASSERT(UMAC, rtStatus = 0x2000008A, log is
#       valid. data1 = 0x158f8cca, data2 = 0xfe10fe1)"
# rtStatus is THE firmware assert code lookup_assert_code expects (raw, with
# the 0x20000000 UMAC CPU flag still on — the tool strips it itself).
_WRT_ASSERT_RE = re.compile(
    r"(?i)uCode\s+ASSERT\s*\(\s*(?P<cpu>\w+)\s*,\s*rtStatus\s*=\s*(?P<code>0x[0-9A-Fa-f]{2,10})"
    r"(?P<rest>[^\n]{0,200})")
# Real logs use both "data1 = 0x…" (UMAC) and "data1:0x…" (LMAC) spellings.
_DATA_FIELD_RE = re.compile(r"(?i)\bdata(?P<n>[123])\s*[:=]\s*(?P<val>0x[0-9A-Fa-f]+)")
_MAX_LOG_SCAN_BYTES = 64 * 1024 * 1024   # decoded WRT logs can be huge

# --- FALLBACK: assert codes mentioned in the agent's write-up --------------
# Real firmware assert codes are 6+ hex digits in known namespaces
# (0x20…/0x10…/0x40…/0x50…/0x00…). Requiring 6+ digits keeps Windows event
# IDs (5002/5005/5010 = "adapter reset" events, NOT firmware asserts) out.
_ASSERT_CODE_RE = re.compile(r"(?i)\bassert\w*\b[^\n]{0,120}?(0x[0-9A-Fa-f]{6,10})")
# lookup_assert_code() tool output already captured in the agent steps.
_LOOKUP_BLOCK_RE = re.compile(r"=== Assert Code Lookup: (0x[0-9A-Fa-f]{6,10})")
_WINDOWS_EVENT_IDS = {"5002", "5005", "5007", "5010", "5032", "5033", "5060", "5061"}
_YELLOW_BANG_RE = re.compile(
    r"(?i)yellow[ _-]?bang|\bYB\b|device (?:lost|drop)|\bCode 10\b")


def _is_windows_event_id(code: str) -> bool:
    return code.lower().removeprefix("0x").lstrip("0") in _WINDOWS_EVENT_IDS


def scan_wrt_log_for_asserts(log_path: str, limit: int = MAX_ASSERTS_PER_CASE) -> list[dict]:
    """Scan the decoded WRT log for the driver's uCode ASSERT lines.

    Returns [{"code", "cpu", "data": {"data1": ..}, "line"}], deduped by
    code (first occurrence kept), capped at `limit`. Missing/unreadable log
    -> [] (never raises).
    """
    if not log_path or not os.path.isfile(log_path):
        return []
    found: list[dict] = []
    seen: set = set()
    try:
        with open(log_path, "r", encoding="utf-8", errors="replace") as fh:
            read = 0
            for line in fh:
                read += len(line)
                if read > _MAX_LOG_SCAN_BYTES:
                    break
                m = _WRT_ASSERT_RE.search(line)
                if not m:
                    continue
                code = m.group("code")
                if code.lower() in seen:
                    continue
                seen.add(code.lower())
                data = {f"data{d.group('n')}": d.group("val")
                        for d in _DATA_FIELD_RE.finditer(m.group("rest"))}
                found.append({"code": code, "cpu": m.group("cpu").upper(),
                              "data": data, "line": line.strip()[:400]})
                if len(found) >= limit:
                    break
    except Exception:
        return found
    return found


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
    """Find firmware assert codes and yellow-bang evidence for a case.

    Source priority:
      1. the decoded WRT log (analysis.log_path) — the driver's own
         "uCode ASSERT(<CPU>, rtStatus = 0x…)" lines. Authoritative.
      2. fallback: assert codes mentioned in the agent's write-up
         (6+ hex digits; Windows event IDs like 5002 rejected).

    Returns {"assert_codes": [str], "asserts": [{code, cpu, data, line,
    source}], "yellow_bang": bool, "source": "wrt_log"|"agent_text"|None}.
    """
    asserts = [dict(a, source="wrt_log")
               for a in scan_wrt_log_for_asserts(getattr(analysis, "log_path", "") or "")]
    source = "wrt_log" if asserts else None

    blob = _analysis_text_blob(analysis)
    if not asserts:
        seen: set = set()
        for regex in (_ASSERT_CODE_RE, _LOOKUP_BLOCK_RE):
            for m in regex.finditer(blob):
                code = m.group(1)
                if code.lower() in seen or _is_windows_event_id(code):
                    continue
                seen.add(code.lower())
                asserts.append({"code": code, "cpu": "", "data": {}, "line": "",
                                "source": "agent_text"})
                if len(asserts) >= MAX_ASSERTS_PER_CASE:
                    break
            if len(asserts) >= MAX_ASSERTS_PER_CASE:
                break
        source = "agent_text" if asserts else None

    yellow = bool(_YELLOW_BANG_RE.search(blob)
                  or _YELLOW_BANG_RE.search(str(getattr(analysis, "issue_type", "") or "")))
    return {"assert_codes": [a["code"] for a in asserts],
            "asserts": asserts, "yellow_bang": yellow, "source": source}


def build_assert_question(code: str, lookup_text: str, context: str = "",
                          cpu: str = "", data: Optional[dict] = None,
                          log_line: str = "") -> str:
    head = ("A WiFi firmware assert was hit in an Intel wireless driver customer "
            f"case (IPS). Assert code (rtStatus): {code}"
            + (f", CPU: {cpu}" if cpu else "") + ".")
    parts = [head]
    if data:
        parts.append("Assert data fields: "
                     + ", ".join(f"{k} = {v}" for k, v in sorted(data.items())))
    if log_line:
        parts += ["", "Driver log line:", log_line.strip()[:400]]
    parts += [
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

    for a in evidence["asserts"]:
        code = a["code"]
        lookup = lookup_assert_code(code)
        question = build_assert_question(code, lookup, context,
                                         cpu=a.get("cpu", ""),
                                         data=a.get("data") or {},
                                         log_line=a.get("line", ""))
        entry = {"kind": "assert", "code": code, "cpu": a.get("cpu", ""),
                 "data": a.get("data") or {}, "source": a.get("source"),
                 "lookup": lookup, "question": question,
                 "answer": None, "error": None}
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
