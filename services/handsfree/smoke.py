"""
Zero-token smoke test for the Handsfree Replyer.

Covers, with NO network / NO LLM tokens / NO IPS access:
  S1  SOQL builder (escaping, TODAY vs since)
  S2  Composer (full report mode + triage-only mode, AI marker)
  S3  Queue store (enqueue → transitions, ledger, traversal guard)
  S4  Runner end-to-end with every external stage mocked and the agent
      driven by the eval FakeAgentClient → draft reaches pending_review

Run:  python -m services.handsfree.smoke
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
from pathlib import Path

PASS_FAIL: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + ("" if cond else f"  ({detail})"))
    if not cond:
        PASS_FAIL.append(name)


# ---------------------------------------------------------------- S1
def smoke_soql() -> None:
    print("[S1] SOQL builder")
    from .ips_client import build_new_cases_soql
    q = build_new_cases_soql("Charles P Chu")
    check("S1.a owner + TODAY",
          "Owner.Name = 'Charles P Chu'" in q and "CreatedDate = TODAY" in q, q)
    q2 = build_new_cases_soql("O'Brien \\ Team", since_iso="2026-07-15T00:00:00Z")
    check("S1.b escaping + since",
          "O\\'Brien \\\\ Team" in q2 and "CreatedDate >= 2026-07-15T00:00:00Z" in q2, q2)
    try:
        build_new_cases_soql("x", since_iso="not-a-date")
        check("S1.c bad since rejected", False)
    except ValueError:
        check("S1.c bad since rejected", True)


# ---------------------------------------------------------------- S2
def _full_analysis():
    from .runner import CaseAnalysis, IncidentReport
    a = CaseAnalysis(case_nbr="01234567", mode="full", ok=True,
                     subject="Wi-Fi drops after resume", issue_type="Connectivity")
    a.incidents.append(IncidentReport(
        issue_time="06/20/2026-10:17:30", result_type="report",
        report={"root_cause_summary": "AP-initiated disconnect (deauth reason 7).",
                "confidence_score": 88,
                "recommended_actions": ["Check AP logs", "Verify AP firmware"],
                "involved_skills": ["Connectivity"],
                "markdown_summary": "# Executive Summary\nAP kicked the client."}))
    return a


def _triage_analysis():
    from .runner import CaseAnalysis
    a = CaseAnalysis(case_nbr="07654321", mode="triage_only", ok=True,
                     subject="YB after stress", issue_type="Yellow Bang (YB)",
                     error="no ZIP attachment")
    a.triage = {
        "Issue summary": {"Symptom": ["Device lost after WB cycles"]},
        "Next action": {"Recommendation": [
            "Please attach the driver log capture",
            "Confirm failure rate across units"]},
    }
    return a


def smoke_composer() -> None:
    print("[S2] Composer")
    from .composer import compose, AI_MARKER
    full = compose(_full_analysis())
    check("S2.a marker present", AI_MARKER in full["plain"])
    check("S2.b root cause + confidence",
          "AP-initiated disconnect" in full["plain"] and "88/100" in full["plain"])
    check("S2.c confidence extracted", full["confidence"] == 88)
    check("S2.d html escaped + br",
          "<br/>" in full["html"] and "<script" not in full["html"])
    tri = compose(_triage_analysis())
    check("S2.e triage questions present",
          "Please attach the driver log capture" in tri["plain"], tri["plain"][:300])
    check("S2.f triage has no confidence", tri["confidence"] is None)


# ---------------------------------------------------------------- S3
def smoke_queue(tmp: Path) -> None:
    print("[S3] Queue store")
    from .queue import HandsfreeStore
    store = HandsfreeStore(tmp / "handsfree")
    rec = store.enqueue(case_nbr="01234567", case_id="500XYZ", subject="s",
                        draft_plain="body", draft_html="<p>body</p>",
                        confidence=88, mode="full", analysis={"k": 1})
    check("S3.a enqueued pending_review", rec["status"] == "pending_review")
    check("S3.b ledger marks analyzed", store.is_processed("01234567"))
    upd = store.update(rec["draft_id"], status="posted",
                       post_result={"ok": True, "backend": "rest"})
    check("S3.c status transition", upd["status"] == "posted")
    try:
        store.update(rec["draft_id"], status="bogus")
        check("S3.d invalid status rejected", False)
    except ValueError:
        check("S3.d invalid status rejected", True)
    check("S3.e traversal guarded", store.get("..\\..\\etc") is None)
    items = store.list_drafts(include_closed=False)
    check("S3.f closed filtered out",
          all(i["status"] not in ("posted", "rejected") for i in items))
    cfg = store.save_config({"owner_name": "Charles P Chu"})
    check("S3.g config round-trip",
          store.load_config()["owner_name"] == "Charles P Chu"
          and cfg["max_cases_per_run"] == 3)


# ---------------------------------------------------------------- S4
def smoke_runner(tmp: Path) -> None:
    print("[S4] Runner end-to-end (all externals mocked, fake agent)")
    import types
    from configs.global_configs import app_config
    from models.models import CaseContext
    from services.log_chatbot_service import Skill
    from services.ace.eval.fakes import FakeAgentClient
    from . import runner as runner_mod
    from .composer import compose
    from .queue import HandsfreeStore

    case_dir = tmp / "01234567"
    case_dir.mkdir(parents=True, exist_ok=True)

    # Fixture: fake etl + pre-decoded .log (decode stage becomes a no-op skip).
    etl = case_dir / "capture_20-06-2026_10-20-00" / "WifiDriverIHVSession.etl.001"
    etl.parent.mkdir(parents=True, exist_ok=True)
    etl.write_bytes(b"\x00fake")
    log_lines = [
        "06/20/2026-10:16:01.000 [I] Connected to BSSID aa:bb:cc:dd:ee:ff",
        "06/20/2026-10:17:30.500 [E] DEAUTH from BSSID aa:bb:cc:dd:ee:ff reason_code=7",
        "06/20/2026-10:17:30.600 [I] Connection terminated by AP",
    ]
    Path(str(etl) + ".log").write_text("\n".join(log_lines), encoding="utf-8")
    fake_zip = case_dir / "logs.zip"
    fake_zip.write_bytes(b"PK\x05\x06" + b"\x00" * 18)

    # --- mock the fetch stage ---
    from services import case_info_service as cis
    orig_process = cis.CaseService.process_case

    def _fake_process(case_ctx: CaseContext) -> CaseContext:
        case_ctx.subject = "Wi-Fi drops right after connecting"
        case_ctx.description = "Device disconnects ~90s after association at 10:17:30."
        case_ctx.wifi_or_bt = "wifi"
        case_ctx.case_download_dir = str(case_dir)
        case_ctx.attachment_list = [["logs.zip", "https://esft/x?FileName=logs.zip",
                                     ["06/20/2026 10:20", "issue at 10:17:30"]]]
        return case_ctx

    # --- mock download + decompose ---
    from utils import attachment_download as adl
    from utils import attachment_decompose as adc
    orig_dload, orig_zip = adl.run_dload_threads, adc.process_single_zip

    def _fake_dload(att_list, download_path, socketio):
        yield [str(fake_zip), "logs.zip", True]

    def _fake_zip_proc(zip_path, download_path_tmp, already, progress_cb=None,
                       cancel_event=None):
        return [str(etl)], [], [], [], []

    # --- mock issue-time organization (no LLM) ---
    from utils import issue_time_ai as ita
    orig_org = ita.organize_issue_context

    def _fake_org(description, first_ts=None, last_ts=None,
                  llm_client=None, llm_model=None):
        return {"clean_description": "Device disconnects ~90s after association",
                "issue_times": ["06/20/2026-10:17:30"],
                "interpretation": "single incident"}

    # --- fake LLM helper on app_config ---
    report = {
        "root_cause_summary": "AP-initiated disconnect: deauth reason_code=7.",
        "confidence_score": 90,
        "recommended_actions": ["Check AP-side logs"],
        "involved_skills": ["Connectivity"],
        "markdown_summary": "# Executive Summary\nAP kicked the client.",
        "applied_bullet_ids": [], "flagged_bullet_ids": [],
    }
    fake_llm = types.SimpleNamespace(
        client=FakeAgentClient(report, report),   # same report either way
        model="fake-model",
        skills={"Connectivity": Skill(name="Connectivity",
                                      description="Wi-Fi connect/disconnect analysis",
                                      keywords=["DEAUTH"], tat_path=None,
                                      expert_rules="Check deauth before blaming driver.")},
        analyze_desc=lambda prompt_path, ctx: {
            "Issue summary": {"Symptom": ["disconnects after association"]},
            "Next action": {"Recommendation": ["n/a"]},
            "Classification": {"issue_type": "Connectivity", "confidence": 0.9},
        },
    )
    orig_llm = getattr(app_config, "llm_helper", None)
    orig_agent = getattr(app_config, "log_chatbot_agent", None)
    orig_files_dir = getattr(app_config, "avatarfiles_dir", None)

    try:
        cis.CaseService.process_case = staticmethod(_fake_process)
        adl.run_dload_threads = _fake_dload
        adc.process_single_zip = _fake_zip_proc
        ita.organize_issue_context = _fake_org
        app_config.llm_helper = fake_llm
        app_config.log_chatbot_agent = None
        if not orig_files_dir:
            app_config.avatarfiles_dir = str(tmp)

        r = runner_mod.HandsfreeRunner(
            progress_cb=lambda s, d: print(f"    · {s}: {d}"))
        analysis = r.analyze_case("01234567")

        check("S4.a mode is full", analysis.mode == "full",
              f"mode={analysis.mode} err={analysis.error} stages={[ (s.name, s.ok, s.detail) for s in analysis.stages ]}")
        check("S4.b agent produced a report",
              analysis.best_incident is not None and
              analysis.best_incident.confidence == 90)
        check("S4.c log path resolved", analysis.log_path.endswith(".etl.001.log"))
        check("S4.d issue time picked up",
              analysis.issue_times == ["06/20/2026-10:17:30"])
        stage_names = [s.name for s in analysis.stages]
        check("S4.e all stages recorded",
              {"fetch_case", "triage", "pick_zip", "download", "decompose",
               "issue_time", "pick_etl", "agent_analysis"} <= set(stage_names),
              str(stage_names))

        # queue integration: compose + enqueue like the orchestrator does
        store = HandsfreeStore(tmp / "handsfree_s4")
        draft = compose(analysis)
        rec = store.enqueue(case_nbr=analysis.case_nbr, case_id="500FAKE",
                            subject=analysis.subject,
                            draft_plain=draft["plain"], draft_html=draft["html"],
                            confidence=draft["confidence"], mode=analysis.mode,
                            analysis=analysis.to_dict())
        check("S4.f draft queued pending_review",
              rec["status"] == "pending_review" and "AP-initiated" in rec["draft_plain"])
    finally:
        cis.CaseService.process_case = orig_process
        adl.run_dload_threads = orig_dload
        adc.process_single_zip = orig_zip
        ita.organize_issue_context = orig_org
        app_config.llm_helper = orig_llm
        app_config.log_chatbot_agent = orig_agent
        if not orig_files_dir:
            app_config.avatarfiles_dir = orig_files_dir


def run_smoke() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="handsfree_smoke_"))
    try:
        smoke_soql()
        smoke_composer()
        smoke_queue(tmp)
        smoke_runner(tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print(f"\nsmoke result: {'ALL PASS' if not PASS_FAIL else 'FAILURES: ' + ', '.join(PASS_FAIL)}")
    return 0 if not PASS_FAIL else 1


if __name__ == "__main__":
    sys.exit(run_smoke())
