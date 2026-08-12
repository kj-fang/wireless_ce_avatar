"""
CLI entry point for the ACE adaptation loop.

Usage:
    python -m services.ace.cli adapt     --since 2026-05-01T00:00:00
    python -m services.ace.cli adapt-one --conversation <cid> [--turn <tid>]
    python -m services.ace.cli show      --skill Connectivity
    python -m services.ace.cli stats

Run ONLY the ACE training step (adapt playbooks from feedback), e.g. our
usual nightly training command:

    python -m services.ace.cli adapt --exclude-user "yuanyuan" --verbose
    # add --push to publish the updated playbook to the cloud share
    # add --namespace bt to train the Bluetooth playbook set instead of wifi

Run the FULL pipeline in one command (adapt -> eval/judge -> review), with
push deferred until the regression review PASSes:

    python -m services.ace.cli run-all --exclude-user "yuanyuan" --push --verbose
    # exit code 0 = review PASS, 2 = regression (nothing published)
    # --no-eval        : only adapt (skip eval + review)
    # --no-review      : adapt + eval, skip the regression review
    # --no-find-killer : skip the post-review whodunit that traces harmful
    #                    bullets back to the offending feedback turn
    # --no-triage      : skip the post-review auto-revert / auto-remove of
    #                    harmful bullets
    # --namespace bt / --limit N / --passes N / --judge-model <m> also apply

For an interactive web UI (pick conversations from a list, watch the
Reflector/Curator stream live, view a diff of bullets added/removed/bumped):

    python -m services.ace.web   # default http://127.0.0.1:5055

The CLI builds an LLM_helper using the same set_up plumbing the main app uses,
then drives the AceRunner over the feedback share folder. Run it on a cron
nightly or wire it into the feedback save hook to adapt online.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
from datetime import datetime, timezone
from functools import partial
from pathlib import Path

from configs import path_configs
from configs.global_configs import app_config
from services.llm_service import LLM_helper
from utils import helpers

from . import sync as ace_mirror_sync
from . import sync_utils as ace_sync
from .history import HistoryWriter
from .pipeline import AceRunner


# --- Run-output capture --------------------------------------------------
# All stdout/stderr for a run is teed to a temp file, then copied into that
# run's `runs/<stamp>/run.log` once a command resolves its stamp folder.
_RUN_LOG_DEST: Path | None = None


def set_run_log_dest(stamp_dir) -> None:
    """Point the run logger at this run's stamp folder (a run.log lands here)."""
    global _RUN_LOG_DEST
    try:
        d = Path(stamp_dir)
        d.mkdir(parents=True, exist_ok=True)
        _RUN_LOG_DEST = d
    except Exception as e:
        print(f"[ace.cli] could not set run log dest {stamp_dir}: {e}")


class _Tee:
    """Write to the real stream AND a capture file, so console + log match."""

    def __init__(self, stream, fh):
        self._stream = stream
        self._fh = fh

    def write(self, data):
        self._stream.write(data)
        try:
            self._fh.write(data)
        except Exception:
            pass
        return len(data)

    def flush(self):
        try:
            self._stream.flush()
        finally:
            try:
                self._fh.flush()
            except Exception:
                pass

    def __getattr__(self, name):
        return getattr(self._stream, name)


def _ensure_avatarfiles_dir() -> None:
    """Initialise `app_config.avatarfiles_dir` the same way `app.py` does so the
    CLI reads/writes the *same* `ace_playbooks` folder the running app uses
    (e.g. `<Downloads>\\IntelAvatar_files\\ace_playbooks`) instead of the cwd
    fallback (`./data/ace_playbooks`)."""
    if getattr(app_config, "avatarfiles_dir", None):
        return
    try:
        avatarfiles_dir, _driver_dir, _prompt_dir = helpers.init_download_dir()
        app_config.set_avatarfiles_dir(avatarfiles_dir)
    except Exception as e:
        print(f"[ace.cli] could not initialise avatarfiles_dir: {e}")


def _resolve_feedback_root() -> Path:
    share = helpers.get_load_path(path_configs.FEEDBACK_DIR_prim, path_configs.FEEDBACK_DIR_bkup)
    if share:
        return Path(share)
    base = getattr(app_config, "avatarfiles_dir", None)
    return Path(base) / "feedback" if base else Path.cwd() / "data" / "feedback"


def _resolve_playbooks_dir(namespace: str = "wifi") -> Path:
    """Cheap — just resolves the local working dir for this namespace ("wifi"
    or "bt"). The (network-bound) cloud sync itself runs once in main(),
    before any subcommand."""
    return ace_sync.local_working_dir(namespace)


def _feedback_prefix(namespace: str) -> str:
    """Filename prefix for this namespace's feedback stream (see
    services/feedback_service.py's domain partitioning — "" for wifi,
    "bt_" for bt). Kept in sync with that module rather than hardcoded here."""
    from services import feedback_service
    return feedback_service._domain_prefix(namespace)


def _push_now(namespace: str) -> None:
    """Synchronous push (CLI process exits right after — a daemon thread
    would just get killed before it finishes)."""
    share = ace_sync.resolve_cloud_playbook_dir(namespace)
    if not share:
        print(f"[ace.cli] --push skipped — {namespace} share unreachable")
        return
    ace_mirror_sync.sync_playbooks_to_remote(
        local_dir=_resolve_playbooks_dir(namespace),
        remote_root_raw=share,
    )


