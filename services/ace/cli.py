"""
CLI entry point for the ACE adaptation loop.

Usage:
    python -m services.ace.cli adapt     --since 2026-05-01T00:00:00
    python -m services.ace.cli adapt-one --conversation <cid> [--turn <tid>]
    python -m services.ace.cli show      --skill Connectivity
    python -m services.ace.cli stats

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
import sys
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


def _build_llm(model: str | None) -> LLM_helper:
    """Mirror configs.set_up_app so Reflector / Curator share the main app's LLM."""
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
        from services.chatbot.agent.system import load_skills_from_yaml
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

def cmd_adapt(args):
    llm = _build_llm(args.model)
    history = HistoryWriter(root=_resolve_playbooks_dir(args.namespace) / "history")
    runner = AceRunner(
        llm=llm,
        playbooks_dir=_resolve_playbooks_dir(args.namespace),
        feedback_root=_resolve_feedback_root(),
        skills=args.skill or None,
        skill_context_provider=partial(_skill_context_provider, namespace=args.namespace),
        history=history,
        feedback_prefix=_feedback_prefix(args.namespace),
    )
    results = runner.run_batch(since=args.since, max_turns=args.limit,
                               run_source=f"cli-adapt-{args.namespace}")
    summary = {
        "processed": len(results),
        "ok":        sum(1 for r in results if r.get("status") == "ok"),
        "skipped":   sum(1 for r in results if r.get("status") != "ok"),
    }
    print(json.dumps(summary, indent=2))
    if args.verbose:
        for r in results:
            print(json.dumps(r, indent=2, default=str)[:2000])
    if args.push:
        _push_now(args.namespace)


def cmd_adapt_one(args):
    """Adapt playbooks from a single feedback session (one conversation).

    If --turn is provided, only that turn is processed. Otherwise every turn
    in the snapshot that carries feedback is processed in order. The batch
    cursor is left untouched so this command can be re-run safely.
    """
    llm = _build_llm(args.model)
    history = HistoryWriter(root=_resolve_playbooks_dir(args.namespace) / "history")
    runner = AceRunner(
        llm=llm,
        playbooks_dir=_resolve_playbooks_dir(args.namespace),
        feedback_root=_resolve_feedback_root(),
        skills=args.skill or None,
        skill_context_provider=partial(_skill_context_provider, namespace=args.namespace),
        history=history,
        feedback_prefix=_feedback_prefix(args.namespace),
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
    }
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


# -- eval / golden subcommands -------------------------------------------------

def _eval_feedback_roots() -> list:
    """Remote-resolved root first, local fallback second (dedup handled by
    the case registry)."""
    roots = [_resolve_feedback_root()]
    base = getattr(app_config, "avatarfiles_dir", None)
    local = Path(base) / "feedback" if base else Path.cwd() / "data" / "feedback"
    if local.exists() and local.resolve() != Path(roots[0]).resolve():
        roots.append(local)
    return roots


def _print_event(ev: str, payload: dict) -> None:
    print(json.dumps({"event": ev, **payload}, default=str))


def cmd_eval(args):
    from .eval.cases import list_cases
    from .eval.golden import GoldenSet
    from .eval.harness import EvalHarness, EvalConfig
    from .eval.store import EvalStore
    from .history import HistoryWriter

    if args.smoke:
        from .eval.smoke import run_smoke
        return run_smoke(cases=args.cases or 2, verbose=args.verbose)

    roots = _eval_feedback_roots()
    golden = GoldenSet(roots[0])

    if args.list_cases:
        cases = list_cases(roots, golden=golden)
        rows = [c.summary() for c in cases]
        print(json.dumps({
            "total": len(rows),
            "replayable": sum(1 for r in rows if r["replayable"]),
            "golden": sum(1 for r in rows if r["golden"]),
            "cases": rows,
        }, indent=2, default=str))
        return 0

    pbs_dir = _resolve_playbooks_dir()
    history = HistoryWriter(pbs_dir / "history")
    store = EvalStore(pbs_dir / "history" / "evals")

    def _sync(job_id: str):
        # eval/golden are wifi-scoped for now (see cmd_adapt/cmd_show for the
        # namespace-aware equivalents).
        try:
            share = ace_sync.resolve_cloud_playbook_dir("wifi")
            if not share:
                print("[ace.cli] rollback sync skipped: share unreachable")
                return
            from .sync import launch_sync_background
            launch_sync_background(
                local_dir=pbs_dir,
                remote_root_raw=share,
                job_id=job_id,
            )
        except Exception as e:
            print(f"[ace.cli] rollback sync skipped: {e}")

    harness = EvalHarness(
        playbooks_dir=pbs_dir,
        feedback_roots=roots,
        history=history,
        store=store,
        llm_factory=_build_llm,
        skills_loader=_load_active_skills,
        golden=golden,
        emit=_print_event,
        sync_fn=_sync,
    )
    config = EvalConfig(
        max_cases=args.cases,
        max_steps=args.max_steps,
        gate=args.gate,
        gate_margin=args.gate_margin,
        case_source=args.source or "",
        conversation_ids=args.conversation or [],
        before=args.before or "",
        agent_model=args.model or "",
        judge_model=args.judge_model or "",
        source="cli",
    )
    report = harness.run(config)
    if args.verbose:
        print(json.dumps(report, indent=2, default=str))
    else:
        print(json.dumps({
            "summary": report.get("summary"),
            "gate": report.get("gate"),
            "error": report.get("error"),
        }, indent=2, default=str))
    return 1 if report.get("error") else 0


