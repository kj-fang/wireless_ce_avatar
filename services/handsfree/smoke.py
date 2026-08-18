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

    # REST comment payload (verified field map for this org: the privacy
    # field is Core_IPS_Public__c, a PUBLIC flag — private means False).
    from .ips_client import IpsClient, PostUnsupported
    fm = {"body_field": "Core_IPS_Rich_Comment__c",
          "plain_field": "Core_IPS_Comment__c",
          "private_field": "Core_IPS_Public__c",
          "private_value": False}
    p = IpsClient.build_comment_payload("500XYZ", "<p>rich</p>",
                                        plain_body="plain", field_map=fm)
    check("S1.d payload: both bodies + case lookup",
          p["Core_IPS_Rich_Comment__c"] == "<p>rich</p>"
          and p["Core_IPS_Comment__c"] == "plain"
          and p["Core_IPS_Case__c"] == "500XYZ", str(p))
    check("S1.e payload: private means Public=False",
          p["Core_IPS_Public__c"] is False, str(p))
    try:
        IpsClient.build_comment_payload("500XYZ", "<p>x</p>", field_map=None)
        check("S1.f unverified field map refused", False)
    except PostUnsupported:
        check("S1.f unverified field map refused", True)

    # Public (customer-visible) payload — used by request_logs replies.
    fm_pub = dict(fm, extra_fields={
        "Core_IPS_Case_Comment_Type__c": "Private to Intel",
        "Core_IPS_Comment_Author_Type__c": "Agent"})
    p = IpsClient.build_comment_payload("500XYZ", "<p>r</p>",
                                        field_map=fm_pub, private=False)
    check("S1.g public payload: Public=True explicitly",
          p["Core_IPS_Public__c"] is True, str(p))
    check("S1.h public payload drops private-flavored extras",
          "Core_IPS_Case_Comment_Type__c" not in p
          and p.get("Core_IPS_Comment_Author_Type__c") == "Agent", str(p))
    try:
        IpsClient.build_comment_payload("500XYZ", "<p>r</p>",
                                        field_map={"body_field": "B__c"},
                                        private=False)
        check("S1.i public refused without verified privacy field", False)
    except PostUnsupported:
        check("S1.i public refused without verified privacy field", True)


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
    # The triage LLM oscillates between 'Issue summary' and 'Issue_summary'
    # (case 00993799 hit the underscore variant and rendered an empty draft).
    a = _triage_analysis()
    a.triage = {
        "Issue_summary": {"Symptom": ["beacon miss during P2P GO session"]},
        "Next_action": {"Recommendation": ["Confirm repro rate"]},
    }
    tri2 = compose(a)
    check("S2.g underscore triage keys tolerated",
          "beacon miss during P2P GO session" in tri2["plain"]
          and "Confirm repro rate" in tri2["plain"], tri2["plain"][:300])


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
    # TWO zips: the reader must choose the OLDER one (repro_logs.zip) — the
    # newest-zip fallback would pick later_capture.zip, so a correct pick
    # proves the reader's choice drives pick_zip.
    fake_zip = case_dir / "repro_logs.7z"
    fake_zip.write_bytes(b"PK\x05\x06" + b"\x00" * 18)
    decoy_zip = case_dir / "later_capture.zip"
    decoy_zip.write_bytes(b"PK\x05\x06" + b"\x00" * 18)

    # --- mock the fetch stage (with comments, chronological story) ---
    from services import case_info_service as cis
    from datetime import datetime
    orig_process = cis.CaseService.process_case

    def _fake_process(case_ctx: CaseContext) -> CaseContext:
        case_ctx.id = "500FAKESFID000AAA"
        case_ctx.subject = "Wi-Fi drops right after connecting"
        case_ctx.description = "Device sometimes disconnects. First seen last week."
        case_ctx.wifi_or_bt = "wifi"
        case_ctx.case_download_dir = str(case_dir)
        case_ctx.comments = [
            [datetime(2026, 6, 19, 9, 0), "Partner",
             "Initial report: disconnect happens randomly."],
            [datetime(2026, 6, 20, 11, 0), "Partner",
             "Reproduced today at 10:17:30. Uploaded repro_logs.7z (driver log) covering it."],
            [datetime(2026, 6, 21, 8, 0), "Partner",
             "Also uploaded later_capture.zip but device did NOT fail in that run."],
        ]
        case_ctx.attachment_list = [
            ["repro_logs.7z", "https://esft/x?FileName=repro_logs.7z",
             ["06/20/2026 10:20", "repro at 10:17:30"]],
            ["later_capture.zip", "https://esft/x?FileName=later_capture.zip",
             ["06/21/2026 08:00", "no failure in this run"]],
        ]
        return case_ctx

    # --- mock download + decompose ---
    from utils import attachment_download as adl
    from utils import attachment_decompose as adc
    orig_dload, orig_zip = adl.run_dload_threads, adc.process_single_zip

    def _fake_dload(att_list, download_path, socketio):
        # Serve whichever zip the runner actually selected.
        name = att_list[0][0]
        yield [str(case_dir / name), name, True]

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
    # Fake reader reply: chooses the OLDER repro_logs.zip (per comment #2)
    # and the issue time stated in the comments — proving comment-aware
    # selection beats the newest-zip fallback.
    reader_reply = json.dumps({
        "clean_description": "Device disconnects ~90s after association (reproduced 06/20).",
        "issue_times": ["06/20/2026-10:17:30"],
        "issue_time_source": "comment #2",
        "attachment_name": "repro_logs.7z",
        "attachment_reason": "uploaded right after the 10:17:30 repro; later_capture.zip had no failure",
        "reasoning": "Comment #2 supersedes the vague description; comment #3 rules out the newer capture.",
    })
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
        chat=lambda messages, system_content=None: reader_reply,
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
        check("S4.d issue time from comments (reader)",
              analysis.issue_times == ["06/20/2026-10:17:30"]
              and analysis.case_reader.get("issue_time_source") == "comment #2")
        stage_names = [s.name for s in analysis.stages]
        check("S4.e all stages recorded",
              {"fetch_case", "triage", "read_case_history", "check_wrt_log",
               "pick_zip", "download", "decompose", "issue_time", "pick_etl",
               "agent_analysis", "echo_kb"} <= set(stage_names),
              str(stage_names))
        check("S4.e2 no assert evidence -> Echo never queried",
              analysis.echo_insights == [], str(analysis.echo_insights))
        check("S4.g reader-chosen zip beats newest-zip fallback",
              analysis.chosen_attachment == "repro_logs.7z",
              f"chosen={analysis.chosen_attachment}")  # .7z proves non-zip archives pass the pick stage
        check("S4.h Salesforce case id captured",
              analysis.case_id == "500FAKESFID000AAA")

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

        # --- request-logs path A: no archive attached at all ----------------
        def _fake_process_no_logs(case_ctx: CaseContext) -> CaseContext:
            case_ctx = _fake_process(case_ctx)
            case_ctx.attachment_list = []
            return case_ctx
        cis.CaseService.process_case = staticmethod(_fake_process_no_logs)
        analysis2 = r.analyze_case("01234567")
        check("S4.i no archive -> request_logs mode",
              analysis2.mode == "request_logs" and analysis2.ok,
              f"mode={analysis2.mode} err={analysis2.error}")
        stage_names2 = [s.name for s in analysis2.stages]
        check("S4.j check_wrt_log recorded, stopped before download",
              "check_wrt_log" in stage_names2 and "download" not in stage_names2,
              str(stage_names2))
        draft2 = compose(analysis2)
        check("S4.k request-logs draft asks for WRT logs",
              "WRT logs" in draft2["plain"] and draft2["confidence"] is None,
              draft2["plain"][:200])

        # --- request-logs path B: archive unzips to no WRT/DDD ETLs ---------
        cis.CaseService.process_case = staticmethod(_fake_process)
        adc.process_single_zip = lambda *a, **k: ([], [], [], [], [])
        analysis3 = r.analyze_case("01234567")
        stage_names3 = [s.name for s in analysis3.stages]
        check("S4.l empty archive -> request_logs after decompose",
              analysis3.mode == "request_logs"
              and "decompose" in stage_names3
              and "check_wrt_log" in stage_names3,
              f"mode={analysis3.mode} err={analysis3.error} stages={stage_names3}")
        draft3 = compose(analysis3)
        check("S4.m reply names the checked archive",
              "repro_logs.7z" in draft3["plain"] and "WRT" in draft3["plain"],
              draft3["plain"][:250])

        # --- decompose CRASH must NOT ask the customer for logs -------------
        # (_stage swallows the exception; empty lists must only mean
        # "no logs" when decompose actually succeeded)
        def _boom(*a, **k):
            raise RuntimeError("corrupt archive")
        adc.process_single_zip = _boom
        analysis4 = r.analyze_case("01234567")
        check("S4.n decompose crash -> triage_only, not request_logs",
              analysis4.mode == "triage_only"
              and analysis4.error == "attachment decompose failed",
              f"mode={analysis4.mode} err={analysis4.error}")
    finally:
        cis.CaseService.process_case = orig_process
        adl.run_dload_threads = orig_dload
        adc.process_single_zip = orig_zip
        ita.organize_issue_context = orig_org
        app_config.llm_helper = orig_llm
        app_config.log_chatbot_agent = orig_agent
        if not orig_files_dir:
            app_config.avatarfiles_dir = orig_files_dir


