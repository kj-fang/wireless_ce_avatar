"""Tool schema and tool-call execution behavior for chatbot agents."""

from __future__ import annotations

import re
from typing import Optional

from utils.assert_code_utils import lookup_assert_code
from utils.softAP_supported_channel import softAP_supported_channel


class ToolExecutionMixin:
    """ToolExecution behavior for the composed agent."""

    def _build_analyze_system_prompt(self, context_section: str) -> str:
        """Build the agentic analysis system prompt used by _chat_with_tools."""
        ace_block = self._build_ace_workflow_block()
        return (
            f"{context_section}"
            + ace_block
            + "You are an Elite Wi-Fi Diagnostic Detective. Your GOAL: Find the REAL Root Cause based on evidence.\n"
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
                        "Your `markdown_summary` format (REQUIRED):\n"
                        "  # Executive Summary\n  (1-2 sentences about the true root cause found in Phase 2)\n\n"
                        "  | Aspect | Finding |\n"
                        "  |--------|---------|\n"
                        "  | Signal | ... |\n"
                        "  (Markdown table with data gaps)\n\n"
                        "  ## Timeline\n"
                        "  - T-Ns: Trigger Event (The Source)\n"
                        "  - T+0s: Physical Failure begins\n"
                        "  - T+Ns: Final Termination\n\n"
                        "  ## Recommendations\n"
                        "  **P0 (Urgent):** ...\n"
                        "  **P1 (Important):** ...\n"
                        "  **P2 (Nice-to-have):** ..."                
        )

    def _invoke_tool(self, tool_name: str, args: dict) -> str:
        """Centralized tool dispatch used by both chat and analyze flows."""
        if tool_name in self.capabilities.disabled_tools:
            return f"{tool_name} is not available for {self.capabilities.profile} log analysis."

        if tool_name == "fetch_filtered_logs":
            return self.fetch_filtered_logs(args.get("skill_name", ""))

        if tool_name == "query_log_detail":
            anchor_text = args.get("anchor_text", "")
            anchor_timestamp = args.get("anchor_timestamp", "")
            context_span = self._resolve_context_span(
                anchor_text,
                args.get("context_span", self.DEFAULT_DETAIL_CONTEXT_SPAN),
            )
            max_hits = min(args.get("max_hits", 3), self.MAX_DETAIL_HITS)
            return self.query_log_detail(
                anchor_text=anchor_text,
                anchor_timestamp=anchor_timestamp,
                context_span=context_span,
                max_hits=max_hits,
            )

        if tool_name in ("get_assembled_log_snapshot", "get_final_state_snapshot"):
            return (
                f"{tool_name} is disabled. "
                "Use fetch_filtered_logs(skill_name) to retrieve skill-focused evidence "
                "or query_log_detail(keyword) to search specific events."
            )

        if tool_name == "lookup_assert_code":
            return lookup_assert_code(args.get("code", ""))

        if tool_name == "softAP_supported_channel":
            err = self._ensure_raw_log_cache()
            if err:
                return err
            log_text = "\n".join(self._raw_log_cache)
            if not log_text.strip():
                return "ERROR: Raw log is empty or unavailable."
            return softAP_supported_channel(log_text)

        return f"Unknown tool: {tool_name}"

    def _append_tool_message(self, messages: list, tool_call, content: str) -> None:
        """Append a tool result message in the required protocol format."""
        messages.append({
            "role": "tool",
            "tool_call_id": tool_call.id,
            "name": tool_call.function.name,
            "content": content,
        })

    def _handle_submit_tool_call(self, tool_call, args: dict, issue_description: str,
                                 step_num: int, max_steps: int, messages: list,
                                 pending_user_nudges: list, steps: list, emit_cb) -> Optional[dict]:
        """Handle submit_final_report and return final response dict when accepted."""
        review = self._review_report_quality(issue_description, args)
        if not review.get("approved", True) and step_num < max_steps - 1:
            required_actions = review.get("required_actions", []) or []
            self._append_tool_message(
                messages,
                tool_call,
                (
                    "Rejected by quality gate. "
                    f"Reason: {review.get('reason', 'insufficient support')}."
                ),
            )
            emit_cb({
                "role": "agent",
                "content": (
                    " **Quality gate:** report needs refinement before final submit.\n"
                    f"Reason: {review.get('reason', 'insufficient support')}"
                ),
            })
            pending_user_nudges.append({
                "role": "user",
                "content": (
                    "Refine your diagnosis before submit_final_report. "
                    f"Reason: {review.get('reason', '')}. "
                    f"Required actions: {', '.join(str(x) for x in required_actions) if required_actions else 'perform temporal/contradiction validation with existing evidence.'}"
                ),
            })
            return None

        self._append_tool_message(messages, tool_call, "Final report accepted.")
        emit_cb({"role": "agent", "content": " **Conclusion Reached!** Generating report."})
        # Models occasionally violate the array schema.  Keep the report
        # contract stable for every profile before it reaches the frontend.
        for key in ("recommended_actions", "involved_skills"):
            value = args.get(key)
            if value is None:
                args[key] = []
            elif isinstance(value, str):
                parts = [part.strip("- *•\t ").strip()
                         for part in re.split(r"[\n;]+", value) if part.strip()]
                args[key] = parts or [value]
            elif isinstance(value, dict):
                args[key] = [str(item) for item in value.values()]
            elif not isinstance(value, list):
                args[key] = [str(value)]
            else:
                args[key] = [str(item) for item in value]
        self._inject_analysis_into_history(issue_description, steps, args)
        return {
            "type": "report",
            "data": args,
            "steps": steps,
            "issue_time": (
                self.issue_time.strftime('%m/%d/%Y %H:%M:%S')
                if self.issue_time else None
            ),
        }

    def _handle_fetch_tool_call(self, tool_call, args: dict, messages: list,
                                pending_user_nudges: list, expert_rules_injected_skills: set,
                                skill_call_counts: dict, no_progress_rounds: int, emit_cb) -> int:
        """Handle fetch_filtered_logs tool call and return updated no_progress_rounds."""
        skill_name = args.get("skill_name")
        skill_call_counts[skill_name] = skill_call_counts.get(skill_name, 0) + 1

        # Enforce max distinct skill fetches to control token budget.
        distinct_skills_fetched = len([k for k, v in skill_call_counts.items() if v >= 1])
        if distinct_skills_fetched > self.MAX_SKILL_FETCHES:
            msg = (
                f"Skill fetch limit reached ({self.MAX_SKILL_FETCHES} skills). "
                "Synthesize findings from already-fetched skills and call submit_final_report."
            )
            emit_cb({"role": "agent", "content": f"⚠️ **Skill cap hit** — {msg}"})
            self._append_tool_message(messages, tool_call, msg)
            return no_progress_rounds

        emit_cb({"role": "agent", "content": f" **Fetching Filtered Logs** for `{skill_name}`..."})

        tool_result = self._invoke_tool("fetch_filtered_logs", {"skill_name": skill_name})
        skill = self.skills.get(skill_name)
        expert_rules = getattr(skill, 'expert_rules', '') if skill else ''

        line_count = tool_result.count('\n')
        preview = tool_result[:500].replace('\n', ' ') + "..."
        if self.capabilities.emit_fetch_previews:
            emit_cb({
                "role": "tool",
                "content": f" **Logs Loaded** (`{skill_name}`, ~{line_count} lines):\n```\n{preview}\n```",
            })
        # emit_cb({
        #     "role": "tool",
        #     "content": f" **Logs Loaded** (`{skill_name}`, ~{line_count} lines):\n```\n{preview}\n```"
        # })

        # Expert rules are prepended in full (never clipped); only the evidence
        # section is clipped so the tool_result immediately follows tool_use.
        if skill_name not in expert_rules_injected_skills and expert_rules:
            rules_section = (
                f"=== Expert Rules for {skill_name} ===\n{expert_rules}\n\n"
                "=== Rule Usage Instruction ===\n"
                "Use these expert rules as investigative clues.\n"
                "For each important claim, map each rule clue to concrete log evidence\n"
                "and decide: supported, refuted, or uncertain.\n\n"
            )
            expert_rules_injected_skills.add(skill_name)
        else:
            rules_section = (
                f"=== Expert Rules for {skill_name} ===\n"
                "(already provided; omitted to save tokens)\n\n"
            )

        evidence_content = self._clip_for_prompt(
            f"=== Skill-Focused Evidence ({skill_name}) ===\n{tool_result}",
            limit=self.MAX_TOOL_RESULT_CHARS_IN_MESSAGES,
        )
        self._append_tool_message(
            messages,
            tool_call,
            rules_section + evidence_content,
        )
        emit_cb({
            "role": "debug",
            "content": (
                f"**Token Budget ({skill_name}):** "
                f"rules={len(rules_section)} chars | "
                f"evidence={len(evidence_content)} chars | "
                f"total={len(rules_section) + len(evidence_content)} chars "
                f"(evidence limit={self.MAX_TOOL_RESULT_CHARS_IN_MESSAGES})"
            ),
        })

        if "New lines merged this round: 0" in tool_result or "Skill cache hit:" in tool_result:
            no_progress_rounds += 1
        else:
            no_progress_rounds = 0

        if skill_call_counts.get(skill_name, 0) >= 3:
            pending_user_nudges.append({
                "role": "user",
                "content": (
                    "Avoid repeatedly querying the same evidence view unless it adds new information. "
                    "Cross-check with another perspective or synthesize current findings."
                ),
            })
        if no_progress_rounds >= 2:
            pending_user_nudges.append({
                "role": "user",
                "content": (
                    "Recent tool calls did not add new evidence. "
                    "Prioritize contradiction checks, timeline reconciliation, and final synthesis."
                ),
            })
        return no_progress_rounds

    def _handle_snapshot_tool_call(self, tool_call, args: dict, messages: list, emit_cb) -> None:
        """Handle get_assembled_log_snapshot tool call."""
        mode = args.get("mode", "summary")
        if mode == "full":
            mode = "compact"
        emit_cb({"role": "agent", "content": f" **Requesting assembled snapshot** (mode={mode})"})
        tool_result = self._invoke_tool("get_assembled_log_snapshot", {"mode": mode})
        emit_cb({
            "role": "tool",
            "content": f" **Assembled Snapshot Loaded**:\n```\n{tool_result[:500]}\n```"
        })
        self._append_tool_message(
            messages,
            tool_call,
            self._clip_for_prompt(tool_result, limit=self.MAX_TOOL_RESULT_CHARS_IN_MESSAGES),
        )

    def _handle_final_state_tool_call(self, tool_call, args: dict, messages: list, emit_cb) -> None:
        """Handle get_final_state_snapshot tool call."""
        tail_lines = args.get("tail_lines", 120)
        emit_cb({
            "role": "agent",
            "content": f" **Requesting final-state snapshot** (tail_lines={tail_lines})"
        })
        tool_result = self._invoke_tool("get_final_state_snapshot", {"tail_lines": tail_lines})
        emit_cb({
            "role": "tool",
            "content": f" **Final-State Snapshot Loaded**:\n```\n{tool_result[:500]}\n```"
        })
        self._append_tool_message(
            messages,
            tool_call,
            self._clip_for_prompt(tool_result, limit=self.MAX_TOOL_RESULT_CHARS_IN_MESSAGES),
        )

    def _handle_detail_tool_call(self, tool_call, args: dict, messages: list,
                                 pending_user_nudges: list, no_match_anchor_counts: dict,
                                 detail_call_counts: dict, emit_cb) -> None:
        """Handle query_log_detail tool call and anti-loop nudges."""
        anchor_text = args.get("anchor_text", "")
        anchor_timestamp = args.get("anchor_timestamp", "")
        detail_sig = f"{anchor_text.lower()}|{anchor_timestamp}"
        detail_call_counts[detail_sig] = detail_call_counts.get(detail_sig, 0) + 1
        context_span = self._resolve_context_span(anchor_text, args.get("context_span", self.DEFAULT_DETAIL_CONTEXT_SPAN))
        max_hits = min(args.get("max_hits", 3), self.MAX_DETAIL_HITS)
        emit_cb({
            "role": "agent",
            "content": (
                " **Querying anchor context** "
                f"(text='{anchor_text}', ts='{anchor_timestamp}')"
            )
        })

        tool_result = self._invoke_tool(
            "query_log_detail",
            {
                "anchor_text": anchor_text,
                "anchor_timestamp": anchor_timestamp,
                "context_span": context_span,
                "max_hits": max_hits,
            },
        )

        query_sig = f"{anchor_text.lower()}|{anchor_timestamp}"
        if "No matching anchor found" in tool_result:
            no_match_anchor_counts[query_sig] = no_match_anchor_counts.get(query_sig, 0) + 1
            if no_match_anchor_counts[query_sig] >= 2:
                pending_user_nudges.append({
                    "role": "user",
                    "content": (
                        "You repeated an anchor query with no matches. "
                        "Switch to a different anchor or synthesize conclusions from existing evidence; "
                        "do not loop on the same missing anchor."
                    ),
                })
        if detail_call_counts.get(detail_sig, 0) >= 3:
            pending_user_nudges.append({
                "role": "user",
                "content": (
                    "Detail queries are repeating similar anchors. "
                    "Move from retrieval to judgment: reconcile timeline and contradictions, then conclude."
                ),
            })

        emit_cb({
            "role": "tool",
            "content": f"📄 **Detail Loaded**:\n```\n{tool_result}\n```"
        })
        self._append_tool_message(
            messages,
            tool_call,
            self._clip_for_prompt(tool_result, limit=self.MAX_TOOL_RESULT_CHARS_IN_MESSAGES),
        )

    def _inject_analysis_into_history(self, issue_description: str,
                                       steps: list, report: dict) -> None:
        """
        After a chat analysis finishes, inject a clean summary into
        self.conversation_history so follow-up chat() calls have full
        context of the prior analysis.

        IMPORTANT: Only plain text/assistant messages, NO tool_use/tool_result.
        """
        # Build a condensed recap of the agent's thinking process
        # ONLY include agent/tool content, excluding any metadata
        thinking_parts = []
        for s in steps:
            role = s.get("role", "")
            content = s.get("content", "")
            # Only extract raw content, skip any tool_id/name fields
            if role in ("agent", "tool") and content:
                thinking_parts.append(content[:300] if role == "tool" else content)

        thinking_recap = "\n".join(thinking_parts)
        # Cap at 5000 chars
        if len(thinking_recap) > 5000:
            thinking_recap = thinking_recap[:5000] + "\n... (truncated)"

        # Build report summary text
        report_parts = []
        if report.get("root_cause_summary"):
            report_parts.append(f"**Root Cause:** {report['root_cause_summary']}")
        if report.get("confidence_score"):
            report_parts.append(f"**Confidence:** {report['confidence_score']}%")
        if report.get("recommended_actions"):
            actions = "\n".join(f"- {a}" for a in report["recommended_actions"])
            report_parts.append(f"**Recommendations:**\n{actions}")
        if report.get("markdown_summary"):
            report_parts.append(f"\n{report['markdown_summary']}")

        report_text = "\n".join(report_parts)

        # Build CLEAN conversation history: ONLY system/user/assistant roles
        # (NO tool_use, tool_result, or any tool-related fields)
        assistant_summary = (
            f"##  Analysis Complete\n\n"
            f"###  Agent Reasoning Process\n{thinking_recap}\n\n"
            f"###  Report\n{report_text}"
        )

        # CRITICAL: Reset to completely clean history
        self.conversation_history = [
            {
                "role": "system",
                "content": (
                    "You are a Wi-Fi troubleshooting expert.\n"
                    f"Available diagnostic skills: {', '.join(self.skills.keys())}.\n\n"
                    "A comprehensive multi-skill analysis has been completed.\n"
                    "Review the results below and answer user follow-up questions.\n"
                    f"Log file: {self.current_log_path}"
                ),
            },
            {
                "role": "user",
                "content": f"Analyze this issue: {issue_description}",
            },
            {
                "role": "assistant",
                "content": assistant_summary,
            },
        ]

    def _build_tools(self) -> list:
        tools = [
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
                                "description": "Which skill's filter to apply (e.g., 'Connectivity', 'Roaming')"
                            }
                        },
                        "required": ["skill_name"]
                    }
                }
            },
            # {
            #     "type": "function",
            #     "function": {
            #         "name": "get_assembled_log_snapshot",
            #         "description": (
            #             "Retrieve assembled-log macro view on demand. "
            #             "Use mode='summary' for metadata only, 'compact' for limited body, "
            #             "or 'full' for complete assembled content."
            #         ),
            #         "parameters": {
            #             "type": "object",
            #             "properties": {
            #                 "mode": {
            #                     "type": "string",
            #                     "enum": ["summary", "compact", "full"],
            #                     "description": "How much assembled content to return.",
            #                     "default": "summary"
            #                 }
            #             },
            #             "required": []
            #         }
            #     }
            # },
            # {
            #     "type": "function",
            #     "function": {
            #         "name": "get_final_state_snapshot",
            #         "description": (
            #             "Retrieve the latest assembled-log tail for end-of-analysis verification. "
            #             "Use this before declaring a persistent failure to check whether later logs show recovery/success."
            #         ),
            #         "parameters": {
            #             "type": "object",
            #             "properties": {
            #                 "tail_lines": {
            #                     "type": "integer",
            #                     "description": "Number of latest lines to inspect. Default 120, range 20-400.",
            #                     "default": 120
            #                 }
            #             },
            #             "required": []
            #         }
            #     }
            # },
            {
                "type": "function",
                "function": {
                    "name": "lookup_assert_code",
                    "description": (
                        "Look up a firmware assert/error code from the Intel Wi-Fi LMAC or UMAC header. "
                        "Accepts the raw code exactly as it appears in the log — flag decomposition is "
                        "handled automatically.\n"
                        "Code formats seen in logs:\n"
                        "  0x20xxxxxx → UMAC assert (0x20000000 CPU flag stripped automatically)\n"
                        "  0x10xxxx   → UMAC namespace (UMAC_ASSERT_START)\n"
                        "  0x40xxxx   → LMAC RCM sub-CPU assert\n"
                        "  0x50xxxx   → LMAC TCM sub-CPU assert\n"
                        "  0x00xxxx   → LMAC direct assert\n"
                        "Call this whenever you see 'assert', 'ASSERT', or a hex code after "
                        "'code=' in the logs."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "code": {
                                "type": "string",
                                "description": (
                                    "Raw assert code from the log, as a hex string "
                                    "e.g. '0x20100505' or '0x34'"
                                )
                            }
                        },
                        "required": ["code"]
                    }
                }
            },
            {
                "type": "function",
                "function": {
                    "name": "softAP_supported_channel",
                    "description": (
                        "Analyze the SoftAP supported channels per country/region from the currently loaded log. "
                        "Takes no arguments — the server reads the full raw log internally. "
                        "Do NOT pass log_text; you do not have the full raw log in context."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {},
                        "required": []
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
        tools = [
            tool for tool in tools
            if tool["function"]["name"] not in self.capabilities.disabled_tools
        ]
        if not self.capabilities.ace_playbooks:
            report = next(
                tool["function"] for tool in tools
                if tool["function"]["name"] == "submit_final_report"
            )
            properties = report["parameters"]["properties"]
            properties.pop("applied_bullet_ids", None)
            properties.pop("flagged_bullet_ids", None)
        return tools
