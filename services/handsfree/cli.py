"""Headless Handsfree Replyer runner.

Examples:
    python -m services.handsfree.cli --owner "Charles P Chu" --once
    python -m services.handsfree.cli --owner "Charles P Chu" --interval 300

This starts the application services needed by the analysis pipeline, but it
never starts Flask, Socket.IO, a browser, or the Handsfree UI. New cases are
analyzed into the on-disk review queue. Posting remains a separate explicit
Approve operation from the UI or another trusted caller.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path
from typing import Optional


def _configure_output() -> None:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run Handsfree case detection and analysis without the web UI."
    )
    owners = parser.add_mutually_exclusive_group(required=True)
    owners.add_argument(
        "--owner",
        help="One IPS/Salesforce Owner.Name to monitor.",
    )
    owners.add_argument(
        "--owners-file",
        type=Path,
        help="UTF-8 text file with one Owner.Name per line; blank lines and # comments are ignored.",
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=300,
        help="Seconds between checks in daemon mode (default: 300).",
    )
    parser.add_argument(
        "--max-cases",
        type=int,
        default=None,
        help="Override the maximum number of new cases per check.",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run one check and exit instead of polling forever.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Persist dry_run=true so posting is disabled if an approve call is made.",
    )
    return parser


def _initialize_services() -> None:
    """Initialize the non-Flask services required by HandsfreeRunner."""
    from configs.set_up_app import set_up

    # set_up() initializes paths, credentials, DriverManager, LLM helpers,
    # Wi-Fi/BT agents and skills. Passing None avoids creating a web server;
    # Handsfree analysis itself does not need Socket.IO.
    set_up(None)


def _store():
    from configs.global_configs import app_config
    from pathlib import Path
    from .queue import HandsfreeStore

    return HandsfreeStore(Path(app_config.avatarfiles_dir) / "handsfree")


def _wait_for_run() -> dict:
    from . import orchestrator

    while True:
        state = orchestrator.get_run_state()
        if state.get("status") != "running":
            return state
        time.sleep(1)


def _run_once(owner: str) -> int:
    from . import orchestrator

    result = orchestrator.start_check_now(owner)
    if not result.get("ok"):
        print(f"[handsfree-cli] check could not start: {result.get('error')}")
        return 1
    state = _wait_for_run()
    status = state.get("status")
    print(f"[handsfree-cli] check finished with status={status}")
    if state.get("error"):
        print(f"[handsfree-cli] error: {state['error']}")
    return 0 if status == "done" else 1


def _load_owners(args) -> list[str]:
    if args.owner is not None:
        owners = [args.owner.strip()]
    else:
        try:
            lines = args.owners_file.read_text(encoding="utf-8-sig").splitlines()
        except OSError as e:
            raise ValueError(f"cannot read owners file {args.owners_file}: {e}") from e
        owners = [line.strip() for line in lines
                  if line.strip() and not line.lstrip().startswith("#")]

    unique = []
    seen = set()
    for owner in owners:
        key = owner.casefold()
        if owner and key not in seen:
            unique.append(owner)
            seen.add(key)
    if not unique:
        raise ValueError("owner list is empty")
    return unique


def _run_owners(owners: list[str]) -> int:
    failures = []
    for owner in owners:
        print(f"[handsfree-cli] checking owner {owner!r}")
        if _run_once(owner) != 0:
            failures.append(owner)
    if failures:
        print("[handsfree-cli] failed owner check(s): " + ", ".join(failures))
        return 1
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    _configure_output()
    args = _build_parser().parse_args(argv)
    if args.interval <= 0:
        print("[handsfree-cli] --interval must be greater than zero", file=sys.stderr)
        return 2
    if args.max_cases is not None and not 1 <= args.max_cases <= 15:
        print("[handsfree-cli] --max-cases must be between 1 and 15", file=sys.stderr)
        return 2
    try:
        owners = _load_owners(args)
    except ValueError as e:
        print(f"[handsfree-cli] {e}", file=sys.stderr)
        return 2

    print("[handsfree-cli] initializing analysis services (no web UI)")
    _initialize_services()
    store = _store()
    updates = {}
    if args.owner is not None:
        updates["owner_name"] = owners[0]
    if args.max_cases is not None:
        updates["max_cases_per_run"] = args.max_cases
    if args.dry_run:
        updates["dry_run"] = True
    cfg = store.save_config(updates)
    print(
        f"[handsfree-cli] owners={len(owners)}, "
        f"max_cases={cfg['max_cases_per_run']}, "
        f"dry_run={cfg['dry_run']}"
    )

    if args.once:
        return _run_owners(owners)

    print(f"[handsfree-cli] polling {len(owners)} owner(s) every "
          f"{args.interval:g}s; press Ctrl+C to stop")
    try:
        while True:
            _run_owners(owners)
            time.sleep(args.interval)
    except KeyboardInterrupt:
        print("\n[handsfree-cli] stopped")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
