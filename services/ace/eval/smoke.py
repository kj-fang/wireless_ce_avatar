"""
Zero-token end-to-end smoke test for the eval harness.

Builds a fully synthetic fixture in %TEMP% (feedback snapshots, attached
logs, playbooks, one history snapshot), drives the REAL replay / scoring /
gate / rollback code with scripted fakes, and asserts:

  Scenario A (keep):     the AFTER playbook leads the fake agent to the
                         correct conclusion → improved cases → gate "keep".
  Scenario B (rollback): the AFTER playbook leads it astray → regressed →
                         gate "rollback", live playbooks byte-identical to
                         the before snapshot, pre-rollback snapshot exists,
                         cursor untouched.

No LLM tokens are spent. Production playbooks/feedback are never touched —
everything lives in the temp fixture.

Run via:  python -m services.ace.cli eval --smoke
"""

from __future__ import annotations

import json
import shutil
import tempfile
import uuid
from pathlib import Path

from ..history import HistoryWriter
from .fakes import FakeAgentClient, FakeAgentLLM, FakeJudgeLLM
from .golden import GoldenSet
from .harness import EvalHarness, EvalConfig
from .store import EvalStore

_MARKER = "SMOKE_AFTER_MARKER"

_FAKE_LOG_LINES = [
    "06/20/2026-10:15:{:02d}.123 [I] WLAN scan complete, 12 APs found".format(i)
    for i in range(10)
] + [
    "06/20/2026-10:16:01.000 [I] Connected to BSSID aa:bb:cc:dd:ee:ff",
    "06/20/2026-10:17:30.500 [E] DEAUTH from BSSID aa:bb:cc:dd:ee:ff reason_code=7",
    "06/20/2026-10:17:30.600 [I] Connection terminated by AP",
    "06/20/2026-10:17:35.000 [I] Reconnect attempt started",
] + [
    "06/20/2026-10:18:{:02d}.000 [I] background maintenance tick".format(i)
    for i in range(10)
]


def _write_fixture(root: Path) -> dict:
    """Create feedback root + logs + two conversations. Returns paths dict."""
    fb = root / "feedback"
    conv_dir = fb / "conversations"
    conv_dir.mkdir(parents=True)

    log_text = "\n".join(_FAKE_LOG_LINES) + "\n"

    # Conversation 1 — log via the shared attached-logs folder.
    logs1 = fb / "logs" / "convS1"
    logs1.mkdir(parents=True)
    (logs1 / "turnS1__tester__session.log").write_text(log_text, encoding="utf-8")

    # Conversation 2 — log via a direct (unscrubbed) local path.
    loose_log = root / "loose_session.log"
    loose_log.write_text(log_text, encoding="utf-8")

    def _snapshot(cid, tid, log_path=""):
        return {
            "schema_version": 4,
            "conversation_id": cid,
            "domain": "wifi",
            "submitted_by": "tester",
            "started_at": "2026-06-20T10:00:00",
            "ended_at": "2026-06-20T10:30:00",
            "log_path": log_path,
            "issue": {
                "case_nbr": f"CASE-{cid}",
                "subject": "Wi-Fi drops right after connecting",
                "description": "Device disconnects ~90s after association.",
                "issue_type": "Connectivity",
                "attachment_time": "",
            },
            "turns": [{
                "turn_id": tid,
                "ts": "2026-06-20T10:20:00",
                "user_message": "Why did the device disconnect?",
                "mode": "tools",
                "skills_used": [{"skill_id": "Connectivity", "step_index": 1}],
                "agent_response_full": {
                    "root_cause_summary": "Recorded human-voted diagnosis: AP kicked the client.",
                },
                "steps_trace": [],
                "feedback": {
                    "vote": -1,
                    "weight": "high",
                    "details": {
                        "correct_root_cause": "AP-initiated disconnect (deauth reason 7)",
                        "correct_conclusion_tag": "AP_KICK",
                        "correct_skill": "Connectivity",
                        "evidence_log_lines": [
                            "[E] DEAUTH from BSSID aa:bb:cc:dd:ee:ff reason_code=7",
                            "[I] Connection terminated by AP",
                        ],
                    },
                },
            }],
        }

    (conv_dir / "convS1.json").write_text(
        json.dumps(_snapshot("convS1", "turnS1"), indent=2), encoding="utf-8")
    (conv_dir / "convS2.json").write_text(
        json.dumps(_snapshot("convS2", "turnS2", log_path=str(loose_log)),
                   indent=2), encoding="utf-8")
    return {"feedback_root": fb, "loose_log": loose_log}


