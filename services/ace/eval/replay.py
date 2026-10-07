"""
Headless agent replay against a specific playbook state.

Isolation guarantees (all verified against the current code):
  * The agent never imports feedback_service — persistence happens only in
    the Flask routes — so a headless replay cannot write production feedback.
  * The agent's ACE usage is render-only (`render_workflow` /
    `render_domain`); nothing writes a playbook unless `run_one/run_batch`
    is called, which replay never does. Belt-and-braces: the runner is built
    over a TEMP COPY of the playbook dir with a _NullLLM that raises if any
    LLM-backed write path is ever reached.
  * A fresh WifiLogAgentSystem per replay — no cache bleed between the
    "before" and "after" arms.
"""

from __future__ import annotations

import json
import shutil
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Callable, Iterator, Optional

from .cases import EvalCase

# Dotfiles that must not follow the playbooks into the temp copy.
_COPY_SKIP_NAMES = {".ace_cursor.json", ".ace_nightly.json", "meta.json"}

# Cap how much of each captured step we keep in the result (full traces can
# embed 16K-char log payloads; reports only need enough to debug the run).
_STEP_CONTENT_CAP = 400
_MAX_STEPS_KEPT = 80


class _NullLLM:
    """AceRunner requires an llm argument. Replay only exercises the render
    path; if any code change ever routes a replay into Reflector/Curator,
    this makes it fail loudly instead of silently burning tokens or writing
    playbooks."""

    def chat(self, *args, **kwargs):
        raise RuntimeError("read-only eval replay must never call the ACE LLM")


@dataclass
class ReplayResult:
    case_key: str                     # "<cid>/<tid>"
    label: str                        # "before" | "after"
    result_type: str = ""             # report | partial_report | text | error
    report: Optional[dict] = None     # submit_final_report args when report/partial
    text: str = ""                    # text/error payload otherwise
    skills_invoked: list[str] = field(default_factory=list)
    applied_bullet_ids: list[str] = field(default_factory=list)
    steps: list[dict] = field(default_factory=list)
    step_count: int = 0
    duration_ms: int = 0
    error: Optional[str] = None

    def report_text(self) -> str:
        """Flatten the report into one text blob for scoring / judging."""
        if not self.report:
            return self.text or ""
        parts = []
        for k in ("root_cause_summary", "markdown_summary"):
            v = self.report.get(k)
            if v:
                parts.append(str(v))
        actions = self.report.get("recommended_actions")
        if isinstance(actions, list):
            parts.extend(str(a) for a in actions)
        return "\n".join(parts)

    def to_dict(self) -> dict:
        return asdict(self)


@contextmanager
def temp_playbooks(src_dir: Path) -> Iterator[Path]:
    """Copy playbook *.json from src_dir into a temp dir; clean up after.
    Works for both a live playbooks dir and a HistoryWriter snapshot run-dir
    (same filenames in both)."""
    tmp = Path(tempfile.mkdtemp(prefix="ace_eval_pb_"))
    try:
        src = Path(src_dir)
        if src.exists():
            for f in src.glob("*.json"):
                if f.name in _COPY_SKIP_NAMES:
                    continue
                shutil.copy2(f, tmp / f.name)
        yield tmp
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def build_agent(llm_client, model: str, skills: dict):
    """Fresh WifiLogAgentSystem — clean caches and history every replay."""
    from services.chatbot.engine.system import WifiLogAgentSystem
    return WifiLogAgentSystem(client=llm_client, model=model, skills=skills)


def _extract_skills_invoked(steps: list[dict], report: Optional[dict]) -> list[str]:
    """Union of skills seen in the step stream (fetch messages) and the
    report's involved_skills, preserving first-seen order."""
    import re
    out: list[str] = []
    seen: set[str] = set()
    inv_re = re.compile(r"`([^`]+)`")
    for s in steps:
        content = s.get("content", "")
        if not isinstance(content, str):
            continue
        if "Fetching filtered logs" in content or "Fetching Filtered Logs" in content:
            m = inv_re.search(content)
            if m:
                sid = m.group(1).strip()
                if sid and sid not in seen:
                    seen.add(sid)
                    out.append(sid)
    if report:
        for sid in report.get("involved_skills") or []:
            sid = str(sid).strip()
            if sid and sid not in seen:
                seen.add(sid)
                out.append(sid)
    return out


def replay_case(
    case: EvalCase,
    playbooks_src: Path,
    llm,
    skills: dict,
    label: str,
    max_steps: int = 6,
    progress: Optional[Callable[[dict], None]] = None,
) -> ReplayResult:
    """Run one case headlessly against the playbook state in `playbooks_src`.

    llm:    an LLM_helper-shaped object (needs .client and .model).
    skills: Dict[str, Skill] — same skills the live agent uses.
    """
    from ..pipeline import AceRunner

    result = ReplayResult(case_key=case.key, label=label)
    t0 = time.time()
    captured: list[dict] = []

    def _capture(step):
        if isinstance(step, dict):
            row = {
                "role": str(step.get("role") or ""),
                "content": str(step.get("content") or "")[:_STEP_CONTENT_CAP],
            }
            if len(captured) < _MAX_STEPS_KEPT:
                captured.append(row)
            if progress:
                try:
                    progress({"case_key": case.key, "label": label, **row})
                except Exception:
                    pass

    try:
        with temp_playbooks(playbooks_src) as tmp_pb_dir:
            runner = AceRunner(
                llm=_NullLLM(),
                playbooks_dir=tmp_pb_dir,
                feedback_root=case.feedback_root,
            )
            agent = build_agent(llm.client, llm.model, skills)
            agent.attach_ace(runner)
            # Copy the log into the replay's private temp dir. The agent
            # writes side-car artifacts (e.g. PreScan's scoped.txt) NEXT TO
            # the log file — replaying directly against an attached log on
            # the shared feedback folder would litter the share.
            log_copy = ""
            if case.log.resolved_path:
                src_log = Path(case.log.resolved_path)
                dst_log = tmp_pb_dir / src_log.name
                shutil.copy2(str(src_log), str(dst_log))
                log_copy = str(dst_log)
            # Log path must be set BEFORE prime_with_context — issue-time
            # alignment reads the log to anchor the timestamp frame.
            agent.current_log_path = log_copy
            agent.prime_with_context(
                case_nbr=case.issue.get("case_nbr", ""),
                subject=case.issue.get("subject", ""),
                description=case.issue.get("description", ""),
                issue_type=case.issue.get("issue_type", ""),
                attachment_time=case.issue.get("attachment_time", ""),
            )
            res = agent.chat(
                case.user_message,
                max_steps=max_steps,
                step_callback=_capture,
            )

        result.result_type = (res or {}).get("type", "") if isinstance(res, dict) else ""
        data = (res or {}).get("data") if isinstance(res, dict) else None
        if result.result_type in ("report", "partial_report") and isinstance(data, dict):
            result.report = data
            result.applied_bullet_ids = [
                str(b) for b in (data.get("applied_bullet_ids") or [])
            ]
        elif data is not None:
            result.text = data if isinstance(data, str) else json.dumps(data, default=str)
    except Exception as e:
        result.result_type = "error"
        result.error = f"{type(e).__name__}: {e}"

    result.steps = captured
    result.step_count = len(captured)
    result.skills_invoked = _extract_skills_invoked(captured, result.report)
    result.duration_ms = int((time.time() - t0) * 1000)
    return result
