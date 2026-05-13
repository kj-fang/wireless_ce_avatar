"""
Skills YAML lifecycle helpers.

The local skill cache is split into two sibling sub-folders:

  <IntelAvatar_files>/skills_config/
  ├── cloud/  → mirror of the latest dated YAML on the share folder.
  │            Refreshed on every app startup. Treat this as read-only —
  │            user edits never land here.
  └── user/   → the user's own dated edits (skills_YYYY-MM-DD.yaml).
               Persists across app restarts; uploads to the share folder
               only happen when the user explicitly clicks "Upload".

The "active source" flag (process-wide, resets on app restart) decides
which folder feeds the running agent. Default after every restart is
"cloud" — so the agent always boots on the latest share-folder version,
even if a user override exists on disk.

Filenames carry an ISO date suffix (skills_YYYY-MM-DD.yaml) so multiple
revisions can coexist.

Kept dependency-free so it can be imported from configs/, blueprints/,
and services/ without circular-import risk.
"""

from __future__ import annotations

import re
import threading
from datetime import date, datetime
from pathlib import Path
from typing import Optional, Tuple

from configs.path_configs import (
    LOCAL_SKILLS_DIR_NAME,
    SKILLS_CONFIG_DIR_prim,
    SKILLS_CONFIG_DIR_bkup,
    SKILLS_YAML_DATED_RE,
    SKILLS_YAML_DATED_TEMPLATE,
    SKILLS_YAML_FILENAME,
    LOCAL_SKILLS_YAML,
)
from utils import helpers


_DATED_RE = re.compile(SKILLS_YAML_DATED_RE)


# --- Active source flag --------------------------------------------------
#
# Process-wide; resets to "cloud" on every app restart. The user can flip
# this via the side-panel toggle (cloud ⇄ user) or implicitly by saving an
# edit in the skill editor.

_ACTIVE_SOURCE_CLOUD = "cloud"
_ACTIVE_SOURCE_USER = "user"
_active_source: str = _ACTIVE_SOURCE_CLOUD
_active_lock = threading.Lock()


def get_active_source() -> str:
    """Return the current active source, one of {"cloud", "user"}."""
    return _active_source


def set_active_source(source: str) -> str:
    """Set the active source. Invalid values are normalised to "cloud"."""
    global _active_source
    s = source if source in (_ACTIVE_SOURCE_CLOUD, _ACTIVE_SOURCE_USER) else _ACTIVE_SOURCE_CLOUD
    with _active_lock:
        _active_source = s
    return _active_source


# --- Filename helpers ----------------------------------------------------

def parse_dated_filename(filename: str) -> Optional[date]:
    """Return the ISO date encoded in `skills_YYYY-MM-DD.yaml`, else None."""
    m = _DATED_RE.match(filename)
    if not m:
        return None
    try:
        return datetime.strptime(m.group(1), "%Y-%m-%d").date()
    except ValueError:
        return None


def find_latest_dated_yaml(directory: str | Path) -> Tuple[Optional[Path], Optional[date]]:
    """
    Scan `directory` for files matching `skills_YYYY-MM-DD.yaml` and return
    `(path, date)` for the newest. Returns `(None, None)` if the directory is
    unreachable or contains no dated files.
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
    """Canonical filename for a YAML produced today (or `d` if provided)."""
    return SKILLS_YAML_DATED_TEMPLATE.format(date=(d or date.today()).isoformat())


# --- Directory layout ----------------------------------------------------

def resolve_cloud_skills_dir() -> Optional[str]:
    """Return the reachable cloud (share folder) skills_config dir, or None."""
    try:
        path = helpers.get_load_path(SKILLS_CONFIG_DIR_prim, SKILLS_CONFIG_DIR_bkup)
        return path or None
    except Exception:
        return None


def _skills_config_root() -> Path:
    """
    Local cache root that holds the `cloud/` and `user/` sub-folders.

    Prefers `<avatarfiles_dir>/skills_config/` so users can find their skill
    files alongside the rest of the case-number downloads they already work
    with. Falls back to the repo-relative `data/` location when
    avatarfiles_dir has not been initialised yet (e.g. very early imports).
    """
    try:
        from configs.global_configs import app_config  # local import: avoid cycles
        base = getattr(app_config, "avatarfiles_dir", None)
        if base:
            d = Path(base) / LOCAL_SKILLS_DIR_NAME
            d.mkdir(parents=True, exist_ok=True)
            return d
    except Exception:
        pass
    return Path(LOCAL_SKILLS_YAML).parent


def local_cloud_baseline_dir() -> Path:
    """Sub-folder that mirrors the share folder's latest dated YAML."""
    d = _skills_config_root() / "cloud"
    d.mkdir(parents=True, exist_ok=True)
    return d


