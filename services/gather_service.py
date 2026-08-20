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

Layout:  <root>/sessions/<user>/<conversation_id>.json            (exe)
         <root>/developer/sessions/<user>/<conversation_id>.json  (developer)
The flat root IS the exe location, exactly as before the split, so that cost
and adoption reporting can count real support traffic without developer runs
quietly inflating it. Only developer records move, and to a SIBLING folder
rather than a child: EXEs already deployed in the field keep writing to the
flat root, so every exe record — old build and new — stays in one place, and
the aggregate readers below (which list <root>/sessions without recursing)
keep finding exe records while naturally excluding developer ones. Every
record also states its own ``execution_mode``, so a file still describes
itself if it is ever moved.
A per-user subfolder makes "how many distinct people" a simple folder count,
while attribution also travels inside every record (user_name field, and now
execution_mode) so a flat DB load can ignore the folder structure entirely.

Who writes
----------
The packaged EXE always records, exactly as it always has. A source checkout
records only when ``INTELAVATAR_COLLECT_DEV=1`` is set on that machine, and
then into its own ``developer/`` tree; without it a developer run writes
nothing at all. See ``_collection_enabled``.
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
from configs.version import __version__ as APP_VERSION
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
#   v6 - Split the overloaded legacy `domain` into case_domain (wifi/bt) and
#        agent_domain (wifi/nw/bt), added packaged app_version, durable turn
#        lifecycle status, and minimal feedback_submission linkage events.
#
# The developer/ split (`execution_mode`, below) deliberately does NOT bump
# this: it adds one optional field, and one directory level for developer runs
# only, and changes the meaning of no existing field. The collector accepts
# records with and without execution_mode, and reads the flat root as "exe".
GATHER_SCHEMA_VERSION = 6

# Bucket for records that carry no domain, rather than assuming the busiest
# one. Defaulting these to "wifi" made real wifi traffic indistinguishable
# from "we don't know" — every pre-v4 record on the share has no domain, so
# they were silently inflating the wifi numbers.
UNKNOWN_DOMAIN = "unknown"

_MAX_DESC_CHARS = 2000        # keep the case summary compact for the DB
_MAX_MSG_CHARS = 4000         # cap the stored first question
_MAX_TURNS = 200              # cap turns[] so a long conversation can't bloat the file
_MAX_INVOCATIONS = 500        # bounded feature ledger per case workflow
_MAX_ATTACHMENT_FILES = 1000  # defensive cap for unusually large cases

# Minimum seconds between async aggregate rebuilds (per process). Each write
# schedules a rebuild but only one runs per cooldown window, so a burst of
# Sends doesn't fan out into a burst of full-scan rebuilds.
#
# The cooldown is per process, so the load on the share scales with the number
# of concurrent EXEs: N users means up to N full scans per window. A rebuild
# reads every session, workflow and feedback event over SMB, so keep this
# generous — the aggregates are a convenience rollup, not a live dashboard,
# and every consumer can also call rebuild_aggregates() on demand. Only
# coarse events (a finished turn, a case starting) schedule one; lifecycle and
# feedback writes deliberately do not.
_AGG_COOLDOWN_SEC = 600

# Record kinds that live under the mode sub-root, one file per id, per user.
_RECORD_DIRS = ("sessions", "workflows", "feedback_submissions")

# Which build produced a record. Reporting has to tell support traffic from
# developer traffic and a flat tree cannot: both landed in the same folders.
# Exe records stay in the flat root and developer records move beside it (see
# `_root_for_mode`); either way the record carries its mode in its own
# `execution_mode` field, so a file still describes itself if it is moved.
EXE_MODE = "exe"
DEVELOPER_MODE = "developer"
_EXECUTION_MODES = (EXE_MODE, DEVELOPER_MODE)

# Opt-in that lets a source checkout collect at all. Unset — or set to
# anything other than "1" — means a developer run writes nothing, which is
# what it did before the split. An environment variable rather than a config
# file, so the opt-in is per machine and leaves no trace in the repository.
_DEV_COLLECT_ENV = "INTELAVATAR_COLLECT_DEV"

# How long to keep buffering locally before re-probing an unreachable share.
# Probing costs up to 8 s per candidate path, and it only ever runs on a
# background writer thread, so this trades a little staleness for not paying
# that cost on every single write while the share is down.
_ROOT_REPROBE_SEC = 300

_root_cache: Optional[Path] = None
# Set only when *we* fell back to local buffering. While this is non-None the
# share gets re-probed periodically and the local records are drained up on
# the first success. Callers that pin _root_cache directly (tests) leave it
# None and keep the old resolve-once behaviour.
_outbox_root: Optional[Path] = None
_root_probed_at: float = 0.0
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


def _execution_mode() -> str:
    """Which build is running: ``exe`` (packaged) or ``developer`` (source).

    Single source of truth for both the directory level on the share and the
    `execution_mode` field inside every record, so the two cannot disagree.
    """
    return EXE_MODE if getattr(sys, "frozen", False) else DEVELOPER_MODE


def _collection_enabled() -> bool:
    """Whether this process may write Gather records at all.

    The packaged EXE always collects, exactly as before. A source checkout
    collects only when the opt-in environment variable is set on that machine,
    so a developer machine still never ships data just by running the app.
    """
    if _execution_mode() == EXE_MODE:
        return True
    return os.environ.get(_DEV_COLLECT_ENV, "").strip() == "1"


def _root_for_mode(root: Path, mode: str) -> Path:
    """Where records of ``mode`` live under ``root``.

    ``exe`` resolves to the flat root itself, not to an ``exe/`` folder. Field
    EXEs already write there and will keep doing so for as long as people run
    them, so anchoring new exe records to the same place keeps every exe
    record in one tree instead of splitting it across two for years. It also
    leaves the aggregate readers working unchanged: they list <root>/sessions
    without recursing, and ``developer/`` is a sibling of it, not a child.
    """
    return root if mode == EXE_MODE else root / mode


def _mode_root(root: Path) -> Path:
    """This process's sub-root: the flat root, or ``<root>/developer``."""
    return _root_for_mode(root, _execution_mode())


def _ensure_layout(root: Path) -> Path:
    """Create this process's record folders; returns the unchanged root."""
    mode_root = _mode_root(root)
    for sub in _RECORD_DIRS:
        (mode_root / sub).mkdir(parents=True, exist_ok=True)
    return root


