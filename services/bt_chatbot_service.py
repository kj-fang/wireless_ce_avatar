"""
Bluetooth Log Chatbot Service
=============================
Sits on top of the shared WiFi log chatbot engine (``WifiLogAgentSystem``)
and only overrides the pieces that differ for BT — currently the Segment1
pre-scan markers used to bracket the driver-init block.

Why not a thin re-export anymore?
    The pre-scan in the WiFi agent looks for WDI markers ("OS issued
    Driver Device Add" / "Got Command (M1 Message) TASK_DOT11_RESET") to
    define Segment1. BT HCI logs never contain those, so for BT users
    Segment1 used to be permanently 0 lines — the agent lost the driver
    init context. ``BtLogAgentSystem`` swaps the markers to ibtpci-flavoured
    equivalents so the same pre-scan logic finds the BT init block.

Future BT-specific overrides (timestamp regex for ``<HH:MM:SS.mmm>``-style
HCI logs, custom continuation-line detection, etc.) live here too.
"""

from services.log_chatbot_service import (
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


class BtLogAgentSystem(WifiLogAgentSystem):
    """
    Bluetooth-flavoured log analysis agent.

    Design intent — generality over a WiFi-shaped model:
      The base agent's pre-scan was built around the Wi-Fi driver lifecycle
      (driver-add → DOT11 reset bookend a "Segment1" init block). That model
      does NOT generalise across the BT log family — collectors, driver
      builds and capture types vary widely — so hard-coding any specific BT
      driver callback names would just trade one case-specific assumption for
      another. BT therefore makes NO assumptions about driver internals and
      relies on the domain-agnostic parts of the pipeline:

        * Segment2 — the issue-time window (purely timestamp-based) — the
          primary, scenario-independent way to scope any BT log.
        * the skill keyword filter — trims the scoped window to the relevant
          evidence regardless of log size.

      The marker-based Segment1 init block is left OFF by default (empty
      marker lists ⇒ Segment1 is simply not produced — a clean no-op). The
      base scan already accepts a str or a list of candidates, so a specific
      deployment that genuinely benefits from a BT init block can populate
      these via config/override using the same data mechanism — but the
      shipped default stays correct for the WHOLE BT case, not one driver.

    SCOPE_FULL_LOG_WHEN_EMPTY = True guarantees that when neither a marker
    block nor an issue-time window matches, BT still scopes the full log so
    analysis always has something to work on (the keyword filter handles the
    volume), instead of degrading to an empty scope.
    """

    # No assumptions about BT driver internals. Opt in via config/override
    # only if a specific deployment proves it needs an init block.
    DRIVER_ADD_MARKER: list = []
    RESET_MARKER: list = []

    # BT has no reliable init/reset lifecycle to bookend a context block, so
    # never let scoping fall through to empty.
    SCOPE_FULL_LOG_WHEN_EMPTY = True


__all__ = [
    "BtLogAgentSystem",
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