def local_user_overrides_dir() -> Path:
    """Sub-folder that holds the user's own dated edits."""
    d = _skills_config_root() / "user"
    d.mkdir(parents=True, exist_ok=True)
    return d


# Back-compat alias — older code paths import this name and just want
# *some* writable local skills directory. Keep returning the root.
def local_skills_dir() -> Path:
    return _skills_config_root()


# --- Lookup helpers ------------------------------------------------------

def find_latest_cloud_baseline_yaml() -> Tuple[Optional[Path], Optional[date]]:
    """Latest dated YAML in the local `cloud/` mirror."""
    return find_latest_dated_yaml(local_cloud_baseline_dir())


def find_latest_user_yaml() -> Tuple[Optional[Path], Optional[date]]:
    """Latest dated YAML in the local `user/` overrides dir."""
    return find_latest_dated_yaml(local_user_overrides_dir())


def find_latest_share_yaml() -> Tuple[Optional[Path], Optional[date]]:
    """
    Latest YAML on the SHARE FOLDER. Falls back to the legacy un-dated
    `skills.yaml` when no dated file exists yet. Returns (None, None)
    when the share is unreachable or contains no YAML at all.
    """
    cloud_dir = resolve_cloud_skills_dir()
    if not cloud_dir:
        return (None, None)
    path, dt = find_latest_dated_yaml(cloud_dir)
    if path is not None:
        return (path, dt)
    legacy = Path(cloud_dir) / SKILLS_YAML_FILENAME
    if legacy.exists():
        return (legacy, None)
    return (None, None)


# --- Back-compat aliases (kept for older blueprints / startup code) ------
def find_latest_local_yaml() -> Tuple[Optional[Path], Optional[date]]:
    """
    Latest dated YAML in whichever local source is currently active.
    Falls back to the legacy un-dated `skills.yaml` directly under
    skills_config/ for migrations from earlier versions.
    """
    if get_active_source() == _ACTIVE_SOURCE_USER:
        u_path, u_date = find_latest_user_yaml()
        if u_path is not None:
            return (u_path, u_date)
        # Active says "user" but no user file exists — degrade silently.
    c_path, c_date = find_latest_cloud_baseline_yaml()
    if c_path is not None:
        return (c_path, c_date)
    legacy = _skills_config_root() / SKILLS_YAML_FILENAME
    if legacy.exists():
        return (legacy, None)
    return (None, None)


def find_latest_cloud_yaml() -> Tuple[Optional[Path], Optional[date]]:
    """Back-compat alias for `find_latest_share_yaml`."""
    return find_latest_share_yaml()


# --- Active YAML resolver ------------------------------------------------

