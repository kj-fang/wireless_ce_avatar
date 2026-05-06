"""
Feedback Sidecar Service

Side-car module that records:
  1. Conversation snapshots — one JSON file per conversation_id, containing
     the issue context plus a list of turns (user_message + agent_response +
     skills used). Updated on every agent turn.
  2. Feedback votes — append-only JSONL stream of thumbs-up / thumbs-down
     events.

Design goals:
  - Independent of the chatbot agent: agent runtime is never blocked by IO
    or exceptions thrown here. Every public function swallows its own errors.
  - No DB. Files only. Future Postgres migration reads these files as the
    "bronze" layer.
  - Append-only writes are atomic on POSIX/NTFS for line-sized payloads.
  - Concurrent-safe via per-file locks.
"""

from __future__ import annotations

import getpass
import json
import os
import re
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Optional

from configs.global_configs import app_config
from configs.path_configs import FEEDBACK_DIR_prim, FEEDBACK_DIR_bkup
from utils import helpers


# --- Storage location ----------------------------------------------------
#
# Resolution order (cached for the lifetime of the process):
#   1. Shared primary  \\infs089b.iil.intel.com\...\feedback\<user>
#   2. Shared backup   \\infs089.iil.intel.com\...\feedback\<user>
#   3. Local fallback  <avatarfiles_dir>\feedback   (off-VPN / share down)
#
# Per-user partition is the security mitigation:
#   - SMB cannot reliably synchronise per-process locks across machines.
#     Two users voting at the same instant would race for one file. Putting
#     each user under their own subfolder makes those writes target
#     different files, so the existing in-process locks are sufficient.
#   - Per-user folder also makes attribution explicit, so a "submitted_by"
#     field is recorded once at write time rather than inferred from the
#     Windows path embedded in log_path.

_root_cache: Optional[Path] = None
_root_lock = threading.Lock()


def _current_user() -> str:
    """Best-effort Windows username; sanitised so it can be a folder name."""
    try:
        u = getpass.getuser() or os.environ.get("USERNAME", "") or "anon"
    except Exception:
        u = os.environ.get("USERNAME", "") or "anon"
    # Folder-safe — strip anything other than alphanum / dash / underscore.
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", u).strip("._-") or "anon"
    return safe


def _resolve_root() -> Path:
    """
    Resolve the feedback root once and cache it. Runs the share probe in
    a worker thread (helpers.get_load_path) with an 8-second timeout so a
    slow / off-VPN machine doesn't stall the chat path.
    """
    global _root_cache
    if _root_cache is not None:
        return _root_cache
    with _root_lock:
        if _root_cache is not None:
            return _root_cache

        user = _current_user()
        share = helpers.get_load_path(FEEDBACK_DIR_prim, FEEDBACK_DIR_bkup)
        if share:
            try:
                root = Path(share) / user
                (root / "conversations").mkdir(parents=True, exist_ok=True)
                _root_cache = root
                print(f"[feedback] using shared root: {root}")
                return root
            except Exception as e:
                print(f"[feedback] shared root unwritable ({share}): {e} — falling back to local")

        base = getattr(app_config, "avatarfiles_dir", None)
        root = Path(base) / "feedback" if base else Path.cwd() / "data" / "feedback"
        (root / "conversations").mkdir(parents=True, exist_ok=True)
        _root_cache = root
        print(f"[feedback] using local root: {root}")
        return root


def _feedback_root() -> Path:
    """Return the resolved feedback root, creating sub-dirs on demand."""
    return _resolve_root()


# Prewarm: resolve the share in a daemon thread at import time so the SMB
# probe (up to 16 s combined timeout) doesn't block the FIRST 👍/👎 click.
# By the time a user can actually vote, _root_cache is almost always set.
def _prewarm_root() -> None:
    try:
        _resolve_root()
    except Exception as e:
        print(f"[feedback] prewarm failed: {e}")


threading.Thread(target=_prewarm_root, name="feedback-prewarm", daemon=True).start()


# --- Privacy scrub -------------------------------------------------------
# Replace the OS-specific user home prefix in any path-like string with a
# canonical placeholder, so a Windows path written by user A doesn't leak
# user A's machine identity to user B reading the shared snapshot.
# `submitted_by` carries the explicit attribution instead.
_USER_HOME_RE = re.compile(r"([A-Za-z]:\\Users\\)([^\\/]+)", re.IGNORECASE)


def _scrub_user_path(s: Any) -> Any:
    """Redact `C:\\Users\\<name>\\` → `<USERHOME>\\` in path-like strings."""
    if not isinstance(s, str) or not s:
        return s
    return _USER_HOME_RE.sub(r"\1<USERHOME>", s)