def _local_root() -> Path:
    base = getattr(app_config, "avatarfiles_dir", None)
    return Path(base) / "Gather" if base else Path.cwd() / "data" / "Gather"


def _record_stamp(path: Path) -> str:
    """Last-written marker of a record file, for outbox collision resolution."""
    try:
        rec = json.loads(path.read_text(encoding="utf-8"))
        return str(rec.get("updated_at") or rec.get("submitted_at") or "")
    except Exception:
        return ""


def _drain_kind(src_dir: Path, dst_dir: Path) -> tuple[int, int, int]:
    """Move one buffered ``<kind>`` folder up to its place on the share."""
    moved = stale = failed = 0
    if not src_dir.is_dir():
        return moved, stale, failed
    for user_dir in src_dir.iterdir():
        if not user_dir.is_dir():
            continue
        for f in user_dir.glob("*.json"):
            dst = dst_dir / user_dir.name / f.name
            try:
                if dst.exists() and _record_stamp(f) <= _record_stamp(dst):
                    f.unlink()
                    stale += 1
                    continue
                dst.parent.mkdir(parents=True, exist_ok=True)
                tmp = dst.with_suffix(dst.suffix + ".tmp")
                tmp.write_bytes(f.read_bytes())
                tmp.replace(dst)
                f.unlink()
                moved += 1
            except Exception:
                failed += 1
    return moved, stale, failed


def _drain_outbox(local: Path, share: Path) -> None:
    """Move records buffered while the share was down onto the share.

    Every record is a whole-file snapshot at ``<kind>/<user>/<id>.json`` under
    its mode's root, so moving one is idempotent and needs no transaction.
    Each machine only ever writes its own user folder, so a collision means
    the same machine reached the share between two writes of the same record;
    the later snapshot wins because a record is always rewritten in full.

    Every mode is drained, not only this process's own: whichever run reaches
    the share first should deliver everything the machine has buffered. Both
    sides of every pair come from `_root_for_mode`, so the local outbox and
    the share can never disagree about where a mode lives — and a record
    buffered before the split, which sits in the flat local root, is simply
    the exe case.
    """
    if local == share or not local.exists():
        return
    pairs = [(_root_for_mode(local, mode) / kind, _root_for_mode(share, mode) / kind)
             for mode in _EXECUTION_MODES for kind in _RECORD_DIRS]
    # Transitional: an earlier revision of this change buffered exe records
    # under local/exe/. Only a machine that ran that revision can have such a
    # folder, so drain it rather than strand it, and drop this once none do.
    pairs += [(local / EXE_MODE / kind, _root_for_mode(share, EXE_MODE) / kind)
              for kind in _RECORD_DIRS]
    moved = stale = failed = 0
    for src_dir, dst_dir in pairs:
        kind_moved, kind_stale, kind_failed = _drain_kind(src_dir, dst_dir)
        moved += kind_moved
        stale += kind_stale
        failed += kind_failed
    if moved or stale or failed:
        print(f"[gather] outbox drained: {moved} uploaded, {stale} already current, "
              f"{failed} left for the next attempt")


def _resolve_root() -> Path:
    """Resolve the Gather root, preferring the share and retrying if it's down.

    A share that was unreachable at startup used to strand every record of
    that run in a local folder with no path back — silently, since a write
    that lands somewhere still looks like a success. The local folder is now
    an outbox: the share is re-probed every ``_ROOT_REPROBE_SEC`` and anything
    buffered locally is moved up on the first success.
    """
    global _root_cache, _outbox_root, _root_probed_at
    if _root_cache is not None and _outbox_root is None:
        return _root_cache
    with _root_lock:
        if _root_cache is not None and _outbox_root is None:
            return _root_cache
        now = time.time()
        if _root_cache is not None and now - _root_probed_at < _ROOT_REPROBE_SEC:
            return _root_cache          # share was down recently — keep buffering
        _root_probed_at = now

        share = helpers.get_load_path(GATHER_DIR_prim, GATHER_DIR_bkup)
        if share:
            try:
                root = _ensure_layout(Path(share))
                if _outbox_root is not None:
                    _drain_outbox(_outbox_root, root)
                    _outbox_root = None
                _root_cache = root
                print(f"[gather] using shared root: {_redact_path(root)}")
                return root
            except Exception as e:
                print(f"[gather] shared root unwritable ({_redact_path(share)}): {e}")

        if _root_cache is None:
            _outbox_root = _ensure_layout(_local_root())
            _root_cache = _outbox_root
            print(f"[gather] share unavailable — buffering locally at "
                  f"{_redact_path(_root_cache)}, will retry every {_ROOT_REPROBE_SEC}s")
        return _root_cache


def _lock_for(path: Path) -> threading.Lock:
    key = str(path)
    with _locks_guard:
        lock = _path_locks.get(key)
        if lock is None:
            lock = threading.Lock()
            _path_locks[key] = lock
        return lock


def _conversation_path(conversation_id: str, user: str) -> Path:
    root = _mode_root(_resolve_root())
    user_dir = root / "sessions" / _safe_id(user)
    user_dir.mkdir(parents=True, exist_ok=True)
    return user_dir / f"{_safe_id(conversation_id)}.json"


def _workflow_path(workflow_id: str, user: str) -> Path:
    root = _mode_root(_resolve_root())
    user_dir = root / "workflows" / _safe_id(user)
    user_dir.mkdir(parents=True, exist_ok=True)
    return user_dir / f"{_safe_id(workflow_id)}.json"


def _feedback_submission_path(feedback_event_id: str, user: str) -> Path:
    root = _mode_root(_resolve_root())
    user_dir = root / "feedback_submissions" / _safe_id(user)
    user_dir.mkdir(parents=True, exist_ok=True)
    return user_dir / f"{_safe_id(feedback_event_id)}.json"


def _agent_domain(value: Any) -> str:
    value = str(value or "").strip().lower()
    return value if value in {"wifi", "nw", "bt"} else UNKNOWN_DOMAIN


