"""Sync local ACE playbooks + history to a remote SMB share.

Two-mode sync:
  1. Top-level playbook JSONs → mirror (copy changed, delete stale remote).
  2. history/ subtree → additive only (copy new, never delete from remote).

All IO is best-effort: failures log a warning and return without raising.
The ACE run itself must never be blocked by a sync error.
"""

from __future__ import annotations

import os
import shutil
import threading
import time
from pathlib import Path
from typing import Callable, Optional

from utils.helpers import to_long_path

_TAG = "[ace.sync]"

_PROBE_TIMEOUT_SEC = 10.0

_SKIP_NAMES = {".ace_cursor.json", ".ace_nightly.json"}


def _probe_remote(remote_root: str, timeout_sec: float = _PROBE_TIMEOUT_SEC) -> bool:
    """Check if a UNC share is reachable within a wall-clock budget."""
    result = [False]

    def _check():
        try:
            result[0] = Path(remote_root).exists()
        except Exception:
            pass

    t = threading.Thread(target=_check, daemon=True)
    t.start()
    t.join(timeout_sec)
    return (not t.is_alive()) and bool(result[0])


def _needs_copy(local_path: Path, remote_mtime: float) -> bool:
    """Return True if local file is newer than the remote copy (2s tolerance)."""
    try:
        local_mtime = local_path.stat().st_mtime
        return local_mtime > (remote_mtime + 2.0)
    except OSError:
        return False


def _mirror_playbook_jsons(local_dir: Path, remote_dir: Path) -> dict:
    """Mirror top-level *.json files: copy changed, delete stale remote."""
    stats = {"copied": 0, "deleted": 0, "unchanged": 0}

    remote_dir.mkdir(parents=True, exist_ok=True)

    # Build map of existing remote files {name: mtime}.
    remote_files: dict[str, float] = {}
    try:
        with os.scandir(str(remote_dir)) as it:
            for entry in it:
                if entry.is_file() and entry.name.endswith(".json"):
                    try:
                        remote_files[entry.name] = entry.stat().st_mtime
                    except OSError:
                        pass
    except OSError as e:
        print(f"{_TAG} scandir({remote_dir}) failed: {e}")
        return stats

    # Copy local → remote (only if newer or missing).
    local_names: set[str] = set()
    for src in sorted(local_dir.glob("*.json")):
        if src.name in _SKIP_NAMES or src.name.startswith("."):
            continue
        local_names.add(src.name)
        remote_mtime = remote_files.get(src.name)
        if remote_mtime is not None and not _needs_copy(src, remote_mtime):
            stats["unchanged"] += 1
            continue
        try:
            shutil.copy2(str(src), str(remote_dir / src.name))
            stats["copied"] += 1
        except OSError as e:
            print(f"{_TAG} mirror copy failed for {src.name}: {e}")

    # Delete remote files not present locally (mirror delete).
    for name in remote_files:
        if name not in local_names:
            try:
                (remote_dir / name).unlink()
                stats["deleted"] += 1
            except OSError as e:
                print(f"{_TAG} mirror delete failed for {name}: {e}")

    return stats


def _sync_history_additive(local_history: Path, remote_history: Path) -> dict:
    """Additive sync: copy new files from local history tree, never delete."""
    stats = {"copied": 0, "skipped_existing": 0}

    for dirpath, _dirnames, filenames in os.walk(str(local_history)):
        rel_dir = os.path.relpath(dirpath, str(local_history))
        remote_subdir = remote_history / rel_dir if rel_dir != "." else remote_history

        for fname in filenames:
            remote_file = remote_subdir / fname
            if remote_file.exists():
                stats["skipped_existing"] += 1
                continue
            try:
                remote_subdir.mkdir(parents=True, exist_ok=True)
                local_file = Path(dirpath) / fname
                shutil.copy2(str(local_file), str(remote_file))
                stats["copied"] += 1
            except OSError as e:
                print(f"{_TAG} history copy failed for {rel_dir}/{fname}: {e}")

    return stats


def sync_playbooks_to_remote(
    local_dir: Path,
    remote_root_raw: str,
    *,
    emit: Optional[Callable[[str, dict], None]] = None,
    job_id: str = "",
) -> None:
    """Sync local playbooks + history to the remote SMB share. Best-effort."""
    started = time.time()

    if not _probe_remote(remote_root_raw):
        msg = f"remote share unreachable ({remote_root_raw}), skipping sync"
        print(f"{_TAG} {msg}")
        if emit:
            emit("sync_warning", {"reason": "unreachable", "error": msg})
        return

    remote_root = Path(to_long_path(remote_root_raw))
    local_dir = Path(local_dir)

    try:
        remote_root.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        msg = f"cannot create remote root {remote_root_raw}: {e}"
        print(f"{_TAG} {msg}")
        if emit:
            emit("sync_warning", {"reason": "mkdir_failed", "error": msg})
        return

    try:
        mirror_stats = _mirror_playbook_jsons(local_dir, remote_root)
    except Exception as e:
        mirror_stats = {"error": str(e)}
        print(f"{_TAG} mirror phase failed: {e}")

    history_stats: dict = {"copied": 0, "skipped_existing": 0}
    local_history = local_dir / "history"
    if local_history.exists():
        remote_history = remote_root / "history"
        try:
            remote_history.mkdir(parents=True, exist_ok=True)
            history_stats = _sync_history_additive(local_history, remote_history)
        except Exception as e:
            history_stats = {"error": str(e)}
            print(f"{_TAG} history sync failed: {e}")

    elapsed = time.time() - started
    print(f"{_TAG} sync complete in {elapsed:.1f}s — "
          f"mirror: {mirror_stats}, history: {history_stats}")
    if emit:
        emit("sync_done", {
            "mirror": mirror_stats,
            "history": history_stats,
            "elapsed_sec": round(elapsed, 1),
        })


def launch_sync_background(
    local_dir: Path,
    remote_root_raw: str,
    *,
    emit: Optional[Callable[[str, dict], None]] = None,
    job_id: str = "",
) -> None:
    """Fire-and-forget: spawns sync in a daemon thread."""
    t = threading.Thread(
        target=sync_playbooks_to_remote,
        args=(local_dir, remote_root_raw),
        kwargs={"emit": emit, "job_id": job_id},
        daemon=True,
        name=f"ace-sync-{job_id[:8]}",
    )
    t.start()
