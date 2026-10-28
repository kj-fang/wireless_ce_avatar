"""Tool schema and tool-call execution behavior for chatbot agents."""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import partial
from types import SimpleNamespace
from typing import Callable, Optional

from services.chatbot.engine.report_quality import _shared_prompt
from utils.assert_code_utils import lookup_assert_code
from utils.softAP_supported_channel import softAP_supported_channel


# ---------------------------------------------------------------------------
# Tool registry
#
# One table, two projections. `schema` is what the LLM is offered; `run` is
# what actually executes. Deriving both from the same entries means a tool
# cannot appear in the menu without a dispatch branch (or the reverse), and
# AgentCapabilityPolicy.disabled_tools can be validated against the registry
# at import time -- a misspelled entry used to disable nothing, silently,
# because the schema filter and the dispatch guard each just missed on a
# string.
#
# `schema=None` marks a tool that is dispatchable but never advertised:
# query_log_detail is reachable from a replayed transcript and has anti-loop
# handling in the reasoning loop, but is deliberately kept off the menu.
# ---------------------------------------------------------------------------


def _schema_fetch_filtered_logs(agent) -> dict:
    return {
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
                            "enum": list(agent.skills.keys()),
                            "description": "Which skill's filter to apply (e.g., 'Connectivity', 'Roaming')"
                        }
                    },
                    "required": ["skill_name"]
                }
            }
        }


def _schema_lookup_assert_code(agent) -> dict:
    return {
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
        }


def _schema_softap_supported_channel(agent) -> dict:
    return {
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
        }


