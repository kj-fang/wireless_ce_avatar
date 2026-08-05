"""Network Experience profile built on the shared Wi-Fi agent engine."""

from __future__ import annotations

from datetime import datetime

from services.chatbot.agent.system import (
    NW_AGENT_POLICY,
    Skill,
    WifiLogAgentSystem,
)


class NwAnalysisAgentSystem(WifiLogAgentSystem):
    """NW-specific product behavior without a second copy of the engine."""

    CAPABILITY_POLICY = NW_AGENT_POLICY

    def fetch_filtered_logs(self, skill_name: str) -> str:
        """Return NW's uncollapsed skill evidence payload."""
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
        if filtered_lines.startswith("Error:") or filtered_lines.startswith("No log lines"):
            return filtered_lines

        body_lines = self._extract_lines_from_filtered_blob(filtered_lines)
        compact_lines = []
        for line in body_lines:
            _, ts_display, message = self._normalize_time_message(line)
            compact_lines.append(f"<{ts_display}> {message}".strip())

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

    def _build_analyze_system_prompt(self, context_section: str) -> str:
        """Build the shorter six-step prompt used by the NW page."""
        return (
            f"{context_section}"
            "You are an Elite Wi-Fi Diagnostic Detective. Your GOAL: Find the REAL Root Cause based on evidence.\n"
            "Available skills:\n"
            + "".join(
                f"  - {skill['name']}: {skill['description']}\n"
                for skill in self.get_skill_descriptions()
                if skill.get("description")
            )
            + "\n"
            "PHASE 1 (SYMPTOM LOCALIZATION):\n"
            "   - Use the most relevant skill to analyze the logs by calling `fetch_filtered_logs`.\n"
            "PHASE 2 (SOURCE RETROSPECTIVE - optional):\n"
            "   - If needed, use additional skills to get more detail from the logs.\n"
            "PHASE 3. Call `submit_final_report` to conclude.\n\n"
            "CRITICAL CONSTRAINTS:\n"
            "- Max step is 6, and use at most 2 skills per step.\n"
            "- NO REPETITION: Do not fetch the same data twice.\n"
            "- IMMEDIATELY call `submit_final_report` after your detail query. Do not over-analyze.\n\n"
            "Your `markdown_summary` format (REQUIRED):\n"
            "  # Executive Summary\n  (1-2 sentences about the true root cause found in Phase 2)\n\n"
            "  | Aspect | Finding |\n"
            "  |--------|---------|\n"
            "  | Signal | ... |\n\n"
            "  ## Timeline\n"
            "  - T-Ns: Trigger Event (The Source)\n"
            "  - T+0s: Physical Failure begins\n"
            "  - T+Ns: Final Termination\n\n"
            "  ## Recommendations\n"
            "  **P0 (Urgent):** ...\n"
            "  **P1 (Important):** ...\n"
            "  **P2 (Nice-to-have):** ..."
        )

    def prime_with_context(self, case_nbr: str = "", subject: str = "",
                           description: str = "", issue_type: str = "",
                           attachment_time: str = "") -> None:
        """Prime NW context using its legacy local-clock parsing contract."""
        self.reset_conversation()
        self.issue_context = {
            "case_nbr": case_nbr,
            "subject": subject,
            "description": description,
            "issue_type": issue_type,
        }

        if attachment_time:
            self.issue_time = None
            self._issue_time_time_only = False
            formats = [
                ("%m/%d/%Y-%H:%M:%S", False),
                ("%m/%d/%Y %H:%M:%S", False),
                ("%Y-%m-%dT%H:%M:%S", False),
                ("%Y-%m-%d %H:%M:%S", False),
                ("%Y-%m-%d %H:%M", False),
                ("%m/%d/%Y-%H:%M:%S.%f", False),
                ("%H:%M:%S", True),
                ("%H:%M", True),
            ]
            for fmt, is_time_only in formats:
                try:
                    parsed = datetime.strptime(attachment_time, fmt)
                except ValueError:
                    continue
                if is_time_only:
                    self.issue_time = datetime.combine(datetime.now().date(), parsed.time())
                    self._issue_time_time_only = True
                else:
                    self.issue_time = parsed
                break
        else:
            self._issue_time_time_only = False

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
                    "You are a Wi-Fi troubleshooting assistant with expert-level knowledge.\n"
                    f"Available skills: {', '.join(self.skills.keys())}.\n"
                    "Use fetch_filtered_logs with the most relevant skill(s), then call "
                    "submit_final_report.\n\n"
                    "=== Case Context ===\n"
                    + "\n".join(context_parts)
                ),
            })


__all__ = ["NwAnalysisAgentSystem", "Skill"]