def current_active_yaml() -> Tuple[Optional[Path], Optional[date], str]:
    """
    Return `(path, date, effective_source)` of the YAML that should be loaded
    right now. `effective_source` may differ from `get_active_source()` —
    for example, when active="user" but no user file exists, the cloud
    baseline is loaded and the effective source is reported as "cloud".
    """
    if get_active_source() == _ACTIVE_SOURCE_USER:
        u_path, u_date = find_latest_user_yaml()
        if u_path is not None:
            return (u_path, u_date, _ACTIVE_SOURCE_USER)
    c_path, c_date = find_latest_cloud_baseline_yaml()
    if c_path is not None:
        return (c_path, c_date, _ACTIVE_SOURCE_CLOUD)
    legacy = _skills_config_root() / SKILLS_YAML_FILENAME
    if legacy.exists():
        return (legacy, None, _ACTIVE_SOURCE_CLOUD)
    return (None, None, _ACTIVE_SOURCE_CLOUD)


# --- Cloud baseline refresh ---------------------------------------------

def refresh_local_cloud_baseline() -> Tuple[Optional[Path], Optional[date]]:
    """
    Pull the latest YAML from the share folder into the local `cloud/` dir,
    replacing any older mirror files. Best-effort: returns (None, None)
    when off-VPN. The local `user/` dir is left untouched.
    """
    share_path, share_date = find_latest_share_yaml()
    if share_path is None:
        return (None, None)

    import shutil as _shutil
    target_dir = local_cloud_baseline_dir()
    # Materialise the date — if the share file was the legacy un-dated
    # `skills.yaml`, stamp it as today's date so the on-disk layout
    # stays consistent.
    target_name = share_path.name if share_date is not None else today_dated_filename()
    target = target_dir / target_name

    try:
        _shutil.copy2(str(share_path), str(target))
    except Exception as e:
        print(f"[skills_yaml] refresh_local_cloud_baseline failed: {e}")
        return (None, None)

    # Prune older mirror files so the cloud/ dir holds a single baseline.
    try:
        for entry in target_dir.iterdir():
            if entry.is_file() and entry.name != target.name \
                    and entry.name.startswith("skills_") and entry.suffix == ".yaml":
                entry.unlink()
    except OSError:
        pass

    return (target, share_date if share_date is not None else date.today())


# --- Status summary for the chatbot init dialog --------------------------

def skills_yaml_status() -> dict:
    """
    Build the payload consumed by the chatbot init dialog and the side panel.

    Returns:
        {
          "active_source":   "cloud" | "user",     # what the agent uses NOW
          "effective_source": "cloud" | "user",    # what's actually loaded
                                                  #  (may fall back to cloud
                                                  #   when active=user but
                                                  #   no user file exists)
          "cloud_local": {                         # local cloud/ mirror
            "path": str | None,
            "date": "YYYY-MM-DD" | None,
            "filename": str | None,
          },
          "user_local": {                          # local user/ overrides
            "path": str | None,
            "date": "YYYY-MM-DD" | None,
            "filename": str | None,
          },
          "share_remote": {                        # latest on share folder
            "path": str | None,
            "date": "YYYY-MM-DD" | None,
            "filename": str | None,
            "reachable": bool,
          },
        }
    """
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
        "active_source":     get_active_source(),
        "effective_source":  effective,
        "cloud_local":       _summary(c_path, c_date),
        "user_local":        _summary(u_path, u_date),
        "share_remote":      {**_summary(s_path, s_date), "reachable": s_path is not None},
    }


# Back-compat shim: the older endpoint returned a flat dict. Keep it so
# legacy callers don't break, but the field semantics now express the
# cloud/user split.
def is_cloud_newer_than_local() -> dict:
    s = skills_yaml_status()
    local = s["user_local"] if s["active_source"] == _ACTIVE_SOURCE_USER else s["cloud_local"]
    share = s["share_remote"]
    cloud_newer = bool(
        share["date"] and (not local["date"] or share["date"] > local["date"])
    )
    return {
        "local_path":       local["path"],
        "local_date":       local["date"],
        "cloud_path":       share["path"],
        "cloud_date":       share["date"],
        "cloud_newer":      cloud_newer,
        "cloud_reachable":  share["reachable"],
    }
