"""
Conversation History Service (local, per-user, Gemini / Claude style)

Stores one JSON file per conversation under ``<avatarfiles_dir>/<domain folder>/``.

How this differs from ``feedback_service``:
  * feedback_service writes a SHARED, vote-gated, path-scrubbed training-data
    layer (only persists when the user casts 👍/👎, lands on a network share).
  * history_service is the user's OWN browsable chat history: every turn is
    persisted immediately, nothing is scrubbed, and the files never leave the
    local machine. It powers the left-sidebar "History" panel — list past
    conversations, click to re-load + resume, delete.

Design goals (borrowed from feedback_service):
  * Never blocks / breaks the chat path — every public function swallows its
    own errors.
  * Files only, no DB.
  * Per-file locks so concurrent SSE threads don't interleave writes.
  * Client-supplied conversation ids are sanitised before becoming path
    components (path-traversal hard-stop).

Domain partitioning (WiFi log_chatbot vs. BT bt_chatbot)
----------------------------------------------------------
Every public function takes an optional ``domain`` kwarg (``""`` = WiFi /
legacy default, ``"bt"`` = Bluetooth). This mirrors the domain partitioning
already used by ``feedback_service``. Two independent safeguards keep the two
bots' histories apart:

  1. DIFFERENT ROOT FOLDERS — wifi -> ``<avatarfiles_dir>/history/`` (legacy,
     unchanged), bt -> ``<avatarfiles_dir>/bt_history/``. Under normal
     operation the two never even see each other's files.
  2. FILENAME PREFIX — bt conversation files are additionally named
     ``bt-<id>.json`` (wifi keeps its legacy bare ``<id>.json``). This means
     that even in the freak case both roots end up pointing at the same
     folder, telling the two apart is a plain filename ``startswith()``
     check — no need to open + parse a single byte of JSON to know which bot
     owns a file. ``list_conversations`` uses this for its glob pattern AND
     defensively filters out foreign-prefixed files from the legacy (wifi)
     domain's listing.

The reasoning trace lives beside the conversation, not inside it
----------------------------------------------------------------
A tools-mode turn emits dozens of step events (thinking, skill invocations,
tool results). Those are worth keeping — reopening a conversation should show
HOW the agent got there, not just what it concluded — but they are one to two
orders of magnitude bigger than the turn itself, and ``list_conversations``
parses every conversation file in full on every sidebar refresh. Storing them
inline would make listing pay for data the sidebar never shows.

So each conversation's bulky parts go to sidecars:

    <root>/<id>.json            the conversation  (shape UNCHANGED)
    <root>/steps/<id>.json      its reasoning trace, keyed by turn_id
    <root>/context/<id>.json    the model-facing conversation, for resuming

Both are subfolders, and the listing globs ``*.json`` without recursing, so
they are invisible to it — listings stay exactly as fast as they are today,
and an older Avatar build never sees the folders at all. Conversation files
keep their existing keys, so an older build reads them unchanged; a
conversation with no sidecar (every file written before this) simply has no
trace and no stored context, and the UI renders and resumes it exactly as it
always did.

The two sidecars are for different readers
------------------------------------------
``steps/`` is for a PERSON: the markdown the UI streamed while the turn ran,
replayed when someone reopens the conversation.

``context/`` is for the MODEL: the messages that actually go back to the API
on the next request — the primed case context, the questions, the assistant
turns and the tool results they were grounded in. Without it, resuming a
conversation could only replay result *text* at the model, so a follow-up
question was answered by something that had read the conclusions but not the
evidence. Neither file substitutes for the other.
"""

from __future__ import annotations

import json
import os
import re
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from configs.global_configs import app_config


# Bump when the on-disk shape changes so a future reader knows the rules.
#   v1 - conversation snapshot with turns[] (turn_id, ts, user_message,
#        result, mode).
#   v2 - the same conversation shape, plus an optional reasoning-trace sidecar
#        at <root>/steps/<id>.json. The conversation file itself did not
#        change, so a v1 reader handles a v2 file unchanged; the version says
#        "a sidecar may exist for this conversation".
#   v3 - adds a second optional sidecar, <root>/context/<id>.json, holding the
#        model-facing conversation so a resumed chat keeps its tool-grounded
#        evidence. Conversation shape still unchanged.
HISTORY_SCHEMA_VERSION = 3

