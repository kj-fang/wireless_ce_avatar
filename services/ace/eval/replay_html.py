"""Renderers for the reverify per-user replay HTML + email body.

The replay HTML mirrors the log_chatbot page: it embeds the captured chat as
a JSON payload and renders it client-side with marked.js, so a down-voter can
open the attachment and see the agent re-answering their case exactly as the
chatbot UI would show it.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

_TEMPLATE_DIR = Path(__file__).resolve().parent / "templates"
_ENV = Environment(
    loader=FileSystemLoader(str(_TEMPLATE_DIR)),
    autoescape=select_autoescape(enabled_extensions=("html", "xml"), default=True),
)
_REPLAY_TMPL = _ENV.get_template("chat_replay.html")
_EMAIL_TMPL = _ENV.get_template("reverify_user_email.html")


def render_replay_html(*, person_email: str, sections: list[dict],
                       generated_at: str | None = None) -> str:
    """One combined HTML for a single person spanning multiple case sections."""
    generated_at = generated_at or datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    data_json = json.dumps({"sections": sections}, ensure_ascii=False)
    # Prevent an embedded "</script>" (or any "</") in the data from breaking
    # out of the <script> block.
    data_json = data_json.replace("</", "<\\/")
    return _REPLAY_TMPL.render(
        person_email=person_email,
        generated_at=generated_at,
        data_json=data_json,
    )


def render_user_email(*, person_email: str, cases: list[dict],
                      namespace_changes: list[dict], attachment_name: str,
                      generated_at: str | None = None) -> str:
    generated_at = generated_at or datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    return _EMAIL_TMPL.render(
        person_email=person_email,
        cases=cases,
        namespace_changes=namespace_changes,
        attachment_name=attachment_name,
        generated_at=generated_at,
    )
