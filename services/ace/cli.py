"""
CLI entry point for the ACE adaptation loop.

Usage:
    python -m services.ace.cli adapt   --since 2026-05-01T00:00:00
    python -m services.ace.cli show    --skill Connectivity
    python -m services.ace.cli stats
    python -m services.ace.cli run-one <conversation_id_or_path> [--turn-id TID] --debug

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

from .pipeline import AceRunner


def _resolve_feedback_root() -> Path:
    share = helpers.get_load_path(path_configs.FEEDBACK_DIR_prim, path_configs.FEEDBACK_DIR_bkup)
    if share:
        return Path(share)
    base = getattr(app_config, "avatarfiles_dir", None)
    return Path(base) / "feedback" if base else Path.cwd() / "data" / "feedback"


def _resolve_playbooks_dir() -> Path:
    base = getattr(app_config, "avatarfiles_dir", None)
    root = Path(base) / "ace_playbooks" if base else Path.cwd() / "data" / "ace_playbooks"
    root.mkdir(parents=True, exist_ok=True)
    return root


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


# -- subcommands --------------------------------------------------------------

def cmd_adapt(args):
    llm = _build_llm(args.model)
    runner = AceRunner(
        llm=llm,
        playbooks_dir=_resolve_playbooks_dir(),
        feedback_root=_resolve_feedback_root(),
        skills=args.skill or None,
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


def cmd_show(args):
    pbs_dir = _resolve_playbooks_dir()
    from .playbook import Playbook
    if args.skill == "workflow":
        pb = Playbook("agent", pbs_dir / "workflow.json")
    else:
        safe = args.skill.replace("/", "_").replace(" ", "_")
        pb = Playbook(args.skill, pbs_dir / f"domain_{safe}.json")
    print(pb.render())


def cmd_run_one(args):
    """Process a single conversation (first turn with feedback) with full debug output."""
    llm = _build_llm(args.model)
    feedback_root = _resolve_feedback_root()

    # Load the conversation snapshot to find the turn with feedback
    conv_path = Path(feedback_root) / "conversations" / f"{args.conversation_id}.json"
    if not conv_path.exists():
        # Try as a file path directly
        conv_path = Path(args.conversation_id)
    if not conv_path.exists():
        print(f"ERROR: conversation not found at {conv_path}")
        return 1

    snap = json.loads(conv_path.read_text(encoding="utf-8"))
    cid = snap["conversation_id"]

    # Find the target turn
    if args.turn_id:
        turn = next((t for t in snap.get("turns", []) if t.get("turn_id") == args.turn_id), None)
        if turn is None:
            print(f"ERROR: turn_id '{args.turn_id}' not found in conversation")
            return 1
    else:
        # Pick first turn that has feedback
        turn = next((t for t in snap.get("turns", []) if t.get("feedback")), None)
        if turn is None:
            print("ERROR: no turn with feedback found in this conversation")
            print("Available turns:")
            for t in snap.get("turns", []):
                has_fb = "✓ feedback" if t.get("feedback") else "✗ no feedback"
                print(f"  {t['turn_id']}  [{has_fb}]  {t.get('user_message', '')[:60]}")
            return 1

    tid = turn["turn_id"]
    print(f"{'#'*80}")
    print(f"# ACE run-one: conversation={cid}")
    print(f"#              turn={tid}")
    print(f"#              user_message: {turn.get('user_message', '')[:80]}")
    print(f"#              vote: {(turn.get('feedback') or {}).get('vote')}")
    print(f"{'#'*80}\n")

    # If the conversation file is outside feedback_root, copy it temporarily
    expected_path = Path(feedback_root) / "conversations" / f"{cid}.json"
    temp_copy = False
    if not expected_path.exists():
        expected_path.parent.mkdir(parents=True, exist_ok=True)
        expected_path.write_text(conv_path.read_text(encoding="utf-8"), encoding="utf-8")
        temp_copy = True

    try:
        runner = AceRunner(
            llm=llm,
            playbooks_dir=_resolve_playbooks_dir(),
            feedback_root=feedback_root,
            debug=True,
        )
        result = runner.run_one(cid, tid)

        print(f"\n{'#'*80}")
        print("# FINAL RESULT")
        print(f"{'#'*80}")
        print(json.dumps(result, indent=2, default=str, ensure_ascii=False))
    finally:
        if temp_copy and expected_path.exists():
            expected_path.unlink()


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

    p_show = sub.add_parser("show", help="Print a playbook")
    p_show.add_argument("--skill", required=True,
                        help='"workflow" or a skill name (e.g. Connectivity)')
    p_show.set_defaults(func=cmd_show)

    p_run = sub.add_parser("run-one", help="Process ONE conversation with full debug output")
    p_run.add_argument("conversation_id",
                       help="Conversation ID or path to the conversation JSON file")
    p_run.add_argument("--turn-id", default=None,
                       help="Specific turn_id to process (defaults to first turn with feedback)")
    p_run.add_argument("--model", default=None, help="Override model id")
    p_run.add_argument("--debug", action="store_true", default=True,
                       help="Enable debug prints (default: True for run-one)")
    p_run.set_defaults(func=cmd_run_one)

    p_stats = sub.add_parser("stats", help="Summary of every playbook on disk")
    p_stats.set_defaults(func=cmd_stats)

    args = parser.parse_args(argv)
    return args.func(args) or 0


if __name__ == "__main__":
    sys.exit(main())
