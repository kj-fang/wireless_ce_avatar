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


# ---------------------------------------------------------------------------
# Record schema version.
#
# Every record this service writes — JSONL rows, per-turn dicts inside the
# conversation snapshot, and the snapshot itself — carries an integer
# `schema_version` field. ETL / downstream Reflectors use this to know which
# parsing rules to apply when new fields are added in the future, so a
# schema bump never breaks bulk loads.
#
# Bump checklist when adding/removing fields:
#   1. Increment RECORD_SCHEMA_VERSION
#   2. Add a one-liner in CHANGELOG-style comment below describing the diff
#
# Version history:
#   v1  - Initial bronze-layer format (vote / detail / step_vote / snapshot)
#   v2  - Added `schema_version` field + `parent_message_id` on turn records
#         (linking multiple turns emitted from a single Send for the
#         multi-incident analysis flow).
#   v3  - Added issue-time (analysis-anchor) feedback fields on detail records:
#         `issue_time_problem` (free-text: what was wrong with the time),
#         `correct_issue_time` (what it should have been), `used_issue_time`
#         (the time the turn actually used), and `log_has_date`. When
#         `log_has_date` is False the analysed log carried no dates, so the
#         three time fields are time-only "HH:MM:SS" (e.g. DDD/tracefmt logs)
#         rather than "MM/DD/YYYY-HH:MM:SS" — ACE can group/interpret them
#         without ambiguity.
# ---------------------------------------------------------------------------
RECORD_SCHEMA_VERSION = 3


