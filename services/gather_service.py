"""
Gather Service — usage-analytics sidecar.

Captures, on the first Send of a chatbot session, the session context the user
entered the chatbot with (Windows user name, date, CASE NUMBER and a short
summary of the case) plus the first question they asked. One tidy JSON file
per conversation is written to a shared "Gather" folder for later ingestion
into a backend database, so we can tally:

  * how many distinct people use the tool,
  * which CASE NUMBERs they worked on,
  * roughly what those cases were about,
  * how many tokens each conversation burned and what it cost in USD,
  * and how all of the above splits between the WiFi and BT chatbots.

Two calls per turn
------------------
``record_send()``  — at the START of a turn: who / which case / the question.
``record_usage()`` — at the END of a turn: tokens + settled USD cost.

They are separate because the token counts do not exist until the LLM has
finished; both merge into the same per-conversation file.

Design goals (mirrors feedback_service):
  * Independent of the chatbot agent — never blocks chat, never raises.
  * No DB. Flat JSON files only ("bronze" layer for a future DB load).
  * Writes happen on a background thread so the HTTP response isn't stalled
    by a slow / off-VPN SMB share.
  * Each record carries an integer `schema_version` so downstream ETL knows
    which parsing rules to apply when fields are added later.

Storage resolution order (cached for the process lifetime):
  1. Shared primary  (configs.path_configs.GATHER_DIR_prim)
  2. Shared backup   (configs.path_configs.GATHER_DIR_bkup)
  3. Local fallback  ``<avatarfiles_dir>/Gather``   (off-VPN / share down)

The shared UNCs are not written out in this file or logged in full — see
path_configs and ``_redact_path`` below.

Layout:  <root>/sessions/<user>/<conversation_id>.json
A per-user subfolder makes "how many distinct people" a simple folder count,
while attribution also travels inside every record (user_name field) so a flat
DB load can ignore the folder structure entirely.
"""

from __future__ import annotations

import getpass
import json
import os
import re
import sys
import threading
import time
import uuid
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Optional

from configs.global_configs import app_config
from configs.llm_pricing import cost_for
from configs.path_configs import GATHER_DIR_prim, GATHER_DIR_bkup
from utils import helpers


# Bump when the record structure changes (downstream ETL keys off this).
#   v1 - Initial usage record (user_name, date, case, log_path, issue_time,
#        messages[]).
#   v2 - Added send_count (incremented on every Send) + silver aggregate files
#        in <root>/aggregates/ (summary, users, cases, daily).
#   v3 - Added issue_time_window_minutes (the ±minutes window around issue_time
#        used for the Segment2 log slice; default 5).
#   v4 - Added LLM cost accounting: model, usage{} (token totals accumulated
#        over the conversation), cost_usd{} (settled at write time, with the
#        rates used), and turns[] (per-Send breakdown). `domain` is now passed
#        by the caller for real — BT sessions were never recorded before v4
#        because only the WiFi route called record_send().
#   v5 - Added a case workflow that exists before a chatbot conversation:
#        <root>/workflows/<user>/<workflow_id>.json.  It carries a generic
#        ai_invocations[] ledger (chatbot_turn, select_attachments_ai_summary,
#        issue_time_prepass) plus case-linked attachment inventory, selection,
#        and per-file download outcomes.  Chatbot session files retain their
#        v4 fields and gain workflow_id for a backwards-compatible join.
GATHER_SCHEMA_VERSION = 5

_MAX_DESC_CHARS = 2000        # keep the case summary compact for the DB
_MAX_MSG_CHARS = 4000         # cap the stored first question
_MAX_TURNS = 200              # cap turns[] so a long conversation can't bloat the file
_MAX_INVOCATIONS = 500        # bounded feature ledger per case workflow
_MAX_ATTACHMENT_FILES = 1000  # defensive cap for unusually large cases

# Minimum seconds between async aggregate rebuilds (per process). Each write
# schedules a rebuild but only one runs per cooldown window, so a burst of
# Sends doesn't fan out into a burst of full-scan rebuilds.
_AGG_COOLDOWN_SEC = 60

_root_cache: Optional[Path] = None
_root_lock = threading.Lock()

_locks_guard = threading.Lock()
_path_locks: dict[str, threading.Lock] = {}

_agg_lock = threading.Lock()
_last_agg_at: float = 0.0


def _redact_path(p: Any) -> str:
    """Hide the full SMB UNC in any log line: keep just ``\\<host>\\…\\<leaf>``."""
    s = str(p or "")
    if s.startswith("\\\\"):
        parts = s.lstrip("\\").split("\\")
        if len(parts) >= 2:
            return rf"\\{parts[0]}\…\{parts[-1]}"
    return s


def _current_user() -> str:
    """Best-effort Windows username; sanitised so it can be a folder name."""
    try:
        u = getpass.getuser() or os.environ.get("USERNAME", "") or "anon"
    except Exception:
        u = os.environ.get("USERNAME", "") or "anon"
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", u).strip("._-") or "anon"
    return safe


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _today() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def _safe_id(value: Any) -> str:
    """Filesystem-safe conversation id (defends against path traversal)."""
    s = str(value or "").strip()
    s = re.sub(r"[^A-Za-z0-9_.-]+", "_", s)
    return s[:128] or "unknown"


def _resolve_root() -> Path:
    """Resolve the Gather root once and cache it (8 s share probe per path)."""
    global _root_cache
    if _root_cache is not None:
        return _root_cache
    with _root_lock:
        if _root_cache is not None:
            return _root_cache

        share = helpers.get_load_path(GATHER_DIR_prim, GATHER_DIR_bkup)
        if share:
            try:
                root = Path(share)
                (root / "sessions").mkdir(parents=True, exist_ok=True)
                (root / "workflows").mkdir(parents=True, exist_ok=True)
                _root_cache = root
                print(f"[gather] using shared root: {_redact_path(root)}")
                return root
            except Exception as e:
                print(f"[gather] shared root unwritable ({_redact_path(share)}): {e} — falling back to local")

        base = getattr(app_config, "avatarfiles_dir", None)
        root = Path(base) / "Gather" if base else Path.cwd() / "data" / "Gather"
        (root / "sessions").mkdir(parents=True, exist_ok=True)
        (root / "workflows").mkdir(parents=True, exist_ok=True)
        _root_cache = root
        print(f"[gather] using local root: {_redact_path(root)}")
        return root


