"""Speclets — the prompts domain experts tune without touching Python.

The same "one shared copy everyone can edit" lifecycle the skills YAML has
(see utils/skills_yaml_utils.py), applied to the prompt text most often
worth adjusting:

    <profile>_agent.md  one file with prompt, report and suggest_time sections
    <profile>_prompt.md   the agent's identity, phases and constraints
    <profile>_report.md   the required ``markdown_summary`` skeleton
    shared_*.md           prompts the engine sends on its own behalf

Layout on the share (and mirrored locally):

    Speclets/wifi_agent.md    or the separate wifi_prompt/report/suggest_time files
    Speclets/bt_agent.md      or the separate bt_prompt/report/suggest_time files
    Speclets/linux_agent.md   or the separate linux_prompt/report/suggest_time files
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

Three places a document can come from
-------------------------------------
The same shape the skills YAML has, because the prompt is the other half of
what makes an agent a domain expert and it deserves the same lifecycle:

    local    ``speclets_user/<name>.md``  — your own version, which a refresh
                                           of the share can never overwrite
    shared   ``speclets/<name>.md``       — the mirror of the share folder
    built-in ``speclet_defaults.py``      — compiled in, used when neither file
                                           exists (and off-VPN)

Which of the first two is live is recorded per document in
``speclets_user/active_sources.json``, so an experiment survives a restart —
the one place this deliberately differs from the skills YAML, whose source
switch is in-memory only. Editing a prompt and pointing a document at your
copy is a per-document decision: you can retarget one agent while every other
one keeps reading the shared text.

``load_into_cache()`` resolves all of this once, so the hot path that builds a
system prompt still only reads an in-memory dict.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import threading
from pathlib import Path
from typing import Optional

from configs.path_configs import (
    LOCAL_SPECLETS_DIR_NAME,
    LOCAL_SPECLETS_USER_DIR_NAME,
    SPECLETS_DIR_bkup,
    SPECLETS_DIR_prim,
)

#: Profiles that own prompt documents. Keys match AgentCapabilityPolicy.profile.
PROFILES = ("wifi", "bt", "nw", "linux")

#: The documents each profile owns.
#:   agent         all three of the below in one file, as ``## prompt`` /
#:                 ``## report`` / ``## suggest_time`` sections. Retargeting a
#:                 domain means replacing all three, so one file is one swap.
#:                 First, because that is the file most people will edit.
#:   prompt        the agent's identity, phases and constraints
#:   report        the required ``markdown_summary`` skeleton
#:   suggest_time  the briefing for "AI suggest issue time", which reads the
#:                 description plus a digest of the log and proposes the
#:                 timestamp the analysis is anchored on. A retargeted agent
#:                 needs this one too: a storage expert should not be hunting
#:                 for a deauth when it picks the moment to look at.
BUNDLE = "agent"
KINDS = (BUNDLE, "prompt", "report", "suggest_time")
#: The three a bundle can carry. ``agent`` is the container, not a section.
BUNDLED_KINDS = tuple(k for k in KINDS if k != BUNDLE)

#: Documents that are the same for every profile. "shared" is not a real
#: profile -- it is a fourth namespace, so ``speclet_filename`` and the cache
#: key need no special case: shared + review -> shared_review.md. These are
#: prompts the engine sends on its own behalf rather than as one of the three
#: agents, which is why they have no per-profile variant.
SHARED = "shared"
SHARED_KINDS = ("review", "issue_time", "followup")

#: Where a live document came from. "shared" is the mirror of the share,
#: "local" your own copy, "built-in" the text compiled into the agent.
SOURCE_SHARED = "shared"
SOURCE_LOCAL = "local"
SOURCE_BUILTIN = "built-in"

#: Records which of the first two a document reads, per document.
_SOURCES_FILENAME = "active_sources.json"

_cache: dict[str, str] = {}
_cache_lock = threading.Lock()
_primed = threading.Event()
#: Resolved source per cache key, for the editor's status payload.
_sources_live: dict[str, str] = {}


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
    return _sibling_local_dir(LOCAL_SPECLETS_DIR_NAME)


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
    # One directory listing instead of a stat() per file. Each of these is ~1 KB
    # but every SMB round-trip costs the better part of a second regardless of
    # size, so asking the share about each name separately dominated this
    # function: 9 remote stats took ~8.5s where a single scandir takes ~1ms.
    def _remote_index(directory):
        try:
            return {
                e.name: (e.stat().st_size, int(e.stat().st_mtime))
                for e in os.scandir(directory) if e.is_file()
            }
        except OSError as e:
            print(f"[speclets] could not list {directory}: {e}")
            return {}

    remote = _remote_index(share_dir)
    local = _remote_index(target_dir)

    skipped = 0
    for profile, kind in all_speclets():
        name = speclet_filename(profile, kind)
        if name not in remote:
            continue
        # copy2 preserves mtime, so a matching size + mtime means the mirror
        # already holds this exact file. Same test skills_yaml_utils uses for
        # the cloud baseline.
        if local.get(name) == remote[name]:
            skipped += 1
            continue
        try:
            shutil.copy2(str(share_dir / name), str(target_dir / name))
            copied += 1
        except Exception as e:
            print(f"[speclets] could not copy {name}: {e}")
    print(f"[speclets] mirrored {copied} file(s), {skipped} already current "
          f"-> {target_dir}")
    return copied


def local_user_speclets_dir() -> Path:
    """Where a prompt the user edited lives, beside the mirror of the share.

    Deliberately a second directory rather than a second filename in the
    first: ``refresh_local_mirror()`` overwrites whatever it mirrors, so a
    user's own version must not live where the share's copy lands.
    """
    return _sibling_local_dir(LOCAL_SPECLETS_USER_DIR_NAME)


def _sibling_local_dir(leaf: str) -> Path:
    try:
        from configs.global_configs import app_config  # local import: avoid cycles

        base = getattr(app_config, "avatarfiles_dir", None)
        if base:
            d = Path(base) / leaf
            d.mkdir(parents=True, exist_ok=True)
            return d
    except Exception:
        pass
    d = Path(__file__).parent.parent / "data" / leaf
    d.mkdir(parents=True, exist_ok=True)
    return d


def _read_file(path: Path) -> Optional[str]:
    if not path.is_file():
        return None
    try:
        text = path.read_text(encoding="utf-8").strip()
    except Exception as e:
        print(f"[speclets] could not read {path.name}: {e}")
        return None
    return text or None


def _read_local(profile: str, kind: str) -> Optional[str]:
    """The SHARED mirror's copy of one document."""
    return _read_file(local_speclets_dir() / speclet_filename(profile, kind))


