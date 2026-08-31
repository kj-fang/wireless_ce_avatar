"""
Linux WiFi Skills YAML lifecycle helpers.

Parallel to `utils.bt_skills_yaml_utils` — same on-disk layout, same
lookup semantics, but every filename uses the ``linux_skills_`` prefix so
the Linux WiFi domain coexists with BT and Wi-Fi in the same
``skills_config/cloud/`` and ``skills_config/user/`` sub-folders without
colliding.

Wi-Fi files:   ``skills_YYYY-MM-DD.yaml``       / legacy ``skills.yaml``
BT files:      ``bt_skills_YYYY-MM-DD.yaml``    / legacy ``bt_skills.yaml``
Linux files:   ``linux_skills_YYYY-MM-DD.yaml`` / legacy ``linux_skills.yaml``

The "active source" flag is independent of both the Wi-Fi and BT ones.
"""

from __future__ import annotations

import re
import threading
from datetime import date, datetime
from pathlib import Path
from typing import Optional, Tuple

from configs.path_configs import (
    SKILLS_CONFIG_DIR_prim,
    SKILLS_CONFIG_DIR_bkup,
    LINUX_SKILLS_YAML_FILENAME,
    LINUX_SKILLS_YAML_DATED_RE,
    LINUX_SKILLS_YAML_DATED_TEMPLATE,
)
from utils import helpers
from utils.skills_yaml_utils import (
    local_cloud_baseline_dir,
    local_user_overrides_dir,
)


_DATED_RE = re.compile(LINUX_SKILLS_YAML_DATED_RE)


# --- Active source flag --------------------------------------------------

_ACTIVE_SOURCE_CLOUD = "cloud"
_ACTIVE_SOURCE_USER = "user"
_active_source: str = _ACTIVE_SOURCE_CLOUD
_active_lock = threading.Lock()


def get_active_source() -> str:
    """Return the current Linux active source, one of {"cloud", "user"}."""
    return _active_source


def set_active_source(source: str) -> str:
    """Set the Linux active source. Invalid values are normalised to "cloud"."""
    global _active_source
    s = source if source in (_ACTIVE_SOURCE_CLOUD, _ACTIVE_SOURCE_USER) else _ACTIVE_SOURCE_CLOUD
    with _active_lock:
        _active_source = s
    return _active_source


# --- Filename helpers ----------------------------------------------------

def parse_dated_filename(filename: str) -> Optional[date]:
    """Return the ISO date encoded in `linux_skills_YYYY-MM-DD.yaml`, else None."""
    m = _DATED_RE.match(filename)
    if not m:
        return None
    try:
        return datetime.strptime(m.group(1), "%Y-%m-%d").date()
    except ValueError:
        return None


def find_latest_dated_yaml(directory: str | Path) -> Tuple[Optional[Path], Optional[date]]:
    """
    Scan `directory` for files matching `linux_skills_YYYY-MM-DD.yaml` and
    return `(path, date)` for the newest. Returns `(None, None)` when the
    directory is unreachable or contains no matching files.
    """
    if not directory:
        return (None, None)
    try:
        d = Path(directory)
        if not d.exists() or not d.is_dir():
            return (None, None)
    except OSError:
        return (None, None)

    newest_path: Optional[Path] = None
    newest_date: Optional[date] = None
    try:
        for entry in d.iterdir():
            if not entry.is_file():
                continue
            parsed = parse_dated_filename(entry.name)
            if parsed is None:
                continue
            if newest_date is None or parsed > newest_date:
                newest_date = parsed
                newest_path = entry
    except OSError:
        return (None, None)

    return (newest_path, newest_date)


def today_dated_filename(d: Optional[date] = None) -> str:
    """Canonical Linux filename for a YAML produced today (or `d` if provided)."""
    return LINUX_SKILLS_YAML_DATED_TEMPLATE.format(date=(d or date.today()).isoformat())


# --- Directory layout ----------------------------------------------------
# Cloud and user dirs are SHARED with the WiFi and BT sides — only filename
# prefix differs. Imported from utils.skills_yaml_utils above.

def resolve_cloud_skills_dir() -> Optional[str]:
    """Return the reachable share-folder skills_config dir, or None."""
    try:
        path = helpers.get_load_path(SKILLS_CONFIG_DIR_prim, SKILLS_CONFIG_DIR_bkup)
        return path or None
    except Exception:
        return None


