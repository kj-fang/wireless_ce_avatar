"""Sync local ACE playbooks + history to a remote SMB share.

Two-mode sync:
  1. Top-level playbook JSONs → mirror (copy changed, delete stale remote).
  2. history/ subtree → additive copy, then prune remote entries older than
     `retention_days` (default 30) so the share doesn't grow forever.

All IO is best-effort: failures log a warning and return without raising.
The ACE run itself must never be blocked by a sync error.
"""

from __future__ import annotations
 
import os
import shutil
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, Optional

from utils.helpers import to_long_path

_TAG = "[ace.sync]"

_PROBE_TIMEOUT_SEC = 10.0

_SKIP_NAMES = {".ace_cursor.json", ".ace_nightly.json"}

_DEFAULT_RETENTION_DAYS = 30


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


def _prune_remote_history_by_age(remote_history: Path, retention_days: int) -> dict:
    """Delete remote history entries older than `retention_days`.

    Same rules as HistoryWriter.prune(): decide by the DATE encoded in the
    filename / dir-name, not by mtime (which additive copies can bump).

      remote_history/turns/YYYY-MM-DD.jsonl   -> unlink
      remote_history/snapshots/YYYY-MM-DD/    -> rmtree (whole day dir)
    """
    stats = {"turns_removed": 0, "snapshot_dirs_removed": 0}
    if retention_days <= 0 or not remote_history.exists():
        return stats
    cutoff = datetime.now() - timedelta(days=retention_days)

    turns_dir = remote_history / "turns"
    if turns_dir.exists():
        try:
            for f in turns_dir.glob("*.jsonl"):
                try:
                    d = datetime.strptime(f.stem, "%Y-%m-%d")
                except ValueError:
                    continue
                if d < cutoff:
                    try:
                        f.unlink()
                        stats["turns_removed"] += 1
                    except OSError as e:
                        print(f"{_TAG} prune: failed to remove remote {f}: {e}")
        except OSError as e:
            print(f"{_TAG} prune (remote turns) failed: {e}")

    snaps_dir = remote_history / "snapshots"
    if snaps_dir.exists():
        try:
            for d in snaps_dir.iterdir():
                if not d.is_dir():
                    continue
                try:
                    day = datetime.strptime(d.name, "%Y-%m-%d")
                except ValueError:
                    continue
                if day < cutoff:
                    try:
                        shutil.rmtree(str(d))
                        stats["snapshot_dirs_removed"] += 1
                    except OSError as e:
                        print(f"{_TAG} prune: failed to rmtree remote {d}: {e}")
        except OSError as e:
            print(f"{_TAG} prune (remote snapshots) failed: {e}")

    return stats


def sync_playbooks_to_remote(
    local_dir: Path,
    remote_root_raw: str,
    *,
    emit: Optional[Callable[[str, dict], None]] = None,
    job_id: str = "",
    retention_days: int = _DEFAULT_RETENTION_DAYS,
) -> None:
    """Sync local playbooks + history to the remote SMB share. Best-effort.

    After the additive history copy, any remote history entry whose encoded
    date is older than `retention_days` is deleted from the share.
    """
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
    prune_stats: dict = {"turns_removed": 0, "snapshot_dirs_removed": 0}
    local_history = local_dir / "history"
    remote_history = remote_root / "history"
    if local_history.exists():
        try:
            remote_history.mkdir(parents=True, exist_ok=True)
            history_stats = _sync_history_additive(local_history, remote_history)
        except Exception as e:
            history_stats = {"error": str(e)}
            print(f"{_TAG} history sync failed: {e}")

    # Prune old remote history even if local/history is empty — the share may
    # still have day-dirs from previous pushes that should now be expired.
    try:
        prune_stats = _prune_remote_history_by_age(remote_history, retention_days)
    except Exception as e:
        prune_stats = {"error": str(e)}
        print(f"{_TAG} history prune failed: {e}")

    elapsed = time.time() - started
    print(f"{_TAG} sync complete in {elapsed:.1f}s — "
          f"mirror: {mirror_stats}, history: {history_stats}, prune: {prune_stats}")
    if emit:
        emit("sync_done", {
            "mirror": mirror_stats,
            "history": history_stats,
            "history_prune": prune_stats,
            "retention_days": retention_days,
            "elapsed_sec": round(elapsed, 1),
        })


def launch_sync_background(
    local_dir: Path,
    remote_root_raw: str,
    *,
    emit: Optional[Callable[[str, dict], None]] = None,
    job_id: str = "",
    retention_days: int = _DEFAULT_RETENTION_DAYS,
) -> None:
    """Fire-and-forget: spawns sync in a daemon thread."""
    t = threading.Thread(
        target=sync_playbooks_to_remote,
        args=(local_dir, remote_root_raw),
        kwargs={"emit": emit, "job_id": job_id, "retention_days": retention_days},
        daemon=True,
        name=f"ace-sync-{job_id[:8]}",
    )
    t.start()
