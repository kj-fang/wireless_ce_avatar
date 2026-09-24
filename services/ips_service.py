"""
Whether a log still owes us a case number, and remembering the answer.

The answer is remembered per log file rather than per session. The user is
answering a question about the file ("this one is case 01010628", "this one has
no case"), and that answer does not stop being true when the app restarts — or
when a slow request that started before they answered writes the session back
over the top of it.
"""

import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import List

from flask import session

from configs.global_configs import app_config
from models.models import CaseContext
from utils import ips_utils

EXPLICIT = "explicit"
DERIVED_FROM_PATH = "derived_from_path"
SKIPPED = "skipped"
ABSENT = "absent"

_SOURCES = {EXPLICIT, DERIVED_FROM_PATH, SKIPPED, ABSENT}

SESSION_SOURCE_KEY = "case_ref_source"

_ANSWER_FILE_NAME = "ips_answers.json"
_MAX_REMEMBERED_ANSWERS = 500

_answer_lock = threading.Lock()

# The answer is filed under the log path, so losing the path loses the answer.
# The session cannot be trusted to hold it — the clobbering request is usually
# one that started before the log was even chosen — and this app serves one
# desktop user, the same reason app_config.last_analyzed_log_path exists.
_last_prompted_log = ""


def _answer_store_path() -> Path:
    root = getattr(app_config, "avatarfiles_dir", "") or ""
    if not root:
        return Path()
    state_dir = Path(root) / "app_state"
    state_dir.mkdir(parents=True, exist_ok=True)
    return state_dir / _ANSWER_FILE_NAME


def _log_key(log_path) -> str:
    text = str(log_path or "").strip()
    if not text:
        return ""
    try:
        return os.path.normcase(os.path.abspath(text))
    except Exception:
        return os.path.normcase(text)


def _load_answers() -> dict:
    path = _answer_store_path()
    if not path or not path.is_file():
        return {}
    try:
        with path.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except Exception:
        # A corrupt store must not block analysis; the worst case is re-asking.
        return {}


def _note_log_path(log_path) -> None:
    global _last_prompted_log
    text = str(log_path or "").strip()
    if text:
        _last_prompted_log = text


def answer_for(log_path) -> dict:
    """The answer already given for this log file, or {}."""
    key = _log_key(log_path)
    if not key:
        return {}
    record = _load_answers().get(key)
    return record if isinstance(record, dict) else {}


