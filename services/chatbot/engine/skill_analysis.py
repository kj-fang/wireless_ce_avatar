"""Skill-level analysis behavior for the shared chatbot agent."""

from __future__ import annotations

import hashlib
import re
from datetime import datetime


class SkillAnalysisMixin:
    """SkillAnalysis behavior for the composed agent."""

    def _analyze_with_skill_prompt(self, skill: Skill, filtered_log: str) -> str:
        """
        Run a focused LLM call using this skill's expert_rules as the system
        prompt and the TAT-filtered log lines as the user message.
        The case issue description (if available) is prepended to give the LLM
        additional context about what problem is being investigated.
        Returns the LLM's analysis text.
        """
        if filtered_log.startswith("Error:") or filtered_log.startswith("No log lines"):
            return filtered_log

        # Build context preamble from stored issue_context
        context_lines = []
        issue_type  = self.issue_context.get("issue_type", "")
        description = self.issue_context.get("description", "")
        subject     = self.issue_context.get("subject", "")
        case_nbr    = self.issue_context.get("case_nbr", "")
        if case_nbr:
            context_lines.append(f"Case: {case_nbr}")
        if subject:
            context_lines.append(f"Subject: {subject}")
        if issue_type:
            context_lines.append(f"Issue type: {issue_type}")
        if description:
            context_lines.append(f"\nIssue description:\n{description}")
        issue_preamble = ("=== Issue Context ===\n" + "\n".join(context_lines) + "\n\n"
                          if context_lines else "")

        messages = [
            {
                "role": "system",
                "content": skill.expert_rules,
            },
            {
                "role": "user",
                "content": (
                    f"{issue_preamble}"
                    f"=== Filtered Log (skill: {skill.name}) ===\n"
                    f"{filtered_log}"
                ),
            },
        ]
        try:
            response = self.client.chat.completions.create(
                model=self.model,
                messages=messages,
                temperature=0.1,
                max_tokens=2000,
            )
            return response.choices[0].message.content or ""
        except Exception as e:
            return f"Skill analysis error: {e}"

    def fetch_filtered_logs(self, skill_name: str) -> str:
        """
        Fetch filtered logs for a specific skill (agentic tool).
        
        Returns compact skill-focused evidence while still merging full
        filtered lines into assembled storage.
        
        Args:
            skill_name: Skill identifier to apply filter
            
        Returns:
            str: Compact skill-focused payload for low-token reasoning
        """
        skill = self.skills.get(skill_name)
        if not skill:
            return f"Error: skill '{skill_name}' not found."
        
        if skill_name in self._filter_cache_by_skill:
            cached = self._filter_cache_by_skill[skill_name]
            export_path = self._export_assembled_log_file()
            total_count = len(self._assembled_log_text.splitlines()) if self._assembled_log_text else 0
            result = self._build_skill_focus_payload(
                skill_name=skill_name,
                new_added=0,
                total_count=total_count,
            ) + (
                "\n\n=== Cache Info ===\n"
                f"Skill cache hit: {skill_name}\n"
                f"Skill-filtered lines in cache: {cached.get('line_count', 0)}"
            )
            if export_path:
                result += f"\nSaved merged filter log: {export_path}"
            return result

        filtered_lines = self._get_filtered_log_lines(skill_name, apply_output_limit=False)

        # If filtering failed, return error as-is
        if filtered_lines.startswith("Error:") or filtered_lines.startswith("No log lines"):
            return filtered_lines

        body_lines = self._extract_lines_from_filtered_blob(filtered_lines)

        # Collapse burst-repeated lines that differ only in variable fields.
        # For each consecutive run of similar lines:
        #   - compact_lines (LLM payload): keep the first line as sample, then append
        #     a single summary "(×N similar — label: v1, v2, …)" showing only what changed.
        #   - deduped_body_lines (assembled log): keep first + last for detail storage.
        _VAR_RE = re.compile(
            r'(?:[0-9a-fA-F]{2}[:\-]){5}[0-9a-fA-F]{2}'  # MAC address
            r'|\b\d{1,3}(?:\.\d{1,3}){3}\b'               # IPv4 address
            r'|0x[0-9a-fA-F]+'                             # 0x hex literal
            r'|\b[0-9a-fA-F]{4,}\b'                        # bare hex string (4+ hex digits)
            r'|\b\d+\b'                                     # decimal integer
        )

        def _msg_pattern(msg: str) -> str:
            return _VAR_RE.sub('*', msg)

        def _variation_summary(run_lines: list) -> str:
            """Return '(×N similar — label: v1, v2, …)' for a run of similar lines."""
            msgs = [self._normalize_time_message(bl)[2] for bl in run_lines]
            all_tok = [_VAR_RE.findall(m) for m in msgs]
            if not all_tok or not all_tok[0]:
                return f"×{len(msgs)} identical lines"
            first_iters = list(_VAR_RE.finditer(msgs[0]))
            varying = []
            for pos in range(len(first_iters)):
                values = [tl[pos] for tl in all_tok if pos < len(tl)]
                if len(set(values)) <= 1:
                    continue
                # Label: last word before the token in the first message
                label = f"field{pos + 1}"
                before = msgs[0][:first_iters[pos].start()].rstrip(' \t=,(')
                lm = re.search(r'(\w+)\s*$', before)
                if lm:
                    label = lm.group(1)
                # Skip first value (already visible in the sample line); cap at 8
                rest = values[1:]
                val_str = (', '.join(rest) if len(rest) <= 8
                           else ', '.join(rest[:5]) + ', …, ' + rest[-1])
                varying.append(f"{label}: {val_str}")
            if varying:
                return f"(×{len(msgs)} similar — {'; '.join(varying)})"
            return f"(×{len(msgs)} identical lines)"

        deduped_body_lines: list = []
        compact_lines: list = []
        run_start = 0
        while run_start < len(body_lines):
            _, _, msg0 = self._normalize_time_message(body_lines[run_start])
            pat0 = _msg_pattern(msg0)
            run_end = run_start + 1
            while run_end < len(body_lines):
                _, _, msgN = self._normalize_time_message(body_lines[run_end])
                if _msg_pattern(msgN) == pat0:
                    run_end += 1
                else:
                    break
            run_len = run_end - run_start
            if run_len <= 2:
                deduped_body_lines.extend(body_lines[run_start:run_end])
                for line in body_lines[run_start:run_end]:
                    _, ts, msg = self._normalize_time_message(line)
                    compact_lines.append(f"<{ts}> {msg}".strip())
            else:
                # Assembled log: keep first + last
                deduped_body_lines.append(body_lines[run_start])
                deduped_body_lines.append(body_lines[run_end - 1])
                # Compact: sample line + single variation summary
                _, ts0, msg0_txt = self._normalize_time_message(body_lines[run_start])
                compact_lines.append(f"<{ts0}> {msg0_txt}".strip())
                compact_lines.append(f"    {_variation_summary(body_lines[run_start:run_end])}")
            run_start = run_end
        body_lines = deduped_body_lines

        self._filter_cache_by_skill[skill_name] = {
            "skill_name": skill_name,
            "line_count": len(compact_lines),
            "lines": compact_lines,
            "created_at": datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%SZ'),
        }

        new_added, total_count = self._merge_lines_into_assembled_log(body_lines, skill_name)
        skill_focus_report = self._build_skill_focus_payload(skill_name, new_added, total_count)
        export_path = self._export_assembled_log_file()

        result = (
            f"=== {skill.name} Filter Applied ===\n"
            f"Skill matched lines: {len(body_lines)}\n"
            f"Assembled total lines after merge: {total_count}\n\n"
            f"=== Skill-Focused Reasoning Payload ===\n"
            f"{skill_focus_report}"
        )
        if export_path:
            result += f"\n\nSaved merged filter log: {export_path}"
        return result

    def analyze_with_skill(self, skill_name: str) -> str:
        """
        Quick one-shot skill analysis: filter logs + analyze with expert rules.
        
        Unlike agentic chat, this directly applies skill expertise without
        multi-step reasoning. Useful for focused, quick analysis when you want
        the skill's expertise applied directly without agent reasoning.
        
        Args:
            skill_name: Which skill's expertise to apply
            
        Returns:
            str: Skill expert's analysis of the filtered logs
            
        Example:
            >>> analysis = agent.analyze_with_skill("Connectivity")
            >>> print(analysis)
            # "Based on the filtered Connectivity logs, the issue appears to be..."
        """
        skill = self.skills.get(skill_name)
        if not skill:
            return f"Error: skill '{skill_name}' not found."
        
        filtered_lines = self._get_filtered_log_lines(skill_name)
        return self._analyze_with_skill_prompt(skill, filtered_lines)

    def query_log_detail(self, anchor_text: str = "", anchor_timestamp: str = "",
                         context_span: int = 20, max_hits: int = 3) -> str:
        """
        Query detailed assembled-log context by semantic anchors instead of line numbers.

        Matching strategy:
          - If anchor_timestamp is provided, match lines containing that timestamp
            text (supports exact fragment match).
          - If anchor_text is provided, match lines containing that text
            (case-insensitive).
          - If both are provided, both conditions must match.

        Returns neighboring context around up to `max_hits` anchor matches.
        Source is assembled log only (not raw log).
        """
        anchor_text = (anchor_text or "").strip()
        anchor_timestamp = (anchor_timestamp or "").strip()
        context_span = self._resolve_context_span(anchor_text, context_span)

        if not anchor_text and not anchor_timestamp:
            return "Error: Provide anchor_text and/or anchor_timestamp for detail query."

        assembled_text = self._assembled_log_text or ""
        if not assembled_text.strip():
            return (
                "No assembled log is available yet. "
                "Call fetch_filtered_logs(skill_name) first."
            )

        assembled_sig = hashlib.md5(assembled_text.encode('utf-8')).hexdigest()[:12]
        cache_key = (
            f"text={anchor_text.lower()}|ts={anchor_timestamp}|"
            f"span={context_span}|hits={max_hits}|assembled={assembled_sig}"
        )
        dedup_key = (
            f"text={anchor_text.lower()}|ts={anchor_timestamp}|"
            f"span={context_span}|hits={max_hits}|assembled={assembled_sig}"
        )

        if dedup_key in self._detail_query_seen and cache_key in self._detail_cache:
            cached = self._detail_cache[cache_key]
            first_line = cached.splitlines()[0] if cached else "(no detail)"
            return (
                "Duplicate query skipped: same anchor parameters were already queried on current assembled snapshot.\n"
                f"Previous detail summary: {first_line}\n"
                "Use a different anchor_text/anchor_timestamp or broader snapshot query for new evidence.\n\n"
                "[Detail dedup cache hit]"
            )

        if cache_key in self._detail_cache:
            return self._detail_cache[cache_key] + "\n\n[Detail cache hit]"

        all_lines = assembled_text.splitlines()

        matched_indices = []
        lower_anchor = anchor_text.lower()
        normalized_ts = anchor_timestamp.strip("<>").strip()
        ts_token = f"<{normalized_ts}>" if normalized_ts else ""

        for idx, raw in enumerate(all_lines):
            line = str(raw)
            line_lower = line.lower()
            ts_ok = (
                (not normalized_ts)
                or (normalized_ts in line)
                or (ts_token and ts_token in line)
            )
            txt_ok = (not lower_anchor) or (lower_anchor in line_lower)
            if ts_ok and txt_ok:
                matched_indices.append(idx)

        # HEAD+TAIL sampling: always include earliest AND latest matches
        # to prevent blindspot where only early errors are seen.
        if len(matched_indices) > max_hits:
            head_count = max(1, max_hits // 3)       # ~1/3 from beginning
            tail_count = max_hits - head_count        # ~2/3 from end
            matched_indices = (
                matched_indices[:head_count]
                + matched_indices[-tail_count:]
            )
        elif len(matched_indices) > 0:
            pass  # use all matches as-is

        if not matched_indices:
            return (
                "No matching anchor found in assembled log. "
                f"anchor_text='{anchor_text}', anchor_timestamp='{anchor_timestamp}'."
            )

        sections = []
        for hit_no, idx in enumerate(matched_indices, start=1):
            start_idx = max(0, idx - context_span)
            end_idx = min(len(all_lines), idx + context_span + 1)

            block = []
            for i in range(start_idx, end_idx):
                marker = ">>>" if i == idx else "   "
                block.append(f"{marker} {str(all_lines[i])}")

            sections.append(
                f"=== Detail Hit {hit_no}/{len(matched_indices)} ===\n" + "\n".join(block)
            )

        detail_text = "\n\n".join(sections)
        if len(detail_text) > self.MAX_QUERY_DETAIL_OUTPUT_CHARS:
            detail_text = (
                detail_text[:self.MAX_QUERY_DETAIL_OUTPUT_CHARS]
                + "\n... (detail truncated for token safety)"
            )
        self._detail_cache[cache_key] = detail_text
        self._detail_query_seen.add(dedup_key)
        return detail_text