# --- Lookup helpers ------------------------------------------------------

def find_latest_cloud_baseline_yaml() -> Tuple[Optional[Path], Optional[date]]:
    """Latest dated Linux YAML in the local `cloud/` mirror."""
    return find_latest_dated_yaml(local_cloud_baseline_dir())


def find_latest_user_yaml() -> Tuple[Optional[Path], Optional[date]]:
    """Latest dated Linux YAML in the local `user/` overrides dir."""
    return find_latest_dated_yaml(local_user_overrides_dir())


def find_latest_share_yaml() -> Tuple[Optional[Path], Optional[date]]:
    """
    Latest Linux YAML on the SHARE FOLDER. Falls back to the legacy un-dated
    `linux_skills.yaml` when no dated file exists yet. Returns (None, None)
    when the share is unreachable or contains no Linux YAML at all.
    """
    cloud_dir = resolve_cloud_skills_dir()
    if not cloud_dir:
        return (None, None)
    path, dt = find_latest_dated_yaml(cloud_dir)
    if path is not None:
        return (path, dt)
    legacy = Path(cloud_dir) / LINUX_SKILLS_YAML_FILENAME
    if legacy.exists():
        return (legacy, None)
    return (None, None)


# --- Active YAML resolver ------------------------------------------------

def current_active_yaml() -> Tuple[Optional[Path], Optional[date], str]:
    """
    Return `(path, date, effective_source)` of the Linux YAML that should be
    loaded right now.
    """
    if get_active_source() == _ACTIVE_SOURCE_USER:
        u_path, u_date = find_latest_user_yaml()
        if u_path is not None:
            return (u_path, u_date, _ACTIVE_SOURCE_USER)
    c_path, c_date = find_latest_cloud_baseline_yaml()
    if c_path is not None:
        return (c_path, c_date, _ACTIVE_SOURCE_CLOUD)
    legacy = local_cloud_baseline_dir().parent / LINUX_SKILLS_YAML_FILENAME
    if legacy.exists():
        return (legacy, None, _ACTIVE_SOURCE_CLOUD)
    return (None, None, _ACTIVE_SOURCE_CLOUD)


# --- Cloud baseline refresh ---------------------------------------------

def refresh_local_cloud_baseline() -> Tuple[Optional[Path], Optional[date]]:
    """
    Pull the latest Linux YAML from the share folder into the local `cloud/`
    dir. Best-effort: returns (None, None) when off-VPN.
    """
    share_path, share_date = find_latest_share_yaml()
    if share_path is None:
        return (None, None)

    import shutil as _shutil
    target_dir = local_cloud_baseline_dir()
    target_name = share_path.name if share_date is not None else today_dated_filename()
    target = target_dir / target_name

    try:
        _shutil.copy2(str(share_path), str(target))
    except Exception as e:
        print(f"[linux_skills_yaml] refresh_local_cloud_baseline failed: {e}")
        return (None, None)

    # Prune older Linux mirror files; never touch WiFi or BT skill files.
    try:
        for entry in target_dir.iterdir():
            if entry.is_file() and entry.name != target.name \
                    and entry.name.startswith("linux_skills_") and entry.suffix == ".yaml":
                entry.unlink()
    except OSError:
        pass

    return (target, share_date if share_date is not None else date.today())


# --- Status summary ------------------------------------------------------

def skills_yaml_status() -> dict:
    """Build the status payload for the Linux chatbot skill panel."""
    def _summary(path: Optional[Path], dt: Optional[date]) -> dict:
        return {
            "path": str(path) if path else None,
            "date": dt.isoformat() if dt else None,
            "filename": path.name if path else None,
        }

    c_path, c_date = find_latest_cloud_baseline_yaml()
    u_path, u_date = find_latest_user_yaml()
    s_path, s_date = find_latest_share_yaml()

    _, _, effective = current_active_yaml()

    return {
        "active_source":    get_active_source(),
        "effective_source": effective,
        "cloud_local":      _summary(c_path, c_date),
        "user_local":       _summary(u_path, u_date),
        "share_remote":     {**_summary(s_path, s_date), "reachable": s_path is not None},
    }