def _case_domain(issue: Optional[dict], explicit: str = "") -> str:
    issue = issue if isinstance(issue, dict) else {}
    value = str(issue.get("wifi_or_bt") or explicit or "").strip().lower()
    return value if value in {"wifi", "bt"} else UNKNOWN_DOMAIN


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
        "app_version": APP_VERSION,
        "execution_mode": _execution_mode(),
        # `domain` remains the pre-v6 agent-domain field for existing ETL.
        "domain": domain or UNKNOWN_DOMAIN,
        "agent_domain": _agent_domain(domain),
        "case_domain": _case_domain(issue),
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


_TURN_TERMINAL_STATUSES = {"completed", "failed", "cancelled"}

# A turn is written by two unordered threads: the route reports the outcome it
# saw, and the usage worker settles tokens milliseconds later with its own
# default of "completed". Rank the states so the more specific outcome wins
# regardless of arrival order — a late success must never erase a Stop or an
# exception, while a late failure may still overwrite an assumed success.
_TURN_STATUS_RANK = {"started": 0, "completed": 1, "failed": 2, "cancelled": 3}


def _upsert_turn(
    record: dict,
    *,
    turn_id: str,
    status: str,
    model: str = "",
    usage: Optional[dict] = None,
    cost_usd: Optional[float] = None,
    latency_ms: Optional[int] = None,
    error_code: str = "",
) -> dict:
    """Create/update one v6 turn without letting late success erase cancel."""
    safe_turn_id = _safe_id(turn_id) if turn_id else ""
    turns = record.get("turns") if isinstance(record.get("turns"), list) else []
    turn = next(
        (item for item in turns if isinstance(item, dict)
         and safe_turn_id and item.get("turn_id") == safe_turn_id),
        None,
    )
    now = _now_iso()
    if turn is None:
        turn = {
            "turn_id": safe_turn_id,
            "ts": now,
            "started_at": now,
            "completed_at": None,
            "status": "started",
            "error_code": "",
            "latency_ms": None,
            "model": "",
            "llm_calls": 0,
            "input_tokens": 0,
            "cache_read_tokens": 0,
            "cache_write_tokens": 0,
            "output_tokens": 0,
            "cost_usd": None,
        }
        turns.append(turn)

    wanted = "completed" if status == "success" else status
    wanted = wanted if wanted in ({"started"} | _TURN_TERMINAL_STATUSES) else "started"
    current = str(turn.get("status") or "started")
    # Stop and exceptions are authoritative for the user-visible turn. A usage
    # worker may finish milliseconds later and still books tokens/cost, but it
    # must not relabel that turn successful.
    if _TURN_STATUS_RANK.get(wanted, 0) >= _TURN_STATUS_RANK.get(current, 0):
        turn["status"] = wanted
    if wanted in _TURN_TERMINAL_STATUSES:
        turn["completed_at"] = turn.get("completed_at") or now
    if model:
        turn["model"] = str(model)
    if isinstance(usage, dict):
        normalised = _normalise_usage(usage)
        for key in (
            "llm_calls", "input_tokens", "cache_read_tokens",
            "cache_write_tokens", "output_tokens",
        ):
            turn[key] = normalised[key]
    if cost_usd is not None:
        turn["cost_usd"] = cost_usd
    if latency_ms is not None:
        try:
            turn["latency_ms"] = max(0, int(latency_ms))
        except (TypeError, ValueError):
            pass
    if error_code:
        turn["error_code"] = str(error_code)[:120]
    record["turns"] = turns[-_MAX_TURNS:]
    return turn


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
        "app_version": APP_VERSION,
        "execution_mode": _execution_mode(),
        # `domain` remains the pre-v6 case-domain field for existing ETL.
        "domain": str(domain or UNKNOWN_DOMAIN).strip().lower() or UNKNOWN_DOMAIN,
        "case_domain": _case_domain(issue, domain),
        # A workflow can use multiple agents; agents_used is authoritative.
        "agent_domain": None,
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
    # Write-once. record_workflow_start runs first, on case load, and sets the
    # CASE's technology (wifi/bt). Everything after it — feature usage, the
    # conversation link — passes the domain of whichever AGENT is running, and
    # that is a different thing: a wifi case analysed with Network Experience
    # would otherwise have its case technology rewritten to "nw". The agent is
    # recorded separately in `agents_used`.
    if domain and not record.get("domain"):
        record["domain"] = str(domain).strip().lower()
    record["case_domain"] = _case_domain(issue, record.get("case_domain") or record.get("domain"))
    record["agent_domain"] = None
    record["app_version"] = APP_VERSION
    record["execution_mode"] = _execution_mode()
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
    if not workflow_id or not _collection_enabled():
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
    if not workflow_id or not _collection_enabled():
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
    confidence: str = "",
    evidence: str = "",
    conflict: bool = False,
) -> None:
    user = _current_user()
    try:
        path = _workflow_path(workflow_id, user)
        with _lock_for(path):
            record = _load_or_new_workflow(path, workflow_id, user, issue, domain)
            audit = record.get("attachment_audit") if isinstance(record.get("attachment_audit"), dict) else {}
            audit["issue_declared_attached"] = declared if isinstance(declared, bool) else None
            audit["declaration_source"] = str(source or "")[:80]
            # Keep how the verdict was reached, not just the verdict. A None
            # from "the summary never said" and a None from "two statements
            # disagreed" need different follow-up, and the raw sentence lets a
            # bad classification be found without re-running the LLM.
            audit["declaration_confidence"] = str(confidence or "none")[:40]
            audit["declaration_evidence"] = str(evidence or "")[:600]
            audit["declaration_conflict"] = bool(conflict)
            _reconcile_attachment_audit(audit)
            record["attachment_audit"] = audit
            _write_workflow(path, record)
    except Exception as e:
        print(f"[gather] attachment declaration failed (workflow={workflow_id}): {e}")
        return
    _maybe_rebuild_aggregates_async()


def record_attachment_declaration(
    *, workflow_id: str, declared: Optional[bool] = None, source: str = "ai_summary",
    issue: Optional[dict] = None, domain: str = "",
    ai_analysis: Any = None,
) -> None:
    """Record what the issue text claims about attachments.

    Prefer passing ``ai_analysis`` — the classification then happens here and the
    confidence and the sentence it came from are stored alongside the verdict.
    ``declared`` remains accepted for callers that already classified.
    """
    if not workflow_id or not _collection_enabled():
        return
    confidence, evidence, conflict = "", "", False
    if ai_analysis is not None:
        try:
            d = infer_declared_attachments_detail(ai_analysis)
            declared = d["declared"]
            confidence, evidence, conflict = d["confidence"], d["evidence"], d["conflict"]
        except Exception:
            pass
    try:
        threading.Thread(
            target=_do_record_attachment_declaration,
            args=(workflow_id, declared, source, issue, domain,
                  confidence, evidence, conflict), daemon=True,
        ).start()
    except Exception as e:
        print(f"[gather] attachment declaration dispatch failed: {e}")