def _read_env_file(path: Path) -> dict:
    """Minimal .env parser (KEY=VALUE lines) — no external dependency."""
    data: dict = {}
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            data[k.strip()] = v.strip().strip('"').strip("'")
    except Exception as e:
        print(f"[ace.cli] .env.api_key read failed ({path}): {e}")
    return data


def _load_env_key() -> tuple:
    """Return (token, url, model) for the LLM from a local .env (preferred) or
    already-set process env vars. Looks for .env in the CWD and the repo root.
    Any field the .env omits stays None so _build_llm falls back to the shared
    keys.py for it."""
    candidates = [Path.cwd() / ".env.api_key", Path(__file__).resolve().parents[1] / ".env.api_key",
                  Path(__file__).resolve().parents[2] / ".env.api_key"]
    data: dict = {}
    for p in candidates:
        if p.is_file():
            data = _read_env_file(p)
            print(f"[ace.cli] using local LLM key from {p}")
            break

    def pick(*names: str):
        for n in names:
            if data.get(n):
                return data[n]
            if os.environ.get(n):
                return os.environ[n]
        return None

    return (pick("GNAIGPT_TOKEN", "GPT_TOKEN"),
            pick("GNAIGPT_URL", "GPT_URL"),
            pick("GNAIGPT_MODEL", "GPT_MODEL"))


def _build_llm(model: str | None) -> LLM_helper:
    """Build the LLM_helper the Reflector / Curator share. Prefers a local
    .env (GNAIGPT_TOKEN / GNAIGPT_URL / GNAIGPT_MODEL) so a dedicated training
    server needs no key share; falls back to the shared keys.py when the .env
    is absent or incomplete."""
    token, url, env_model = _load_env_key()
    if not (token and url):
        # .env missing/incomplete — fall back to the shared keys.py module.
        key_path = helpers.get_load_path(path_configs.KEY_PATH_prim, path_configs.KEY_PATH_bkup)
        if key_path is None:
            raise RuntimeError("No local .env key and could not resolve key share — VPN reachable?")
        key = helpers.load_module(key_path, "key_moudle")
        token = token or key.gnaigpt_token
        url = url or key.gnaigpt_url
        env_model = env_model or key.gnaigpt_model
    llm = LLM_helper()
    llm.set_up(
        gpt_token=token,
        gpt_url=url,
        model=model or env_model,
        classifitation_path=path_configs.CLASSIFY_PATH,
    )
    return llm


_SKILLS_CACHE: dict[str, dict] = {}


def _load_active_skills(namespace: str = "wifi") -> dict:
    """Best-effort load of the active skills YAML for this namespace (same
    one the live agent uses) so Reflector/Curator can see each skill's
    description + expert_rules. Cached per namespace. Returns an empty dict
    on any failure — callers degrade gracefully."""
    if namespace in _SKILLS_CACHE:
        return _SKILLS_CACHE[namespace]
    try:
        if namespace == "bt":
            from utils import bt_skills_yaml_utils as skills_yaml_utils
        else:
            from utils import skills_yaml_utils
        from services.log_chatbot_service import load_skills_from_yaml
        yaml_path, _date, _src = skills_yaml_utils.current_active_yaml()
        if not yaml_path:
            _SKILLS_CACHE[namespace] = {}
            return _SKILLS_CACHE[namespace]
        loaded = load_skills_from_yaml(str(yaml_path)) or {}
        _SKILLS_CACHE[namespace] = loaded
        print(f"[ace.cli] loaded {len(loaded)} {namespace} skill definition(s) from {yaml_path}")
    except Exception as e:
        print(f"[ace.cli] {namespace} skill YAML unavailable ({e}); Reflector/Curator will run without skill context")
        _SKILLS_CACHE[namespace] = {}
    return _SKILLS_CACHE[namespace]


def _skill_context_provider(sid: str, namespace: str = "wifi"):
    skills = _load_active_skills(namespace)
    sk = skills.get(sid)
    if sk is None:
        return None
    try:
        return {
            "description": getattr(sk, "description", "") or "",
            "expert_rules": getattr(sk, "expert_rules", "") or "",
            "keywords": list(getattr(sk, "keywords", []) or []),
        }
    except Exception:
        return None


# -- subcommands --------------------------------------------------------------

def _write_adapt_artifact(
    *,
    namespace: str,
    playbooks_dir: Path,
    feedback_root: Path,
    mode: str,
    results: list[dict],
    summary: dict,
    bullets_before: dict[str, dict],
    bullets_after: dict[str, dict],
    since: str | None = None,
    limit: int | None = None,
    conversation_id: str | None = None,
    turn_ids: list[str] | None = None,
    run_stamp: str | None = None,
    runs_dir_root: Path | None = None,
) -> Path:
    from .eval import runner as eval_runner

    ts_utc = datetime.now(timezone.utc).isoformat(timespec="seconds")
    stamp = run_stamp or ts_utc.replace(":", "").replace("-", "")
    runs_root = Path(runs_dir_root) if runs_dir_root is not None else Path(eval_runner.DEFAULT_RUNS_DIR)
    run_dir = runs_root / stamp
    run_dir.mkdir(parents=True, exist_ok=True)

    changed_bullets = _diff_playbook_bullet_state(bullets_before, bullets_after)
    submitters = sorted({
        str(r.get("submitted_by") or "").strip()
        for r in results
        if str(r.get("submitted_by") or "").strip()
    })

    payload = {
        "ts_utc": ts_utc,
        "namespace": namespace,
        "mode": mode,
        "playbooks_dir": str(playbooks_dir),
        "feedback_root": str(feedback_root),
        "since": since,
        "limit": limit,
        "conversation_id": conversation_id,
        "turn_ids": turn_ids or [],
        "summary": summary,
        "submitters": submitters,
        "playbook_changes": changed_bullets,
        "results": results,
    }

    out_path = run_dir / f"adapt_{stamp}.json"
    out_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"[ace.cli] adapt artifact written: {out_path}")
    return out_path