# --- Storage location ----------------------------------------------------
#
# Resolution order (cached for the lifetime of the process):
#   1. Shared primary  \\infs089b.iil.intel.com\...\feedback
#   2. Shared backup   \\infs089.iil.intel.com\...\feedback
#   3. Local fallback  <avatarfiles_dir>\feedback   (off-VPN / share down)
#
# Layout is flat — every user writes to the same files. Attribution travels
# inside the record itself via the `submitted_by` field, so a reviewer can
# read the whole feedback history from a single `feedback.jsonl` without
# crawling per-user subfolders.
#
# Cross-machine write contention on the JSONL files is acceptable for our
# volume: appended lines are small (a few KB at most), the in-process lock
# serialises writes within each process, and even an SMB-level interleave
# would only ever corrupt a single line — which the reader can simply skip.

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

    The root is shared across all users — no per-user partitioning.
    """
    global _root_cache
    if _root_cache is not None:
        return _root_cache
    with _root_lock:
        if _root_cache is not None:
            return _root_cache

        share = helpers.get_load_path(FEEDBACK_DIR_prim, FEEDBACK_DIR_bkup)
        if share:
            try:
                root = Path(share)
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


# Prewarm: resolve the share in a daemon thread so the SMB probe (up to
# 16 s combined timeout) doesn't block the FIRST 👍/👎 click. By the
# time a user can actually vote, _root_cache is almost always set.
#
# We deliberately do NOT auto-fire at module import: doing that races
# `set_up()` and would resolve the local fallback before
# `app_config.avatarfiles_dir` is populated, leaving feedback files in
# an inconsistent location (cwd-relative `data/feedback/` instead of
# `<avatarfiles_dir>/feedback/`). Callers should invoke `prewarm()`
# from set_up_app.py once configuration is ready.
def prewarm() -> None:
    """Kick off share resolution in a daemon thread. Idempotent."""
    if _root_cache is not None:
        return

    def _run():
        try:
            _resolve_root()
        except Exception as e:
            print(f"[feedback] prewarm failed: {e}")

    threading.Thread(target=_run, name="feedback-prewarm", daemon=True).start()


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


# --- Domain partitioning -------------------------------------------------
#
# Feedback streams are split by analysis DOMAIN so the Bluetooth bot's
# training signal never mixes with the Wi-Fi bot's. The domain becomes a
# filename prefix on every JSONL stream and conversation snapshot:
#
#   wifi / "" (default) → feedback.jsonl,    conversations/<id>.json
#                         (LEGACY names, kept byte-for-byte so existing
#                          bronze-layer ETL keeps working)
#   bt                  → bt_feedback.jsonl, conversations/bt_<id>.json
#
# A domain is resolved once per conversation: the value passed when the
# conversation is first buffered wins, and every later vote/detail for that
# conversation inherits it (see _resolve_domain), so a JSONL row and its
# conversation snapshot can never disagree on which stream they belong to.

_DOMAIN_PREFIXES = {"bt": "bt_"}   # canonical-domain → filename prefix


def _norm_domain(domain: Any) -> str:
    """Normalise a raw domain hint to a canonical key. '' = wifi/default."""
    d = domain.strip().lower() if isinstance(domain, str) else ""
    if d in ("bt", "bluetooth"):
        return "bt"
    return ""  # wifi / default — keep legacy filenames unchanged


def _domain_prefix(domain: Any) -> str:
    """Filename prefix for a domain ('' for wifi/default, 'bt_' for BT)."""
    return _DOMAIN_PREFIXES.get(_norm_domain(domain), "")


def _resolve_domain(conversation_id: str, domain_hint: Any) -> str:
    """
    Authoritative domain for a conversation: prefer the value recorded on
    the buffered snapshot (set when the conversation was first created),
    falling back to the caller's hint. Guarantees a vote/detail JSONL row
    lands in the same stream as its conversation snapshot.
    """
    with _pending_lock:
        snap = _pending_buffer.get(conversation_id)
        if snap and snap.get("_domain"):
            return snap["_domain"]
    return _norm_domain(domain_hint)


def _feedback_log_path(domain: str = "") -> Path:
    return _feedback_root() / f"{_domain_prefix(domain)}feedback.jsonl"


def _feedback_detail_path(domain: str = "") -> Path:
    return _feedback_root() / f"{_domain_prefix(domain)}feedback_details.jsonl"


def _step_votes_path(domain: str = "") -> Path:
    """Per-step thumbs from the live conversation view (one row each)."""
    return _feedback_root() / f"{_domain_prefix(domain)}feedback_step_votes.jsonl"


def _helpful_skills_path(domain: str = "") -> Path:
    """`Glad it helped` quick-prompt picks — positive ACE signal stream."""
    return _feedback_root() / f"{_domain_prefix(domain)}feedback_helpful_skills.jsonl"


def _skill_assessments_path(domain: str = "") -> Path:
    """Per-skill chip assessments (helpful / redundant / wrong) from the
    in-line response UI."""
    return _feedback_root() / f"{_domain_prefix(domain)}feedback_skill_assessments.jsonl"


# Strict pattern for client-supplied IDs that end up as filesystem path
# components. UUIDs (server-generated) match this; short alphanumeric IDs
# do too. Anything containing `/`, `\`, `..`, control chars or other
# odd punctuation is REJECTED to prevent path-traversal attacks via the
# /feedback/* endpoints, which all read conversation_id / turn_id from
# the request body.
_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,80}$")


def _safe_id(value: Any, fallback: str = "unknown") -> str:
    """
    Sanitise an ID coming from an untrusted source so it's safe to use
    in filesystem path components. Returns `fallback` when the value is
    missing, the wrong type, empty after trimming, or doesn't match the
    strict allow-list pattern. The allow-list also implicitly rejects
    `..`, `.`, and path separators since those contain non-matching
    characters or violate the length check.
    """
    if not isinstance(value, str):
        return fallback
    cleaned = value.strip()
    if not _SAFE_ID_RE.match(cleaned):
        return fallback
    return cleaned


def _conversation_path(conversation_id: str, domain: str = "") -> Path:
    # Hard-stop path traversal: even though the server generates
    # conversation_id as a uuid4, the /feedback/* endpoints accept
    # whatever the client sends — sanitise before the value ever
    # becomes a path component.
    safe = _safe_id(conversation_id)
    return _feedback_root() / "conversations" / f"{_domain_prefix(domain)}{safe}.json"


def _attached_logs_dir(conversation_id: str, domain: str = "") -> Path:
    """Shared sub-folder for opt-in attached session logs, keyed by conv id."""
    safe = _safe_id(conversation_id)
    return _feedback_root() / "logs" / f"{_domain_prefix(domain)}{safe}"


def _feedback_weight(*, has_detail: bool, yaml_modified: bool) -> str:
    """
    Classify a feedback event into one of two weight buckets.

      "high" — the user filled out structured details, attached a YAML, or
               edited the skill configuration earlier in the session.
      "low"  — the user only cast a thumbs-up / thumbs-down.

    Reviewers sort the queue by weight so high-signal feedback surfaces first.
    """
    return "high" if (has_detail or yaml_modified) else "low"


# Allowed `category` values for structured feedback issues. Kept as a stable
# label space so downstream training data has consistent classes. These
# describe the common Wi-Fi-log-debug failure modes a user will tag.
DETAIL_CATEGORIES = {
    "missed_evidence",     # agent missed a critical log line / event
    "wrong_skill",         # wrong skill chosen for the symptom
    "wrong_conclusion",    # wrong root cause / category
    "hallucinated",        # cited log lines that don't exist
    "stuck_repeated",      # loop / repeated same fetch
    "incomplete",          # stopped before finishing the analysis
    "over_investigated",   # did unnecessary follow-up
    "bad_output",          # output unclear / misleading
    "other",
    # Legacy values still accepted for back-compat with older clients.
    "wrong_input",
    "wrong_order",
    "missing_step",
    "stuck",
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
            elif kind == "copy_file":
                _, src, dst = job
                _do_copy_file(src, dst)
        except Exception as e:
            print(f"[feedback] worker job {job!r} failed: {e}")


def _do_copy_file(src: Path, dst: Path) -> None:
    """Worker-side file copy with create-dir + mtime preservation."""
    import shutil as _shutil
    try:
        dst.parent.mkdir(parents=True, exist_ok=True)
        _shutil.copy2(str(src), str(dst))
    except Exception as e:
        print(f"[feedback] copy_file failed ({src} → {dst}): {e}")


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
    # `_domain` is an internal routing field — drive the on-disk path from it
    # then strip it so the snapshot file carries only the human-readable
    # `domain` field.
    domain_for_path = snapshot_copy.pop("_domain", "")
    path = _conversation_path(conversation_id, domain_for_path)
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


def _enqueue_copy(src: Path, dst: Path) -> None:
    _ensure_worker()
    _io_queue.put(("copy_file", src, dst))


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
                  issue: Optional[dict], log_path: str,
                  domain: str = "") -> dict:
    norm = _norm_domain(domain)
    return {
        "schema_version": RECORD_SCHEMA_VERSION,
        "conversation_id": conversation_id,
        "session_id": session_id or "",
        "submitted_by": _current_user(),
        # Human-readable analysis domain on disk ("wifi" | "bt").
        "domain": norm or "wifi",
        # Internal routing key (""/"bt") — stripped before the file is
        # written (see _do_flush); drives the bt_ filename prefix.
        "_domain": norm,
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
    domain: str = "",
) -> None:
    """
    Buffer a new conversation in memory. NOT written to disk yet —
    nothing persists until a vote arrives.

    domain: "" (wifi/default) or "bt". Recorded on the snapshot so every
            later vote/detail for this conversation routes to the same
            (possibly bt_-prefixed) feedback stream.
    """
    if not conversation_id:
        return
    try:
        with _pending_lock:
            if conversation_id in _pending_buffer:
                return
            _pending_buffer[conversation_id] = _new_snapshot(
                conversation_id, session_id, issue, log_path, domain
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
    parent_message_id: str = "",
    domain: str = "",
) -> None:
    """
    Append one turn to the conversation buffer. Writes to disk only if
    the conversation already has a snapshot file (i.e., a previous turn
    in this conversation was voted on). Never raises.

    parent_message_id:
        UUID stamped client-side once per Send click. When a single Send
        produces multiple incident analyses (multi-time chained calls),
        every resulting turn shares the same parent_message_id, letting
        downstream ETL recover the co-firing relationship that's
        otherwise lost when turns are flattened into a per-row table.
    """
    if not conversation_id or not turn_id:
        return
    try:
        steps_list = list(steps or [])
        skills_used = _extract_skills_used(steps_list)

        turn_record = {
            "schema_version": RECORD_SCHEMA_VERSION,
            "turn_id": turn_id,
            "parent_message_id": (parent_message_id or "").strip() or None,
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
                snap = _new_snapshot(conversation_id, session_id, issue, log_path, domain)
                _pending_buffer[conversation_id] = snap
                # Track whether a previous turn already flushed this conv to
                # disk; if so we want write-through. We mark this on the
                # buffer so we don't have to hit disk to check.
            elif domain and not snap.get("_domain"):
                # Back-fill domain on a snapshot created before domain
                # tracking (e.g. ensure_conversation ran on an older path).
                norm = _norm_domain(domain)
                snap["_domain"] = norm
                snap["domain"] = norm or "wifi"
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
    yaml_modified: bool = False,
    domain: str = "",
) -> bool:
    """
    Append a vote event to feedback.jsonl AND patch the matching turn in
    the conversation snapshot so a turn's feedback is visible in one place.

    vote: +1 (thumbs up), -1 (thumbs down), or 0 (clear a previous vote).
    Any other value is rejected.
    yaml_modified: True if the user edited the skill YAML during this session
                   — promotes the event to weight="high".

    Session logs are NOT attached here. Log attachment is an explicit opt-in
    inside the "More feedback" modal (typically when the user gives a
    thumbs-down and chooses to share the log for diagnosis).

    Returns True on success, False otherwise.
    """
    if vote not in (1, -1, 0):
        return False
    if not conversation_id or not turn_id:
        return False

    eff_domain = _resolve_domain(conversation_id, domain)
    weight = _feedback_weight(has_detail=False, yaml_modified=bool(yaml_modified))
    event = {
        "schema_version": RECORD_SCHEMA_VERSION,
        "ts": _now_iso(),
        "session_id": session_id or "",
        "submitted_by": _current_user(),
        "domain": eff_domain or "wifi",
        "conversation_id": conversation_id,
        "turn_id": turn_id,
        "vote": vote,
        "weight": weight,
        "yaml_modified": bool(yaml_modified),
    }

    # 1) Patch the in-memory buffer immediately — fast, no disk IO. This is
    # the source of truth that the next flush will serialise.
    with _pending_lock:
        snap = _pending_buffer.get(conversation_id)
        if snap is not None:
            for t in snap.get("turns", []):
                if t.get("turn_id") == turn_id:
                    if vote == 0:
                        # Vote retracted — drop the inline verdict so the
                        # ACE pipeline treats this turn as untagged again.
                        t.pop("feedback", None)
                    else:
                        t["feedback"] = {
                            "vote": vote,
                            "ts": event["ts"],
                            "weight": weight,
                            "yaml_modified": bool(yaml_modified),
                        }
                    break
            snap["_persisted"] = True   # subsequent record_turn writes through

    # 2) Enqueue writes — return to client immediately.
    _enqueue_append(_feedback_log_path(eff_domain), event)
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


# Whitelist of `feedback_layer` values — the dispatch hint that tells
# downstream Reflectors whether to route this record to the skill-bullet
# pipeline or the agent-prompt pipeline.
FEEDBACK_LAYERS = {"skill", "agent", "both"}

# Whitelist of `agent_workflow` assessments. These map directly to
# specific sections of the agent's system prompt that ACE Reflector /
# Curator can target:
#   * appropriate            → no delta needed (positive signal)
#   * stopped_too_early      → Phase 2 trigger bullet ("invoke more skills when …")
#   * over_investigated      → Phase 2 / termination bullet ("stop after Phase 1 if …")
#   * loop_or_stuck          → constraint bullet ("avoid repeating the same fetch")
#   * wrong_direction        → routing / direction bullet ("when keywords X
#                              dominate, pursue Y first") — covers wrong
#                              skill at ANY step, not just Phase 1
AGENT_WORKFLOW_TAGS = {
    "appropriate",
    "stopped_too_early",
    "over_investigated",
    "loop_or_stuck",
    "wrong_direction",
    # Legacy value (renamed from "wrong_first_skill"). Kept so older
    # cached client submissions keep validating.
    "wrong_phase1_skill",
}


# Whitelist of conclusion tag values accepted from the "More feedback"
# modal. Keeping the list server-side prevents typos / arbitrary strings
# from polluting the structured feedback stream that Reflector / Curator
# downstream will aggregate over.
CORRECT_CONCLUSION_TAGS = {
    "OS_INITIATED",
    "RF_INTERFERENCE",
    "AP_KICK",
    "FIRMWARE_CRASH",
    "MCC_MISMATCH",
    "DRIVER_INIT_FAILURE",
    "AUTH_FAILURE",
    "ASSOC_FAILURE",
    "HANDSHAKE_FAILURE",
    "WAKE_RESUME_DELAY",
    "BIOS_CONFIG_ISSUE",
    "ROAMING_DECISION",
    "OTHER",
}

# Bluetooth conclusion categories — the common ibtpci / HCI controller
# failure modes a BT triage reviewer tags. Kept separate from the Wi-Fi set
# above so each chatbot offers domain-appropriate options; BT submissions
# land in the bt_-prefixed feedback streams regardless.
BT_CONCLUSION_TAGS = {
    "FW_FATAL_EXCEPTION",        # FATAL/SYSTEM EXCEPTION in controller (FW assert)
    "FW_DOWNLOAD_FAILURE",       # FW image / SFI burst download failed
    "HW_ERROR",                  # HardwareError / HW reset failure
    "DEVICE_YELLOW_BANG",        # device lost / Code 43 / YB
    "SURPRISE_REMOVAL",          # surprise removal / device disappeared
    "RECOVERY_FAILURE",          # PLDR rejected / recovery disabled / no recovery
    "POWER_STATE_ISSUE",         # D0/D3 power-state transition issue
    "SIGNATURE_VERIFY_FAILURE",  # secure boot / signature verification failed
    "TRANSPORT_ERROR",           # USB / PCIe transport-level error
    "DRIVER_INIT_FAILURE",       # driver / adapter init failure (shared w/ WiFi)
    "COEX_INTERFERENCE",         # BT/Wi-Fi coexistence / RF interference
    "PAIRING_CONNECTION",        # pairing / connection / HCI command failure
    "AUDIO_QUALITY",             # A2DP / audio streaming quality
    "OTHER",
}

# Validation accepts EITHER domain's tags. Each frontend only ever offers
# its own set, and records are already partitioned into wifi/bt streams, so
# a single union keeps one validation path without cross-contaminating the
# offered options.
ALL_CONCLUSION_TAGS = CORRECT_CONCLUSION_TAGS | BT_CONCLUSION_TAGS


def record_detail(
    *,
    session_id: str,
    conversation_id: str,
    turn_id: str,
    vote: Optional[int] = None,
    issues: Optional[list] = None,
    expected_outcome: str = "",
    correct_root_cause: str = "",
    correct_conclusion_tag: str = "",
    correct_skill: str = "",
    correct_approach: str = "",
    evidence_log_lines: Optional[list] = None,
    agent_workflow: str = "",
    feedback_layer: str = "",
    skill_feedback: Optional[list] = None,
    step_feedback: Optional[list] = None,
    issue_time_problem: str = "",
    correct_issue_time: str = "",
    used_issue_time: str = "",
    log_has_date: bool = True,
    severity: Optional[int] = None,
    yaml_modified: bool = False,
    log_path: str = "",
    attach_log: bool = False,
    general_comment: str = "",          # legacy, kept for back-compat
    domain: str = "",
) -> bool:
    """
    Append a structured detailed feedback record. Used by the "More feedback"
    modal: lets the user point at specific skills / steps and label what
    went wrong, so the data is suitable as training labels.

    yaml_modified:       True if the user edited the local skill YAML during
                         this session (used only as a review-queue priority
                         signal — no skill config is copied).
    log_path:            Local path to the current session log. Only copied
                         to `<feedback_root>/logs/<conversation_id>/` when
                         `attach_log=True` — typically when the user ticks
                         "Attach session log" in the More feedback modal
                         after a thumbs-down.
    attach_log:          Explicit user consent to share the session log.

    All filled-form submissions are weighted "high" so they sort above plain
    👍/👎 events in the review queue.
    """
    if not conversation_id or not turn_id:
        return False

    cleaned_issues = _sanitise_issues(issues)
    general_comment = (general_comment or "").strip()
    expected_outcome = (expected_outcome or "").strip()
    correct_root_cause = (correct_root_cause or "").strip()
    correct_skill = (correct_skill or "").strip()
    correct_approach = (correct_approach or "").strip()

    # Severity 1-5 (or None when the user skipped it).
    severity_val: Optional[int] = None
    if severity is not None and severity != "":
        try:
            s = int(severity)
            if 1 <= s <= 5:
                severity_val = s
        except (TypeError, ValueError):
            severity_val = None

    # Conclusion tag must come from the whitelisted set (or be empty).
    # Accept either domain's tags — the frontend only offers its own set.
    eff_domain = _resolve_domain(conversation_id, domain)
    tag_in = (correct_conclusion_tag or "").strip().upper()
    correct_conclusion_tag = tag_in if tag_in in ALL_CONCLUSION_TAGS else ""

    # Agent-workflow assessment must come from the whitelisted set (or empty).
    # This is the primary ACE signal for the agent-prompt-layer Playbook —
    # each value maps cleanly to a target prompt section.
    wf_in = (agent_workflow or "").strip().lower()
    agent_workflow = wf_in if wf_in in AGENT_WORKFLOW_TAGS else ""

    # Issue-time problem is free-text (capped so a stray paste can't bloat
    # the JSONL stream). Was previously gated against ISSUE_TIME_PROBLEM_TAGS
    # but the whitelist was dropped — feedback is now free-form by design.
    issue_time_problem = (issue_time_problem or "").strip()[:500]
    correct_issue_time = (correct_issue_time or "").strip()
    used_issue_time = (used_issue_time or "").strip()

    # Dispatch hint (Skill Playbook vs Agent-prompt Playbook). The wizard's
    # 3-way router sends an explicit `feedback_layer` (skill | agent | both);
    # when present and valid it is authoritative — the user stated intent
    # directly. Fall back to inferring from which fields were filled only for
    # older pre-wizard clients that send nothing.
    explicit_layer = (feedback_layer or "").strip().lower()
    if explicit_layer in FEEDBACK_LAYERS:
        feedback_layer = explicit_layer
    else:
        feedback_layer = ""
        cat_set = {(it.get("category") or "") for it in cleaned_issues}
        if (agent_workflow
                or correct_skill
                or correct_approach
                or cat_set & {"wrong_skill", "wrong_input", "wrong_order",
                              "missing_step", "stuck"}):
            feedback_layer = "agent"
        if (correct_root_cause or correct_conclusion_tag
                or evidence_log_lines or cat_set & {"bad_output"}):
            feedback_layer = "both" if feedback_layer == "agent" else "skill"

    # Evidence log lines — strip blanks; cap to a sane length so a stray
    # paste of an entire log file doesn't bloat the JSONL stream.
    cleaned_evidence: list[str] = []
    if isinstance(evidence_log_lines, list):
        for line in evidence_log_lines:
            s = str(line).rstrip()
            if s:
                cleaned_evidence.append(s)
        cleaned_evidence = cleaned_evidence[:50]

    # Per-skill feedback from the wizard's skill lane. Each row:
    #   {skill_id, assessment in {helpful,redundant,wrong}, what_wrong,
    #    evidence_lines: [..]}
    # We fan it out into the channels ACE already consumes, so no Reflector
    # change is needed:
    #   * assessment         → turns[].skill_assessments  (Reflector input)
    #   * what_wrong (wrong) → an issues[] row scoped to that skill
    #                          (becomes free_text_issues for the Reflector)
    #   * evidence_lines     → aggregated into evidence_log_lines (grep-verified)
    # The raw rows are also kept verbatim under `skill_feedback` so offline
    # training keeps per-skill attribution of the reason + evidence.
    cleaned_skill_feedback: list[dict] = []
    skill_assessment_rows: list[dict] = []
    if isinstance(skill_feedback, list):
        for sf in skill_feedback:
            if not isinstance(sf, dict):
                continue
            sid = (sf.get("skill_id") or "").strip()
            assess = (sf.get("assessment") or "").strip().lower()
            if not sid or assess not in SKILL_ASSESSMENT_VALUES:
                continue
            what_wrong = (sf.get("what_wrong") or "").strip()
            ev: list[str] = []
            raw_ev = sf.get("evidence_lines")
            if isinstance(raw_ev, list):
                for ln in raw_ev:
                    s = str(ln).rstrip()
                    if s:
                        ev.append(s)
            ev = ev[:50]
            cleaned_skill_feedback.append({
                "skill_id": sid,
                "assessment": assess,
                "what_wrong": what_wrong or None,
                "evidence_lines": ev or None,
            })
            skill_assessment_rows.append({"skill_id": sid, "assessment": assess})
            if ev:
                cleaned_evidence.extend(ev)
            # "wrong" skill + a reason → a skill-scoped issue row so the
            # Reflector sees the per-skill correction in free_text_issues.
            if assess == "wrong" and what_wrong:
                cleaned_issues.append({
                    "scope": "skill", "skill_id": sid, "step_index": None,
                    "step_label": None, "category": "wrong_conclusion",
                    "should_be": None, "comment": what_wrong,
                })
        cleaned_evidence = cleaned_evidence[:80]

    # Per-step feedback from the wizard's Agent-workflow lane. Each row:
    #   {step_index, skill_id, step_label,
    #    assessment in {helpful,redundant,wrong}, what_wrong,
    #    evidence_lines: [..]}
    # Mirrors skill_feedback but pins the verdict to a specific reasoning
    # step. We fan it into the channels the Reflector already reads:
    #   * what_wrong (wrong) → a step-scoped issues[] row (free_text_issues)
    #   * evidence_lines     → aggregated into evidence_log_lines
    # and keep the raw rows verbatim under `step_feedback` for offline
    # training so per-step attribution of the reason + evidence survives.
    cleaned_step_feedback: list[dict] = []
    if isinstance(step_feedback, list):
        step_evidence: list[str] = []
        for stf in step_feedback:
            if not isinstance(stf, dict):
                continue
            assess = (stf.get("assessment") or "").strip().lower()
            if assess not in SKILL_ASSESSMENT_VALUES:
                continue
            raw_idx = stf.get("step_index")
            try:
                step_idx = int(raw_idx) if raw_idx is not None and raw_idx != "" else None
            except (TypeError, ValueError):
                step_idx = None
            if step_idx is None or step_idx < 0:
                continue
            sid = (stf.get("skill_id") or "").strip() or None
            step_label = (stf.get("step_label") or "").strip() or None
            what_wrong = (stf.get("what_wrong") or "").strip()
            ev: list[str] = []
            raw_ev = stf.get("evidence_lines")
            if isinstance(raw_ev, list):
                for ln in raw_ev:
                    s = str(ln).rstrip()
                    if s:
                        ev.append(s)
            ev = ev[:50]
            cleaned_step_feedback.append({
                "step_index": step_idx,
                "skill_id": sid,
                "step_label": step_label,
                "assessment": assess,
                "what_wrong": what_wrong or None,
                "evidence_lines": ev or None,
            })
            if ev:
                step_evidence.extend(ev)
            # "wrong" step + a reason → a step-scoped issue row so the
            # Reflector sees the per-step correction in free_text_issues.
            if assess == "wrong" and what_wrong:
                cleaned_issues.append({
                    "scope": "step", "skill_id": sid, "step_index": step_idx,
                    "step_label": step_label, "category": "wrong_conclusion",
                    "should_be": None, "comment": what_wrong,
                })
        if step_evidence:
            cleaned_evidence.extend(step_evidence)
            cleaned_evidence = cleaned_evidence[:80]

    has_detail = bool(
        cleaned_issues or general_comment or expected_outcome
        or correct_root_cause or correct_conclusion_tag
        or correct_skill or correct_approach or cleaned_evidence
        or agent_workflow or severity_val is not None
        or issue_time_problem or correct_issue_time
        or cleaned_skill_feedback or cleaned_step_feedback
    )

    # If everything is empty and there is no vote either, ignore.
    if not has_detail and vote not in (1, -1):
        return False

    weight = _feedback_weight(has_detail=has_detail, yaml_modified=bool(yaml_modified))

    record = {
        "schema_version": RECORD_SCHEMA_VERSION,
        "ts": _now_iso(),
        "session_id": session_id or "",
        "submitted_by": _current_user(),
        "domain": eff_domain or "wifi",
        "conversation_id": conversation_id,
        "turn_id": turn_id,
        "vote": vote if vote in (1, -1) else None,
        "issues": cleaned_issues,
        # High-ACE-value structured signals.
        "correct_root_cause":     correct_root_cause or None,
        "correct_conclusion_tag": correct_conclusion_tag or None,
        "correct_skill":          correct_skill or None,
        # Free-text "right direction" — abstract approach the agent should
        # have taken, complementing the skill dropdown. Either or both may
        # be filled.
        "correct_approach":       correct_approach or None,
        "evidence_log_lines":     cleaned_evidence or None,
        "expected_outcome":       expected_outcome or None,
        # Auto-inferred dispatch hint (skill vs agent vs both).
        "feedback_layer":         feedback_layer or None,
        # Agent-prompt-layer ACE signal (maps to specific prompt sections).
        "agent_workflow":         agent_workflow or None,
        # Per-skill verdicts from the wizard's skill lane, with the reason +
        # evidence kept attributed to each skill (also fanned out into
        # skill_assessments / issues / evidence_log_lines above).
        "skill_feedback":         cleaned_skill_feedback or None,
        # Per-step verdicts from the wizard's Agent-workflow lane, attributed
        # to a specific reasoning step (also fanned out into issues /
        # evidence_log_lines above).
        "step_feedback":          cleaned_step_feedback or None,
        # Issue-time (analysis anchor) feedback: what was wrong with the time,
        # the corrected time(s) the user expected, and the time actually used.
        "issue_time_problem":     issue_time_problem or None,
        "correct_issue_time":     correct_issue_time or None,
        "used_issue_time":        used_issue_time or None,
        # How to read the issue-time fields above: when False the analysed log
        # had no dates, so used/correct issue times are time-only "HH:MM:SS"
        # (e.g. DDD logs) — lets ACE group/interpret them without ambiguity.
        "log_has_date":           bool(log_has_date),
        # Quantitative severity 1-5 — used to prioritise the ACE review queue.
        "severity":               severity_val,
        # Legacy free-form field; UI no longer surfaces it, but we keep
        # accepting + persisting whatever older clients send so historic
        # data already on disk stays consistent.
        "general_comment":        general_comment or None,
        "weight": weight,
        "yaml_modified": bool(yaml_modified),
        "attached_log":  bool(attach_log and log_path),
    }

    # Attach the session log only when the user explicitly opted in. The
    # checkbox lives in the More feedback modal; defaults to checked on
    # thumbs-down so submitting a bug report sends the log by default,
    # while a thumbs-up never silently uploads the log.
    if attach_log and log_path:
        _enqueue_log_attach(conversation_id, turn_id, log_path, eff_domain)

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
                fb["weight"] = weight
                fb["yaml_modified"] = bool(yaml_modified)
                fb["details"] = {
                    "ts": record["ts"],
                    "issues": cleaned_issues,
                    "correct_root_cause":     record["correct_root_cause"],
                    "correct_conclusion_tag": record["correct_conclusion_tag"],
                    "correct_skill":          record["correct_skill"],
                    "correct_approach":       record["correct_approach"],
                    "evidence_log_lines":     record["evidence_log_lines"],
                    "expected_outcome":       record["expected_outcome"],
                    "feedback_layer":         record["feedback_layer"],
                    "agent_workflow":         record["agent_workflow"],
                    "skill_feedback":         record["skill_feedback"],
                    "step_feedback":          record["step_feedback"],
                    "severity":               record["severity"],
                    "general_comment":        record["general_comment"],
                }
                t["feedback"] = fb
                # Upsert per-skill verdicts into the turn's skill_assessments
                # so the ACE Reflector reads them exactly as it does the
                # inline chips' output (redundant → workflow, wrong → domain).
                if skill_assessment_rows:
                    existing = t.setdefault("skill_assessments", [])
                    for srow in skill_assessment_rows:
                        prev = next((s for s in existing
                                     if s.get("skill_id") == srow["skill_id"]), None)
                        if prev is not None:
                            prev["assessment"] = srow["assessment"]
                            prev["ts"] = record["ts"]
                        else:
                            existing.append({
                                "skill_id": srow["skill_id"],
                                "assessment": srow["assessment"],
                                "ts": record["ts"],
                            })
                break
            snap["_persisted"] = True

    # 2) Enqueue both writes — return to client immediately.
    _enqueue_append(_feedback_detail_path(eff_domain), record)
    _enqueue_flush(conversation_id)
    return True


# --- Public attach helpers (also used directly from blueprints) --------

def _enqueue_log_attach(conversation_id: str, turn_id: str, log_path: str,
                        domain: str = "") -> None:
    """
    Copy `log_path` to the shared per-conversation logs folder. The filename
    embeds `<turn_id>__<submitted_by>__<original_name>` so the on-disk layout
    is self-describing — a reviewer can see which user attached which log
    without having to open the JSONL record.
    """
    if not conversation_id or not log_path:
        return
    try:
        src = Path(log_path)
        if not src.exists() or not src.is_file():
            return
        user_tag = _current_user()
        # Sanitise turn_id same as conversation_id — both come from
        # the client. src.name is a basename via Path.name semantics.
        safe_turn = _safe_id(turn_id, fallback="turn")
        dst = (
            _attached_logs_dir(conversation_id, domain)
            / f"{safe_turn}__{user_tag}__{src.name}"
        )
        _enqueue_copy(src, dst)
    except Exception as e:
        print(f"[feedback] attach_log skipped ({log_path}): {e}")


def attach_log(conversation_id: str, turn_id: str, log_path: str,
               domain: str = "") -> bool:
    """
    Public helper: copy a session log to the shared logs folder. Safe to
    call from any thread; the copy happens on the IO worker. Returns False
    only when arguments are obviously bad — never raises.
    """
    if not conversation_id or not log_path:
        return False
    try:
        _enqueue_log_attach(conversation_id, turn_id, log_path,
                            _resolve_domain(conversation_id, domain))
        return True
    except Exception as e:
        print(f"[feedback] attach_log failed: {e}")
        return False


def record_step_vote(
    *,
    session_id: str,
    conversation_id: str,
    turn_id: str,
    step_index: int,
    vote: int,
    domain: str = "",
) -> bool:
    """
    Record a per-step thumbs from the live conversation view. Each click
    lands as one row in `feedback_step_votes.jsonl` AND patches the
    conversation snapshot's `turns[].step_votes[]` so reviewers can see
    which steps the user flagged when reading the conversation back.

    vote: +1 or -1. Anything else is rejected silently (return False).
    """
    if vote not in (1, -1):
        return False
    if not conversation_id or not turn_id:
        return False
    try:
        step_index_int = int(step_index)
    except (TypeError, ValueError):
        return False
    if step_index_int < 0:
        return False

    eff_domain = _resolve_domain(conversation_id, domain)
    event = {
        "schema_version": RECORD_SCHEMA_VERSION,
        "ts": _now_iso(),
        "session_id": session_id or "",
        "submitted_by": _current_user(),
        "domain": eff_domain or "wifi",
        "conversation_id": conversation_id,
        "turn_id": turn_id,
        "step_index": step_index_int,
        "vote": vote,
    }

    # Patch in-memory buffer first so subsequent flushes carry the vote.
    with _pending_lock:
        snap = _pending_buffer.get(conversation_id)
        if snap is not None:
            for t in snap.get("turns", []):
                if t.get("turn_id") != turn_id:
                    continue
                votes = t.setdefault("step_votes", [])
                existing = next(
                    (s for s in votes if s.get("step_index") == step_index_int),
                    None,
                )
                if existing is not None:
                    existing["vote"] = vote
                    existing["ts"] = event["ts"]
                else:
                    votes.append({
                        "step_index": step_index_int,
                        "vote": vote,
                        "ts": event["ts"],
                    })
                break
            snap["_persisted"] = True

    _enqueue_append(_step_votes_path(eff_domain), event)
    _enqueue_flush(conversation_id)
    return True


def record_helpful_skill(
    *,
    session_id: str,
    conversation_id: str,
    turn_id: str,
    skill_id: str,
    domain: str = "",
) -> bool:
    """
    Record the "Glad it helped" picker output after a thumbs-up vote.
    Appended to `feedback_helpful_skills.jsonl` and attached to the
    conversation snapshot's `turns[].helpful_skills[]` so ACE Curator
    can aggregate skill-level `helpful_count` over time.
    """
    if not conversation_id or not turn_id or not skill_id:
        return False
    skill_id = str(skill_id).strip()
    if not skill_id:
        return False

    eff_domain = _resolve_domain(conversation_id, domain)
    event = {
        "schema_version": RECORD_SCHEMA_VERSION,
        "ts": _now_iso(),
        "session_id": session_id or "",
        "submitted_by": _current_user(),
        "domain": eff_domain or "wifi",
        "conversation_id": conversation_id,
        "turn_id": turn_id,
        "skill_id": skill_id,
    }

    with _pending_lock:
        snap = _pending_buffer.get(conversation_id)
        if snap is not None:
            for t in snap.get("turns", []):
                if t.get("turn_id") != turn_id:
                    continue
                hs = t.setdefault("helpful_skills", [])
                if skill_id not in hs:
                    hs.append(skill_id)
                break
            snap["_persisted"] = True

    _enqueue_append(_helpful_skills_path(eff_domain), event)
    _enqueue_flush(conversation_id)
    return True


SKILL_ASSESSMENT_VALUES = ("helpful", "redundant", "wrong")


def record_skill_assessment(
    *,
    session_id: str,
    conversation_id: str,
    turn_id: str,
    skill_id: str,
    assessment: str,
) -> bool:
    """
    Record an in-line per-skill chip click. One row in
    `feedback_skill_assessments.jsonl` AND a patch into the conversation
    snapshot's `turns[].skill_assessments[]`. Re-clicking the same chip
    updates the assessment; passing an empty/None assessment clears it.

    assessment: one of "helpful" | "redundant" | "wrong", or "" to clear.
    """
    if not conversation_id or not turn_id or not skill_id:
        return False
    skill_id = str(skill_id).strip()
    if not skill_id:
        return False

    raw = (assessment or "").strip().lower()
    if raw and raw not in SKILL_ASSESSMENT_VALUES:
        return False

    eff_domain = _resolve_domain(conversation_id, "")
    event = {
        "schema_version": RECORD_SCHEMA_VERSION,
        "ts": _now_iso(),
        "session_id": session_id or "",
        "submitted_by": _current_user(),
        "domain": eff_domain or "wifi",
        "conversation_id": conversation_id,
        "turn_id": turn_id,
        "skill_id": skill_id,
        "assessment": raw,
    }

    with _pending_lock:
        snap = _pending_buffer.get(conversation_id)
        if snap is not None:
            for t in snap.get("turns", []):
                if t.get("turn_id") != turn_id:
                    continue
                items = t.setdefault("skill_assessments", [])
                existing = next(
                    (s for s in items if s.get("skill_id") == skill_id),
                    None,
                )
                if not raw:
                    if existing is not None:
                        items.remove(existing)
                else:
                    if existing is not None:
                        existing["assessment"] = raw
                        existing["ts"] = event["ts"]
                    else:
                        items.append({
                            "skill_id": skill_id,
                            "assessment": raw,
                            "ts": event["ts"],
                        })
                break
            snap["_persisted"] = True

    _enqueue_append(_skill_assessments_path(eff_domain), event)
    _enqueue_flush(conversation_id)
    return True


def get_recent_votes(limit: int = 50, domain: str = "") -> list[dict]:
    """Read the last N feedback events. For debugging / UI inspection only."""
    path = _feedback_log_path(domain)
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
