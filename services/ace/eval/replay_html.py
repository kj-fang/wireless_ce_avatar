"""Renderers for the reverify per-user replay HTML + email body.

The replay HTML is built to look EXACTLY like the live log_chatbot page: it
pulls that page's real <style> block verbatim and drives the SAME chat
rendering (msg bubbles, the "Agent Processing Steps" card, and the report
card) from the captured turn, so a down-voter sees the agent re-answering
their case in the identical interface.
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

_TEMPLATE_DIR = Path(__file__).resolve().parent / "templates"
# repo_root/templates/log_chatbot.html — the real chatbot page.
_CHATBOT_HTML = Path(__file__).resolve().parents[3] / "templates" / "log_chatbot.html"

_ENV = Environment(
    loader=FileSystemLoader(str(_TEMPLATE_DIR)),
    autoescape=select_autoescape(enabled_extensions=("html", "xml"), default=True),
)
_REPLAY_TMPL = _ENV.get_template("chat_replay.html")
_EMAIL_TMPL = _ENV.get_template("reverify_user_email.html")


def _load_chatbot_css() -> str:
    """Return the FIRST <style> block from the live log_chatbot page verbatim,
    so the replay's chat area is pixel-identical to what users see."""
    try:
        html = _CHATBOT_HTML.read_text(encoding="utf-8")
    except Exception as e:
        print(f"[reverify] could not read log_chatbot.html for CSS: {e}")
        return ""
    m = re.search(r"<style>(.*?)</style>", html, re.DOTALL)
    return m.group(1) if m else ""


_CHATBOT_CSS = _load_chatbot_css()


def render_replay_html(*, person_email: str, sections: list[dict],
                       generated_at: str | None = None) -> str:
    """One combined HTML for a single person spanning multiple case sections,
    styled with the real chatbot page's CSS."""
    generated_at = generated_at or datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    data_json = json.dumps({"sections": sections}, ensure_ascii=False)
    # Prevent an embedded "</script>" (or any "</") in the data from breaking
    # out of the <script> block.
    data_json = data_json.replace("</", "<\\/")
    return _REPLAY_TMPL.render(
        person_email=person_email,
        generated_at=generated_at,
        data_json=data_json,
        chatbot_css=_CHATBOT_CSS,
    )


def render_user_email(*, person_email: str, cases: list[dict],
                      namespace_changes: list[dict],
                      attachment_name: str | None = None,
                      has_attachment: bool = True,
                      no_log_case_count: int = 0,
                      generated_at: str | None = None) -> str:
    generated_at = generated_at or datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    date = generated_at.split(" ")[0]
    # Flatten per-namespace changes into one triage-style table.
    changed_bullet_rows: list[dict] = []
    for ns in namespace_changes or []:
        changed_bullet_rows.extend(ns.get("changes") or [])
    return _EMAIL_TMPL.render(
        person_email=person_email,
        date=date,
        cases=cases or [],
        attachment_name=attachment_name,
        has_attachment=bool(has_attachment),
        no_log_case_count=int(no_log_case_count or 0),
        changed_bullet_rows=changed_bullet_rows,
        generated_at=generated_at,
    )
