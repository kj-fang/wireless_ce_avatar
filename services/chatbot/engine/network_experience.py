"""Network Experience profile built on the shared Wi-Fi agent engine."""

from __future__ import annotations

from datetime import datetime

from services.chatbot.engine.system import (
    NW_AGENT_POLICY,
    Skill,
    WifiLogAgentSystem,
)
from utils.issue_time_utils import resolve_issue_time


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

    # The shorter six-step analysis prompt NW used to define here is now
    # nw_prompt.md / nw_report.md under Speclets, with byte-identical
    # fallbacks in speclet_defaults.py. NW_AGENT_POLICY.ace_playbooks=False
    # keeps the playbook block out of it exactly as this override did.

    def prime_with_context(self, case_nbr: str = "", subject: str = "",
                           description: str = "", issue_type: str = "",
                           attachment_time: str = "") -> None:
        """Prime NW context, resolving issue time the same way BT does.

        The hand-rolled strptime ladder this used to carry (eight formats,
        time-only entries dated to *today*) is now resolve_issue_time, which
        BT already used: a time-only value is aligned to the LOG's date rather
        than today's, and a value that parses to nothing falls back to the
        log's latest timestamp instead of leaving issue_time unset. Ported
        from main; no tz frame conversion here, same as BT.
        """
        self.reset_conversation()
        self.issue_context = {
            "case_nbr": case_nbr,
            "subject": subject,
            "description": description,
            "issue_type": issue_type,
        }

        dt, src = resolve_issue_time(attachment_time, self.current_log_path)
        self.issue_time = dt
        self._issue_time_time_only = (src == "input_time_only")

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
