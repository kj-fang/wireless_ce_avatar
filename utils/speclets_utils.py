"""Speclets — the prompts domain experts tune without touching Python.

The same "one shared copy everyone can edit" lifecycle the skills YAML has
(see utils/skills_yaml_utils.py), applied to the prompt text most often
worth adjusting:

    <profile>_prompt.md   the agent's identity, phases and constraints
    <profile>_report.md   the required ``markdown_summary`` skeleton
    shared_*.md           prompts the engine sends on its own behalf

Layout on the share (and mirrored locally):

    Speclets/wifi_prompt.md   Speclets/wifi_report.md
    Speclets/bt_prompt.md     Speclets/bt_report.md
    Speclets/nw_prompt.md     Speclets/nw_report.md
    Speclets/shared_review.md      the final-report quality auditor
    Speclets/shared_issue_time.md  free-text issue-time extraction
    Speclets/shared_followup.md    the post-analysis follow-up framing

The shared three have no per-profile variant because the engine is not
speaking as one of the three agents when it sends them -- it is auditing,
extracting or re-framing, and all three profiles want the same behaviour.
They carry the most domain judgement of anything here (the auditor alone
encodes an eleven-rule hierarchy of truth), which is exactly why they should
be editable by the people who own that judgement.

Loading is deliberately non-blocking. ``prime_async()`` refreshes the local
mirror and fills the in-memory cache on a daemon thread, so a slow or
unreachable share never delays application start. Until that finishes — and
forever, if the share is unreachable — ``get_speclet()`` returns None and the
caller keeps using the defaults compiled into the agent. That makes an
off-VPN run behave exactly as it did before Speclets existed.
"""

from __future__ import annotations

import shutil
import threading
from pathlib import Path
from typing import Optional

from configs.path_configs import (
    LOCAL_SPECLETS_DIR_NAME,
    SPECLETS_DIR_bkup,
    SPECLETS_DIR_prim,
)

#: Profiles that own a speclet pair. Keys match AgentCapabilityPolicy.profile.
PROFILES = ("wifi", "bt", "nw")

#: The two documents each profile owns.
KINDS = ("prompt", "report")

#: Documents that are the same for every profile. "shared" is not a real
#: profile -- it is a fourth namespace, so ``speclet_filename`` and the cache
#: key need no special case: shared + review -> shared_review.md. These are
#: prompts the engine sends on its own behalf rather than as one of the three
#: agents, which is why they have no per-profile variant.
SHARED = "shared"
SHARED_KINDS = ("review", "issue_time", "followup")

_cache: dict[str, str] = {}
_cache_lock = threading.Lock()
_primed = threading.Event()


def all_speclets():
    """Every (profile, kind) pair the mirror, cache and publisher iterate.

    One generator so the three loops cannot drift: adding a shared document
    means adding a name to SHARED_KINDS, nothing else.
    """
    for profile in PROFILES:
        for kind in KINDS:
            yield profile, kind
    for kind in SHARED_KINDS:
        yield SHARED, kind


def speclet_filename(profile: str, kind: str) -> str:
    """``wifi`` + ``prompt`` -> ``wifi_prompt.md``."""
    return f"{profile}_{kind}.md"


def _cache_key(profile: str, kind: str) -> str:
    return f"{profile}_{kind}"


def local_speclets_dir() -> Path:
    """Local mirror of the share, beside skills_config/ under avatarfiles_dir.

    Falls back to a repo-relative data/ path when avatarfiles_dir has not been
    initialised yet, mirroring skills_yaml_utils._skills_config_root().
    """
    try:
        from configs.global_configs import app_config  # local import: avoid cycles

        base = getattr(app_config, "avatarfiles_dir", None)
        if base:
            d = Path(base) / LOCAL_SPECLETS_DIR_NAME
            d.mkdir(parents=True, exist_ok=True)
            return d
    except Exception:
        pass
    d = Path(__file__).parent.parent / "data" / LOCAL_SPECLETS_DIR_NAME
    d.mkdir(parents=True, exist_ok=True)
    return d