# ---------------------------------------------------------------- S5
def smoke_orchestrator(tmp: Path) -> None:
    print("[S5] Approve/post guards (scan failure aborts; marker dedups)")
    from . import orchestrator as orch
    from .composer import AI_MARKER
    from .queue import HandsfreeStore

    store = HandsfreeStore(tmp / "handsfree_s5")
    rec = store.enqueue(case_nbr="09999999", case_id="500S5FAKE", subject="s5",
                        draft_plain=AI_MARKER + "\n\nbody", draft_html="<p>b</p>",
                        confidence=None, mode="full", analysis={})
    draft_id = rec["draft_id"]

    class _ScanBoom:
        FIELD_RICH_BODY = "Core_IPS_Rich_Comment__c"
        def get_case_comments(self, case_id):
            raise RuntimeError("IPS unreachable")

    class _HasMarker:
        FIELD_RICH_BODY = "Core_IPS_Rich_Comment__c"
        def get_case_comments(self, case_id):
            return [{"Id": "C1",
                     "Core_IPS_Rich_Comment__c": AI_MARKER + " posted earlier"}]

    orig_store_fn, orig_ips = orch._store, orch.IpsClient
    try:
        orch._store = lambda: store
        orch.IpsClient = _ScanBoom
        res = orch.approve_and_post(draft_id)
        check("S5.a scan failure aborts the post",
              res["ok"] is False and "scan failed" in res["error"], str(res))
        check("S5.b draft still pending_review after abort (retryable)",
              store.get(draft_id)["status"] == "pending_review")

        orch.IpsClient = _HasMarker
        res2 = orch.approve_and_post(draft_id)
        check("S5.c existing AI comment -> refuse + mark posted",
              res2["ok"] is False and store.get(draft_id)["status"] == "posted",
              str(res2))
        check("S5.d second approve of posted draft refused",
              orch.approve_and_post(draft_id)["error"] == "draft already posted")
    finally:
        orch._store = orig_store_fn
        orch.IpsClient = orig_ips