def _lock_for(path: Path) -> threading.Lock:
    key = str(path)
    with _locks_guard:
        lock = _path_locks.get(key)
        if lock is None:
            lock = threading.Lock()
            _path_locks[key] = lock
        return lock


def _conversation_path(conversation_id: str, user: str) -> Path:
    root = _resolve_root()
    user_dir = root / "sessions" / _safe_id(user)
    user_dir.mkdir(parents=True, exist_ok=True)
    return user_dir / f"{_safe_id(conversation_id)}.json"


def _workflow_path(workflow_id: str, user: str) -> Path:
    root = _resolve_root()
    user_dir = root / "workflows" / _safe_id(user)
    user_dir.mkdir(parents=True, exist_ok=True)
    return user_dir / f"{_safe_id(workflow_id)}.json"


def _clean_case(issue: Optional[dict]) -> dict:
    """Pull a tidy, compact case summary out of the issue context dict."""
    issue = issue if isinstance(issue, dict) else {}
    desc = str(issue.get("description") or "").strip()
    if len(desc) > _MAX_DESC_CHARS:
        desc = desc[:_MAX_DESC_CHARS] + "…"
    return {
        "case_nbr": str(issue.get("case_nbr") or "").strip(),
        "subject": str(issue.get("subject") or "").strip(),
        "issue_type": str(issue.get("issue_type") or "").strip(),
        "description": desc,
    }


def _new_record(
    conversation_id: str,
    workflow_id: str,
    session_id: str,
    user: str,
    issue: Optional[dict],
    log_path: str,
    issue_time: str,
    issue_time_window_minutes: Optional[int],
    domain: str,
) -> dict:
    now = _now_iso()
    return {
        "schema_version": GATHER_SCHEMA_VERSION,
        "record_type": "chatbot_session",
        "conversation_id": _safe_id(conversation_id),
        "workflow_id": _safe_id(workflow_id) if workflow_id else "",
        "session_id": session_id or "",
        "user_name": user,                  # required attribution
        "date": _today(),                   # required local date (YYYY-MM-DD)
        "created_at": now,
        "updated_at": now,
        "domain": domain or "wifi",
        "case": _clean_case(issue),
        "log_path": log_path or "",
        "issue_time": issue_time or "",
        # ±minutes window around issue_time used for the Segment2 log slice
        # (sidebar-adjustable; default 5). None when not supplied.
        "issue_time_window_minutes": issue_time_window_minutes,
        "messages": [],
        "message_count": 0,    # number of question texts stored (<=1)
        "send_count": 0,       # total Sends on this conversation (for stats)
        # v4 cost accounting — filled in by record_usage() once the turn ends.
        # record_send() runs BEFORE the LLM does any work, so nothing is known
        # about tokens at this point.
        "model": "",
        "usage": _empty_usage(),
        "cost_usd": _empty_cost(),
        "turns": [],
    }


def _empty_usage() -> dict:
    return {
        "llm_calls": 0,
        "input_tokens": 0,        # uncached prompt tokens
        "cache_read_tokens": 0,
        "cache_write_tokens": 0,
        "output_tokens": 0,
        "total_tokens": 0,
    }


def _empty_cost() -> dict:
    # `total: None` (not 0.0) means "not priced" — an unknown model must not
    # look like a free conversation. Only record_usage() sets real numbers.
    return {
        "input": 0.0,
        "cache": 0.0,
        "output": 0.0,
        "total": None,
        "pricing_version": "",
        "rate_input_per_mtok": None,
        "rate_output_per_mtok": None,
    }


def new_workflow_id() -> str:
    """Return an opaque identifier for one case-analysis workflow."""
    return str(uuid.uuid4())


def _normalise_usage(usage: Optional[dict]) -> dict:
    src = usage if isinstance(usage, dict) else {}

    def _n(key: str) -> int:
        try:
            return max(0, int(src.get(key) or 0))
        except (TypeError, ValueError):
            return 0

    out = {key: _n(key) for key in _empty_usage()}
    # Some lightweight callers provide the token buckets but not total_tokens.
    if not out["total_tokens"]:
        out["total_tokens"] = (
            out["input_tokens"] + out["cache_read_tokens"]
            + out["cache_write_tokens"] + out["output_tokens"]
        )
    return out


def _attachment_name(item: Any) -> str:
    if isinstance(item, (list, tuple)) and item:
        return str(item[0] or "").strip()
    if isinstance(item, dict):
        return str(item.get("name") or item.get("filename") or "").strip()
    return str(item or "").strip()


def _new_attachment_file(name: str) -> dict:
    return {
        "name": name,
        "discovered": True,
        "selected": False,
        "download_status": "not_attempted",
        "bytes": None,
        "latency_ms": None,
        "attempt_count": 0,
        "error_code": "",
        "updated_at": _now_iso(),
    }


def _reconcile_attachment_audit(audit: dict) -> None:
    files = audit.get("files") if isinstance(audit.get("files"), list) else []
    audit["files"] = files[-_MAX_ATTACHMENT_FILES:]
    audit["discovered_count"] = sum(1 for f in files if isinstance(f, dict) and f.get("discovered"))
    audit["selected_count"] = sum(1 for f in files if isinstance(f, dict) and f.get("selected"))
    audit["download_succeeded_count"] = sum(
        1 for f in files if isinstance(f, dict) and f.get("download_status") in ("success", "already_exists")
    )
    audit["download_failed_count"] = sum(
        1 for f in files if isinstance(f, dict) and f.get("download_status") == "failed"
    )
    declared = audit.get("issue_declared_attached")
    discovered = audit["discovered_count"]
    if declared is True and discovered:
        status = "MATCH_FOUND"
    elif declared is True and not discovered:
        status = "CLAIMED_BUT_MISSING"
    elif declared is False and discovered:
        status = "FOUND_NOT_DECLARED"
    elif declared is False and not discovered:
        status = "NONE"
    elif discovered:
        status = "FOUND_DECLARATION_UNKNOWN"
    else:
        status = "DECLARATION_UNKNOWN"
    audit["reconciliation_status"] = status
    audit["updated_at"] = _now_iso()