def read_user_speclet(profile: str, kind: str) -> Optional[str]:
    """The user's own copy of one document, if they have written one."""
    return _read_file(local_user_speclets_dir() / speclet_filename(profile, kind))


# --- which source each document reads ------------------------------------

def _sources_path() -> Path:
    return local_user_speclets_dir() / _SOURCES_FILENAME


def read_sources() -> dict[str, str]:
    """``{"linux_prompt": "local"}``. A document not listed reads the share."""
    try:
        raw = json.loads(_sources_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(raw, dict):
        return {}
    return {str(k): str(v) for k, v in raw.items()
            if str(v) in (SOURCE_SHARED, SOURCE_LOCAL)}


def get_source(profile: str, kind: str) -> str:
    """Which source this document is set to read: "shared" or "local"."""
    return read_sources().get(_cache_key(profile, kind), SOURCE_SHARED)


def set_source(profile: str, kind: str, source: str) -> str:
    """Point one document at the share or at the user's own copy.

    Persisted, so an experiment survives a restart. Returns what was stored.
    """
    chosen = SOURCE_LOCAL if source == SOURCE_LOCAL else SOURCE_SHARED
    sources = read_sources()
    key = _cache_key(profile, kind)
    if chosen == SOURCE_SHARED:
        sources.pop(key, None)
    else:
        sources[key] = chosen
    if kind == BUNDLE:
        # Selecting one whole document is an atomic switch of its three parts.
        # Old per-part choices must not keep pointing at a different version.
        for part in BUNDLED_KINDS:
            sources.pop(_cache_key(profile, part), None)
    try:
        path = _sources_path()
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(sources, indent=1, sort_keys=True), encoding="utf-8")
        shutil.move(str(tmp), str(path))
    except Exception as e:
        print(f"[speclets] could not record the active source for {key}: {e}")
    return chosen


