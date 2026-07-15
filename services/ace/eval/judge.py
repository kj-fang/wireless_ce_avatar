"""
LLM judge — blind A/B comparison of two replays against ground truth.

One LLM call per case. Slot assignment (which of before/after becomes
Report A) is randomized per call so position bias cannot systematically
favor the newer playbook; the result is mapped back to before/after terms
before scoring sees it.
"""

from __future__ import annotations

import json
import random
from typing import Optional

from ..roles import _extract_json
from .cases import EvalCase
from .prompts import fill_judge_prompt, TAG_KEYWORDS
from .replay import ReplayResult

_REPORT_EXCERPT_CAP = 4000
_BASELINE_EXCERPT_CAP = 1500


def _report_excerpt(replay: ReplayResult, cap: int = _REPORT_EXCERPT_CAP) -> str:
    text = replay.report_text()
    if not text.strip():
        return f"(no report produced — result_type={replay.result_type}" \
               + (f", error={replay.error}" if replay.error else "") + ")"
    if len(text) > cap:
        text = text[:cap] + f"\n…[+{len(text) - cap} chars truncated]"
    return text


def _baseline_excerpt(case: EvalCase) -> str:
    base = case.ground_truth.baseline_report
    if not isinstance(base, dict):
        return "(none recorded)"
    parts = []
    for k in ("root_cause_summary", "markdown_summary"):
        v = base.get(k)
        if v:
            parts.append(str(v))
    text = "\n".join(parts) or json.dumps(base, default=str)[:500]
    if len(text) > _BASELINE_EXCERPT_CAP:
        text = text[:_BASELINE_EXCERPT_CAP] + "…"
    return text


class Judge:
    def __init__(self, llm, rng: Optional[random.Random] = None):
        """llm: an LLM_helper-shaped object exposing .chat(messages, system_content)."""
        self.llm = llm
        self._rng = rng or random.Random()

    def compare(self, case: EvalCase, before: ReplayResult,
                after: ReplayResult) -> dict:
        """Returns a judge dict in before/after terms:
            {winner: before|after|tie,
             root_cause_match_before, root_cause_match_after,
             evidence_quality_before, evidence_quality_after,
             inferred_tag_before, inferred_tag_after,
             rationale, slot_of_after, error?}
        On any failure returns {"winner": "tie", "error": "..."} so scoring
        degrades gracefully to deterministic-only.
        """
        after_is_a = self._rng.random() < 0.5
        rep_a, rep_b = (after, before) if after_is_a else (before, after)

        issue = case.issue
        gt = case.ground_truth
        prompt = fill_judge_prompt(
            tag_universe=", ".join(sorted(TAG_KEYWORDS.keys()) + ["OTHER"]),
            case_context=(
                f"case_nbr: {issue.get('case_nbr', '')}\n"
                f"subject: {issue.get('subject', '')}\n"
                f"issue_type: {issue.get('issue_type', '')}\n"
                f"description: {(issue.get('description') or '')[:1500]}\n"
                f"user question: {case.user_message[:500]}"
            ),
            correct_root_cause=gt.correct_root_cause or "(not provided)",
            correct_conclusion_tag=gt.correct_conclusion_tag or "(not provided)",
            correct_skill=gt.correct_skill or "(not provided)",
            evidence_log_lines=(
                "\n".join(f"  {ln}" for ln in gt.evidence_log_lines)
                or "  (not provided)"
            ),
            vote=gt.vote,
            baseline_excerpt=_baseline_excerpt(case),
            report_a=_report_excerpt(rep_a),
            report_b=_report_excerpt(rep_b),
        )

        try:
            raw = self.llm.chat(
                messages=[{"role": "user", "content": prompt}],
                system_content=(
                    "You are a strict, evidence-driven Wi-Fi triage reviewer. "
                    "Output strict JSON only."
                ),
            )
            res = _extract_json(raw)
        except Exception as e:
            return {"winner": "tie", "error": f"judge failed: {e}"}

        # Map slots back to before/after.
        def _num(key_a, key_b):
            va, vb = res.get(key_a), res.get(key_b)
            va = float(va) if isinstance(va, (int, float)) else None
            vb = float(vb) if isinstance(vb, (int, float)) else None
            return (va, vb) if after_is_a else (vb, va)

        m_after, m_before = _num("root_cause_match_a", "root_cause_match_b")
        e_after, e_before = _num("evidence_quality_a", "evidence_quality_b")
        tag_a = str(res.get("inferred_tag_a") or "")
        tag_b = str(res.get("inferred_tag_b") or "")
        tag_after, tag_before = (tag_a, tag_b) if after_is_a else (tag_b, tag_a)

        slot_winner = str(res.get("winner") or "tie").strip().upper()
        if slot_winner == "A":
            winner = "after" if after_is_a else "before"
        elif slot_winner == "B":
            winner = "before" if after_is_a else "after"
        else:
            winner = "tie"

        return {
            "winner": winner,
            "root_cause_match_before": m_before,
            "root_cause_match_after": m_after,
            "evidence_quality_before": e_before,
            "evidence_quality_after": e_after,
            "inferred_tag_before": tag_before,
            "inferred_tag_after": tag_after,
            "rationale": str(res.get("rationale") or ""),
            "slot_of_after": "A" if after_is_a else "B",
        }
