"""
Linux Wi-Fi Driver Log Chatbot Service
======================================
Sits on top of the shared Wi-Fi log chatbot engine (``WifiLogAgentSystem``)
and only overrides the pieces that differ for Linux — currently the
Segment1 pre-scan markers used to bracket the driver-init block, and the
capability policy that disables the Windows-only tools.

Why not a thin re-export?
    The pre-scan in the WiFi agent looks for WDI markers ("OS issued
    Driver Device Add" / "Got Command (M1 Message) TASK_DOT11_RESET") to
    define Segment1. Those markers come from the Windows driver and never
    appear in a Linux dmesg/journalctl capture (iwlwifi/cfg80211/mac80211/
    wpa_supplicant), so ``LinuxLogAgentSystem`` makes NO assumptions about a
    specific Linux driver's init lifecycle — same design intent as
    ``BtLogAgentSystem`` — and relies on the domain-agnostic parts of the
    pipeline (Segment2 issue-time window + the skill keyword filter).

Unlike BT's ``.hci.txt`` captures (already in the customer's local time) and
Wi-Fi's ETL decode-host captures (always GMT+8, needing a customer-frame
conversion), a Linux dmesg/journalctl capture is read straight off the
reporting machine with no separate decode step, so it carries no decode-host
offset either. ``prime_with_context`` is therefore NOT overridden here: the
base class's timezone handling already degrades to a no-op when it can't
detect a decode-host/customer split, which is exactly the Linux case.
"""

from services.chatbot.engine.system import (
    LINUX_AGENT_POLICY,
    WifiLogAgentSystem,
    Skill,
    SKILL_FILE_MAP,
    SKILL_DESCRIPTIONS,
    FALLBACK_KEYWORDS,
    sync_to_local,
    build_skill_file_map,
    load_skills_from_data_dir,
    load_skills_from_yaml,
    get_builtin_skills,
)


class LinuxLogAgentSystem(WifiLogAgentSystem):
    """
    Linux-flavoured (dmesg / journalctl) Wi-Fi driver log analysis agent.

    No assumptions about a specific Linux driver's internals — captures vary
    widely across distros, kernel versions and driver builds — so the
    marker-based Segment1 init block is left OFF by default (empty marker
    lists ⇒ Segment1 is simply not produced). Segment2 (the issue-time
    window) and the skill keyword filter remain the primary, scenario-
    independent way to scope any Linux log.

    SCOPE_FULL_LOG_WHEN_EMPTY = True guarantees that when neither a marker
    block nor an issue-time window matches, the agent still scopes the full
    log so analysis always has something to work on, instead of degrading to
    an empty scope.
    """

    DRIVER_ADD_MARKER: list = []
    RESET_MARKER: list = []
    SCOPE_FULL_LOG_WHEN_EMPTY = True
    CAPABILITY_POLICY = LINUX_AGENT_POLICY


__all__ = [
    "LinuxLogAgentSystem",
    "WifiLogAgentSystem",
    "Skill",
    "SKILL_FILE_MAP",
    "SKILL_DESCRIPTIONS",
    "FALLBACK_KEYWORDS",
    "sync_to_local",
    "build_skill_file_map",
    "load_skills_from_data_dir",
    "load_skills_from_yaml",
    "get_builtin_skills",
]
