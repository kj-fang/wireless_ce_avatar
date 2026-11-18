"""
What the assistant needs from the running app: the LLM client, who the user
is, and a ready agent. The chat route and the scheduled-task ticker both come
through here, so a task run later behaves like the same user asking.
"""

from __future__ import annotations

from services.assistant.agent import AssistantAgent


class AssistantUnavailable(RuntimeError):
    """The assistant cannot run here (no LLM client configured)."""


def llm() -> tuple:
    """(client, model) of the app's LLM helper. Raises AssistantUnavailable
    when the app has no API key - there is no deterministic fallback for a
    chat."""
    from configs.global_configs import app_config
    helper = getattr(app_config, "llm_helper", None)
    client = getattr(helper, "client", None) if helper is not None else None
    if client is None:
        raise AssistantUnavailable(
            "The assistant needs the app's AI model, and no API key is configured on this PC.")
    return client, getattr(helper, "model", "") or "gpt-4.1"


_mailbox: list = []


def own_mailbox() -> str:
    """Mail address of the Windows account this app runs as (looked up once:
    `whoami /upn` starts a process)."""
    if not _mailbox:
        try:
            from utils.helpers import detect_user_email
            _mailbox.append(str(detect_user_email() or "").strip())
        except Exception:
            _mailbox.append("")
    return _mailbox[0]


def identity() -> dict:
    """{"owner_name", "mailbox", "role", "signed_in"} as of now.

    owner_name is the IPS sign-in Muse already holds for this PC; it is ""
    until that sign-in has succeeded. Never starts an interactive sign-in."""
    owner, state = "", "signed_out"
    try:
        from services.handsfree import login
        status = login.status()
        state = str(status.get("status") or "signed_out")
        owner = str((status.get("identity") or {}).get("name") or "")
    except Exception:
        pass
    role = "client"
    try:
        from services.handsfree import scheduler
        role = scheduler.role()
    except Exception:
        pass
    return {"owner_name": owner, "mailbox": own_mailbox(), "role": role,
            "signed_in": bool(owner), "sign_in_state": state}


def start_silent_sign_in(retry_failed: bool = False) -> None:
    """Ask Muse to resolve the IPS identity in the background when it has
    not yet (cookie file / Windows SSO - never a visible browser). A sign-in
    that already failed is tried again only when the user asks."""
    try:
        from services.handsfree import login
        wanted = ("signed_out", "failed") if retry_failed else ("signed_out",)
        if login.status().get("status") in wanted:
            login.start_sign_in(interactive=False)
    except Exception:
        pass


def new_agent(*, headless: bool = False) -> AssistantAgent:
    client, model = llm()
    return AssistantAgent(client, model, headless=headless, identity=identity)
