"""
Post-Review corrupted-bullet triage.

After `services.ace.eval.review` produces a `review_*.json` report, this
tool walks every bullet the reviewer flagged as `harmful` + `revert`
(above the confidence gate) and, for each one, offers the user an
interactive CLI choice:

  * If a previous version of the bullet exists in the newest playbook
    snapshot on disk (under `services/ace/eval/snapshots/`), show it and
    ask whether to REVERT the live bullet to that previous version.
  * If no previous version exists, ask whether to REMOVE the corrupted
    bullet from the live playbook or KEEP it as-is.

Only the *live* playbook files (resolved per namespace, same as the CLI)
are ever modified. Snapshots are read-only reference material.

Usage:
    python -m services.ace.eval.corrupted_bullet <path/to/review_*.json>
    python -m services.ace.eval.corrupted_bullet review_20260722T085140+0000.json
    python -m services.ace.eval.corrupted_bullet <review.json> --yes-revert
    python -m services.ace.eval.corrupted_bullet <review.json> --namespace bt
    python -m services.ace.eval.corrupted_bullet <review.json> -y   # revert-if-possible-else-remove, no prompts

When the review report is referenced by bare filename, it is resolved by
searching `services/ace/eval/runs/` recursively and choosing the newest
matching stamp folder.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


# --- config -----------------------------------------------------------------
# Live playbook directory this tool is allowed to modify. Resolved
# dynamically (per machine/user + namespace) via the same helper the CLI
# uses — never a hard-coded path. The shared network copy under
# `\\infs089b...\ace_playbook` is NEVER touched here.
def _resolve_live_dir(namespace: str = "wifi") -> Path:
    """Local live playbook dir for `namespace`.

    Bypasses `path_configs.DOWNLOADS_DIR` (pinned to the trainer server's
    `C:\\Users\\admin`) for the "wifi" namespace by reusing
    `runner._default_playbooks_dir()`, which reads the CURRENT user's
    Downloads folder from the registry. On any dev box this lands in
    `<Downloads>\\IntelAvatar_files\\ace_playbooks\\local\\` — a folder the
    logged-in account actually has write access to.

    For "bt" the eval package doesn't have a dedicated helper, so we fall
    back to `services.ace.cli._resolve_playbooks_dir` — matching the
    pre-existing behaviour.
    """
    if namespace == "wifi":
        from .runner import _default_playbooks_dir
        return _default_playbooks_dir()
    from services.ace.cli import _resolve_playbooks_dir, _ensure_avatarfiles_dir
    _ensure_avatarfiles_dir()
    return _resolve_playbooks_dir(namespace)


# Snapshot root on the shared server — READ-ONLY reference for reverts.
# Layout on disk:
#     \\infs089b...\history\snapshots\<YYYY-MM-DD>\<timestampT...__uuid>\
#         domain_*.json
#         workflow.json
# Wrapped as Path so downstream `.is_dir()` / `relative_to()` calls stay
# consistent instead of mixing str and Path.
SNAPSHOTS_DIR = Path(
    r"\\infs089b.iil.intel.com\HOME\WirelessCE\Intel_WirelessCE_Avatar\history\snapshots"
)

# Reviewer verdict qualifies as "corrupted" only when confidence >= this.
# Matches `review.CONFIDENCE_GATE` so what we act on aligns with what
# triggered the FAIL verdict upstream.
CONFIDENCE_GATE = 0.7

# Default folder to resolve bare review filenames against (matches
# `review.DEFAULT_RUNS_DIR`).
DEFAULT_RUNS_DIR = Path(__file__).resolve().parent / "runs"


def _pick_review_in_stamp_dir(stamp_dir: Path) -> Path | None:
    """Return the newest `review_*.json` inside a stamp folder, if any."""
    matches = [m for m in stamp_dir.glob("review_*.json") if m.is_file()]
    if not matches:
        return None
    matches.sort(key=lambda x: x.stat().st_mtime)
    return matches[-1]


# --- review-report parsing --------------------------------------------------
def _resolve_review_path(review_path: Path) -> Path:
    """
    Locate a review report under the `runs/<stamp>/review_<stamp>.json`
    layout. Accepted inputs (checked in order):

      1. Path to an existing review file.
      2. Path to a stamp folder — returns its newest `review_*.json`.
      3. Bare filename — searched flat under `DEFAULT_RUNS_DIR`, then
         recursively through every stamp folder (files only). Ties broken
         by mtime so the newest wins.
      4. Bare stamp name (e.g. `20260729T075201+0000`) — returns the
         newest `review_*.json` inside `DEFAULT_RUNS_DIR/<stamp>/`.
    """
    p = Path(review_path)

    # (1) Exact file.
    if p.is_file():
        return p.resolve()

    # (2) Stamp folder anywhere.
    if p.is_dir():
        picked = _pick_review_in_stamp_dir(p)
        if picked is not None:
            return picked.resolve()

    # (3) Bare filename at flat layout.
    fallback = DEFAULT_RUNS_DIR / p.name
    if fallback.is_file():
        return fallback.resolve()

    if DEFAULT_RUNS_DIR.is_dir():
        # (3 cont.) Filename inside a stamp folder — drop directory matches.
        hits = [h for h in DEFAULT_RUNS_DIR.rglob(p.name) if h.is_file()]
        if hits:
            hits.sort(key=lambda x: x.stat().st_mtime)
            return hits[-1].resolve()

        # (4) Bare stamp name → look for review_*.json inside the stamp dir.
        stamp_dir = DEFAULT_RUNS_DIR / p.name
        if stamp_dir.is_dir():
            picked = _pick_review_in_stamp_dir(stamp_dir)
            if picked is not None:
                return picked.resolve()

    raise FileNotFoundError(
        f"review report not found: {review_path} "
        f"(also tried {fallback} and stamp-folder lookup under {DEFAULT_RUNS_DIR})"
    )


def _extract_corrupted_ids(report: dict) -> list[str]:
    """
    Deduplicated, order-preserving list of bullet ids that the reviewer
    marked as `harmful` + `revert` with confidence >= CONFIDENCE_GATE.
    Multiple cases can flag the same bullet — we surface it once.
    """
    seen: set[str] = set()
    out: list[str] = []
    for case in (report.get("cases") or []):
        for v in (case.get("reviewed_bullets") or []):
            if not isinstance(v, dict):
                continue
            if v.get("verdict") != "harmful":
                continue
            if v.get("recommended_action") != "revert":
                continue
            try:
                conf = float(v.get("confidence") or 0)
            except (TypeError, ValueError):
                conf = 0.0
            if conf < CONFIDENCE_GATE:
                continue
            bid = v.get("bullet_id")
            if not bid or bid in seen:
                continue
            seen.add(bid)
            out.append(bid)
    return out


# --- live playbook lookup ---------------------------------------------------
def _find_bullet_in_live(
    bullet_id: str,
    live_dir: Path,
) -> tuple[Path, dict, list, int] | None:
    """
    Locate `bullet_id` across every `*.json` in `live_dir`.

    Returns `(playbook_path, bullet_obj, bullets_list, index)` on hit, or
    `None` if the id isn't present in any live playbook file. The list
    and index are returned so callers can splice/replace without re-loading.
    """
    if not live_dir.is_dir():
        return None
    for pb_file in sorted(live_dir.glob("*.json")):
        try:
            data = json.loads(pb_file.read_text(encoding="utf-8"))
        except Exception as e:
            print(f"[corrupted] WARN: cannot parse {pb_file.name}: {e}",
                  file=sys.stderr)
            continue
        bullets = data.get("bullets") if isinstance(data, dict) else None
        if not isinstance(bullets, list):
            continue
        for i, b in enumerate(bullets):
            if isinstance(b, dict) and b.get("id") == bullet_id:
                return pb_file, b, bullets, i
    return None


# --- snapshot resolution ----------------------------------------------------
def _newest_snapshot_dir(snapshots_root: Path) -> Path | None:
    """
    Newest snapshot folder on disk, regardless of timing:
        snapshots/<YYYY-MM-DD>/<timestampT...__uuid>/
    Picks the lexicographically-largest date folder, then the
    lexicographically-largest timestamped subfolder inside it. ISO-8601
    date prefixes make lex order == chronological order.
    """
    if not snapshots_root.is_dir():
        return None
    date_dirs = sorted(
        [p for p in snapshots_root.iterdir() if p.is_dir()],
        key=lambda p: p.name,
    )
    if not date_dirs:
        return None
    newest_date = date_dirs[-1]
    snap_dirs = sorted(
        [p for p in newest_date.iterdir() if p.is_dir()],
        key=lambda p: p.name,
    )
    if not snap_dirs:
        return None
    return snap_dirs[-1]


def _newest_pre_adapt_snapshot_dir(snapshots_root: Path) -> Path | None:
    """
    Newest snapshot folder whose `meta.json` has `source == "pre-adapt"`.

    The unqualified newest snapshot is a post-adapt copy (mirrors live),
    so reverting to it is a no-op. Pre-adapt snapshots are the "state
    right before this run" and are what we want as revert targets.

    Walks date folders newest -> oldest, and timestamped subfolders
    newest -> oldest within each. First `source == "pre-adapt"` wins.
    Returns None if none exist / share is unreachable.
    """
    if not snapshots_root.is_dir():
        return None
    date_dirs = sorted(
        [p for p in snapshots_root.iterdir() if p.is_dir()],
        key=lambda p: p.name,
        reverse=True,
    )
    for date_dir in date_dirs:
        snap_dirs = sorted(
            [p for p in date_dir.iterdir() if p.is_dir()],
            key=lambda p: p.name,
            reverse=True,
        )
        for snap_dir in snap_dirs:
            meta_path = snap_dir / "meta.json"
            if not meta_path.is_file():
                continue
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
            except Exception:
                continue
            if isinstance(meta, dict) and meta.get("source") == "pre-adapt":
                return snap_dir
    return None


def _lookup_previous_bullet(
    bullet_id: str,
    playbook_filename: str,
    snapshot_dir: Path,
) -> dict | None:
    """
    Open `<snapshot_dir>/<playbook_filename>` and return the bullet dict
    whose id matches. Returns None if the file is missing/unreadable or
    the bullet isn't present.
    """
    snap_file = snapshot_dir / playbook_filename
    if not snap_file.is_file():
        return None
    try:
        data = json.loads(snap_file.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"[corrupted] WARN: cannot parse snapshot {snap_file}: {e}",
              file=sys.stderr)
        return None
    for b in (data.get("bullets") or []):
        if isinstance(b, dict) and b.get("id") == bullet_id:
            return b
    return None


# --- live playbook mutation -------------------------------------------------
def _write_playbook(pb_path: Path, data: dict) -> None:
    """Rewrite the live playbook JSON, preserving formatting conventions
    used elsewhere in the repo (indent=2, ensure_ascii=False)."""
    pb_path.write_text(
        json.dumps(data, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def _revert_bullet_in_place(
    pb_path: Path,
    bullet_id: str,
    replacement: dict,
) -> bool:
    """Load the live playbook, replace the matching bullet with
    `replacement`, and write it back. Returns True on success."""
    try:
        data = json.loads(pb_path.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"[corrupted] ERROR: cannot reload {pb_path.name}: {e}",
              file=sys.stderr)
        return False
    bullets = data.get("bullets")
    if not isinstance(bullets, list):
        print(f"[corrupted] ERROR: {pb_path.name} has no bullets array",
              file=sys.stderr)
        return False
    for i, b in enumerate(bullets):
        if isinstance(b, dict) and b.get("id") == bullet_id:
            bullets[i] = replacement
            _write_playbook(pb_path, data)
            return True
    print(f"[corrupted] ERROR: bullet {bullet_id} vanished from "
          f"{pb_path.name} before revert.", file=sys.stderr)
    return False


def _remove_bullet_in_place(pb_path: Path, bullet_id: str) -> bool:
    """Load the live playbook, drop the matching bullet, and write back."""
    try:
        data = json.loads(pb_path.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"[corrupted] ERROR: cannot reload {pb_path.name}: {e}",
              file=sys.stderr)
        return False
    bullets = data.get("bullets")
    if not isinstance(bullets, list):
        print(f"[corrupted] ERROR: {pb_path.name} has no bullets array",
              file=sys.stderr)
        return False
    new_bullets = [b for b in bullets
                   if not (isinstance(b, dict) and b.get("id") == bullet_id)]
    if len(new_bullets) == len(bullets):
        print(f"[corrupted] ERROR: bullet {bullet_id} vanished from "
              f"{pb_path.name} before remove.", file=sys.stderr)
        return False
    data["bullets"] = new_bullets
    _write_playbook(pb_path, data)
    return True


# --- interactive prompts ----------------------------------------------------
def _prompt_yes_no(question: str, default: bool, auto: bool | None) -> bool:
    """Prompt for a y/n answer. `auto` bypasses input() when set."""
    if auto is not None:
        return auto
    suffix = " [Y/n] " if default else " [y/N] "
    try:
        raw = input(question + suffix).strip().lower()
    except EOFError:
        return default
    if not raw:
        return default
    return raw in ("y", "yes")


def _prompt_remove_or_keep(question: str, auto_remove: bool | None) -> bool:
    """Return True to remove, False to keep. Default is keep."""
    if auto_remove is not None:
        return auto_remove
    try:
        raw = input(question + " [r=remove, K=keep] ").strip().lower()
    except EOFError:
        return False
    return raw in ("r", "remove")


def _pretty(obj: dict) -> str:
    return json.dumps(obj, indent=2, ensure_ascii=False)


# --- main flow --------------------------------------------------------------
def process(
    review_path: Path,
    auto_revert: bool | None = None,
    auto_remove: bool | None = None,
    live_dir: Path | None = None,
    namespace: str = "wifi",
) -> dict:
    if live_dir is None:
        live_dir = _resolve_live_dir(namespace)
    review_path = _resolve_review_path(review_path)
    report = json.loads(review_path.read_text(encoding="utf-8"))
    corrupted_ids = _extract_corrupted_ids(report)

    print(f"[corrupted] review    : {review_path.name}")
    print(f"[corrupted] live dir  : {live_dir}")
    if not live_dir.is_dir():
        print(f"[corrupted] ERROR: live playbook dir does not exist. "
              f"Aborting.", file=sys.stderr)
        return {"error": "live_dir_missing"}

    snapshot_dir = _newest_pre_adapt_snapshot_dir(SNAPSHOTS_DIR)
    if snapshot_dir is None:
        # Distinguish "share unreachable" from "share reachable but no
        # pre-adapt snapshot exists" — the difference matters because
        # unreachable means auto-y would aggressively REMOVE every corrupted
        # bullet instead of reverting.
        if not SNAPSHOTS_DIR.is_dir():
            print(f"[corrupted] WARN: snapshot share unreachable: "
                  f"{SNAPSHOTS_DIR} — check VPN. Every corrupted bullet "
                  f"will be treated as 'no previous version' (i.e. removed "
                  f"under --yes / -y).")
        else:
            print(f"[corrupted] WARN: no pre-adapt snapshot found under "
                  f"{SNAPSHOTS_DIR}. All bullets will be treated as "
                  f"'no previous version'.")
    else:
        print(f"[corrupted] snapshot : {snapshot_dir.relative_to(SNAPSHOTS_DIR)}")

    print(f"[corrupted] corrupted : "
          f"{corrupted_ids if corrupted_ids else '(none)'}")
    if not corrupted_ids:
        return {
            "review": review_path.name,
            "snapshot": str(snapshot_dir) if snapshot_dir else None,
            "results": [],
        }

    results: list[dict] = []
    for bid in corrupted_ids:
        print("\n" + "=" * 72)
        print(f"[corrupted] bullet: {bid}")

        hit = _find_bullet_in_live(bid, live_dir)
        if hit is None:
            print(f"  not found in any live playbook under {live_dir}"
                  f" — skipping.")
            results.append({"bullet_id": bid, "action": "skipped_not_in_live"})
            continue
        pb_path, live_bullet, _live_bullets, _idx = hit
        pb_name = pb_path.name
        live_updated = live_bullet.get("updated_at", "")
        print(f"  playbook_file : {pb_name}")
        print(f"  updated_at    : {live_updated}")

        prev_bullet = None
        if snapshot_dir is not None:
            prev_bullet = _lookup_previous_bullet(bid, pb_name, snapshot_dir)

        if prev_bullet is not None:
            print("\n  --- CURRENT (corrupted) ---")
            print(_pretty(live_bullet))
            print("\n  --- PREVIOUS (from snapshot) ---")
            print(_pretty(prev_bullet))
            if _prompt_yes_no(
                f"\n  Revert {bid} in {pb_name} to the previous version?",
                default=True,
                auto=auto_revert,
            ):
                ok = _revert_bullet_in_place(pb_path, bid, prev_bullet)
                results.append({
                    "bullet_id": bid,
                    "playbook_file": pb_name,
                    "action": "reverted" if ok else "revert_failed",
                })
                if ok:
                    print(f"  → reverted {bid} in {pb_name}")
            else:
                results.append({
                    "bullet_id": bid,
                    "playbook_file": pb_name,
                    "action": "kept_corrupted",
                })
                print(f"  → kept corrupted version in {pb_name}")
        else:
            print(f"\n  No previous version of {bid} in snapshot "
                  f"(file: {pb_name}).")
            print("  --- CURRENT (corrupted) ---")
            print(_pretty(live_bullet))
            if _prompt_remove_or_keep(
                f"\n  Remove {bid} from {pb_name}, or keep it?",
                auto_remove=auto_remove,
            ):
                ok = _remove_bullet_in_place(pb_path, bid)
                results.append({
                    "bullet_id": bid,
                    "playbook_file": pb_name,
                    "action": "removed" if ok else "remove_failed",
                })
                if ok:
                    print(f"  → removed {bid} from {pb_name}")
            else:
                results.append({
                    "bullet_id": bid,
                    "playbook_file": pb_name,
                    "action": "kept_no_previous",
                })
                print(f"  → kept corrupted version in {pb_name}")

    # Summary
    print("\n" + "=" * 72)
    tally: dict[str, int] = {}
    for r in results:
        tally[r["action"]] = tally.get(r["action"], 0) + 1
    print(f"[corrupted] done. {len(results)} bullet(s) processed:")
    for action, n in sorted(tally.items()):
        print(f"    {action:<24} {n}")

    return {
        "review": review_path.name,
        "snapshot": str(snapshot_dir) if snapshot_dir else None,
        "results": results,
    }


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="python -m services.ace.eval.corrupted_bullet",
        description=("Interactively triage bullets flagged as harmful+revert "
                     "by services.ace.eval.review. For each such bullet, "
                     "either revert to the newest snapshot's version or "
                     "remove/keep it if no previous version exists."),
    )
    p.add_argument("review", type=Path,
                   help="Path to a review_*.json produced by "
                        "services.ace.eval.review (bare filename is "
                        "resolved against services/ace/eval/runs/).")
    p.add_argument("--namespace", choices=("wifi", "bt"), default="wifi",
                   help="Which live playbook set to triage (default wifi).")
    p.add_argument("-y", "--yes", action="store_true",
                   help="Non-interactive shortcut: for every corrupted "
                        "bullet, revert to the snapshot version if one "
                        "exists, otherwise remove it. Equivalent to "
                        "passing both --yes-revert and --yes-remove.")
    p.add_argument("--yes-revert", action="store_true",
                   help="Non-interactive: revert every bullet that has a "
                        "previous version, without prompting.")
    p.add_argument("--no-revert", action="store_true",
                   help="Non-interactive: keep every corrupted bullet that "
                        "has a previous version (do not revert).")
    p.add_argument("--yes-remove", action="store_true",
                   help="Non-interactive: remove every corrupted bullet "
                        "that has no previous version.")
    p.add_argument("--no-remove", action="store_true",
                   help="Non-interactive: keep every corrupted bullet that "
                        "has no previous version (do not remove).")
    args = p.parse_args(argv)

    # `--yes` is a shorthand for "revert if possible, else remove". Fold it
    # into the existing --yes-revert / --yes-remove flags before validating.
    if args.yes:
        if args.no_revert or args.no_remove:
            p.error("--yes cannot be combined with --no-revert or --no-remove")
        args.yes_revert = True
        args.yes_remove = True

    if args.yes_revert and args.no_revert:
        p.error("--yes-revert and --no-revert are mutually exclusive")
    if args.yes_remove and args.no_remove:
        p.error("--yes-remove and --no-remove are mutually exclusive")

    auto_revert: bool | None = None
    if args.yes_revert:
        auto_revert = True
    elif args.no_revert:
        auto_revert = False

    auto_remove: bool | None = None
    if args.yes_remove:
        auto_remove = True
    elif args.no_remove:
        auto_remove = False

    process(args.review, auto_revert=auto_revert, auto_remove=auto_remove,
            namespace=args.namespace)
    return 0


if __name__ == "__main__":
    sys.exit(main())
