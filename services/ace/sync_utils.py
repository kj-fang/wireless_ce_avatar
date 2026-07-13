"""
ACE playbook cloud sync.

Default users do NOT run reflection — they only READ the playbooks the agent
injects at generation time. So the playbook follows the same simple cache
rule the skills YAML uses:

    every boot -> pull the latest playbook from the share and use it;
    share unreachable -> fall back to the last version already on disk.

  ace_playbooks/
    local/   the ONE directory every AceRunner reads (and, on the rare
             machine that runs reflection, writes). Refreshed from the share
             on every boot; left untouched when the share is unreachable.

Share layout (`\\\\infs089b...\\ace_playbook\\`):
    workflow.json
    domain_<skill>.json
    history/
        workflow_<timestamp>.json
        domain_<skill>_<timestamp>.json

`sync_at_boot()` is the one function callers need at startup:
    1. migrate a pre-existing flat ace_playbooks/*.json layout into local/
       (older installs wrote directly into ace_playbooks/).
    2. refresh local/ from the share (best-effort, bounded wait). A share
       file replaces the local copy only when it is newer (by mtime), so an
       unpushed local edit is never clobbered by a staler share copy.

`push_local_to_cloud_async()` is called after every reflection save
(services/ace/pipeline.py) to best-effort mirror the fresh local/ files back
to the share, archiving the previous share copy under history/ first. It
runs in a daemon thread so a slow/unreachable share never blocks the chat
response or the CLI/batch run. Default users never trigger this (they don't
reflect); it's here for the admin/cron machine that does.
"""

from __future__ import annotations

import shutil
import threading
from datetime import datetime
from pathlib import Path
from typing import Iterable, Optional

from configs import path_configs
from utils import helpers

# Bounded wait for the SMB existence probe — matches helpers.get_load_path's
# own default, kept explicit here so callers can see the budget at a glance.
_PROBE_TIMEOUT_SEC = 8


def _playbooks_root() -> Path:
    """<avatarfiles_dir>/ace_playbooks, falling back to a repo-relative dir
    before avatarfiles_dir is initialised (mirrors cli.py / web/server.py)."""
    try:
        from configs.global_configs import app_config  # local import: avoid cycles
        base = getattr(app_config, "avatarfiles_dir", None)
    except Exception:
        base = None
    root = Path(base) / "ace_playbooks" if base else Path.cwd() / "data" / "ace_playbooks"
    root.mkdir(parents=True, exist_ok=True)
    return root


def local_working_dir() -> Path:
    """The single directory AceRunner should be pointed at."""
    d = _playbooks_root() / "local"
    d.mkdir(parents=True, exist_ok=True)
    return d


def resolve_cloud_playbook_dir() -> Optional[str]:
    """Reachable share folder for the playbooks, or None (unreachable/slow)."""
    try:
        return helpers.get_load_path(
            path_configs.ACE_PLAYBOOK_DIR_prim,
            path_configs.ACE_PLAYBOOK_DIR_bkup,
            timeout_sec=_PROBE_TIMEOUT_SEC,
        )
    except Exception:
        return None


def _is_playbook_file(p: Path) -> bool:
    return p.is_file() and p.suffix == ".json" and (
        p.name == "workflow.json" or p.name.startswith("domain_")
    )


# ----- one-time migration from the pre-sync flat layout ---------------------

def migrate_legacy_flat_layout() -> int:
    """Older installs wrote workflow.json / domain_*.json / .ace_cursor.json
    directly into ace_playbooks/. Move them into ace_playbooks/local/ once,
    so existing machines keep their accumulated bullets instead of starting
    over. No-op once local/ already has content or nothing legacy is left.
    Returns the number of files moved."""
    root = _playbooks_root()
    local_dir = local_working_dir()
    if any(_is_playbook_file(p) for p in local_dir.glob("*.json")):
        return 0  # local/ already populated — nothing to migrate

    moved = 0
    # NOTE: pathlib's "*.json" already matches dotfiles like .ace_cursor.json
    # (unlike a shell glob), so a single pass here is enough — no separate
    # explicit ".ace_cursor.json" glob, which would double-list it and log a
    # spurious "already moved" error on the second (duplicate) attempt.
    for p in root.glob("*.json"):
        if p.parent != root or not p.is_file():
            continue
        try:
            shutil.move(str(p), str(local_dir / p.name))
            moved += 1
        except Exception as e:
            print(f"[ace.sync] failed to migrate {p}: {e}")
    if moved:
        print(f"[ace.sync] migrated {moved} legacy playbook file(s) into {local_dir}")
    return moved


