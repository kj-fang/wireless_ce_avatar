"""
Generic Log Chatbot Service
===========================
Sits on top of the shared Wi-Fi log chatbot engine (``WifiLogAgentSystem``)
for the same reason every other profile does: that base class owns the
300-570 line preprocessing/tool-loop code, and reimplementing it here would
reintroduce the drift risk the shared engine exists to prevent.

This is an independent, domain-agnostic profile -- not "Linux without the
Linux label." Every flag below is decided from what a log of *unknown*
format can and cannot assume, not by analogy to any other profile:

- No marker pair is valid for "any log," so the Segment1 pre-scan markers
  resolve to empty (the same conclusion BT and Linux each reached
  independently for their own log families).
- With no markers, Segment1 can never bound anything, so
  ``SCOPE_FULL_LOG_WHEN_EMPTY`` must be True: it is the only sane fallback
  when the issue-time window is also empty.
- ``GENERIC_AGENT_POLICY`` disables the two tools in the registry tied to a
  specific subsystem (Wi-Fi/BT assert codes, Wi-Fi SoftAP channels), which
  have no meaning for a log of unknown domain, and reads timestamps as-is
  since a log of unknown provenance cannot assume a decode-host clock.
"""

from services.chatbot.engine.system import (
    GENERIC_AGENT_POLICY,
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


class GenericLogAgentSystem(WifiLogAgentSystem):
    """
    Domain-agnostic log analysis agent for a log of unknown/arbitrary format.

    No assumptions about any specific subsystem's init lifecycle or
    vocabulary -- a generic log's structure is not known in advance -- so the
    marker-based Segment1 init block is left OFF by default (empty marker
    lists ⇒ Segment1 is simply not produced). Segment2 (the issue-time
    window) and the skill keyword filter remain the primary,
    scenario-independent way to scope any log.

    SCOPE_FULL_LOG_WHEN_EMPTY = True guarantees that when neither a marker
    block nor an issue-time window matches, the agent still scopes the full
    log so analysis always has something to work on, instead of degrading to
    an empty scope.
    """

    DRIVER_ADD_MARKER: list = []
    RESET_MARKER: list = []
    SCOPE_FULL_LOG_WHEN_EMPTY = True
    CAPABILITY_POLICY = GENERIC_AGENT_POLICY


__all__ = [
    "GenericLogAgentSystem",
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
