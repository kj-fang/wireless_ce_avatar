"""
Post-Review whodunit — trace corrupted bullets back to the offending feedback.

Given a `review_*.json` produced by `services.ace.eval.review`, for every
bullet flagged `harmful` + `revert` (above `CONFIDENCE_GATE`):

  1. Locate the bullet in the live playbook (fallback: newest snapshot).
  2. Collect suspect turns from `history/turns/*.jsonl` within +/-1 day of
     the bullet's `updated_at` (main line: UPDATE/REMOVE ops naming this
     bullet_id) and `created_at` (side line: ADD via `source_turn_ids`,
     since Curator's ADD op does NOT record the assigned id).
  3. Enrich each suspect with the matching entry in
     `history/feedback_details.jsonl` to surface `submitted_by`.
  4. Ask the LLM to nominate the most likely culprit turn, per bullet.
  5. Emit a human-readable report to stdout AND write
     `runs/<review-stamp>/killers_<review-stamp>.json`.

The tool never modifies live playbooks, snapshots, turn logs, or feedback
files. Failures on a single bullet (bullet missing, no suspects, LLM error)
are isolated so the rest of the report still lands on disk.

Usage:
    python -m services.ace.eval.find_the_killer <review_*.json>
    python -m services.ace.eval.find_the_killer <review_*.json> --namespace bt
    python -m services.ace.eval.find_the_killer <review_*.json> --model <name>
    python -m services.ace.eval.find_the_killer <review_*.json> --json
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

# Reuse the review-path resolver, live-dir resolver, newest-snapshot lookup
# and confidence gate that `corrupted_bullet.py` already exposes so the two
# CLIs pick the same review file for the same argument and agree on which
# bullets count as "corrupted".
from .corrupted_bullet import (
    CONFIDENCE_GATE,
    SNAPSHOTS_DIR,
    _newest_snapshot_dir,
    _resolve_live_dir,
    _resolve_review_path,
)


# --- config -----------------------------------------------------------------
LLM_TEMPERATURE = 0.2
LLM_MAX_TOKENS = 800

# Below this LLM-reported confidence the detective's nomination is treated as a
# best-guess-of-elimination rather than a real accusation: status downgrades to
# `ok_low_confidence` and `main_suspect.is_confident_guess` becomes False so
# downstream readers do not mistake it for a positive identification.
LLM_CONFIDENCE_GATE = 0.6


# --- review-report parsing --------------------------------------------------
def _extract_corrupted_bullets(review: dict) -> list[dict]:
    """Deduplicated corrupted-bullet list, keyed by bullet_id, keeping the
    highest-confidence verdict so the strongest `why` reaches the LLM."""
    by_id: dict[str, dict] = {}
    for case in (review.get("cases") or []):
        case_id = case.get("case_id") or ""
        for v in (case.get("reviewed_bullets") or []):
            if not isinstance(v, dict):
                continue
            if v.get("verdict") != "harmful":
                continue
            if v.get("recommended_action") != "revert":
                continue
            try:
                conf = float(v.get("confidence") or 0)
            except (TypeError, ValueError):
                conf = 0.0
            if conf < CONFIDENCE_GATE:
                continue
            bid = v.get("bullet_id")
            if not bid:
                continue
            prev = by_id.get(bid)
            if prev is None or conf > float(prev.get("confidence") or 0):
                by_id[bid] = {
                    "bullet_id": bid,
                    "why": v.get("why") or "",
                    "confidence": conf,
                    "case_id": case_id,
                }
    return sorted(by_id.values(), key=lambda x: x["bullet_id"])


# --- bullet lookup ----------------------------------------------------------
def _find_bullet_in_playbooks(bullet_id: str,
                              dirs: list[Path]) -> tuple[dict | None, Path | None]:
    """Search each directory's `*.json` for the bullet. Returns the first
    match as (bullet_dict, playbook_path); (None, None) if not present."""
    for d in dirs:
        if not d or not d.is_dir():
            continue
        for pb in sorted(d.glob("*.json")):
            try:
                data = json.loads(pb.read_text(encoding="utf-8"))
            except Exception:
                continue
            if not isinstance(data, dict):
                continue
            for b in (data.get("bullets") or []):
                if isinstance(b, dict) and b.get("id") == bullet_id:
                    return b, pb
    return None, None


# --- turn log scanning ------------------------------------------------------
def _parse_iso_to_local_date(ts):
    """Parse an ISO-8601 timestamp (with or without TZ) into a local date.
    Turn log filenames are minted from naive `datetime.now()`, so we must
    convert TZ-aware bullet timestamps back to local to compare correctly."""
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(str(ts))
    except (TypeError, ValueError):
        return None
    if dt.tzinfo is not None:
        dt = dt.astimezone()
    return dt.date()


def _candidate_jsonl_files(turns_dir: Path, center_date) -> list[Path]:
    """Turn-log files within +/-1 day of `center_date`. The +/-1 window
    absorbs cross-midnight batches and TZ-boundary drift, at the cost of
    reading at most 3 tiny jsonl files."""
    if center_date is None or not turns_dir.is_dir():
        return []
    out: list[Path] = []
    for delta in (-1, 0, 1):
        d = center_date + timedelta(days=delta)
        p = turns_dir / f"{d.isoformat()}.jsonl"
        if p.is_file():
            out.append(p)
    return out


def _iter_records(files: list[Path]):
    """Decoded turn records, line by line. Malformed lines are silently
    skipped — a bad record must never abort the whole scan."""
    for p in files:
        try:
            lines = p.read_text(encoding="utf-8").splitlines()
        except Exception:
            continue
        for ln in lines:
            ln = ln.strip()
            if not ln:
                continue
            try:
                yield json.loads(ln)
            except Exception:
                continue


def _collect_suspect_turns(bullet_id: str, bullet: dict,
                           turns_dir: Path) -> list[dict]:
    """Two-line suspect collection.

    - Main line: any turn whose `curate_result.operations_applied` contains
      an UPDATE / REMOVE naming `bullet_id`.
    - Side line: any turn whose `turn_id` appears in `bullet.source_turn_ids`
      (covers ADD, since Curator's ADD op has no `bullet_id` field).

    Scans only the `.jsonl` files within +/-1 day of `updated_at` (main)
    and `created_at` (side). Deduplicated by turn_id, newest ts first.
    """
    src_ids = set(bullet.get("source_turn_ids") or [])
    upd_date = _parse_iso_to_local_date(bullet.get("updated_at"))
    add_date = _parse_iso_to_local_date(bullet.get("created_at"))
    files_set: set[Path] = set()
    files_set.update(_candidate_jsonl_files(turns_dir, upd_date))
    files_set.update(_candidate_jsonl_files(turns_dir, add_date))
    files = sorted(files_set)

    suspects: dict[str, dict] = {}
    for rec in _iter_records(files):
        tid = rec.get("turn_id")
        if not tid:
            continue

        curate = rec.get("curate_result") or {}
        applied_ops = curate.get("operations_applied") or []

        matched_ops: list[dict] = []

        # Main line: UPDATE / REMOVE that names this bullet_id explicitly.
        for op in applied_ops:
            if not isinstance(op, dict):
                continue
            op_type = (op.get("type") or "").upper()
            if op_type in {"UPDATE", "REMOVE"} and op.get("bullet_id") == bullet_id:
                matched_ops.append({
                    "type": op_type,
                    "detail": op.get("new_content") or op.get("reason") or "",
                })

        # Side line: this turn's id is remembered as an origin turn for the
        # bullet. Attach the ADD op whose content best resembles the bullet's
        # current content (falls back to the first ADD in the turn, or an
        # empty stub if the turn has none — the turn is still surfaced so
        # the user sees the source of a corrupted-at-birth bullet).
        if tid in src_ids:
            add_ops = [op for op in applied_ops
                       if isinstance(op, dict)
                       and (op.get("type") or "").upper() == "ADD"]
            content = bullet.get("content") or ""
            best = None
            for op in add_ops:
                oc = op.get("content") or ""
                if oc and (oc == content or oc[:80] == content[:80]):
                    best = op
                    break
            if best is None and add_ops:
                best = add_ops[0]
            matched_ops.append({
                "type": "ADD_source",
                "detail": (best.get("content") if best else ""),
            })

        if not matched_ops:
            continue

        if tid in suspects:
            suspects[tid]["ops"].extend(matched_ops)
        else:
            suspects[tid] = {
                "turn_id": tid,
                "conversation_id": rec.get("conversation_id"),
                "ts": rec.get("ts"),
                "feedback_from_turn": rec.get("feedback") or {},
                "curator_reasoning": (curate.get("reasoning") or ""),
                "ops": matched_ops,
            }

    return sorted(suspects.values(),
                  key=lambda s: s.get("ts") or "",
                  reverse=True)


# --- feedback_details.jsonl -------------------------------------------------
def _load_feedback_index(feedback_path: Path) -> dict[str, dict]:
    """Index feedback_details.jsonl by `turn_id`. Later duplicates win, on
    the assumption that an append-only log's later entry supersedes an
    earlier one for the same turn."""
    idx: dict[str, dict] = {}
    if not feedback_path.is_file():
        return idx
    try:
        lines = feedback_path.read_text(encoding="utf-8").splitlines()
    except Exception:
        return idx
    for ln in lines:
        ln = ln.strip()
        if not ln:
            continue
        try:
            rec = json.loads(ln)
        except Exception:
            continue
        tid = rec.get("turn_id")
        if tid:
            idx[tid] = rec
    return idx


def _enrich_suspects_with_feedback(suspects: list[dict],
                                   feedback_idx: dict[str, dict]) -> list[dict]:
    """Attach `submitted_by` and the full feedback detail to each suspect."""
    for s in suspects:
        rec = feedback_idx.get(s["turn_id"]) or {}
        s["submitted_by"] = rec.get("submitted_by")
        s["feedback_detail"] = {
            "vote": rec.get("vote"),
            "weight": rec.get("weight"),
            "issues": rec.get("issues"),
            "general_comment": rec.get("general_comment"),
            "yaml_modified": rec.get("yaml_modified"),
            "attached_yaml": rec.get("attached_yaml"),
            "session_id": rec.get("session_id"),
        }
    return suspects


# --- LLM detective ----------------------------------------------------------
DETECTIVE_SYSTEM = (
    "You are a diligent forensic analyst for an LLM playbook system. A "
    "bullet in the playbook was flagged as 'harmful' by an automated "
    "reviewer, meaning it degraded the agent's answer quality. You are "
    "given the reviewer's explanation of why the bullet is harmful, the "
    "bullet's current content, and every turn that plausibly caused the "
    "damage (each with its user feedback, curator reasoning, and the exact "
    "edit performed on the bullet). Identify the single most likely culprit "
    "turn and explain your reasoning briefly. Output STRICT JSON only, no "
    "prose, no markdown fences."
)

DETECTIVE_JSON_SCHEMA = (
    "{\n"
    '  "main_suspect": {\n'
    '    "turn_id":         "<turn_id or null>",\n'
    '    "submitted_by":    "<name or null>",\n'
    '    "conversation_id": "<id or null>",\n'
    '    "confidence":      <float 0.0-1.0>,\n'
    '    "reasoning":       "<one or two sentences>"\n'
    "  },\n"
    '  "other_suspects": [\n'
    '    { "turn_id": "<id>", "submitted_by": "<name or null>", "note": "<short>" }\n'
    "  ]\n"
    "}"
)


def _build_detective_prompt(bullet_id: str, why: str,
                            current_content: str,
                            suspects: list[dict]) -> str:
    """Compact dossier: cause of death + one section per suspect. Excludes
    reflection / full agent answer to keep focus on the feedback -> curator
    -> edit chain, which is where the culprit signal lives."""
    lines: list[str] = []
    lines.append(f"Bullet ID: {bullet_id}")
    lines.append("Reviewer's cause-of-death (why harmful):")
    lines.append(f"  {why}")
    lines.append("")
    lines.append("Bullet content NOW (post-corruption):")
    lines.append(f"  {current_content}")
    lines.append("")
    lines.append(f"Suspect turns ({len(suspects)}), newest first:")
    for i, s in enumerate(suspects, 1):
        lines.append(f"--- Suspect {i} ---")
        lines.append(f"  turn_id:         {s.get('turn_id')}")
        lines.append(f"  conversation_id: {s.get('conversation_id')}")
        lines.append(f"  ts:              {s.get('ts')}")
        lines.append(f"  submitted_by:    {s.get('submitted_by')}")
        fb = s.get("feedback_detail") or {}
        lines.append(f"  feedback.vote:            {fb.get('vote')}")
        lines.append(f"  feedback.weight:          {fb.get('weight')}")
        lines.append(f"  feedback.issues:          "
                     f"{json.dumps(fb.get('issues'), ensure_ascii=False)}")
        lines.append(f"  feedback.general_comment: {fb.get('general_comment')}")
        lines.append(f"  curator_reasoning:")
        lines.append(f"    {s.get('curator_reasoning') or '(none)'}")
        lines.append(f"  ops_on_this_bullet:")
        for op in (s.get("ops") or []):
            detail = (op.get("detail") or "").strip().replace("\n", " ")
            if len(detail) > 400:
                detail = detail[:400] + "…"
            lines.append(f"    - [{op.get('type')}] {detail}")
    lines.append("")
    lines.append("Return STRICT JSON with this shape:")
    lines.append(DETECTIVE_JSON_SCHEMA)
    return "\n".join(lines)


def _extract_usage(resp) -> dict:
    """Pull `{prompt,completion,total}_tokens` from an OpenAI-style response,
    tolerating both attribute-style and dict-style usage payloads."""
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


def _parse_detective_json(raw: str) -> dict | None:
    """Best-effort JSON parse: try raw first; then strip common markdown
    fences; then fall back to the outermost `{...}` slice."""
    raw = (raw or "").strip()
    if not raw:
        return None
    candidates: list[str] = [raw]
    stripped = raw.strip("`").lstrip("json").strip()
    if stripped and stripped != raw:
        candidates.append(stripped)
    if "{" in raw and "}" in raw:
        candidates.append(raw[raw.find("{"): raw.rfind("}") + 1])
    for c in candidates:
        try:
            obj = json.loads(c)
        except Exception:
            continue
        if isinstance(obj, dict):
            return obj
    return None


def _run_detective(llm, bullet_id: str, why: str,
                   current_content: str,
                   suspects: list[dict]) -> tuple[dict, dict]:
    """One LLM call per corrupted bullet. Returns (verdict, usage).
    Catches everything the LLM/network can throw so a single failure cannot
    abort the outer loop over the other corrupted bullets."""
    if not suspects:
        return {
            "status": "no_suspects",
            "note": ("No turns touched this bullet within the +/-1 day "
                     "window of created_at/updated_at — turn log likely "
                     "expired or the bullet was corrupted outside the "
                     "logging window."),
        }, {}
    prompt = _build_detective_prompt(bullet_id, why, current_content, suspects)
    try:
        resp = llm.client.chat.completions.create(
            model=llm.model,
            messages=[
                {"role": "system", "content": DETECTIVE_SYSTEM},
                {"role": "user", "content": prompt},
            ],
            temperature=LLM_TEMPERATURE,
            max_tokens=LLM_MAX_TOKENS,
        )
        content = (resp.choices[0].message.content or "").strip()
        usage = _extract_usage(resp)
    except Exception as e:
        return {"status": "llm_failed", "error": str(e)}, {}

    parsed = _parse_detective_json(content)
    if parsed is None:
        return {"status": "llm_output_unparseable", "raw": content[:2000]}, usage
    ms = parsed.get("main_suspect") if isinstance(parsed.get("main_suspect"), dict) else None
    try:
        ms_conf = float((ms or {}).get("confidence") or 0.0)
    except (TypeError, ValueError):
        ms_conf = 0.0
    confident = ms is not None and ms_conf >= LLM_CONFIDENCE_GATE
    if ms is not None:
        ms["is_confident_guess"] = confident
    parsed["status"] = "ok" if confident else "ok_low_confidence"
    parsed["confidence_gate"] = LLM_CONFIDENCE_GATE
    return parsed, usage


# --- output -----------------------------------------------------------------
def _print_bullet_report(entry: dict) -> None:
    """Human-readable console report for one corrupted bullet."""
    print("\n" + "=" * 72)
    print(f"bullet_id : {entry['bullet_id']}")
    print(f"why       : {entry['why']}")
    verdict = entry.get("verdict") or {}
    status = verdict.get("status") or "unknown"
    print(f"status    : {status}")
    if status in ("ok", "ok_low_confidence"):
        ms = verdict.get("main_suspect") or {}
        header = "MAIN SUSPECT"
        if status == "ok_low_confidence":
            header += (f"  [!] LOW CONFIDENCE (< {LLM_CONFIDENCE_GATE}) - "
                       f"treat as best guess, not accusation")
        print(header)
        print(f"  turn_id           : {ms.get('turn_id')}")
        print(f"  submitted_by      : {ms.get('submitted_by')}")
        print(f"  conversation_id   : {ms.get('conversation_id')}")
        print(f"  confidence        : {ms.get('confidence')}")
        print(f"  is_confident_guess: {ms.get('is_confident_guess')}")
        print(f"  reasoning         : {ms.get('reasoning')}")
        others = verdict.get("other_suspects") or []
        if others:
            print(f"OTHER SUSPECTS ({len(others)}):")
            for o in others:
                print(f"  - turn_id={o.get('turn_id')} "
                      f"by={o.get('submitted_by')} "
                      f"note={o.get('note')}")
    elif status == "no_suspects":
        print(f"  {verdict.get('note')}")
    elif status == "bullet_not_found":
        print(f"  {verdict.get('note')}")
    elif status == "llm_failed":
        print(f"  error: {verdict.get('error')}")
    elif status == "llm_output_unparseable":
        print("  LLM output could not be parsed as JSON.")
    print(f"suspects_considered: {len(entry.get('suspects') or [])}")


def _stamp_from_review(review_path: Path) -> str:
    """Reuse the timestamp already in the review filename so the killers
    file lines up naturally with its source review."""
    name = review_path.stem
    if name.startswith("review_"):
        return name[len("review_"):]
    return datetime.now().strftime("%Y%m%dT%H%M%S")


# --- orchestration ----------------------------------------------------------
def process(review_path: Path,
            namespace: str = "wifi",
            model: str | None = None,
            json_only: bool = False) -> dict:
    review_path = _resolve_review_path(Path(review_path))
    review = json.loads(review_path.read_text(encoding="utf-8"))
    live_dir = _resolve_live_dir(namespace)
    turns_dir = live_dir / "history" / "turns"
    feedback_path = Path(r"\\infs089.iil.intel.com\HOME\WirelessCE\Intel_WirelessCE_Avatar\feedback\feedback_details.jsonl")
    snapshot_dir = _newest_snapshot_dir(SNAPSHOTS_DIR)

    corrupted = _extract_corrupted_bullets(review)
    if not json_only:
        print(f"[killer] review        : {review_path.name}")
        print(f"[killer] live dir      : {live_dir}")
        print(f"[killer] turns dir     : {turns_dir}")
        print(f"[killer] feedback file : "
              f"{feedback_path.name if feedback_path.is_file() else '(missing)'}")
        print(f"[killer] snapshot dir  : "
              f"{snapshot_dir if snapshot_dir else '(none reachable)'}")
        print(f"[killer] corrupted     : "
              f"{[c['bullet_id'] for c in corrupted] or '(none)'}")

    feedback_idx = _load_feedback_index(feedback_path)
    # Lazily built on the first bullet that actually has suspects — avoids
    # paying the .env / keys.py resolution cost when there is nothing to ask.
    llm = None
    entries: list[dict] = []
    total_usage = {
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
        "calls": 0,
    }

    for c in corrupted:
        bullet_id = c["bullet_id"]
        bullet, source_pb = _find_bullet_in_playbooks(
            bullet_id,
            [live_dir] + ([snapshot_dir] if snapshot_dir else []),
        )
        if bullet is None:
            entry = {
                **c,
                "verdict": {
                    "status": "bullet_not_found",
                    "note": ("Bullet is not present in the live playbook or "
                             "the newest snapshot; cannot scope the search "
                             "without its created_at / updated_at / "
                             "source_turn_ids."),
                },
                "suspects": [],
            }
            entries.append(entry)
            if not json_only:
                _print_bullet_report(entry)
            continue

        suspects = _collect_suspect_turns(bullet_id, bullet, turns_dir)
        suspects = _enrich_suspects_with_feedback(suspects, feedback_idx)

        if llm is None and suspects:
            from services.ace.cli import _build_llm
            llm = _build_llm(model)

        verdict, usage = _run_detective(
            llm, bullet_id, c["why"],
            bullet.get("content") or "", suspects,
        )
        if usage:
            for k in ("prompt_tokens", "completion_tokens", "total_tokens"):
                total_usage[k] += usage.get(k, 0)
            total_usage["calls"] += 1

        entry = {
            **c,
            "playbook_file": source_pb.name if source_pb else None,
            "bullet_current_content": bullet.get("content"),
            "bullet_updated_at": bullet.get("updated_at"),
            "bullet_created_at": bullet.get("created_at"),
            "suspects": suspects,
            "verdict": verdict,
            "llm_usage": usage,
        }
        entries.append(entry)
        if not json_only:
            _print_bullet_report(entry)

    report = {
        "review": review_path.name,
        "namespace": namespace,
        "ts_local": datetime.now().astimezone().isoformat(),
        "corrupted_count": len(corrupted),
        "results": entries,
        "llm_usage_total": total_usage,
    }

    stamp = _stamp_from_review(review_path)
    out_dir = Path(__file__).resolve().parent / "runs" / stamp
    out_dir.mkdir(parents=True, exist_ok=True)
    out_file = out_dir / f"killers_{stamp}.json"
    out_file.write_text(
        json.dumps(report, indent=2, ensure_ascii=False, default=str) + "\n",
        encoding="utf-8",
    )

    if json_only:
        print(json.dumps(report, indent=2, ensure_ascii=False, default=str))
    else:
        print("\n" + "=" * 72)
        print(f"[killer] wrote {out_file}")
        print(f"[killer] llm token usage : "
              f"prompt={total_usage['prompt_tokens']} "
              f"completion={total_usage['completion_tokens']} "
              f"total={total_usage['total_tokens']} "
              f"calls={total_usage['calls']}")

    return report


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="python -m services.ace.eval.find_the_killer",
        description=("Post-review whodunit: for each bullet the reviewer "
                     "flagged as harmful+revert (confidence >= gate), scan "
                     "the turn log within +/-1 day of the bullet's "
                     "created_at/updated_at, enrich with feedback_details, "
                     "and ask the LLM which feedback most likely corrupted it."),
    )
    p.add_argument("review", type=Path,
                   help="Path (or bare filename / stamp) of a review_*.json "
                        "produced by services.ace.eval.review. Same "
                        "resolution rules as services.ace.eval.corrupted_bullet.")
    p.add_argument("--namespace", choices=("wifi", "bt"), default="wifi",
                   help="Which live playbook set to inspect (default wifi).")
    p.add_argument("--model", default=None,
                   help="Override the default LLM model name.")
    p.add_argument("--json", action="store_true",
                   help="Emit the final JSON report on stdout in addition "
                        "to writing the file.")
    args = p.parse_args(argv)
    process(args.review, namespace=args.namespace,
            model=args.model, json_only=args.json)
    return 0


if __name__ == "__main__":
    sys.exit(main())
