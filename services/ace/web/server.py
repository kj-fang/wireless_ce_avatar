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

from .. import sync_utils as ace_sync
from ..pipeline import AceRunner
from ..playbook import Playbook
from ..cli import _skill_context_provider as _ace_skill_context_provider


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


def _resolve_playbooks_dir() -> Path:
    """Cheap — just resolves the local working dir. The (network-bound)
    cloud sync itself runs once at startup, see main()."""
    return ace_sync.local_working_dir()


def _build_llm(model: Optional[str] = None) -> LLM_helper:
    key_path = helpers.get_load_path(path_configs.KEY_PATH_prim, path_configs.KEY_PATH_bkup)
    if key_path is None:
        raise RuntimeError("Could not resolve key share — VPN reachable?")
    key = helpers.load_module(key_path, "key_moudle")
    llm = LLM_helper()
    provider = getattr(key, "LLM_PROVIDER", "anthropic")
    llm.set_up(
        gpt_token=getattr(key, f"{provider}_token"),
        gpt_url=getattr(key, f"{provider}_url"),
        model=model or getattr(key, f"{provider}_model"),
        classifitation_path=path_configs.CLASSIFY_PATH,
        provider=provider,
    )
    return llm


# ---------------------------------------------------------------------------
# Conversation listing
# ---------------------------------------------------------------------------

def _list_conversations(conv_dir: Path, source: str) -> list[dict]:
    out: list[dict] = []
    if not conv_dir.exists():
        return out
    for f in sorted(conv_dir.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
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
    return out


def _list_all_conversations(force: bool = False) -> tuple[list[dict], dict]:
    """Merge conversations from the resolved remote root and the local fallback.

    Returns ``(items, diag)``. Items are deduped by ``conversation_id`` —
    when both roots contain the same id, the remote copy wins (canonical),
    but the diag reports how many were found in each.
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
        remote_items = _list_conversations(remote_dir, source)
    if local_dir.exists():
        local_items = _list_conversations(local_dir, "local-fallback" if not same_root else source)

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


def _list_playbooks() -> list[dict]:
    pbs_dir = _resolve_playbooks_dir()
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


def _render_playbook(name: str) -> dict:
    pbs_dir = _resolve_playbooks_dir()
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

def _snapshot_bullets() -> dict[str, dict[str, dict]]:
    """Return {playbook_name: {bullet_id: {content, helpful, harmful, ...}}}."""
    snap: dict[str, dict[str, dict]] = {}
    for s in _list_playbooks():
        name = s["display_name"]
        rendered = _render_playbook(name)
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
    def __init__(self, socketio: SocketIO):
        self.socketio = socketio
        self._lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None
        self._state: dict = {"status": "idle"}

    @property
    def state(self) -> dict:
        with self._lock:
            return dict(self._state)

    def is_running(self) -> bool:
        with self._lock:
            return self._state.get("status") == "running"

    def start(self, conversation_ids: list[str], model: Optional[str]) -> dict:
        with self._lock:
            if self._state.get("status") == "running":
                return {"ok": False, "error": "another adapt job is already running",
                        "job_id": self._state.get("job_id")}
            job_id = str(uuid.uuid4())
            self._state = {
                "status": "running",
                "job_id": job_id,
                "started_at": time.time(),
                "conversation_ids": conversation_ids,
            }
        # Resolve which feedback_root each conversation lives in BEFORE we hand
        # off to the worker, so the job can target remote vs local correctly.
        items, _diag = _list_all_conversations()
        roots_by_cid: dict[str, str] = {it["conversation_id"]: it["feedback_root"]
                                        for it in items if it.get("conversation_id")}
        t = threading.Thread(target=self._run,
                             args=(job_id, conversation_ids, model, roots_by_cid),
                             daemon=True)
        self._thread = t
        t.start()
        return {"ok": True, "job_id": job_id}

    def _emit(self, event: str, payload: dict) -> None:
        # `emit` here is called from the worker thread; flask-socketio handles
        # marshalling under async_mode='threading'.
        try:
            self.socketio.emit(event, payload)
        except Exception as e:
            print(f"[ace.web] emit({event}) failed: {e}")

    def _run(self, job_id: str, conversation_ids: list[str], model: Optional[str],
             roots_by_cid: dict[str, str]) -> None:
        self._emit("adapt_started", {
            "job_id": job_id,
            "conversation_ids": conversation_ids,
            "playbooks_dir": str(_resolve_playbooks_dir()),
            "feedback_root": str(_resolve_feedback_root()),
            "roots_by_cid": roots_by_cid,
        })
        results: list[dict] = []
        totals = {"processed": 0, "ok": 0, "skipped": 0, "errors": 0}
        try:
            llm = _build_llm(model)
            playbooks_dir = _resolve_playbooks_dir()
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
                        skill_context_provider=_ace_skill_context_provider,
                    )
                return runners[root_str]

            def progress_cb(evt: dict) -> None:
                self._emit("adapt_progress", {"job_id": job_id, **evt})

            # One disk-walk snapshot at job start. After each turn we build
            # the new "after" state by overlaying the runner's in-memory
            # playbook bullets onto the previous snapshot — no disk re-reads.
            overall_before = _snapshot_bullets()
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
                snap_path = runner.feedback_root / "conversations" / f"{cid}.json"
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
                    res = runner.run_one(cid, tid, progress=progress_cb)
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

            self._emit("adapt_done", {
                "job_id": job_id,
                "results": results,
                "totals": totals,
                "overall_diff": overall_diff,
            })
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


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------

def create_app() -> tuple[Flask, SocketIO, JobManager]:
    _ensure_avatarfiles_dir()
    templates_dir = Path(__file__).parent / "templates"
    app = Flask(__name__, template_folder=str(templates_dir))
    socketio = SocketIO(app, async_mode="threading")

    @app.route("/")
    def index():
        return render_template(
            "ace_adapt.html",
            playbooks_dir=str(_resolve_playbooks_dir()),
            feedback_root=str(_resolve_feedback_root()),
        )

    @app.route("/api/conversations")
    def api_conversations():
        force = request.args.get("force", "").lower() in ("1", "true", "yes")
        items, diag = _list_all_conversations(force=force)
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
        return jsonify({"items": items, **diag})

    @app.route("/api/playbooks")
    def api_playbooks():
        return jsonify({"items": _list_playbooks(),
                        "playbooks_dir": str(_resolve_playbooks_dir())})

    @app.route("/api/playbooks/<name>")
    def api_playbook_one(name: str):
        return jsonify(_render_playbook(name))

    @app.route("/api/adapt", methods=["POST"])
    def api_adapt():
        body = request.get_json(silent=True) or {}
        cids = body.get("conversation_ids") or []
        if not cids:
            return jsonify({"ok": False, "error": "no conversation_ids provided"}), 400
        model = body.get("model")
        return jsonify(jobs.start(cids, model))

    @app.route("/api/job")
    def api_job():
        return jsonify(jobs.state)

    return app, socketio, jobs


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

    try:
        ace_sync.sync_at_boot()
    except Exception as e:
        print(f"[ace.web] cloud sync skipped: {e}")

    app, socketio, _jobs = create_app()
    print(f"[ace.web] serving on http://{args.host}:{args.port}")
    print(f"[ace.web] playbooks_dir = {_resolve_playbooks_dir()}")
    root, source = _resolve_feedback_root_full()
    print(f"[ace.web] feedback_root = {root} ({source})")
    # allow_unsafe_werkzeug=True keeps the dev server happy on flask-socketio>=5.
    socketio.run(app, host=args.host, port=args.port, allow_unsafe_werkzeug=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