def resolve_share_dir(timeout_sec: int = 8) -> Optional[str]:
    """First reachable Speclets share, or None when off-VPN."""
    from utils import helpers  # local import: helpers pulls in heavier deps

    return helpers.get_load_path(SPECLETS_DIR_prim, SPECLETS_DIR_bkup, timeout_sec)


def refresh_local_mirror() -> int:
    """Copy every speclet from the share into the local mirror.

    Best-effort by design: a share that is unreachable, or missing some of the
    six files, simply leaves the corresponding local copy (and therefore the
    built-in default) in place. Returns the number of files refreshed.
    """
    share = resolve_share_dir()
    if not share:
        print("[speclets] share unreachable - using built-in defaults.")
        return 0

    share_dir = Path(share)
    target_dir = local_speclets_dir()
    copied = 0
    for profile, kind in all_speclets():
        name = speclet_filename(profile, kind)
        src = share_dir / name
        if not src.is_file():
            continue
        try:
            shutil.copy2(str(src), str(target_dir / name))
            copied += 1
        except Exception as e:
            print(f"[speclets] could not copy {name}: {e}")
    print(f"[speclets] mirrored {copied} file(s) -> {target_dir}")
    return copied


def _read_local(profile: str, kind: str) -> Optional[str]:
    path = local_speclets_dir() / speclet_filename(profile, kind)
    if not path.is_file():
        return None
    try:
        text = path.read_text(encoding="utf-8").strip()
    except Exception as e:
        print(f"[speclets] could not read {path.name}: {e}")
        return None
    return text or None


def load_into_cache() -> int:
    """Read whatever the local mirror holds into the in-memory cache."""
    found = {}
    for profile, kind in all_speclets():
        text = _read_local(profile, kind)
        if text:
            found[_cache_key(profile, kind)] = text
    with _cache_lock:
        _cache.clear()
        _cache.update(found)
    _primed.set()
    print(f"[speclets] {len(found)} speclet(s) active.")
    return len(found)


def get_speclet(profile: str, kind: str) -> Optional[str]:
    """Return the override text, or None to signal "use the built-in default".

    Never blocks and never raises: a caller on the hot path (building a system
    prompt) must not wait on the share, so a not-yet-primed cache simply
    reports "no override".
    """
    with _cache_lock:
        return _cache.get(_cache_key(profile, kind))


def is_primed() -> bool:
    """True once a load pass has completed (with or without any files found)."""
    return _primed.is_set()


def refresh_and_load() -> int:
    """Full pass: pull the share into the local mirror, then load the cache."""
    try:
        refresh_local_mirror()
    except Exception as e:
        print(f"[speclets] mirror refresh failed: {e}")
    try:
        return load_into_cache()
    except Exception as e:
        print(f"[speclets] cache load failed: {e}")
        _primed.set()  # unblock is_primed() waiters even on failure
        return 0


def prime_async() -> threading.Thread:
    """Run :func:`refresh_and_load` on a daemon thread and return immediately.

    Called from set_up_app during boot. The share probe alone can take seconds
    off-VPN, which is why this never runs on the startup path.
    """
    t = threading.Thread(target=refresh_and_load, name="speclets-prime", daemon=True)
    t.start()
    return t


def publish_defaults_to_share(defaults: dict[str, str], overwrite: bool = False) -> int:
    """Seed the share with the built-in defaults.

    Used once to populate an empty Speclets folder so the team has something
    to edit. ``defaults`` maps ``"<profile>_<kind>"`` to text. Existing files
    are left alone unless ``overwrite`` is set, so this can never silently
    discard someone's edits.
    """
    share = resolve_share_dir()
    if not share:
        print("[speclets] share unreachable - nothing published.")
        return 0

    share_dir = Path(share)
    written = 0
    for profile, kind in all_speclets():
        key = _cache_key(profile, kind)
        text = defaults.get(key)
        if not text:
            continue
        target = share_dir / speclet_filename(profile, kind)
        if target.exists() and not overwrite:
            print(f"[speclets] {target.name} already exists - left untouched.")
            continue
        try:
            target.write_text(text.strip() + "\n", encoding="utf-8")
            written += 1
        except Exception as e:
            print(f"[speclets] could not write {target.name}: {e}")
    print(f"[speclets] published {written} default(s) -> {share_dir}")
    return written
