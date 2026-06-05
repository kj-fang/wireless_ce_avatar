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
from copy import deepcopy
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

from ..pipeline import AceRunner
from ..playbook import Playbook
from services import feedback_service


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


def _resolve_feedback_root() -> Path:
    # Reuse the live app's resolver (same SMB-probe + caching + local fallback)
    # so the UI never disagrees with feedback_service about where snapshots live.
    try:
        return feedback_service._feedback_root()
    except Exception as e:
        print(f"[ace.web] feedback_service._feedback_root() failed: {e}")
        base = getattr(app_config, "avatarfiles_dir", None)
        return Path(base) / "feedback" if base else Path.cwd() / "data" / "feedback"


def _resolve_playbooks_dir() -> Path:
    base = getattr(app_config, "avatarfiles_dir", None)
    root = Path(base) / "ace_playbooks" if base else Path.cwd() / "data" / "ace_playbooks"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _build_llm(model: Optional[str] = None) -> LLM_helper:
    key_path = helpers.get_load_path(path_configs.KEY_PATH_prim, path_configs.KEY_PATH_bkup)
    if key_path is None:
        raise RuntimeError("Could not resolve key share — VPN reachable?")
    key = helpers.load_module(key_path, "key_moudle")
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

def _list_conversations() -> list[dict]:
    root = _resolve_feedback_root() / "conversations"
    out: list[dict] = []
    if not root.exists():
        return out
    for f in sorted(root.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
        try:
            snap = json.loads(f.read_text(encoding="utf-8"))
        except Exception as e:
            out.append({"conversation_id": f.stem, "error": f"parse failed: {e}"})
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
        out.append({
            "conversation_id": snap.get("conversation_id") or f.stem,
            "file": f.name,
            "modified": f.stat().st_mtime,
            "case_nbr": issue.get("case_nbr") or "",
            "issue_type": issue.get("issue_type") or "",
            "subject": issue.get("subject") or "",
            "total_turns": len(turns),
            "feedback_turns": len(feedback_turns),
            "last_vote": last_vote,
            "submitted_by": snap.get("submitted_by") or "",
            "submitters": submitters,
            "ts": snap.get("started_at") or snap.get("ended_at") or snap.get("ts") or "",
        })
    return out


def _list_playbooks() -> list[dict]:
    pbs_dir = _resolve_playbooks_dir()
    out: list[dict] = []
    for f in sorted(pbs_dir.glob("*.json")):
        scope = "agent" if f.name == "workflow.json" else f.stem.removeprefix("domain_")
        pb = Playbook(scope, f)
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
        pb = Playbook("agent", path)
    else:
        safe = name.replace("/", "_").replace(" ", "_")
        path = pbs_dir / f"domain_{safe}.json"
        pb = Playbook(name, path)
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
        t = threading.Thread(target=self._run,
                             args=(job_id, conversation_ids, model),
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

    def _run(self, job_id: str, conversation_ids: list[str], model: Optional[str]) -> None:
        self._emit("adapt_started", {
            "job_id": job_id,
            "conversation_ids": conversation_ids,
            "playbooks_dir": str(_resolve_playbooks_dir()),
            "feedback_root": str(_resolve_feedback_root()),
        })
        results: list[dict] = []
        totals = {"processed": 0, "ok": 0, "skipped": 0, "errors": 0}
        try:
            llm = _build_llm(model)
            runner = AceRunner(
                llm=llm,
                playbooks_dir=_resolve_playbooks_dir(),
                feedback_root=_resolve_feedback_root(),
            )

            def progress_cb(evt: dict) -> None:
                self._emit("adapt_progress", {"job_id": job_id, **evt})

            overall_before = _snapshot_bullets()

            for cid in conversation_ids:
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
                    before = _snapshot_bullets()
                    res = runner.run_one(cid, tid, progress=progress_cb)
                    results.append(res)
                    totals["processed"] += 1
                    if res.get("status") == "ok":
                        totals["ok"] += 1
                    else:
                        totals["skipped"] += 1
                    after = _snapshot_bullets()
                    diff = _diff_bullets(before, after)
                    self._emit("adapt_turn_diff", {
                        "job_id": job_id,
                        "conversation_id": cid,
                        "turn_id": tid,
                        "diff": diff,
                    })

            overall_after = _snapshot_bullets()
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
    app.config["SECRET_KEY"] = "ace-web-" + str(uuid.uuid4())
    socketio = SocketIO(app, async_mode="threading", cors_allowed_origins="*")
    jobs = JobManager(socketio)

    @app.route("/")
    def index():
        return render_template(
            "ace_adapt.html",
            playbooks_dir=str(_resolve_playbooks_dir()),
            feedback_root=str(_resolve_feedback_root()),
        )

    @app.route("/api/conversations")
    def api_conversations():
        root = _resolve_feedback_root()
        conv_dir = root / "conversations"
        diag = {
            "feedback_root": str(root),
            "conversations_dir": str(conv_dir),
            "exists": conv_dir.exists(),
        }
        if not conv_dir.exists():
            diag["error"] = (
                f"Folder does not exist: {conv_dir}. "
                "Check VPN reachability to the feedback share, or ensure "
                "the local fallback under <avatarfiles_dir>/feedback was populated."
            )
            return jsonify({"items": [], **diag})
        items = _list_conversations()
        diag["json_file_count"] = sum(1 for _ in conv_dir.glob("*.json"))
        diag["loaded"] = len(items)
        if not items:
            diag["error"] = (
                f"No usable conversation snapshots under {conv_dir}. "
                f"({diag['json_file_count']} *.json file(s) present but none parseable.)"
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
    args = parser.parse_args(argv)

    app, socketio, _jobs = create_app()
    print(f"[ace.web] serving on http://{args.host}:{args.port}")
    print(f"[ace.web] playbooks_dir = {_resolve_playbooks_dir()}")
    print(f"[ace.web] feedback_root = {_resolve_feedback_root()}")
    # allow_unsafe_werkzeug=True keeps the dev server happy on flask-socketio>=5.
    socketio.run(app, host=args.host, port=args.port, allow_unsafe_werkzeug=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
