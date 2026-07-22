"""
Post-Reflector regression double-check.

After the Reflector updates the playbook and a fresh eval is run, this
tool compares the new eval report against the previous one in the same
folder and, for cases whose scores dropped meaningfully, asks an LLM
to judge whether any bullets that were modified inside the touched-
window (see TOUCHED_WINDOW below) are likely responsible.

Output: a standalone JSON report `review_<ts>.json` next to the eval file.

Usage:
    python -m services.ace.eval.review <path/to/eval_YYYYMMDDTHHMMSS+0000.json>
    python -m services.ace.eval.review <eval.json> --model <optional-override>

This module is intentionally self-contained — it only imports helpers from
`services.ace.cli` to reuse LLM / playbooks_dir wiring. No production code
is modified.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from services.ace import cli as ace_cli


# --- tunables (hard-coded per current requirements) --------------------------
SCORE_DROP_THRESHOLD     = 1.0   # baseline - current >= 1.0 → regression
CONFIDENCE_GATE          = 0.7   # LLM confidence needed to flag gate FAIL
REVIEW_TEMPERATURE       = 0.2
REVIEW_MAX_TOKENS        = 1500

_FACETS = ("root_cause_match", "evidence_coverage")

# Fixed baseline eval report: the review always compares the newest eval
# against this file, never against the previous eval run.
BASELINE_EVAL_FILENAME   = "baseline.json"

# Playbook JSONs used by the review live on the shared network folder
# (same source `services/ace/eval/runner.py` reads). Only the JSON files at
# this top level are consumed — the `history/` subfolder underneath is
# intentionally ignored (glob is non-recursive).
PLAYBOOKS_DIR = Path(
    r"\\infs089b.iil.intel.com\HOME\WirelessCE\Intel_WirelessCE_Avatar\ace_playbook"
)

# Per-playbook-file "touched" window: for each *.json in the playbook dir we
# read the file-level `updated_at` and treat any bullet whose `updated_at`
# falls within this many minutes BEFORE it as freshly touched. This makes
# the window follow the Reflector run that actually wrote the file, rather
# than a wall-clock "today" that drifts if the review runs the next day.
TOUCHED_WINDOW = timedelta(hours=1)


# --- baseline resolution -----------------------------------------------------
def _find_baseline(current_path: Path) -> Path | None:
    """
    Baseline is a fixed file — `baseline.json` sitting next to `current_path`.
    Returns None if it doesn't exist. Filesystem lookup is case-insensitive
    on Windows so `Baseline.json` also matches.
    """
    candidate = current_path.parent / BASELINE_EVAL_FILENAME
    if candidate.is_file() and candidate.resolve() != current_path.resolve():
        return candidate
    return None


# --- playbook scanning -------------------------------------------------------
def _load_touched_bullets(playbooks_dir: Path) -> dict[str, dict]:
    """
    Walk every `*.json` playbook file in `playbooks_dir` and return a map
    `bullet_id -> {id, section, content, playbook_file, cutoff, pb_updated}`
    for bullets recently touched.

    "Recently touched" is defined *per playbook file* using the file-level
    top-level `updated_at`:

        cutoff       = <file.updated_at> - TOUCHED_WINDOW
        bullet is touched  iff  cutoff <= bullet.updated_at <= file.updated_at

    Files without a parseable file-level `updated_at` are skipped (nothing
    in them is treated as touched).

    The glob is intentionally non-recursive so the `history/` subfolder
    under the shared playbook directory is skipped.
    """
    touched: dict[str, dict] = {}
    if not playbooks_dir.is_dir():
        return touched
    for pb_file in sorted(playbooks_dir.glob("*.json")):
        if not pb_file.is_file():
            continue
        try:
            data = json.loads(pb_file.read_text(encoding="utf-8"))
        except Exception as e:
            print(f"[review] WARN: could not parse {pb_file.name}: {e}",
                  file=sys.stderr)
            continue

        pb_ts = (data or {}).get("updated_at") or ""
        try:
            pb_updated = datetime.fromisoformat(pb_ts)
        except Exception:
            print(f"[review] WARN: {pb_file.name} has no valid file-level "
                  f"updated_at ({pb_ts!r}); skipping.", file=sys.stderr)
            continue
        cutoff = pb_updated - TOUCHED_WINDOW

        for b in (data.get("bullets") or []):
            ts = b.get("updated_at") or ""
            try:
                bt = datetime.fromisoformat(ts)
            except Exception:
                continue
            if cutoff <= bt:
                bid = b.get("id")
                if bid:
                    touched[bid] = {
                        "id": bid,
                        "section": b.get("section", ""),
                        "content": b.get("content", ""),
                        "playbook_file": pb_file.name,
                        "bullet_updated_at":   bt.isoformat(timespec="seconds"),
                        "playbook_updated_at": pb_updated.isoformat(timespec="seconds"),
                        "cutoff":              cutoff.isoformat(timespec="seconds"),
                    }
    return touched


def _extract_applied_and_flagged(case: dict) -> tuple[list[str], list[str]]:
    """
    Parse `case.agent.answer` (a JSON-encoded string) and return
    (applied_bullet_ids, flagged_bullet_ids). Returns ([], []) on any error.
    """
    ans_str = ((case.get("agent") or {}).get("answer")) or ""
    if not isinstance(ans_str, str) or not ans_str:
        return [], []
    try:
        obj = json.loads(ans_str)
    except Exception:
        return [], []
    if not isinstance(obj, dict):
        return [], []
    applied = [str(x) for x in (obj.get("applied_bullet_ids") or [])]
    flagged = [str(x) for x in (obj.get("flagged_bullet_ids") or [])]
    return applied, flagged


def _score_drops(current_scores: dict, baseline_scores: dict) -> dict:
    """Return {facet: delta} where delta = curr - base, for facets that
    dropped by >= SCORE_DROP_THRESHOLD."""
    drops: dict[str, float] = {}
    for facet in _FACETS:
        try:
            c = float(current_scores.get(facet))
            b = float(baseline_scores.get(facet))
        except (TypeError, ValueError):
            continue
        if b - c >= SCORE_DROP_THRESHOLD:
            drops[facet] = round(c - b, 3)
    return drops


def _should_review(current_case: dict, baseline_case: dict | None) -> tuple[bool, dict]:
    """Step-1 gate: any facet dropped >= SCORE_DROP_THRESHOLD vs baseline."""
    cur = ((current_case.get("judge") or {}).get("mean_scores") or {})
    if not cur:
        return False, {}
    if baseline_case is None:
        return False, {}
    base = ((baseline_case.get("judge") or {}).get("mean_scores") or {})
    if not base:
        return False, {}
    drops = _score_drops(cur, base)
    if not drops:
        return False, {}
    return True, drops


# --- LLM reviewer ------------------------------------------------------------
REVIEWER_SYSTEM = (
    "You are an Intel Wi-Fi playbook curator performing a post-Reflector "
    "regression review. The Reflector recently modified one or more "
    "bullets in the playbook, and an evaluation case scored lower than "
    "before. You are given:\n"
    "  - which score facets dropped, and by how much,\n"
    "  - the judge's reasoning (why the case scored lower),\n"
    "  - the CURRENT agent's answer JSON (lower-scored, cites `applied_bullet_ids`),\n"
    "  - the BASELINE agent's answer JSON (higher-scored, from the previous eval run) — "
    "compare the two to see what shifted after the playbook change,\n"
    "  - a list of bullets to review. Every listed bullet was BOTH cited by "
    "the current agent AND recently modified by the Reflector, so each is a "
    "prime suspect for the score drop.\n"
    "\n"
    "For EACH bullet, judge its effect on the score drop. Use the BASELINE "
    "vs CURRENT answer diff as your primary signal — if the current answer "
    "omits or contradicts something the baseline got right, and a reviewed "
    "bullet plausibly caused that shift, mark it accordingly.\n"
    "\n"
    "verdict values (choose ONE per bullet):\n"
    "  harmful  — bullet misled the agent and caused the score drop.\n"
    "  neutral  — bullet was cited but had no material impact on the score.\n"
    "  helpful  — bullet was cited and clearly helped; the score drop is caused by something else.\n"
    "\n"
    "recommended_action values:\n"
    "  revert — undo the recent modification (for `harmful` bullets).\n"
    "  keep   — leave the bullet as-is (for `neutral` and `helpful`).\n"
    "\n"
    "Return STRICT JSON — a top-level list, no prose, no markdown fences:\n"
    "[\n"
    "  {\n"
    '    "bullet_id": "<id>",\n'
    '    "verdict": "harmful | neutral | helpful",\n'
    '    "confidence": <float 0.0-1.0>,\n'
    '    "why": "<one or two sentences>",\n'
    '    "recommended_action": "revert | keep"\n'
    "  }\n"
    "]\n"
)


def _build_reviewer_user_prompt(
    case_id: str,
    score_drops: dict,
    judge_reasoning: str,
    agent_answer: str,
    baseline_answer: str,
    bullets_to_review: list[dict],
    flagged_by_agent: list[str],
) -> str:
    payload = {
        "case_id": case_id,
        "score_drops": score_drops,
        "flagged_by_agent": flagged_by_agent,
        "judge_reasoning": judge_reasoning,
        "current_agent_answer": agent_answer,
        "baseline_agent_answer": baseline_answer,
        "bullets_to_review": bullets_to_review,
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


_JSON_LIST_RE = re.compile(r"\[.*\]", re.DOTALL)


def _parse_reviewer_output(raw: str) -> list[dict]:
    if not raw:
        return [{"error": "reviewer_empty_output"}]
    cleaned = raw.strip()
    # Strip optional markdown fences.
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?", "", cleaned).strip()
        if cleaned.endswith("```"):
            cleaned = cleaned[:-3].strip()
    # Try direct parse first.
    try:
        obj = json.loads(cleaned)
        if isinstance(obj, list):
            return obj
    except Exception:
        pass
    # Fallback: grep the first JSON list.
    m = _JSON_LIST_RE.search(cleaned)
    if m:
        try:
            obj = json.loads(m.group(0))
            if isinstance(obj, list):
                return obj
        except Exception as e:
            return [{"error": f"reviewer_json_parse_failed: {e}", "raw": raw[:2000]}]
    return [{"error": "reviewer_json_parse_failed", "raw": raw[:2000]}]


def _review_case_with_llm(
    llm,
    case_id: str,
    score_drops: dict,
    judge_reasoning: str,
    agent_answer: str,
    baseline_answer: str,
    bullets_to_review: list[dict],
    flagged_by_agent: list[str],
) -> tuple[list[dict], dict]:
    """Returns (verdicts, usage_dict). usage_dict is empty on failure."""
    if not bullets_to_review:
        return [], {}
    user = _build_reviewer_user_prompt(
        case_id, score_drops, judge_reasoning,
        agent_answer, baseline_answer,
        bullets_to_review, flagged_by_agent,
    )
    try:
        resp = llm.client.chat.completions.create(
            model=llm.model,
            messages=[
                {"role": "system", "content": REVIEWER_SYSTEM},
                {"role": "user", "content": user},
            ],
            temperature=REVIEW_TEMPERATURE,
            max_tokens=REVIEW_MAX_TOKENS,
        )
        content = (resp.choices[0].message.content or "").strip()
        usage = _extract_usage_from_resp(resp)
    except Exception as e:
        return [{"error": f"reviewer_call_failed: {e}"}], {}
    return _parse_reviewer_output(content), usage


def _extract_usage_from_resp(resp) -> dict:
    """Pull token counts out of an OpenAI-style response.usage."""
    u = getattr(resp, "usage", None)
    if u is None:
        return {}
    def _g(name: str) -> int:
        v = getattr(u, name, None)
        if v is None and isinstance(u, dict):
            v = u.get(name)
        try:
            return int(v) if v is not None else 0
        except Exception:
            return 0
    return {
        "prompt_tokens":     _g("prompt_tokens"),
        "completion_tokens": _g("completion_tokens"),
        "total_tokens":      _g("total_tokens"),
    }


# --- main --------------------------------------------------------------------
def _build_bullets_to_review(
    applied_ids: set[str],
    touched_map: dict[str, dict],
) -> list[dict]:
    """
    Bullets sent to the LLM for judgement. Only `applied ∩ touched`, because
    a bullet the agent did not cite cannot have affected this case's score
    — those are auto-marked `neutral` later without an LLM call.
    """
    suspects = applied_ids & set(touched_map.keys())
    out: list[dict] = []
    for bid in sorted(suspects):
        info = touched_map[bid]
        out.append({
            "id":              bid,
            "section":         info["section"],
            "content":         info["content"],
            "playbook_file":   info["playbook_file"],
            "usage":           "applied",
        })
    return out


def _auto_neutral_entries(
    applied_ids: set[str],
    touched_map: dict[str, dict],
) -> list[dict]:
    """
    Auto-generate neutral verdicts for bullets that were modified inside
    the touched window but NOT cited by the agent. Rationale: if the agent
    never used a bullet, it cannot have influenced this case's score. Kept
    in the report for traceability without spending LLM tokens on them.
    """
    touched_not_applied = set(touched_map.keys()) - applied_ids
    out: list[dict] = []
    for bid in sorted(touched_not_applied):
        info = touched_map[bid]
        out.append({
            "bullet_id":          bid,
            "usage":              "not_applied_but_touched",
            "verdict":            "neutral",
            "confidence":         1.0,
            "why":                "Agent did not cite this bullet; by definition it had no impact on this case's score.",
            "recommended_action": "keep",
            "section":            info["section"],
            "playbook_file":      info["playbook_file"],
        })
    return out


def _now_utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# Default folder where `services.ace.eval.runner` writes eval_*.json reports.
# A bare filename passed to `review()` (or the CLI) is looked up here first.
DEFAULT_RUNS_DIR = Path(__file__).resolve().parent / "runs"


def _resolve_eval_path(current_eval_path: Path) -> Path:
    """
    Locate an eval report. If `current_eval_path` doesn't exist as given,
    fall back to `DEFAULT_RUNS_DIR / <name>` so callers can pass just the
    filename (e.g. `eval_20260716T075201+0000.json`).
    """
    p = Path(current_eval_path)
    if p.is_file():
        return p.resolve()
    fallback = DEFAULT_RUNS_DIR / p.name
    if fallback.is_file():
        return fallback.resolve()
    raise FileNotFoundError(
        f"current eval not found: {current_eval_path} "
        f"(also tried {fallback})"
    )


def review(current_eval_path: Path, model: str | None = None) -> dict:
    ace_cli._ensure_avatarfiles_dir()
    current_eval_path = _resolve_eval_path(current_eval_path)

    current_report = json.loads(current_eval_path.read_text(encoding="utf-8"))

    baseline_path = _find_baseline(current_eval_path)
    if baseline_path is None:
        raise FileNotFoundError(
            f"[review] baseline eval not found: expected "
            f"'{BASELINE_EVAL_FILENAME}' next to {current_eval_path.name} "
            f"in {current_eval_path.parent}. Aborting."
        )
    baseline_report = json.loads(baseline_path.read_text(encoding="utf-8"))
    baseline_by_id = {
        c.get("case_id"): c
        for c in ((baseline_report or {}).get("cases") or [])
        if c.get("case_id")
    }

    # Playbook snapshot to scan for touched bullets: the shared network
    # folder. `history/` under it is skipped (non-recursive glob).
    playbooks_dir = PLAYBOOKS_DIR
    if not playbooks_dir.is_dir():
        print(f"[review] WARNING: playbook share not reachable: "
              f"{playbooks_dir} — check VPN / network access. "
              f"No bullets will be reviewed.")
    touched_map = _load_touched_bullets(playbooks_dir)
    touched_ids = sorted(touched_map.keys())

    print(f"[review] current    : {current_eval_path.name}")
    print(f"[review] baseline   : {baseline_path.name}")
    print(f"[review] playbooks  : {playbooks_dir}")
    print(f"[review] touched window: last {TOUCHED_WINDOW} before each "
          f"playbook's file-level updated_at")
    print(f"[review] touched bullets: "
          f"{touched_ids if touched_ids else '(none)'}")

    llm = None  # lazy — only build if a case actually needs review
    reviewed_cases: list[dict] = []
    any_fail = False
    review_usage_totals = {
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
        "calls": 0,
    }

    for case in (current_report.get("cases") or []):
        cid = case.get("case_id")
        baseline_case = baseline_by_id.get(cid)
        should, drops = _should_review(case, baseline_case)
        if not should:
            continue

        applied, flagged = _extract_applied_and_flagged(case)
        applied_set = set(applied)

        bullets_to_review = _build_bullets_to_review(applied_set, touched_map)
        auto_neutral      = _auto_neutral_entries(applied_set, touched_map)
        if not bullets_to_review and not auto_neutral:
            print(f"[review] {cid}: dropped {drops} but no touched bullets "
                  f"intersect — nothing to review, skip")
            continue

        judge_block = case.get("judge") or {}
        reasoning_samples = judge_block.get("reasoning_samples") or []
        judge_reasoning = "\n---\n".join(str(r) for r in reasoning_samples)
        agent_answer = ((case.get("agent") or {}).get("answer")) or ""
        baseline_answer = ((baseline_case.get("agent") or {}).get("answer")) or ""

        if bullets_to_review:
            if llm is None:
                llm = ace_cli._build_llm(model)
                print(f"[review] LLM       : {getattr(llm, 'model', '?')}")
            print(f"[review] {cid}: drops={drops} — LLM-reviewing "
                  f"{len(bullets_to_review)} applied bullet(s), "
                  f"auto-neutral {len(auto_neutral)} not-applied bullet(s)")
            verdicts, call_usage = _review_case_with_llm(
                llm, cid, drops, judge_reasoning,
                agent_answer, baseline_answer,
                bullets_to_review, flagged,
            )
            if call_usage:
                review_usage_totals["prompt_tokens"]     += int(call_usage.get("prompt_tokens",     0))
                review_usage_totals["completion_tokens"] += int(call_usage.get("completion_tokens", 0))
                review_usage_totals["total_tokens"]      += int(call_usage.get("total_tokens",      0))
                review_usage_totals["calls"]             += 1
        else:
            print(f"[review] {cid}: drops={drops} — no applied+touched bullets; "
                  f"auto-neutral {len(auto_neutral)} not-applied bullet(s)")
            verdicts = []

        # Append auto-neutral entries for touched-but-not-applied bullets.
        verdicts = list(verdicts) + auto_neutral

        for v in verdicts:
            if not isinstance(v, dict):
                continue
            try:
                conf = float(v.get("confidence") or 0)
            except (TypeError, ValueError):
                conf = 0.0
            if v.get("verdict") == "harmful" and conf >= CONFIDENCE_GATE:
                any_fail = True

        reviewed_cases.append({
            "case_id":          cid,
            "score_drops":      drops,
            "applied_bullet_ids":  applied,
            "flagged_bullet_ids":  flagged,
            "reviewed_bullets": verdicts,
        })

    report = {
        "ts_utc":               _now_utc_iso(),
        "baseline_eval":        baseline_path.name,
        "current_eval":         current_eval_path.name,
        "playbooks_dir":        str(playbooks_dir),
        "touched_bullet_ids":   touched_ids,
        "confidence_gate":      CONFIDENCE_GATE,
        "gate_verdict":         "FAIL" if any_fail else "PASS",
        "review_usage":         review_usage_totals,
        "cases":                reviewed_cases,
    }

    stamp = report["ts_utc"].replace(":", "").replace("-", "")
    out_path = current_eval_path.parent / f"review_{stamp}.json"
    out_path.write_text(
        json.dumps(report, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    print(f"\n[review] wrote {out_path}")
    print(f"[review] verdict: {report['gate_verdict']} "
          f"({len(reviewed_cases)} case(s) reviewed)")
    ru = review_usage_totals
    if ru.get("calls"):
        print(f"[review] tokens : {ru['total_tokens']:,} total "
              f"({ru['prompt_tokens']:,} prompt + {ru['completion_tokens']:,} completion) "
              f"across {ru['calls']} call(s)")
    return report


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="python -m services.ace.eval.review",
        description=("Post-Reflector regression review. Compares the given "
                     "eval report against the previous eval in the same "
                     "folder, and asks an LLM to judge whether any bullets "
                     "modified inside the touched window (per-playbook, "
                     "see TOUCHED_WINDOW) caused the score drop."),
    )
    p.add_argument("current_eval", type=Path,
                   help="Path to the eval_*.json to be checked "
                        "(typically the most recent one).")
    p.add_argument("--model", type=str, default=None,
                   help="Override chatbot model. Defaults to the app's "
                        "configured model.")
    args = p.parse_args(argv)
    report = review(args.current_eval, model=args.model)
    return 0 if report.get("gate_verdict") == "PASS" else 2


if __name__ == "__main__":
    sys.exit(main())