# ----- pull (share -> local/) -----------------------------------------------

def pull_latest_from_cloud() -> tuple[int, Optional[str]]:
    """Best-effort refresh of local/ from the share. Each share file replaces
    the local copy only when the share copy is newer (by mtime), so an
    unpushed local edit is never overwritten by a staler share version.
    Returns (files_updated, share_path); share_path is None when the share
    was unreachable within the probe budget — callers should treat that as
    "keep using whatever is already local (the last version)"."""
    share = resolve_cloud_playbook_dir()
    if not share:
        return (0, None)

    share_dir = Path(share)
    local_dir = local_working_dir()
    updated = 0
    try:
        for src in share_dir.glob("*.json"):
            if not _is_playbook_file(src):
                continue
            dst = local_dir / src.name
            try:
                if dst.exists() and dst.stat().st_mtime >= src.stat().st_mtime:
                    continue  # local is same-or-newer — keep it
                shutil.copy2(str(src), str(dst))
                updated += 1
            except Exception as e:
                print(f"[ace.sync] failed to pull {src.name}: {e}")
    except Exception as e:
        print(f"[ace.sync] failed to list share folder {share_dir}: {e}")
        return (0, None)
    return (updated, share)


# ----- boot entry point ------------------------------------------------------

def sync_at_boot() -> dict:
    """Call once at startup (set_up_app.py / cli.py / web/server.py). Always
    safe to call even when the share is unreachable — every step degrades to
    a no-op and local/ is left exactly as it was (the last version)."""
    migrated = migrate_legacy_flat_layout()
    updated, share = pull_latest_from_cloud()
    status = {
        "migrated": migrated,
        "share_reachable": share is not None,
        "updated": updated,
    }
    if share is None:
        print("[ace.sync] share unreachable — using last local playbooks as-is")
    else:
        print(f"[ace.sync] boot sync: migrated={migrated} updated={updated} share={share}")
    return status


# ----- push (local/ -> share, after every reflection save) ------------------

def push_local_to_cloud(paths: Iterable[Path]) -> dict:
    """Best-effort push the given local playbook files to the share,
    archiving the previous share copy under history/ first. Synchronous —
    callers that don't want to block should use push_local_to_cloud_async.
    """
    share = resolve_cloud_playbook_dir()
    if not share:
        return {"share_reachable": False, "pushed": 0}

    share_dir = Path(share)
    history_dir = share_dir / "history"
    ts = datetime.now().strftime("%Y%m%dT%H%M%S")
    pushed = 0
    for src in paths:
        src = Path(src)
        if not src.exists() or not _is_playbook_file(src):
            continue
        dst = share_dir / src.name
        try:
            if dst.exists():
                history_dir.mkdir(parents=True, exist_ok=True)
                archived = history_dir / f"{dst.stem}_{ts}{dst.suffix}"
                shutil.copy2(str(dst), str(archived))
            shutil.copy2(str(src), str(dst))
            pushed += 1
        except Exception as e:
            print(f"[ace.sync] failed to push {src.name}: {e}")
    return {"share_reachable": True, "pushed": pushed}


def push_local_to_cloud_async(paths: Iterable[Path]) -> None:
    """Fire-and-forget push in a daemon thread so a slow/unreachable share
    never blocks the caller (chat response, CLI batch run, web adapt job)."""
    paths = list(paths)

    def _run():
        try:
            push_local_to_cloud(paths)
        except Exception as e:
            print(f"[ace.sync] async push failed: {e}")

    threading.Thread(target=_run, daemon=True).start()