def _schema_submit_final_report(agent) -> dict:
    schema = {
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
    if not agent.capabilities.ace_playbooks:
        properties = schema["function"]["parameters"]["properties"]
        properties.pop("applied_bullet_ids", None)
        properties.pop("flagged_bullet_ids", None)
    return schema


# The two snapshot tools below were withdrawn from the menu but are still
# answered by name, so a model working from an older transcript gets a
# redirect instead of "Unknown tool". Their original schemas:
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


def _run_fetch_filtered_logs(agent, args: dict) -> str:
    return agent.fetch_filtered_logs(args.get("skill_name", ""))


def _run_query_log_detail(agent, args: dict) -> str:
    anchor_text = args.get("anchor_text", "")
    anchor_timestamp = args.get("anchor_timestamp", "")
    context_span = agent._resolve_context_span(
        anchor_text,
        args.get("context_span", agent.DEFAULT_DETAIL_CONTEXT_SPAN),
    )
    max_hits = min(args.get("max_hits", 3), agent.MAX_DETAIL_HITS)
    return agent.query_log_detail(
        anchor_text=anchor_text,
        anchor_timestamp=anchor_timestamp,
        context_span=context_span,
        max_hits=max_hits,
    )


def _run_withdrawn_snapshot(agent, args: dict, _name: str = "") -> str:
    return (
        f"{_name} is disabled. "
        "Use fetch_filtered_logs(skill_name) to retrieve skill-focused evidence "
        "or query_log_detail(keyword) to search specific events."
    )


def _run_lookup_assert_code(agent, args: dict) -> str:
    return lookup_assert_code(args.get("code", ""))


def _run_softap_supported_channel(agent, args: dict) -> str:
    err = agent._ensure_raw_log_cache()
    if err:
        return err
    log_text = "\n".join(agent._raw_log_cache)
    if not log_text.strip():
        return "ERROR: Raw log is empty or unavailable."
    return softAP_supported_channel(log_text)


def _run_submit_final_report(agent, args: dict) -> str:
    # Terminal tool: the reasoning loop intercepts this call before dispatch,
    # so arriving here means it was invoked outside that path.
    return "submit_final_report is handled by the reasoning loop."


@dataclass(frozen=True)
class Tool:
    """One diagnostic tool: how it is advertised, and what it runs."""

    name: str
    run: Callable[..., str]
    schema: Optional[Callable[..., dict]] = None


TOOLS: tuple[Tool, ...] = (
    Tool("fetch_filtered_logs", _run_fetch_filtered_logs, _schema_fetch_filtered_logs),
    Tool("query_log_detail", _run_query_log_detail),
    Tool("get_assembled_log_snapshot",
         partial(_run_withdrawn_snapshot, _name="get_assembled_log_snapshot")),
    Tool("get_final_state_snapshot",
         partial(_run_withdrawn_snapshot, _name="get_final_state_snapshot")),
    Tool("lookup_assert_code", _run_lookup_assert_code, _schema_lookup_assert_code),
    Tool("softAP_supported_channel", _run_softap_supported_channel,
         _schema_softap_supported_channel),
    Tool("submit_final_report", _run_submit_final_report, _schema_submit_final_report),
)

TOOLS_BY_NAME: dict[str, Tool] = {t.name: t for t in TOOLS}
TOOL_NAMES: frozenset[str] = frozenset(TOOLS_BY_NAME)


def validate_disabled_tools(profile: str, disabled) -> None:
    """Fail at import time when a policy disables a tool that does not exist.

    The same trick handler_map() plays for route endpoints. Before this, a
    typo in disabled_tools was a silent no-op: the schema filter and the
    dispatch guard both just failed to match, leaving the tool fully enabled
    -- the exact opposite of what the policy asked for.
    """
    unknown = sorted(set(disabled) - TOOL_NAMES)
    if unknown:
        raise RuntimeError(
            "Profile '%s' disables unknown tool(s): %s. Known tools: %s"
            % (profile, ", ".join(unknown), ", ".join(sorted(TOOL_NAMES)))
        )


def _schema_probe():
    """Minimal stand-in for an agent, enough to render every schema once."""
    return SimpleNamespace(
        skills={},
        capabilities=SimpleNamespace(ace_playbooks=True),
    )


def _validate_registry() -> None:
    """Assert the registry key and the advertised tool name agree.

    Each name is written twice -- once as the Tool entry's key, once inside
    the schema handed to the model -- and nothing else compares them. If they
    drift, the model calls the advertised name, TOOLS_BY_NAME misses, and the
    call comes back as "Unknown tool" at runtime. Catch it at import instead.
    """
    probe = _schema_probe()
    for tool in TOOLS:
        if tool.schema is None:
            continue
        advertised = tool.schema(probe).get("function", {}).get("name")
        if advertised != tool.name:
            raise RuntimeError(
                "Tool registry mismatch: entry %r advertises itself as %r"
                % (tool.name, advertised)
            )


_validate_registry()


def _type_ok(value, json_type: str) -> bool:
    if json_type == "string":
        return isinstance(value, str)
    if json_type == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if json_type == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if json_type == "array":
        return isinstance(value, list)
    if json_type == "object":
        return isinstance(value, dict)
    return True          # unconstrained or a type we do not police


def validate_tool_args(tool: Tool, agent, args: dict) -> Optional[str]:
    """Check one tool call against its own schema; return a message or None.

    The provider enforces the schema on calls it generates, but not every
    call arrives that way: a replayed transcript, a hand-built request or a
    hallucinated name all reach dispatch unchecked. Reading the constraints
    off the schema rather than restating them means a new tool is covered the
    day it is registered.

    The return value is phrased for the model, because that is where it goes:
    the reasoning loop feeds it back as the tool result, so a bad call turns
    into a correctable message instead of a stack trace or a silent default.
    """
    if tool.schema is None:
        return None
    params = tool.schema(agent).get("function", {}).get("parameters", {}) or {}
    properties = params.get("properties", {}) or {}

    for name in params.get("required", []) or []:
        value = args.get(name)
        if value is None or (isinstance(value, str) and not value.strip()):
            return (
                f"{tool.name} requires '{name}'. "
                f"Re-issue the call with all of: {', '.join(params.get('required', []))}."
            )

    for name, value in args.items():
        spec = properties.get(name)
        if not isinstance(spec, dict) or value is None:
            continue          # extras are the provider's business, not ours
        expected = spec.get("type")
        if expected and not _type_ok(value, expected):
            return (
                f"{tool.name}: '{name}' must be a {expected}, "
                f"got {type(value).__name__}."
            )
        allowed = spec.get("enum")
        if allowed and value not in allowed:
            return (
                f"{tool.name}: '{name}' must be one of {', '.join(map(str, allowed))}. "
                f"Got {value!r}."
            )
    return None



class ToolExecutionMixin:
    """ToolExecution behavior for the composed agent."""

    def _build_analyze_system_prompt(self, context_section: str) -> str:
        """Build the agentic analysis system prompt used by _chat_with_tools.

        The identity/phases body and the report skeleton are per-profile
        Speclets: shared Markdown files domain experts edit without touching
        Python (see utils/speclets_utils.py). When the share has not been
        mirrored — off-VPN, or the background prime has not finished yet —
        this falls back to the built-in defaults, which are byte-identical
        to the prompts these agents carried inline before Speclets existed.
        """
        from services.chatbot.engine.speclet_defaults import default_speclet
        from utils.speclets_utils import get_speclet

        profile = self.capabilities.profile
        body = get_speclet(profile, "prompt") or default_speclet(profile, "prompt")
        report = get_speclet(profile, "report") or default_speclet(profile, "report")

        skills_block = "".join(
            f"  - {s['name']}: {s['description']}\n"
            for s in self.get_skill_descriptions()
            if s.get('description')
        )
        body = body.replace("{skills}", skills_block)

        # Gate on the policy flag rather than "is an AceRunner attached?": NW
        # deliberately runs without playbook context even on a build where one
        # happens to be wired up.
        ace_block = (
            self._build_ace_workflow_block()
            if self.capabilities.ace_playbooks
            else ""
        )
        return f"{context_section}{ace_block}{body}\n\n{report}"

    def _invoke_tool(self, tool_name: str, args: dict) -> str:
        """Centralized tool dispatch used by both chat and analyze flows.

        Second of the two gates over capabilities.disabled_tools: _build_tools
        keeps a disabled tool off the menu, this keeps it from running if it is
        called anyway (a replayed transcript, or a hallucinated name). Both
        gates now read the same registry, so they cannot disagree.
        """
        if tool_name in self.capabilities.disabled_tools:
            return f"{tool_name} is not available for {self.capabilities.profile} log analysis."

        tool = TOOLS_BY_NAME.get(tool_name)
        if tool is None:
            return f"Unknown tool: {tool_name}"
        complaint = validate_tool_args(tool, self, args)
        if complaint:
            return complaint
        return tool.run(self, args)

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
                    _shared_prompt("followup")
                    .replace("{skills}", ", ".join(self.skills.keys()))
                    .replace("{log_path}", str(self.current_log_path))
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
        """The menu handed to the LLM: every registered tool that has a schema
        and is not disabled for this profile."""
        return [
            tool.schema(self)
            for tool in TOOLS
            if tool.schema is not None
            and tool.name not in self.capabilities.disabled_tools
        ]