def _new_workflow_record(
    workflow_id: str,
    user: str,
    issue: Optional[dict],
    domain: str,
    attachment_list: Optional[list] = None,
) -> dict:
    now = _now_iso()
    seen: set[str] = set()
    files = []
    for item in attachment_list or []:
        name = _attachment_name(item)
        if not name or name in seen:
            continue
        seen.add(name)
        files.append(_new_attachment_file(name))
        if len(files) >= _MAX_ATTACHMENT_FILES:
            break
    audit = {
        "issue_declared_attached": None,
        "declaration_source": "",
        "discovered_count": 0,
        "selected_count": 0,
        "download_succeeded_count": 0,
        "download_failed_count": 0,
        "reconciliation_status": "NONE",
        "files": files,
        "updated_at": now,
    }
    _reconcile_attachment_audit(audit)
    return {
        "schema_version": GATHER_SCHEMA_VERSION,
        "record_type": "case_workflow",
        "workflow_id": _safe_id(workflow_id),
        "user_name": user,
        "date": _today(),
        "created_at": now,
        "updated_at": now,
        "domain": str(domain or "wifi").strip().lower() or "wifi",
        "case": _clean_case(issue),
        "conversation_ids": [],
        "ai_invocations": [],
        "attachment_audit": audit,
    }


def _load_or_new_workflow(
    path: Path,
    workflow_id: str,
    user: str,
    issue: Optional[dict],
    domain: str,
    attachment_list: Optional[list] = None,
) -> dict:
    record = None
    if path.exists():
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            record = None
    if not isinstance(record, dict):
        record = _new_workflow_record(workflow_id, user, issue, domain, attachment_list)
    if issue:
        record["case"] = _clean_case(issue)
    if domain:
        record["domain"] = str(domain).strip().lower()
    record["schema_version"] = GATHER_SCHEMA_VERSION
    record["updated_at"] = _now_iso()
    return record


def _write_workflow(path: Path, record: dict) -> None:
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def _do_record_workflow_start(
    workflow_id: str,
    issue: Optional[dict],
    domain: str,
    attachment_list: Optional[list],
) -> None:
    user = _current_user()
    try:
        path = _workflow_path(workflow_id, user)
        with _lock_for(path):
            record = _load_or_new_workflow(path, workflow_id, user, issue, domain, attachment_list)
            # Merge a refreshed attachment inventory without erasing download state.
            audit = record.get("attachment_audit") if isinstance(record.get("attachment_audit"), dict) else {}
            files = audit.get("files") if isinstance(audit.get("files"), list) else []
            by_name = {str(f.get("name") or ""): f for f in files if isinstance(f, dict)}
            for item in attachment_list or []:
                name = _attachment_name(item)
                if name and name not in by_name and len(files) < _MAX_ATTACHMENT_FILES:
                    f = _new_attachment_file(name)
                    files.append(f)
                    by_name[name] = f
            audit["files"] = files
            _reconcile_attachment_audit(audit)
            record["attachment_audit"] = audit
            _write_workflow(path, record)
    except Exception as e:
        print(f"[gather] workflow start failed (workflow={workflow_id}): {e}")
        return
    _maybe_rebuild_aggregates_async()


def record_workflow_start(
    *,
    workflow_id: str,
    issue: Optional[dict] = None,
    domain: str = "",
    attachment_list: Optional[list] = None,
) -> None:
    """Create the v5 workflow as soon as a case is loaded (before Click AI)."""
    if not workflow_id or not getattr(sys, "frozen", False):
        return
    try:
        threading.Thread(
            target=_do_record_workflow_start,
            args=(workflow_id, issue, domain, list(attachment_list or [])),
            daemon=True,
        ).start()
    except Exception as e:
        print(f"[gather] workflow dispatch failed: {e}")


def _do_record_attachment_selection(
    workflow_id: str,
    selected_names: list[str],
    issue: Optional[dict],
    domain: str,
) -> None:
    user = _current_user()
    try:
        path = _workflow_path(workflow_id, user)
        with _lock_for(path):
            record = _load_or_new_workflow(path, workflow_id, user, issue, domain)
            audit = record.get("attachment_audit") if isinstance(record.get("attachment_audit"), dict) else {}
            files = audit.get("files") if isinstance(audit.get("files"), list) else []
            selected = {str(n or "").strip() for n in selected_names if str(n or "").strip()}
            by_name = {str(f.get("name") or ""): f for f in files if isinstance(f, dict)}
            for name in selected:
                if name not in by_name and len(files) < _MAX_ATTACHMENT_FILES:
                    f = _new_attachment_file(name)
                    files.append(f)
                    by_name[name] = f
            for f in files:
                if isinstance(f, dict):
                    f["selected"] = str(f.get("name") or "") in selected
                    f["updated_at"] = _now_iso()
            audit["files"] = files
            _reconcile_attachment_audit(audit)
            record["attachment_audit"] = audit
            _write_workflow(path, record)
    except Exception as e:
        print(f"[gather] attachment selection failed (workflow={workflow_id}): {e}")
        return
    _maybe_rebuild_aggregates_async()


def record_attachment_selection(
    *, workflow_id: str, selected_files: Optional[list] = None,
    issue: Optional[dict] = None, domain: str = "",
) -> None:
    if not workflow_id or not getattr(sys, "frozen", False):
        return
    names = [_attachment_name(item) for item in (selected_files or [])]
    try:
        threading.Thread(
            target=_do_record_attachment_selection,
            args=(workflow_id, names, issue, domain), daemon=True,
        ).start()
    except Exception as e:
        print(f"[gather] attachment selection dispatch failed: {e}")


def _do_record_attachment_declaration(
    workflow_id: str,
    declared: Optional[bool],
    source: str,
    issue: Optional[dict],
    domain: str,
) -> None:
    user = _current_user()
    try:
        path = _workflow_path(workflow_id, user)
        with _lock_for(path):
            record = _load_or_new_workflow(path, workflow_id, user, issue, domain)
            audit = record.get("attachment_audit") if isinstance(record.get("attachment_audit"), dict) else {}
            audit["issue_declared_attached"] = declared if isinstance(declared, bool) else None
            audit["declaration_source"] = str(source or "")[:80]
            _reconcile_attachment_audit(audit)
            record["attachment_audit"] = audit
            _write_workflow(path, record)
    except Exception as e:
        print(f"[gather] attachment declaration failed (workflow={workflow_id}): {e}")
        return
    _maybe_rebuild_aggregates_async()