def remember_answer(log_path, case_nbr: str, source: str) -> None:
    key = _log_key(log_path)
    if not key:
        return
    path = _answer_store_path()
    if not path:
        return
    try:
        with _answer_lock:
            answers = _load_answers()
            answers[key] = {
                "case_nbr": case_nbr or "",
                "source": source,
                "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            }
            if len(answers) > _MAX_REMEMBERED_ANSWERS:
                oldest = sorted(answers.items(),
                                key=lambda kv: str((kv[1] or {}).get("at", "")))
                answers = dict(oldest[-_MAX_REMEMBERED_ANSWERS:])
            tmp = path.with_suffix(".tmp")
            with tmp.open("w", encoding="utf-8") as fh:
                json.dump(answers, fh, indent=2)
            os.replace(tmp, path)
    except Exception as e:
        print(f"[ips] could not remember the answer for {key}: {e}")


def is_skipped(log_path) -> bool:
    return answer_for(log_path).get("source") == SKIPPED


def current_case_nbr() -> str:
    """The canonical case number already attached to this session, or ""."""
    raw = session.get("case_context") or {}
    if not isinstance(raw, dict) or not raw:
        return ""
    return ips_utils.normalise_ips(raw.get("case_nbr"))


def current_source() -> str:
    value = str(session.get(SESSION_SOURCE_KEY) or "").strip()
    return value if value in _SOURCES else ABSENT


def _restore_from_store(log_path) -> None:
    """
    Put a remembered answer back on a session that lost it.

    Sessions are held server-side and written back whole at the end of each
    request, so a slow request that read the session before the user answered
    overwrites the answer on its way out — the case number is simply gone, and
    the user is asked again. The file keyed by log path is the durable record
    of what they said; this restores it.
    """
    if current_case_nbr() or current_source() != ABSENT:
        return
    record = answer_for(log_path)
    source = record.get("source")
    if source == SKIPPED:
        _remember_on_session("", SKIPPED)
    elif source in (EXPLICIT, DERIVED_FROM_PATH) and record.get("case_nbr"):
        _remember_on_session(str(record["case_nbr"]), source)


def candidates_for(log_path) -> List[str]:
    """Case numbers worth offering for this log, best guess first."""
    found = ips_utils.derive_ips_candidates(log_path)
    attached = current_case_nbr()
    if attached and attached not in found:
        found.insert(0, attached)
    return found


def needs_ips(log_path) -> bool:
    """Whether the modal should block this log."""
    _restore_from_store(log_path)
    if current_case_nbr():
        return False
    return current_source() != SKIPPED


def prompt_state(log_path) -> dict:
    """Everything the client needs to decide whether and how to prompt."""
    _note_log_path(log_path)
    candidates = candidates_for(log_path)
    return {
        "needs_ips": needs_ips(log_path),
        "ips_candidates": candidates,
        "suggested_ips": candidates[0] if candidates else "",
        "case_nbr": current_case_nbr(),
        "case_ref_source": current_source(),
        # A skip is remembered against the path, so the dialog has to be able
        # to name the log it is about to mark as caseless.
        "log_path": str(log_path or ""),
    }


def blocking_state(log_path=None):
    """
    The prompt payload when this session may not start chatting yet, else None.

    The check lives on the server because the requirement is that the case
    number is always recorded, and a client-side dialog is only ever a
    suggestion — the same conversation can be reached from five entry points
    and from a restored session.
    """
    path = log_path if log_path is not None else (
        session.get("chatbot_log_path")
        or _last_prompted_log
        or app_config.last_analyzed_log_path
        or ""
    )
    if not needs_ips(path):
        return None
    state = prompt_state(path)
    state.update({
        "success": False,
        "ips_required": True,
        "log_path": path,
        "error": "Enter the IPS case number for this log before starting the conversation.",
    })
    return state


def _remember_on_session(canonical: str, source: str) -> str:
    """Write one answer onto the session. Returns the canonical number, or ""."""
    session[SESSION_SOURCE_KEY] = source

    raw = session.get("case_context") or {}
    have_context = isinstance(raw, dict) and bool(raw)

    if source == SKIPPED:
        if have_context:
            context = CaseContext.from_session(raw)
            context.case_ref_source = SKIPPED
            session["case_context"] = context.to_session()
        return ""

    context = CaseContext.from_session(raw) if have_context else CaseContext()
    previous = str(context.case_nbr or "")
    context.case_nbr = canonical
    context.case_ref_source = source
    session["case_context"] = context.to_session()

    # The extracted-file index is keyed by case number, and the results page
    # looks it up by the number on the context. Renaming one without the other
    # leaves that page with nothing to show.
    if previous and previous != canonical:
        results = app_config.download_results.pop(previous, None)
        if results is not None:
            app_config.download_results[canonical] = results

    return canonical


def attach(case_nbr: str, source: str, log_path="") -> str:
    """
    Record the user's answer on the session and against the log file.

    Returns the canonical case number, or "" for a skip. Raises ValueError when
    the answer is not usable, so the route can report it rather than storing a
    case number nobody can trace.
    """
    if source not in _SOURCES:
        raise ValueError(f"unknown case reference source: {source}")

    _note_log_path(log_path)

    if source == SKIPPED:
        remember_answer(log_path, "", SKIPPED)
        return _remember_on_session("", SKIPPED)

    canonical = ips_utils.normalise_ips(case_nbr)
    if not canonical:
        raise ValueError("Enter an 8-digit case number, for example 01010628.")

    remember_answer(log_path, canonical, source)
    return _remember_on_session(canonical, source)

