"""
ACE playbook cloud sync.

Default users do NOT run reflection — they only READ the playbooks the agent
injects at generation time. So the playbook follows the same simple cache
rule the skills YAML uses:

    every boot -> pull the latest playbook from the share and use it;
    share unreachable -> fall back to the last version already on disk.

Two independent namespaces are supported — "wifi" (default) and "bt" — each
with its own local working directory and its own share folder. They are kept
separate (not merged into one workflow.json/domain_*.json set) because BT and
WiFi log-analysis styles differ enough that letting one domain's reflected
bullets bleed into the other's playbook would pollute both:

  ace_playbooks/local/       WiFi's working dir — the ONE directory the WiFi
                              AceRunner reads (and, on the rare machine that
                              runs reflection, writes).
  ace_playbooks_bt/local/    BT's equivalent, fully isolated.

Both are refreshed from their respective share on every boot; left untouched
when the share is unreachable.

Share layout (mirrored per namespace, e.g. `\\\\infs089b...\\ace_playbook\\`
for wifi and `...\\ace_playbook_bt\\` for bt):
    workflow.json
    domain_<skill>.json
    history/
        workflow_<timestamp>.json
        domain_<skill>_<timestamp>.json

`sync_at_boot(namespace=...)` is the one function callers need at startup:
    1. migrate a pre-existing flat ace_playbooks*/*.json layout into local/
       (older installs wrote directly into ace_playbooks/ — wifi only).
    2. refresh local/ from the share (best-effort, bounded wait). A share
       file replaces the local copy only when it is newer (by mtime), so an
       unpushed local edit is never clobbered by a staler share copy.

Pushing local/ back to the share is intentionally NOT done here (and not
triggered automatically after every online reflection turn — see
services/ace/pipeline.py). It is a deliberate, job-level action taken by
services/ace/web/server.py's JobManager after a manual adapt run or a
NightlyScheduler batch run, using the richer mirror+history sync in
services/ace/sync.py (`launch_sync_background`) via `push_to_cloud_async()`
below. Keeping push out of the per-turn hot path means an ordinary user's
live chat session never blocks on (or spams) the SMB share; only the
centrally-run adapt job does.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Optional

from configs import path_configs
from utils import helpers

from . import sync as ace_mirror_sync

# Bounded wait for the SMB existence probe — matches helpers.get_load_path's
# own default, kept explicit here so callers can see the budget at a glance.
_PROBE_TIMEOUT_SEC = 8

# namespace -> (local dir name under avatarfiles_dir, share prim/bkup UNCs)
_NAMESPACES = {
    "wifi": {
        "local_dirname": "ace_playbooks",
        "share_prim": path_configs.ACE_PLAYBOOK_DIR_prim,
        "share_bkup": path_configs.ACE_PLAYBOOK_DIR_bkup,
    },
    "bt": {
        "local_dirname": "ace_playbooks_bt",
        "share_prim": path_configs.ACE_PLAYBOOK_BT_DIR_prim,
        "share_bkup": path_configs.ACE_PLAYBOOK_BT_DIR_bkup,
    },
}


def _ns(namespace: str) -> dict:
    cfg = _NAMESPACES.get(namespace)
    if cfg is None:
        raise ValueError(f"Unknown ACE playbook sync namespace: {namespace!r}")
    return cfg


def _playbooks_root(namespace: str = "wifi") -> Path:
    """<avatarfiles_dir>/<local_dirname>, falling back to a repo-relative dir
    before avatarfiles_dir is initialised (mirrors cli.py / web/server.py)."""
    try:
        from configs.global_configs import app_config  # local import: avoid cycles
        base = getattr(app_config, "avatarfiles_dir", None)
    except Exception:
        base = None
    dirname = _ns(namespace)["local_dirname"]
    root = Path(base) / dirname if base else Path.cwd() / "data" / dirname
    root.mkdir(parents=True, exist_ok=True)
    return root


def local_working_dir(namespace: str = "wifi") -> Path:
    """The single directory AceRunner should be pointed at for this namespace."""
    d = _playbooks_root(namespace) / "local"
    d.mkdir(parents=True, exist_ok=True)
    return d


def resolve_cloud_playbook_dir(namespace: str = "wifi") -> Optional[str]:
    """Reachable share folder for this namespace's playbooks, or None
    (unreachable/slow)."""
    cfg = _ns(namespace)
    try:
        return helpers.get_load_path(
            cfg["share_prim"],
            cfg["share_bkup"],
            timeout_sec=_PROBE_TIMEOUT_SEC,
        )
    except Exception:
        return None


def _is_playbook_file(p: Path) -> bool:
    return p.is_file() and p.suffix == ".json" and (
        p.name == "workflow.json" or p.name.startswith("domain_")
    )


# ----- one-time migration from the pre-sync flat layout ---------------------

def migrate_legacy_flat_layout(namespace: str = "wifi") -> int:
    """Older installs wrote workflow.json / domain_*.json / .ace_cursor.json
    directly into ace_playbooks/ (wifi only — bt never had a flat layout).
    Move them into <namespace>/local/ once, so existing machines keep their
    accumulated bullets instead of starting over. No-op once local/ already
    has content or nothing legacy is left. Returns the number of files
    moved."""
    root = _playbooks_root(namespace)
    local_dir = local_working_dir(namespace)
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
            print(f"[ace.sync:{namespace}] failed to migrate {p}: {e}")
    if moved:
        print(f"[ace.sync:{namespace}] migrated {moved} legacy playbook file(s) into {local_dir}")
    return moved


# ----- pull (share -> local/) -----------------------------------------------

def pull_latest_from_cloud(namespace: str = "wifi") -> tuple[int, Optional[str]]:
    """Best-effort refresh of local/ from the share. Each share file replaces
    the local copy only when the share copy is newer (by mtime), so an
    unpushed local edit is never overwritten by a staler share version.
    Returns (files_updated, share_path); share_path is None when the share
    was unreachable within the probe budget — callers should treat that as
    "keep using whatever is already local (the last version)"."""
    share = resolve_cloud_playbook_dir(namespace)
    if not share:
        return (0, None)

    share_dir = Path(share)
    local_dir = local_working_dir(namespace)
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
                print(f"[ace.sync:{namespace}] failed to pull {src.name}: {e}")
    except Exception as e:
        print(f"[ace.sync:{namespace}] failed to list share folder {share_dir}: {e}")
        return (0, None)
    return (updated, share)


# ----- boot entry point ------------------------------------------------------

def sync_at_boot(namespace: str = "wifi") -> dict:
    """Call once at startup (set_up_app.py / cli.py / web/server.py) for each
    namespace in use. Always safe to call even when the share is
    unreachable — every step degrades to a no-op and local/ is left exactly
    as it was (the last version)."""
    migrated = migrate_legacy_flat_layout(namespace) if namespace == "wifi" else 0
    updated, share = pull_latest_from_cloud(namespace)
    status = {
        "namespace": namespace,
        "migrated": migrated,
        "share_reachable": share is not None,
        "updated": updated,
    }
    if share is None:
        print(f"[ace.sync:{namespace}] share unreachable — using last local playbooks as-is")
    else:
        print(f"[ace.sync:{namespace}] boot sync: migrated={migrated} updated={updated} share={share}")
    return status


# ----- push (local/ -> share, job-level: manual adapt run / nightly) --------

def push_to_cloud_async(namespace: str = "wifi", *, emit=None, job_id: str = "") -> None:
    """Fire-and-forget full mirror sync of this namespace's local/ dir (plus
    its local history/ tree, additively) to the share, via
    services/ace/sync.py. Called by services/ace/web/server.py's JobManager
    after a manual adapt run or a NightlyScheduler batch run — NOT from
    pipeline.py, so an ordinary user's live chat session never blocks on (or
    spams) the SMB share. No-op (logged) when the share is unreachable."""
    share = resolve_cloud_playbook_dir(namespace)
    if not share:
        print(f"[ace.sync:{namespace}] push skipped — share unreachable")
        return
    ace_mirror_sync.launch_sync_background(
        local_dir=local_working_dir(namespace),
        remote_root_raw=share,
        emit=emit,
        job_id=job_id,
    )
