"""
Sync the golden-set cases folder from the shared SMB server down to a
local cache, so evaluation runs can hit the local copy (fast) instead of
streaming every case JSON + log file over the network (slow).

Behaviour:
  * Local dir is created if missing.
  * Server reachability is probed with a short wall-clock budget; if the
    share is unreachable we log a warning and reuse whatever is already
    in the local cache.
  * We walk the server tree recursively. Each file is copied to the
    matching local path when:
        - the local file is missing, OR
        - the server mtime is newer than the local mtime (2s tolerance).
  * Local-only files are NEVER deleted — this lets a user keep private
    test cases on disk without them being wiped by a sync.
  * All errors are logged and swallowed. A stale local cache is always
    preferable to blowing up the eval run.
"""

from __future__ import annotations

import os
import shutil
import threading
import time
from pathlib import Path
from typing import Optional

from utils.helpers import to_long_path

_TAG = "[eval.golden_sync]"
_PROBE_TIMEOUT_SEC = 10.0
_MTIME_TOLERANCE_SEC = 2.0


def _probe_remote(remote_root: str, timeout_sec: float = _PROBE_TIMEOUT_SEC) -> bool:
    """Return True if the UNC share responds within the wall-clock budget."""
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


def _needs_download(server_file: Path, local_file: Path) -> bool:
    """True if local copy is missing or older than the server copy."""
    if not local_file.exists():
        return True
    try:
        server_mtime = server_file.stat().st_mtime
        local_mtime = local_file.stat().st_mtime
    except OSError:
        # If we can't stat either side, err on the side of re-copying.
        return True
    return server_mtime > (local_mtime + _MTIME_TOLERANCE_SEC)


def _ensure_local_dir(local_path: Path) -> bool:
    """Create the local golden-set folder (and its parents) if missing.

    Returns True on success, False if mkdir raised. Logs a one-shot
    "created" message the first time the folder actually gets made so it
    is obvious in eval output that the cache was bootstrapped.
    """
    if local_path.exists():
        return True
    try:
        local_path.mkdir(parents=True, exist_ok=True)
        print(f"{_TAG} created local golden-set folder: {local_path}")
        return True
    except OSError as e:
        print(f"{_TAG} cannot create local dir {local_path}: {e}")
        return False


def sync_golden_set(
    server_dir: Path | str,
    local_dir: Path | str,
    *,
    verbose: bool = True,
) -> Path:
    """Mirror server → local for the golden-set folder.

    Returns the local directory as a Path. The returned path is always the
    caller's ``local_dir`` even if the server was unreachable or the sync
    hit errors — downstream code should then work off whatever cache is
    already on disk.
    """
    server_dir_raw = str(server_dir)
    local_path = Path(local_dir)

    # Always guarantee the local dir (and its parents, e.g.
    # C:\Users\admin\Downloads\IntelAvatar_files) exist before we touch
    # anything network-bound.
    if not _ensure_local_dir(local_path):
        return local_path

    started = time.time()

    if not _probe_remote(server_dir_raw):
        print(f"{_TAG} server unreachable ({server_dir_raw}); "
              f"using existing local cache at {local_path}")
        return local_path

    server_root = Path(to_long_path(server_dir_raw))
    if not server_root.exists():
        print(f"{_TAG} server path not found after probe: {server_dir_raw}")
        return local_path

    stats = {
        "copied_new": 0,
        "copied_updated": 0,
        "unchanged": 0,
        "errors": 0,
        "files_seen": 0,
    }

    for dirpath, _dirnames, filenames in os.walk(str(server_root)):
        rel_dir = os.path.relpath(dirpath, str(server_root))
        local_subdir = local_path if rel_dir == "." else local_path / rel_dir

        for fname in filenames:
            stats["files_seen"] += 1
            server_file = Path(dirpath) / fname
            local_file = local_subdir / fname

            local_existed = local_file.exists()
            try:
                if not _needs_download(server_file, local_file):
                    stats["unchanged"] += 1
                    continue
                local_subdir.mkdir(parents=True, exist_ok=True)
                shutil.copy2(str(server_file), str(local_file))
                if local_existed:
                    stats["copied_updated"] += 1
                else:
                    stats["copied_new"] += 1
            except OSError as e:
                stats["errors"] += 1
                print(f"{_TAG} copy failed for {rel_dir}/{fname}: {e}")

    elapsed = time.time() - started
    if verbose:
        print(f"{_TAG} sync done in {elapsed:.1f}s from {server_dir_raw} "
              f"→ {local_path}")
        print(f"{_TAG}   new={stats['copied_new']} "
              f"updated={stats['copied_updated']} "
              f"unchanged={stats['unchanged']} "
              f"errors={stats['errors']} "
              f"(seen {stats['files_seen']} file(s))")
    return local_path


def resolve_cases_dir(
    server_dir: Path | str,
    local_dir: Path | str,
    *,
    do_sync: bool = True,
    verbose: bool = True,
) -> Path:
    """Convenience wrapper: optionally sync, then return the local dir."""
    local_path = Path(local_dir)
    if do_sync:
        return sync_golden_set(server_dir, local_path, verbose=verbose)
    _ensure_local_dir(local_path)
    return local_path


__all__ = ["sync_golden_set", "resolve_cases_dir", "_ensure_local_dir"]