# ---------------------------------------------------------------- S6
def smoke_ui_commenter() -> None:
    print("[S6] UI commenter privacy-state detection")
    from .ui_commenter import _control_is_checked

    class _El:
        def __init__(self, tag="span", selected=False, attrs=None):
            self.tag_name = tag
            self._selected = selected
            self._attrs = attrs or {}
        def is_selected(self):
            return self._selected
        def get_attribute(self, name):
            return self._attrs.get(name)

    check("S6.a native checkbox: is_selected wins",
          _control_is_checked(_El("input", selected=True))
          and not _control_is_checked(_El("input", selected=False)))
    check("S6.b aria-checked respected",
          _control_is_checked(_El(attrs={"aria-checked": "true"}))
          and not _control_is_checked(_El(attrs={"aria-checked": "false"})))
    check("S6.c class-marked label detected",
          _control_is_checked(_El(attrs={"class": "slds-checkbox is-selected"}))
          and not _control_is_checked(_El(attrs={"class": "slds-checkbox"})))


# ---------------------------------------------------------------- S7
def smoke_echo_kb() -> None:
    print("[S7] Echo KB client (detection + insights, no network)")
    from .runner import CaseAnalysis, IncidentReport
    from .composer import compose_plain
    from .echo_client import (EchoUnavailable, find_assert_evidence,
                              collect_echo_insights)

    a = CaseAnalysis(case_nbr="01234567", mode="full", ok=True,
                     clean_description="WiFi dies during roam")
    a.incidents.append(IncidentReport(
        report_text="Firmware hit ASSERT with code 0x02001234 during scan.",
        steps=[{"role": "tool",
                "content": "=== Assert Code Lookup: 0x02001234 ===\nName: FOO"},
               {"role": "assistant", "content": "assert 0X02001234 again"}]))
    ev = find_assert_evidence(a)
    check("S7.a agent-text fallback: code detected + deduped case-insensitively",
          ev["assert_codes"] == ["0x02001234"] and ev["source"] == "agent_text"
          and not ev["yellow_bang"], str(ev))

    # Windows event-log ID (5002 = adapter reset) is NOT a firmware assert.
    e = CaseAnalysis(case_nbr="01234570", mode="full", ok=True)
    e.incidents.append(IncidentReport(
        report_text="Event log shows assert event 0x5002 (adapter reset) twice."))
    check("S7.a2 Windows event ID 5002 rejected as assert code",
          find_assert_evidence(e)["assert_codes"] == [],
          str(find_assert_evidence(e)))

    # WRT-log scan is authoritative and beats agent-text mentions.
    import tempfile as _tf
    wrt = Path(_tf.mkdtemp(prefix="hf_s7_")) / "x.etl.log"
    wrt.write_text(
        "81242 07/09/2026-00:26:19.480 [1] [NIC_DEBUG] [INFO] scan start\n"
        "81243 07/09/2026-00:26:19.481 [1] [NIC_DEBUG] [ERROR] "
        "[prvNicDbgHandleUmacErrLog]:FATAL_ERROR: uCode ASSERT(UMAC, "
        "rtStatus = 0x2000008A, log is  valid. data1 = 0x158f8cca, data2 = 0xfe10fe1)\n"
        "81244 07/09/2026-00:26:19.482 [1] [NIC_DEBUG] [ERROR] "
        "FATAL_ERROR: uCode ASSERT(UMAC, rtStatus = 0x2000008A, repeated)\n",
        encoding="utf-8")
    w = CaseAnalysis(case_nbr="01234571", mode="full", ok=True, log_path=str(wrt))
    w.incidents.append(IncidentReport(report_text="assert 0x5002 in event log"))
    evw = find_assert_evidence(w)
    check("S7.a3 WRT log assert wins: rtStatus code + CPU + data fields, deduped",
          evw["source"] == "wrt_log" and evw["assert_codes"] == ["0x2000008A"]
          and evw["asserts"][0]["cpu"] == "UMAC"
          and evw["asserts"][0]["data"] == {"data1": "0x158f8cca", "data2": "0xfe10fe1"},
          str(evw))
    q_asked: list[str] = []
    collect_echo_insights(w, ask=lambda q: (q_asked.append(q) or "ok"))
    check("S7.a4 question carries rtStatus, CPU, data fields and the log line",
          "0x2000008A" in q_asked[0] and "CPU: UMAC" in q_asked[0]
          and "data1 = 0x158f8cca" in q_asked[0] and "uCode ASSERT" in q_asked[0],
          q_asked[0][:400])

    asked: list[str] = []
    def _fake_ask(q):
        asked.append(q)
        return "Echo says: known race in scan abort path."
    insights = collect_echo_insights(a, ask=_fake_ask)
    check("S7.b insight collected with lookup-grounded question",
          len(insights) == 1 and insights[0]["kind"] == "assert"
          and insights[0]["answer"].startswith("Echo says")
          and "Assert Code Lookup" in asked[0] and "0x02001234" in asked[0],
          str(insights)[:300])

    def _down(q):
        raise EchoUnavailable("backend down")
    insights2 = collect_echo_insights(a, ask=_down)
    check("S7.c Echo outage recorded per-insight, never raises",
          insights2[0]["answer"] is None and "down" in insights2[0]["error"])

    yb = CaseAnalysis(case_nbr="01234568", mode="triage_only", ok=True,
                      issue_type="Yellow Bang (YB)")
    ev_yb = find_assert_evidence(yb)
    ins_yb = collect_echo_insights(yb, ask=_fake_ask)
    check("S7.d yellow bang without assert -> one yellow_bang insight",
          ev_yb["yellow_bang"] and not ev_yb["assert_codes"]
          and len(ins_yb) == 1 and ins_yb[0]["kind"] == "yellow_bang")

    a.echo_insights = [
        {"kind": "assert", "code": "0x02001234",
         "answer": "Known race in scan abort path.", "error": None},
        {"kind": "assert", "code": "0xDEAD", "answer": None,
         "error": "backend down"},
    ]
    a.incidents[0].report = {"root_cause_summary": "assert during scan",
                             "confidence_score": 80}
    plain = compose_plain(a)
    check("S7.e answered insights composed into draft, failures omitted",
          "Knowledge base insights (Echo):" in plain
          and "Known race in scan abort path." in plain
          and "backend down" not in plain,
          plain[:400])

    # Transport hardening: Echo is intranet -> must bypass proxy env
    # (proxy-dmz answers 403 for internal hosts) and verify TLS against the
    # OS store (Intel corp CA is not in certifi).
    from .echo_client import _make_httpx_client_factory
    import os as _os
    prev = {k: _os.environ.get(k) for k in ("HTTPS_PROXY", "HTTP_PROXY", "NO_PROXY")}
    try:
        _os.environ["HTTPS_PROXY"] = "http://proxy-dmz.intel.com:912"
        _os.environ["NO_PROXY"] = "only.snowflake.host"
        client = _make_httpx_client_factory()()
        mounts_have_proxy = any(
            getattr(getattr(t, "_pool", None), "_proxy_url", None) is not None
            for t in getattr(client, "_mounts", {}).values())
        check("S7.f MCP httpx client ignores proxy env (trust_env=False)",
              client.trust_env is False and not mounts_have_proxy)
        ctx = getattr(getattr(client._transport, "_pool", None), "_ssl_context", None)
        check("S7.g MCP httpx client verifies TLS via OS trust store",
              ctx is not None and "truststore" in type(ctx).__module__,
              str(type(ctx)))
    finally:
        for k, v in prev.items():
            if v is None:
                _os.environ.pop(k, None)
            else:
                _os.environ[k] = v


def run_smoke() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="handsfree_smoke_"))
    try:
        smoke_soql()
        smoke_composer()
        smoke_queue(tmp)
        smoke_runner(tmp)
        smoke_orchestrator(tmp)
        smoke_ui_commenter()
        smoke_echo_kb()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print(f"\nsmoke result: {'ALL PASS' if not PASS_FAIL else 'FAILURES: ' + ', '.join(PASS_FAIL)}")
    return 0 if not PASS_FAIL else 1


if __name__ == "__main__":
    sys.exit(run_smoke())