def _write_playbook(path: Path, scope: str, bullets: list[tuple[str, str]]) -> None:
    payload = {
        "scope": scope,
        "next_seq": len(bullets) + 1,
        "updated_at": "2026-06-20T10:00:00",
        "bullets": [
            {
                "id": f"agent-{i + 1:05d}",
                "section": section,
                "content": content,
                "helpful_count": 1, "harmful_count": 0, "neutral_count": 0,
                "created_at": "2026-06-20T10:00:00",
                "updated_at": "2026-06-20T10:00:00",
                "source_turn_ids": [],
            }
            for i, (section, content) in enumerate(bullets)
        ],
    }
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def _make_skills() -> dict:
    from services.chatbot.engine.system import Skill
    return {
        "Connectivity": Skill(
            name="Connectivity",
            description="Wi-Fi connect/disconnect analysis",
            keywords=["DEAUTH", "Connection terminated"],
            tat_path=None,
            expert_rules="Check for AP-initiated deauth before blaming the driver.",
        ),
    }


def _reports() -> tuple[dict, dict]:
    """(correct_report, wrong_report) — correctness relative to ground truth
    AP_KICK. Judge markers [BEFORE]/[AFTER] are stamped by the scenario."""
    correct = {
        "root_cause_summary": "AP-initiated disconnect: deauth from AP with reason_code=7.",
        "confidence_score": 90,
        "recommended_actions": ["Check AP-side logs", "Verify AP firmware"],
        "involved_skills": ["Connectivity"],
        "markdown_summary": (
            "# Executive Summary\nThe AP kicked the client (deauth from ap).\n"
            "## Evidence\n"
            "[E] DEAUTH from BSSID aa:bb:cc:dd:ee:ff reason_code=7\n"
            "[I] Connection terminated by AP\n"
        ),
        "applied_bullet_ids": [],
        "flagged_bullet_ids": [],
    }
    wrong = {
        "root_cause_summary": "Firmware crash caused the disconnect (fw assert).",
        "confidence_score": 85,
        "recommended_actions": ["Collect fw dump"],
        "involved_skills": ["BSOD"],
        "markdown_summary": "# Executive Summary\nA firmware crash (fw assert) dropped the link.\n",
        "applied_bullet_ids": [],
        "flagged_bullet_ids": [],
    }
    return correct, wrong


def _run_scenario(*, root: Path, after_is_correct: bool, verbose: bool,
                  max_cases: int) -> dict:
    fixture = _write_fixture(root)
    fb_root = fixture["feedback_root"]

    pbs_dir = root / "ace_playbooks"
    pbs_dir.mkdir()
    history = HistoryWriter(pbs_dir / "history")
    store = EvalStore(pbs_dir / "history" / "evals")

    # BEFORE state: one baseline workflow bullet, then snapshot it.
    _write_playbook(pbs_dir / "workflow.json", "agent", [
        ("skill_selection_rules", "Start with the Connectivity skill for disconnect symptoms"),
    ])
    cursor_path = pbs_dir / ".ace_cursor.json"
    cursor_path.write_text('{"last_ts": "2026-06-20T00:00:00"}', encoding="utf-8")
    before_run_dir = history.snapshot_playbooks(
        pbs_dir, run_id="smokebefore", source="pre-adapt", meta={})
    before_bytes = (pbs_dir / "workflow.json").read_bytes()
    cursor_bytes = cursor_path.read_bytes()

    # AFTER (live) state: add the marker bullet — this is what an adapt run
    # would have produced, and what the fake agent keys its behaviour on.
    _write_playbook(pbs_dir / "workflow.json", "agent", [
        ("skill_selection_rules", "Start with the Connectivity skill for disconnect symptoms"),
        ("evidence_thresholds", f"{_MARKER}: require a deauth log line before concluding AP kick"),
    ])

    correct, wrong = _reports()
    if after_is_correct:
        report_marked = {**correct, "root_cause_summary": "[AFTER] " + correct["root_cause_summary"]}
        report_plain = {**wrong, "root_cause_summary": "[BEFORE] " + wrong["root_cause_summary"]}
        judge = FakeJudgeLLM(winner="after")
    else:
        report_marked = {**wrong, "root_cause_summary": "[AFTER] " + wrong["root_cause_summary"]}
        report_plain = {**correct, "root_cause_summary": "[BEFORE] " + correct["root_cause_summary"]}
        judge = FakeJudgeLLM(winner="before")

    agent_client = FakeAgentClient(report_plain, report_marked, marker=_MARKER)
    agent_llm = FakeAgentLLM(agent_client)

    def _llm_factory(model):
        return judge if model == "fake-judge" else agent_llm

    events: list[dict] = []

    def _emit(ev, payload):
        events.append({"event": ev, **payload})
        if verbose:
            print(json.dumps({"event": ev, **payload}, default=str)[:400])

    # Fingerprint the shared feedback tree so we can assert the replay wrote
    # nothing into it (attached-log side-car writes are the known hazard).
    fb_files_before = {str(p) for p in fb_root.rglob("*") if p.is_file()}

    harness = EvalHarness(
        playbooks_dir=pbs_dir,
        feedback_roots=[fb_root],
        history=history,
        store=store,
        llm_factory=_llm_factory,
        skills_loader=_make_skills,
        golden=GoldenSet(fb_root),
        emit=_emit,
        sync_fn=None,
    )
    config = EvalConfig(
        max_cases=max_cases,
        gate=True,
        agent_model="fake-agent",
        judge_model="fake-judge",
        source="smoke",
        run_id=f"smoke{uuid.uuid4().hex[:6]}",
    )
    report = harness.run(config)

    fb_files_after = {str(p) for p in fb_root.rglob("*") if p.is_file()}

    return {
        "report": report,
        "events": events,
        "before_run_dir": before_run_dir,
        "before_bytes": before_bytes,
        "cursor_bytes": cursor_bytes,
        "pbs_dir": pbs_dir,
        "cursor_path": cursor_path,
        "history": history,
        "fb_new_files": sorted(fb_files_after - fb_files_before),
    }


