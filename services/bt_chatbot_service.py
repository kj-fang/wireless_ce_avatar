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
from datetime import datetime
from utils.issue_time_utils import resolve_issue_time, parse_issue_time_string, format_issue_time


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

    # ------------------------------------------------------------------
    # Override WiFi-specific system prompts with BT-appropriate identity
    # ------------------------------------------------------------------
    def _build_analyze_system_prompt(self, context_section: str) -> str:
        """Build the agentic analysis system prompt for Bluetooth log analysis."""
        ace_block = self._build_ace_workflow_block()
        return (
            f"{context_section}"
            + ace_block
            + "You are an Elite Bluetooth Diagnostic Detective. Your GOAL: Find the REAL Root Cause based on evidence.\n"
            + "Available skills:\n"
            + "".join(
                f"  - {s['name']}: {s['description']}\n"
                for s in self.get_skill_descriptions()
                if s.get('description')
            )
            + "\n"
            "PHASE 1 (SYMPTOM LOCALIZATION):\n"
            "   - Call `fetch_filtered_logs` with the most relevant skill to get symptom-focused log evidence.\n"
            "   - Call `fetch_filtered_logs` with skill `assert_code_analysis` to scan for firmware asserts.\n"
            "PHASE 2 (SOURCE RETROSPECTIVE - optional):\n"
            "   - if needed, based on the analysis from PHASE1, use additional skills to get more detail from the logs.\n"
            "PHASE 3. Call `submit_final_report` to conclude.\n\n"
            "CRITICAL CONSTRAINTS:\n"
            "- Max step is 8\n"
            "- 🛑 NO REPETITION: Do not fetch the same data twice. If Phase 1 keywords are found in Phase 2, ignore them.\n"
            "- 🛑 IMMEDIATELY call `submit_final_report` after your detail query. Do not over-analyze.\n\n"
            + self.REPORT_MARKDOWN_TEMPLATE
        )

    def _chat_simple(self, user_message: str, temperature: float = 0.2,
                     max_tokens: int = 4000) -> dict:
        """Simple chat mode for Bluetooth troubleshooting."""
        if not self.conversation_history:
            log_snippet = ""
            if self.current_log_path:
                try:
                    from utils.helpers import read_log_file
                    lines = read_log_file(self.current_log_path)
                    log_snippet = "\n".join(str(l) for l in lines[:500])
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

    # ------------------------------------------------------------------
    # Override tool list: remove WiFi-only tools (lookup_assert_code,
    # softAP_supported_channel) that have no BT equivalent.
    # ------------------------------------------------------------------
    def _build_tools(self) -> list:
        return [
            {
                "type": "function",
                "function": {
                    "name": "fetch_filtered_logs",
                    "description": (
                        "Filter from the original full log using the specified skill, then merge results "
                        "into a cumulative timestamp-assembled log (line numbers are not persisted). "
                        "Returns a compact skill-focused evidence payload to save tokens."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "skill_name": {
                                "type": "string",
                                "enum": list(self.skills.keys()),
                                "description": "Which skill's filter to apply (e.g., 'Connectivity', 'Yellow_Bang')"
                            }
                        },
                        "required": ["skill_name"]
                    }
                }
            },
            {
                "type": "function",
                "function": {
                    "name": "submit_final_report",
                    "description": (
                        "Call this tool once you have identified the root cause. "
                        "Submits the structured final analysis report."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "root_cause_summary": {
                                "type": "string",
                                "description": "One-sentence root cause summary"
                            },
                            "confidence_score": {
                                "type": "integer",
                                "description": "Confidence 0-100"
                            },
                            "recommended_actions": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": "Bullet-point actions"
                            },
                            "involved_skills": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": "Skills used in this diagnosis"
                            },
                            "markdown_summary": {
                                "type": "string",
                                "description": "Full Markdown report for engineers"
                            },
                            "applied_bullet_ids": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": (
                                    "ACE playbook bullet ids (e.g. 'conn-00042', 'agent-00007') "
                                    "that you actually relied on for this analysis. "
                                    "Leave empty if no playbook bullets applied."
                                )
                            },
                            "flagged_bullet_ids": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": (
                                    "ACE playbook bullet ids that conflicted with the evidence "
                                    "and should be flagged as harmful in the next reflection."
                                )
                            }
                        },
                        "required": [
                            "root_cause_summary", "confidence_score",
                            "recommended_actions", "involved_skills", "markdown_summary"
                        ]
                    }
                }
            }
        ]


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