# ── Attachment-claim parsing ────────────────────────────────────────────────
# The claim is written by an LLM, so the wording is never guaranteed. These
# rules are ordered most-explicit first and are applied to ONE statement at a
# time (see _iter_statements) rather than to the whole summary joined together.
#
# Evaluating a joined blob is what made the earlier defect possible: a negation
# in one sentence and the word "attached" in another combined into a false
# positive. Per-statement evaluation removes that whole class of error instead
# of patching individual phrasings.
_DECL_NOUN = r"(?:log|dump|attachment|file|capture|trace)s?"
_DECL_COPULA = r"(?:files?\s+)?(?:(?:is|are|was|were|been)\s+)?"
_DECL_NEG_WORD = r"(?:no|not|none|never|without|n't|n/a|na)"

_DECL_RULES: tuple[tuple[str, str, Optional[bool]], ...] = (
    # 1. Explicit field with an explicit value — the shape the prompt asks for.
    #    Separator is deliberately loose: ':' '=' '-' en/em dash, or '(...)'.
    ("explicit_field", rf"{_DECL_NOUN}\s+(?:files?\s+)?attached\s*[:=\-–—(\[]*\s*"
                       r"\b(yes|no|true|false|none|n/?a)\b", None),
    ("explicit_field", r"(?:attachments?|logs?|dumps?)[_ ]present\s*[:=\-–—]*\s*"
                       r"\b(yes|no|true|false|none|n/?a)\b", None),
    ("explicit_field", r"new case attachment uploaded\s*[:=\-–—]*\s*"
                       r"\b(yes|no|true|false|none|n/?a)\b", None),
    # 2. A bare field whose value is an absence word: "Attachments: none".
    ("explicit_field", rf"(?:attachments?|log files?|logs?)\s*[:=–—]\s*"
                       rf"\b(?:{_DECL_NEG_WORD}|not\s+(?:provided|available|attached|uploaded))\b", False),
    # 3. Explicit negation in prose, in either word order.
    ("sentence", rf"\b(?:no|without)\s+{_DECL_NOUN}\s+{_DECL_COPULA}attached\b", False),
    ("sentence", rf"\b{_DECL_NOUN}\s+{_DECL_COPULA}(?:not|never)\s+attached\b", False),
    ("sentence", rf"\b(?:did\s+not|didn't|has\s+not|hasn't|have\s+not|haven't)\s+"
                 rf"(?:\w+\s+){{0,3}}(?:attach|upload|provide|share)(?:ed)?\b", False),
    ("sentence", rf"\bno\s+{_DECL_NOUN}\s+(?:\w+\s+){{0,3}}(?:uploaded|provided|shared|available)\b", False),
    # 4. Explicit affirmation. Runs last so any negation above wins.
    ("sentence", rf"\b{_DECL_NOUN}\s+{_DECL_COPULA}attached\b", True),
    ("sentence", rf"\battached\s+{_DECL_NOUN}\b", True),
    ("sentence", rf"\b{_DECL_NOUN}\s+(?:\w+\s+){{0,2}}(?:uploaded|provided|shared)\b", True),
)

_DECL_TRUE_TOKENS = {"yes", "true"}
_DECL_FALSE_TOKENS = {"no", "false", "none", "na", "n/a"}


def _iter_statements(value: Any) -> Iterator[str]:
    """Yield each leaf string of the AI JSON, split into single statements.

    Keeping statements separate is what makes the rules safe: a rule can only
    ever see one claim at a time, so wording elsewhere in the summary cannot
    flip the verdict.
    """
    if isinstance(value, dict):
        for k, v in value.items():
            # Keys carry meaning too ("Log files attached": "Yes").
            if isinstance(v, (str, int, float, bool)) or v is None:
                yield f"{k}: {v}"
            else:
                yield str(k)
                yield from _iter_statements(v)
    elif isinstance(value, (list, tuple)):
        for v in value:
            yield from _iter_statements(v)
    elif value is not None:
        text = str(value)
        # Normalise the separators an LLM varies freely, then split on hard
        # boundaries only — never on '.', which would shred version strings
        # such as "10.7.2.1_93286".
        text = text.replace("—", " - ").replace("–", " - ")
        for part in re.split(r"[\r\n;•]+", text):
            part = re.sub(r"\s+", " ", part).strip()
            if part:
                yield part


def infer_declared_attachments_detail(ai_analysis: Any) -> dict:
    """Classify the issue's attachment claim, with the evidence behind it.

    Returns a dict shaped for the warehouse:
        declared    True / False / None (None = the summary never says)
        confidence  "explicit_field" | "sentence" | "none"
        evidence    the statement the verdict came from (truncated)
        conflict    True when statements of equal confidence disagree

    The value is evidence about what the issue *says*. What was actually found
    and downloaded always comes from attachment_list and the transfer worker.
    """
    verdicts: list[tuple[str, bool, str]] = []   # (confidence, declared, evidence)

    for statement in _iter_statements(ai_analysis):
        for confidence, pattern, fixed in _DECL_RULES:
            m = re.search(pattern, statement, re.IGNORECASE)
            if not m:
                continue
            if fixed is None:
                token = (m.group(1) or "").lower().replace("/", "")
                if token in _DECL_TRUE_TOKENS:
                    declared = True
                elif token in _DECL_FALSE_TOKENS:
                    declared = False
                else:
                    continue
            else:
                declared = fixed
            verdicts.append((confidence, declared, statement[:300]))
            break  # first (most explicit) rule wins for this statement

    if not verdicts:
        return {"declared": None, "confidence": "none", "evidence": "", "conflict": False}

    explicit = [v for v in verdicts if v[0] == "explicit_field"]
    chosen_pool = explicit or verdicts
    values = {v[1] for v in chosen_pool}
    conflict = len(values) > 1
    if conflict:
        # Disagreement at the same confidence is not something to guess at —
        # report unknown and keep the evidence so it can be reviewed.
        return {
            "declared": None,
            "confidence": chosen_pool[0][0],
            "evidence": " | ".join(v[2] for v in chosen_pool[:3])[:600],
            "conflict": True,
        }
    return {
        "declared": chosen_pool[0][1],
        "confidence": chosen_pool[0][0],
        "evidence": chosen_pool[0][2],
        "conflict": False,
    }


