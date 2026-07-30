"""Standalone Flask + Socket.IO app for driving ACE adapt runs and inspecting
playbooks.

Endpoints
---------
GET  /                          → renders the single-page UI.
GET  /api/conversations         → list every snapshot under feedback/conversations
                                  with case_nbr, ts, feedback-turn count, votes.
GET  /api/playbooks             → stats per playbook on disk.
GET  /api/playbooks/<name>      → rendered text + raw bullets for one playbook
                                  ("workflow" or a skill name).
POST /api/adapt                 → kick off a background adapt over a list of
                                  conversation_ids. One job at a time.
GET  /api/job                   → status of the running/last job.

Socket.IO events emitted to all clients during a job
----------------------------------------------------
adapt_started      {job_id, conversation_ids}
adapt_progress     {phase, event, ...}            # forwarded from pipeline/roles
adapt_turn_diff    {conversation_id, turn_id, diff: {playbook_name: {...}}}
adapt_done         {job_id, results, totals, overall_diff}
adapt_error        {job_id, error}
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
import time
import uuid
from dataclasses import asdict
from pathlib import Path
from typing import Optional

from flask import Flask, jsonify, render_template, request
from flask_socketio import SocketIO

# Reuse the same plumbing the CLI uses so the web UI writes to the SAME
# ace_playbooks folder the running Avatar app reads from.
from configs import path_configs
from configs.global_configs import app_config
from services.llm_service import LLM_helper
from utils import helpers

from functools import partial

from ..pipeline import AceRunner
from ..playbook import Playbook
from ..cli import (_skill_context_provider as _ace_skill_context_provider,
                   _load_active_skills as _load_agent_skills)
from ..history import HistoryWriter
from .. import sync_utils as ace_sync
from ..sync import launch_sync_background

# The old Evaluate-tab machinery (cases/golden/harness/store submodules) was
# retired when the eval package was refactored to the leaner judge.py/review.py
# flow. Keep the imports optional so the web server still boots — the legacy
# `/api/eval*` endpoints just return 503 when these are missing.
try:
    from ..eval.cases import list_cases as _eval_list_cases  # type: ignore
    from ..eval.golden import GoldenSet  # type: ignore
    from ..eval.harness import EvalHarness, EvalConfig  # type: ignore
    from ..eval.store import EvalStore  # type: ignore
    _EVAL_LEGACY_AVAILABLE = True
except Exception as _eval_import_err:  # noqa: BLE001
    print(f"[ace.web] legacy eval modules unavailable, /api/eval* disabled: "
          f"{_eval_import_err}")
    _eval_list_cases = None  # type: ignore
    GoldenSet = None  # type: ignore
    EvalHarness = None  # type: ignore
    EvalConfig = None  # type: ignore
    EvalStore = None  # type: ignore
    _EVAL_LEGACY_AVAILABLE = False

from .scheduler import NightlyScheduler

_NAMESPACES = ("wifi", "bt")


def _norm_namespace(value) -> str:
    v = (value or "wifi").strip().lower()
    return v if v in _NAMESPACES else "wifi"


def _feedback_prefix(namespace: str) -> str:
    """Filename prefix for this namespace's feedback stream (see
    services/feedback_service.py's domain partitioning — "" for wifi,
    "bt_" for bt)."""
    from services import feedback_service
    return feedback_service._domain_prefix(namespace)


# ---------------------------------------------------------------------------
# Module-level caches (shared across endpoints + adapt jobs)
# ---------------------------------------------------------------------------
# Conversation snapshot metadata — keyed by file path. Re-parse only when
# the file's mtime changes. Without this, every /api/conversations call
# re-reads and re-decodes every snapshot JSON on the (possibly remote) share.
_CONV_META_CACHE: dict[Path, tuple[float, dict]] = {}
_CONV_META_LOCK = threading.Lock()

# Playbook objects — keyed by JSON path. Reuse the same Playbook instance
# across calls and let `reload_if_changed()` handle on-disk updates instead
# of constructing a fresh Playbook (which re-parses the JSON) every time.
_PB_CACHE: dict[Path, Playbook] = {}
_PB_CACHE_LOCK = threading.Lock()


def _get_cached_playbook(scope: str, path: Path) -> Playbook:
    """Return a shared Playbook instance for `path`, reloading if the file
    has changed since we last read it."""
    path = Path(path)
    with _PB_CACHE_LOCK:
        pb = _PB_CACHE.get(path)
        if pb is None:
            pb = Playbook(scope, path)
            _PB_CACHE[path] = pb
            return pb
    # reload_if_changed has its own lock and a fast-path no-op when unchanged
    pb.reload_if_changed()
    return pb


# ---------------------------------------------------------------------------
# Path helpers (mirror services.ace.cli)
# ---------------------------------------------------------------------------

def _ensure_avatarfiles_dir() -> None:
    if getattr(app_config, "avatarfiles_dir", None):
        return
    try:
        avatarfiles_dir, _d, _p = helpers.init_download_dir()
        app_config.set_avatarfiles_dir(avatarfiles_dir)
    except Exception as e:
        print(f"[ace.web] could not initialise avatarfiles_dir: {e}")


# Cached (path, source) so we only pay the SMB probe once per process.
_FB_ROOT_CACHE: Optional[tuple[Path, str]] = None
_FB_ROOT_LOCK = threading.Lock()
# Optional override set by --feedback-dir on the CLI.
_FB_ROOT_OVERRIDE: Optional[Path] = None
# Hard wall-clock budget for the remote probe before we fall back to local.
_REMOTE_PROBE_BUDGET_SEC = 15.0


def _probe_share(path: str, timeout_sec: float) -> bool:
    """Check Path(path).exists() in a worker thread; return True if reachable
    within `timeout_sec`. Anything else (timeout, exception) → False."""
    result = [False]

    def _check():
        try:
            result[0] = Path(path).exists()
        except Exception:
            pass

    t = threading.Thread(target=_check, daemon=True)
    t.start()
    t.join(timeout_sec)
    return (not t.is_alive()) and bool(result[0])


def _local_feedback_root() -> Path:
    base = getattr(app_config, "avatarfiles_dir", None)
    return Path(base) / "feedback" if base else Path.cwd() / "data" / "feedback"


def _resolve_feedback_root_full(force: bool = False) -> tuple[Path, str]:
    """Resolve the feedback root with remote-first / local-fallback semantics.

    Returns ``(path, source)`` where ``source`` is one of
    ``override`` | ``remote-primary`` | ``remote-backup`` | ``local-fallback``.
    The remote probe shares a wall-clock budget of ``_REMOTE_PROBE_BUDGET_SEC``
    across both shares so a slow / off-VPN machine cannot stall the UI.
    Result is cached for the life of the process unless ``force=True``.
    """
    global _FB_ROOT_CACHE
    if not force and _FB_ROOT_CACHE is not None:
        return _FB_ROOT_CACHE

    with _FB_ROOT_LOCK:
        if not force and _FB_ROOT_CACHE is not None:
            return _FB_ROOT_CACHE

        if _FB_ROOT_OVERRIDE is not None:
            try:
                _FB_ROOT_OVERRIDE.mkdir(parents=True, exist_ok=True)
                (_FB_ROOT_OVERRIDE / "conversations").mkdir(parents=True, exist_ok=True)
            except Exception as e:
                print(f"[ace.web] override path unwritable: {e}")
            _FB_ROOT_CACHE = (_FB_ROOT_OVERRIDE, "override")
            print(f"[ace.web] feedback root = {_FB_ROOT_OVERRIDE} (override)")
            return _FB_ROOT_CACHE

        deadline = time.time() + _REMOTE_PROBE_BUDGET_SEC
        for label, share in (
            ("remote-primary", path_configs.FEEDBACK_DIR_prim),
            ("remote-backup", path_configs.FEEDBACK_DIR_bkup),
        ):
            budget = deadline - time.time()
            if budget <= 0:
                print(f"[ace.web] remote probe budget exhausted before {label}")
                break
            print(f"[ace.web] probing {label} ({budget:.1f}s budget): {share}")
            if _probe_share(share, budget):
                p = Path(share)
                try:
                    (p / "conversations").mkdir(parents=True, exist_ok=True)
                except Exception as e:
                    print(f"[ace.web] {label} reachable but unwritable: {e}")
                    continue
                _FB_ROOT_CACHE = (p, label)
                print(f"[ace.web] feedback root = {p} ({label})")
                return _FB_ROOT_CACHE
            else:
                print(f"[ace.web] {label} unreachable within budget")

        local = _local_feedback_root()
        try:
            (local / "conversations").mkdir(parents=True, exist_ok=True)
        except Exception as e:
            print(f"[ace.web] local fallback unwritable: {e}")
        _FB_ROOT_CACHE = (local, "local-fallback")
        print(f"[ace.web] feedback root = {local} (local-fallback)")
        return _FB_ROOT_CACHE


def _resolve_feedback_root() -> Path:
    """Backward-compatible: return only the resolved path."""
    return _resolve_feedback_root_full()[0]


def _resolve_playbooks_dir(namespace: str = "wifi") -> Path:
    """Cheap — just resolves the local working dir for this namespace ("wifi"
    or "bt"). The (network-bound) cloud sync itself runs once at startup for
    both namespaces, see main()."""
    return ace_sync.local_working_dir(namespace)


# Key module is cached at process scope: the file rarely moves, and the SMB
# probe inside get_load_path() (~8s per share, sequential) was the main
# reason "Start reflection" felt slow. Only the first job pays the probe.
_KEY_CACHE: Optional[tuple[str, object]] = None
_KEY_CACHE_LOCK = threading.Lock()


def _resolve_key_module(force: bool = False) -> object:
    global _KEY_CACHE
    if not force and _KEY_CACHE is not None:
        return _KEY_CACHE[1]
    with _KEY_CACHE_LOCK:
        if not force and _KEY_CACHE is not None:
            return _KEY_CACHE[1]
        key_path = helpers.get_load_path(
            path_configs.KEY_PATH_prim,
            path_configs.KEY_PATH_bkup,
        )
        if key_path is None:
            raise RuntimeError("Could not resolve key share — VPN reachable?")
        mod = helpers.load_module(key_path, "key_module")
        _KEY_CACHE = (key_path, mod)
        return mod


def _build_llm(model: Optional[str] = None) -> LLM_helper:
    key = _resolve_key_module()
    llm = LLM_helper()
    llm.set_up(
        gpt_token=key.gnaigpt_token,
        gpt_url=key.gnaigpt_url,
        model=model or key.gnaigpt_model,
        classifitation_path=path_configs.CLASSIFY_PATH,
    )
    return llm


# ---------------------------------------------------------------------------
# Conversation listing
# ---------------------------------------------------------------------------

def _belongs_to_namespace(filename: str, namespace: str) -> bool:
    """True if `filename` is this namespace's conversation snapshot.

    All domains' snapshots live in the same conversations/ folder,
    distinguished only by filename prefix (see
    services/feedback_service.py's domain partitioning, and
    eval/cases.py's identical startswith("bt_") fallback). Without this a BT
    conversation shows up in the WiFi picker (and vice versa) with no visual
    distinction — selecting it silently no-ops downstream since AceRunner
    looks up the prefixed filename, but it's confusing UX.
    """
    my_prefix = _feedback_prefix(namespace)
    if my_prefix:
        return filename.startswith(my_prefix)
    # wifi/default has no prefix of its own — a file belongs to it as long
    # as it doesn't carry some OTHER domain's prefix.
    other_prefixes = [_feedback_prefix(ns) for ns in _NAMESPACES if ns != namespace]
    return not any(p and filename.startswith(p) for p in other_prefixes)


def _list_conversations(conv_dir: Path, source: str, namespace: str = "wifi") -> list[dict]:
    out: list[dict] = []
    if not conv_dir.exists():
        return out
    # Iterate unsorted (one stat per file instead of two on the SMB share);
    # sort at the end by the mtime we already read.
    for f in conv_dir.glob("*.json"):
        if not _belongs_to_namespace(f.name, namespace):
            continue
        try:
            mtime = f.stat().st_mtime
        except Exception:
            mtime = 0.0

        # Hot path: file unchanged since last parse → reuse cached metadata.
        with _CONV_META_LOCK:
            cached = _CONV_META_CACHE.get(f)
        if cached and cached[0] == mtime:
            item = dict(cached[1])
            item["source"] = source
            item["feedback_root"] = str(conv_dir.parent)
            out.append(item)
            continue

        try:
            snap = json.loads(f.read_text(encoding="utf-8"))
        except Exception as e:
            out.append({"conversation_id": f.stem, "error": f"parse failed: {e}",
                        "source": source, "feedback_root": str(conv_dir.parent)})
            continue
        turns = snap.get("turns") or []
        feedback_turns = [t for t in turns if t.get("feedback")]
        votes = [(t.get("feedback") or {}).get("vote", 0) for t in feedback_turns]
        last_vote = votes[-1] if votes else 0
        issue = snap.get("issue") or {}
        # Submitters: conversation-level + every per-turn feedback submitter
        # (deduplicated, ordered by first appearance). A single conversation
        # can be reviewed by multiple users so we surface them all.
        submitters: list[str] = []
        seen: set[str] = set()
        def _add(sub):
            if sub and sub not in seen:
                seen.add(sub)
                submitters.append(sub)
        _add(snap.get("submitted_by"))
        for t in feedback_turns:
            _add((t.get("feedback") or {}).get("submitted_by"))
        meta = {
            "conversation_id": snap.get("conversation_id") or f.stem,
            "file": f.name,
            "modified": mtime,
            "case_nbr": issue.get("case_nbr") or "",
            "issue_type": issue.get("issue_type") or "",
            "subject": issue.get("subject") or "",
            "total_turns": len(turns),
            "feedback_turns": len(feedback_turns),
            "last_vote": last_vote,
            "submitted_by": snap.get("submitted_by") or "",
            "submitters": submitters,
            "ts": snap.get("started_at") or snap.get("ended_at") or snap.get("ts") or "",
        }
        with _CONV_META_LOCK:
            _CONV_META_CACHE[f] = (mtime, meta)

        item = dict(meta)
        item["source"] = source
        item["feedback_root"] = str(conv_dir.parent)
        out.append(item)
    out.sort(key=lambda d: d.get("modified") or 0, reverse=True)
    return out


def _list_all_conversations(force: bool = False, namespace: str = "wifi") -> tuple[list[dict], dict]:
    """Merge conversations from the resolved remote root and the local fallback.

    Returns ``(items, diag)``. Items are deduped by ``conversation_id`` —
    when both roots contain the same id, the remote copy wins (canonical),
    but the diag reports how many were found in each. Only conversations
    belonging to `namespace` are included (see _belongs_to_namespace).
    """
    remote_root, source = _resolve_feedback_root_full(force=force)
    remote_dir = remote_root / "conversations"
    local_root = _local_feedback_root()
    local_dir = local_root / "conversations"

    remote_items: list[dict] = []
    local_items: list[dict] = []
    # Don't double-scan when the resolved root IS the local fallback.
    same_root = remote_root.resolve() == local_root.resolve() if local_root.exists() else False

    if remote_dir.exists() and not same_root:
        remote_items = _list_conversations(remote_dir, source, namespace)
    if local_dir.exists():
        local_items = _list_conversations(local_dir, "local-fallback" if not same_root else source, namespace)

    # Dedup by conversation_id: prefer remote (canonical).
    merged: dict[str, dict] = {}
    for it in remote_items:
        merged[it.get("conversation_id")] = it
    for it in local_items:
        cid = it.get("conversation_id")
        if cid not in merged:
            merged[cid] = it

    items = sorted(merged.values(), key=lambda d: d.get("modified") or 0, reverse=True)
    diag = {
        "feedback_root": str(remote_root),
        "source": source,
        "remote_dir": str(remote_dir),
        "remote_exists": remote_dir.exists(),
        "remote_file_count": sum(1 for _ in remote_dir.glob("*.json")) if remote_dir.exists() else 0,
        "remote_loaded": len(remote_items),
        "local_dir": str(local_dir),
        "local_exists": local_dir.exists(),
        "local_file_count": sum(1 for _ in local_dir.glob("*.json")) if local_dir.exists() else 0,
        "local_loaded": len(local_items),
        "merged_loaded": len(items),
        "remote_primary": path_configs.FEEDBACK_DIR_prim,
        "remote_backup": path_configs.FEEDBACK_DIR_bkup,
        "remote_budget_sec": _REMOTE_PROBE_BUDGET_SEC,
    }
    return items, diag


def _list_playbooks(namespace: str = "wifi") -> list[dict]:
    pbs_dir = _resolve_playbooks_dir(namespace)
    out: list[dict] = []
    for f in sorted(pbs_dir.glob("*.json")):
        scope = "agent" if f.name == "workflow.json" else f.stem.removeprefix("domain_")
        pb = _get_cached_playbook(scope, f)
        st = pb.stats()
        st["file"] = f.name
        st["display_name"] = "workflow" if scope == "agent" else scope
        st["modified"] = f.stat().st_mtime
        out.append(st)
    return out


def _render_playbook(name: str, namespace: str = "wifi") -> dict:
    pbs_dir = _resolve_playbooks_dir(namespace)
    if name == "workflow":
        path = pbs_dir / "workflow.json"
        pb = _get_cached_playbook("agent", path)
    else:
        safe = name.replace("/", "_").replace(" ", "_")
        path = pbs_dir / f"domain_{safe}.json"
        pb = _get_cached_playbook(name, path)
    return {
        "name": name,
        "file": path.name,
        "exists": path.exists(),
        "text": pb.render(),
        "bullets": [asdict(b) for b in pb.bullets],
        "stats": pb.stats(),
    }


# ---------------------------------------------------------------------------
# Diff (before / after snapshot of every playbook on disk)
# ---------------------------------------------------------------------------

def _snapshot_bullets(namespace: str = "wifi") -> dict[str, dict[str, dict]]:
    """Return {playbook_name: {bullet_id: {content, helpful, harmful, ...}}}."""
    snap: dict[str, dict[str, dict]] = {}
    for s in _list_playbooks(namespace):
        name = s["display_name"]
        rendered = _render_playbook(name, namespace)
        snap[name] = {b["id"]: b for b in rendered["bullets"]}
    return snap


def _diff_bullets(before: dict, after: dict) -> dict:
    """Compute per-playbook added / removed / bumped / updated diffs."""
    out: dict[str, dict] = {}
    all_names = set(before) | set(after)
    for name in sorted(all_names):
        b = before.get(name, {})
        a = after.get(name, {})
        added = []
        removed = []
        bumped = []
        updated = []
        for bid, ab in a.items():
            if bid not in b:
                added.append(ab)
            else:
                bb = b[bid]
                if ab.get("content") != bb.get("content"):
                    updated.append({"id": bid, "before": bb.get("content"), "after": ab.get("content")})
                if (ab.get("helpful_count", 0) != bb.get("helpful_count", 0) or
                        ab.get("harmful_count", 0) != bb.get("harmful_count", 0) or
                        ab.get("neutral_count", 0) != bb.get("neutral_count", 0)):
                    bumped.append({
                        "id": bid,
                        "content": ab.get("content"),
                        "before": {k: bb.get(k, 0) for k in ("helpful_count", "harmful_count", "neutral_count")},
                        "after":  {k: ab.get(k, 0) for k in ("helpful_count", "harmful_count", "neutral_count")},
                    })
        for bid, bb in b.items():
            if bid not in a:
                removed.append(bb)
        if added or removed or bumped or updated:
            out[name] = {
                "added": added,
                "removed": removed,
                "bumped": bumped,
                "updated": updated,
            }
    return out


# ---------------------------------------------------------------------------
# Background job manager
# ---------------------------------------------------------------------------

class JobManager:
    def __init__(self, socketio: SocketIO, history: Optional[HistoryWriter] = None,
                 eval_store: Optional[EvalStore] = None):
        self.socketio = socketio
        self.history = history          # wifi's HistoryWriter (eval harness still assumes wifi)
        self.eval_store = eval_store
        self._extra_history: dict[str, HistoryWriter] = {}   # other namespaces, lazy
        self._lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None
        self._state: dict = {"status": "idle"}

    def _history_for(self, namespace: str) -> Optional[HistoryWriter]:
        namespace = _norm_namespace(namespace)
        if namespace == "wifi":
            return self.history
        hw = self._extra_history.get(namespace)
        if hw is None:
            hw = HistoryWriter(root=_resolve_playbooks_dir(namespace) / "history")
            self._extra_history[namespace] = hw
        return hw

    @property
    def state(self) -> dict:
        with self._lock:
            return dict(self._state)

    def is_running(self) -> bool:
        with self._lock:
            return self._state.get("status") == "running"

    def start(self, conversation_ids: list[str], model: Optional[str],
              source: str = "manual-selected",
              validate_after: bool = False,
              eval_overrides: Optional[dict] = None,
              namespace: str = "wifi") -> dict:
        namespace = _norm_namespace(namespace)
        with self._lock:
            if self._state.get("status") == "running":
                return {"ok": False, "error": "another adapt job is already running",
                        "job_id": self._state.get("job_id")}
            job_id = str(uuid.uuid4())
            self._state = {
                "status": "running",
                "job_id": job_id,
                "started_at": time.time(),
                "namespace": namespace,
                "conversation_ids": conversation_ids,
                "source": source,
                "validate_after": bool(validate_after),
            }
        # Resolve which feedback_root each conversation lives in BEFORE we hand
        # off to the worker, so the job can target remote vs local correctly.
        items, _diag = _list_all_conversations(namespace=namespace)
        roots_by_cid: dict[str, str] = {it["conversation_id"]: it["feedback_root"]
                                        for it in items if it.get("conversation_id")}
        t = threading.Thread(target=self._run,
                             args=(job_id, conversation_ids, model, roots_by_cid,
                                   source, validate_after, eval_overrides, namespace),
                             daemon=True)
        self._thread = t
        t.start()
        return {"ok": True, "job_id": job_id, "validate_after": bool(validate_after)}

    def _emit(self, event: str, payload: dict) -> None:
        # `emit` here is called from the worker thread; flask-socketio handles
        # marshalling under async_mode='threading'.
        try:
            self.socketio.emit(event, payload)
        except Exception as e:
            print(f"[ace.web] emit({event}) failed: {e}")

    def _pre_adapt_snapshot(self, job_id: str, namespace: str = "wifi") -> Optional[str]:
        """Snapshot the live playbooks BEFORE an adapt run so the chained
        eval has a 'before' arm (and the gate a rollback target). Returns
        the "YYYY-MM-DD/<run_dir>" ref, or None on failure."""
        history = self._history_for(namespace)
        if history is None:
            return None
        try:
            from datetime import datetime as _dt
            run_dir = history.snapshot_playbooks(
                _resolve_playbooks_dir(namespace), run_id=job_id, source="pre-adapt",
                meta={"reason": "validate-after-adapt baseline"},
            )
            if not run_dir:
                return None
            return f"{_dt.now().strftime('%Y-%m-%d')}/{run_dir}"
        except Exception as e:
            print(f"[ace.web] pre-adapt snapshot failed: {e}")
            return None

    def _run(self, job_id: str, conversation_ids: list[str], model: Optional[str],
             roots_by_cid: dict[str, str], source: str = "manual-selected",
             validate_after: bool = False,
             eval_overrides: Optional[dict] = None,
             namespace: str = "wifi") -> None:
        started_at = time.time()
        before_ref = self._pre_adapt_snapshot(job_id, namespace) if validate_after else None
        if validate_after and before_ref is None:
            self._emit("adapt_progress", {
                "job_id": job_id, "phase": "eval", "event": "warning",
                "message": "pre-adapt snapshot failed — validation after adapt "
                           "will be skipped",
            })
            validate_after = False
        self._emit("adapt_started", {
            "job_id": job_id,
            "namespace": namespace,
            "conversation_ids": conversation_ids,
            "playbooks_dir": str(_resolve_playbooks_dir(namespace)),
            "feedback_root": str(_resolve_feedback_root()),
            "roots_by_cid": roots_by_cid,
            "source": source,
            "validate_after": validate_after,
        })
        results: list[dict] = []
        totals = {"processed": 0, "ok": 0, "skipped": 0, "errors": 0}
        try:
            llm = _build_llm(model)
            playbooks_dir = _resolve_playbooks_dir(namespace)
            default_root = _resolve_feedback_root()
            # One AceRunner per distinct feedback_root so a job mixing remote +
            # local conversations still finds each snapshot on disk.
            runners: dict[str, AceRunner] = {}

            def _runner_for(root_str: str) -> AceRunner:
                if root_str not in runners:
                    runners[root_str] = AceRunner(
                        llm=llm,
                        playbooks_dir=playbooks_dir,
                        feedback_root=Path(root_str),
                        skill_context_provider=partial(_ace_skill_context_provider, namespace=namespace),
                        history=self._history_for(namespace),
                        feedback_prefix=_feedback_prefix(namespace),
                    )
                return runners[root_str]

            def progress_cb(evt: dict) -> None:
                self._emit("adapt_progress", {"job_id": job_id, **evt})

            # One disk-walk snapshot at job start. After each turn we build
            # the new "after" state by overlaying the runner's in-memory
            # playbook bullets onto the previous snapshot — no disk re-reads.
            overall_before = _snapshot_bullets(namespace)
            prev_snap: dict[str, dict[str, dict]] = overall_before

            def _overlay_runner_state(prev: dict, runner: AceRunner) -> dict:
                snap = dict(prev)  # shallow copy: untouched playbooks share inner dicts
                snap["workflow"] = {b.id: asdict(b) for b in runner.workflow_pb.bullets}
                for nm, pb in runner.domain_pbs.items():
                    snap[nm] = {b.id: asdict(b) for b in pb.bullets}
                return snap

            for cid in conversation_ids:
                root_str = roots_by_cid.get(cid) or str(default_root)
                runner = _runner_for(root_str)
                snap_path = runner.feedback_root / "conversations" / f"{runner.feedback_prefix}{cid}.json"
                if not snap_path.exists():
                    self._emit("adapt_progress", {
                        "job_id": job_id, "phase": "pipeline", "event": "no_snapshot",
                        "conversation_id": cid,
                    })
                    totals["skipped"] += 1
                    continue
                try:
                    snap = json.loads(snap_path.read_text(encoding="utf-8"))
                except Exception as e:
                    self._emit("adapt_progress", {
                        "job_id": job_id, "phase": "pipeline", "event": "snapshot_read_error",
                        "conversation_id": cid, "error": str(e),
                    })
                    totals["errors"] += 1
                    continue

                turn_ids = [t.get("turn_id") for t in snap.get("turns", [])
                            if t.get("turn_id") and t.get("feedback")]
                if not turn_ids:
                    self._emit("adapt_progress", {
                        "job_id": job_id, "phase": "pipeline", "event": "no_feedback_turns",
                        "conversation_id": cid,
                    })
                    totals["skipped"] += 1
                    continue

                for tid in turn_ids:
                    before = prev_snap
                    res = runner.run_one(cid, tid, progress=progress_cb,
                                         run_id=job_id, run_source=source)
                    results.append(res)
                    totals["processed"] += 1
                    if res.get("status") == "ok":
                        totals["ok"] += 1
                    else:
                        totals["skipped"] += 1
                    after = _overlay_runner_state(before, runner)
                    diff = _diff_bullets(before, after)
                    self._emit("adapt_turn_diff", {
                        "job_id": job_id,
                        "conversation_id": cid,
                        "turn_id": tid,
                        "diff": diff,
                    })
                    prev_snap = after

            overall_after = prev_snap
            overall_diff = _diff_bullets(overall_before, overall_after)

            self._snapshot_after_run(job_id=job_id, source=source,
                                     started_at=started_at, totals=totals,
                                     namespace=namespace)
            self._sync_to_remote(job_id, namespace)

            self._emit("adapt_done", {
                "job_id": job_id,
                "results": results,
                "totals": totals,
                "overall_diff": overall_diff,
                "validate_after": validate_after,
            })

            if validate_after and before_ref:
                # Chain the eval in the SAME worker thread: state stays
                # "running" so no other job can slip in between adapt and
                # its validation.
                with self._lock:
                    self._state = {
                        "status": "running", "job_id": job_id,
                        "mode": "eval", "chained_from": "adapt",
                        "started_at": time.time(),
                    }
                cfg = {
                    "before": before_ref,
                    "conversation_ids": conversation_ids,
                    "source": "post-adapt-gate",
                    "gate": True,
                    **(eval_overrides or {}),
                }
                self._run_eval_inner(job_id, cfg)
                return

            with self._lock:
                self._state = {
                    "status": "done",
                    "job_id": job_id,
                    "finished_at": time.time(),
                    "totals": totals,
                    "overall_diff": overall_diff,
                }
        except Exception as e:
            import traceback
            err = f"{type(e).__name__}: {e}"
            print(f"[ace.web] job {job_id} failed:\n{traceback.format_exc()}")
            self._emit("adapt_error", {"job_id": job_id, "error": err})
            with self._lock:
                self._state = {
                    "status": "error",
                    "job_id": job_id,
                    "finished_at": time.time(),
                    "error": err,
                }

    # ---------- batch (newly-added-since-cursor) ----------
    def _snapshot_after_run(self, *, job_id: str, source: str,
                            started_at: float, totals: dict,
                            namespace: str = "wifi") -> None:
        """Copy every playbook JSON into history/snapshots/<date>/<ts>__<job>/
        and prune history older than the retention window. Best-effort: never
        raises."""
        history = self._history_for(namespace)
        if history is None:
            return
        try:
            playbooks_dir = _resolve_playbooks_dir(namespace)
            meta = {
                "namespace": namespace,
                "totals": totals,
                "started_at": started_at,
                "finished_at": time.time(),
            }
            history.snapshot_playbooks(playbooks_dir, run_id=job_id,
                                       source=source, meta=meta)
            history.prune()
        except Exception as e:
            print(f"[ace.web] history snapshot failed for {job_id}: {e}")

    def _sync_to_remote(self, job_id: str, namespace: str = "wifi") -> None:
        """Fire-and-forget sync of playbooks + history to this namespace's
        remote SMB share (ace_playbook for wifi, ace_playbook_bt for bt —
        see services/ace/sync_utils.py's namespace table)."""
        def _emit_sync(event: str, payload: dict) -> None:
            self._emit(event, {"job_id": job_id, "namespace": namespace, **payload})
        share = ace_sync.resolve_cloud_playbook_dir(namespace)
        if not share:
            print(f"[ace.sync] push skipped — {namespace} share unreachable")
            return
        try:
            launch_sync_background(
                local_dir=_resolve_playbooks_dir(namespace),
                remote_root_raw=share,
                emit=_emit_sync,
                job_id=job_id,
            )
        except Exception as e:
            print(f"[ace.sync] failed to launch sync: {e}")

    def start_batch(self, model: Optional[str] = None, source: str = "batch",
                    validate_after: bool = False,
                    eval_overrides: Optional[dict] = None,
                    namespace: str = "wifi") -> dict:
        """Kick off AceRunner.run_batch() on the resolved feedback root.
        Uses the same one-job-at-a-time lock as start(). The cursor at
        <playbooks_dir>/.ace_cursor.json determines which feedback events
        count as 'newly added'."""
        namespace = _norm_namespace(namespace)
        with self._lock:
            if self._state.get("status") == "running":
                return {"ok": False, "error": "another adapt job is already running",
                        "job_id": self._state.get("job_id")}
            job_id = str(uuid.uuid4())
            self._state = {
                "status": "running",
                "job_id": job_id,
                "started_at": time.time(),
                "mode": "batch",
                "namespace": namespace,
                "source": source,
                "validate_after": bool(validate_after),
            }
        t = threading.Thread(target=self._run_batch,
                             args=(job_id, model, source, validate_after,
                                   eval_overrides, namespace),
                             daemon=True)
        self._thread = t
        t.start()
        return {"ok": True, "job_id": job_id, "mode": "batch",
                "validate_after": bool(validate_after)}

    def _run_batch(self, job_id: str, model: Optional[str],
                   source: str = "batch", validate_after: bool = False,
                   eval_overrides: Optional[dict] = None,
                   namespace: str = "wifi") -> None:
        started_at = time.time()
        playbooks_dir = _resolve_playbooks_dir(namespace)
        feedback_root = _resolve_feedback_root()
        before_ref = self._pre_adapt_snapshot(job_id, namespace) if validate_after else None
        if validate_after and before_ref is None:
            self._emit("adapt_progress", {
                "job_id": job_id, "phase": "eval", "event": "warning",
                "message": "pre-adapt snapshot failed — validation after adapt "
                           "will be skipped",
            })
            validate_after = False
        self._emit("adapt_started", {
            "job_id": job_id,
            "mode": "batch",
            "namespace": namespace,
            "playbooks_dir": str(playbooks_dir),
            "feedback_root": str(feedback_root),
            "source": source,
            "validate_after": validate_after,
        })
        try:
            llm = _build_llm(model)
            runner = AceRunner(
                llm=llm,
                playbooks_dir=playbooks_dir,
                feedback_root=feedback_root,
                skill_context_provider=partial(_ace_skill_context_provider, namespace=namespace),
                history=self._history_for(namespace),
                feedback_prefix=_feedback_prefix(namespace),
            )

            def progress_cb(evt: dict) -> None:
                self._emit("adapt_progress", {"job_id": job_id, **evt})

            overall_before = _snapshot_bullets(namespace)
            results = runner.run_batch(progress=progress_cb,
                                        run_id=job_id, run_source=source)
            overall_after = _snapshot_bullets(namespace)
            overall_diff = _diff_bullets(overall_before, overall_after)

            totals = {
                "processed": len(results),
                "ok":        sum(1 for r in results if r.get("status") == "ok"),
                "skipped":   sum(1 for r in results if r.get("status") != "ok"),
            }
            self._snapshot_after_run(job_id=job_id, source=source,
                                     started_at=started_at, totals=totals,
                                     namespace=namespace)
            self._sync_to_remote(job_id, namespace)
            self._emit("adapt_done", {
                "job_id": job_id,
                "mode": "batch",
                "results": results,
                "totals": totals,
                "overall_diff": overall_diff,
                "validate_after": validate_after,
            })

            if validate_after and before_ref:
                adapted_cids = sorted({
                    r.get("conversation_id") for r in results
                    if r.get("status") == "ok" and r.get("conversation_id")
                })
                with self._lock:
                    self._state = {
                        "status": "running", "job_id": job_id,
                        "mode": "eval", "chained_from": "batch",
                        "started_at": time.time(),
                    }
                cfg = {
                    "before": before_ref,
                    "conversation_ids": adapted_cids,
                    "source": "post-adapt-gate",
                    "gate": True,
                    **(eval_overrides or {}),
                }
                self._run_eval_inner(job_id, cfg)
                return

            with self._lock:
                self._state = {
                    "status": "done",
                    "job_id": job_id,
                    "mode": "batch",
                    "finished_at": time.time(),
                    "totals": totals,
                    "overall_diff": overall_diff,
                }
        except Exception as e:
            import traceback
            err = f"{type(e).__name__}: {e}"
            print(f"[ace.web] batch job {job_id} failed:\n{traceback.format_exc()}")
            self._emit("adapt_error", {"job_id": job_id, "error": err})
            with self._lock:
                self._state = {
                    "status": "error",
                    "job_id": job_id,
                    "mode": "batch",
                    "finished_at": time.time(),
                    "error": err,
                }

    # ---------- eval ----------
    def start_eval(self, config: dict) -> dict:
        """Kick off a standalone eval job (Evaluate tab). config keys match
        EvalConfig fields (before, conversation_ids, max_cases, gate, ...)."""
        if not _EVAL_LEGACY_AVAILABLE or self.eval_store is None:
            return {"ok": False,
                    "error": "legacy eval harness is unavailable in this build"}
        with self._lock:
            if self._state.get("status") == "running":
                return {"ok": False, "error": "another job is already running",
                        "job_id": self._state.get("job_id")}
            job_id = str(uuid.uuid4())
            self._state = {
                "status": "running",
                "job_id": job_id,
                "started_at": time.time(),
                "mode": "eval",
                "source": config.get("source") or "manual-eval",
            }
        t = threading.Thread(target=self._run_eval_inner,
                             args=(job_id, config), daemon=True)
        self._thread = t
        t.start()
        return {"ok": True, "job_id": job_id, "mode": "eval"}

    def _build_eval_harness(self, job_id: str) -> EvalHarness:
        # wifi-only for now — no BT golden cases exist yet to make a BT eval
        # run meaningful. GoldenSet/EvalHarness both already accept a
        # namespace/domain param (see eval/golden.py's GoldenSet docstring)
        # for whenever that changes; this is the one place to thread it
        # through (playbooks_dir/history/eval_store -> self._history_for(ns)
        # + _resolve_playbooks_dir(ns), golden=GoldenSet(roots[0], ns)).
        roots = [_resolve_feedback_root()]
        local = _local_feedback_root()
        if local.exists() and local.resolve() != Path(roots[0]).resolve():
            roots.append(local)
        return EvalHarness(
            playbooks_dir=_resolve_playbooks_dir(),
            feedback_roots=roots,
            history=self.history,
            store=self.eval_store,
            llm_factory=_build_llm,
            skills_loader=_load_agent_skills,
            golden=GoldenSet(roots[0]),
            emit=lambda ev, payload: self._emit(ev, {"job_id": job_id, **payload}),
            sync_fn=self._sync_to_remote,
        )

    def _run_eval_inner(self, job_id: str, config: dict) -> None:
        """Worker body for eval jobs (standalone or chained after adapt).
        Owns the final state transition."""
        try:
            harness = self._build_eval_harness(job_id)
            eval_config = EvalConfig(
                max_cases=int(config.get("max_cases") or 6),
                max_steps=int(config.get("max_steps") or 6),
                gate=bool(config.get("gate", True)),
                gate_min_cases=int(config.get("gate_min_cases") or 2),
                gate_margin=int(config.get("gate_margin") or 1),
                case_source=config.get("case_source") or config.get("source_mode") or "",
                conversation_ids=list(config.get("conversation_ids") or []),
                before=config.get("before") or "",
                agent_model=config.get("model") or config.get("agent_model") or "",
                judge_model=config.get("judge_model") or "",
                run_id=job_id,
                source=config.get("source") or "manual-eval",
                adapt_job_id=job_id if config.get("source") == "post-adapt-gate" else "",
            )
            report = harness.run(eval_config)
            with self._lock:
                self._state = {
                    "status": "error" if report.get("error") else "done",
                    "job_id": job_id,
                    "mode": "eval",
                    "finished_at": time.time(),
                    "summary": report.get("summary"),
                    "gate": report.get("gate"),
                    "error": report.get("error"),
                }
        except Exception as e:
            import traceback
            err = f"{type(e).__name__}: {e}"
            print(f"[ace.web] eval job {job_id} failed:\n{traceback.format_exc()}")
            self._emit("eval_error", {"job_id": job_id, "error": err})
            with self._lock:
                self._state = {
                    "status": "error",
                    "job_id": job_id,
                    "mode": "eval",
                    "finished_at": time.time(),
                    "error": err,
                }


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------

def create_app() -> tuple[Flask, SocketIO, JobManager, NightlyScheduler]:
    _ensure_avatarfiles_dir()
    templates_dir = Path(__file__).parent / "templates"
    app = Flask(__name__, template_folder=str(templates_dir))
    socketio = SocketIO(app, async_mode="threading")

    history = HistoryWriter(root=_resolve_playbooks_dir() / "history")
    eval_store = (EvalStore(_resolve_playbooks_dir() / "history" / "evals")
                  if _EVAL_LEGACY_AVAILABLE else None)
    # Warm slow caches off the boot path. Each thread is best-effort: if the
    # warm fails (e.g. VPN off at boot), the first request/job still pays the
    # wait like it did before, and correctness is unchanged.
    def _safe(fn, label: str) -> None:
        try:
            fn()
        except Exception as e:
            print(f"[ace.web] warm {label} failed: {e}")

    threading.Thread(target=lambda: _safe(history.prune, "history.prune"),
                     daemon=True, name="ace-history-prune-boot").start()
    threading.Thread(target=lambda: _safe(_resolve_feedback_root_full, "feedback-root"),
                     daemon=True, name="ace-warm-feedback-root").start()
    threading.Thread(target=lambda: _safe(_resolve_key_module, "key-cache"),
                     daemon=True, name="ace-warm-key").start()

    jobs = JobManager(socketio, history=history, eval_store=eval_store)

    def _nightly_run(ns: str, sched: "NightlyScheduler") -> None:
        # Fire-and-forget: JobManager owns the actual work + progress events;
        # NightlyScheduler just records last_run_iso + surfaces the trigger error.
        r = jobs.start_batch(model=None, source="nightly",
                             validate_after=sched.validate, namespace=ns)
        if not r.get("ok"):
            raise RuntimeError(r.get("error") or "start_batch refused")

    # One NightlyScheduler per namespace — each fires jobs.start_batch() for
    # ITS OWN namespace so a BT nightly run never touches WiFi's playbooks
    # (and vice versa). State (enabled / fire time / last result) persists
    # per-namespace under that namespace's own local playbooks dir.
    schedulers: dict[str, NightlyScheduler] = {}
    for ns in _NAMESPACES:
        sched = NightlyScheduler(
            state_path=_resolve_playbooks_dir(ns) / ".ace_nightly.json",
            run_fn=lambda ns=ns: _nightly_run(ns, schedulers[ns]),
        )
        schedulers[ns] = sched
        sched.resume_if_enabled()
    nightly = schedulers["wifi"]  # backward-compat alias for anything below expecting the old name

    @app.route("/")
    def index():
        namespace = _norm_namespace(request.args.get("namespace"))
        return render_template(
            "ace_adapt.html",
            namespace=namespace,
            playbooks_dir=str(_resolve_playbooks_dir(namespace)),
            feedback_root=str(_resolve_feedback_root()),
        )

    @app.route("/api/conversations")
    def api_conversations():
        force = request.args.get("force", "").lower() in ("1", "true", "yes")
        namespace = _norm_namespace(request.args.get("namespace"))
        items, diag = _list_all_conversations(force=force, namespace=namespace)
        if not items:
            if not diag["remote_exists"] and not diag["local_exists"]:
                diag["error"] = (
                    f"Neither remote ({diag['remote_dir']}) nor local "
                    f"({diag['local_dir']}) conversations folder exists."
                )
            else:
                diag["error"] = (
                    f"No usable conversation snapshots found. "
                    f"remote: {diag['remote_file_count']} file(s)/{diag['remote_loaded']} loaded; "
                    f"local: {diag['local_file_count']} file(s)/{diag['local_loaded']} loaded."
                )
        return jsonify({"items": items, "namespace": namespace, **diag})

    @app.route("/api/playbooks")
    def api_playbooks():
        namespace = _norm_namespace(request.args.get("namespace"))
        return jsonify({"items": _list_playbooks(namespace),
                        "namespace": namespace,
                        "playbooks_dir": str(_resolve_playbooks_dir(namespace))})

    @app.route("/api/playbooks/<name>")
    def api_playbook_one(name: str):
        namespace = _norm_namespace(request.args.get("namespace"))
        return jsonify(_render_playbook(name, namespace))

    @app.route("/api/adapt", methods=["POST"])
    def api_adapt():
        body = request.get_json(silent=True) or {}
        cids = body.get("conversation_ids") or []
        if not cids:
            return jsonify({"ok": False, "error": "no conversation_ids provided"}), 400
        model = body.get("model")
        namespace = _norm_namespace(body.get("namespace"))
        return jsonify(jobs.start(
            cids, model, source="manual-selected",
            validate_after=bool(body.get("validate")),
            eval_overrides=body.get("eval") or None,
            namespace=namespace,
        ))

    @app.route("/api/adapt/new", methods=["POST"])
    def api_adapt_new():
        body = request.get_json(silent=True) or {}
        model = body.get("model")
        namespace = _norm_namespace(body.get("namespace"))
        return jsonify(jobs.start_batch(
            model,
            validate_after=bool(body.get("validate")),
            eval_overrides=body.get("eval") or None,
            namespace=namespace,
        ))

    @app.route("/api/job")
    def api_job():
        return jsonify(jobs.state)

    @app.route("/api/nightly", methods=["GET"])
    def api_nightly_status():
        namespace = _norm_namespace(request.args.get("namespace"))
        return jsonify({"namespace": namespace, **schedulers[namespace].status()})

    @app.route("/api/nightly/start", methods=["POST"])
    def api_nightly_start():
        body = request.get_json(silent=True) or {}
        namespace = _norm_namespace(body.get("namespace"))
        return jsonify({"namespace": namespace,
                        **schedulers[namespace].start(validate=body.get("validate"))})

    @app.route("/api/nightly/stop", methods=["POST"])
    def api_nightly_stop():
        body = request.get_json(silent=True) or {}
        namespace = _norm_namespace(body.get("namespace"))
        return jsonify({"namespace": namespace, **schedulers[namespace].stop()})

    # ---------- eval ----------
    def _eval_roots() -> list[Path]:
        roots = [_resolve_feedback_root()]
        local = _local_feedback_root()
        if local.exists() and local.resolve() != Path(roots[0]).resolve():
            roots.append(local)
        return roots

    def _eval_unavailable_response():
        return jsonify({
            "ok": False,
            "error": "legacy eval harness is unavailable in this build",
            "items": [],
        }), 503

    @app.route("/api/eval/cases")
    def api_eval_cases():
        if not _EVAL_LEGACY_AVAILABLE:
            return _eval_unavailable_response()
        force = request.args.get("force", "").lower() in ("1", "true", "yes")
        roots = _eval_roots()
        golden = GoldenSet(roots[0])
        cases = _eval_list_cases(roots, golden=golden, force=force)
        rows = [c.summary() for c in cases]
        return jsonify({
            "items": rows,
            "total": len(rows),
            "replayable": sum(1 for r in rows if r["replayable"]),
            "golden": sum(1 for r in rows if r["golden"]),
            "feedback_roots": [str(r) for r in roots],
        })

    @app.route("/api/eval", methods=["POST"])
    def api_eval_start():
        if not _EVAL_LEGACY_AVAILABLE:
            return _eval_unavailable_response()
        body = request.get_json(silent=True) or {}
        # Hard server-side cost cap regardless of what the client sends.
        try:
            body["max_cases"] = min(int(body.get("max_cases") or 6), 15)
        except (TypeError, ValueError):
            body["max_cases"] = 6
        body.setdefault("source", "manual-eval")
        return jsonify(jobs.start_eval(body))

    @app.route("/api/eval/reports")
    def api_eval_reports():
        if not _EVAL_LEGACY_AVAILABLE or eval_store is None:
            return _eval_unavailable_response()
        return jsonify({"items": eval_store.list_reports()})

    @app.route("/api/eval/reports/<date>/<name>")
    def api_eval_report_one(date: str, name: str):
        if not _EVAL_LEGACY_AVAILABLE or eval_store is None:
            return _eval_unavailable_response()
        rep = eval_store.read_report(date, name)
        if rep is None:
            return jsonify({"error": "not found"}), 404
        return jsonify(rep)

    @app.route("/api/eval/golden", methods=["POST", "DELETE"])
    def api_eval_golden():
        if not _EVAL_LEGACY_AVAILABLE:
            return _eval_unavailable_response()
        body = request.get_json(silent=True) or {}
        cid = (body.get("conversation_id") or "").strip()
        tid = (body.get("turn_id") or "").strip() or None
        if not cid:
            return jsonify({"ok": False, "error": "conversation_id required"}), 400
        golden = GoldenSet(_eval_roots()[0])
        if request.method == "POST":
            res = golden.add(cid, tid, note=(body.get("note") or "").strip())
            return jsonify({"ok": not res.get("error"), **res})
        removed = golden.remove(cid, tid)
        return jsonify({"ok": removed, "removed": removed})

    # ---------- judge & review (minimal UI-triggered endpoints) ----------
    # These wrap `services.ace.eval.runner.evaluate` and
    # `services.ace.eval.review.review` so the Judge & Review tab in the UI
    # can kick them off with default settings. Both calls block the request
    # thread for as long as the underlying job takes.
    _judge_review_lock = threading.Lock()

    def _latest_eval_report() -> Optional[Path]:
        """
        Newest `eval_*.json` under DEFAULT_RUNS_DIR, searched recursively.

        The runner writes to `runs/<stamp>/eval_<stamp>.json`, so a flat
        `glob("eval_*.json")` would miss everything and the Review button
        would report "no eval_*.json report found" even right after a
        Judge run finished. `rglob` walks into every stamp folder; picking
        by mtime keeps the newest on top regardless of filename format
        (dated stamps, `baseline.json`, etc.).
        """
        from ..eval.runner import DEFAULT_RUNS_DIR
        if not DEFAULT_RUNS_DIR.exists():
            return None
        reports = sorted(
            (p for p in DEFAULT_RUNS_DIR.rglob("eval_*.json") if p.is_file()),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        return reports[0] if reports else None

    @app.route("/api/judge", methods=["POST"])
    def api_judge():
        if not _judge_review_lock.acquire(blocking=False):
            return jsonify({"ok": False,
                            "error": "another judge/review job is already running"}), 409
        try:
            from ..eval.runner import _default_cases_dir, DEFAULT_RUNS_DIR, evaluate
            report = evaluate(
                cases_dir=_default_cases_dir(),
                runs_dir=DEFAULT_RUNS_DIR,
            )
            latest = _latest_eval_report()
            return jsonify({
                "ok": True,
                "report_file": str(latest) if latest else None,
                "aggregate": (report or {}).get("aggregate"),
                "cases": [
                    {"case_id": c.get("case_id"),
                     "judge": (c.get("judge") or {}).get("mean_scores"),
                     "overall": (c.get("judge") or {}).get("mean_overall")}
                    for c in (report or {}).get("cases", [])
                ],
            })
        except Exception as e:
            import traceback as _tb
            print(f"[ace.web] /api/judge failed:\n{_tb.format_exc()}")
            return jsonify({"ok": False, "error": f"{type(e).__name__}: {e}"}), 500
        finally:
            _judge_review_lock.release()

    @app.route("/api/review", methods=["POST"])
    def api_review():
        if not _judge_review_lock.acquire(blocking=False):
            return jsonify({"ok": False,
                            "error": "another judge/review job is already running"}), 409
        try:
            from ..eval.review import review as _review_run
            latest = _latest_eval_report()
            if latest is None:
                return jsonify({"ok": False,
                                "error": "no eval_*.json report found — run Judge first"}), 400
            report = _review_run(latest)
            return jsonify({
                "ok": True,
                "current_eval": latest.name,
                "gate_verdict": (report or {}).get("gate_verdict"),
                "report": report,
            })
        except Exception as e:
            import traceback as _tb
            print(f"[ace.web] /api/review failed:\n{_tb.format_exc()}")
            return jsonify({"ok": False, "error": f"{type(e).__name__}: {e}"}), 500
        finally:
            _judge_review_lock.release()

    # ---------- corrupted-bullet triage (post-review revert/remove) ----------
    # Bridges the interactive `services.ace.eval.corrupted_bullet` CLI into
    # the Judge & Review tab. The UI renders one row per harmful bullet with
    # two action buttons; those buttons hit the endpoints below to look up
    # the previous version and mutate the LIVE playbook dir (never the
    # shared network share).
    from ..eval.corrupted_bullet import (
        SNAPSHOTS_DIR as _CORRUPTED_SNAPSHOTS_DIR,
        _newest_snapshot_dir as _corrupted_newest_snapshot,
        _find_bullet_in_live as _corrupted_find_in_live,
        _lookup_previous_bullet as _corrupted_lookup_previous,
        _revert_bullet_in_place as _corrupted_revert,
        _remove_bullet_in_place as _corrupted_remove,
    )

    @app.route("/api/corrupted/lookup", methods=["POST"])
    def api_corrupted_lookup():
        body = request.get_json(silent=True) or {}
        bullet_id = (body.get("bullet_id") or "").strip()
        if not bullet_id:
            return jsonify({"ok": False, "error": "bullet_id required"}), 400
        ns = _norm_namespace(body.get("namespace"))
        live_dir = _resolve_playbooks_dir(ns)

        hit = _corrupted_find_in_live(bullet_id, live_dir)
        if hit is None:
            # Bullet not present in the live playbook set (already removed,
            # or namespace mismatch). UI shows "not in live" and hides both
            # action buttons for this row.
            return jsonify({
                "ok": True,
                "bullet_id":     bullet_id,
                "in_live":       False,
                "playbook_file": None,
                "current":       None,
                "previous":      None,
                "snapshot_dir":  None,
            })
        pb_path, live_bullet, _bullets, _idx = hit

        snap_dir = _corrupted_newest_snapshot(_CORRUPTED_SNAPSHOTS_DIR)
        prev = (_corrupted_lookup_previous(bullet_id, pb_path.name, snap_dir)
                if snap_dir else None)
        return jsonify({
            "ok":            True,
            "bullet_id":     bullet_id,
            "in_live":       True,
            "playbook_file": pb_path.name,
            "current":       live_bullet,
            "previous":      prev,
            "snapshot_dir":  (str(snap_dir.relative_to(_CORRUPTED_SNAPSHOTS_DIR))
                              if snap_dir else None),
        })

    @app.route("/api/corrupted/action", methods=["POST"])
    def api_corrupted_action():
        body = request.get_json(silent=True) or {}
        bullet_id = (body.get("bullet_id") or "").strip()
        action = (body.get("action") or "").strip().lower()
        if not bullet_id:
            return jsonify({"ok": False, "error": "bullet_id required"}), 400
        if action not in ("revert", "remove"):
            return jsonify({"ok": False,
                            "error": "action must be 'revert' or 'remove'"}), 400
        ns = _norm_namespace(body.get("namespace"))
        live_dir = _resolve_playbooks_dir(ns)

        hit = _corrupted_find_in_live(bullet_id, live_dir)
        if hit is None:
            return jsonify({"ok": False,
                            "error": f"{bullet_id} not found in live playbook "
                                     f"dir {live_dir}"}), 404
        pb_path, _live_bullet, _bullets, _idx = hit

        if action == "revert":
            snap_dir = _corrupted_newest_snapshot(_CORRUPTED_SNAPSHOTS_DIR)
            if snap_dir is None:
                return jsonify({"ok": False,
                                "error": "no snapshot directory found"}), 400
            prev = _corrupted_lookup_previous(bullet_id, pb_path.name, snap_dir)
            if prev is None:
                return jsonify({"ok": False,
                                "error": f"{bullet_id} has no previous version "
                                         f"in newest snapshot for "
                                         f"{pb_path.name}"}), 400
            if not _corrupted_revert(pb_path, bullet_id, prev):
                return jsonify({"ok": False, "error": "revert failed"}), 500
            return jsonify({
                "ok":            True,
                "action":        "revert",
                "bullet_id":     bullet_id,
                "playbook_file": pb_path.name,
            })

        # action == "remove"
        if not _corrupted_remove(pb_path, bullet_id):
            return jsonify({"ok": False, "error": "remove failed"}), 500
        return jsonify({
            "ok":            True,
            "action":        "remove",
            "bullet_id":     bullet_id,
            "playbook_file": pb_path.name,
        })

    # ---------- history ----------
    @app.route("/api/history/snapshots")
    def api_history_snapshots():
        return jsonify({"items": history.list_snapshots()})

    @app.route("/api/history/snapshots/<date>/<run_dir>/<path:filename>")
    def api_history_snapshot_file(date: str, run_dir: str, filename: str):
        text = history.read_snapshot_file(date, run_dir, filename)
        if text is None:
            return jsonify({"error": "not found"}), 404
        download = request.args.get("download", "").lower() in ("1", "true", "yes")
        # Serve JSON verbatim so the browser can pretty-print it, but pass
        # download=1 to force save-as with the original filename.
        from flask import Response
        headers = {"Content-Type": "application/json; charset=utf-8"}
        if download:
            safe = (filename or "").replace("\r", "").replace("\n", "").replace('"', "")
            headers["Content-Disposition"] = f'attachment; filename="{safe}"'
        return Response(text, headers=headers)

    @app.route("/api/history/turns")
    def api_history_turn_dates():
        return jsonify({"dates": history.list_turn_dates()})

    @app.route("/api/history/turns/<date>")
    def api_history_turns_for_date(date: str):
        try:
            offset = int(request.args.get("offset", 0))
        except ValueError:
            offset = 0
        try:
            limit = int(request.args.get("limit", 200))
        except ValueError:
            limit = 200
        return jsonify({"date": date,
                        "items": history.read_turns(date, offset=offset, limit=limit)})

    return app, socketio, jobs, nightly


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="ACE adaptation web UI")
    parser.add_argument("--port", type=int, default=5055)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--feedback-dir", default=None,
                        help="Override feedback root (skips remote SMB probe). "
                             "Useful when off-VPN or pointing at a captured dump.")
    args = parser.parse_args(argv)

    if args.feedback_dir:
        global _FB_ROOT_OVERRIDE
        _FB_ROOT_OVERRIDE = Path(args.feedback_dir).expanduser().resolve()
        print(f"[ace.web] --feedback-dir override: {_FB_ROOT_OVERRIDE}")

    for ns in _NAMESPACES:
        try:
            ace_sync.sync_at_boot(namespace=ns)
        except Exception as e:
            print(f"[ace.web] cloud sync skipped for {ns}: {e}")

    app, socketio, _jobs, _nightly = create_app()
    print(f"[ace.web] serving on http://{args.host}:{args.port}")
    for ns in _NAMESPACES:
        print(f"[ace.web] {ns} playbooks_dir = {_resolve_playbooks_dir(ns)}")
    root, source = _resolve_feedback_root_full()
    print(f"[ace.web] feedback_root = {root} ({source})")
    # allow_unsafe_werkzeug=True keeps the dev server happy on flask-socketio>=5.
    socketio.run(app, host=args.host, port=args.port, allow_unsafe_werkzeug=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