def record_attachment_declaration(
    *, workflow_id: str, declared: Optional[bool], source: str = "ai_summary",
    issue: Optional[dict] = None, domain: str = "",
) -> None:
    if not workflow_id or not getattr(sys, "frozen", False):
        return
    try:
        threading.Thread(
            target=_do_record_attachment_declaration,
            args=(workflow_id, declared, source, issue, domain), daemon=True,
        ).start()
    except Exception as e:
        print(f"[gather] attachment declaration dispatch failed: {e}")


def infer_declared_attachments(ai_analysis: Any) -> Optional[bool]:
    """Best-effort extraction of the issue's attachment claim from AI JSON.

    This value is evidence about what the issue *says*.  Actual discovery and
    download success always come from attachment_list and the transfer worker.
    """
    chunks: list[str] = []

    def _walk(value: Any) -> None:
        if isinstance(value, dict):
            for k, v in value.items():
                chunks.append(str(k))
                _walk(v)
        elif isinstance(value, (list, tuple)):
            for v in value:
                _walk(v)
        elif value is not None:
            chunks.append(str(value))

    _walk(ai_analysis)
    text = "\n".join(chunks)
    patterns = (
        r"(?:log|dump|attachment|file)s?\s+(?:files?\s+)?(?:are\s+)?attached\s*[:=-]?\s*(yes|no|true|false)",
        r"(?:attachments?|logs?|dumps?)_present\s*[:=-]?\s*(yes|no|true|false)",
        r"(?:new case attachment uploaded)\s*[:=-]?\s*(yes|no|true|false)",
    )
    for pattern in patterns:
        m = re.search(pattern, text, re.IGNORECASE)
        if m:
            return m.group(1).lower() in ("yes", "true")
    # A concise affirmative/negative sentence is common in the prompt output.
    if re.search(r"\b(?:no|without)\s+(?:log|dump|attachment|file)s?\s+(?:file\s+)?(?:is|are|were\s+)?attached\b", text, re.IGNORECASE):
        return False
    if re.search(r"\b(?:log|dump|attachment|file)s?\s+(?:file\s+)?(?:is|are|were\s+)?attached\b", text, re.IGNORECASE):
        return True
    return None


def _do_record_attachment_download_result(
    workflow_id: str,
    name: str,
    status: str,
    byte_count: Optional[int],
    latency_ms: Optional[int],
    attempt_count: int,
    error_code: str,
    issue: Optional[dict],
    domain: str,
) -> None:
    user = _current_user()
    try:
        path = _workflow_path(workflow_id, user)
        with _lock_for(path):
            record = _load_or_new_workflow(path, workflow_id, user, issue, domain)
            audit = record.get("attachment_audit") if isinstance(record.get("attachment_audit"), dict) else {}
            files = audit.get("files") if isinstance(audit.get("files"), list) else []
            target = next((f for f in files if isinstance(f, dict) and str(f.get("name") or "") == name), None)
            if target is None and len(files) < _MAX_ATTACHMENT_FILES:
                target = _new_attachment_file(name)
                files.append(target)
            if target is not None:
                target.update({
                    "selected": True,
                    "download_status": status or "failed",
                    "bytes": max(0, int(byte_count)) if byte_count is not None else None,
                    "latency_ms": max(0, int(latency_ms)) if latency_ms is not None else None,
                    "attempt_count": max(0, int(attempt_count or 0)),
                    "error_code": str(error_code or "")[:120],
                    "updated_at": _now_iso(),
                })
            audit["files"] = files
            _reconcile_attachment_audit(audit)
            record["attachment_audit"] = audit
            _write_workflow(path, record)
    except Exception as e:
        print(f"[gather] attachment result failed (workflow={workflow_id}, file={name}): {e}")
        return
    _maybe_rebuild_aggregates_async()


def record_attachment_download_result(
    *, workflow_id: str, name: str, status: str,
    byte_count: Optional[int] = None, latency_ms: Optional[int] = None,
    attempt_count: int = 0, error_code: str = "",
    issue: Optional[dict] = None, domain: str = "",
) -> None:
    if not workflow_id or not name or not getattr(sys, "frozen", False):
        return
    try:
        threading.Thread(
            target=_do_record_attachment_download_result,
            args=(workflow_id, name, status, byte_count, latency_ms,
                  attempt_count, error_code, issue, domain),
            daemon=True,
        ).start()
    except Exception as e:
        print(f"[gather] attachment result dispatch failed: {e}")


def _do_record_feature_usage(
    workflow_id: str,
    feature_code: str,
    model: str,
    usage: dict,
    issue: Optional[dict],
    domain: str,
    conversation_id: str,
    turn_id: str,
    trigger: str,
    status: str,
    latency_ms: Optional[int],
    error_code: str,
) -> None:
    user = _current_user()
    try:
        path = _workflow_path(workflow_id, user)
        with _lock_for(path):
            record = _load_or_new_workflow(path, workflow_id, user, issue, domain)
            normalised = _normalise_usage(usage)
            settled = cost_for(model, normalised)
            invocations = record.get("ai_invocations") if isinstance(record.get("ai_invocations"), list) else []
            invocations.append({
                "invocation_id": str(uuid.uuid4()),
                "ts": _now_iso(),
                "feature_code": str(feature_code or "unknown")[:80],
                "trigger": str(trigger or "")[:80],
                "domain": str(domain or record.get("domain") or "wifi"),
                "conversation_id": _safe_id(conversation_id) if conversation_id else "",
                "turn_id": _safe_id(turn_id) if turn_id else "",
                "model": str(model or ""),
                "usage": normalised,
                "cost_usd": settled if settled is not None else {
                    **_empty_cost(), "unpriced_model": model or "(unset)"
                },
                "status": str(status or "success")[:40],
                "latency_ms": max(0, int(latency_ms)) if latency_ms is not None else None,
                "error_code": str(error_code or "")[:120],
            })
            record["ai_invocations"] = invocations[-_MAX_INVOCATIONS:]
            if conversation_id:
                ids = record.get("conversation_ids") if isinstance(record.get("conversation_ids"), list) else []
                safe_conversation_id = _safe_id(conversation_id)
                if safe_conversation_id not in ids:
                    ids.append(safe_conversation_id)
                record["conversation_ids"] = ids[-200:]
            _write_workflow(path, record)
    except Exception as e:
        print(f"[gather] feature usage failed (workflow={workflow_id}, feature={feature_code}): {e}")
        return
    _maybe_rebuild_aggregates_async()


