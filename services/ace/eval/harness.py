"""
EvalHarness — orchestrates before/after replays, scoring, the gate, and
rollback.

Gate semantics:
    skipped              gate disabled in config
    insufficient-cases   fewer than gate_min_cases scored — never rollback
                         on a tiny sample
    rollback             regressed >= improved + gate_margin
    keep                 otherwise

Rollback restores playbook JSONs from the "before" snapshot via
HistoryWriter.restore_snapshot, after first archiving the current live state
as a "pre-rollback" snapshot so the rollback itself is reversible from the
History tab. The batch cursor is intentionally left alone.
"""

from __future__ import annotations

import shutil
import tempfile
import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Optional

from ..history import HistoryWriter
from .cases import list_cases, select_cases
from .golden import GoldenSet
from .judge import Judge
from .replay import replay_case
from .scoring import score_deterministic, case_verdict
from .store import EvalStore


@dataclass
class EvalConfig:
    max_cases: int = 6
    max_steps: int = 6
    gate: bool = True
    gate_min_cases: int = 2
    gate_margin: int = 1
    case_source: str = ""            # "" -> auto-pick: golden+affected if golden set non-empty else auto
    conversation_ids: list[str] = field(default_factory=list)
    before: str = ""                 # "YYYY-MM-DD/<run_dir>"; "" -> newest snapshot
    agent_model: str = ""
    judge_model: str = ""
    run_id: str = ""
    source: str = "manual-eval"      # | post-adapt-gate | nightly-gate | cli
    adapt_job_id: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