def _run_adapt_batch(
    args,
    *,
    allow_push: bool = True,
    artifact_run_stamp: str | None = None,
    artifact_runs_dir: Path | None = None,
) -> list[dict]:
    """Run one adapt batch and return per-turn results."""
    playbooks_dir = _resolve_playbooks_dir(args.namespace)
    feedback_root = _resolve_feedback_root()
    bullets_before = _capture_playbook_bullet_state(playbooks_dir)
    llm = _build_llm(args.model)
    llm.reset_usage()
    history = HistoryWriter(root=playbooks_dir / "history")
    runner = AceRunner(
        llm=llm,
        playbooks_dir=playbooks_dir,
        feedback_root=feedback_root,
        skills=args.skill or None,
        skill_context_provider=partial(_skill_context_provider, namespace=args.namespace),
        history=history,
        feedback_prefix=_feedback_prefix(args.namespace),
        exclude_users=args.exclude_user or None,
    )
    pinned_convs = getattr(args, "conversation", None) or []
    if pinned_convs:
        # Cursor-safe: use run_one per conversation so the batch cursor is not
        # advanced and nightly runs still process these conversations normally.
        print(f"[ace.cli] --conversation filter: {pinned_convs}")
        results = []
        prefix = _feedback_prefix(args.namespace)
        for cid in pinned_convs:
            snap_path = feedback_root / "conversations" / f"{prefix}{cid}.json"
            if not snap_path.exists():
                print(f"[ace.cli] snapshot not found, skipping: {snap_path}")
                results.append({"status": "no_snapshot", "conversation_id": cid})
                continue
            try:
                snap = json.loads(snap_path.read_text(encoding="utf-8"))
            except Exception as e:
                results.append({"status": "snapshot_read_error", "conversation_id": cid, "error": str(e)})
                continue
            turn_ids = [t.get("turn_id") for t in snap.get("turns", [])
                        if t.get("turn_id") and t.get("feedback")]
            if not turn_ids:
                print(f"[ace.cli] no feedback turns in {cid}, skipping")
                results.append({"status": "no_feedback_turns", "conversation_id": cid})
                continue
            for tid in turn_ids:
                results.append(runner.run_one(cid, tid, run_source=f"cli-adapt-pinned-{args.namespace}"))
    else:
        results = runner.run_batch(
            since=args.since,
            max_turns=args.limit,
            run_source=f"cli-adapt-{args.namespace}",
        )
    summary = {
        "processed": len(results),
        "ok": sum(1 for r in results if r.get("status") == "ok"),
        "skipped": sum(1 for r in results if r.get("status") != "ok"),
        "excluded": sum(1 for r in results if r.get("status") == "excluded_user"),
        "token_usage": llm.get_usage(),
    }
    bullets_after = _capture_playbook_bullet_state(playbooks_dir)
    _write_adapt_artifact(
        namespace=args.namespace,
        playbooks_dir=playbooks_dir,
        feedback_root=feedback_root,
        mode="adapt",
        results=results,
        summary=summary,
        bullets_before=bullets_before,
        bullets_after=bullets_after,
        since=args.since,
        limit=args.limit,
        run_stamp=artifact_run_stamp,
        runs_dir_root=artifact_runs_dir,
    )
    print(json.dumps(summary, indent=2))
    if args.verbose:
        for r in results:
            print(json.dumps(r, indent=2, default=str)[:2000])
    if allow_push and args.push:
        _push_now(args.namespace)
    return results

def cmd_adapt(args):
    if getattr(args, "dry_run", False):
        # Preview only: no LLM, no writes, no cursor advance. Lists the turns
        # a real run would process next (honouring --exclude-user).
        runner = AceRunner(
            llm=None,
            playbooks_dir=_resolve_playbooks_dir(args.namespace),
            feedback_root=_resolve_feedback_root(),
            skill_context_provider=partial(_skill_context_provider, namespace=args.namespace),
            feedback_prefix=_feedback_prefix(args.namespace),
            exclude_users=args.exclude_user or None,
        )
        preview = runner.preview_batch(since=args.since, max_turns=args.limit)
        counts: dict = {}
        for p in preview:
            counts[p["status"]] = counts.get(p["status"], 0) + 1
        would = [p for p in preview if p["status"] == "would_run"]
        excluded = [p for p in preview if p["status"] == "excluded_user"]
        print(json.dumps({"dry_run": True, "namespace": args.namespace,
                          "total_events": len(preview), "by_status": counts},
                         indent=2))
        print(f"\n-- WOULD RUN ({len(would)}) --")
        for p in would:
            print(f"  {p['conversation_id']}  turn={str(p['turn_id'])[:8]}  by={p.get('submitted_by','')}")
        if excluded:
            print(f"\n-- EXCLUDED ({len(excluded)}) --")
            for p in excluded:
                print(f"  {p['conversation_id']}  by={p.get('submitted_by','')}")
        return 0

    _run_adapt_batch(args, allow_push=True)
    return 0


