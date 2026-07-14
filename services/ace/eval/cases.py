"""
Eval-case registry — turns voted feedback conversations into replayable
evaluation cases.

A case is one (conversation, turn) pair carrying:
  * the original issue context + user message (what to replay),
  * a resolved log file path (attached log preferred; a scrubbed `log_path`
    is only usable when it un-scrubs to a file on THIS machine),
  * the user-supplied ground truth (correct_root_cause / conclusion tag /
    skill / evidence lines) used for scoring.

Nothing here calls an LLM or writes anything — the registry is a pure read
layer, cheap enough for the UI to refresh on demand.
"""

from __future__ import annotations

import json
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, asdict, replace
from pathlib import Path
from typing import Iterable, Iterator, Optional


# Mirror of feedback_service._USER_HOME_RE — the scrubber that replaced the
# username in path-like strings before they hit the shared snapshot.
_USERHOME_TOKEN = "<USERHOME>"
_USERHOME_RE = re.compile(r"([A-Za-z]:\\Users\\)<USERHOME>", re.IGNORECASE)

# Filename prefix per domain (mirror of feedback_service._DOMAIN_PREFIXES).
_DOMAIN_PREFIXES = {"bt": "bt_"}


def _domain_prefix(domain: str) -> str:
    return _DOMAIN_PREFIXES.get((domain or "").strip().lower(), "")


@dataclass
class LogResolution:
    resolved_path: Optional[str]      # usable local path or None
    source: str                       # "attached" | "log_path" | "none"
    candidates: list[str] = field(default_factory=list)
    reason: str = ""                  # why unresolvable, when source == "none"


@dataclass
class GroundTruth:
    vote: int = 0
    weight: str = ""
    correct_root_cause: str = ""
    correct_conclusion_tag: str = ""
    correct_skill: str = ""
    correct_approach: str = ""
    evidence_log_lines: list[str] = field(default_factory=list)
    skills_used: list[str] = field(default_factory=list)
    baseline_report: Optional[dict] = None   # the recorded agent_response_full

    @property
    def rich(self) -> bool:
        """True when there is enough structure for deterministic scoring."""
        return bool(self.correct_root_cause or self.correct_conclusion_tag
                    or self.evidence_log_lines)


@dataclass
class EvalCase:
    conversation_id: str
    turn_id: str
    domain: str                       # "wifi" | "bt"
    feedback_root: str
    ts: str                           # feedback / turn timestamp (recency key)
    issue: dict                       # case_nbr, subject, description, issue_type, attachment_time
    user_message: str
    mode: str                         # replay requires "tools"
    ground_truth: GroundTruth
    log: LogResolution
    replayable: bool
    reasons: list[str] = field(default_factory=list)
    golden: bool = False              # joined from GoldenSet by list_cases

    @property
    def key(self) -> str:
        return f"{self.conversation_id}/{self.turn_id}"

    def summary(self) -> dict:
        """Thin dict for API responses / CLI listings — no log contents."""
        return {
            "conversation_id": self.conversation_id,
            "turn_id": self.turn_id,
            "domain": self.domain,
            "ts": self.ts,
            "case_nbr": self.issue.get("case_nbr") or "",
            "subject": self.issue.get("subject") or "",
            "issue_type": self.issue.get("issue_type") or "",
            "vote": self.ground_truth.vote,
            "weight": self.ground_truth.weight,
            "has_root_cause": bool(self.ground_truth.correct_root_cause),
            "conclusion_tag": self.ground_truth.correct_conclusion_tag,
            "correct_skill": self.ground_truth.correct_skill,
            "evidence_lines": len(self.ground_truth.evidence_log_lines),
            "rich_ground_truth": self.ground_truth.rich,
            "log_source": self.log.source,
            "replayable": self.replayable,
            "reasons": self.reasons,
            "golden": self.golden,
        }