def infer_declared_attachments(ai_analysis: Any) -> Optional[bool]:
    """Back-compatible wrapper: just the True/False/None verdict."""
    return infer_declared_attachments_detail(ai_analysis)["declared"]


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
    if not workflow_id or not name or not _collection_enabled():
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
                "app_version": APP_VERSION,
                "feature_code": str(feature_code or "unknown")[:80],
                "trigger": str(trigger or "")[:80],
                # `domain` is retained as the pre-v6 agent-domain alias.
                "domain": str(domain or record.get("domain") or UNKNOWN_DOMAIN),
                "agent_domain": _agent_domain(domain),
                "case_domain": str(record.get("case_domain") or _case_domain(issue)),
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
    if not workflow_id or not feature_code or not _collection_enabled():
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
    turn_id: str = "",
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
        if path.exists():
            try:
                record = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                record = None
        if not isinstance(record, dict):
            record = _new_record(
                conversation_id, workflow_id, session_id, user, issue, log_path,
                issue_time, issue_time_window_minutes, domain
            )
        # "Have we captured the question yet?" — deliberately NOT "did the file
        # exist?". _do_record_usage may have created the record first (the two
        # workers are unordered), and that skeleton carries no message. Keying
        # off the file's existence would drop the user's question in that case.
        needs_question = not record.get("messages")
        # session_id is only known here; backfill it onto a usage-made skeleton.
        if session_id and not record.get("session_id"):
            record["session_id"] = session_id

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
        # v6 forward-fill. The legacy session `domain` continues to mean the
        # selected agent, while case_domain is derived independently.
        record["domain"] = str(record.get("domain") or domain or UNKNOWN_DOMAIN)
        record["agent_domain"] = _agent_domain(domain or record.get("agent_domain") or record.get("domain"))
        case_dom = _case_domain(issue)
        if case_dom != UNKNOWN_DOMAIN or not record.get("case_domain"):
            record["case_domain"] = case_dom
        record["app_version"] = APP_VERSION
        record["execution_mode"] = _execution_mode()
        record["updated_at"] = _now_iso()

        # Only the FIRST Send of a conversation records the question — later
        # sends just refresh context (no message accumulation).
        if needs_question:
            msg = str(user_message or "").strip()
            if msg:
                if len(msg) > _MAX_MSG_CHARS:
                    msg = msg[:_MAX_MSG_CHARS] + "…"
                record["messages"] = [{"ts": _now_iso(), "text": msg}]
                record["message_count"] = 1

        # Send counter increments on every call so aggregates can distinguish
        # "how many conversations" from "how many questions sent".
        record["send_count"] = int(record.get("send_count") or 0) + 1
        if turn_id:
            _upsert_turn(record, turn_id=turn_id, status="started")
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

    # Back-link the conversation onto its workflow. Without this the workflow's
    # conversation_ids only listed conversations that happened to trigger a
    # feature invocation, leaving a field that looks authoritative but is not.
    # Only the first Send of a conversation writes, so this costs one extra
    # small write per conversation, not per turn.
    if workflow_id:
        _link_conversation_to_workflow(workflow_id, conversation_id, issue, domain)

    # Refresh silver aggregates in the background (debounced).
    _maybe_rebuild_aggregates_async()


def _link_conversation_to_workflow(
    workflow_id: str, conversation_id: str, issue: Optional[dict], domain: str,
) -> None:
    """
    Link a conversation to its workflow and record which agent it ran on.

    Two different things are being tracked, and conflating them loses data:

      workflow["domain"]      the CASE's technology, from Salesforce when the
                              case loads. Only ever "wifi" or "bt" — the field
                              behind it (`wifi_or_bt`) has no third value.
      workflow["agents_used"] which of the three agents on the main page the
                              user actually opened: wifi, nw or bt.

    "nw" is a tool, not a case type, so it can never appear in `domain`. And
    the attachment claim is classified on the select-attachments page, before
    any agent has been chosen, so it cannot be attributed to one at the time
    it is written. `agents_used` is what makes the per-agent split possible
    after the fact: it lives on the same workflow as `attachment_audit`, so
    the two join with no extra lookup.
    """
    user = _current_user()
    try:
        wpath = _workflow_path(workflow_id, user)
        with _lock_for(wpath):
            wrecord = _load_or_new_workflow(wpath, workflow_id, user, issue, domain)
            ids = wrecord.get("conversation_ids")
            if not isinstance(ids, list):
                ids = []
            agents = wrecord.get("agents_used")
            if not isinstance(agents, list):
                agents = []

            safe = _safe_id(conversation_id)
            agent = str(domain or "").strip().lower()
            new_conversation = safe not in ids
            new_agent = bool(agent) and agent not in agents
            if not new_conversation and not new_agent:
                return          # nothing changed — skip the write entirely

            if new_conversation:
                ids.append(safe)
                wrecord["conversation_ids"] = ids[-200:]
            if new_agent:
                agents.append(agent)
                wrecord["agents_used"] = sorted(agents)
            _write_workflow(wpath, wrecord)
    except Exception as e:
        print(f"[gather] conversation link failed (workflow={workflow_id}): {e}")


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
    turn_id: str = "",
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
    # The packaged EXE always records. A source checkout records only if the
    # machine opted in (INTELAVATAR_COLLECT_DEV=1), and then under developer/,
    # so developer testing never lands in the support numbers either way.
    if not _collection_enabled():
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
                  issue_time, window, domain, turn_id),
            daemon=True,
        )
        t.start()
    except Exception as e:
        print(f"[gather] record_send dispatch failed: {e}")


