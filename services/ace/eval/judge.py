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
import os
import re
import statistics
from typing import Any


# --- Evidence-coverage penalty tuning ---------------------------------------
# evidence_coverage = 5 * (Σ weights / N) ** COV_POWER
#   POWER = 1.0  linear (each miss costs 5/N points)
#   POWER = 1.5  mild non-linear
#   POWER = 2.0  quadratic — miss 1 of 11 items drops ~0.87; miss 3 drops ~2.36
#   POWER = 3.0  cubic — miss 1 of 11 items drops ~1.24; miss 2 drops ~2.24
# Tune via env var ACE_JUDGE_COV_POWER (default 2.0). Higher = stricter.
_COV_POWER = float(os.environ.get("ACE_JUDGE_COV_POWER", "2.0"))
# Partial-credit weight for "partial" coverage. Env var ACE_JUDGE_PARTIAL_WEIGHT.
_PARTIAL_WEIGHT = float(os.environ.get("ACE_JUDGE_PARTIAL_WEIGHT", "0.5"))


JUDGE_SYSTEM = (
    "You are an impartial technical grader for Wi-Fi log-analysis answers.\n"
    "Compare an AGENT_ANSWER against an EXPECTED reference. Judge meaning, "
    "not wording — synonyms, reorderings, and extra-but-correct detail must "
    "not be penalized. Severely penalize contradictions and fabricated facts.\n"
    "\n"
    "You will produce TWO outputs:\n"
    "\n"
    "(1) root_cause_match  — a float in [0.0, 5.0] with 0.5 increments allowed.\n"
    "    Does the AGENT_ANSWER identify the SAME primary causal mechanism\n"
    "    described in EXPECTED root_cause? Score by meaning, not wording.\n"
    "      5.0 = mechanism fully matches\n"
    "      4.0 = mostly matches, minor phrasing / secondary detail off\n"
    "      3.0 = partially correct — captures some aspect but renames or\n"
    "            reframes the mechanism (different sub-cause / category)\n"
    "      2.0 = weak match — right domain, wrong specific mechanism\n"
    "      1.0 = contradicts or fabricates the mechanism\n"
    "      0.0 = wrong or missing entirely\n"
    "\n"
    "(2) evidence_items  — a per-item coverage array.\n"
    "    You will be given a NUMBERED list of expected key_evidence items\n"
    "    ([1], [2], ..., [N]). For EACH item, decide how the AGENT_ANSWER\n"
    "    covers it:\n"
    "      \"full\"    = agent explicitly mentions this item by meaning.\n"
    "                  If the expected item contains specifics (a timestamp,\n"
    "                  status code, BSSID, count, channel, RSSI value),\n"
    "                  those specifics MUST appear in the agent answer for\n"
    "                  it to count as \"full\".\n"
    "      \"partial\" = agent mentions the topic of this item but omits its\n"
    "                  key specifics (e.g. mentions ‘missed beacons’ but not\n"
    "                  the exact count / threshold / timestamp).\n"
    "      \"none\"    = agent does not mention this item at all.\n"
    "\n"
    "    Return exactly one entry per expected item, in order, using the\n"
    "    expected item's numeric id. Do NOT invent items beyond the given\n"
    "    list. Do NOT skip items — if unsure, mark \"none\".\n"
    "\n"
    "Return STRICT JSON, no prose, no markdown fences:\n"
    "{\n"
    '  "root_cause_match": <float 0.0-5.0>,\n'
    '  "evidence_items": [\n'
    '    {"id": 1, "coverage": "full"|"partial"|"none", "note": "<short>"},\n'
    "    ...one entry per expected key_evidence item, in order...\n"
    "  ],\n"
    '  "reasoning": "<<= 4 sentences, in English>"\n'
    "}\n"
)


def _strip_recommended_actions(actual_answer: str) -> str:
    """Remove only the `recommended_actions` field from the agent JSON blob
    before sending to the judge — the judge rubric doesn't score
    recommendations, and they are the largest dead-weight field. Everything
    else is preserved verbatim. Falls back to the raw string if parsing fails.
    """
    if not actual_answer or not isinstance(actual_answer, str):
        return str(actual_answer or "")
    text = actual_answer.strip()
    if not text.startswith("{"):
        return text
    try:
        obj = json.loads(text)
    except Exception:
        return text
    if not isinstance(obj, dict) or "recommended_actions" not in obj:
        return text
    slim = {k: v for k, v in obj.items() if k != "recommended_actions"}
    return json.dumps(slim, ensure_ascii=False, indent=2)


