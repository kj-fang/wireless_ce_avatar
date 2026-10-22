from __future__ import annotations

import asyncio
import os
import ssl
from typing import Optional


DEFAULT_ECHO_SSE_URL = "https://echo-backend.intel.com/mcp/sse"
DEFAULT_TIMEOUT_S = 90
MAX_HISTORY_MESSAGES = 12
MAX_HISTORY_MESSAGE_CHARS = 2000


class EchoChatError(RuntimeError):
    pass


def build_echo_question(message: str, history: Optional[list] = None) -> str:
    turns = []
    for turn in (history or [])[-MAX_HISTORY_MESSAGES:]:
        if not isinstance(turn, dict) or turn.get("role") not in ("user", "assistant"):
            continue
        content = str(turn.get("content") or "").strip()
        if content:
            speaker = "User" if turn["role"] == "user" else "Echo"
            turns.append(f"{speaker}: {content[:MAX_HISTORY_MESSAGE_CHARS]}")

    current = str(message or "").strip()
    if not current:
        raise ValueError("message is required")
    if not turns:
        return current
    return ("Continue this Echo chat using only the conversation below as context.\n"
            + "\n".join(turns)
            + f"\nUser: {current}")


def _make_httpx_client_factory():
    try:
        import truststore
        verify = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    except Exception:
        verify = True

    def factory(headers=None, timeout=None, auth=None):
        from mcp.shared import _httpx_utils
        httpx_module = (_httpx_utils.httpx2 if hasattr(_httpx_utils, "httpx2")
                        else __import__("httpx"))
        options = {
            "follow_redirects": True,
            "trust_env": False,
            "verify": verify,
            "timeout": timeout or httpx_module.Timeout(30.0, read=300.0),
        }
        if headers:
            options["headers"] = headers
        if auth is not None:
            options["auth"] = auth
        return httpx_module.AsyncClient(**options)

    return factory


async def _ask_async(question: str, url: str, timeout: float, result_box: dict) -> None:
    from mcp import ClientSession
    from mcp.client.sse import sse_client

    async with sse_client(url, timeout=15, sse_read_timeout=timeout,
                          httpx_client_factory=_make_httpx_client_factory()) as (read, write):
        async with ClientSession(read, write) as client:
            await client.initialize()
            tools = await client.list_tools()
            tool = next((item for item in tools.tools if item.name == "echo_chat"), None)
            if tool is None:
                raise EchoChatError("Echo does not provide the echo_chat tool")

            argument = "question"
            schema = (getattr(tool, "input_schema", None)
                      or getattr(tool, "inputSchema", None) or {})
            required = schema.get("required") or []
            properties = schema.get("properties") or {}
            for name in required:
                if (properties.get(name) or {}).get("type") == "string":
                    argument = name
                    break

            result = await client.call_tool("echo_chat", {argument: question})
            text = "\n".join(
                item.text for item in (getattr(result, "content", None) or [])
                if getattr(item, "text", None)
            ).strip()
            if getattr(result, "is_error", False) or getattr(result, "isError", False):
                raise EchoChatError(f"Echo returned an error: {text[:300]}")
            if not text or text.startswith("Error executing tool"):
                raise EchoChatError(f"Echo returned no usable answer: {text[:300]}")
            result_box["answer"] = text


def ask_echo_chat(message: str, *, history: Optional[list] = None,
                  url: Optional[str] = None,
                  timeout: float = DEFAULT_TIMEOUT_S) -> str:
    question = build_echo_question(message, history)
    result_box: dict = {}
    try:
        asyncio.run(asyncio.wait_for(
            _ask_async(question, url or os.environ.get("ECHO_MCP_URL") or DEFAULT_ECHO_SSE_URL,
                       timeout, result_box),
            timeout=timeout + 30,
        ))
    except EchoChatError:
        raise
    except Exception as error:
        if "answer" not in result_box:
            leaf = error
            while hasattr(leaf, "exceptions") and leaf.exceptions:
                leaf = leaf.exceptions[0]
            raise EchoChatError(f"{type(leaf).__name__}: {leaf}") from None
    if "answer" not in result_box:
        raise EchoChatError("Echo did not return an answer")
    return result_box["answer"]