def _deep_scrub(obj: Any) -> Any:
    """Recursively apply _scrub_user_path to every string in a JSON tree."""
    if isinstance(obj, str):
        return _scrub_user_path(obj)
    if isinstance(obj, dict):
        return {k: _deep_scrub(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_deep_scrub(v) for v in obj]
    return obj


def _feedback_log_path() -> Path:
    return _feedback_root() / "feedback.jsonl"


def _feedback_detail_path() -> Path:
    return _feedback_root() / "feedback_details.jsonl"


def _conversation_path(conversation_id: str) -> Path:
    return _feedback_root() / "conversations" / f"{conversation_id}.json"


# Allowed `category` values for structured feedback issues. Kept as a stable
# label space so downstream training data has consistent classes.
DETAIL_CATEGORIES = {
    "wrong_skill",   # the chosen skill is wrong; another would have been better
    "wrong_input",   # right skill, wrong arguments / filter
    "wrong_order",   # called at the wrong point in the reasoning sequence
    "bad_output",    # skill ran, but its output was unhelpful
    "missing_step",  # the agent should have done an additional step
    "stuck",         # reasoning got stuck / looped
    "other",
}

DETAIL_SCOPES = {"overall", "skill", "step"}


# --- Locks ---------------------------------------------------------------
# One lock per file path keeps concurrent SSE threads from interleaving
# writes to the same conversation snapshot.
_locks_guard = threading.Lock()
_path_locks: dict[str, threading.Lock] = {}


# --- Async IO worker ----------------------------------------------------
# All disk writes (jsonl appends + snapshot flushes) are pushed onto a
# single background queue so HTTP endpoints (/feedback/vote, /feedback/detail
# and the chat SSE close-out) return immediately. This is the key to making
# 👍/👎 feel snappy when the share is on a slow VPN link.
#
# Job shapes:
#   ("append_jsonl", path: Path, line: str)
#   ("flush", conversation_id: str)
import queue as _queue_mod  # noqa: E402

_io_queue: "_queue_mod.Queue[tuple]" = _queue_mod.Queue()
_worker_started = False
_worker_start_lock = threading.Lock()


def _ensure_worker() -> None:
    """Lazily start the IO worker thread on first enqueue."""
    global _worker_started
    if _worker_started:
        return
    with _worker_start_lock:
        if _worker_started:
            return
        t = threading.Thread(target=_io_worker_loop, name="feedback-io", daemon=True)
        t.start()
        _worker_started = True


def _io_worker_loop() -> None:
    while True:
        try:
            job = _io_queue.get()
        except Exception:
            continue
        if job is None:
            return
        try:
            kind = job[0]
            if kind == "append_jsonl":
                _, path, line = job
                _do_append_jsonl(path, line)
            elif kind == "flush":
                _, conversation_id = job
                _do_flush(conversation_id)
        except Exception as e:
            print(f"[feedback] worker job {job!r} failed: {e}")


def _do_append_jsonl(path: Path, line: str) -> None:
    try:
        with _lock_for(path):
            with path.open("a", encoding="utf-8") as f:
                f.write(line)
    except Exception as e:
        print(f"[feedback] append_jsonl failed ({path}): {e}")


def _do_flush(conversation_id: str) -> None:
    """Worker-side implementation of flush — actual disk IO."""
    if not conversation_id:
        return
    with _pending_lock:
        snap = _pending_buffer.get(conversation_id)
        if not snap:
            return
        # Deep copy so the write isn't perturbed by concurrent mutation,
        # and strip internal-only flags that don't belong on disk.
        snapshot_copy = json.loads(json.dumps(snap, ensure_ascii=False))
    snapshot_copy.pop("_persisted", None)
    path = _conversation_path(conversation_id)
    try:
        with _lock_for(path):
            tmp = path.with_suffix(".json.tmp")
            tmp.write_text(
                json.dumps(snapshot_copy, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            tmp.replace(path)
    except Exception as e:
        print(f"[feedback] flush failed (conv={conversation_id}): {e}")


def _enqueue_flush(conversation_id: str) -> None:
    if not conversation_id:
        return
    _ensure_worker()
    _io_queue.put(("flush", conversation_id))


def _enqueue_append(path: Path, record: dict) -> None:
    line = json.dumps(record, ensure_ascii=False) + "\n"
    _ensure_worker()
    _io_queue.put(("append_jsonl", path, line))


def _lock_for(path: Path) -> threading.Lock:
    key = str(path)
    with _locks_guard:
        lock = _path_locks.get(key)
        if lock is None:
            lock = threading.Lock()
            _path_locks[key] = lock
        return lock


# --- Helpers -------------------------------------------------------------

def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _extract_skills_used(steps: list[dict]) -> list[dict]:
    """
    Walk the step_callback events captured during a chat turn and pull out
    a clean list of skill invocations. We watch for messages emitted by
    log_chatbot_service when a tool/skill is invoked, e.g.:
        "🔍 **Fetching filtered logs** for `skill_name`..."
        "🔍 **Invoking** `skill_name`..."
    The order of returned entries preserves invocation order.
    """
    import re

    skills: list[dict] = []
    inv_re = re.compile(r"`([^`]+)`")
    for idx, step in enumerate(steps or []):
        if not isinstance(step, dict):
            continue
        content = step.get("content", "")
        if not isinstance(content, str):
            continue
        is_invoke = (
            "Fetching filtered logs" in content
            or "Invoking" in content
            or "Invoking skill" in content
        )
        if not is_invoke:
            continue
        m = inv_re.search(content)
        if not m:
            continue
        skill_id = m.group(1).strip()
        if not skill_id:
            continue
        skills.append({"skill_id": skill_id, "step_index": idx})
    return skills


def _summarise_response(result: Any) -> str:
    """
    Produce a compact text summary of the agent.chat() result for the
    snapshot. Used for the human-readable `agent_response` field; the full
    payload lives in `agent_response_full` for training data.
    """
    if not isinstance(result, dict):
        return ""
    rtype = result.get("type", "")
    data = result.get("data", "")
    if rtype == "text" and isinstance(data, str):
        return data
    if rtype in ("report", "partial_report") and isinstance(data, dict):
        # Pull the most informative free-text fields.
        parts = []
        if data.get("root_cause_summary"):
            parts.append(f"Root cause: {data['root_cause_summary']}")
        if data.get("confidence_score") is not None:
            parts.append(f"Confidence: {data['confidence_score']}")
        if data.get("recommended_actions"):
            actions = data["recommended_actions"]
            if isinstance(actions, list):
                parts.append("Actions: " + "; ".join(str(a) for a in actions))
        return "\n".join(parts) or json.dumps(data, ensure_ascii=False)[:1000]
    if rtype == "error":
        return f"[error] {data}"
    return json.dumps(result, ensure_ascii=False)[:1000]


def _serialise_steps(steps: list) -> list:
    """
    Coerce the raw step dicts streamed during a chat turn into JSON-safe
    rows. The agent emits steps as `{"role": ..., "content": ...}`; we keep
    that shape and drop anything else (tool_call_id, tool name, etc., if
    present) so the snapshot stays small but lossless for the trace itself.
    """
    out = []
    for s in steps or []:
        if not isinstance(s, dict):
            continue
        role = s.get("role")
        content = s.get("content")
        row = {
            "role": role if isinstance(role, str) else "",
            "content": content if isinstance(content, str) else
                       (json.dumps(content, ensure_ascii=False) if content is not None else ""),
        }
        out.append(row)
    return out


def _full_response_payload(result: Any) -> Any:
    """
    Return the raw payload of the agent's reply, unsummarised. For
    `report` / `partial_report` this is the full report dict (so
    skill_findings + markdown_summary are preserved for training). For
    `text` / `error` it returns the raw `data` field. Returns None for
    anything we don't recognise so the snapshot stays small.
    """
    if not isinstance(result, dict):
        return None
    rtype = result.get("type", "")
    data = result.get("data")
    if rtype in ("report", "partial_report") and isinstance(data, dict):
        return data
    if rtype in ("text", "error"):
        return data
    return None


# --- Vote-gated persistence ---------------------------------------------
#
# Conversations and their turns are buffered in memory. They are only
# flushed to disk when a user actually casts a 👍/👎 (or submits a Layer-2
# detail). Conversations that nobody votes on never produce any file —
# this keeps the shared training-data layer free of unlabelled noise.
#
# Once a conversation has been flushed, all subsequent record_turn calls
# also write through to disk, so post-vote turns stay in context.

_pending_buffer: dict = {}    # conversation_id -> snapshot dict (in memory)
_pending_lock = threading.Lock()


def _new_snapshot(conversation_id: str, session_id: str,
                  issue: Optional[dict], log_path: str) -> dict:
    return {
        "conversation_id": conversation_id,
        "session_id": session_id or "",
        "submitted_by": _current_user(),
        "started_at": _now_iso(),
        "ended_at": _now_iso(),
        "log_path": _scrub_user_path(log_path or ""),
        "issue": issue or {},
        "turns": [],
        "skill_set_summary": [],
    }


def _recompute_skill_summary(snapshot: dict) -> None:
    seen = set()
    ordered: list[str] = []
    for t in snapshot.get("turns", []):
        for s in t.get("skills_used", []):
            sid = s.get("skill_id")
            if sid and sid not in seen:
                seen.add(sid)
                ordered.append(sid)
    snapshot["skill_set_summary"] = ordered


def _flush_buffered_to_disk(conversation_id: str) -> bool:
    """
    Enqueue an asynchronous flush of the buffered conversation snapshot.
    Returns True if there is something to flush. The actual disk write
    happens on the IO worker thread so callers don't block on SMB IO.
    """
    if not conversation_id:
        return False
    with _pending_lock:
        if conversation_id not in _pending_buffer:
            return False
    _enqueue_flush(conversation_id)
    return True


# --- Public API ----------------------------------------------------------

def ensure_conversation(
    conversation_id: str,
    session_id: str,
    issue: Optional[dict] = None,
    log_path: str = "",
) -> None:
    """
    Buffer a new conversation in memory. NOT written to disk yet —
    nothing persists until a vote arrives.
    """
    if not conversation_id:
        return
    try:
        with _pending_lock:
            if conversation_id in _pending_buffer:
                return
            _pending_buffer[conversation_id] = _new_snapshot(
                conversation_id, session_id, issue, log_path
            )
    except Exception as e:
        print(f"[feedback] ensure_conversation failed: {e}")


def record_turn(
    *,
    session_id: str,
    conversation_id: str,
    turn_id: str,
    user_message: str,
    agent_result: Any,
    steps: Optional[Iterable[dict]] = None,
    mode: str = "tools",
    duration_ms: int = 0,
    issue: Optional[dict] = None,
    log_path: str = "",
) -> None:
    """
    Append one turn to the conversation buffer. Writes to disk only if
    the conversation already has a snapshot file (i.e., a previous turn
    in this conversation was voted on). Never raises.
    """
    if not conversation_id or not turn_id:
        return
    try:
        steps_list = list(steps or [])
        skills_used = _extract_skills_used(steps_list)

        turn_record = {
            "turn_id": turn_id,
            "ts": _now_iso(),
            "user_message": _scrub_user_path(user_message or ""),
            "agent_response": _scrub_user_path(_summarise_response(agent_result)),
            # Full unsummarised payload — preserves skill_findings,
            # markdown_summary, etc. for downstream training labels.
            "agent_response_full": _deep_scrub(_full_response_payload(agent_result)),
            "result_type": (agent_result or {}).get("type") if isinstance(agent_result, dict) else "",
            "mode": mode,
            "skills_used": skills_used,
            "step_count": len(steps_list),
            # Full reasoning trace (every step_callback event). Lets future
            # analysis see what the agent did, not just which skills it
            # invoked. Stripped to JSON-safe primitives.
            "steps_trace": _deep_scrub(_serialise_steps(steps_list)),
            "duration_ms": duration_ms,
            "feedback": None,
        }

        already_persisted = False
        with _pending_lock:
            snap = _pending_buffer.get(conversation_id)
            if snap is None:
                snap = _new_snapshot(conversation_id, session_id, issue, log_path)
                _pending_buffer[conversation_id] = snap
                # Track whether a previous turn already flushed this conv to
                # disk; if so we want write-through. We mark this on the
                # buffer so we don't have to hit disk to check.
            if issue:
                snap["issue"] = issue
            if log_path:
                snap["log_path"] = _scrub_user_path(log_path)
            snap["ended_at"] = _now_iso()
            snap.setdefault("turns", []).append(turn_record)
            _recompute_skill_summary(snap)
            already_persisted = bool(snap.get("_persisted"))

        # Write through ONLY if a previous turn already triggered a flush.
        # Enqueue (worker handles SMB IO) so the chat SSE close doesn't block.
        if already_persisted:
            _enqueue_flush(conversation_id)
    except Exception as e:
        print(f"[feedback] record_turn failed (turn={turn_id}): {e}")


def record_vote(
    *,
    session_id: str,
    conversation_id: str,
    turn_id: str,
    vote: int,
) -> bool:
    """
    Append a vote event to feedback.jsonl AND patch the matching turn in
    the conversation snapshot so a turn's feedback is visible in one place.

    vote: +1 (thumbs up) or -1 (thumbs down). Other values are rejected.
    Returns True on success, False otherwise.
    """
    if vote not in (1, -1):
        return False
    if not conversation_id or not turn_id:
        return False

    event = {
        "ts": _now_iso(),
        "session_id": session_id or "",
        "submitted_by": _current_user(),
        "conversation_id": conversation_id,
        "turn_id": turn_id,
        "vote": vote,
    }

    # 1) Patch the in-memory buffer immediately — fast, no disk IO. This is
    # the source of truth that the next flush will serialise.
    with _pending_lock:
        snap = _pending_buffer.get(conversation_id)
        if snap is not None:
            for t in snap.get("turns", []):
                if t.get("turn_id") == turn_id:
                    t["feedback"] = {"vote": vote, "ts": event["ts"]}
                    break
            snap["_persisted"] = True   # subsequent record_turn writes through

    # 2) Enqueue both writes — return to client immediately.
    _enqueue_append(_feedback_log_path(), event)
    _enqueue_flush(conversation_id)
    return True


def _sanitise_issues(issues: Any) -> list[dict]:
    """
    Coerce a raw `issues` list from the request into a clean list of dicts
    that match the documented schema. All fields are optional; obviously
    bad rows are dropped silently.
    """
    if not isinstance(issues, list):
        return []
    cleaned: list[dict] = []
    for it in issues:
        if not isinstance(it, dict):
            continue
        scope = (it.get("scope") or "").strip()
        if scope and scope not in DETAIL_SCOPES:
            scope = ""
        category = (it.get("category") or "").strip()
        if category and category not in DETAIL_CATEGORIES:
            category = "other"

        # step_index: optional non-negative int
        step_index = it.get("step_index")
        try:
            step_index = int(step_index) if step_index is not None and step_index != "" else None
        except (TypeError, ValueError):
            step_index = None

        row = {
            "scope":      scope or None,
            "skill_id":   (it.get("skill_id") or "").strip() or None,
            "step_index": step_index,
            "step_label": (it.get("step_label") or "").strip() or None,
            "category":   category or None,
            "should_be":  (it.get("should_be") or "").strip() or None,
            "comment":    (it.get("comment") or "").strip() or None,
        }
        # Drop rows that say literally nothing.
        if not any(v for v in row.values()):
            continue
        cleaned.append(row)
    return cleaned


def record_detail(
    *,
    session_id: str,
    conversation_id: str,
    turn_id: str,
    vote: Optional[int] = None,
    issues: Optional[list] = None,
    general_comment: str = "",
) -> bool:
    """
    Append a structured detailed feedback record. Used by the "More feedback"
    modal: lets the user point at specific skills / steps and label what
    went wrong, so the data is suitable as training labels.

    All fields are optional — at minimum we need conversation_id + turn_id.
    Returns True on success.
    """
    if not conversation_id or not turn_id:
        return False

    cleaned_issues = _sanitise_issues(issues)
    general_comment = (general_comment or "").strip()
    # If everything is empty and there is no vote either, ignore — nothing to log.
    if not cleaned_issues and not general_comment and vote not in (1, -1):
        return False

    record = {
        "ts": _now_iso(),
        "session_id": session_id or "",
        "submitted_by": _current_user(),
        "conversation_id": conversation_id,
        "turn_id": turn_id,
        "vote": vote if vote in (1, -1) else None,
        "issues": cleaned_issues,
        "general_comment": general_comment or None,
    }

    # 1) Patch the in-memory buffer immediately. Preserve any prior
    # Layer-1 vote unless this submission overrides it.
    with _pending_lock:
        snap = _pending_buffer.get(conversation_id)
        if snap is not None:
            for t in snap.get("turns", []):
                if t.get("turn_id") != turn_id:
                    continue
                fb = t.get("feedback") or {}
                if record["vote"] is not None:
                    fb["vote"] = record["vote"]
                fb["details"] = {
                    "ts": record["ts"],
                    "issues": cleaned_issues,
                    "general_comment": record["general_comment"],
                }
                t["feedback"] = fb
                break
            snap["_persisted"] = True

    # 2) Enqueue both writes — return to client immediately.
    _enqueue_append(_feedback_detail_path(), record)
    _enqueue_flush(conversation_id)
    return True


def get_recent_votes(limit: int = 50) -> list[dict]:
    """Read the last N feedback events. For debugging / UI inspection only."""
    path = _feedback_log_path()
    if not path.exists():
        return []
    try:
        with path.open("r", encoding="utf-8") as f:
            lines = f.readlines()
        out = []
        for line in lines[-limit:]:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except Exception:
                continue
        return out
    except Exception as e:
        print(f"[feedback] get_recent_votes failed: {e}")
        return []