def cmd_adapt_one(args):
    """Adapt playbooks from a single feedback session (one conversation).

    If --turn is provided, only that turn is processed. Otherwise every turn
    in the snapshot that carries feedback is processed in order. The batch
    cursor is left untouched so this command can be re-run safely.
    """
    playbooks_dir = _resolve_playbooks_dir(args.namespace)
    feedback_root = _resolve_feedback_root()
    bullets_before = _capture_playbook_bullet_state(playbooks_dir)
    llm = _build_llm(args.model)
    llm.reset_usage()
    history = HistoryWriter(root=playbooks_dir / "history")
    runner = AceRunner(
        llm=llm,
        playbooks_dir=playbooks_dir,
        feedback_root=feedback_root,
        skills=args.skill or None,
        skill_context_provider=partial(_skill_context_provider, namespace=args.namespace),
        history=history,
        feedback_prefix=_feedback_prefix(args.namespace),
        exclude_users=args.exclude_user or None,
    )

    cid = args.conversation
    if args.turn:
        turn_ids = [args.turn]
    else:
        snap_path = runner.feedback_root / "conversations" / f"{runner.feedback_prefix}{cid}.json"
        if not snap_path.exists():
            print(json.dumps({"status": "no_snapshot", "conversation_id": cid}, indent=2))
            return 1
        try:
            snap = json.loads(snap_path.read_text(encoding="utf-8"))
        except Exception as e:
            print(json.dumps({"status": "snapshot_read_error", "conversation_id": cid, "error": str(e)}, indent=2))
            return 1
        turn_ids = [
            t.get("turn_id")
            for t in snap.get("turns", [])
            if t.get("turn_id") and t.get("feedback")
        ]
        if not turn_ids:
            print(json.dumps({"status": "no_feedback_turns", "conversation_id": cid}, indent=2))
            return 0

    results = [runner.run_one(cid, tid, run_source=f"cli-adapt-one-{args.namespace}")
               for tid in turn_ids]
    summary = {
        "conversation_id": cid,
        "processed": len(results),
        "ok":        sum(1 for r in results if r.get("status") == "ok"),
        "skipped":   sum(1 for r in results if r.get("status") != "ok"),
        "excluded":  sum(1 for r in results if r.get("status") == "excluded_user"),
        "token_usage": llm.get_usage(),
    }
    bullets_after = _capture_playbook_bullet_state(playbooks_dir)
    _write_adapt_artifact(
        namespace=args.namespace,
        playbooks_dir=playbooks_dir,
        feedback_root=feedback_root,
        mode="adapt-one",
        results=results,
        summary=summary,
        bullets_before=bullets_before,
        bullets_after=bullets_after,
        conversation_id=cid,
        turn_ids=[str(tid) for tid in turn_ids],
    )
    print(json.dumps(summary, indent=2))
    if args.verbose:
        for r in results:
            print(json.dumps(r, indent=2, default=str)[:2000])
    if args.push:
        _push_now(args.namespace)
    return 0


def cmd_show(args):
    pbs_dir = _resolve_playbooks_dir(args.namespace)
    from .playbook import Playbook
    if args.skill == "workflow":
        pb = Playbook("agent", pbs_dir / "workflow.json")
    else:
        safe = args.skill.replace("/", "_").replace(" ", "_")
        pb = Playbook(args.skill, pbs_dir / f"domain_{safe}.json")
    print(pb.render())


def cmd_stats(args):
    pbs_dir = _resolve_playbooks_dir(args.namespace)
    from .playbook import Playbook
    files = sorted(pbs_dir.glob("*.json"))
    out = []
    for f in files:
        scope = "agent" if f.name == "workflow.json" else f.stem.removeprefix("domain_")
        pb = Playbook(scope, f)
        out.append(pb.stats())
    print(json.dumps(out, indent=2))


def _capture_playbook_bullet_state(playbooks_dir: Path) -> dict[str, dict]:
    """Snapshot all bullets keyed by id from workflow/domain JSONs."""
    state: dict[str, dict] = {}
    for f in sorted(playbooks_dir.glob("*.json")):
        if not (f.name == "workflow.json" or f.name.startswith("domain_")):
            continue
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        bullets = data.get("bullets") if isinstance(data, dict) else None
        if not isinstance(bullets, list):
            continue
        for b in bullets:
            if not isinstance(b, dict):
                continue
            bid = str(b.get("id") or "").strip()
            if not bid:
                continue
            state[bid] = {
                "playbook_file": f.name,
                "bullet": b,
            }
    return state


def _diff_playbook_bullet_state(before: dict[str, dict], after: dict[str, dict]) -> list[dict]:
    """Return per-bullet before/after diff rows for email reporting."""
    out: list[dict] = []
    for bid in sorted(set(before) | set(after)):
        old = before.get(bid)
        new = after.get(bid)

        if old is None and new is not None:
            change_type = "added"
        elif old is not None and new is None:
            change_type = "removed"
        else:
            old_blob = json.dumps(old["bullet"], ensure_ascii=False, sort_keys=True)
            new_blob = json.dumps(new["bullet"], ensure_ascii=False, sort_keys=True)
            same_file = old.get("playbook_file") == new.get("playbook_file")
            if old_blob == new_blob and same_file:
                continue
            change_type = "updated"

        old_file = old.get("playbook_file") if old else "-"
        new_file = new.get("playbook_file") if new else "-"
        playbook_label = old_file if old_file == new_file else f"{old_file} -> {new_file}"

        out.append({
            "bullet_id": bid,
            "change_type": change_type,
            "playbook_label": playbook_label,
            "before_text": (json.dumps(old["bullet"], indent=2, ensure_ascii=False)
                            if old else "(none)"),
            "after_text": (json.dumps(new["bullet"], indent=2, ensure_ascii=False)
                           if new else "(none)"),
        })
    return out