def unscrub_path(scrubbed: str) -> Optional[str]:
    """Recover a privacy-scrubbed path for the CURRENT user.

    `C:\\Users\\<USERHOME>\\...` → `<Path.home()>\\...`. Only meaningful when
    the snapshot was written by this same user — other users' paths will
    unscrub to a non-existent file and be rejected by the existence check
    in resolve_case_log. Returns the candidate string (existence NOT checked
    here) or None when there is nothing to recover.
    """
    s = (scrubbed or "").strip()
    if not s:
        return None
    if _USERHOME_TOKEN not in s:
        return s   # unscathed path (e.g. a share path) — caller checks existence
    home = Path.home()
    # Replace drive-qualified C:\Users\<USERHOME> with the real home dir
    # (the text after the token already starts with a backslash).
    out = _USERHOME_RE.sub(lambda m: str(home), s)
    # Guard against a stray token that didn't match the drive-qualified form.
    if _USERHOME_TOKEN in out:
        return None
    return out


def resolve_case_log(feedback_root: Path, snapshot: dict, turn_id: str) -> LogResolution:
    """Find a usable log file for one turn.

    Priority:
      1. attached log for THIS turn:  logs/<prefix><cid>/<turn_id>__*
      2. any other attached log for the conversation (same session log,
         attached from a different turn)
      3. the snapshot's `log_path` after un-scrubbing, if it exists locally
    """
    cid = snapshot.get("conversation_id") or ""
    domain = snapshot.get("domain") or "wifi"
    candidates: list[str] = []

    logs_dir = Path(feedback_root) / "logs" / f"{_domain_prefix(domain)}{cid}"
    try:
        if logs_dir.exists():
            # Ignore side-car artifacts the agent writes next to logs
            # (PreScan's scoped.txt) — they are derived output, not a log.
            files = sorted(p for p in logs_dir.iterdir()
                           if p.is_file() and p.name != "scoped.txt")
            # 1. exact-turn attachment
            for p in files:
                candidates.append(str(p))
                if p.name.startswith(f"{turn_id}__"):
                    return LogResolution(str(p), "attached", candidates)
            # 2. any attachment from the same conversation
            if files:
                return LogResolution(str(files[0]), "attached", candidates)
    except Exception as e:
        candidates.append(f"(logs dir error: {e})")

    # 3. recorded log_path (scrubbed) — only works for the submitting user's
    # own machine or a share path that is still reachable.
    raw = snapshot.get("log_path") or ""
    cand = unscrub_path(raw)
    if cand:
        candidates.append(cand)
        try:
            if Path(cand).is_file():
                return LogResolution(cand, "log_path", candidates)
        except Exception:
            pass

    reason = "no attached log" + (f"; log_path unusable ({raw})" if raw else "; no log_path recorded")
    return LogResolution(None, "none", candidates, reason)


def _ground_truth_from_turn(turn: dict) -> GroundTruth:
    fb = turn.get("feedback") or {}
    details = fb.get("details") or {}
    evidence = details.get("evidence_log_lines") or []
    if not isinstance(evidence, list):
        evidence = []
    skills_used = []
    for s in turn.get("skills_used") or []:
        sid = (s.get("skill_id") or "").strip() if isinstance(s, dict) else ""
        if sid and sid not in skills_used:
            skills_used.append(sid)

    baseline = turn.get("agent_response_full")
    if not isinstance(baseline, dict):
        baseline = None

    return GroundTruth(
        vote=fb.get("vote") or 0,
        weight=fb.get("weight") or "",
        correct_root_cause=(details.get("correct_root_cause") or "").strip(),
        correct_conclusion_tag=(details.get("correct_conclusion_tag") or "").strip(),
        correct_skill=(details.get("correct_skill") or "").strip(),
        correct_approach=(details.get("correct_approach") or "").strip(),
        evidence_log_lines=[str(x).rstrip() for x in evidence if str(x).strip()],
        skills_used=skills_used,
        baseline_report=baseline,
    )


