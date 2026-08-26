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

from services.chatbot.engine.system import (
    BT_AGENT_POLICY,
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
from utils.issue_time_utils import resolve_issue_time


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
    CAPABILITY_POLICY = BT_AGENT_POLICY

    # Bluetooth keeps two genuine overrides: the simple-chat prompt carries a
    # BT identity rather than the Wi-Fi one, and prime_with_context skips the
    # Wi-Fi timezone conversion because .hci.txt timestamps are already in the
    # log frame. The Wi-Fi-only tool filtering that used to live here is now
    # BT_AGENT_POLICY.disabled_tools, and the agentic analysis prompt is now
    # bt_prompt.md / bt_report.md under Speclets (see speclet_defaults.py for
    # the built-in fallback copies).

    def _chat_simple(self, user_message: str, temperature: float = 0.2,
                     max_tokens: int = 4000) -> dict:
        """Simple chat mode for Bluetooth troubleshooting."""
        if not self.conversation_history:
            log_snippet = ""
            if self.current_log_path:
                try:
                    from itertools import islice
                    with open(self.current_log_path, "r", encoding="utf-8", errors="replace") as f:
                        log_snippet = "".join(islice(f, 500))
                except Exception:
                    log_snippet = "(unable to read log file)"

            system_msg = (
                "You are a Bluetooth Troubleshooting Assistant.\n"
                "Answer user questions about the log file concisely and accurately.\n"
            )

            if self.issue_context:
                ctx_parts = []
                if self.issue_context.get("case_nbr"):
                    ctx_parts.append(f"Case #: {self.issue_context['case_nbr']}")
                if self.issue_context.get("issue_type"):
                    ctx_parts.append(f"Issue Type: {self.issue_context['issue_type']}")
                if self.issue_context.get("subject"):
                    ctx_parts.append(f"Subject: {self.issue_context['subject']}")
                if self.issue_context.get("description"):
                    ctx_parts.append(f"Description: {self.issue_context['description']}")
                if ctx_parts:
                    system_msg += "\n=== CASE CONTEXT ===\n" + "\n".join(ctx_parts) + "\n\n"

            if self.current_log_path:
                system_msg += f"Log file: {self.current_log_path}\n"
            if log_snippet:
                system_msg += f"\n=== Log Excerpt (first 500 lines) ===\n{log_snippet}\n"

            self.conversation_history.append({"role": "system", "content": system_msg})

        self.conversation_history.append({"role": "user", "content": user_message})

        try:
            response = self.client.chat.completions.create(
                model=self.model,
                messages=self.conversation_history,
                temperature=temperature,
                max_tokens=max_tokens,
            )
            # Same accounting the shared _chat_simple does — this override
            # exists only for the BT system prompt, so a BT turn must still
            # reach gather_service with its token cost.
            self._accumulate_turn_usage(getattr(response, "usage", None))
            content = response.choices[0].message.content or ""
            self.conversation_history.append({"role": "assistant", "content": content})
            return {"type": "text", "data": content}
        except Exception as e:
            error_msg = f"Chat error: {str(e)}"
            print(f"[ERROR] {error_msg}")
            return {"type": "text", "data": error_msg}

    def prime_with_context(self, case_nbr: str = "", subject: str = "",
                           description: str = "", issue_type: str = "",
                           attachment_time: str = "") -> None:
        """Prime the BT agent with case context.

        Key difference from WiFi: BT .hci.txt timestamps are already in the
        CUSTOMER's local time (the HCI decode preserves the original clock),
        whereas WiFi .log timestamps are in the Taiwan decode host's GMT+8.
        Therefore BT skips the entire ``determine_issue_time_frames`` /
        ``taiwan_to_local`` / ``local_to_taiwan`` timezone frame conversion
        — the attachment_time and log content are already in the same frame.
        """
        self.conversation_history = []
        self._detail_cache = {}
        self._detail_query_seen = set()
        self._chat_rules_injected_skills = set()
        self._filter_cache_by_skill = {}
        self._assembled_entries_by_key = {}
        self._assembled_entries_no_ts = {}
        self._assembled_log_text = ""
        self.issue_context = {
            "case_nbr":   case_nbr,
            "subject":    subject,
            "description": description,
            "issue_type": issue_type,
        }

        # BT: no timezone frame conversion needed — .hci.txt timestamps are
        # already in customer local time, same frame as attachment_time.
        self.issue_time_customer = None
        self.issue_time_tz = ""

        # Resolve issue_time: parse attachment_time strictly, fall back to
        # the log file's latest timestamp when no usable input exists.
        dt, src = resolve_issue_time(attachment_time, self.current_log_path)
        self.issue_time = dt
        self._issue_time_time_only = (src == "input_time_only")
        print(f"[DEBUG] BT prime_with_context issue_time={dt} source={src} "
              f"raw='{attachment_time}' (no tz conversion — BT log is customer-local)")

        context_parts = []
        if case_nbr:
            context_parts.append(f"Case: {case_nbr}")
        if subject:
            context_parts.append(f"Subject: {subject}")
        if issue_type:
            context_parts.append(f"Classified issue type: {issue_type}")
        if description:
            context_parts.append(f"\nIssue description:\n{description}")

        if context_parts:
            self.conversation_history.append({
                "role": "system",
                "content": (
                    "You are a Bluetooth troubleshooting assistant with expert-level knowledge.\n"
                    f"Available skills: {', '.join(self.skills.keys())}.\n"
                    "Use fetch_filtered_logs with the most relevant skill(s), then call "
                    "submit_final_report.\n\n"
                    "=== Case Context ===\n"
                    + "\n".join(context_parts)
                )
            })



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
