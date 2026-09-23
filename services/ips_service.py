"""
Whether a log still owes us a case number, and remembering when it does not.

A skip is remembered per log file rather than per session: the user is
answering a question about the file ("this one has no case"), and that answer
does not stop being true when the app restarts.
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

_SKIP_FILE_NAME = "ips_skips.json"
_MAX_REMEMBERED_SKIPS = 500

_skip_lock = threading.Lock()


def _skip_store_path() -> Path:
    root = getattr(app_config, "avatarfiles_dir", "") or ""
    if not root:
        return Path()
    state_dir = Path(root) / "app_state"
    state_dir.mkdir(parents=True, exist_ok=True)
    return state_dir / _SKIP_FILE_NAME


def _log_key(log_path) -> str:
    text = str(log_path or "").strip()
    if not text:
        return ""
    try:
        return os.path.normcase(os.path.abspath(text))
    except Exception:
        return os.path.normcase(text)


def _load_skips() -> dict:
    path = _skip_store_path()
    if not path or not path.is_file():
        return {}
    try:
        with path.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except Exception:
        # A corrupt store must not block analysis; the worst case is re-asking.
        return {}


def is_skipped(log_path) -> bool:
    key = _log_key(log_path)
    return bool(key) and key in _load_skips()


def remember_skip(log_path) -> None:
    key = _log_key(log_path)
    if not key:
        return
    path = _skip_store_path()
    if not path:
        return
    try:
        with _skip_lock:
            skips = _load_skips()
            skips[key] = datetime.now(timezone.utc).isoformat(timespec="seconds")
            if len(skips) > _MAX_REMEMBERED_SKIPS:
                oldest = sorted(skips.items(), key=lambda kv: str(kv[1]))
                skips = dict(oldest[-_MAX_REMEMBERED_SKIPS:])
            tmp = path.with_suffix(".tmp")
            with tmp.open("w", encoding="utf-8") as fh:
                json.dump(skips, fh, indent=2)
            os.replace(tmp, path)
    except Exception as e:
        print(f"[ips] could not remember skip for {key}: {e}")


def current_case_nbr() -> str:
    """The canonical case number already attached to this session, or ""."""
    raw = session.get("case_context") or {}
    if not isinstance(raw, dict) or not raw:
        return ""
    return ips_utils.normalise_ips(raw.get("case_nbr"))


def current_source() -> str:
    value = str(session.get(SESSION_SOURCE_KEY) or "").strip()
    return value if value in _SOURCES else ABSENT


def candidates_for(log_path) -> List[str]:
    """Case numbers worth offering for this log, best guess first."""
    found = ips_utils.derive_ips_candidates(log_path)
    attached = current_case_nbr()
    if attached and attached not in found:
        found.insert(0, attached)
    return found


def needs_ips(log_path) -> bool:
    """Whether the modal should block this log."""
    if current_case_nbr():
        return False
    if current_source() == SKIPPED:
        return False
    return not is_skipped(log_path)


def prompt_state(log_path) -> dict:
    """Everything the client needs to decide whether and how to prompt."""
    candidates = candidates_for(log_path)
    return {
        "needs_ips": needs_ips(log_path),
        "ips_candidates": candidates,
        "suggested_ips": candidates[0] if candidates else "",
        "case_nbr": current_case_nbr(),
        "case_ref_source": current_source(),
    }


def blocking_state(log_path=None):
    """
    The prompt payload when this session may not start chatting yet, else None.

    The check lives on the server because the requirement is that the case
    number is always recorded, and a client-side dialog is only ever a
    suggestion — the same conversation can be reached from five entry points
    and from a restored session.
    """
    path = log_path if log_path is not None else session.get("chatbot_log_path", "")
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


def attach(case_nbr: str, source: str) -> str:
    """
    Record the user's answer on the session.

    Returns the canonical case number, or "" for a skip. Raises ValueError when
    the answer is not usable, so the route can report it rather than storing a
    case number nobody can trace.
    """
    if source not in _SOURCES:
        raise ValueError(f"unknown case reference source: {source}")

    if source == SKIPPED:
        session[SESSION_SOURCE_KEY] = SKIPPED
        raw = session.get("case_context") or {}
        if isinstance(raw, dict) and raw:
            context = CaseContext.from_session(raw)
            context.case_ref_source = SKIPPED
            session["case_context"] = context.to_session()
        return ""

    canonical = ips_utils.normalise_ips(case_nbr)
    if not canonical:
        raise ValueError("Enter an 8-digit case number, for example 01010628.")

    raw = session.get("case_context") or {}
    context = CaseContext.from_session(raw) if isinstance(raw, dict) and raw else CaseContext()
    previous = str(context.case_nbr or "")
    context.case_nbr = canonical
    context.case_ref_source = source
    session["case_context"] = context.to_session()
    session[SESSION_SOURCE_KEY] = source

    # The extracted-file index is keyed by case number, and the results page
    # looks it up by the number on the context. Renaming one without the other
    # leaves that page with nothing to show.
    if previous and previous != canonical:
        results = app_config.download_results.pop(previous, None)
        if results is not None:
            app_config.download_results[canonical] = results

    return canonical