def _build_user_prompt(case: dict, actual_answer: str) -> str:
    expected = case.get("expected") or {}
    root_cause = expected.get("root_cause") or ""
    key_evidence = list(expected.get("key_evidence") or [])
    numbered = "\n".join(f"  [{i+1}] {item}" for i, item in enumerate(key_evidence))
    if not numbered:
        numbered = "  (none provided)"
    question = case.get("user_question") or ""
    skill = case.get("skill") or "(unspecified)"
    slim_answer = _strip_recommended_actions(actual_answer)
    return (
        f"CASE_ID: {case.get('case_id')}\n"
        f"SKILL: {skill}\n"
        f"USER_QUESTION:\n{question}\n\n"
        f"EXPECTED root_cause:\n{root_cause}\n\n"
        f"EXPECTED key_evidence (N={len(key_evidence)} items — you MUST return "
        f"exactly {len(key_evidence)} entries in evidence_items, one per id):\n"
        f"{numbered}\n\n"
        f"AGENT_ANSWER (to be graded):\n{slim_answer}\n"
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


def _extract_usage(resp: Any) -> dict:
    """Pull prompt/completion/total token counts out of an OpenAI-style
    response. Returns an empty dict if the SDK didn't attach usage."""
    u = getattr(resp, "usage", None)
    if u is None:
        return {}
    def _get(name: str) -> int:
        v = getattr(u, name, None)
        if v is None and isinstance(u, dict):
            v = u.get(name)
        try:
            return int(v) if v is not None else 0
        except Exception:
            return 0
    return {
        "prompt_tokens":     _get("prompt_tokens"),
        "completion_tokens": _get("completion_tokens"),
        "total_tokens":      _get("total_tokens"),
    }


def _normalize(result: dict, expected_evidence_count: int) -> dict:
    # root_cause_match: allow 0.5 increments, clamp to [0, 5].
    raw_root = result.get("root_cause_match")
    if raw_root is None:
        # Backwards compat: some judges may still return the old "scores" dict.
        raw_root = (result.get("scores") or {}).get("root_cause_match")
    root = _clamp(raw_root, 0, 5)
    root = round(root * 2) / 2  # snap to 0.5 grid

    # evidence_coverage: mechanical, computed from per-item coverage returned
    # by the judge. full=1.0, partial=_PARTIAL_WEIGHT, none=0.0. Missing
    # entries (judge skipped an expected item) are treated as "none" so the
    # judge cannot inflate the score by omission. Final score applies a
    # non-linear penalty: score = 5 * (Σ weights / N) ** _COV_POWER, so each
    # missing item hurts more than a linear scheme.
    weights = {"full": 1.0, "partial": _PARTIAL_WEIGHT, "none": 0.0}
    raw_items = result.get("evidence_items") or []
    per_item: dict[int, dict] = {}
    for it in raw_items:
        try:
            idx = int(it.get("id"))
        except Exception:
            continue
        if idx < 1 or idx > max(expected_evidence_count, 1):
            continue
        cov = str(it.get("coverage") or "").strip().lower()
        if cov not in weights:
            cov = "none"
        per_item[idx] = {
            "coverage": cov,
            "weight": weights[cov],
            "note": str(it.get("note") or "").strip(),
        }
    # Backfill any missing expected item as "none".
    for i in range(1, expected_evidence_count + 1):
        per_item.setdefault(i, {"coverage": "none", "weight": 0.0,
                                 "note": "missing_from_judge"})

    if expected_evidence_count > 0:
        total = sum(v["weight"] for v in per_item.values())
        ratio = total / expected_evidence_count
        evidence = round(5.0 * (ratio ** _COV_POWER), 3)
    else:
        # No expected evidence items — nothing to score, treat as full.
        evidence = 5.0
    evidence = max(0.0, min(5.0, evidence))

    norm_scores = {
        "root_cause_match": root,
        "evidence_coverage": evidence,
    }
    overall = round(sum(norm_scores.values()) / len(norm_scores), 3)
    return {
        "scores": norm_scores,
        "overall": overall,
        "evidence_items": per_item,
        "reasoning": str(result.get("reasoning") or "").strip(),
    }


def judge_once(llm, case: dict, actual_answer: str,
               temperature: float = 0.2) -> dict:
    """Single judge call. Returns the normalized result or an error stub."""
    user = _build_user_prompt(case, actual_answer)
    expected_evidence = list(
        ((case.get("expected") or {}).get("key_evidence") or [])
    )
    n_items = len(expected_evidence)
    # Per-item scoring can produce long JSON. Each evidence_items entry with
    # its `note` explanation runs ~150-200 tokens in practice; add fixed
    # overhead for root_cause_match + reasoning + JSON scaffolding. Cap at a
    # sane upper bound so we don't burn the whole context window.
    max_tokens = min(4000, max(1500, 500 + 200 * n_items))
    try:
        resp = llm.client.chat.completions.create(
            model=llm.model,
            messages=[
                {"role": "system", "content": JUDGE_SYSTEM},
                {"role": "user", "content": user},
            ],
            temperature=temperature,
            max_tokens=max_tokens,
        )
        content = (resp.choices[0].message.content or "").strip()
        usage = _extract_usage(resp)
    except Exception as e:
        return {"error": f"judge_call_failed: {e}", "raw": "", "usage": {}}

    parsed = _parse_judge_json(content)
    if parsed is None:
        return {"error": "judge_json_parse_failed", "raw": content[:2000],
                "usage": usage}
    return _normalize(parsed, n_items) | {"raw": content, "usage": usage}


def judge(llm, case: dict, actual_answer: str,
          passes: int = 1, temperature: float = 0.2) -> dict:
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

    return _aggregate_runs(runs, errors)


def judge_multi(llms: list, case: dict, actual_answer: str,
                temperature: float = 0.2) -> dict:
    """Run one judge pass per LLM in `llms` and aggregate.

    Each entry in `llms` produces exactly one `judge_once` call. Every pass
    record is tagged with the `model` string it came from so per-model
    breakdowns are recoverable from the report.
    """
    runs: list[dict] = []
    errors: list[str] = []
    for llm in llms:
        r = judge_once(llm, case, actual_answer, temperature=temperature)
        model_name = getattr(llm, "model", None)
        r["model"] = model_name
        runs.append(r)
        if "error" in r:
            errors.append(f"[{model_name}] {r['error']}")

    agg = _aggregate_runs(runs, errors)
    agg["models"] = [getattr(l, "model", None) for l in llms]
    return agg


def _aggregate_runs(runs: list[dict], errors: list[str]) -> dict:
    valid = [r for r in runs if "scores" in r]
    if not valid:
        return {
            "passes": runs,
            "mean_scores": {},
            "mean_overall": 0.0,
            "stdev_overall": 0.0,
            "reasoning_samples": [],
            "errors": errors or ["no_valid_judge_runs"],
            "usage": {
                "prompt_tokens":     sum(int((r.get("usage") or {}).get("prompt_tokens",     0)) for r in runs),
                "completion_tokens": sum(int((r.get("usage") or {}).get("completion_tokens", 0)) for r in runs),
                "total_tokens":      sum(int((r.get("usage") or {}).get("total_tokens",      0)) for r in runs),
                "calls":             len(runs),
            },
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
    usage_totals = {
        "prompt_tokens":     sum(int((r.get("usage") or {}).get("prompt_tokens",     0)) for r in runs),
        "completion_tokens": sum(int((r.get("usage") or {}).get("completion_tokens", 0)) for r in runs),
        "total_tokens":      sum(int((r.get("usage") or {}).get("total_tokens",      0)) for r in runs),
        "calls":             len(runs),
    }
    return {
        "passes": runs,
        "mean_scores": mean_scores,
        "mean_overall": mean_overall,
        "stdev_overall": stdev_overall,
        "reasoning_samples": [r.get("reasoning", "") for r in valid],
        "errors": errors,
        "usage": usage_totals,
    }