def run_smoke(cases: int = 2, verbose: bool = False) -> int:
    failures: list[str] = []

    def check(name: str, cond: bool, detail: str = ""):
        status = "PASS" if cond else "FAIL"
        print(f"  {status}  {name}" + (f"  ({detail})" if detail and not cond else ""))
        if not cond:
            failures.append(name)

    # ---------------- Scenario A: after is correct → keep ----------------
    print("[smoke A] after-playbook improves the agent → expect gate 'keep'")
    root_a = Path(tempfile.mkdtemp(prefix="ace_smoke_a_"))
    try:
        out = _run_scenario(root=root_a, after_is_correct=True,
                            verbose=verbose, max_cases=cases)
        rep = out["report"]
        summary = rep.get("summary") or {}
        gate = rep.get("gate") or {}
        check("A: no run error", not rep.get("error"), str(rep.get("error")))
        check("A: replayed expected case count",
              summary.get("cases") == min(cases, 2), str(summary))
        check("A: all cases improved",
              summary.get("improved") == summary.get("cases"), str(summary))
        check("A: gate decision is keep", gate.get("decision") == "keep", str(gate))
        check("A: report persisted",
              any(True for _ in (out["pbs_dir"] / "history" / "evals").rglob("*.json")))
        check("A: live playbooks NOT rolled back",
              (out["pbs_dir"] / "workflow.json").read_bytes() != out["before_bytes"])
        ev_names = {e["event"] for e in out["events"]}
        check("A: emitted lifecycle events",
              {"eval_started", "eval_case_done", "eval_gate", "eval_done"} <= ev_names,
              str(ev_names))
        check("A: nothing written into the shared feedback tree",
              out["fb_new_files"] == [], str(out["fb_new_files"]))
    finally:
        shutil.rmtree(root_a, ignore_errors=True)

    # ---------------- Scenario B: after is wrong → rollback ----------------
    print("[smoke B] after-playbook regresses the agent → expect rollback")
    root_b = Path(tempfile.mkdtemp(prefix="ace_smoke_b_"))
    try:
        out = _run_scenario(root=root_b, after_is_correct=False,
                            verbose=verbose, max_cases=cases)
        rep = out["report"]
        summary = rep.get("summary") or {}
        gate = rep.get("gate") or {}
        check("B: no run error", not rep.get("error"), str(rep.get("error")))
        check("B: all cases regressed",
              summary.get("regressed") == summary.get("cases"), str(summary))
        check("B: gate decision is rollback",
              gate.get("decision") == "rollback", str(gate))
        check("B: rollback executed", gate.get("rolled_back") is True, str(gate))
        check("B: live workflow.json byte-identical to before snapshot",
              (out["pbs_dir"] / "workflow.json").read_bytes() == out["before_bytes"])
        check("B: cursor untouched",
              out["cursor_path"].read_bytes() == out["cursor_bytes"])
        snaps = out["history"].list_snapshots()
        check("B: pre-rollback snapshot recorded",
              any(s.get("source") == "pre-rollback" for s in snaps),
              str([s.get("source") for s in snaps]))
        check("B: rollback event emitted",
              any(e["event"] == "eval_rollback" for e in out["events"]))
    finally:
        shutil.rmtree(root_b, ignore_errors=True)

    print(f"\nsmoke result: {'ALL PASS' if not failures else 'FAILURES: ' + ', '.join(failures)}")
    return 0 if not failures else 1