def cmd_eval_report(args):
    from .eval.store import EvalStore
    store = EvalStore(_resolve_playbooks_dir() / "history" / "evals")
    if args.date and args.name:
        rep = store.read_report(args.date, args.name)
    else:
        rep = store.latest()
    if rep is None:
        print(json.dumps({"error": "no eval report found"}))
        return 1
    print(json.dumps(rep, indent=2, default=str))
    return 0


def cmd_golden(args):
    from .eval.golden import GoldenSet
    from .eval.cases import list_cases
    roots = _eval_feedback_roots()
    golden = GoldenSet(roots[0])

    if args.action == "list":
        entries = golden.list()
        # Enrich with replayability so the user sees at a glance which golden
        # cases will actually run.
        cases = {(c.conversation_id, c.turn_id): c for c in list_cases(roots)}
        for e in entries:
            match = None
            for (cid, tid), c in cases.items():
                if cid == e.get("conversation_id") and (
                        e.get("turn_id") is None or tid == e.get("turn_id")):
                    match = c
                    break
            e["replayable"] = bool(match and match.replayable)
            e["subject"] = match.issue.get("subject", "") if match else ""
        print(json.dumps({"count": len(entries), "entries": entries},
                         indent=2, default=str))
        return 0

    if args.action == "add":
        res = golden.add(args.conversation, args.turn or None, note=args.note or "")
        print(json.dumps(res, indent=2, default=str))
        return 1 if res.get("error") else 0

    if args.action == "remove":
        ok = golden.remove(args.conversation, args.turn or None)
        print(json.dumps({"removed": ok}))
        return 0 if ok else 1

    print(f"unknown golden action: {args.action}")
    return 1


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
    p_adapt.add_argument("--limit", type=int, default=None, help="Stop after N turns")
    p_adapt.add_argument("--model", default=None, help="Override model id")
    p_adapt.add_argument("--verbose", action="store_true")
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

    p_eval = sub.add_parser(
        "eval",
        help="Replay cases against before/after playbooks, score, and gate",
    )
    p_eval.add_argument("--cases", type=int, default=6,
                        help="Max cases to replay (cost cap, default 6)")
    p_eval.add_argument("--source", default=None,
                        choices=["golden", "golden+affected", "auto"],
                        help="Case selection (default: golden+affected when a "
                             "golden set exists, else auto)")
    p_eval.add_argument("--conversation", action="append",
                        help="Restrict/prioritize to this conversation id (repeatable)")
    p_eval.add_argument("--before", default=None,
                        help='Before-snapshot ref "YYYY-MM-DD/<run_dir>" '
                             "(default: newest snapshot)")
    p_eval.add_argument("--gate", dest="gate", action="store_true", default=True,
                        help="Enable auto-rollback on regression (default)")
    p_eval.add_argument("--no-gate", dest="gate", action="store_false",
                        help="Report only; never roll back")
    p_eval.add_argument("--gate-margin", type=int, default=1,
                        help="Rollback when regressed >= improved + N (default 1)")
    p_eval.add_argument("--model", default=None, help="Agent replay model")
    p_eval.add_argument("--judge-model", default=None,
                        help="Judge model (defaults to the agent model)")
    p_eval.add_argument("--max-steps", type=int, default=6,
                        help="Agentic step cap per replay (default 6)")
    p_eval.add_argument("--list-cases", action="store_true",
                        help="Print the case registry (zero tokens) and exit")
    p_eval.add_argument("--smoke", action="store_true",
                        help="Run the zero-token end-to-end smoke test with fakes")
    p_eval.add_argument("--verbose", action="store_true")
    p_eval.set_defaults(func=cmd_eval)

    p_eval_report = sub.add_parser("eval-report", help="Print a stored eval report")
    p_eval_report.add_argument("--date", default=None, help="YYYY-MM-DD")
    p_eval_report.add_argument("--name", default=None, help="Report filename")
    p_eval_report.set_defaults(func=cmd_eval_report)

    p_golden = sub.add_parser(
        "golden", help="Manage the curated golden-case set for eval runs")
    p_golden.add_argument("action", choices=["list", "add", "remove"])
    p_golden.add_argument("--conversation", default=None,
                          help="Conversation id (required for add/remove)")
    p_golden.add_argument("--turn", default=None,
                          help="Optional turn id (default: whole conversation)")
    p_golden.add_argument("--note", default=None,
                          help="Why this case is golden (add only)")
    p_golden.set_defaults(func=cmd_golden)

    args = parser.parse_args(argv)
    if getattr(args, "cmd", "") == "golden" and args.action in ("add", "remove") \
            and not args.conversation:
        parser.error("golden add/remove requires --conversation")
    _ensure_avatarfiles_dir()
    namespace = getattr(args, "namespace", "wifi")
    try:
        ace_sync.sync_at_boot(namespace=namespace)
    except Exception as e:
        print(f"[ace.cli] cloud sync skipped: {e}")
    print(f"[ace.cli] namespace = {namespace}")
    print(f"[ace.cli] playbooks_dir = {_resolve_playbooks_dir(namespace)}")
    return args.func(args) or 0


if __name__ == "__main__":
    sys.exit(main())
