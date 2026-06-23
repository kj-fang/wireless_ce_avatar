"""
LLM-as-judge for ACE playbook evaluation.

Given a golden `expected` block and the chatbot's freshly produced `actual`
answer, the judge scores meaning-level alignment on four rubric facets and
returns an averaged score plus brief reasoning.

The judge is intentionally separate from `services.ace.roles` so this file
can evolve (different rubric, different model, multiple passes for noise
reduction) without touching the production Reflector / Curator.
"""

from __future__ import annotations

import json
import re
import statistics
from typing import Any


JUDGE_SYSTEM = (
    "You are an impartial technical grader for Wi-Fi log-analysis answers.\n"
    "Compare an AGENT_ANSWER against an EXPECTED reference. Judge meaning, "
    "not wording — synonyms, reorderings, and extra-but-correct detail must "
    "not be penalized. Severely penalize contradictions and fabricated facts.\n"
    "\n"
    "Score each facet on an integer 0..5 scale:\n"
    "  root_cause_match       5 = identifies the same primary cause; "
    "0 = wrong or missing.\n"
    "  evidence_coverage      5 = cites all key evidence items (timestamps, "
    "fields, codes); 0 = none cited.\n"
    "  action_alignment       5 = recommends substantively the same fix or "
    "next step; 0 = wrong/missing.\n"
    "  hallucination_freedom  5 = no claims unsupported by the log or "
    "contradicting expected; 0 = severe hallucination.\n"
    "\n"
    "Return STRICT JSON, no prose, no markdown fences:\n"
    "{\n"
    '  "scores": {\n'
    '    "root_cause_match": <int 0-5>,\n'
    '    "evidence_coverage": <int 0-5>,\n'
    '    "action_alignment": <int 0-5>,\n'
    '    "hallucination_freedom": <int 0-5>\n'
    "  },\n"
    '  "overall": <float 0-5>,\n'
    '  "reasoning": "<<= 4 sentences, in English>"\n'
    "}\n"
)


def _build_user_prompt(case: dict, actual_answer: str) -> str:
    expected = case.get("expected") or {}
    expected_block = json.dumps(expected, indent=2, ensure_ascii=False)
    question = case.get("user_question") or ""
    skill = case.get("skill") or "(unspecified)"
    return (
        f"CASE_ID: {case.get('case_id')}\n"
        f"SKILL: {skill}\n"
        f"USER_QUESTION:\n{question}\n\n"
        f"EXPECTED (golden reference):\n{expected_block}\n\n"
        f"AGENT_ANSWER (to be graded):\n{actual_answer}\n"
    )


_JSON_RE = re.compile(r"\{.*\}", re.DOTALL)


def _parse_judge_json(raw: str) -> dict | None:
    if not raw:
        return None
    raw = raw.strip()
    # Strip accidental ```json fences.
    raw = re.sub(r"^```(?:json)?", "", raw).strip()
    raw = re.sub(r"```$", "", raw).strip()
    try:
        return json.loads(raw)
    except Exception:
        m = _JSON_RE.search(raw)
        if not m:
            return None
        try:
            return json.loads(m.group(0))
        except Exception:
            return None


def _clamp(value: Any, lo: float, hi: float) -> float:
    try:
        v = float(value)
    except Exception:
        return lo
    return max(lo, min(hi, v))


def _normalize(result: dict) -> dict:
    scores = result.get("scores") or {}
    facets = ("root_cause_match", "evidence_coverage",
              "action_alignment", "hallucination_freedom")
    norm_scores = {f: _clamp(scores.get(f), 0, 5) for f in facets}
    # Re-derive `overall` ourselves so the judge can't bias it.
    overall = round(sum(norm_scores.values()) / len(norm_scores), 3)
    return {
        "scores": norm_scores,
        "overall": overall,
        "reasoning": str(result.get("reasoning") or "").strip(),
    }


def judge_once(llm, case: dict, actual_answer: str,
               temperature: float = 0.2) -> dict:
    """Single judge call. Returns the normalized result or an error stub."""
    user = _build_user_prompt(case, actual_answer)
    try:
        resp = llm.client.chat.completions.create(
            model=llm.model,
            messages=[
                {"role": "system", "content": JUDGE_SYSTEM},
                {"role": "user", "content": user},
            ],
            temperature=temperature,
            max_tokens=600,
        )
        content = (resp.choices[0].message.content or "").strip()
    except Exception as e:
        return {"error": f"judge_call_failed: {e}", "raw": ""}

    parsed = _parse_judge_json(content)
    if parsed is None:
        return {"error": "judge_json_parse_failed", "raw": content[:2000]}
    return _normalize(parsed) | {"raw": content}


def judge(llm, case: dict, actual_answer: str,
          passes: int = 3, temperature: float = 0.2) -> dict:
    """Run the judge `passes` times and aggregate.

    Returns:
        {
          "passes": [<judge_once result>, ...],
          "mean_scores": {facet: float, ...},
          "mean_overall": float,
          "stdev_overall": float,
          "reasoning_samples": [str, ...],
          "errors": [str, ...],
        }
    """
    runs: list[dict] = []
    errors: list[str] = []
    for _ in range(max(1, int(passes))):
        r = judge_once(llm, case, actual_answer, temperature=temperature)
        runs.append(r)
        if "error" in r:
            errors.append(r["error"])

    valid = [r for r in runs if "scores" in r]
    if not valid:
        return {
            "passes": runs,
            "mean_scores": {},
            "mean_overall": 0.0,
            "stdev_overall": 0.0,
            "reasoning_samples": [],
            "errors": errors or ["no_valid_judge_runs"],
        }

    facets = list(valid[0]["scores"].keys())
    mean_scores = {
        f: round(statistics.fmean(r["scores"][f] for r in valid), 3)
        for f in facets
    }
    overalls = [r["overall"] for r in valid]
    mean_overall = round(statistics.fmean(overalls), 3)
    stdev_overall = round(
        statistics.pstdev(overalls) if len(overalls) > 1 else 0.0, 3
    )
    return {
        "passes": runs,
        "mean_scores": mean_scores,
        "mean_overall": mean_overall,
        "stdev_overall": stdev_overall,
        "reasoning_samples": [r.get("reasoning", "") for r in valid],
        "errors": errors,
    }