def write_user_speclet(profile: str, kind: str, text: str) -> Path:
    """Save the user's version of one document and return its path.

    When a local bundle already holds this document, the edit goes back into
    that section instead of into a separate file — otherwise the save would
    land in a file the bundle outranks, and appear to do nothing.
    """
    if kind in BUNDLED_KINDS and get_source(profile, BUNDLE) == SOURCE_LOCAL:
        bundle = read_user_speclet(profile, BUNDLE)
        if bundle and split_bundle(bundle).get(kind):
            sections = split_bundle(bundle)
            sections[kind] = text.strip()
            return write_user_speclet(profile, BUNDLE, join_bundle(sections))
    path = local_user_speclets_dir() / speclet_filename(profile, kind)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text.strip() + "\n", encoding="utf-8")
    shutil.move(str(tmp), str(path))
    return path


def delete_user_speclet(profile: str, kind: str) -> bool:
    """Remove the user's version, so the document reads the share again."""
    path = local_user_speclets_dir() / speclet_filename(profile, kind)
    if not path.is_file():
        return False
    path.unlink()
    return True


# --- one file holding all three ------------------------------------------

_SECTION_RE = re.compile(
    r"^#{1,3}[ \t]*(prompt|report|suggest[ _-]?time)[ \t]*:?[ \t]*$",
    re.IGNORECASE | re.MULTILINE,
)


def split_bundle(text: str) -> dict[str, str]:
    """Sections of an ``agent`` bundle, keyed by kind.

    A heading line — ``# prompt``, ``## report``, ``### suggest time`` — opens
    a section and the next one closes it. Anything before the first heading is
    ignored, so a bundle can carry a note to whoever edits it. A file with no
    headings yields nothing rather than guessing which document it is.
    """
    if not text:
        return {}
    found: dict[str, str] = {}
    matches = list(_SECTION_RE.finditer(text))
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        kind = match.group(1).lower().replace(" ", "_").replace("-", "_")
        body = text[match.end():end].strip()
        if body:
            found[kind] = body
    return found


def join_bundle(sections: dict[str, str]) -> str:
    """The inverse: one file from the three documents, in a fixed order."""
    parts = []
    for kind in BUNDLED_KINDS:
        body = (sections.get(kind) or "").strip()
        if body:
            parts.append(f"## {kind}\n\n{body}")
    return "\n\n".join(parts) + "\n"


# --- resolution ----------------------------------------------------------

def _resolve_detail(profile: str, kind: str) -> tuple[Optional[str], str, bool]:
    """Resolve text, source and whether it came from a bundle section."""
    if kind in BUNDLED_KINDS:
        if get_source(profile, BUNDLE) == SOURCE_LOCAL:
            local_bundle = read_user_speclet(profile, BUNDLE)
            section = split_bundle(local_bundle).get(kind)
            if section:
                return section, SOURCE_LOCAL, True
        if get_source(profile, kind) == SOURCE_LOCAL:
            local_part = read_user_speclet(profile, kind)
            if local_part:
                return local_part, SOURCE_LOCAL, False
        shared_bundle = _read_local(profile, BUNDLE)
        section = split_bundle(shared_bundle).get(kind)
        if section:
            return section, SOURCE_SHARED, True
    elif get_source(profile, kind) == SOURCE_LOCAL:
        text = read_user_speclet(profile, kind)
        if text:
            return text, SOURCE_LOCAL, False
    text = _read_local(profile, kind)
    if text:
        return text, SOURCE_SHARED, False
    return None, SOURCE_BUILTIN, False


def resolve(profile: str, kind: str) -> tuple[Optional[str], str]:
    """``(text, source)`` for one document, following its recorded source.

    ``text`` is None when neither file exists: the caller then uses the text
    compiled into the agent, which is what ``source == "built-in"`` means.
    """
    text, source, _via_bundle = _resolve_detail(profile, kind)
    return text, source


def from_bundle(profile: str, kind: str) -> bool:
    """Whether this document's live text came from a bundle section."""
    return _resolve_detail(profile, kind)[2]