# Hard cap on how many conversations the list endpoint returns / scans.
_LIST_LIMIT = 300

# Trim very large stored results so a single huge report can't bloat a file
# without bound. 200k chars is far above any real report.
_MAX_RESULT_CHARS = 200_000

# --- Sidecars -------------------------------------------------------------
# Subfolders (NOT files in the root) so the conversation listing's
# non-recursive "*.json" glob never sees them — see the module docstring.
_STEPS_SUBDIR = "steps"
_CONTEXT_SUBDIR = "context"

# Per-turn bounds on the stored trace. The live buffer allows 1000 steps and
# the agent already truncates tool output to a ~400 char preview before it
# emits, so these only ever bite on a pathological run. They exist so one
# runaway turn cannot turn a 9 KB conversation into an unopenable file.
_MAX_STEPS_PER_TURN = 300
_MAX_STEP_CHARS = 8_000


# --- Domain partitioning --------------------------------------------------
# Canonical domain key -> (root folder name, filename prefix). "" (wifi) is
# the legacy default: unchanged folder name and no filename prefix, so every
# conversation file already on a user's machine keeps working untouched.
_DOMAIN_FOLDERS = {"": "history", "bt": "bt_history"}
_DOMAIN_PREFIXES = {"": "", "bt": "bt-"}


def _norm_domain(domain: Any) -> str:
    """Normalise a raw domain hint to a canonical key. '' = wifi/default."""
    d = domain.strip().lower() if isinstance(domain, str) else ""
    if d in ("bt", "bluetooth"):
        return "bt"
    return ""


def _domain_folder(domain: Any) -> str:
    return _DOMAIN_FOLDERS.get(_norm_domain(domain), "history")


def _domain_prefix(domain: Any) -> str:
    return _DOMAIN_PREFIXES.get(_norm_domain(domain), "")


# --- Storage location ----------------------------------------------------
# One cached root Path per domain (wifi and bt resolve to different folders).
_root_cache: dict[str, Path] = {}
_root_lock = threading.Lock()


def _resolve_root(domain: str = "") -> Path:
    """
    Resolve the local history root for one domain and cache it.

    Primary:  <avatarfiles_dir>/<domain folder>   (same parent the rest of the
              app uses for case downloads — users find their history next to
              their logs). wifi -> "history" (legacy, unchanged), bt ->
              "bt_history" (its own folder, never mixed with wifi's).
    Fallback: <cwd>/data/<domain folder>          (avatarfiles_dir not set yet)
    """
    key = _norm_domain(domain)
    cached = _root_cache.get(key)
    if cached is not None:
        return cached
    with _root_lock:
        cached = _root_cache.get(key)
        if cached is not None:
            return cached
        base = getattr(app_config, "avatarfiles_dir", None)
        folder = _domain_folder(key)
        root = Path(base) / folder if base else Path.cwd() / "data" / folder
        try:
            root.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            print(f"[history] could not create root {root}: {e}")
        _root_cache[key] = root
        return root


def _history_root(domain: str = "") -> Path:
    return _resolve_root(domain)


# --- Path-traversal hard-stop -------------------------------------------
# conversation_id is server-generated as a uuid4, but the /history/* routes
# accept whatever the client sends — sanitise before it becomes a filename.
_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,80}$")