class EvalHarness:
    def __init__(
        self,
        *,
        playbooks_dir: Path,
        feedback_roots: list[Path],
        history: HistoryWriter,
        store: EvalStore,
        llm_factory: Callable[[Optional[str]], Any],
        skills_loader: Callable[[], dict],
        golden: Optional[GoldenSet] = None,
        emit: Optional[Callable[[str, dict], None]] = None,
        sync_fn: Optional[Callable[[str], None]] = None,
        domain: str = "wifi",
    ):
        """
        llm_factory:   model_id|None -> LLM_helper-shaped object (.client/.model/.chat)
        skills_loader: () -> Dict[str, Skill] (same skills the live agent uses)
        emit:          (event_name, payload) -> None  (Socket.IO / CLI progress)
        sync_fn:       (job_id) -> None  fire-and-forget remote sync after rollback
        """
        self.playbooks_dir = Path(playbooks_dir)
        self.feedback_roots = [Path(r) for r in feedback_roots if r]
        self.history = history
        self.store = store
        self.llm_factory = llm_factory
        self.skills_loader = skills_loader
        if golden is not None:
            self.golden = golden
        elif self.feedback_roots:
            self.golden = GoldenSet(self.feedback_roots[0])
        else:
            self.golden = None
        self.emit = emit or (lambda ev, payload: None)
        self.sync_fn = sync_fn
        self.domain = domain

    # ------------------------------------------------------------------
    def resolve_before_dir(self, before_ref: str) -> tuple[Optional[Path], str]:
        """Resolve "YYYY-MM-DD/<run_dir>" (or newest when empty) into the
        snapshot directory path. Returns (path|None, ref_string)."""
        snapshots = self.history.list_snapshots()
        if not snapshots:
            return None, ""
        if before_ref:
            try:
                date, run_dir = before_ref.split("/", 1)
            except ValueError:
                return None, before_ref
            for s in snapshots:
                if s["date"] == date and s["run_dir"] == run_dir:
                    p = (self.history._snapshots_dir / date / run_dir)
                    return (p if p.is_dir() else None), before_ref
            return None, before_ref
        newest = snapshots[0]
        ref = f"{newest['date']}/{newest['run_dir']}"
        p = self.history._snapshots_dir / newest["date"] / newest["run_dir"]
        return (p if p.is_dir() else None), ref

    # ------------------------------------------------------------------
    def run(self, config: EvalConfig) -> dict:
        run_id = config.run_id or uuid.uuid4().hex[:12]
        started_at = _now_iso()

        report: dict = {
            "schema_version": 1,
            "run_id": run_id,
            "source": config.source,
            "adapt_job_id": config.adapt_job_id or None,
            "started_at": started_at,
            "finished_at": None,
            "config": config.to_dict(),
            "before": {}, "after": {},
            "cases": [], "skipped_cases": [],
            "summary": {}, "gate": {},
            "error": None,
        }

        # ---- resolve before / after playbook dirs ----
        before_dir, before_ref = self.resolve_before_dir(config.before)
        if before_dir is None:
            report["error"] = (
                f"no usable 'before' snapshot (ref={config.before or 'newest'!r}); "
                "run an adapt with snapshotting first"
            )
            report["finished_at"] = _now_iso()
            self.emit("eval_error", {"run_id": run_id, "error": report["error"]})
            self.store.save(report)
            return report
        report["before"] = {"ref": before_ref}
        report["after"] = {"ref": "live", "playbooks_dir": str(self.playbooks_dir)}

        # ---- case selection ----
        if self.golden is not None:
            try:
                self.golden.refresh()
            except Exception:
                pass
        all_cases = list_cases(self.feedback_roots, domain=self.domain,
                               golden=self.golden)
        golden_count = sum(1 for c in all_cases if c.golden)
        source = config.case_source or (
            "golden+affected" if golden_count else "auto"
        )
        report["config"]["case_source_effective"] = source

        picked = select_cases(all_cases, config.max_cases,
                              conversation_ids=config.conversation_ids or None,
                              source=source)
        report["skipped_cases"] = [
            {"conversation_id": c.conversation_id, "turn_id": c.turn_id,
             "reasons": c.reasons}
            for c in all_cases
            if not c.replayable and (
                c.golden or c.conversation_id in set(config.conversation_ids or [])
            )
        ]

        self.emit("eval_started", {
            "run_id": run_id,
            "source": config.source,
            "case_source": source,
            "before_ref": before_ref,
            "golden_count": golden_count,
            "case_keys": [c.key for c in picked],
        })

        if not picked:
            report["error"] = "no replayable cases matched the selection"
            report["finished_at"] = _now_iso()
            self.emit("eval_error", {"run_id": run_id, "error": report["error"]})
            self.store.save(report)
            return report

        # ---- pin "after" so a concurrent playbook write mid-eval can't skew
        # later cases ----
        after_ref_dir = Path(tempfile.mkdtemp(prefix="ace_eval_after_"))
        try:
            for f in self.playbooks_dir.glob("*.json"):
                if f.name.startswith("."):
                    continue
                shutil.copy2(f, after_ref_dir / f.name)

            agent_llm = self.llm_factory(config.agent_model or None)
            judge_llm = (
                self.llm_factory(config.judge_model or None)
                if config.judge_model and config.judge_model != config.agent_model
                else agent_llm
            )
            judge = Judge(judge_llm)
            skills = self.skills_loader()

            counts = {"improved": 0, "regressed": 0, "same": 0, "inconclusive": 0}
            deltas: list[float] = []

            for case in picked:
                self.emit("eval_case_started", {
                    "run_id": run_id, "case_key": case.key,
                    "case_nbr": case.issue.get("case_nbr", ""),
                    "subject": case.issue.get("subject", ""),
                    "vote": case.ground_truth.vote,
                    "golden": case.golden,
                })

                def _progress(step, _ck=case.key):
                    self.emit("eval_replay_progress", {"run_id": run_id, **step})

                r_before = replay_case(case, before_dir, agent_llm, skills,
                                       "before", max_steps=config.max_steps,
                                       progress=_progress)
                r_after = replay_case(case, after_ref_dir, agent_llm, skills,
                                      "after", max_steps=config.max_steps,
                                      progress=_progress)

                det_b = score_deterministic(r_before, case.ground_truth)
                det_a = score_deterministic(r_after, case.ground_truth)
                jres = judge.compare(case, r_before, r_after)
                row = case_verdict(det_b, det_a, jres)

                counts[row["verdict"]] = counts.get(row["verdict"], 0) + 1
                if row["verdict"] != "inconclusive":
                    deltas.append(row["delta"])

                case_row = {
                    "conversation_id": case.conversation_id,
                    "turn_id": case.turn_id,
                    "golden": case.golden,
                    "case_nbr": case.issue.get("case_nbr", ""),
                    "subject": case.issue.get("subject", ""),
                    "vote": case.ground_truth.vote,
                    "log_source": case.log.source,
                    "ground_truth": {
                        "correct_root_cause": case.ground_truth.correct_root_cause,
                        "correct_conclusion_tag": case.ground_truth.correct_conclusion_tag,
                        "correct_skill": case.ground_truth.correct_skill,
                        "evidence_lines": len(case.ground_truth.evidence_log_lines),
                    },
                    **row,
                    "before_replay": {
                        "result_type": r_before.result_type,
                        "skills_invoked": r_before.skills_invoked,
                        "applied_bullet_ids": r_before.applied_bullet_ids,
                        "duration_ms": r_before.duration_ms,
                        "error": r_before.error,
                        "report_excerpt": r_before.report_text()[:1200],
                    },
                    "after_replay": {
                        "result_type": r_after.result_type,
                        "skills_invoked": r_after.skills_invoked,
                        "applied_bullet_ids": r_after.applied_bullet_ids,
                        "duration_ms": r_after.duration_ms,
                        "error": r_after.error,
                        "report_excerpt": r_after.report_text()[:1200],
                    },
                }
                report["cases"].append(case_row)
                self.emit("eval_case_done", {
                    "run_id": run_id, "case_key": case.key,
                    "verdict": row["verdict"],
                    "score_before": row["score_before"],
                    "score_after": row["score_after"],
                    "judge_winner": (jres or {}).get("winner"),
                    "judge_rationale": (jres or {}).get("rationale", ""),
                })

            scored = sum(counts.values()) - counts["inconclusive"]
            report["summary"] = {
                "cases": len(picked),
                **counts,
                "scored": scored,
                "mean_delta": round(sum(deltas) / len(deltas), 4) if deltas else 0.0,
            }

            # ---- gate ----
            gate: dict = {"enabled": bool(config.gate), "decision": "",
                          "rolled_back": False, "restored_from": None,
                          "pre_rollback_snapshot": None}
            if not config.gate:
                gate["decision"] = "skipped"
            elif scored < config.gate_min_cases:
                gate["decision"] = "insufficient-cases"
            elif counts["regressed"] >= counts["improved"] + config.gate_margin:
                gate["decision"] = "rollback"
            else:
                gate["decision"] = "keep"
            self.emit("eval_gate", {"run_id": run_id, "decision": gate["decision"],
                                    "summary": report["summary"]})

            if gate["decision"] == "rollback":
                pre_name = self.history.snapshot_playbooks(
                    self.playbooks_dir, run_id=run_id, source="pre-rollback",
                    meta={"eval_run_id": run_id, "reason": "gate rollback"},
                )
                gate["pre_rollback_snapshot"] = pre_name
                date, run_dir = before_ref.split("/", 1)
                restore = self.history.restore_snapshot(
                    date, run_dir, self.playbooks_dir)
                gate["rolled_back"] = not restore.get("errors")
                gate["restored_from"] = before_ref
                gate["restore_detail"] = restore
                self.emit("eval_rollback", {
                    "run_id": run_id, "restored_from": before_ref,
                    "pre_rollback_snapshot": pre_name,
                    "restored_files": restore.get("restored", []),
                    "errors": restore.get("errors", []),
                })
                if self.sync_fn:
                    try:
                        self.sync_fn(run_id)
                    except Exception as e:
                        print(f"[ace.eval] post-rollback sync failed: {e}")

            report["gate"] = gate
        except Exception as e:
            import traceback
            report["error"] = f"{type(e).__name__}: {e}"
            print(f"[ace.eval] run failed: {e}\n{traceback.format_exc()}")
            self.emit("eval_error", {"run_id": run_id, "error": report["error"]})
        finally:
            shutil.rmtree(after_ref_dir, ignore_errors=True)

        report["finished_at"] = _now_iso()
        saved = self.store.save(report)
        self.emit("eval_done", {
            "run_id": run_id,
            "report_ref": str(saved) if saved else None,
            "summary": report.get("summary"),
            "gate": report.get("gate"),
            "error": report.get("error"),
        })
        return report