def load_into_cache() -> int:
    """Resolve every document into the in-memory cache.

    Called at boot and again whenever a prompt is edited, so the next turn
    builds its system prompt from the new text without a restart — the engine
    reads :func:`get_speclet` fresh on every turn.
    """
    found: dict[str, str] = {}
    sources: dict[str, str] = {}
    for profile, kind in all_speclets():
        text, source = resolve(profile, kind)
        key = _cache_key(profile, kind)
        sources[key] = source
        if text:
            found[key] = text
    with _cache_lock:
        _cache.clear()
        _cache.update(found)
        _sources_live.clear()
        _sources_live.update(sources)
    _primed.set()
    local_count = sum(1 for s in sources.values() if s == SOURCE_LOCAL)
    print(f"[speclets] {len(found)} speclet(s) active"
          + (f", {local_count} from your own copy." if local_count else "."))
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


def documents_for(profile: str) -> list[tuple[str, str]]:
    """The documents one profile's editor may touch.

    Its own bundle and three parts, plus the three the engine sends on its own behalf. The shared
    three are included because they carry the most domain judgement of
    anything here — and listed last, so the editor can mark them as affecting
    every agent rather than just this one.
    """
    own = [(profile, kind) for kind in KINDS] if profile in PROFILES else []
    return own + [(SHARED, kind) for kind in SHARED_KINDS]


def speclet_status(profile: str, default_text=None) -> dict:
    """What the prompt editor shows: every document, and where it reads from.

    ``default_text(profile, kind)`` supplies the built-in text so the payload
    can report its size and whether the live text still equals it — the editor
    needs that to say "this agent is running the text it shipped with".
    """
    share_dir = local_speclets_dir()
    user_dir = local_user_speclets_dir()
    docs = []
    for doc_profile, kind in documents_for(profile):
        key = _cache_key(doc_profile, kind)
        name = speclet_filename(doc_profile, kind)
        live, source = resolve(doc_profile, kind)
        built_in = (default_text(doc_profile, kind) or "") if default_text else ""
        shared_text = _read_local(doc_profile, kind)
        user_text = read_user_speclet(doc_profile, kind)
        docs.append({
            "doc": key,
            "filename": name,
            "profile": doc_profile,
            "kind": kind,
            # A document the engine sends for itself, not as this agent:
            # editing it changes every profile.
            "shared_across_profiles": doc_profile == SHARED,
            "source": source,
            "reads": get_source(doc_profile, kind),
            # Where this one's live text physically came from, so the editor
            # can say "from the one-file bundle" instead of leaving someone
            # wondering why the single-document file they see is not in use.
            "via_bundle": from_bundle(doc_profile, kind),
            "has_shared": shared_text is not None,
            "has_local": user_text is not None,
            "has_builtin": bool(built_in),
            "chars": len(live or built_in),
            "is_builtin_text": (live or built_in).strip() == built_in.strip()
            if built_in else False,
            "local_mtime": _mtime_text(user_dir / name),
            "shared_mtime": _mtime_text(share_dir / name),
        })
    return {
        "success": True,
        "profile": profile,
        "documents": docs,
        "local_dir": str(user_dir),
        "shared_dir": str(share_dir),
        "primed": is_primed(),
    }


def _mtime_text(path: Path) -> str:
    try:
        from datetime import datetime

        return datetime.fromtimestamp(path.stat().st_mtime).strftime("%Y-%m-%d %H:%M")
    except OSError:
        return ""


def publish_user_speclet(profile: str, kind: str) -> tuple[bool, str]:
    """Copy the user's version of one document to the share.

    ``(ok, message)``. The share is the team's copy, so this is the one
    operation here that changes what everyone else loads — the caller asks
    first, and the message names the file that was written.
    """
    text = read_user_speclet(profile, kind)
    if not text:
        return False, "There is no local copy of this prompt to upload."
    share = resolve_share_dir()
    if not share:
        return False, "The shared Speclets folder is unreachable (off-VPN?)."
    name = speclet_filename(profile, kind)
    try:
        (Path(share) / name).write_text(text.strip() + "\n", encoding="utf-8")
    except Exception as e:
        return False, f"Could not write {name} to the share: {e}"
    return True, f"Uploaded {name} to the shared Speclets folder."


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