def record_feature_usage(
    *, workflow_id: str, feature_code: str, model: str = "",
    usage: Optional[dict] = None, issue: Optional[dict] = None,
    domain: str = "", conversation_id: str = "", turn_id: str = "",
    trigger: str = "", status: str = "success",
    latency_ms: Optional[int] = None, error_code: str = "",
) -> None:
    """Append one warehouse-ready FACT_AI_INVOCATION-shaped event."""
    if not workflow_id or not feature_code or not getattr(sys, "frozen", False):
        return
    try:
        threading.Thread(
            target=_do_record_feature_usage,
            args=(workflow_id, feature_code, str(model or ""), dict(usage or {}),
                  issue, domain, conversation_id, turn_id, trigger, status,
                  latency_ms, error_code),
            daemon=True,
        ).start()
    except Exception as e:
        print(f"[gather] feature usage dispatch failed: {e}")


def _do_record(
    conversation_id: str,
    workflow_id: str,
    session_id: str,
    user_message: str,
    issue: Optional[dict],
    log_path: str,
    issue_time: str,
    issue_time_window_minutes: Optional[int],
    domain: str,
) -> None:
    """Worker-side: create or update the per-conversation usage record."""
    user = _current_user()
    try:
        path = _conversation_path(conversation_id, user)
    except Exception as e:
        print(f"[gather] could not resolve path (conv={conversation_id}): {e}")
        return

    with _lock_for(path):
        record = None
        existed = path.exists()
        if existed:
            try:
                record = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                record = None
        first_send = not isinstance(record, dict)
        if first_send:
            record = _new_record(
                conversation_id, workflow_id, session_id, user, issue, log_path,
                issue_time, issue_time_window_minutes, domain
            )

        # Refresh latest context (the user may have loaded a log / set a time
        # after the conversation started).
        if issue:
            record["case"] = _clean_case(issue)
        if log_path:
            record["log_path"] = log_path
        if issue_time:
            record["issue_time"] = issue_time
        if issue_time_window_minutes is not None:
            record["issue_time_window_minutes"] = issue_time_window_minutes
        if workflow_id:
            record["workflow_id"] = _safe_id(workflow_id)
        record["updated_at"] = _now_iso()

        # Only the FIRST Send of a conversation records the question — later
        # sends just refresh context (no message accumulation).
        if first_send:
            msg = str(user_message or "").strip()
            if msg:
                if len(msg) > _MAX_MSG_CHARS:
                    msg = msg[:_MAX_MSG_CHARS] + "…"
                record["messages"] = [{"ts": _now_iso(), "text": msg}]
                record["message_count"] = 1

        # Send counter increments on every call so aggregates can distinguish
        # "how many conversations" from "how many questions sent".
        record["send_count"] = int(record.get("send_count") or 0) + 1
        # Forward-fill schema_version on existing v1 records we touch.
        record["schema_version"] = GATHER_SCHEMA_VERSION

        try:
            tmp = path.with_suffix(".json.tmp")
            tmp.write_text(
                json.dumps(record, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            tmp.replace(path)
        except Exception as e:
            print(f"[gather] write failed (conv={conversation_id}): {e}")
            return

    # Refresh silver aggregates in the background (debounced).
    _maybe_rebuild_aggregates_async()


def record_send(
    *,
    conversation_id: str,
    workflow_id: str = "",
    session_id: str = "",
    user_message: str = "",
    issue: Optional[dict] = None,
    log_path: str = "",
    issue_time: str = "",
    issue_time_window_minutes: Optional[int] = None,
    domain: str = "",
) -> None:
    """
    Record one Send into the shared Gather folder for usage analytics.

    Captures the entry session (user name, date, case context) and the first
    question on the first Send of a conversation. Subsequent Sends only
    refresh context (latest case info / log path / issue time / issue-time
    window / updated_at) on the same file — questions are NOT accumulated.
    Runs on a background thread and never raises, so the chat path is never
    blocked or broken.
    """
    if not conversation_id:
        return
    # Only the FROZEN (packaged release) build records usage analytics. In
    # DEVELOP mode (running from source, sys.frozen is False) we skip the
    # Gather write entirely so developer testing doesn't pollute the shared
    # stats on the network share.
    if not getattr(sys, "frozen", False):
        return
    # Normalise the window to a plain int (or None) so the stored record is
    # JSON-clean regardless of what the caller passed.
    try:
        window = int(issue_time_window_minutes) if issue_time_window_minutes is not None else None
    except (TypeError, ValueError):
        window = None
    try:
        t = threading.Thread(
            target=_do_record,
            args=(conversation_id, workflow_id, session_id, user_message, issue, log_path,
                  issue_time, window, domain),
            daemon=True,
        )
        t.start()
    except Exception as e:
        print(f"[gather] record_send dispatch failed: {e}")


def _do_record_usage(conversation_id: str, workflow_id: str, model: str, usage: dict) -> None:
    """Worker-side: merge one finished turn's tokens + cost into the record."""
    user = _current_user()
    try:
        path = _conversation_path(conversation_id, user)
    except Exception as e:
        print(f"[gather] usage: could not resolve path (conv={conversation_id}): {e}")
        return

    with _lock_for(path):
        # record_send() created this file at the start of the turn. If it is
        # missing the turn was not recorded (e.g. dev build) — nothing to do.
        if not path.exists():
            return
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return
        if not isinstance(record, dict):
            return

        def _n(src: dict, key: str) -> int:
            try:
                return max(0, int(src.get(key) or 0))
            except (TypeError, ValueError):
                return 0

        # ---- accumulate token totals across every Send in this conversation
        totals = record.get("usage")
        if not isinstance(totals, dict):
            totals = _empty_usage()
        for key in _empty_usage():
            totals[key] = _n(totals, key) + _n(usage, key)
        record["usage"] = totals

        # ---- settle THIS turn, then add it to the conversation total.
        # Cost is computed per turn and summed, never recomputed from the
        # running totals — that keeps the arithmetic correct if the model or
        # the rate changes partway through a conversation.
        turn_cost = cost_for(model, usage)
        prior = record.get("cost_usd")
        if not isinstance(prior, dict):
            prior = _empty_cost()

        if turn_cost is not None:
            def _f(src: dict, key: str) -> float:
                try:
                    return float(src.get(key) or 0.0)
                except (TypeError, ValueError):
                    return 0.0

            record["cost_usd"] = {
                "input": round(_f(prior, "input") + turn_cost["input"], 6),
                "cache": round(_f(prior, "cache") + turn_cost["cache"], 6),
                "output": round(_f(prior, "output") + turn_cost["output"], 6),
                "total": round(_f(prior, "total") + turn_cost["total"], 6),
                "pricing_version": turn_cost["pricing_version"],
                "rate_input_per_mtok": turn_cost["rate_input_per_mtok"],
                "rate_output_per_mtok": turn_cost["rate_output_per_mtok"],
            }
        else:
            # Unknown model: keep the token counts, leave cost unpriced rather
            # than guessing a rate. `unpriced_model` tells the ETL why.
            prior["unpriced_model"] = model or "(unset)"
            record["cost_usd"] = prior

        # ---- per-turn breakdown (bounded)
        turns = record.get("turns")
        if not isinstance(turns, list):
            turns = []
        turns.append({
            "ts": _now_iso(),
            "model": model or "",
            "llm_calls": _n(usage, "llm_calls"),
            "input_tokens": _n(usage, "input_tokens"),
            "cache_read_tokens": _n(usage, "cache_read_tokens"),
            "cache_write_tokens": _n(usage, "cache_write_tokens"),
            "output_tokens": _n(usage, "output_tokens"),
            "cost_usd": turn_cost["total"] if turn_cost else None,
        })
        record["turns"] = turns[-_MAX_TURNS:]

        if model:
            record["model"] = model
        if workflow_id:
            record["workflow_id"] = _safe_id(workflow_id)
        record["updated_at"] = _now_iso()
        record["schema_version"] = GATHER_SCHEMA_VERSION

        try:
            tmp = path.with_suffix(".json.tmp")
            tmp.write_text(
                json.dumps(record, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            tmp.replace(path)
        except Exception as e:
            print(f"[gather] usage write failed (conv={conversation_id}): {e}")
            return

    _maybe_rebuild_aggregates_async()


def record_usage(
    *,
    conversation_id: str,
    workflow_id: str = "",
    model: str = "",
    usage: Optional[dict] = None,
    issue: Optional[dict] = None,
    domain: str = "",
    turn_id: str = "",
    latency_ms: Optional[int] = None,
) -> None:
    """
    Record one finished turn's token usage + USD cost.

    Call this AFTER ``agent.chat()`` returns — ``record_send`` runs before the
    LLM is invoked, so token counts do not exist yet at that point. Pass the
    agent's ``last_turn_usage`` dict straight through.

    Same contract as record_send: background thread, never raises, and a no-op
    outside the packaged (frozen) build.
    """
    if not conversation_id or not isinstance(usage, dict):
        return
    # Nothing was actually spent — don't append an empty turn.
    if not any(int(usage.get(k) or 0) for k in ("llm_calls", "input_tokens", "output_tokens")):
        return
    # Mirrors record_send: only the packaged release build writes analytics, so
    # developer runs don't pollute the shared share-folder statistics.
    if not getattr(sys, "frozen", False):
        return
    try:
        t = threading.Thread(
            target=_do_record_usage,
            args=(conversation_id, workflow_id, str(model or ""), dict(usage)),
            daemon=True,
        )
        t.start()
        # v5 generic feature ledger.  Session totals remain for backwards
        # compatibility; this invocation is the future FACT_AI_INVOCATION row.
        if workflow_id:
            record_feature_usage(
                workflow_id=workflow_id,
                feature_code="chatbot_turn",
                model=model,
                usage=usage,
                issue=issue,
                domain=domain,
                conversation_id=conversation_id,
                turn_id=turn_id,
                trigger="chat_send",
                status="success",
                latency_ms=latency_ms,
            )
    except Exception as e:
        print(f"[gather] record_usage dispatch failed: {e}")


# ---------------------------------------------------------------------------
# Silver layer: aggregate rollups derived from the bronze per-conversation
# records. Files live at <root>/aggregates/*.json and are fully regenerated
# from the bronze records, so they're always consistent with the source of
# truth (no fragile cross-machine counter increments on the SMB share).
# ---------------------------------------------------------------------------

def iter_records() -> Iterator[dict]:
    """Yield every bronze record under <root>/sessions/<user>/*.json."""
    try:
        root = _resolve_root()
    except Exception:
        return
    sess_dir = root / "sessions"
    if not sess_dir.exists():
        return
    for user_dir in sess_dir.iterdir():
        if not user_dir.is_dir():
            continue
        for f in user_dir.glob("*.json"):
            try:
                rec = json.loads(f.read_text(encoding="utf-8"))
            except Exception:
                continue
            if isinstance(rec, dict):
                yield rec


def iter_workflows() -> Iterator[dict]:
    """Yield v5 case-workflow bronze records."""
    try:
        root = _resolve_root()
    except Exception:
        return
    workflow_dir = root / "workflows"
    if not workflow_dir.exists():
        return
    for user_dir in workflow_dir.iterdir():
        if not user_dir.is_dir():
            continue
        for f in user_dir.glob("*.json"):
            try:
                rec = json.loads(f.read_text(encoding="utf-8"))
            except Exception:
                continue
            if isinstance(rec, dict):
                yield rec


def _min_iso(a: Optional[str], b: Optional[str]) -> Optional[str]:
    if not a:
        return b
    if not b:
        return a
    return a if a <= b else b


def _max_iso(a: Optional[str], b: Optional[str]) -> Optional[str]:
    if not a:
        return b
    if not b:
        return a
    return a if a >= b else b


def compute_aggregates() -> dict:
    """Scan all bronze records and return summary + per-dimension rollups."""
    def _zero_spend() -> dict:
        return {"tokens": 0, "cost_usd": 0.0, "llm_calls": 0}

    users: dict[str, dict] = defaultdict(lambda: {
        "sessions": 0,
        "sends": 0,
        "first_seen": None,
        "last_seen": None,
        "cases": Counter(),     # case_nbr -> session count
        "spend": _zero_spend(),
    })
    domains: dict[str, dict] = defaultdict(lambda: {
        "sessions": 0,
        "sends": 0,
        "users": set(),
        "spend": _zero_spend(),
    })
    cases: dict[str, dict] = defaultdict(lambda: {
        "sessions": 0,
        "sends": 0,
        "first_seen": None,
        "last_seen": None,
        "subject": "",
        "issue_type": "",
        "users": Counter(),     # user_name -> session count
        "spend": _zero_spend(),
    })
    daily: dict[str, dict] = defaultdict(lambda: {
        "sessions": 0,
        "sends": 0,
        "users": set(),
        "cases": set(),
        "spend": _zero_spend(),
    })

    total_sessions = 0
    total_sends = 0
    all_users: set[str] = set()
    all_cases: set[str] = set()
    grand_spend = _zero_spend()
    unpriced_sessions = 0

    for rec in iter_records():
        u = (rec.get("user_name") or "anon").strip() or "anon"
        case = rec.get("case") or {}
        c = str(case.get("case_nbr") or "").strip()
        subj = str(case.get("subject") or "").strip()
        itype = str(case.get("issue_type") or "").strip()
        date = (rec.get("date") or "") or (rec.get("created_at") or "")[:10]
        # send_count is v2+; fall back to message_count (always 0 or 1) for v1.
        sends = int(rec.get("send_count") or rec.get("message_count") or 0) or 1
        created = rec.get("created_at") or ""
        updated = rec.get("updated_at") or created
        # v1-v3 records predate cost accounting; treat them as zero spend
        # rather than skipping them, so usage counts stay comparable.
        dom = str(rec.get("domain") or "").strip() or "wifi"
        usage_rec = rec.get("usage") if isinstance(rec.get("usage"), dict) else {}
        cost_rec = rec.get("cost_usd") if isinstance(rec.get("cost_usd"), dict) else {}
        try:
            rec_tokens = max(0, int(usage_rec.get("total_tokens") or 0))
        except (TypeError, ValueError):
            rec_tokens = 0
        try:
            rec_calls = max(0, int(usage_rec.get("llm_calls") or 0))
        except (TypeError, ValueError):
            rec_calls = 0
        rec_cost_raw = cost_rec.get("total")
        try:
            rec_cost = float(rec_cost_raw) if rec_cost_raw is not None else 0.0
        except (TypeError, ValueError):
            rec_cost = 0.0
        # Tokens spent but nothing priced => unknown model. Surface the count so
        # a missing rate shows up as a gap instead of looking like $0 spend.
        if rec_tokens and rec_cost_raw is None:
            unpriced_sessions += 1

        def _add_spend(bucket: dict) -> None:
            s = bucket["spend"]
            s["tokens"] += rec_tokens
            s["cost_usd"] = round(s["cost_usd"] + rec_cost, 6)
            s["llm_calls"] += rec_calls

        total_sessions += 1
        total_sends += sends
        all_users.add(u)
        if c:
            all_cases.add(c)
        grand_spend["tokens"] += rec_tokens
        grand_spend["cost_usd"] = round(grand_spend["cost_usd"] + rec_cost, 6)
        grand_spend["llm_calls"] += rec_calls

        db_dom = domains[dom]
        db_dom["sessions"] += 1
        db_dom["sends"] += sends
        db_dom["users"].add(u)
        _add_spend(db_dom)

        ub = users[u]
        ub["sessions"] += 1
        ub["sends"] += sends
        ub["first_seen"] = _min_iso(ub["first_seen"], created)
        ub["last_seen"] = _max_iso(ub["last_seen"], updated)
        _add_spend(ub)
        if c:
            ub["cases"][c] += 1

        if c:
            cb = cases[c]
            cb["sessions"] += 1
            cb["sends"] += sends
            _add_spend(cb)
            cb["first_seen"] = _min_iso(cb["first_seen"], created)
            cb["last_seen"] = _max_iso(cb["last_seen"], updated)
            if subj and not cb["subject"]:
                cb["subject"] = subj
            if itype and not cb["issue_type"]:
                cb["issue_type"] = itype
            cb["users"][u] += 1

        if date:
            db = daily[date]
            db["sessions"] += 1
            db["sends"] += sends
            db["users"].add(u)
            _add_spend(db)
            if c:
                db["cases"].add(c)

    # v5 workflow facts are aggregated separately so pre-chat AI usage and
    # attachment outcomes are visible without distorting session counts.
    feature_spend: dict[str, dict] = defaultdict(lambda: {
        "invocations": 0,
        "llm_calls": 0,
        "tokens": 0,
        "cost_usd": 0.0,
        "unpriced_invocations": 0,
        "by_domain": defaultdict(lambda: {"invocations": 0, "tokens": 0, "cost_usd": 0.0}),
    })
    attachment_stats = {
        "workflows": 0,
        "declared_yes": 0,
        "declared_no": 0,
        "declared_unknown": 0,
        "discovered_files": 0,
        "selected_files": 0,
        "download_succeeded_files": 0,
        "download_failed_files": 0,
        "reconciliation_status": Counter(),
    }
    for workflow in iter_workflows():
        dom = str(workflow.get("domain") or "wifi")
        for inv in workflow.get("ai_invocations") or []:
            if not isinstance(inv, dict):
                continue
            feature = str(inv.get("feature_code") or "unknown")
            usage = inv.get("usage") if isinstance(inv.get("usage"), dict) else {}
            cost = inv.get("cost_usd") if isinstance(inv.get("cost_usd"), dict) else {}
            try:
                calls = max(0, int(usage.get("llm_calls") or 0))
                tokens = max(0, int(usage.get("total_tokens") or 0))
            except (TypeError, ValueError):
                calls = tokens = 0
            raw_cost = cost.get("total")
            try:
                usd = float(raw_cost) if raw_cost is not None else 0.0
            except (TypeError, ValueError):
                usd = 0.0
            bucket = feature_spend[feature]
            bucket["invocations"] += 1
            bucket["llm_calls"] += calls
            bucket["tokens"] += tokens
            bucket["cost_usd"] = round(bucket["cost_usd"] + usd, 6)
            if tokens and raw_cost is None:
                bucket["unpriced_invocations"] += 1
            db = bucket["by_domain"][dom]
            db["invocations"] += 1
            db["tokens"] += tokens
            db["cost_usd"] = round(db["cost_usd"] + usd, 6)

        audit = workflow.get("attachment_audit")
        if not isinstance(audit, dict):
            continue
        attachment_stats["workflows"] += 1
        declared = audit.get("issue_declared_attached")
        attachment_stats[
            "declared_yes" if declared is True else "declared_no" if declared is False else "declared_unknown"
        ] += 1
        for src, dst in (
            ("discovered_count", "discovered_files"),
            ("selected_count", "selected_files"),
            ("download_succeeded_count", "download_succeeded_files"),
            ("download_failed_count", "download_failed_files"),
        ):
            try:
                attachment_stats[dst] += max(0, int(audit.get(src) or 0))
            except (TypeError, ValueError):
                pass
        attachment_stats["reconciliation_status"][str(audit.get("reconciliation_status") or "NONE")] += 1

    feature_spend_out = {
        feature: {**data, "by_domain": dict(data["by_domain"])}
        for feature, data in sorted(feature_spend.items())
    }
    attachment_stats["reconciliation_status"] = dict(attachment_stats["reconciliation_status"])

    users_out = {
        u: {
            "sessions": v["sessions"],
            "sends": v["sends"],
            "distinct_cases": len(v["cases"]),
            "first_seen": v["first_seen"],
            "last_seen": v["last_seen"],
            "cases": dict(v["cases"].most_common()),
            "spend": v["spend"],
        }
        for u, v in users.items()
    }
    domains_out = {
        d: {
            "sessions": v["sessions"],
            "sends": v["sends"],
            "distinct_users": len(v["users"]),
            "spend": v["spend"],
        }
        for d, v in sorted(domains.items())
    }
    cases_out = {
        c: {
            "sessions": v["sessions"],
            "sends": v["sends"],
            "distinct_users": len(v["users"]),
            "first_seen": v["first_seen"],
            "last_seen": v["last_seen"],
            "subject": v["subject"],
            "issue_type": v["issue_type"],
            "users": dict(v["users"].most_common()),
            "spend": v["spend"],
        }
        for c, v in cases.items()
    }
    daily_out = {
        d: {
            "sessions": v["sessions"],
            "sends": v["sends"],
            "distinct_users": len(v["users"]),
            "distinct_cases": len(v["cases"]),
            "spend": v["spend"],
        }
        for d, v in sorted(daily.items())
    }

    top_users = sorted(
        (
            {"user_name": u, "sessions": d["sessions"], "sends": d["sends"],
             "distinct_cases": d["distinct_cases"],
             "cost_usd": d["spend"]["cost_usd"], "tokens": d["spend"]["tokens"]}
            for u, d in users_out.items()
        ),
        key=lambda x: (x["sessions"], x["sends"]),
        reverse=True,
    )[:20]
    top_cases = sorted(
        (
            {"case_nbr": c, "sessions": d["sessions"], "sends": d["sends"],
             "distinct_users": d["distinct_users"], "subject": d["subject"],
             "cost_usd": d["spend"]["cost_usd"], "tokens": d["spend"]["tokens"]}
            for c, d in cases_out.items()
        ),
        key=lambda x: (x["sessions"], x["distinct_users"]),
        reverse=True,
    )[:20]

    summary = {
        "schema_version": GATHER_SCHEMA_VERSION,
        "generated_at": _now_iso(),
        "total_sessions": total_sessions,
        "total_sends": total_sends,
        "distinct_users": len(all_users),
        "distinct_cases": len(all_cases),
        "spend": grand_spend,
        # Sessions that burned tokens but could not be priced (model missing
        # from configs/llm_pricing.py). Non-zero means total cost is understated.
        "unpriced_sessions": unpriced_sessions,
        "by_domain": domains_out,
        "feature_spend": feature_spend_out,
        "attachment_stats": attachment_stats,
        "top_users": top_users,
        "top_cases": top_cases,
    }
    return {
        "summary": summary,
        "users": users_out,
        "cases": cases_out,
        "daily": daily_out,
        "domains": domains_out,
        "feature_spend": feature_spend_out,
        "attachment_stats": attachment_stats,
    }


def _write_json_atomic(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def rebuild_aggregates() -> dict:
    """Regenerate <root>/aggregates/*.json from the bronze layer.

    Returns the summary dict (useful as an HTTP response body).
    """
    aggs = compute_aggregates()
    try:
        root = _resolve_root()
    except Exception as e:
        print(f"[gather] aggregate rebuild: cannot resolve root: {e}")
        return aggs["summary"]
    out_dir = root / "aggregates"
    for name, data in aggs.items():
        try:
            _write_json_atomic(out_dir / f"{name}.json", data)
        except Exception as e:
            print(f"[gather] aggregate write failed ({name}): {e}")
    return aggs["summary"]


def _maybe_rebuild_aggregates_async() -> None:
    """Schedule a background aggregate rebuild, debounced by ``_AGG_COOLDOWN_SEC``."""
    global _last_agg_at
    now = time.time()
    with _agg_lock:
        if now - _last_agg_at < _AGG_COOLDOWN_SEC:
            return
        _last_agg_at = now
    try:
        threading.Thread(target=_safe_rebuild, daemon=True).start()
    except Exception as e:
        print(f"[gather] aggregate dispatch failed: {e}")


def _safe_rebuild() -> None:
    try:
        rebuild_aggregates()
    except Exception as e:
        print(f"[gather] aggregate rebuild failed: {e}")