def _build_cases_for_snapshot(feedback_root: Path, f: Path,
                              snap: dict) -> list[EvalCase]:
    """Extract every feedback-carrying turn of one parsed snapshot."""
    snap_domain = (snap.get("domain") or "wifi").strip().lower()
    # BT files also carry a bt_ filename prefix; the domain field is
    # authoritative but check the prefix as a fallback for old files.
    if snap_domain == "wifi" and f.name.startswith("bt_"):
        snap_domain = "bt"
    cid = snap.get("conversation_id") or f.stem
    issue = snap.get("issue") or {}
    out: list[EvalCase] = []
    for turn in snap.get("turns") or []:
        tid = turn.get("turn_id") or ""
        fb = turn.get("feedback")
        if not tid or not fb:
            continue

        gt = _ground_truth_from_turn(turn)
        log = resolve_case_log(Path(feedback_root), snap, tid)
        mode = (turn.get("mode") or "").strip()
        user_message = (turn.get("user_message") or "").strip()

        reasons: list[str] = []
        if mode != "tools":
            reasons.append(f"mode is '{mode}' (replay needs the agentic 'tools' mode)")
        if not user_message:
            reasons.append("empty user_message")
        if log.resolved_path is None:
            reasons.append(log.reason or "log unresolvable")
        replayable = not reasons

        out.append(EvalCase(
            conversation_id=cid,
            turn_id=tid,
            domain=snap_domain,
            feedback_root=str(feedback_root),
            ts=turn.get("ts") or snap.get("ended_at") or "",
            issue={
                "case_nbr": issue.get("case_nbr") or "",
                "subject": issue.get("subject") or "",
                "description": issue.get("description") or "",
                "issue_type": issue.get("issue_type") or "",
                "attachment_time": issue.get("attachment_time") or "",
            },
            user_message=user_message,
            mode=mode,
            ground_truth=gt,
            log=log,
            replayable=replayable,
            reasons=reasons,
        ))
    return out


# Per-file case cache. Parsing a snapshot AND probing its attached-logs dir
# costs several SMB round trips; keyed by mtime so unchanged files are free
# on subsequent scans (mirrors server._CONV_META_CACHE).
_CASE_CACHE: dict[str, tuple[float, list[EvalCase]]] = {}
_CASE_CACHE_LOCK = threading.Lock()
# Concurrency for the first (cold) scan of a remote share — the work is SMB
# latency-bound, so a modest pool cuts a ~N×RTT scan by ~8x.
_SCAN_WORKERS = 8


def _cases_for_file(feedback_root: Path, f: Path,
                    force: bool = False,
                    mtime: Optional[float] = None) -> list[EvalCase]:
    key = str(f)
    if mtime is None:
        try:
            mtime = f.stat().st_mtime
        except Exception:
            return []
    if not force:
        with _CASE_CACHE_LOCK:
            hit = _CASE_CACHE.get(key)
        if hit and hit[0] == mtime:
            return hit[1]
    try:
        snap = json.loads(f.read_text(encoding="utf-8"))
    except Exception:
        return []
    cases = _build_cases_for_snapshot(feedback_root, f, snap)
    with _CASE_CACHE_LOCK:
        _CASE_CACHE[key] = (mtime, cases)
    return cases


def _scan_conv_dir(conv_dir: Path) -> list[tuple[Path, float]]:
    """(path, mtime) for every conversation JSON via ONE directory
    enumeration. On Windows, DirEntry.stat() is served from the enumeration
    data — no per-file round trip, which matters enormously on SMB."""
    import os
    out: list[tuple[Path, float]] = []
    try:
        with os.scandir(conv_dir) as it:
            for entry in it:
                if not entry.name.endswith(".json"):
                    continue
                try:
                    if not entry.is_file():
                        continue
                    out.append((Path(entry.path), entry.stat().st_mtime))
                except Exception:
                    continue
    except Exception:
        return []
    out.sort(key=lambda t: t[0].name)
    return out


def iter_cases(feedback_root: Path, domain: str = "wifi") -> Iterator[EvalCase]:
    """Walk conversations/*.json under one feedback root; yield one EvalCase
    per feedback-carrying turn. Malformed files are skipped silently."""
    conv_dir = Path(feedback_root) / "conversations"
    if not conv_dir.exists():
        return
    for f, mtime in _scan_conv_dir(conv_dir):
        for case in _cases_for_file(Path(feedback_root), f, mtime=mtime):
            if domain and case.domain != domain:
                continue
            yield case


