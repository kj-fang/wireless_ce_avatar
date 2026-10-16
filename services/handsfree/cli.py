"""Headless Handsfree Replyer runner.

Examples:
    python -m services.handsfree.cli --owner "Charles P Chu" --once
    python -m services.handsfree.cli --owner "Charles P Chu" --interval 300
    python -m services.handsfree.cli --list-view Handsfree_replier_test_case --once
    python -m services.handsfree.cli --sso-login
    python -m services.handsfree.cli --sso-check

This starts the application services needed by the analysis pipeline, but it
never starts Flask, Socket.IO, a browser, or the Handsfree UI. Each owner's
new cases and open cases with new customer comments are analyzed into the
on-disk review queue. Posting remains a separate explicit
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
    targets = parser.add_mutually_exclusive_group(required=True)
    targets.add_argument(
        "--owner",
        help="One IPS/Salesforce Owner.Name to monitor.",
    )
    targets.add_argument(
        "--owners-file",
        type=Path,
        help="UTF-8 text file with one Owner.Name per line; blank lines and # comments are ignored.",
    )
    targets.add_argument(
        "--case-number",
        help="Analyze one explicit case, bypassing owner/date and processed-case checks; requires --once.",
    )
    targets.add_argument(
        "--list-view",
        action="append",
        metavar="DEVELOPER_NAME",
        help="IPS case list view to scan (developer name); repeat for up to 3, their cases are combined.",
    )
    targets.add_argument(
        "--sso-login",
        action="store_true",
        help="Sign in to Salesforce once in a visible Chrome and keep the sign-in for unattended runs.",
    )
    targets.add_argument(
        "--sso-check",
        action="store_true",
        help="Verify the saved sign-in still works unattended (exit 0 = OK, 1 = expired).",
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
        help="Override the maximum number of cases analyzed per owner per round.",
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


def _initialize_sso_only() -> None:
    # Not set_up(): its background prewarm would start its own SSO on the same profile.
    from configs.global_configs import app_config
    from services.driver_manage_service import DriverManager
    from utils import helpers

    avatarfiles_dir, _driver_dir, _prompt_dir = helpers.init_download_dir()
    app_config.set_avatarfiles_dir(avatarfiles_dir)
    if app_config.driver_manager is None:
        app_config.set_driver_manager(DriverManager(avatarfiles_dir))


def _sso_login() -> int:
    from services.case_info_service import CaseService

    _initialize_sso_only()
    print("[handsfree-cli] a Chrome window will open: sign in with your Intel account "
          "(choose 'Stay signed in' if asked). Waiting up to 10 minutes.")
    try:
        CaseService.interactive_sso_login()
    except Exception as e:
        print(f"[handsfree-cli] sign-in failed: {e}", file=sys.stderr)
        return 1
    print("[handsfree-cli] sign-in saved and verified in the background; profile: "
          f"{CaseService._sso_profile_dir()}")
    return 0


def _sso_check() -> int:
    from datetime import datetime
    from services.case_info_service import CaseService

    _initialize_sso_only()
    try:
        CaseService.verify_saved_sign_in()
    except Exception as e:
        print(f"[handsfree-cli] {datetime.now():%Y-%m-%d %H:%M} SSO check FAILED: {e}")
        return 1
    print(f"[handsfree-cli] {datetime.now():%Y-%m-%d %H:%M} SSO check OK "
          "(Salesforce + esft signed in unattended)")
    return 0


def _store():
    from .queue import open_store

    return open_store()


def _wait_for_run() -> dict:
    from . import orchestrator

    while True:
        state = orchestrator.get_run_state()
        if state.get("status") != "running":
            return state
        time.sleep(1)


def _run_once(owner: str = "", list_views: Optional[list[str]] = None) -> int:
    from . import orchestrator

    store = _store()
    max_cases = int(store.load_config().get("max_cases_per_run") or 3)
    summary = orchestrator.run_auto_scan(store, owner, max_cases, trigger="cli",
                                         list_views=list_views)
    if not summary.get("ok"):
        print(f"[handsfree-cli] scan failed: {summary.get('error')}")
        return 1
    errors = [c["case_nbr"] for c in summary["cases"] if c.get("mode") == "error"]
    print(f"[handsfree-cli] scan finished: {len(summary['cases'])} case(s) run"
          + (f", failed: {', '.join(errors)}" if errors else ""))
    return 1 if errors else 0


def _cleanup() -> None:
    """Retention housekeeping after an owner round; never fails the run."""
    from . import orchestrator

    try:
        res = orchestrator.cleanup_queue(_store())
        print(f"[handsfree-cli] cleanup: {res['drafts_removed']} draft(s), "
              f"{res['log_folders_removed']} log folder(s) removed "
              f"(retention {res['retention_days']} days; "
              f"{len(res['closed_cases'])} closed case(s))")
    except Exception as e:
        print(f"[handsfree-cli] cleanup skipped: {type(e).__name__}: {e}")


def _run_case_once(case_nbr: str) -> int:
    from . import orchestrator

    result = orchestrator.start_case_run(case_nbr)
    if not result.get("ok"):
        print(f"[handsfree-cli] case run could not start: {result.get('error')}")
        return 1
    state = _wait_for_run()
    status = state.get("status")
    print(f"[handsfree-cli] case {result['case_nbr']} finished with status={status}")
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


def _load_list_views(args) -> list[str]:
    from .scheduler import MAX_LIST_VIEWS

    views = []
    for name in args.list_view:
        name = name.strip()
        if name and name not in views:
            views.append(name)
    if not views:
        raise ValueError("--list-view is empty")
    if len(views) > MAX_LIST_VIEWS:
        raise ValueError(f"at most {MAX_LIST_VIEWS} --list-view values")
    return views


def _run_owners(owners: list[str]) -> int:
    failures = []
    for owner in owners:
        print(f"[handsfree-cli] checking owner {owner!r}")
        if _run_once(owner) != 0:
            failures.append(owner)
    _cleanup()
    if failures:
        print("[handsfree-cli] failed owner check(s): " + ", ".join(failures))
        return 1
    return 0


def _run_list_views(views: list[str]) -> int:
    print("[handsfree-cli] checking list view(s) " + ", ".join(repr(v) for v in views))
    rc = _run_once(list_views=views)
    _cleanup()
    return rc


def main(argv: Optional[list[str]] = None) -> int:
    _configure_output()
    args = _build_parser().parse_args(argv)
    if args.sso_login:
        return _sso_login()
    if args.sso_check:
        return _sso_check()
    if args.interval <= 0:
        print("[handsfree-cli] --interval must be greater than zero", file=sys.stderr)
        return 2
    if args.max_cases is not None and not 1 <= args.max_cases <= 15:
        print("[handsfree-cli] --max-cases must be between 1 and 15", file=sys.stderr)
        return 2
    if args.case_number is not None and not args.once:
        print("[handsfree-cli] --case-number requires --once", file=sys.stderr)
        return 2
    if args.case_number is not None:
        # The repository's one definition of a typed IPS case number
        # (utils/check_ips_utils): 6-8 digits, zero-padded to eight, leading 0.
        from utils.check_ips_utils import normalise_ips
        case_number = normalise_ips(args.case_number)
        if not case_number:
            print(f"[handsfree-cli] --case-number {args.case_number!r} is not an IPS "
                  "case number (six to eight digits, e.g. 01009610)", file=sys.stderr)
            return 2
        args.case_number = case_number
    try:
        views = _load_list_views(args) if args.list_view is not None else []
        owners = ([] if args.case_number is not None or views
                  else _load_owners(args))
    except ValueError as e:
        print(f"[handsfree-cli] {e}", file=sys.stderr)
        return 2

    print("[handsfree-cli] initializing analysis services (no web UI)")
    _initialize_services()
    from .queue import HandsfreeShareUnavailable
    try:
        store = _store()
    except HandsfreeShareUnavailable as e:
        print(f"[handsfree-cli] {e}", file=sys.stderr)
        return 1
    updates = {}
    if args.owner is not None:
        updates["owner_name"] = owners[0]
    if args.max_cases is not None:
        updates["max_cases_per_run"] = args.max_cases
    if args.dry_run:
        updates["dry_run"] = True
    cfg = store.save_config(updates)
    print(
        (f"[handsfree-cli] case_number={args.case_number!r}, "
         f"max_cases={cfg['max_cases_per_run']}, dry_run={cfg['dry_run']}"
         if args.case_number is not None else
         f"[handsfree-cli] list_views={views}, "
         f"max_cases={cfg['max_cases_per_run']}, dry_run={cfg['dry_run']}"
         if views else
         f"[handsfree-cli] owners={len(owners)}, "
        f"max_cases={cfg['max_cases_per_run']}, "
         f"dry_run={cfg['dry_run']}")
    )

    if args.case_number is not None:
        return _run_case_once(args.case_number)

    run_round = ((lambda: _run_list_views(views)) if views
                 else (lambda: _run_owners(owners)))
    if args.once:
        return run_round()

    print(f"[handsfree-cli] polling "
          + (f"{len(views)} list view(s)" if views else f"{len(owners)} owner(s)")
          + f" for new cases and customer updates every {args.interval:g}s; "
          "press Ctrl+C to stop")
    try:
        while True:
            run_round()
            time.sleep(args.interval)
    except KeyboardInterrupt:
        print("\n[handsfree-cli] stopped")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
