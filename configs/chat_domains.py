"""
Chat domains — where a conversation's files are filed, decided in one place.

A chatbot profile names its domain in configs/chatbot_ui.py: "wifi", "bt",
"linux". History and feedback each have to turn that name into a folder and a
filename prefix, and each used to keep its own private map that knew about
Bluetooth and nothing else. Every other name fell through to the Wi-Fi
default, so a Linux conversation was written into the Wi-Fi history files,
labelled wifi, and listed on the Wi-Fi History tab. Nothing failed and nothing
was logged -- the data was simply filed under another domain's name.

So the maps live here instead, declared once per domain. Adding a domain is
one row in DOMAINS; forgetting one is no longer silent, because an
unrecognised name says so the first time it is seen rather than quietly
becoming Wi-Fi.

Two different questions are answered here, and they do not have the same
answers:

  * Which files does this conversation belong in?  ->  DOMAINS.
    Wi-Fi and NW are one storage domain: NW declares domain "wifi" and shares
    Wi-Fi's folder, which is why "nw" is an alias rather than a row.

  * Which agent produced this turn?  ->  AGENT_DOMAINS.
    Gather measures NW separately from Wi-Fi, so it needs a name Wi-Fi's
    storage key does not provide.

This module deliberately imports nothing from the app, so anything may import
it without dragging in configuration or the LLM client.
"""

from __future__ import annotations

from typing import Any, NamedTuple

# Wi-Fi is the empty key: it owns the original folder and unprefixed
# filenames, so every conversation already on a user's machine keeps working
# without a migration.
DEFAULT_DOMAIN = ""


class ChatDomain(NamedTuple):
    """One storage domain: its canonical key, what callers may call it, and
    the folder and filename prefixes its files use."""

    key: str
    aliases: tuple[str, ...]
    history_folder: str
    history_prefix: str
    feedback_prefix: str


# To add a domain: add a row, and give it a folder and prefixes nothing else
# uses. Leave the existing rows alone -- their values are on disk already.
DOMAINS: tuple[ChatDomain, ...] = (
    ChatDomain(
        key="",
        aliases=("", "wifi", "nw"),
        history_folder="history",
        history_prefix="",
        feedback_prefix="",
    ),
    ChatDomain(
        key="bt",
        aliases=("bt", "bluetooth"),
        history_folder="bt_history",
        history_prefix="bt-",
        feedback_prefix="bt_",
    ),
    ChatDomain(
        key="linux",
        aliases=("linux",),
        history_folder="linux_history",
        history_prefix="linux-",
        feedback_prefix="linux_",
    ),
)

# The agent labels Gather accepts. Wi-Fi and NW are separate here although
# they share one storage domain, because "which agent ran" and "whose files
# are these" are different questions.
AGENT_DOMAINS: frozenset[str] = frozenset({"wifi", "nw", "bt", "linux"})

_BY_ALIAS: dict[str, ChatDomain] = {
    alias: domain for domain in DOMAINS for alias in domain.aliases
}
_BY_KEY: dict[str, ChatDomain] = {domain.key: domain for domain in DOMAINS}

# An unknown domain is reported once per name, not once per call: this sits on
# the path of every save, and a warning that repeats per request is a warning
# people learn to scroll past.
_warned: set[str] = set()


def normalize(domain: Any) -> str:
    """
    Canonical storage key for a raw domain hint.

    An unrecognised name still falls back to the default, because refusing to
    file a conversation would lose it -- but it says so the first time, since
    a profile whose domain nobody registered is a bug in the registry and not
    something a user can act on.
    """
    raw = domain.strip().lower() if isinstance(domain, str) else ""
    known = _BY_ALIAS.get(raw)
    if known is not None:
        return known.key
    if raw and raw not in _warned:
        _warned.add(raw)
        print(
            f"[chat_domains] unknown domain {raw!r}: filing it under the "
            f"default domain. Add a row to configs/chat_domains.py if it "
            f"should have files of its own."
        )
    return DEFAULT_DOMAIN


def _entry(domain: Any) -> ChatDomain:
    return _BY_KEY[normalize(domain)]


def history_folder(domain: Any) -> str:
    """Folder name holding this domain's saved conversations."""
    return _entry(domain).history_folder


def history_prefix(domain: Any) -> str:
    """Filename prefix for this domain's conversation files."""
    return _entry(domain).history_prefix


def feedback_prefix(domain: Any) -> str:
    """Filename prefix for this domain's feedback files."""
    return _entry(domain).feedback_prefix


def is_agent_domain(value: Any) -> bool:
    """Whether `value` names an agent Gather can attribute a turn to."""
    return str(value or "").strip().lower() in AGENT_DOMAINS


def known_keys() -> tuple[str, ...]:
    """Every canonical storage key, for tests and diagnostics."""
    return tuple(domain.key for domain in DOMAINS)