def _do_record_usage(
    conversation_id: str,
    workflow_id: str,
    model: str,
    usage: dict,
    issue: Optional[dict] = None,
    domain: str = "",
    turn_id: str = "",
    latency_ms: Optional[int] = None,
    status: str = "completed",
    error_code: str = "",
) -> None:
    """Worker-side: merge one finished turn's tokens + cost into the record."""
    user = _current_user()
    try:
        path = _conversation_path(conversation_id, user)
    except Exception as e:
        print(f"[gather] usage: could not resolve path (conv={conversation_id}): {e}")
        return

    with _lock_for(path):
        # record_send() normally creates this file at the start of the turn,
        # but both run on their own background threads and nothing orders
        # them. Waiting for the other thread is not an option either — it may
        # have failed outright. So create the record here when it is missing
        # and let _do_record fill in the context it owns; it preserves every
        # field it does not itself set. Bailing out instead would silently
        # discard the turn together with its settled cost.
        record = None
        if path.exists():
            try:
                record = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                record = None
        if not isinstance(record, dict):
            record = _new_record(
                conversation_id, workflow_id, "", user, issue,
                "", "", None, domain,
            )

        def _n(src: dict, key: str) -> int:
            try:
                return max(0, int(src.get(key) or 0))
            except (TypeError, ValueError):
                return 0

        # ---- accumulate token totals across every Send in this conversation.
        # Normalise first: a caller that supplies the buckets but not
        # total_tokens would otherwise leave the conversation total at 0 while
        # every other number on the record was right — a silent zero in the
        # by-domain token rollups, with the cost beside it looking correct.
        # The feature ledger already normalises; this keeps the two agreeing.
        usage = _normalise_usage(usage)
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

        # Once a conversation has hit a model with no rate, its total is a
        # floor and stays one however many priced turns follow. The priced
        # branch below rebuilds cost_usd from scratch, so the marker has to be
        # read before it and re-applied after — otherwise the next Send that
        # happens to be priced silently clears it and the session reads as
        # fully accounted for.
        unpriced_model = prior.get("unpriced_model")
        try:
            unpriced_turns = max(0, int(prior.get("unpriced_turns") or 0))
        except (TypeError, ValueError):
            unpriced_turns = 0

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
            # Unknown model: keep the token counts, and keep whatever earlier
            # turns did settle to, rather than guessing a rate or throwing away
            # money we already know about. `total` stays None only while
            # nothing at all has been priced.
            unpriced_model = model or "(unset)"
            unpriced_turns += 1
            record["cost_usd"] = prior

        # `unpriced_model` names the last model with no rate; `unpriced_turns`
        # says how much of the conversation is missing from `total`, which is
        # what separates one stray turn from a figure that means almost
        # nothing. Both are sticky for the life of the conversation.
        if unpriced_model:
            record["cost_usd"]["unpriced_model"] = unpriced_model
            record["cost_usd"]["unpriced_turns"] = unpriced_turns

        # ---- per-turn breakdown (bounded). v6 updates the `started` row made
        # by record_send; legacy callers with no turn_id still get one row.
        _upsert_turn(
            record,
            turn_id=turn_id,
            status=status,
            model=model,
            usage=usage,
            cost_usd=turn_cost["total"] if turn_cost else None,
            latency_ms=latency_ms,
            error_code=error_code,
        )

        if model:
            record["model"] = model
        if workflow_id:
            record["workflow_id"] = _safe_id(workflow_id)
        record["domain"] = str(record.get("domain") or domain or UNKNOWN_DOMAIN)
        record["agent_domain"] = _agent_domain(domain or record.get("agent_domain") or record.get("domain"))
        case_dom = _case_domain(issue)
        if case_dom != UNKNOWN_DOMAIN or not record.get("case_domain"):
            record["case_domain"] = case_dom
        record["app_version"] = APP_VERSION
        record["execution_mode"] = _execution_mode()
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
    status: str = "completed",
    error_code: str = "",
) -> None:
    """
    Record one finished turn's token usage + USD cost.

    Call this AFTER ``agent.chat()`` returns — ``record_send`` runs before the
    LLM is invoked, so token counts do not exist yet at that point. Pass the
    agent's ``last_turn_usage`` dict straight through.

    Same contract as record_send: background thread, never raises, and a no-op
    outside the packaged build unless the machine opted into developer
    collection.
    """
    if not conversation_id:
        return
    # A turn can finish before the provider reports usage (or without making
    # an LLM call). It still needs a terminal lifecycle state in schema v6.
    usage = usage if isinstance(usage, dict) else {}

    # Nothing was actually spent — don't append an empty turn.
    #
    # Cache buckets count towards "spent": once prompt caching is enabled a
    # turn can be billed almost entirely as cache reads, with input/output at
    # or near zero. Leaving them out would drop exactly the turns caching is
    # meant to make cheap.
    #
    # The coercion is guarded because `usage` is whatever the caller handed
    # over, and this function documents that it never raises — an unguarded
    # int() on a non-numeric value would break that contract in the chat path.
    def _spent(key: str) -> int:
        try:
            return max(0, int(usage.get(key) or 0))
        except (TypeError, ValueError):
            return 0

    has_spend = any(_spent(k) for k in (
        "llm_calls", "input_tokens", "output_tokens",
        "cache_read_tokens", "cache_write_tokens",
    ))
    # Mirrors record_send: the packaged build always writes, a source checkout
    # only when opted in, and then into its own developer/ tree.
    if not _collection_enabled():
        return
    if not has_spend:
        # Lifecycle state is useful even for a zero-token cancellation/failure.
        record_turn_status(
            conversation_id=conversation_id,
            turn_id=turn_id,
            status=status,
            workflow_id=workflow_id,
            issue=issue,
            domain=domain,
            latency_ms=latency_ms,
            error_code=error_code,
        )
        return
    try:
        t = threading.Thread(
            target=_do_record_usage,
            # issue/domain are forwarded so the worker can still build a valid
            # record if it happens to reach the file before record_send's own
            # worker does — the two threads have no ordering guarantee.
            args=(conversation_id, workflow_id, str(model or ""), dict(usage),
                  issue, domain, turn_id, latency_ms, status, error_code),
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
                status="success" if status == "completed" else status,
                latency_ms=latency_ms,
                error_code=error_code,
            )
    except Exception as e:
        print(f"[gather] record_usage dispatch failed: {e}")