def list_cases(feedback_roots: Iterable[Path], domain: str = "wifi",
               only_replayable: bool = False,
               golden: Optional["GoldenSet"] = None,
               force: bool = False) -> list[EvalCase]:
    """Collect cases across roots (remote + local fallback), dedupe by
    (conversation_id, turn_id) — first root wins (pass remote first).
    Joins the golden flag when a GoldenSet is provided.

    Per-file results are cached by mtime; `force=True` re-parses everything
    (e.g. after a golden log-pin changed a case's resolution without
    touching its snapshot file). The cold scan fans out over a small thread
    pool because each file costs several SMB round trips.
    """
    # One-shot golden membership — checking per case via golden.contains()
    # would re-stat the shared registry file once per case over SMB.
    golden_pairs: set = set()
    golden_whole: set = set()
    if golden is not None:
        golden_pairs, golden_whole = golden.key_sets()

    seen: set[tuple[str, str]] = set()
    out: list[EvalCase] = []
    for root in feedback_roots:
        if root is None:
            continue
        conv_dir = Path(root) / "conversations"
        if not conv_dir.exists():
            continue
        files = _scan_conv_dir(conv_dir)
        with ThreadPoolExecutor(max_workers=_SCAN_WORKERS) as pool:
            per_file = pool.map(
                lambda fm, _r=Path(root): _cases_for_file(
                    _r, fm[0], force=force, mtime=fm[1]),
                files,
            )
        for cases in per_file:
            for case in cases:
                if domain and case.domain != domain:
                    continue
                k = (case.conversation_id, case.turn_id)
                if k in seen:
                    continue
                seen.add(k)
                # Cached instances are shared — copy before flag mutation.
                case = replace(case)
                if golden is not None:
                    case.golden = (k in golden_pairs
                                   or case.conversation_id in golden_whole)
                if only_replayable and not case.replayable:
                    continue
                out.append(case)
    out.sort(key=lambda c: c.ts or "", reverse=True)
    return out


def select_cases(cases: list[EvalCase], max_cases: int,
                 conversation_ids: Optional[list[str]] = None,
                 source: str = "auto") -> list[EvalCase]:
    """Pick the cases an eval run will replay.

    source:
      "golden"          — golden-marked cases only.
      "golden+affected" — golden cases plus every case from the conversations
                          in `conversation_ids` (the just-adapted ones).
      "auto"            — ranked auto-selection (rich negatives → negatives →
                          recent), one case per conversation.

    Always capped at max_cases; `conversation_ids` cases are placed first so
    the post-adapt gate never crowds them out.
    """
    replayable = [c for c in cases if c.replayable]
    affected_set = set(conversation_ids or [])

    def _affected(pool):
        return [c for c in pool if c.conversation_id in affected_set]

    picked: list[EvalCase] = []
    seen_keys: set[str] = set()

    def _take(pool):
        for c in pool:
            if len(picked) >= max_cases:
                return
            if c.key in seen_keys:
                continue
            seen_keys.add(c.key)
            picked.append(c)

    if source == "golden":
        _take([c for c in replayable if c.golden])
        return picked

    if source == "golden+affected":
        _take(_affected(replayable))          # affected first — never crowded out
        _take([c for c in replayable if c.golden])
        return picked

    # auto: affected first, then ranked pool (one case per conversation)
    _take(_affected(replayable))
    ranked = sorted(
        replayable,
        key=lambda c: (
            0 if (c.ground_truth.rich and c.ground_truth.vote < 0)
            else 1 if c.ground_truth.vote < 0
            else 2,
        ),
    )
    per_conv_seen: set[str] = {c.conversation_id for c in picked}
    for c in ranked:
        if len(picked) >= max_cases:
            break
        if c.key in seen_keys or c.conversation_id in per_conv_seen:
            continue
        seen_keys.add(c.key)
        per_conv_seen.add(c.conversation_id)
        picked.append(c)
    return picked


def case_to_dict(case: EvalCase) -> dict:
    """Full JSON-safe dump (for reports)."""
    d = asdict(case)
    return d