def _safe_id(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    cleaned = value.strip()
    return cleaned if _SAFE_ID_RE.match(cleaned) else ""


def _conversation_path(conversation_id: str, domain: str = "") -> Optional[Path]:
    safe = _safe_id(conversation_id)
    if not safe:
        return None
    return _history_root(domain) / f"{_domain_prefix(domain)}{safe}.json"


def _sidecar_path(subdir: str, conversation_id: str, domain: str = "") -> Optional[Path]:
    """Path of one sidecar for a conversation, or None for a bad id.

    Sidecars share the conversation's filename (bt- prefix included), one
    folder down, so the pairing is obvious to anyone browsing the folder.
    """
    safe = _safe_id(conversation_id)
    if not safe:
        return None
    return (_history_root(domain) / subdir
            / f"{_domain_prefix(domain)}{safe}.json")


def _steps_path(conversation_id: str, domain: str = "") -> Optional[Path]:
    """Sidecar holding this conversation's reasoning trace, or None."""
    return _sidecar_path(_STEPS_SUBDIR, conversation_id, domain)


def _context_path(conversation_id: str, domain: str = "") -> Optional[Path]:
    """Sidecar holding this conversation's model-facing messages, or None."""
    return _sidecar_path(_CONTEXT_SUBDIR, conversation_id, domain)


# --- Locks ---------------------------------------------------------------
_locks_guard = threading.Lock()
_path_locks: dict[str, threading.Lock] = {}


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


def _derive_title(user_message: str, issue: Optional[dict]) -> str:
    """First user message wins; fall back to issue subject/description."""
    msg = (user_message or "").strip()
    if msg:
        first_line = msg.splitlines()[0].strip()
        return (first_line[:60] + "…") if len(first_line) > 60 else first_line
    if isinstance(issue, dict):
        for key in ("subject", "description"):
            v = (issue.get(key) or "").strip()
            if v:
                return (v[:60] + "…") if len(v) > 60 else v
    return "New conversation"


def assistant_text_from_result(result: Any) -> str:
    """
    Compact plain-text rendering of an agent.chat() result. Used to rebuild
    the agent's conversation_history when a saved conversation is resumed,
    so follow-up questions have textual context.
    """
    if not isinstance(result, dict):
        return str(result or "")
    rtype = result.get("type", "")
    data = result.get("data", "")
    if rtype == "text" and isinstance(data, str):
        return data
    if rtype in ("report", "partial_report") and isinstance(data, dict):
        parts: list[str] = []
        if data.get("root_cause_summary"):
            parts.append(f"Root cause: {data['root_cause_summary']}")
        if data.get("confidence_score") is not None:
            parts.append(f"Confidence: {data['confidence_score']}")
        actions = data.get("recommended_actions")
        if isinstance(actions, list) and actions:
            parts.append("Recommended actions: " + "; ".join(str(a) for a in actions))
        if data.get("markdown_summary"):
            parts.append(str(data["markdown_summary"]))
        return "\n".join(parts) or json.dumps(data, ensure_ascii=False)[:2000]
    if rtype == "error":
        return f"[error] {data}"
    return json.dumps(result, ensure_ascii=False)[:2000]


def _trim_result(result: Any) -> Any:
    """Keep stored result JSON-safe and bounded in size."""
    try:
        encoded = json.dumps(result, ensure_ascii=False)
    except Exception:
        # Non-serialisable payload — fall back to its string form.
        return {"type": "text", "data": str(result)}
    if len(encoded) <= _MAX_RESULT_CHARS:
        return result
    # Too big — keep a readable text stand-in rather than the full blob.
    return {"type": "text", "data": assistant_text_from_result(result)[:_MAX_RESULT_CHARS]}


def _serialise_steps(steps: Any) -> list[dict]:
    """Coerce raw step events into bounded, JSON-safe rows.

    The agent emits ``{"role": ..., "content": ...}``; the chat route may add
    ``ts_ms``, the millisecond offset from the start of the turn, so a replay
    can show the timing the run actually had instead of the timing of the
    replay. Everything else is dropped — the trace is for reading, not for
    feeding back into the model.
    """
    out: list[dict] = []
    for step in list(steps or [])[:_MAX_STEPS_PER_TURN]:
        if not isinstance(step, dict):
            continue
        content = step.get("content")
        content = content if isinstance(content, str) else str(content or "")
        if len(content) > _MAX_STEP_CHARS:
            content = content[:_MAX_STEP_CHARS] + "…"
        row = {"role": str(step.get("role") or "agent"), "content": content}
        ts_ms = step.get("ts_ms")
        if isinstance(ts_ms, (int, float)) and ts_ms >= 0:
            row["ts_ms"] = int(ts_ms)
        out.append(row)
    return out


def _read_snapshot(path: Path) -> Optional[dict]:
    try:
        with path.open("r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else None
    except Exception:
        return None


# --- Public API ----------------------------------------------------------

def record_turn(
    *,
    conversation_id: str,
    session_id: str,
    turn_id: str,
    user_message: str,
    agent_result: Any,
    mode: str = "tools",
    issue: Optional[dict] = None,
    log_path: str = "",
    issue_time: str = "",
    domain: str = "",
    steps: Optional[list] = None,
    agent_context: Optional[list] = None,
) -> None:
    """
    Append one turn to the conversation's history file, creating the file on
    the first turn. Persists immediately (local disk, no vote gate).

    ``domain`` selects which bot's history store this turn belongs to
    ("" = WiFi/legacy, "bt" = Bluetooth) — see the module docstring for the
    folder + filename-prefix partitioning this drives. Never raises.

    ``steps`` is the turn's reasoning trace (the step events the UI streamed
    while it ran) and ``agent_context`` is the model-facing conversation after
    the turn. Both go to sidecars, never into the conversation file, and only
    after the turn itself is safely on disk — they are nice-to-haves, the turn
    is not.
    """
    path = _conversation_path(conversation_id, domain)
    if path is None or not turn_id:
        return
    try:
        with _lock_for(path):
            snapshot = None
            if path.exists():
                snapshot = _read_snapshot(path)
            if not isinstance(snapshot, dict):
                snapshot = {
                    "schema_version": HISTORY_SCHEMA_VERSION,
                    "conversation_id": _safe_id(conversation_id),
                    "session_id": session_id or "",
                    "title": _derive_title(user_message, issue),
                    "created_at": _now_iso(),
                    "updated_at": _now_iso(),
                    "log_path": log_path or "",
                    "issue": issue or {},
                    "issue_time": issue_time or "",
                    "domain": _norm_domain(domain) or "wifi",
                    "turns": [],
                }

            # Keep latest context on the snapshot.
            if log_path:
                snapshot["log_path"] = log_path
            if issue:
                snapshot["issue"] = issue
            if issue_time:
                snapshot["issue_time"] = issue_time
            snapshot["updated_at"] = _now_iso()

            # Defend against a corrupt / non-list ``turns`` from an existing
            # file: normalise to a list before appending so a bad snapshot
            # can't make this best-effort writer raise and drop the turn.
            turns = snapshot.get("turns")
            if not isinstance(turns, list):
                turns = []
                snapshot["turns"] = turns
            turns.append({
                "turn_id": turn_id,
                "ts": _now_iso(),
                "user_message": user_message or "",
                "result": _trim_result(agent_result),
                "mode": mode,
            })

            # tmp+replace inline (already holding the lock).
            tmp = path.with_suffix(".json.tmp")
            tmp.write_text(
                json.dumps(snapshot, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            tmp.replace(path)
    except Exception as e:
        print(f"[history] record_turn failed (conv={conversation_id}): {e}")
        return

    if steps:
        _record_steps(
            conversation_id=conversation_id,
            turn_id=turn_id,
            steps=steps,
            domain=domain,
        )
    if agent_context:
        record_context(
            conversation_id=conversation_id,
            messages=agent_context,
            domain=domain,
        )


def _record_steps(*, conversation_id: str, turn_id: str,
                  steps: Any, domain: str = "") -> None:
    """Merge one turn's reasoning trace into the conversation's sidecar.

    Keyed by turn_id, so re-recording a turn replaces its trace instead of
    appending a second copy. Never raises: the conversation file is already
    written by the time this runs, and losing a trace must not look like
    losing the turn.
    """
    path = _steps_path(conversation_id, domain)
    if path is None or not turn_id:
        return
    rows = _serialise_steps(steps)
    if not rows:
        return
    try:
        with _lock_for(path):
            path.parent.mkdir(parents=True, exist_ok=True)
            sidecar = _read_snapshot(path) if path.exists() else None
            if not isinstance(sidecar, dict):
                sidecar = {
                    "schema_version": HISTORY_SCHEMA_VERSION,
                    "conversation_id": _safe_id(conversation_id),
                    "domain": _norm_domain(domain) or "wifi",
                    "created_at": _now_iso(),
                    "turns": {},
                }
            turns = sidecar.get("turns")
            if not isinstance(turns, dict):
                turns = {}
            turns[turn_id] = {
                "ts": _now_iso(),
                "step_count": len(rows),
                # True when the run emitted more steps than we keep, so a
                # reader can say "trace shortened" instead of quietly showing
                # a partial trace as if it were the whole thing.
                "truncated": len(list(steps)) > len(rows),
                "steps": rows,
            }
            sidecar["turns"] = turns
            sidecar["updated_at"] = _now_iso()
            tmp = path.with_suffix(".json.tmp")
            tmp.write_text(
                json.dumps(sidecar, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            tmp.replace(path)
    except Exception as e:
        print(f"[history] step trace failed (conv={conversation_id}, turn={turn_id}): {e}")


def record_context(*, conversation_id: str, messages: Any, domain: str = "") -> None:
    """Snapshot the model-facing conversation so a later resume is grounded.

    Whole-snapshot overwrite, not a merge: this is the conversation as the
    agent will send it next time, and the newest one is by definition the
    complete one. Never raises — a conversation that loses its context still
    resumes, just from result text as it did before this existed.
    """
    path = _context_path(conversation_id, domain)
    if path is None or not isinstance(messages, list) or not messages:
        return
    rows = [m for m in messages if isinstance(m, dict) and m.get("role")]
    if not rows:
        return
    try:
        with _lock_for(path):
            path.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "schema_version": HISTORY_SCHEMA_VERSION,
                "conversation_id": _safe_id(conversation_id),
                "domain": _norm_domain(domain) or "wifi",
                "updated_at": _now_iso(),
                "message_count": len(rows),
                "messages": rows,
            }
            tmp = path.with_suffix(".json.tmp")
            tmp.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2, default=str),
                encoding="utf-8",
            )
            tmp.replace(path)
    except Exception as e:
        print(f"[history] context save failed (conv={conversation_id}): {e}")


def get_context(conversation_id: str, domain: str = "") -> list:
    """Return the stored model-facing messages, or [] when there are none."""
    path = _context_path(conversation_id, domain)
    if path is None or not path.exists():
        return []
    payload = _read_snapshot(path)
    messages = payload.get("messages") if isinstance(payload, dict) else None
    if not isinstance(messages, list):
        return []
    return [m for m in messages if isinstance(m, dict) and m.get("role")]


def _load_steps(conversation_id: str, domain: str = "") -> dict:
    """Return ``{turn_id: [step, …]}`` for one conversation; {} when none."""
    path = _steps_path(conversation_id, domain)
    if path is None or not path.exists():
        return {}
    sidecar = _read_snapshot(path)
    turns = sidecar.get("turns") if isinstance(sidecar, dict) else None
    if not isinstance(turns, dict):
        return {}
    out: dict[str, list] = {}
    for turn_id, entry in turns.items():
        if isinstance(entry, dict) and isinstance(entry.get("steps"), list):
            out[str(turn_id)] = entry["steps"]
    return out


def list_conversations(limit: int = _LIST_LIMIT, domain: str = "") -> list[dict]:
    """
    Return lightweight summaries of stored conversations for one domain,
    newest first. Each item: conversation_id, title, created_at, updated_at,
    turn_count, log_path, case_nbr, issue_type. Conversations with no turns
    are skipped. Never raises.
    """
    key = _norm_domain(domain)
    prefix = _domain_prefix(key)
    root = _history_root(key)
    out: list[dict] = []
    try:
        pattern = f"{prefix}*.json" if prefix else "*.json"
        files = list(root.glob(pattern))
    except Exception as e:
        print(f"[history] list glob failed: {e}")
        return []

    # Defense-in-depth for the legacy (wifi, no prefix) domain: if another
    # domain's root ever collapsed into this same folder, a bare "*.json"
    # glob would also match ITS prefixed files (e.g. "bt-<id>.json"). Exclude
    # any filename carrying a KNOWN foreign prefix — a cheap startswith()
    # check on the name already in hand, no JSON parsing required.
    if not prefix:
        foreign_prefixes = tuple(p for p in _DOMAIN_PREFIXES.values() if p)
        files = [fp for fp in files if not fp.name.startswith(foreign_prefixes)]

    for fp in files:
        snap = _read_snapshot(fp)
        if not snap:
            continue
        turns = snap.get("turns") or []
        if not turns:
            continue
        issue = snap.get("issue") if isinstance(snap.get("issue"), dict) else {}
        out.append({
            "conversation_id": snap.get("conversation_id") or fp.stem,
            "title": snap.get("title") or "Conversation",
            "created_at": snap.get("created_at") or "",
            "updated_at": snap.get("updated_at") or snap.get("created_at") or "",
            "turn_count": len(turns),
            "log_path": snap.get("log_path") or "",
            "pinned": bool(snap.get("pinned")),
            # Case fields so the sidebar can show more than the chat title.
            # Cast to str first: a corrupt / future-schema file could hold a
            # non-string here, and a bare .strip() would raise and break
            # listing every conversation.
            "case_nbr": str(issue.get("case_nbr") or "").strip(),
            "issue_type": str(issue.get("issue_type") or "").strip(),
        })

    # Pinned conversations float to the top; within each group, newest first.
    out.sort(key=lambda c: c.get("updated_at") or "", reverse=True)
    out.sort(key=lambda c: bool(c.get("pinned")), reverse=True)
    return out[: max(0, int(limit))]


def get_conversation(conversation_id: str, domain: str = "",
                     with_steps: bool = False) -> Optional[dict]:
    """Return the full snapshot for one conversation, or None. Never raises.

    ``with_steps`` attaches each turn's stored reasoning trace as ``steps`` on
    the returned dict — in memory only, the file on disk keeps its v1 shape.
    Turns recorded before the sidecar existed simply carry no ``steps`` key,
    which is what the UI keys off to decide whether to draw the trace card.
    """
    path = _conversation_path(conversation_id, domain)
    if path is None or not path.exists():
        return None
    snapshot = _read_snapshot(path)
    if snapshot is None or not with_steps:
        return snapshot
    try:
        by_turn = _load_steps(conversation_id, domain)
        if by_turn:
            for turn in snapshot.get("turns") or []:
                if not isinstance(turn, dict):
                    continue
                steps = by_turn.get(str(turn.get("turn_id") or ""))
                if steps:
                    turn["steps"] = steps
    except Exception as e:
        print(f"[history] step merge failed (conv={conversation_id}): {e}")
    return snapshot


def delete_conversation(conversation_id: str, domain: str = "") -> bool:
    """Delete one conversation, and its reasoning trace if it has one.

    Returns True if the conversation file was removed. The sidecar is deleted
    on a best-effort basis: an orphaned trace is invisible to every reader
    (nothing lists that folder), so failing to remove it must not report the
    conversation as still present.
    """
    path = _conversation_path(conversation_id, domain)
    if path is None:
        return False
    removed = False
    try:
        with _lock_for(path):
            if path.exists():
                path.unlink()
                removed = True
    except Exception as e:
        print(f"[history] delete failed (conv={conversation_id}): {e}")
    for subdir in (_STEPS_SUBDIR, _CONTEXT_SUBDIR):
        sidecar = _sidecar_path(subdir, conversation_id, domain)
        if sidecar is None:
            continue
        try:
            with _lock_for(sidecar):
                if sidecar.exists():
                    sidecar.unlink()
        except Exception as e:
            print(f"[history] {subdir} sidecar delete failed (conv={conversation_id}): {e}")
    return removed


def _update_snapshot(conversation_id: str, mutate, domain: str = "") -> bool:
    """
    Read the conversation snapshot, apply mutate(snapshot) in place, and write
    it back atomically. Returns True on success. Never raises.
    """
    path = _conversation_path(conversation_id, domain)
    if path is None or not path.exists():
        return False
    try:
        with _lock_for(path):
            snapshot = _read_snapshot(path)
            if not isinstance(snapshot, dict):
                return False
            mutate(snapshot)
            tmp = path.with_suffix(".json.tmp")
            tmp.write_text(
                json.dumps(snapshot, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            tmp.replace(path)
            return True
    except Exception as e:
        print(f"[history] update failed (conv={conversation_id}): {e}")
        return False


def rename_conversation(conversation_id: str, title: str, domain: str = "") -> bool:
    """Set a custom title for one conversation. Returns True on success."""
    clean = (title or "").strip()
    if not clean:
        return False
    clean = clean[:200]

    def _apply(snap: dict) -> None:
        snap["title"] = clean

    return _update_snapshot(conversation_id, _apply, domain)


def set_pinned(conversation_id: str, pinned: bool, domain: str = "") -> bool:
    """Pin or unpin one conversation. Returns True on success."""
    def _apply(snap: dict) -> None:
        snap["pinned"] = bool(pinned)

    return _update_snapshot(conversation_id, _apply, domain)
