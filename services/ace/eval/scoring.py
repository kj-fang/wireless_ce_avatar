"""
Deterministic scoring + composite verdict for eval replays.

Deterministic components (each may be None = "no ground truth for this
component", in which case it drops out of the composite and the remaining
weights are renormalized):

    completed  — did the replay produce a full report?
    tag        — inferred conclusion tag == correct_conclusion_tag
    skill      — was the user-named correct_skill actually invoked?
    evidence   — fraction of ground-truth evidence lines present in report

The composite blends these with the judge's root-cause match (when a judge
result is available). Deterministic components carry ~70% of the weight so a
biased/flaky judge can't flip a verdict on its own.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, asdict
from difflib import SequenceMatcher
from typing import Optional

from .prompts import TAG_KEYWORDS
from .replay import ReplayResult
from .cases import GroundTruth

# Composite weights (renormalized over available components per case).
_WEIGHTS = {
    "completed": 0.10,
    "tag":       0.30,
    "skill":     0.15,
    "evidence":  0.15,
    "judge":     0.30,
}

# Verdict thresholds on the composite delta (after - before).
_DELTA_IMPROVED = 0.10
_DELTA_REGRESSED = -0.10
# Judge-only tiebreak needs at least this margin between match scores.
_JUDGE_MARGIN = 0.15


@dataclass
class DetScores:
    completed: float = 0.0
    tag: Optional[float] = None
    skill: Optional[float] = None
    evidence: Optional[float] = None
    inferred_tag: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


def infer_conclusion_tag(report_text: str) -> str:
    """Conservative tag inference from report text.

    Returns the tag whose keywords match, but ONLY when exactly one tag
    family matches — any ambiguity yields "" so the tag component drops out
    rather than mis-scoring.
    Exact tag tokens (e.g. the literal string "AP_KICK") count as a match
    for that tag too.
    """
    text = (report_text or "").lower()
    if not text.strip():
        return ""
    hits: list[str] = []
    for tag, keywords in TAG_KEYWORDS.items():
        if tag.lower() in text:
            hits.append(tag)
            continue
        for kw in keywords:
            if kw in text:
                hits.append(tag)
                break
    hits = list(dict.fromkeys(hits))
    return hits[0] if len(hits) == 1 else ""


_TS_RE = re.compile(
    r"\d{1,4}[-/]\d{1,2}[-/]\d{1,4}[ T-]\d{1,2}:\d{2}(:\d{2})?(\.\d+)?"
    r"|\d{1,2}:\d{2}:\d{2}(\.\d+)?"
)
_HEX_RE = re.compile(r"0x[0-9a-fA-F]+|[0-9a-fA-F]{8,}")
_WS_RE = re.compile(r"\s+")


def _normalize_line(line: str) -> str:
    """Strip timestamps / hex addresses / whitespace noise so an evidence
    line matches even when the report re-quotes it slightly differently."""
    s = (line or "").lower()
    s = _TS_RE.sub(" ", s)
    s = _HEX_RE.sub(" ", s)
    s = _WS_RE.sub(" ", s).strip()
    return s


def evidence_overlap(evidence_lines: list[str], report_text: str) -> Optional[float]:
    """Fraction of ground-truth evidence lines present in the report.
    None when there are no evidence lines to check."""
    lines = [ln for ln in (evidence_lines or []) if str(ln).strip()]
    if not lines:
        return None
    norm_report = _normalize_line(report_text)
    report_rows = [_normalize_line(r) for r in (report_text or "").splitlines() if r.strip()]
    matched = 0
    for ln in lines:
        norm = _normalize_line(str(ln))
        if not norm:
            continue
        if norm in norm_report:
            matched += 1
            continue
        best = max(
            (SequenceMatcher(None, norm, row).ratio() for row in report_rows),
            default=0.0,
        )
        if best >= 0.8:
            matched += 1
    return matched / len(lines)


def score_deterministic(replay: ReplayResult, gt: GroundTruth) -> DetScores:
    det = DetScores()
    det.completed = (
        1.0 if replay.result_type == "report"
        else 0.5 if replay.result_type == "partial_report"
        else 0.0
    )

    text = replay.report_text()
    det.inferred_tag = infer_conclusion_tag(text)

    if gt.correct_conclusion_tag:
        det.tag = 1.0 if (det.inferred_tag
                          and det.inferred_tag == gt.correct_conclusion_tag) else 0.0

    if gt.correct_skill:
        invoked = {s.strip().lower() for s in replay.skills_invoked}
        det.skill = 1.0 if gt.correct_skill.strip().lower() in invoked else 0.0

    det.evidence = evidence_overlap(gt.evidence_log_lines, text)
    return det


def _composite(det: DetScores, judge_match: Optional[float]) -> float:
    """Weighted mean over the components that exist for this case."""
    parts: list[tuple[float, float]] = [( _WEIGHTS["completed"], det.completed )]
    if det.tag is not None:
        parts.append((_WEIGHTS["tag"], det.tag))
    if det.skill is not None:
        parts.append((_WEIGHTS["skill"], det.skill))
    if det.evidence is not None:
        parts.append((_WEIGHTS["evidence"], det.evidence))
    if judge_match is not None:
        parts.append((_WEIGHTS["judge"], judge_match))
    total_w = sum(w for w, _ in parts)
    if total_w <= 0:
        return 0.0
    return sum(w * v for w, v in parts) / total_w


def case_verdict(det_before: DetScores, det_after: DetScores,
                 judge: Optional[dict]) -> dict:
    """Combine both arms into a per-case verdict.

    judge dict (already mapped back to before/after by Judge.compare):
        {winner: before|after|tie, root_cause_match_before, root_cause_match_after, ...}
    """
    jb = ja = None
    winner = ""
    if isinstance(judge, dict) and not judge.get("error"):
        jb = judge.get("root_cause_match_before")
        ja = judge.get("root_cause_match_after")
        winner = judge.get("winner") or ""
        jb = float(jb) if isinstance(jb, (int, float)) else None
        ja = float(ja) if isinstance(ja, (int, float)) else None

    score_before = _composite(det_before, jb)
    score_after = _composite(det_after, ja)
    delta = score_after - score_before

    if delta >= _DELTA_IMPROVED:
        verdict = "improved"
    elif delta <= _DELTA_REGRESSED:
        verdict = "regressed"
    elif winner in ("before", "after") and jb is not None and ja is not None \
            and abs(ja - jb) >= _JUDGE_MARGIN:
        verdict = "improved" if winner == "after" else "regressed"
    elif det_before.completed == 0.0 and det_after.completed == 0.0:
        # Neither arm produced a report — nothing comparable happened.
        verdict = "inconclusive"
    else:
        verdict = "same"

    return {
        "verdict": verdict,
        "score_before": round(score_before, 4),
        "score_after": round(score_after, 4),
        "delta": round(delta, 4),
        "det_before": det_before.to_dict(),
        "det_after": det_after.to_dict(),
        "judge": judge,
    }