def cmd_pipeline(args):
    """Full pipeline in one command: adapt → eval (judge) → review.

    Reuses cmd_adapt for the training step (so --exclude-user / --push / the
    local .env key etc. all apply), then replays the golden cases against the
    freshly-updated playbook (eval/runner) and runs the regression review
    (eval/review). Returns 0 when the review verdict is PASS, 2 on regression.
    """
    # 1. Adapt (train). Honours --namespace / --exclude-user / etc.
    # NOTE: push is deliberately deferred to AFTER review passes (see step 6),
    # so we suppress --push during the adapt step here. A regression (or, in the
    # future, a manager who has not yet approved) must never reach the cloud.
    from .eval import runner as eval_runner

    want_push = bool(getattr(args, "push", False))
    runs_dir = Path(args.runs_dir or eval_runner.DEFAULT_RUNS_DIR)
    pipeline_stamp = datetime.now(timezone.utc).isoformat(timespec="seconds").replace(":", "").replace("-", "")
    set_run_log_dest(runs_dir / pipeline_stamp)
    playbooks_dir = _resolve_playbooks_dir(args.namespace)
    bullets_before = _capture_playbook_bullet_state(playbooks_dir)
    args.push = False
    print("[pipeline] ===== STEP 1/3: adapt =====")
    adapt_results = _run_adapt_batch(
        args,
        allow_push=False,
        artifact_run_stamp=pipeline_stamp,
        artifact_runs_dir=runs_dir,
    )
    submitter_counts: dict[str, int] = {}
    for r in adapt_results:
        if r.get("status") != "ok":
            continue
        submitter = str(r.get("submitted_by") or "").strip()
        if not submitter:
            continue
        submitter_counts[submitter] = submitter_counts.get(submitter, 0) + 1

    used_submitter_rows = [
        {"submitter": name, "count": cnt}
        for name, cnt in sorted(submitter_counts.items(), key=lambda kv: (-kv[1], kv[0].lower()))
    ]
    used_submitters = [row["submitter"] for row in used_submitter_rows]
    used_submitter_total = sum(row["count"] for row in used_submitter_rows)

    # Collect vote==-1 cases from this adapt batch now (adapt phase). The
    # re-answer + per-user email is deferred until AFTER review PASSes; the
    # manifest is written here so it exists even if review later fails.
    from .eval import reverify as eval_reverify
    feedback_root = _resolve_feedback_root()
    try:
        eval_reverify.build_downvote_manifest(
            namespace=args.namespace,
            feedback_root=feedback_root,
            playbooks_dir=playbooks_dir,
            adapt_results=adapt_results,
            playbook_changes=[],
            run_stamp=pipeline_stamp,
            runs_dir=runs_dir,
        )
    except Exception as exc:
        print(f"[pipeline] WARN: downvote manifest build failed: {exc}")

    if args.no_eval:
        if want_push:
            print("[pipeline] --no-eval set: push deferred (no eval/review gate); "
                  "run `adapt --push` explicitly if you want to publish now.")
        else:
            print("[pipeline] --no-eval set: stopping after adapt.")
        return 0

    # 2. Eval / judge — replay golden cases against the updated playbook and
    # score them. Lazy import: eval/runner imports this module, so a top-level
    # import here would be circular.
    print("[pipeline] ===== STEP 2/3: eval (judge) =====")
    cases_dir = args.cases_dir or eval_runner._default_cases_dir()
    report = eval_runner.evaluate(
        cases_dir=cases_dir,
        runs_dir=runs_dir,
        run_stamp=pipeline_stamp,
        case_id_filter=None,
        passes=args.passes,
        judge_temperature=0.2,
        chat_temperature=0.0,
        max_steps=args.max_steps,
        use_tools=not args.no_tools,
        model=args.model,
        judge_model=args.judge_model,
    )
    # Resolve eval report path from the runner output when available.
    eval_path_raw = report.get("eval_path") if isinstance(report, dict) else None
    if eval_path_raw:
        out_path = Path(eval_path_raw)
    elif isinstance(report, dict) and report.get("ts_utc"):
        # Fallback for older runner outputs that didn't include eval_path.
        stamp = str(report["ts_utc"]).replace(":", "").replace("-", "")
        out_path = Path(runs_dir) / stamp / f"eval_{stamp}.json"
    else:
        status = str((report or {}).get("status") or "unknown")
        print(f"[pipeline] eval produced no reviewable report (status={status}).")
        if status in ("no_cases", "no_cases_triggered"):
            return 0
        return 1

    # 3. Review — regression check of this eval vs the previous one in runs/.
    print("[pipeline] ===== STEP 3/3: review =====")
    from .eval import review as eval_review
    rreport = eval_review.review(out_path, model=args.model)
    verdict = rreport.get("gate_verdict")
    print(f"[pipeline] review verdict: {verdict}")

    # Path to the review_*.json review() just wrote — shared by both
    # find-the-killer (forensics) and triage (mutation).
    review_stamp = (rreport.get("ts_utc") or "").replace(":", "").replace("-", "")
    review_path = Path(runs_dir) / f"review_{review_stamp}.json"

    # 4. Find the killer — for every harmful+revert bullet the reviewer named,
    # trace the offending feedback turn. Runs BEFORE triage so we read the
    # still-corrupted live bullet (with post-corruption updated_at /
    # source_turn_ids) rather than the post-triage state.
    killer_report = None
    from .eval import find_the_killer as eval_killer
    if not getattr(args, "no_find_killer", False) and \
            eval_killer._extract_corrupted_bullets(rreport):
        print("[pipeline] ===== STEP 4: find the killer =====")
        try:
            killer_report = eval_killer.process(
                review_path,
                namespace=args.namespace,
                model=args.model,
            )
        except Exception as exc:
            print(f"[pipeline] WARN: find_the_killer failed: {exc}")

    # 5. Triage — when the review FAILs, auto-fix flagged harmful bullets
    # by default (revert if snapshot exists, else remove). Use --no-triage
    # to skip this mutation step.
    triage_report = None
    if verdict != "PASS" and not getattr(args, "no_triage", False):
        print("[pipeline] ===== STEP 5: corrupted-bullet triage =====")
        from .eval import corrupted_bullet as eval_triage
        triage_report = eval_triage.process(
            review_path,
            auto_revert=True,
            auto_remove=True,
            namespace=args.namespace,
        )

    # 6. Publish — only a passing review is allowed to reach the cloud share.
    # FUTURE: instead of pushing here, notify the manager (email) and wait for
    # an explicit approval before calling _push_now(). For now push runs
    # automatically on PASS when --push was requested.
    if verdict == "PASS":
        if want_push:
            print("[pipeline] review PASSED — publishing playbook to cloud share.")
            _push_now(args.namespace)
    elif want_push:
        print("[pipeline] review did NOT pass — push withheld (nothing published).")

    bullets_after = _capture_playbook_bullet_state(playbooks_dir)
    rreport["_playbook_changes"] = _diff_playbook_bullet_state(
        bullets_before,
        bullets_after,
    )
    rreport["_feedback_submitter_rows"] = used_submitter_rows
    rreport["_feedback_submitter_total"] = used_submitter_total
    rreport["_feedback_submitters"] = used_submitters

    # 7. Notification (optional) — sent when notify_email recipient list is set.
    # Uses SMTP relay/auth settings from environment variables.
    try:
        from .eval import notify_email as eval_notify
        if eval_notify.notify_from_env(
            namespace=args.namespace,
            review_report=rreport,
            triage_report=triage_report,
            killer_report=killer_report,
        ):
            print("[pipeline] notification email sent.")
        else:
            print("[pipeline] notification skipped (recipient list not set).")
    except Exception as exc:
        print(f"[pipeline] WARN: notification failed: {exc}")

    # 6. Re-answer + down-voter notification (optional) — only on PASS, when
    # --reanswer-notify is set. Rebuilds the manifest with the real bullet
    # before/after so each down-voter's email shows what their feedback
    # changed, then re-runs the agent on their case with the updated playbook.
    if verdict == "PASS" and getattr(args, "reanswer_notify", False):
        print("[pipeline] ===== STEP 6: re-answer + down-voter notify =====")
        try:
            manifest_path = eval_reverify.build_downvote_manifest(
                namespace=args.namespace,
                feedback_root=feedback_root,
                playbooks_dir=playbooks_dir,
                adapt_results=adapt_results,
                playbook_changes=rreport["_playbook_changes"],
                run_stamp=pipeline_stamp,
                runs_dir=runs_dir,
            )
            if manifest_path is not None:
                eval_reverify.reverify_and_notify(
                    manifests=[manifest_path],
                    runs_dir=runs_dir,
                    run_stamp=pipeline_stamp,
                    dry_run=bool(getattr(args, "dry_run", False)),
                    max_steps=args.max_steps,
                )
        except Exception as exc:
            print(f"[pipeline] WARN: re-answer notification failed: {exc}")

    # Mirror `python -m services.ace.eval.review`: 0 = PASS, 2 = regression.
    return 0 if verdict == "PASS" else 2


