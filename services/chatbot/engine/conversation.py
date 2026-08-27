"""Conversation orchestration behavior for the shared chatbot agent."""

from __future__ import annotations

import json
from typing import Any, Optional


class ConversationMixin:
    """Conversation behavior for the composed agent."""

    # ---- Conversation persistence (ported from main PR #133) ----------
    # History now stores the full reasoning trace, so a resumed chat can
    # be grounded in the evidence the earlier turns gathered rather than
    # just their prose. Lives on the mixin because it is history
    # behaviour; the byte budget it honours is a class constant on
    # WifiLogAgentSystem (MAX_PERSISTED_CONTEXT_CHARS in system.py).
    @staticmethod
    def _plain_message(m) -> Optional[dict]:
        """Flatten one history entry into a plain, JSON-safe message dict.

        ``conversation_history`` holds a mix of dicts we appended ourselves and
        raw SDK message objects straight off the response, so this reads both
        shapes through attribute-or-key access and keeps only the fields the
        API round-trips: the tool_calls ids and arguments especially, since
        those are what pair an assistant tool_use with its tool results.
        """
        def field(key):
            return m.get(key) if isinstance(m, dict) else getattr(m, key, None)

        role = field("role")
        tool_calls = field("tool_calls") or []
        if not role:
            # A raw assistant SDK object can read back with no role; if it
            # carries tool_calls it is an assistant turn by construction.
            role = "assistant" if tool_calls else None
        if not role:
            return None

        out: dict = {"role": role, "content": field("content")}
        calls = []
        for tc in tool_calls:
            def sub(obj, key):
                return obj.get(key) if isinstance(obj, dict) else getattr(obj, key, None)
            fn = sub(tc, "function")
            call_id = sub(tc, "id")
            name = sub(fn, "name") if fn is not None else None
            arguments = sub(fn, "arguments") if fn is not None else None
            if not call_id or not name:
                continue
            calls.append({
                "id": call_id,
                "type": sub(tc, "type") or "function",
                "function": {"name": name, "arguments": arguments or "{}"},
            })
        if calls:
            out["tool_calls"] = calls
        tool_call_id = field("tool_call_id")
        if tool_call_id:
            out["tool_call_id"] = tool_call_id
            name = field("name")
            if name:
                out["name"] = name
        # An assistant message with tool_calls legitimately has no content;
        # anything else with neither content nor tool_calls carries nothing.
        if out["content"] is None and "tool_calls" not in out:
            return None
        return out

    def export_conversation_context(self, max_chars: Optional[int] = None) -> list[dict]:
        """Snapshot the model-facing conversation so it can be resumed later.

        This is NOT the UI trace — it is what actually goes to the API on the
        next request: the primed case context, the questions, the assistant
        turns and the tool results they were grounded in.

        Trimmed to a character budget from the OLDEST end, and only ever at a
        group boundary: an assistant tool_use and the tool results answering it
        are kept or dropped together, so a restored context can never open with
        an orphan. The head — the priming message the conversation opened with
        — is always kept; it is small and it is what tells the model which case
        this is.
        """
        budget = self.MAX_PERSISTED_CONTEXT_CHARS if max_chars is None else max_chars
        history = self.conversation_history or []

        # Walk into groups: [assistant-with-tool_calls + its tool results] or
        # [single message]. Grouping mirrors _repair_tool_use_consistency so
        # the two agree on what a severable unit is.
        groups: list[list[dict]] = []
        i, n = 0, len(history)
        while i < n:
            plain = self._plain_message(history[i])
            call_ids = set(self._msg_tool_call_ids(history[i]))
            if call_ids and plain is not None:
                group = [plain]
                j = i + 1
                while j < n and self._msg_role(history[j]) == "tool":
                    tool_plain = self._plain_message(history[j])
                    if tool_plain is not None:
                        group.append(tool_plain)
                    j += 1
                groups.append(group)
                i = j
                continue
            if plain is not None and self._msg_role(history[i]) != "tool":
                groups.append([plain])
            i += 1

        if not groups:
            return []

        def size(group: list[dict]) -> int:
            try:
                return len(json.dumps(group, ensure_ascii=False, default=str))
            except Exception:
                return sum(len(str(msg)) for msg in group)

        head, tail = groups[0], groups[1:]
        total = size(head)
        kept: list[list[dict]] = []
        # Newest-first so the most recent evidence is what survives the budget.
        for group in reversed(tail):
            group_size = size(group)
            if total + group_size > budget:
                break
            kept.append(group)
            total += group_size
        kept.reverse()

        out: list[dict] = list(head)
        for group in kept:
            out.extend(group)
        return out

    def import_conversation_context(self, messages: Any) -> int:
        """Restore a context produced by ``export_conversation_context``.

        Returns how many messages were adopted (0 when there was nothing
        usable, so the caller can fall back to rebuilding from result text).
        The restored list is run through the same repair pass every request
        gets, so a snapshot that was truncated or hand-edited into an invalid
        state degrades to a smaller valid context instead of a 400.
        """
        if not isinstance(messages, list):
            return 0
        restored = [m for m in messages if isinstance(m, dict) and m.get("role")]
        if not restored:
            return 0
        self.conversation_history = restored
        try:
            self._repair_tool_use_consistency()
        except Exception as e:
            print(f"[chat] restored context repair failed: {e}")
        return len(self.conversation_history)

    def chat(self, user_message: str, use_tools: bool = False, max_steps: int = 6,
             temperature: float = 0.2, max_tokens: int = 4000, step_callback=None) -> dict:
        """
        Process user message with flexible LLM call - simple or agentic mode.
        
        Two modes available:
          
          MODE 1: Simple Conversation (use_tools=False, DEFAULT)
            - Direct LLM call, no tools
            - Perfect for free-form Q&A chatbot
            - Faster, fewer tokens
          
          MODE 2: Agentic Reasoning (use_tools=True)
            - LLM can call diagnostic tools
            - Autonomous skill selection and investigation
            - For complex root-cause analysis
        
        Args:
            user_message: User's question or statement
            use_tools: Enable agentic tool mode (default False for simple chat)
            max_steps: Max reasoning iterations when use_tools=True (default 6)
            temperature: Sampling temperature for response generation
            max_tokens: Maximum tokens for direct/simple response generation
            
        Returns:
            dict: {
                "type": "text" | "report" | "error",
                "data": str or dict depending on mode
            }
            
        Examples:
            # Simple chatbot (default, no tools)
            >>> result = agent.chat("What errors are in the log?")
            >>> print(result["data"])  # Direct answer
            
            # With tools for diagnosis
            >>> result = agent.chat(
            ...     "Why does device disconnect?",
            ...     use_tools=True
            ... )
            >>> # Agent may call fetch_filtered_logs, query_log_detail, etc.
        """
        try:
            temperature = float(temperature)
        except Exception:
            temperature = 0.2
        temperature = max(0.0, min(1.0, temperature))

        try:
            max_tokens = int(max_tokens)
        except Exception:
            max_tokens = 4000
        max_tokens = max(256, min(8000, max_tokens))

        # Fresh turn — discard any stop signal left over from a previous turn
        # so the user's new message is never pre-cancelled.
        if self.capabilities.cooperative_cancellation:
            self.cancel_event.clear()

        # Fresh turn — token counters describe THIS turn only.
        self._reset_turn_usage()

        # Delegate to appropriate implementation
        if use_tools:
            return self._chat_with_tools(user_message, max_steps, temperature=temperature, step_callback=step_callback)
        else:
            return self._chat_simple(user_message, temperature=temperature, max_tokens=max_tokens)

    def _chat_simple(self, user_message: str, temperature: float = 0.2,
                     max_tokens: int = 4000) -> dict:
        """
        Simple chat mode: Direct conversation without tools.
        
        Perfect for chatbot UI where users expect immediate, conversational responses.
        """
        if not self.conversation_history:
            # Initialize system message with context on first turn
            log_snippet = ""
            if self.current_log_path:
                try:
                    from utils.helpers import read_log_file
                    lines = read_log_file(self.current_log_path)
                    log_snippet = "\n".join(str(l) for l in lines[:500])
                except Exception:
                    log_snippet = "(unable to read log file)"

            # Build comprehensive system message
            system_msg = (
                "You are a Wi-Fi Troubleshooting Assistant.\n"
                "Answer user questions about the log file concisely and accurately.\n"
            )
            
            # Add case context if available
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
            
            # Add log file reference
            if self.current_log_path:
                system_msg += f"Log file: {self.current_log_path}\n"
            
            # Add log snippet for reference
            if log_snippet:
                system_msg += f"\n=== Log Excerpt (first 500 lines) ===\n{log_snippet}\n"

            self.conversation_history.append({
                "role": "system",
                "content": system_msg,
            })

        # Add user message to history
        self.conversation_history.append({"role": "user", "content": user_message})

        try:
            # Simple LLM call (no tools)
            response = self.client.chat.completions.create(
                model=self.model,
                messages=self.conversation_history,
                temperature=temperature,
                max_tokens=max_tokens,
            )
            self._accumulate_turn_usage(getattr(response, "usage", None))
            content = response.choices[0].message.content or ""

            # Add assistant response to history
            self.conversation_history.append({"role": "assistant", "content": content})
            
            return {"type": "text", "data": content}
        except Exception as e:
            error_msg = f"Chat error: {str(e)}"
            print(f"[ERROR] {error_msg}")
            return {"type": "text", "data": error_msg}

    @staticmethod
    def _msg_role(m):
        return m.get("role") if isinstance(m, dict) else getattr(m, "role", None)

    @staticmethod
    def _msg_tool_call_ids(m):
        """Return the list of tool_call ids on an assistant message
        (works for both plain dicts and the OpenAI SDK message object)."""
        tcs = m.get("tool_calls") if isinstance(m, dict) else getattr(m, "tool_calls", None)
        ids = []
        for tc in (tcs or []):
            tid = tc.get("id") if isinstance(tc, dict) else getattr(tc, "id", None)
            if tid:
                ids.append(tid)
        return ids

    @staticmethod
    def _msg_tool_call_id(m):
        """tool_call_id of a role:\"tool\" message (dict or SDK object)."""
        return m.get("tool_call_id") if isinstance(m, dict) else getattr(m, "tool_call_id", None)

    def _repair_tool_use_consistency(self) -> None:
        """Rebuild conversation_history so every assistant ``tool_use`` is
        immediately followed by ``tool_result`` message(s) answering ALL of its
        ids — the invariant the (Claude-backed) API enforces.

        It drops only the broken parts, keeping valid context intact:
          * an assistant message whose tool_calls are NOT all answered by the
            tool messages right after it — dropped together with those partial
            tool results;
          * a stray ``role:"tool"`` message with no owning assistant tool_use.

        Called right before every LLM request, so an orphan from ANY source —
        an aborted prior turn, a per-step token-limit bail-out, a mid-history
        injection (e.g. after editing a skill) — can never reach the API and
        trigger a 400 ("tool_use ids ... without tool_result blocks ...").
        """
        hist = self.conversation_history or []
        n = len(hist)
        out = []
        i = 0
        dropped = 0
        while i < n:
            m = hist[i]
            # Identify a tool-use message by its tool_calls, NOT by role: the
            # raw assistant SDK message object does not reliably expose `.role`
            # to our helpers (it can read back as None). Classifying by role
            # would miss it and then wrongly drop its valid tool_results.
            call_ids = set(self._msg_tool_call_ids(m))
            if call_ids:
                # Consume the immediately-following run of tool results and keep
                # ONLY those that match THIS message's ids (exactly once each);
                # extras / duplicates / mismatched ids are dropped.
                j = i + 1
                matched = []
                seen = set()
                while j < n and self._msg_role(hist[j]) == "tool":
                    tid = self._msg_tool_call_id(hist[j])
                    if tid in call_ids and tid not in seen:
                        matched.append(hist[j])
                        seen.add(tid)
                    else:
                        dropped += 1   # extra / duplicate / mismatched tool result
                    j += 1
                if seen == call_ids:
                    out.append(m)
                    out.extend(matched)
                else:
                    # Not every tool_use was answered → drop the message AND its
                    # partial tool results (can't send an unanswered tool_use).
                    dropped += 1 + len(matched)
                i = j
                continue
            if self._msg_role(m) == "tool":
                # A tool result not consumed by a tool-use run above = orphan.
                dropped += 1
                i += 1
                continue
            out.append(m)
            i += 1
        if dropped:
            print(f"[chat] 🧹 Repaired tool_use/tool_result consistency — "
                  f"dropped {dropped} orphan/stray message(s) before sending.")
            self.conversation_history = out

    def _history_skeleton(self) -> str:
        """Compact one-line-per-message view of conversation_history showing
        index + role + tool id(s). Dumped when an LLM request fails so a
        tool_use/tool_result mismatch can be pinpointed by message index and id
        (e.g. the API's "messages.N: tool_use ids ... without tool_result")."""
        lines = []
        for idx, m in enumerate(self.conversation_history or []):
            role = self._msg_role(m)
            ids = self._msg_tool_call_ids(m)
            tcid = self._msg_tool_call_id(m)
            if ids:  # tool-use message (classify by tool_calls, role may be None)
                lines.append(f"  [{idx}] {role or 'assistant?'} tool_use={[s[:12] for s in ids]}")
            elif tcid:
                lines.append(f"  [{idx}] {role or 'tool?'} tool_result={tcid[:12]}")
            else:
                lines.append(f"  [{idx}] {role}")
        return "\n".join(lines) if lines else "  (empty)"

    def _chat_with_tools(self, user_message: str, max_steps: int = 6,
                         temperature: float = 0.1, step_callback=None) -> dict:
        """
        Agentic chat mode: LLM can use diagnostic tools.
        
        For complex analysis where agent needs to investigate multiple skills,
        inspect specific log sections, and provide structured diagnoses.

        This method detects follow-up turns (conversation_history already has
        messages) and appends the new user message so the LLM can continue
        investigating with full context of prior analysis.
        """
        def _emit(step):
            if step_callback:
                step_callback(step)

        tools = self._build_tools()
        final_report = None

        # Self-heal: surgically drop any orphan tool_use/tool_result left by a
        # prior aborted turn (LLM error, tool-arg JSON parse failure, per-step
        # token-limit bail-out, etc.) so the API can't 400 on it. Keeps valid
        # context — unlike a full conversation reset.
        if self.capabilities.repair_tool_history:
            self._repair_tool_use_consistency()

        # Detect first user turn: prime_with_context may have added a system
        # message but no user message yet — treat that as first turn so full
        # initialization (system prompt rebuild, issue-time extraction, log
        # preprocessing) still runs.
        _first_user_turn = not any(
            (isinstance(m, dict) and m.get("role") == "user")
            for m in self.conversation_history
        )

        if _first_user_turn:
            # First user turn — rebuild system prompt for agentic mode
            # (replaces any simpler prompt from prime_with_context).
            self.conversation_history = []

            context_section = ""
            if self.issue_context:
                context_parts = []
                if self.issue_context.get("case_nbr"):
                    context_parts.append(f"**Case Number:** {self.issue_context.get('case_nbr')}")
                if self.issue_context.get("issue_type"):
                    context_parts.append(f"**Issue Type:** {self.issue_context.get('issue_type')}")
                if self.issue_context.get("subject"):
                    context_parts.append(f"**Subject:** {self.issue_context.get('subject')}")
                if context_parts:
                    context_section = "\n=== BACKGROUND CONTEXT ===\n" + "\n".join(context_parts) + "\n\n"

            system_content = self._build_analyze_system_prompt(context_section)

            self.conversation_history.append({
                "role": "system",
                "content": system_content,
            })

            # Surface the ACE workflow playbook that was injected into the
            # system prompt, so users can see exactly which learned rules are
            # steering the agent on this turn.
            if self.capabilities.ace_playbooks and self.ace_runner is not None:
                try:
                    _wf_text = self.ace_runner.render_workflow()
                except Exception as _e:
                    _wf_text = ""
                    print(f"[ace] render_workflow (ui emit) failed: {_e}")
                if _wf_text and _wf_text.strip() not in ("", "(empty playbook)"):
                    _emit({
                        "role": "agent",
                        "content": (
                            "🧠 **ACE Workflow Playbook injected** "
                            "(orchestration rules learned from past cases)\n\n"
                            f"```\n{_wf_text}\n```"
                        ),
                    })

            self.conversation_history.append({"role": "user", "content": user_message})

            # --- Issue time extraction ---
            # By the time we get here `self.issue_time` is normally already
            # set: prime_with_context resolved it from attachment_time (and
            # fell back to the log's latest timestamp if needed), and the
            # /chat route may have overridden it with the sidebar value.
            # Only run the LLM-based extractor if everything upstream came
            # back empty — and try the user's message first.
            if self.issue_time:
                time_source = self.capabilities.primed_issue_time_source
            else:
                self.issue_time = self._extract_issue_time(user_message)
                time_source = "user_message"

            if not self.issue_time:
                for context_key in self.capabilities.context_issue_time_fallbacks:
                    context_value = self.issue_context.get(context_key)
                    if context_value:
                        self.issue_time = self._extract_issue_time(context_value)
                        time_source = f"issue_context.{context_key}"
                    if self.issue_time:
                        break

            if self.issue_time:
                # Add a "customer wall clock" annotation when the issue time
                # frame detection produced a different customer-side value
                # (typical for non-Asia customers — the log shows GMT+8, the
                # customer's screenshot shows their own clock). prime_with_
                # context stamps these on self when it resolves the frames.
                customer_dt = getattr(self, "issue_time_customer", None)
                customer_tz = (getattr(self, "issue_time_tz", "") or "").strip()
                extracted = self.issue_time.strftime('%m/%d/%Y %H:%M:%S')
                has_customer = bool(self.capabilities.show_customer_issue_time
                                    and customer_dt and customer_tz
                                    and customer_dt != self.issue_time)
                if self.capabilities.compact_issue_time_notice:
                    content = (
                        f"Issue Time Extracted: `{extracted}`\n"
                        f"(source: {time_source})\n"
                        "Agent will look for events around this timestamp in filtered logs."
                    )
                elif has_customer:
                    # Two clean lines, no nested parentheses: the log-frame
                    # value (what we scan) on top, the customer wall clock below.
                    content = (
                        f"🕒 **Issue Time Extracted:** `{extracted}` — ETL decode-host time (GMT+8)\n"
                        f"👤 **Customer wall clock:** "
                        f"`{customer_dt.strftime('%m/%d/%Y %H:%M:%S')}` — {customer_tz}\n"
                        f"Agent will look for events around this timestamp in filtered logs. "
                        f"_(source: {time_source})_"
                    )
                else:
                    content = (
                        f"🕒 **Issue Time Extracted:** `{extracted}`\n"
                        f"Agent will look for events around this timestamp in filtered logs. "
                        f"_(source: {time_source})_"
                    )
                _emit({"role": "agent", "content": content})

            # --- Raw log preprocessing (scope-narrowing) ---
            load_err = self._ensure_raw_log_cache()
            if load_err:
                _emit({"role": "error", "content": f"❌ Raw log load failed: {load_err}"})
            else:
                self._preprocess_raw_log_context()
                pre_msg_parts = [
                    f"- **Segment1 — Driver init block:** {len(self._driver_init_lines)} lines "
                    f"(driver load occurrences: {self._driver_init_count})",
                    f"- **Segment2 — Event window:** {len(self._issue_time_window_lines)} lines",
                ]
                if self._scoped_log_lines:
                    filter_scope = (
                        f"{len(self._scoped_log_lines)} lines (scoped: Segment1 + issue-time window)"
                        if self.issue_time
                        else f"{len(self._raw_log_cache)} lines (full raw log — no issue time)"
                    )
                    pre_msg_parts.append(f"- **Skill filter input:** {filter_scope}")
                _emit({
                    "role": "agent",
                    "content": "🔍 **Pre-Analysis Scan Complete**\n" + "\n".join(pre_msg_parts),
                })

                if self._scoped_log_lines:
                    seg2_scope = (
                        f"±5 min of issue time {self.issue_time.strftime('%m/%d/%Y %H:%M:%S')}"
                        if self.issue_time
                        else "Segment1 end → EOF"
                    )
                    self.conversation_history.append({
                        "role": "user",
                        "content": (
                            "[PRE-SCAN INFO]\n"
                            f"Skill filtering scope has been narrowed to {len(self._scoped_log_lines)} lines "
                            f"(full raw log: {len(self._raw_log_cache)} lines).\n"
                            f"Segment1 (driver init): {len(self._driver_init_lines)} lines | "
                            f"driver load occurrences: {self._driver_init_count}\n"
                            f"Segment2 ({seg2_scope}): {len(self._issue_time_window_lines)} lines"
                        ),
                    })
        else:
            # Follow-up turn — history already has prior analysis context.
            # Assembled-log caches are still warm so all tools work as normal.
            self.conversation_history.append({"role": "user", "content": user_message})

        # Per-call tracking
        skill_call_counts: dict = {}
        no_match_anchor_counts: dict = {}
        detail_call_counts: dict = {}
        no_progress_rounds: int = 0
        step_token_usages: list = []

        def _emit_token_report():
            if not step_token_usages:
                return
            rows = [
                "📊 **Token Usage Report**\n",
                "| Step | Prompt | Completion | Total |",
                "|------|--------|------------|-------|",
            ]
            total_p = total_c = total_t = 0
            for s in step_token_usages:
                rows.append(f"| {s['step']} | {s['prompt']:,} | {s['completion']:,} | {s['total']:,} |")
                total_p += s["prompt"]; total_c += s["completion"]; total_t += s["total"]
            rows.append(f"| **Total** | **{total_p:,}** | **{total_c:,}** | **{total_t:,}** |")
            _emit({"role": "token_usage", "content": "\n".join(rows)})

        for step_idx in range(max_steps):
            pending_user_nudges: list = []

            # Cooperative stop: the user clicked "Stop" and job_runtime set our
            # cancel_event. Bail out cleanly BEFORE spending another LLM call —
            # emit the token report and return a short notice.
            if (self.capabilities.cooperative_cancellation
                    and self.cancel_event.is_set()):
                _emit({"role": "agent", "content": "⏹️ **Stopped by user.** Analysis halted before completion."})
                _emit_token_report()
                return {"type": "text", "data": "⏹️ Analysis stopped by user."}

            _emit({"role": "agent", "content": f"💭 **Reasoning Step {step_idx + 1}/{max_steps}** — Thinking..."})

            # Force-conclude pressure in final steps
            if step_idx >= max_steps - self.FORCE_CONCLUDE_LAST_N_STEPS:
                pending_user_nudges.append({
                    "role": "user",
                    "content": (
                        "You are in the final steps. Stop gathering new evidence and call "
                        "submit_final_report now using current evidence. If uncertain, state "
                        "uncertainties explicitly in the report."
                    ),
                })

            # Guarantee the request never carries an orphan tool_use/tool_result
            # (created this turn or a prior one) — the #1 cause of the API's
            # "tool_use ids ... without tool_result blocks" 400.
            if self.capabilities.repair_tool_history:
                self._repair_tool_use_consistency()
            try:
                response = self.client.chat.completions.create(
                    model=self.model,
                    messages=self.conversation_history,
                    tools=tools,
                    tool_choice="auto",
                    temperature=temperature,
                    max_tokens=4096,
                )
            except Exception as e:
                error_msg = f"LLM API error at reasoning step {step_idx}: {str(e)}"
                print(f"[ERROR] {error_msg}")
                # Dump the role + tool-id skeleton so a tool_use/tool_result
                # mismatch can be pinpointed by message index + id.
                if self.capabilities.diagnose_history_on_llm_error:
                    print(f"[chat] conversation_history skeleton at failure "
                          f"({len(self.conversation_history)} msgs):\n{self._history_skeleton()}")
                return {"type": "error", "data": error_msg}

            message = response.choices[0].message
            self.conversation_history.append(message)

            # Token usage accounting
            usage = getattr(response, 'usage', None)
            self._accumulate_turn_usage(usage)
            if usage:
                print(
                    f"[TOKEN] Chat step {step_idx + 1}: "
                    f"prompt={usage.prompt_tokens} "
                    f"completion={usage.completion_tokens} "
                    f"total={usage.total_tokens}"
                )
                if self.capabilities.emit_step_token_usage:
                    _emit({
                        "role": "token_usage",
                        "content": (
                            f"Token Usage (Step {step_idx + 1}): "
                            f"Prompt: {usage.prompt_tokens} | "
                            f"Completion: {usage.completion_tokens} | "
                            f"Total: {usage.total_tokens}"
                        ),
                    })
                # _emit({
                #     "role": "token_usage",
                #     "content": (
                #         f"📊 **Token Usage (Step {step_idx + 1}):** "
                #         f"Prompt: {usage.prompt_tokens} | "
                #         f"Completion: {usage.completion_tokens} | "
                #         f"Total: {usage.total_tokens}"
                #     ),
                # })
                step_token_usages.append({
                    "step": step_idx + 1,
                    "prompt": usage.prompt_tokens,
                    "completion": usage.completion_tokens,
                    "total": usage.total_tokens,
                })
                if usage.total_tokens > self.MAX_TOKENS_PER_STEP:
                    _emit({
                        "role": "error",
                        "content": (
                            f"🛑 **Stopped:** per-step token limit exceeded at step {step_idx + 1}. "
                            f"total_tokens={usage.total_tokens}, limit={self.MAX_TOKENS_PER_STEP}."
                        ),
                    })
                    _emit_token_report()
                    return {
                        "type": "partial_report",
                        "issue_time": (
                            self.issue_time.strftime('%m/%d/%Y %H:%M:%S')
                            if self.issue_time else None
                        ),
                        "data": {
                            "root_cause_summary": "Analysis stopped due to per-step token limit.",
                            "confidence_score": 20,
                            "recommended_actions": [
                                "Narrow the question scope",
                                "Use simple mode for broad questions",
                            ],
                            "involved_skills": [],
                            "markdown_summary": (
                                "## Partial Result\n"
                                "Analysis stopped because a single reasoning step exceeded the token limit."
                            ),
                        },
                    }
                elif usage.total_tokens > int(self.MAX_TOKENS_PER_STEP * 0.85):
                    pending_user_nudges.append({
                        "role": "user",
                        "content": (
                            "Token budget is getting tight. "
                            "Avoid broad new searches; use current evidence and submit_final_report soon."
                        ),
                    })

            if message.content:
                _emit({"role": "agent", "content": f"🧠 **Thinking:**\n{message.content[:500]}"})

            if message.tool_calls:
                final_report = None
                original_tool_calls = list(message.tool_calls)

                # Tool fan-out cap (tighter in final steps)
                max_calls_this_step = self.MAX_TOOL_CALLS_PER_STEP
                if step_idx >= max_steps - self.FORCE_CONCLUDE_LAST_N_STEPS:
                    max_calls_this_step = 1
                if len(original_tool_calls) > max_calls_this_step:
                    _emit({
                        "role": "agent",
                        "content": (
                            f"🧭 **Tool cap applied:** executing {max_calls_this_step}/"
                            f"{len(original_tool_calls)} tool calls this step."
                        ),
                    })
                else:
                    _emit({"role": "agent", "content": f"🧭 **Tool calls:** {len(original_tool_calls)} this step."})

                tool_calls = original_tool_calls[:max_calls_this_step]
                skipped_tool_calls = original_tool_calls[max_calls_this_step:]

                # In final steps only allow submit_final_report; skip everything else
                if step_idx >= max_steps - self.FORCE_CONCLUDE_LAST_N_STEPS:
                    has_submit = any(tc.function.name == "submit_final_report" for tc in tool_calls)
                    if not has_submit:
                        for skipped_call in original_tool_calls:
                            self.conversation_history.append({
                                "role": "tool",
                                "tool_call_id": skipped_call.id,
                                "name": skipped_call.function.name,
                                "content": (
                                    "Skipped in final-step mode. "
                                    "Call submit_final_report immediately using existing evidence."
                                ),
                            })
                        pending_user_nudges.append({
                            "role": "user",
                            "content": "Call submit_final_report NOW with your current findings.",
                        })
                        self.conversation_history.extend(pending_user_nudges)
                        continue

                # Acknowledge skipped calls so the API sees a tool result for each
                for skipped_call in skipped_tool_calls:
                    self.conversation_history.append({
                        "role": "tool",
                        "tool_call_id": skipped_call.id,
                        "name": skipped_call.function.name,
                        "content": "Skipped (tool cap). Will be retried in a later step if still needed.",
                    })

                for tool_call in tool_calls:
                    try:
                        args = json.loads(tool_call.function.arguments)
                    except json.JSONDecodeError as e:
                        print(f"[ERROR] Failed to parse tool arguments: {e}")
                        # MUST still answer this tool_call — the assistant
                        # message already carries it, so skipping the
                        # response would leave an orphan tool_use and 400
                        # the very next API call. Reply with an error so
                        # the model can retry / recover.
                        if self.capabilities.recover_invalid_tool_arguments:
                            self.conversation_history.append({
                                "role": "tool",
                                "tool_call_id": tool_call.id,
                                "name": getattr(tool_call.function, "name", "unknown"),
                                "content": f"Error: could not parse tool arguments as JSON ({e}). "
                                           f"Please re-issue the call with valid JSON arguments.",
                            })
                        continue

                    if tool_call.function.name == "submit_final_report":
                        final_report = args
                        _emit({"role": "agent", "content": "✅ **Conclusion reached!** Generating report."})
                        self.conversation_history.append({
                            "role": "tool",
                            "tool_call_id": tool_call.id,
                            "name": "submit_final_report",
                            "content": "Final report received and processed successfully.",
                        })

                    elif tool_call.function.name == "fetch_filtered_logs":
                        skill_label = args.get("skill_name", "")

                        # Skill fetch cap
                        skill_call_counts[skill_label] = skill_call_counts.get(skill_label, 0) + 1
                        distinct = len([k for k, v in skill_call_counts.items() if v >= 1])
                        if distinct > self.MAX_SKILL_FETCHES:
                            msg = (
                                f"Skill fetch limit reached ({self.MAX_SKILL_FETCHES} distinct skills). "
                                "Synthesize findings from already-fetched skills and call submit_final_report."
                            )
                            _emit({"role": "agent", "content": f"⚠️ **Skill cap hit** — {msg}"})
                            self.conversation_history.append({
                                "role": "tool",
                                "tool_call_id": tool_call.id,
                                "name": tool_call.function.name,
                                "content": msg,
                            })
                            continue

                        _emit({"role": "agent", "content": f"🔍 **Fetching filtered logs** for `{skill_label}`..."})
                        tool_result = self._invoke_tool("fetch_filtered_logs", {"skill_name": skill_label})
                        preview = tool_result[:400].replace('\n', ' ') + "..."
                        if self.capabilities.emit_fetch_previews:
                            _emit({
                                "role": "tool",
                                "content": f"Logs loaded (`{skill_label}`):\n```\n{preview}\n```",
                            })
                        # _emit({"role": "tool", "content": f"📄 **Logs loaded** (`{skill_label}`):\n```\n{preview}\n```"})

                        # No-progress detection
                        if "New lines merged this round: 0" in tool_result or "Skill cache hit:" in tool_result:
                            no_progress_rounds += 1
                        else:
                            no_progress_rounds = 0

                        if skill_call_counts.get(skill_label, 0) >= 3:
                            pending_user_nudges.append({
                                "role": "user",
                                "content": (
                                    "Avoid repeatedly querying the same skill unless it adds new information. "
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

                        # Expert rules injection (+ ACE domain playbook for the
                        # same skill, once per case, gated by the same set so
                        # the token-saving omit-on-repeat behaviour applies to
                        # both).
                        skill_obj = self.skills.get(skill_label)
                        expert_rules = getattr(skill_obj, 'expert_rules', '') if skill_obj else ''
                        if expert_rules:
                            if skill_label not in self._chat_rules_injected_skills:
                                rules_section = (
                                    f"=== Expert Rules for {skill_label} ===\n{expert_rules}\n\n"
                                    "=== Rule Usage Instruction ===\n"
                                    "Use these expert rules as investigative clues.\n"
                                    "For each important claim, map each rule clue to concrete log evidence\n"
                                    "and decide: supported, refuted, or uncertain.\n\n"
                                )
                                ace_domain_block = (
                                    self._build_ace_domain_block(skill_label)
                                    if self.capabilities.ace_playbooks else ""
                                )
                                if ace_domain_block:
                                    _emit({
                                        "role": "agent",
                                        "content": (
                                            f"🧠 **ACE Domain Playbook injected for `{skill_label}`** "
                                            "(lessons from past cases)\n\n"
                                            f"```\n{ace_domain_block}```"
                                        ),
                                    })
                                rules_section += ace_domain_block
                                self._chat_rules_injected_skills.add(skill_label)
                            else:
                                rules_section = (
                                    f"=== Expert Rules for {skill_label} ===\n"
                                    "(already provided; omitted to save tokens)\n\n"
                                )
                            content = rules_section + self._clip_for_prompt(
                                f"=== Skill-Focused Evidence ({skill_label}) ===\n{tool_result}",
                                limit=self.MAX_TOOL_RESULT_CHARS_IN_MESSAGES,
                            )
                        else:
                            # No expert rules for this skill, but ACE may still
                            # have learned domain bullets — inject them so the
                            # playbook isn't silently dropped on cold skills.
                            ace_block = (
                                self._build_ace_domain_block(skill_label)
                                if (self.capabilities.ace_playbooks
                                    and skill_label not in self._chat_rules_injected_skills)
                                else ""
                            )
                            if ace_block:
                                self._chat_rules_injected_skills.add(skill_label)
                                _emit({
                                    "role": "agent",
                                    "content": (
                                        f"🧠 **ACE Domain Playbook injected for `{skill_label}`** "
                                        "(lessons from past cases)\n\n"
                                        f"```\n{ace_block}```"
                                    ),
                                })
                            content = ace_block + self._clip_for_prompt(
                                tool_result, limit=self.MAX_TOOL_RESULT_CHARS_IN_MESSAGES
                            )

                        self.conversation_history.append({
                            "role": "tool",
                            "tool_call_id": tool_call.id,
                            "name": tool_call.function.name,
                            "content": content,
                        })

                    else:
                        # Other tools: query_log_detail, get_assembled_log_snapshot, etc.
                        skill_label = args.get("skill_name") or tool_call.function.name

                        # Anti-loop for query_log_detail
                        if tool_call.function.name == "query_log_detail":
                            anchor_text = args.get("anchor_text", "")
                            anchor_ts = args.get("anchor_timestamp", "")
                            detail_sig = f"{anchor_text.lower()}|{anchor_ts}"
                            detail_call_counts[detail_sig] = detail_call_counts.get(detail_sig, 0) + 1

                        _emit({"role": "agent", "content": f"🔍 **Invoking** `{skill_label}`..."})
                        tool_result = self._invoke_tool(tool_call.function.name, args)

                        if tool_call.function.name == "query_log_detail":
                            if "No matching anchor found" in tool_result:
                                no_match_anchor_counts[detail_sig] = no_match_anchor_counts.get(detail_sig, 0) + 1
                                if no_match_anchor_counts[detail_sig] >= 2:
                                    pending_user_nudges.append({
                                        "role": "user",
                                        "content": (
                                            "You repeated an anchor query with no matches. "
                                            "Switch to a different anchor or synthesize from existing evidence."
                                        ),
                                    })
                            if detail_call_counts.get(detail_sig, 0) >= 3:
                                pending_user_nudges.append({
                                    "role": "user",
                                    "content": (
                                        "Detail queries are repeating similar anchors. "
                                        "Move from retrieval to judgment: reconcile timeline and conclude."
                                    ),
                                })

                        preview = tool_result[:400].replace('\n', ' ') + "..."
                        _emit({"role": "tool", "content": f"📄 **Result** (`{skill_label}`):\n```\n{preview}\n```"})
                        self.conversation_history.append({
                            "role": "tool",
                            "tool_call_id": tool_call.id,
                            "name": tool_call.function.name,
                            "content": self._clip_for_prompt(tool_result, limit=self.MAX_TOOL_RESULT_CHARS_IN_MESSAGES),
                        })

                if final_report is not None:
                    _emit_token_report()
                    return {
                        "type": "report",
                        "data": final_report,
                        "issue_time": (
                            self.issue_time.strftime('%m/%d/%Y %H:%M:%S')
                            if self.issue_time else None
                        ),
                    }

            else:
                # Agent gave a text answer with no tool calls
                content = message.content or ""
                _emit_token_report()
                return {"type": "text", "data": content}

            # Flush nudges into history so they take effect next step
            self.conversation_history.extend(pending_user_nudges)

        _emit_token_report()
        return {
            "type": "error",
            "data": f"Reached maximum reasoning steps ({max_steps}) without a definitive conclusion. "
                    "Try breaking down the question or asking more specific queries.",
            "issue_time": (
                self.issue_time.strftime('%m/%d/%Y %H:%M:%S')
                if self.issue_time else None
            ),
        }