def _do_record_turn_status(
    conversation_id: str,
    workflow_id: str,
    turn_id: str,
    status: str,
    issue: Optional[dict],
    domain: str,
    latency_ms: Optional[int],
    error_code: str,
) -> None:
    user = _current_user()
    try:
        path = _conversation_path(conversation_id, user)
        with _lock_for(path):
            record = None
            if path.exists():
                try:
                    record = json.loads(path.read_text(encoding="utf-8"))
                except Exception:
                    record = None
            if not isinstance(record, dict):
                record = _new_record(
                    conversation_id, workflow_id, "", user, issue,
                    "", "", None, domain,
                )
            _upsert_turn(
                record,
                turn_id=turn_id,
                status=status,
                latency_ms=latency_ms,
                error_code=error_code,
            )
            record["app_version"] = APP_VERSION
            record["execution_mode"] = _execution_mode()
            record["schema_version"] = GATHER_SCHEMA_VERSION
            record["updated_at"] = _now_iso()
            tmp = path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(path)
    except Exception as e:
        print(f"[gather] turn status failed (conv={conversation_id}, turn={turn_id}): {e}")
    # No aggregate rebuild: a lifecycle transition changes no rolled-up number,
    # and the record_usage that settles the same turn schedules one anyway.


def record_turn_status(
    *,
    conversation_id: str,
    turn_id: str,
    status: str,
    workflow_id: str = "",
    issue: Optional[dict] = None,
    domain: str = "",
    latency_ms: Optional[int] = None,
    error_code: str = "",
) -> None:
    """Persist a turn lifecycle transition; never changes workflow status."""
    if (
        not conversation_id
        or not turn_id
        or status not in ({"started"} | _TURN_TERMINAL_STATUSES)
        or not _collection_enabled()
    ):
        return
    try:
        threading.Thread(
            target=_do_record_turn_status,
            args=(conversation_id, workflow_id, turn_id, status, issue, domain,
                  latency_ms, error_code),
            daemon=True,
        ).start()
    except Exception as e:
        print(f"[gather] turn status dispatch failed: {e}")


def _do_record_feedback_submit(
    feedback_event_id: str,
    conversation_id: str,
    workflow_id: str,
    session_id: str,
    turn_id: str,
    issue: Optional[dict],
    domain: str,
) -> None:
    """Write one minimal, idempotent feedback-to-case linkage event."""
    user = _current_user()
    try:
        path = _feedback_submission_path(feedback_event_id, user)
        case = _clean_case(issue)
        event = {
            "schema_version": GATHER_SCHEMA_VERSION,
            "record_type": "feedback_submission",
            "feedback_event_id": _safe_id(feedback_event_id),
            "submitted_at": _now_iso(),
            "date": _today(),
            "user_name": user,
            "case_nbr": case.get("case_nbr") or "",
            "workflow_id": _safe_id(workflow_id) if workflow_id else "",
            "conversation_id": _safe_id(conversation_id) if conversation_id else "",
            "session_id": session_id or "",
            "turn_id": _safe_id(turn_id) if turn_id else "",
            "app_version": APP_VERSION,
            "execution_mode": _execution_mode(),
            # `domain` stays as the pre-v6 agent-domain alias.
            "domain": _agent_domain(domain),
            "agent_domain": _agent_domain(domain),
            "case_domain": _case_domain(issue),
        }
        with _lock_for(path):
            _write_json_atomic(path, event)
    except Exception as e:
        print(f"[gather] feedback submit failed (event={feedback_event_id}): {e}")
    # No aggregate rebuild: feedback_stats is a counter nobody reads in real
    # time, and Submit lands right after a turn that already scheduled one.


def record_feedback_submit(
    *,
    feedback_event_id: str,
    conversation_id: str,
    turn_id: str,
    workflow_id: str = "",
    session_id: str = "",
    issue: Optional[dict] = None,
    domain: str = "",
) -> None:
    """Count a detailed Feedback Submit and retain only its join keys."""
    if (
        not feedback_event_id
        or not conversation_id
        or not turn_id
        or not _collection_enabled()
    ):
        return
    try:
        threading.Thread(
            target=_do_record_feedback_submit,
            args=(feedback_event_id, conversation_id, workflow_id, session_id,
                  turn_id, issue, domain),
            daemon=True,
        ).start()
    except Exception as e:
        print(f"[gather] feedback submit dispatch failed: {e}")


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


