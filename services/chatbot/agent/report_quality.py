"""Report quality gates and ACE context behavior for chatbot agents."""

from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Optional


class ReportQualityMixin:
    """ReportQuality behavior for the composed agent."""

    def _extract_issue_time(self, issue_description: str) -> Optional[datetime]:
        # Fast path: deterministic regex parsing from issue_description text.
        text = (issue_description or "").strip()
        strict_match = re.search(
            r"(\d{1,2}/\d{1,2}/\d{4})[\s-](\d{1,2}):(\d{2}):(\d{2})(?:\.(\d{1,3}))?",
            text,
        )
        if strict_match:
            mmddyyyy = strict_match.group(1)
            hh = strict_match.group(2).zfill(2)
            minute = strict_match.group(3)
            second = strict_match.group(4)
            milli = (strict_match.group(5) or "000").ljust(3, "0")[:3]
            try:
                return datetime.strptime(
                    f"{mmddyyyy}-{hh}:{minute}:{second}.{milli}",
                    "%m/%d/%Y-%H:%M:%S.%f",
                )
            except ValueError:
                pass

        malformed_match = re.search(
            r"(\d{1,2}/\d{1,2}/\d{4})[\s-](\d{1,2}):(\d{2}):(\d{3})",
            text,
        )
        if malformed_match:
            mmddyyyy = malformed_match.group(1)
            hh = malformed_match.group(2).zfill(2)
            minute = malformed_match.group(3)
            sec_triplet = malformed_match.group(4)
            second = sec_triplet[:2]
            milli = (sec_triplet[2:] + "00")[:3]
            try:
                return datetime.strptime(
                    f"{mmddyyyy}-{hh}:{minute}:{second}.{milli}",
                    "%m/%d/%Y-%H:%M:%S.%f",
                )
            except ValueError:
                pass

        # Fallback: let the LLM extract timestamp from free-form issue text.
        prompt = (
            "Extract the exact date and time mentioned in the following user issue description.\n"
            "If a time is found, output ONLY the timestamp in 'MM/DD/YYYY-HH:MM:SS' format "
            "(e.g., 10/28/2025-11:25:50).\n"
            "If no time is mentioned, output 'NONE'.\n\n"
            f"User Description: {issue_description}"
        )
        try:
            response = self.client.chat.completions.create(
                model=self.model,
                messages=[{"role": "user", "content": prompt}],
                temperature=0
            )
            time_str = response.choices[0].message.content.strip()
            if time_str != "NONE":
                return datetime.strptime(f"{time_str}.000", "%m/%d/%Y-%H:%M:%S.%f")
        except Exception as e:
            print(f"[!] Time extraction failed: {e}")
        return None

    def _precheck_report_quality(self, issue_description: str, report: dict, evidence_tail: str) -> Optional[dict]:
        """Lightweight deterministic guardrails before LLM quality review."""
        issue_text = (issue_description or "").lower()
        report_text = (
            f"{report.get('root_cause_summary', '')}\n{report.get('markdown_summary', '')}"
        ).lower()
        tail_text = (evidence_tail or "").lower()

        asks_direct_question = ("?" in (issue_description or "")) or any(
            t in issue_text for t in ("why", "what", "how", "can", "could", "cannot", "can't")
        )
        answer_markers = (
            "because", "due to", "caused by", "no evidence", "not observed",
            "normal background", "maintenance", "works as expected", "healthy", "stable",
        )
        if asks_direct_question and not any(m in report_text for m in answer_markers):
            return {
                "approved": False,
                "reason": "Final conclusion does not directly answer the user's question.",
                "required_actions": [
                    "Start root_cause_summary with a direct answer to the user question.",
                    "Then provide evidence-based rationale.",
                ],
            }

        severe_claim_markers = (
            "fatal", "critical", "persistent failure", "cannot scan", "can't scan",
            "failed to scan", "crash", "assert", "bsod",
        )
        healthy_tail_markers = (
            "scan is allowed", "connected", "assoc_rsp", "probe_rx", "probe_tx",
            "beacon", "rssi", "allowed (true)",
        )
        hard_fail_tail_markers = (
            "deauth", "task_disconnect", "termination", "bsod", "assert", "fw crash",
        )

        severe_claim = any(m in report_text for m in severe_claim_markers)
        healthy_tail_hits = sum(1 for m in healthy_tail_markers if m in tail_text)
        hard_fail_tail = any(m in tail_text for m in hard_fail_tail_markers)

        if severe_claim and healthy_tail_hits >= 2 and not hard_fail_tail:
            return {
                "approved": False,
                "reason": "Report likely overstates severity: tail evidence suggests normal background maintenance or healthy final state.",
                "required_actions": [
                    "Re-check latest log tail before concluding persistent failure.",
                    "Separate transient/background maintenance from fatal root cause.",
                ],
            }

        return None

    def _review_report_quality(self, issue_description: str, report: dict) -> dict:
        """
        Generic quality gate for final report, avoiding case-specific hardcoding.
        Checks temporal consistency and contradiction risk against compact assembled evidence.
        """
        try:
            evidence = self.get_assembled_log_snapshot(mode="compact")
            evidence_tail = self._get_assembled_log_tail(max_lines=120)
            deterministic = self._precheck_report_quality(issue_description, report, evidence_tail)
            if deterministic:
                return deterministic
            report_text = json.dumps(report, ensure_ascii=False)
            prompt = (
                "You are a diagnostic quality auditor. Evaluate whether the proposed final report "
                "is sufficiently supported by evidence and temporally consistent.\n"
                "Do NOT require domain-specific keywords. Apply generic checks only:\n"
                "1) Claims must be tied to explicit evidence.\n"
                "2) Early failures must be checked against later state to avoid stale conclusions.\n"
                "3) Detect state transitions (e.g., unavailable -> available, fail -> success). "
                "If transition exists, avoid absolute failure conclusions.\n"
                "4) If contradictions or evidence gaps exist, require uncertainty wording.\n"
                "5) Prefer latest confirmed state over earlier transient state.\n"
                "6) Before approving any persistent failure claim, verify latest log tail for success "
                "signals of the same target (for example probe/connected-like evidence).\n"
                "7) Treat explicit gate-status lines like '<feature> is ALLOWED/DISALLOWED/ENABLED/DISABLED' "
                "as high-priority state indicators; prefer the latest state bit.\n"
                "8) Apply hierarchy-of-truth conflict resolution: capability state > physical events > task intent > warning/error.\n"
                "If a lower layer conflicts with a higher layer, reject absolute lower-layer conclusions.\n"
                "9) Confirm that skill rules were used as investigative clues and validated/refuted by logs; "
                "rules are not ground truth by themselves.\n"
                "10) The report MUST directly answer the user's question in the first sentence.\n"
                "11) Distinguish transient/background maintenance behavior from persistent fatal failures.\n"
                "Return strict JSON only with this schema:\n"
                "{\"approved\": true|false, \"reason\": \"...\", \"required_actions\": [\"...\"]}\n\n"
                f"Issue:\n{issue_description}\n\n"
                f"Evidence (compact assembled snapshot):\n{evidence}\n\n"
                f"Latest evidence tail (high priority for final-state checks):\n{evidence_tail}\n\n"
                f"Proposed report JSON:\n{report_text}"
            )

            response = self.client.chat.completions.create(
                model=self.model,
                messages=[{"role": "user", "content": prompt}],
                temperature=0,
                max_tokens=400,
            )
            raw = (response.choices[0].message.content or "").strip()
            parsed = None
            try:
                parsed = json.loads(raw)
            except Exception:
                m = re.search(r'\{[\s\S]*\}', raw)
                if m:
                    parsed = json.loads(m.group(0))
            if isinstance(parsed, dict) and "approved" in parsed:
                parsed.setdefault("reason", "")
                parsed.setdefault("required_actions", [])
                if not isinstance(parsed.get("required_actions"), list):
                    parsed["required_actions"] = [str(parsed.get("required_actions"))]
                return parsed
        except Exception as e:
            print(f"[WARN] Report quality review skipped: {e}")

        return {"approved": True, "reason": "quality gate fallback", "required_actions": []}

    def _get_assembled_log_tail(self, max_lines: int = 120) -> str:
        """Return the latest assembled-log lines for final-state verification."""
        text = (self._assembled_log_text or "").strip()
        if not text:
            return "(no assembled log yet)"
        lines = text.splitlines()
        if len(lines) <= max_lines:
            return "\n".join(lines)
        return "\n".join(lines[-max_lines:])

    def _build_outcome_injection(self, tail_count: int = 2000, max_signals: int = 15) -> str:
        """
        Scan the raw log tail for outcome-level signals and return a compact
        auto-injected summary so the model has end-state awareness from step 0.
        Returns empty string if no raw log or no signals found.
        """
        if not self._raw_log_cache:
            err = self._ensure_raw_log_cache()
            if err or not self._raw_log_cache:
                return ""

        tail_lines = self._raw_log_cache[-tail_count:]
        signals = []
        for line in tail_lines:
            line_str = str(line)
            if any(kw in line_str for kw in self._OUTCOME_SIGNAL_KEYWORDS):
                signals.append(line_str.strip())

        if not signals:
            return ""

        # Keep latest N signals to avoid token bloat.
        signals = signals[-max_signals:]
        compact = []
        for s in signals:
            _, ts_display, msg = self._normalize_time_message(s)
            compact.append(f"<{ts_display}> {msg}")

        return (
            "[AUTO-INJECTED: Final Physical State Evidence from log tail]\n"
            "These are outcome-level signals from the end of the log. "
            "Use them to verify whether features eventually succeeded before concluding persistent failure.\n"
            + "\n".join(compact)
        )

    def _build_ace_workflow_block(self) -> str:
        """
        Render the ACE workflow playbook + bullet-citation reminder for the
        agent's system prompt. Returns "" when no AceRunner is attached or
        the workflow playbook has no bullets yet (so we don't waste tokens
        on an empty header on a cold install).
        """
        if self.ace_runner is None:
            print("[ace] workflow block skipped: no AceRunner attached")
            return ""
        try:
            text = self.ace_runner.render_workflow()
        except Exception as e:
            print(f"[ace] render_workflow failed: {e}")
            return ""
        if not text or text.strip() in ("", "(empty playbook)"):
            print("[ace] workflow block skipped: workflow playbook is empty")
            return ""
        n_bullets = sum(1 for ln in text.splitlines() if ln.lstrip().startswith("- "))
        try:
            pb_path = getattr(self.ace_runner.workflow_pb, "path", "?")
        except Exception:
            pb_path = "?"
        print(f"[ace] injecting {n_bullets} workflow bullets into prompt (from {pb_path})")
        return (
            "\n=== ACE Workflow Playbook (orchestration rules learned from past cases) ===\n"
            + text
            + "\nApply the bullets above when they fit. Cite the bullet ids you used in\n"
              "submit_final_report.applied_bullet_ids; cite ids you found misleading in\n"
              "flagged_bullet_ids. Ignore bullets that don't apply.\n"
              "=== End Workflow Playbook ===\n\n"
        )

    def _build_ace_domain_block(self, skill_name: str) -> str:
        """
        Render the ACE domain playbook for one skill. Returns "" when ACE is
        not attached, the playbook is empty, or rendering fails.
        """
        if self.ace_runner is None or not skill_name:
            if self.ace_runner is None:
                print(f"[ace] domain block skipped ({skill_name!r}): no AceRunner attached")
            return ""
        try:
            text = self.ace_runner.render_domain(skill_name, ensure=True)
        except Exception as e:
            print(f"[ace] render_domain({skill_name}) failed: {e}")
            return ""
        if not text or text.strip() in ("", "(empty playbook)"):
            print(f"[ace] domain block skipped ({skill_name!r}): playbook is empty")
            return ""
        n_bullets = sum(1 for ln in text.splitlines() if ln.lstrip().startswith("- "))
        try:
            pb_path = getattr(self.ace_runner.domain_pbs.get(skill_name), "path", "?")
        except Exception:
            pb_path = "?"
        print(f"[ace] injecting {n_bullets} domain bullets for {skill_name!r} into prompt (from {pb_path})")
        return (
            f"=== ACE Domain Playbook for {skill_name} (lessons from past cases) ===\n"
            + text
            + "\n=== End Domain Playbook ===\n\n"
        )
