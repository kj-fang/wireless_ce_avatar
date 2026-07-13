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
from pathlib import Path

from configs import path_configs
from configs.global_configs import app_config
from services.llm_service import LLM_helper
from utils import helpers

from . import sync_utils as ace_sync
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


def _resolve_playbooks_dir() -> Path:
    """Cheap — just resolves the local working dir. The (network-bound)
    cloud sync itself runs once in main(), before any subcommand."""
    return ace_sync.local_working_dir()


def _build_llm(model: str | None) -> LLM_helper:
    """Mirror configs.set_up_app so Reflector / Curator share the main app's LLM."""
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


_SKILLS_CACHE: dict | None = None


def _load_active_skills() -> dict:
    """Best-effort load of the active skills YAML (same one the live agent uses)
    so Reflector/Curator can see each skill's description + expert_rules. Cached.
    Returns an empty dict on any failure — callers degrade gracefully."""
    global _SKILLS_CACHE
    if _SKILLS_CACHE is not None:
        return _SKILLS_CACHE
    try:
        from utils import skills_yaml_utils
        from services.log_chatbot_service import load_skills_from_yaml
        yaml_path, _date, _src = skills_yaml_utils.current_active_yaml()
        if not yaml_path:
            _SKILLS_CACHE = {}
            return _SKILLS_CACHE
        _SKILLS_CACHE = load_skills_from_yaml(str(yaml_path)) or {}
        print(f"[ace.cli] loaded {len(_SKILLS_CACHE)} skill definition(s) from {yaml_path}")
    except Exception as e:
        print(f"[ace.cli] skill YAML unavailable ({e}); Reflector/Curator will run without skill context")
        _SKILLS_CACHE = {}
    return _SKILLS_CACHE


def _skill_context_provider(sid: str):
    skills = _load_active_skills()
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
    runner = AceRunner(
        llm=llm,
        playbooks_dir=_resolve_playbooks_dir(),
        feedback_root=_resolve_feedback_root(),
        skills=args.skill or None,
        skill_context_provider=_skill_context_provider,
    )
    results = runner.run_batch(since=args.since, max_turns=args.limit)
    summary = {
        "processed": len(results),
        "ok":        sum(1 for r in results if r.get("status") == "ok"),
        "skipped":   sum(1 for r in results if r.get("status") != "ok"),
    }
    print(json.dumps(summary, indent=2))
    if args.verbose:
        for r in results:
            print(json.dumps(r, indent=2, default=str)[:2000])


def cmd_adapt_one(args):
    """Adapt playbooks from a single feedback session (one conversation).

    If --turn is provided, only that turn is processed. Otherwise every turn
    in the snapshot that carries feedback is processed in order. The batch
    cursor is left untouched so this command can be re-run safely.
    """
    llm = _build_llm(args.model)
    runner = AceRunner(
        llm=llm,
        playbooks_dir=_resolve_playbooks_dir(),
        feedback_root=_resolve_feedback_root(),
        skills=args.skill or None,
        skill_context_provider=_skill_context_provider,
    )

    cid = args.conversation
    if args.turn:
        turn_ids = [args.turn]
    else:
        snap_path = runner.feedback_root / "conversations" / f"{cid}.json"
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

    results = [runner.run_one(cid, tid) for tid in turn_ids]
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
    return 0


def cmd_show(args):
    pbs_dir = _resolve_playbooks_dir()
    from .playbook import Playbook
    if args.skill == "workflow":
        pb = Playbook("agent", pbs_dir / "workflow.json")
    else:
        safe = args.skill.replace("/", "_").replace(" ", "_")
        pb = Playbook(args.skill, pbs_dir / f"domain_{safe}.json")
    print(pb.render())


def cmd_stats(args):
    pbs_dir = _resolve_playbooks_dir()
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
    p_adapt.add_argument("--since", default=None,
                         help="ISO timestamp to start from (defaults to last cursor)")
    p_adapt.add_argument("--skill", action="append",
                         help="Pre-create a domain playbook for this skill (repeatable)")
    p_adapt.add_argument("--limit", type=int, default=None, help="Stop after N turns")
    p_adapt.add_argument("--model", default=None, help="Override model id")
    p_adapt.add_argument("--verbose", action="store_true")
    p_adapt.set_defaults(func=cmd_adapt)

    p_adapt_one = sub.add_parser(
        "adapt-one",
        help="Adapt playbooks from a single feedback session (one conversation)",
    )
    p_adapt_one.add_argument("--conversation", required=True,
                             help="Conversation id (matches conversations/<id>.json)")
    p_adapt_one.add_argument("--turn", default=None,
                             help="Optional turn id; if omitted, all feedback turns in the session are processed")
    p_adapt_one.add_argument("--skill", action="append",
                             help="Pre-create a domain playbook for this skill (repeatable)")
    p_adapt_one.add_argument("--model", default=None, help="Override model id")
    p_adapt_one.add_argument("--verbose", action="store_true")
    p_adapt_one.set_defaults(func=cmd_adapt_one)

    p_show = sub.add_parser("show", help="Print a playbook")
    p_show.add_argument("--skill", required=True,
                        help='"workflow" or a skill name (e.g. Connectivity)')
    p_show.set_defaults(func=cmd_show)

    p_stats = sub.add_parser("stats", help="Summary of every playbook on disk")
    p_stats.set_defaults(func=cmd_stats)

    args = parser.parse_args(argv)
    _ensure_avatarfiles_dir()
    try:
        ace_sync.sync_at_boot()
    except Exception as e:
        print(f"[ace.cli] cloud sync skipped: {e}")
    print(f"[ace.cli] playbooks_dir = {_resolve_playbooks_dir()}")
    return args.func(args) or 0


if __name__ == "__main__":
    sys.exit(main())