def iter_feedback_submissions() -> Iterator[dict]:
    """Yield v6 detailed-feedback submission linkage events."""
    try:
        root = _resolve_root()
    except Exception:
        return
    event_dir = root / "feedback_submissions"
    if not event_dir.exists():
        return
    for user_dir in event_dir.iterdir():
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
    # Two independent rollups of the same sessions: which agent ran (wifi /
    # nw / bt) and which technology the case is (wifi / bt). A wifi case
    # analysed by the NW agent lands in agent "nw" and case "wifi".
    domains: dict[str, dict] = defaultdict(lambda: {
        "sessions": 0,
        "sends": 0,
        "users": set(),
        "spend": _zero_spend(),
    })
    case_domains: dict[str, dict] = defaultdict(lambda: {
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
        dom = str(rec.get("agent_domain") or rec.get("domain") or "").strip() or UNKNOWN_DOMAIN
        # Pre-v6 records have no case_domain. Their legacy `domain` was the
        # agent, but wifi_chatbot and bt_chatbot only ever ran on their own
        # technology, so for those two the agent identifies the case as well.
        # Only "nw" is genuinely ambiguous and stays unknown.
        case_dom = str(rec.get("case_domain") or "").strip()
        if not case_dom:
            case_dom = dom if dom in ("wifi", "bt") else UNKNOWN_DOMAIN
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
        # A conversation that priced some turns and not others carries a
        # numeric total *and* the marker: that total is a floor, not the bill,
        # so it belongs in this count too.
        if rec_tokens and (rec_cost_raw is None or cost_rec.get("unpriced_model")):
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

        for bucket in (domains[dom], case_domains[case_dom]):
            bucket["sessions"] += 1
            bucket["sends"] += sends
            bucket["users"].add(u)
            _add_spend(bucket)

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
        # by_domain is the pre-v6 alias for the agent rollup — the same meaning
        # it carries in the session summary, and the same one the per-invocation
        # `domain` field carries. Keying it by case technology instead would
        # make one name mean two things inside a single summary.json.
        "by_domain": defaultdict(lambda: {"invocations": 0, "tokens": 0, "cost_usd": 0.0}),
        "by_case_domain": defaultdict(lambda: {"invocations": 0, "tokens": 0, "cost_usd": 0.0}),
        "by_agent_domain": defaultdict(lambda: {"invocations": 0, "tokens": 0, "cost_usd": 0.0}),
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
        # Same shape again, keyed by the agent the workflow was analysed with.
        "by_agent": defaultdict(lambda: {
            "workflows": 0,
            "declared_yes": 0,
            "declared_no": 0,
            "declared_unknown": 0,
            "discovered_files": 0,
            "selected_files": 0,
            "download_succeeded_files": 0,
            "download_failed_files": 0,
        }),
    }
    for workflow in iter_workflows():
        case_dom = str(workflow.get("case_domain") or workflow.get("domain") or UNKNOWN_DOMAIN)
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
            agent_dom = str(inv.get("agent_domain") or inv.get("domain") or UNKNOWN_DOMAIN)
            for dimension in ("by_domain", "by_agent_domain"):
                db = bucket[dimension][agent_dom]
                db["invocations"] += 1
                db["tokens"] += tokens
                db["cost_usd"] = round(db["cost_usd"] + usd, 6)
            cdb = bucket["by_case_domain"][case_dom]
            cdb["invocations"] += 1
            cdb["tokens"] += tokens
            cdb["cost_usd"] = round(cdb["cost_usd"] + usd, 6)

        audit = workflow.get("attachment_audit")
        if not isinstance(audit, dict):
            continue
        attachment_stats["workflows"] += 1
        declared = audit.get("issue_declared_attached")
        declared_key = (
            "declared_yes" if declared is True
            else "declared_no" if declared is False
            else "declared_unknown"
        )
        attachment_stats[declared_key] += 1

        # The claim is classified before an agent is chosen, so it carries the
        # case technology (wifi/bt) and never "nw". Split it by the agents the
        # workflow actually ran instead — that is the wifi/nw/bt distinction
        # the three buttons on the main page make. A workflow analysed with
        # two agents counts under both; "none" means no agent was opened.
        agents = workflow.get("agents_used")
        agents = [a for a in agents if a] if isinstance(agents, list) else []
        for agent in (agents or ["none"]):
            ab = attachment_stats["by_agent"][str(agent)]
            ab["workflows"] += 1
            ab[declared_key] += 1

        for src, dst in (
            ("discovered_count", "discovered_files"),
            ("selected_count", "selected_files"),
            ("download_succeeded_count", "download_succeeded_files"),
            ("download_failed_count", "download_failed_files"),
        ):
            try:
                value = max(0, int(audit.get(src) or 0))
            except (TypeError, ValueError):
                continue
            attachment_stats[dst] += value
            for agent in (agents or ["none"]):
                attachment_stats["by_agent"][str(agent)][dst] += value

        attachment_stats["reconciliation_status"][str(audit.get("reconciliation_status") or "NONE")] += 1

    # v6 feedback submission facts deliberately carry no feedback text. They
    # answer only: how many detailed submits did each user make, and which
    # Case Numbers can those events be joined to in the feedback store?
    feedback_users: dict[str, dict] = defaultdict(
        lambda: {"submissions": 0, "case_numbers": set()}
    )
    feedback_cases: dict[str, dict] = defaultdict(
        lambda: {"submissions": 0, "users": set()}
    )
    feedback_total = 0
    for event in iter_feedback_submissions():
        feedback_total += 1
        user_name = str(event.get("user_name") or "anon").strip() or "anon"
        case_nbr = str(event.get("case_nbr") or "").strip()
        feedback_users[user_name]["submissions"] += 1
        if case_nbr:
            feedback_users[user_name]["case_numbers"].add(case_nbr)
            feedback_cases[case_nbr]["submissions"] += 1
            feedback_cases[case_nbr]["users"].add(user_name)
    feedback_stats = {
        "total_submissions": feedback_total,
        "distinct_users": len(feedback_users),
        "distinct_cases": len(feedback_cases),
        "by_user": {
            user_name: {
                "submissions": data["submissions"],
                "distinct_cases": len(data["case_numbers"]),
                "case_numbers": sorted(data["case_numbers"]),
            }
            for user_name, data in sorted(feedback_users.items())
        },
        "by_case": {
            case_nbr: {
                "submissions": data["submissions"],
                "distinct_users": len(data["users"]),
                "users": sorted(data["users"]),
            }
            for case_nbr, data in sorted(feedback_cases.items())
        },
    }

    feature_spend_out = {
        feature: {
            **data,
            "by_domain": dict(data["by_domain"]),
            "by_case_domain": dict(data["by_case_domain"]),
            "by_agent_domain": dict(data["by_agent_domain"]),
        }
        for feature, data in sorted(feature_spend.items())
    }
    attachment_stats["reconciliation_status"] = dict(attachment_stats["reconciliation_status"])
    attachment_stats["by_agent"] = {
        agent: dict(data)
        for agent, data in sorted(attachment_stats["by_agent"].items())
    }

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
    def _domains_out(source: dict[str, dict]) -> dict:
        return {
            d: {
                "sessions": v["sessions"],
                "sends": v["sends"],
                "distinct_users": len(v["users"]),
                "spend": v["spend"],
            }
            for d, v in sorted(source.items())
        }

    domains_out = _domains_out(domains)
    case_domains_out = _domains_out(case_domains)
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
        # by_domain is the pre-v6 name for the agent rollup; kept for existing
        # consumers. by_case_domain is the new, independent technology split.
        "by_domain": domains_out,
        "by_agent_domain": domains_out,
        "by_case_domain": case_domains_out,
        "feature_spend": feature_spend_out,
        "attachment_stats": attachment_stats,
        "feedback_stats": feedback_stats,
        "top_users": top_users,
        "top_cases": top_cases,
    }
    return {
        "summary": summary,
        "users": users_out,
        "cases": cases_out,
        "daily": daily_out,
        "domains": domains_out,
        "agent_domains": domains_out,
        "case_domains": case_domains_out,
        "feature_spend": feature_spend_out,
        "attachment_stats": attachment_stats,
        "feedback_stats": feedback_stats,
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
    if _outbox_root is not None:
        # Buffering locally: the only records visible are this machine's
        # undelivered ones, so a rollup built now would be a wrong answer
        # written to a folder nobody reads. It rebuilds after the drain.
        return
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