def cmd_notify_test(args):
    """Send a standalone SMTP test email using ACE_* env configuration."""
    from .eval import notify_email as eval_notify

    try:
        sent = eval_notify.send_test_from_env(
            namespace=args.namespace,
            note=args.note or "",
            to_override=args.to,
            cc_override=args.cc,
        )
    except Exception as exc:
        print(f"[notify-test] FAILED: {exc}")
        return 1

    if not sent:
        print("[notify-test] skipped: recipient list is empty. "
              "Set ACE_NOTIFY_TO in notify_email.py or pass --to.")
        return 2

    print("[notify-test] test email sent.")
    return 0


def cmd_reverify_notify(args):
    """Re-answer down-voted cases with the updated playbook and email each
    down-voter one combined HTML replay. Consumes downvote manifests written
    by run-all's adapt phase (wifi + bt), combining per person."""
    from datetime import datetime as _dt
    from .eval import reverify as eval_reverify
    from .eval import runner as eval_runner

    runs_dir = Path(args.runs_dir or eval_runner.DEFAULT_RUNS_DIR)
    if args.manifest:
        manifests = [Path(m) for m in args.manifest]
    else:
        manifests = eval_reverify.find_latest_manifests(runs_dir)
        if not manifests:
            print(f"[reverify-notify] no downvote manifests found under {runs_dir}")
            return 0
        print(f"[reverify-notify] using manifests: {[str(m) for m in manifests]}")

    run_stamp = _dt.now(timezone.utc).isoformat(timespec="seconds").replace(":", "").replace("-", "")
    set_run_log_dest(runs_dir / run_stamp)
    eval_reverify.reverify_and_notify(
        manifests=manifests,
        runs_dir=runs_dir,
        run_stamp=run_stamp,
        dry_run=bool(args.dry_run),
        max_steps=args.max_steps,
    )
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description="ACE adaptation CLI")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_adapt = sub.add_parser("adapt", help="Walk new feedback events and update playbooks")
    p_adapt.add_argument("--namespace", choices=("wifi", "bt"), default="wifi",
                         help="Which playbook set to adapt: wifi (default) or bt")
    p_adapt.add_argument("--since", default=None,
                         help="ISO timestamp to start from (defaults to last cursor)")
    p_adapt.add_argument("--skill", action="append",
                         help="Pre-create a domain playbook for this skill (repeatable)")
    p_adapt.add_argument("--exclude-user", action="append", metavar="SUBMITTER",
                         help="Ignore feedback from this submitter (email/UPN); "
                              "repeatable. Matched case-insensitively.")
    p_adapt.add_argument("--dry-run", action="store_true",
                         help="Preview which turns would run (and which are "
                              "excluded) without calling the LLM or writing anything")
    p_adapt.add_argument("--limit", type=int, default=None, help="Stop after N turns")
    p_adapt.add_argument("--model", default=None, help="Override model id")
    p_adapt.add_argument("--verbose", action="store_true")
    p_adapt.add_argument("--conversation", action="append", metavar="CID",
                         help="Only adapt this conversation id (repeatable); cursor is NOT advanced")
    p_adapt.add_argument("--push", action="store_true",
                         help="After adapting, mirror-sync local playbooks + history to the cloud share")
    p_adapt.set_defaults(func=cmd_adapt)

    p_adapt_one = sub.add_parser(
        "adapt-one",
        help="Adapt playbooks from a single feedback session (one conversation)",
    )
    p_adapt_one.add_argument("--namespace", choices=("wifi", "bt"), default="wifi",
                             help="Which playbook set to adapt: wifi (default) or bt")
    p_adapt_one.add_argument("--conversation", required=True,
                             help="Conversation id (matches conversations/<id>.json)")
    p_adapt_one.add_argument("--turn", default=None,
                             help="Optional turn id; if omitted, all feedback turns in the session are processed")
    p_adapt_one.add_argument("--skill", action="append",
                             help="Pre-create a domain playbook for this skill (repeatable)")
    p_adapt_one.add_argument("--exclude-user", action="append", metavar="SUBMITTER",
                             help="Ignore feedback from this submitter (email/UPN); "
                                  "repeatable. Matched case-insensitively.")
    p_adapt_one.add_argument("--model", default=None, help="Override model id")
    p_adapt_one.add_argument("--verbose", action="store_true")
    p_adapt_one.add_argument("--push", action="store_true",
                             help="After adapting, mirror-sync local playbooks + history to the cloud share")
    p_adapt_one.set_defaults(func=cmd_adapt_one)

    p_show = sub.add_parser("show", help="Print a playbook")
    p_show.add_argument("--namespace", choices=("wifi", "bt"), default="wifi",
                        help="Which playbook set to read from: wifi (default) or bt")
    p_show.add_argument("--skill", required=True,
                        help='"workflow" or a skill name (e.g. Connectivity)')
    p_show.set_defaults(func=cmd_show)

    p_stats = sub.add_parser("stats", help="Summary of every playbook on disk")
    p_stats.add_argument("--namespace", choices=("wifi", "bt"), default="wifi",
                         help="Which playbook set to summarize: wifi (default) or bt")
    p_stats.set_defaults(func=cmd_stats)

    p_pipe = sub.add_parser(
        "run-all",
        help="Full pipeline in one command: adapt → eval (judge) → review",
    )
    # --- adapt options (same as `adapt`) ---
    p_pipe.add_argument("--namespace", choices=("wifi", "bt"), default="wifi",
                        help="Which playbook set to adapt: wifi (default) or bt")
    p_pipe.add_argument("--since", default=None,
                        help="ISO timestamp to start from (defaults to last cursor)")
    p_pipe.add_argument("--limit", type=int, default=None, help="Stop after N turns")
    p_pipe.add_argument("--skill", action="append",
                        help="Pre-create a domain playbook for this skill (repeatable)")
    p_pipe.add_argument("--exclude-user", action="append", metavar="SUBMITTER",
                        help="Ignore feedback from this submitter (repeatable, case-insensitive)")
    p_pipe.add_argument("--model", default=None, help="Override LLM model id")
    p_pipe.add_argument("--conversation", action="append", metavar="CID",
                        help="Only adapt this conversation id (repeatable); cursor is NOT advanced")
    p_pipe.add_argument("--push", action="store_true",
                        help="After adapting, mirror-sync playbooks + history to the cloud share")
    p_pipe.add_argument("--verbose", action="store_true")
    # --- eval / judge options ---
    p_pipe.add_argument("--cases-dir", type=Path, default=None,
                        help="Golden cases dir (default: eval's built-in golden_set share)")
    p_pipe.add_argument("--runs-dir", type=Path, default=None,
                        help="Where eval reports are written (default: eval's runs/)")
    p_pipe.add_argument("--passes", type=int, default=1,
                        help="Judge passes per case (default 1)")
    p_pipe.add_argument("--max-steps", type=int, default=6,
                        help="Max chatbot tool steps per case (default 6)")
    p_pipe.add_argument("--no-tools", action="store_true",
                        help="Disable chatbot tool use during replay")
    p_pipe.add_argument("--judge-model", nargs="+", default=None, metavar="MODEL",
                        help="One or more judge model names")
    # --- stage control ---
    p_pipe.add_argument("--no-eval", action="store_true",
                        help="Only adapt; skip eval + review")
    p_pipe.add_argument("--no-review", action="store_true",
                        help="Adapt + eval; skip the regression review")
    # --- post-review forensics (runs by default when review flagged bullets) ---
    p_pipe.add_argument("--no-find-killer", action="store_true",
                        help="Skip the post-review whodunit that traces "
                             "harmful+revert bullets back to the offending "
                             "feedback turn. Default is to run when the "
                             "review flagged any corrupted bullets.")
    # --- post-review triage (runs by default when review FAILs) ---
    p_pipe.add_argument("--no-triage", action="store_true",
                        help="Skip post-review auto-fix of harmful bullets. "
                             "Default behavior on FAIL is auto-revert (or "
                             "remove when no snapshot version exists).")
    # --- re-answer + down-voter notify (only on PASS) ---
    p_pipe.add_argument("--reanswer-notify", action="store_true",
                        help="On review PASS, re-run the agent on this "
                             "namespace's down-voted cases with the updated "
                             "playbook and email each down-voter a replay HTML.")
    p_pipe.add_argument("--dry-run", action="store_true",
                        help="With --reanswer-notify: build the replay HTML but "
                             "do not send any email.")
    p_pipe.set_defaults(func=cmd_pipeline)

    p_notify = sub.add_parser(
        "notify-test",
        help="Send a standalone SMTP test email using ACE_* environment variables",
    )
    p_notify.add_argument("--namespace", choices=("wifi", "bt"), default="wifi",
                          help="Tag used in the email subject (default wifi)")
    p_notify.add_argument("--to", default=None,
                          help="Override recipient list for this run only "
                               "(comma/semicolon separated)")
    p_notify.add_argument("--cc", default=None,
                          help="Override CC list for this run only "
                               "(comma/semicolon separated)")
    p_notify.add_argument("--note", default="",
                          help="Optional test note shown in the email body")
    p_notify.set_defaults(func=cmd_notify_test)

    p_reverify = sub.add_parser(
        "reverify-notify",
        help="Re-answer down-voted cases with the updated playbook and email "
             "each down-voter a combined HTML replay (combines wifi + bt).",
    )
    p_reverify.add_argument("--manifest", action="append", metavar="PATH",
                            help="Downvote manifest path (repeatable). Default: "
                                 "newest downvotes_*.json per namespace under runs/.")
    p_reverify.add_argument("--runs-dir", type=Path, default=None,
                            help="Where run artifacts live (default: eval's runs/)")
    p_reverify.add_argument("--max-steps", type=int, default=6,
                            help="Max chatbot tool steps per case (default 6)")
    p_reverify.add_argument("--dry-run", action="store_true",
                            help="Build the replay HTML but do not send email.")
    p_reverify.set_defaults(func=cmd_reverify_notify)

    args = parser.parse_args(argv)

    # Tee all console output to a temp file for the duration of the run; once
    # a command resolves its stamp folder (via set_run_log_dest) the captured
    # log is copied there as run.log for later debugging.
    boot_stamp = datetime.now(timezone.utc).isoformat(timespec="seconds").replace(":", "").replace("-", "")
    tmp_log = Path(tempfile.gettempdir()) / f"ace_run_{boot_stamp}.log"
    _old_out, _old_err = sys.stdout, sys.stderr
    _fh = open(tmp_log, "w", encoding="utf-8")
    sys.stdout = _Tee(_old_out, _fh)
    sys.stderr = _Tee(_old_err, _fh)
    try:
        _ensure_avatarfiles_dir()
        namespace = getattr(args, "namespace", "wifi")
        try:
            ace_sync.sync_at_boot(namespace=namespace)
        except Exception as e:
            print(f"[ace.cli] cloud sync skipped: {e}")
        print(f"[ace.cli] namespace = {namespace}")
        print(f"[ace.cli] playbooks_dir = {_resolve_playbooks_dir(namespace)}")
        return args.func(args) or 0
    finally:
        sys.stdout, sys.stderr = _old_out, _old_err
        try:
            _fh.flush()
            _fh.close()
        except Exception:
            pass
        if _RUN_LOG_DEST is not None:
            try:
                dest = _RUN_LOG_DEST / "run.log"
                shutil.copy2(tmp_log, dest)
                print(f"[ace.cli] run log saved: {dest}")
            except Exception as e:
                print(f"[ace.cli] run log copy failed ({tmp_log} -> {_RUN_LOG_DEST}): {e}")
        else:
            print(f"[ace.cli] run log (no stamp folder): {tmp_log}")


if __name__ == "__main__":
    sys.exit(main())
