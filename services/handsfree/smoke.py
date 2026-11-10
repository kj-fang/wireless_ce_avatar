"""
Zero-token smoke test for Beacon (the handsfree replyer).

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
    yesterday = build_new_cases_soql("Charles P Chu", created_date="YESTERDAY")
    check("S1.a2 YESTERDAY test date selectable",
          "CreatedDate = YESTERDAY" in yesterday, yesterday)
    try:
        build_new_cases_soql("x", created_date="ALL TIME")
        check("S1.a3 unsupported date literal rejected", False)
    except ValueError:
        check("S1.a3 unsupported date literal rejected", True)
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
    from .ips_client import IpsClient, PostUnsupported, build_customer_updates_soql
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
    # The comment type marks private posts as private, so a public post needs
    # its own value: with the type left empty IPS accepts the insert but
    # shows the comment nowhere (seen live on case 01032011).
    try:
        IpsClient.build_comment_payload("500XYZ", "<p>r</p>",
                                        field_map=fm_pub, private=False)
        check("S1.g2 public refused without a public comment type", False)
    except PostUnsupported as e:
        check("S1.g2 public refused without a public comment type",
              "Core_IPS_Case_Comment_Type__c" in str(e), str(e))
    fm_pub["public_extra_fields"] = {
        "Core_IPS_Case_Comment_Type__c": "Public to Customers"}
    p = IpsClient.build_comment_payload("500XYZ", "<p>r</p>",
                                        field_map=fm_pub, private=False)
    check("S1.g public payload: Public=True explicitly",
          p["Core_IPS_Public__c"] is True, str(p))
    check("S1.h public payload: public comment type, never the private one",
          p.get("Core_IPS_Case_Comment_Type__c") == "Public to Customers"
          and p.get("Core_IPS_Comment_Author_Type__c") == "Agent", str(p))
    p_priv = IpsClient.build_comment_payload("500XYZ", "<p>r</p>", field_map=fm_pub)
    check("S1.h2 private payload keeps the private comment type",
          p_priv.get("Core_IPS_Case_Comment_Type__c") == "Private to Intel"
          and p_priv["Core_IPS_Public__c"] is False, str(p_priv))
    # This org's customer-visible comments carry Public=False as well (the
    # comment type is what makes them visible): an explicit public_value wins
    # over the inverse-of-private default; a non-boolean one is ignored.
    p_pub_explicit = IpsClient.build_comment_payload(
        "500XYZ", "<p>r</p>", field_map=dict(fm_pub, public_value=False), private=False)
    p_pub_bad = IpsClient.build_comment_payload(
        "500XYZ", "<p>r</p>", field_map=dict(fm_pub, public_value="no"), private=False)
    check("S1.h3 public payload: an explicit boolean public_value is used as is, else the inverse",
          p_pub_explicit["Core_IPS_Public__c"] is False
          and p_pub_explicit.get("Core_IPS_Case_Comment_Type__c") == "Public to Customers"
          and p_pub_bad["Core_IPS_Public__c"] is True, f"{p_pub_explicit} {p_pub_bad}")
    try:
        IpsClient.build_comment_payload("500XYZ", "<p>r</p>",
                                        field_map={"body_field": "B__c"},
                                        private=False)
        check("S1.i public refused without verified privacy field", False)
    except PostUnsupported:
        check("S1.i public refused without verified privacy field", True)
    # A migrated map with a verified boolean but no public comment type must
    # not post an invisible customer reply either, whatever extra_fields says.
    try:
        IpsClient.build_comment_payload("500XYZ", "<p>r</p>", private=False,
                                        field_map=dict(fm, extra_fields={"Src__c": "x"}))
        check("S1.j public refused when no public comment type is configured at all", False)
    except PostUnsupported as e:
        check("S1.j public refused when no public comment type is configured at all",
              "public_extra_fields" in str(e), str(e))
    check("S1.k customer-updates SOQL has no LIMIT unless asked (caller pages)",
          "LIMIT" not in build_customer_updates_soql(["500A"], "2026-10-05T00:00:00Z")
          and build_customer_updates_soql(["500A"], "2026-10-05T00:00:00Z", limit=7)
          .endswith("LIMIT 7"))


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
    # Beacon is the user-facing name (footer). The IPS markers are the dedup
    # contract with every comment already posted: they must stay as they are.
    from .composer import CHECKLIST_TAG, _FOOTER
    check("S2.h footer names Beacon; AI_MARKER / CHECKLIST_TAG unchanged (dedup contract)",
          "Generated by Beacon" in _FOOTER and full["plain"].startswith(AI_MARKER)
          and AI_MARKER == "[AI-Avatar preliminary analysis]"
          and CHECKLIST_TAG == "[AI-Avatar first-response]", _FOOTER)


# ---------------------------------------------------------------- S3
def smoke_queue(tmp: Path) -> None:
    print("[S3] Queue store")
    from .queue import HandsfreeStore
    store = HandsfreeStore(tmp / "handsfree")
    rec = store.enqueue(case_nbr="01234567", case_id="500XYZ", subject="s",
                        draft_plain="body", draft_html="<p>body</p>",
                        confidence=88, mode="full", analysis={"k": 1},
                        owner_name="Charles P Chu")
    check("S3.a enqueued pending_review", rec["status"] == "pending_review")
    check("S3.b ledger marks analyzed", store.is_processed("01234567"))
    owner_items = store.list_drafts(owner_name="charles p chu")
    check("S3.b2 owner is persisted and filter is case-insensitive",
          len(owner_items) == 1 and owner_items[0]["owner_name"] == "Charles P Chu",
          str(owner_items))
    check("S3.b3 unknown owner filter returns no drafts",
          store.list_drafts(owner_name="Another Owner") == [])
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
    # Queue rows expose the decoded-log folder only when a log was decoded.
    cap = tmp / "cap" / "WifiDriverIHVSession.etl.001.log"
    rec_log = store.enqueue(case_nbr="01234568", case_id="500XYZ", subject="s",
                            draft_plain="body", draft_html="<p>body</p>",
                            confidence=None, mode="full",
                            analysis={"log_path": str(cap)})
    by_id = {i["draft_id"]: i for i in store.list_drafts()}
    check("S3.h decoded-log folder listed only for decoded cases",
          by_id[rec_log["draft_id"]]["log_dir"] == str(cap.parent)
          and by_id[rec["draft_id"]]["log_dir"] == "",
          str({k: v.get("log_dir") for k, v in by_id.items()}))
    rec_err = store.enqueue(case_nbr="01234569", case_id="", subject="",
                            draft_plain="fetch failed", draft_html="<p>x</p>",
                            confidence=None, mode="error", analysis={"error": "no subject"})
    check("S3.j an 'error' draft (case fetch failed) is queued but never marks the "
          "case processed",
          rec_err["status"] == "pending_review" and not store.is_processed("01234569")
          and store.ledger_entry("01234569") == {})
    import threading as _th
    def _writer(key):
        for i in range(40):
            store.save_config({key: i})
    ts = [_th.Thread(target=_writer, args=(k,)) for k in ("w_a", "w_b", "w_c")]
    [t.start() for t in ts]
    [t.join() for t in ts]
    cfg_after = store.load_config()
    check("S3.k concurrent partial config writes never lose each other's keys",
          all(cfg_after.get(k) == 39 for k in ("w_a", "w_b", "w_c"))
          and cfg_after["owner_name"] == "Charles P Chu", str(cfg_after))
    # Reset a case: unposted drafts archived, posted ones + post marks kept,
    # analysis marks dropped, decoded files purged only on request.
    from . import bsod as bsod_mod
    orig_sub_path = bsod_mod._submissions_path
    bsod_mod._submissions_path = lambda: tmp / "handsfree" / "bsod_submissions.json"
    try:
        case_dir = tmp / "01234570"
        cap_dir = case_dir / "WRT" / "Log" / "cap_01-01-2026_10-00-00_1"
        cap_dir.mkdir(parents=True, exist_ok=True)
        (cap_dir / "WifiDriverIHVSession.etl.001").write_bytes(b"etl")
        (cap_dir / "WifiDriverIHVSession.etl.001.log").write_text("log", encoding="utf-8")
        (case_dir / "upload.zip").write_bytes(b"zip")
        r1 = store.enqueue(case_nbr="01234570", case_id="500R", subject="s",
                           draft_plain="b", draft_html="<p>b</p>", confidence=70, mode="full",
                           analysis={"log_path": str(cap_dir / "WifiDriverIHVSession.etl.001.log")})
        r2 = store.enqueue(case_nbr="01234570", case_id="500R", subject="s",
                           draft_plain="fr", draft_html="<p>fr</p>", confidence=None,
                           mode="first_response", analysis={})
        store.update(r2["draft_id"], status="posted", post_result={"ok": True})
        store.mark_posted("01234570", comment_id="CMT-1", first_response=True)
        store.mark_customer_update("01234570", "2026-10-01T00:00:00Z")
        bsod_mod.record_submission("01234570", ["x.zip"], "\\\\share\\01234570", "a@b", 200)
        res = store.reset_case("01234570", purge_downloads=True, case_dir=str(case_dir))
        forgotten = bsod_mod.forget_case("01234570")
        after = store.ledger_entry("01234570")
        ids_left = {d["draft_id"] for d in store.list_drafts(include_closed=True)
                    if d["case_nbr"] == "01234570"}
        check("S3.r reset_case: unposted draft archived (not deleted), posted draft and the "
              "post marks kept, analysis marks dropped, decoded/extracted files purged but "
              "the archive kept, BSOD record forgotten",
              res["archived"] == [r1["draft_id"]] and res["kept_posted"] == [r2["draft_id"]]
              and (tmp / "handsfree" / Path(res["archive_dir"]).name / (r1["draft_id"] + ".json")).exists()
              and ids_left == {r2["draft_id"]}
              and "analyzed_at" not in after and "last_customer_update" not in after
              and after.get("first_response_comment_id") == "CMT-1" and after.get("comment_id") == "CMT-1"
              and store.first_response_posted("01234570")
              and not (cap_dir / "WifiDriverIHVSession.etl.001.log").exists()
              and not (cap_dir / "WifiDriverIHVSession.etl.001").exists()
              and (case_dir / "upload.zip").exists()
              and forgotten == 1 and bsod_mod.submissions("01234570") == [],
              f"res={res} after={after} left={ids_left}")
        store.list_drafts()   # still readable
    finally:
        bsod_mod._submissions_path = orig_sub_path
    from .case_reader import normalize_action_owner as nao
    check("S3.i action owner normalized: intel / customer / unknown",
          nao("Intel") == "intel" and nao("case owner (Intel FAE)") == "intel"
          and nao("Customer") == "customer" and nao("partner / OEM") == "customer"
          and nao("unknown") == "" and nao(None) == ""
          and by_id[rec["draft_id"]]["action_owner"] == "")


# ---------------------------------------------------------------- S4
def smoke_runner(tmp: Path) -> None:
    from .composer import CHECKLIST_TAG
    print("[S4] Runner end-to-end (all externals mocked, fake agent)")
    import types
    from configs.global_configs import app_config
    from models.models import CaseContext
    from services.chatbot.engine.system import Skill
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
    # The real loader needs app_config.prompt_dir (unset without app boot);
    # without this stub the triage stage fails and analyze_desc never runs.
    orig_prompt = cis.CaseService.load_case_summary_prompt

    def _fake_process(case_ctx: CaseContext) -> CaseContext:
        case_ctx.id = "500FAKESFID000AAA"
        case_ctx.subject = "Wi-Fi drops right after connecting"
        case_ctx.description = "Device sometimes disconnects. First seen last week."
        case_ctx.wifi_or_bt = "wifi"
        case_ctx.subcategory = "WiFi Connectivity"
        case_ctx.case_download_dir = str(case_dir)
        case_ctx.comments = [
            [datetime(2026, 6, 19, 9, 0), "Partner",
             "Initial report: disconnect happens randomly."],
            [datetime(2026, 6, 20, 11, 0), "Partner",
             "Reproduced today at 10:17:30. Uploaded repro_logs.7z (driver log) covering it."],
            [datetime(2026, 6, 21, 8, 0), "Partner",
             "Also uploaded later_capture.zip but device did NOT fail in that run."],
            [datetime(2026, 6, 21, 9, 0), "Agent",
             "INTERNAL: suspect FW regression, sighting 12345 - do not share."],
        ]
        case_ctx.attachment_list = [
            ["repro_logs.7z", "https://esft/x?FileName=repro_logs.7z",
             ["06/20/2026 10:20", "repro at 10:17:30"]],
            ["later_capture.zip", "https://esft/x?FileName=later_capture.zip",
             ["06/21/2026 08:00", "no failure in this run"]],
        ]
        case_ctx.env_detail = {"Computer Model": "LvzhouD",
                               "Assert Error (32-bit value or NA)": "NA"}
        return case_ctx

    # --- mock download + decompose ---
    from utils import attachment_download as adl
    from utils import attachment_decompose as adc
    orig_dload, orig_zip = adl.run_dload_threads, adc.process_single_zip
    from services.etl_parser import bt_parser
    orig_bt_decode = bt_parser.bt_decode_via_cli

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
        "missing_info": [{"item": "repro_steps",
                          "reason": "no reproduction steps mentioned anywhere"}],
        "action_owner": "Intel",
        "next_action": "Intel to analyze repro_logs.7z for the 10:17:30 failure",
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
    # Vision-aware wrapper: a content LIST (image blocks) gets the image
    # description reply; plain text prompts go to whatever `chat` is set to
    # at that moment (the tests swap it). The last text prompt is kept so a
    # test can assert what the reader was shown.
    seen_prompts = {}
    _plain_chat = lambda messages, system_content=None: fake_llm.chat_text(messages, system_content)
    def _vision_or_text(messages, system_content=None):
        content = messages[0]["content"]
        if isinstance(content, list):
            seen_prompts["vision_blocks"] = content
            return '{"images": ["WRT tool screenshot: Dump & Collect button highlighted, preset Alnair applied; annotation: press after issue happens"]}'
        seen_prompts["text"] = content
        return fake_llm.chat_text(messages, system_content)
    fake_llm.chat_text = fake_llm.chat
    fake_llm.chat = _vision_or_text
    orig_llm = getattr(app_config, "llm_helper", None)
    orig_agent = getattr(app_config, "log_chatbot_agent", None)
    orig_files_dir = getattr(app_config, "avatarfiles_dir", None)

    from . import ips_client as ipc_mod
    orig_ipc = ipc_mod.IpsClient

    class _FakeIps:
        """One Partner comment with an embedded screenshot (force.com URL)."""
        FIELD_RICH_BODY = "Core_IPS_Rich_Comment__c"
        fetched: list = []
        def get_case_comments(self, case_id):
            return [{"Id": "CIMG1", "CreatedDate": "2026-06-20T03:05:00.000+0000",
                     "Core_IPS_Comment_Author_Type__c": "Partner",
                     "Core_IPS_Rich_Comment__c":
                         '<p>Repro steps attached as screenshot.<br/><img src="https://intel.file.force.com/servlet/rtaImage?eid=1&amp;refid=2">'
                         '<img src="https://attacker.file.force.com/servlet/rtaImage?refid=9">'
                         '<img src="http://intel.file.force.com/servlet/rtaImage?refid=8"></p>'}]
        def fetch_binary(self, url, max_bytes=0):
            _FakeIps.fetched.append(url)
            return ("image/png", b"PNG fake")

    try:
        ipc_mod.IpsClient = _FakeIps
        cis.CaseService.process_case = staticmethod(_fake_process)
        cis.CaseService.load_case_summary_prompt = staticmethod(
            lambda wifi_or_bt: "prompt_fake.py")
        adl.run_dload_threads = _fake_dload
        adc.process_single_zip = _fake_zip_proc
        ita.organize_issue_context = _fake_org
        app_config.llm_helper = fake_llm
        app_config.log_chatbot_agent = None
        if not orig_files_dir:
            app_config.avatarfiles_dir = str(tmp)

        r = runner_mod.HandsfreeRunner(
            progress_cb=lambda s, d: print(f"    · {s}: {d}"))

        # BT cases must inspect extracted contents before triage-only exit.
        # This matches Avatar's local-upload rule: ibtpci-/ibtusb- ETLs are
        # recognized BT captures.
        bt_etl = case_dir / "bt_capture" / "ibtpci-driver.etl"
        bt_etl.parent.mkdir(parents=True, exist_ok=True)
        bt_etl.write_bytes(b"\x00fake")
        bt_hci = Path(str(bt_etl) + ".hci.txt")
        bt_hci.write_text("12:00:00.000 [INFO] Bluetooth capture\n",
                          encoding="utf-8")

        def _fake_process_bt(case_ctx: CaseContext) -> CaseContext:
            case_ctx = _fake_process(case_ctx)
            case_ctx.wifi_or_bt = "bt"
            case_ctx.subcategory = "Bluetooth"
            return case_ctx

        cis.CaseService.process_case = staticmethod(_fake_process_bt)
        adc.process_single_zip = lambda *a, **k: ([], [], [], [str(bt_etl)], [])
        bt_parser.bt_decode_via_cli = lambda *a, **k: str(bt_hci)
        bt_analysis = r.analyze_case("01234567")
        bt_stages = [s.name for s in bt_analysis.stages]
        check("S4.bt valid BT capture is content-checked",
              bt_analysis.bt_case_valid is True
              and bt_analysis.mode == "full"
              and bt_analysis.log_path == str(bt_hci)
              and "decompose" in bt_stages
              and "check_bt_log" in bt_stages
              and "decode_bt" in bt_stages      # cached HCI: recorded, not skipped
              and "agent_analysis" in bt_stages,
              f"valid={getattr(bt_analysis, 'bt_case_valid', None)} "
              f"mode={bt_analysis.mode} log={bt_analysis.log_path} stages={bt_stages}")

        cis.CaseService.process_case = staticmethod(_fake_process)
        adc.process_single_zip = _fake_zip_proc
        # Same run with a snapshot collector (the page's live panel feed).
        live_snaps: list = []
        r_live = runner_mod.HandsfreeRunner(progress_cb=r.progress,
                                            snapshot_cb=live_snaps.append)
        # One run with the Echo next-step advice on (faked, answered in the
        # background thread): it must land on the analysis, in the live
        # snapshots and in the private draft, and never in a public reply.
        from . import echo_client as echo_mod
        orig_ask = echo_mod.ask_echo_kb
        asked_adv: list = []
        def _fake_advice(q, **kw):
            asked_adv.append(q)
            return ("- Ask the customer for the AP model / controller version and a sniffer "
                    "capture during the drop.\n- Check the roaming thresholds.")
        echo_mod.ask_echo_kb = _fake_advice
        runner_mod.HandsfreeRunner.ECHO_ADVICE_ENABLED = True
        try:
            analysis = r_live.analyze_case("01234567")
        finally:
            runner_mod.HandsfreeRunner.ECHO_ADVICE_ENABLED = False
            echo_mod.ask_echo_kb = orig_ask
        from .composer import compose_plain as _cp_adv
        adv = analysis.echo_advice
        adv_snaps = [sn["echo_advice"]["status"] for sn in live_snaps if sn.get("echo_advice")]
        check("S4.echo next-step advice: the question carries the case summary, category "
              "and attachments; answered in the background; live snapshots went asking -> "
              "answered; the private draft quotes it",
              len(asked_adv) == 1 and "Connectivity" in asked_adv[0]
              and "repro_logs.7z" in asked_adv[0] and "NEXT STEPS" in asked_adv[0]
              and adv.get("status") == "answered" and "sniffer" in adv.get("answer", "")
              and adv_snaps[:1] == ["asking"] and "answered" in adv_snaps
              and "Echo knowledge base — suggested next steps" in _cp_adv(analysis),
              f"asked={len(asked_adv)} adv={ {k: adv.get(k) for k in ('status', 'error', 'elapsed_s')} } "
              f"snaps={adv_snaps} q={asked_adv[0][:200] if asked_adv else ''}")

        check("S4.a mode is full", analysis.mode == "full",
              f"mode={analysis.mode} err={analysis.error} stages={[ (s.name, s.ok, s.detail) for s in analysis.stages ]}")
        check("S4.b agent produced a report",
              analysis.best_incident is not None and
              analysis.best_incident.confidence == 90)
        check("S4.c log path resolved", analysis.log_path.endswith(".etl.001.log"))
        check("S4.d issue time from comments (reader)",
              analysis.issue_times == ["06/20/2026-10:17:30"]
              and analysis.case_reader.get("issue_time_source") == "comment #2")
        live = {}
        for sn in live_snaps:
            live.update(sn)
        sent = (live.get("sent_for_analysis") or [{}])[0]
        inc0 = analysis.incidents[0] if analysis.incidents else None
        try:
            json.dumps(live)
            live_json_ok = True
        except Exception:
            live_json_ok = False
        check("S4.live snapshots: case facts as they become known, the exact agent "
              "inputs (description + both clocks + log), the final outcome; JSON-safe",
              live_json_ok and live.get("subject") == analysis.subject
              and live.get("issue_times") == analysis.issue_times
              and live.get("issue_time_source") == "comment #2"
              and live.get("chosen_attachment") == "repro_logs.7z"
              and live.get("finished") is True and live.get("mode") == "full"
              and live.get("confidence") == 90
              and any(sn.get("current_stage") == "agent_analysis" for sn in live_snaps)
              and {st["name"] for st in live.get("stages") or []} >= {"fetch_case", "agent_analysis"}
              and inc0 is not None and sent == inc0.sent_for_analysis
              and sent.get("user_message") == inc0.user_message
              and sent.get("description", "").startswith(analysis.clean_description)
              and bool(sent.get("issue_time_log"))
              and sent.get("issue_time_reported") == "06/20/2026-10:17:30"
              and sent.get("log_file", "").endswith(".log"),
              f"json_ok={live_json_ok} keys={sorted(live)[:12]} sent={sent} "
              f"stages={[sn.get('current_stage') for sn in live_snaps if 'current_stage' in sn][:20]}")
        dd, tdn, pdn = analysis.domain_decision, analysis.time_decision, analysis.pick_decision
        cand_src = [c["source"] for c in dd.get("candidates", [])]
        chosen_caps = [c for c in pdn.get("captures", []) if c.get("role") == "chosen"]
        check("S4.img live panel carries the image reader's output (comment, author, date, "
              "the description text)",
              isinstance(live.get("comment_images"), list) and live["comment_images"]
              and live["comment_images"][0].get("comment_id") == "CIMG1"
              and live["comment_images"][0].get("author") == "Partner"
              and "WRT" in (live["comment_images"][0].get("descriptions") or [""])[0],
              f"imgs={live.get('comment_images')}")
        # The fixture carries an explicit IPS subcategory ('WiFi Connectivity'),
        # so with the fake reader saying 'Others' the subcategory, not triage,
        # is the first specific candidate.
        check("S4.dec1 issue-category decision: candidates in priority order (reader, IPS "
              "subcategory, triage); the fake reader says 'Others' so the IPS subcategory "
              "wins over triage; the domain's required logs named",
              dd.get("winner") == "Connectivity" and dd.get("winner_source") == "ips_subcategory"
              and cand_src == ["reader", "ips_subcategory", "triage"]
              and dd["candidates"][0]["resolved"] == "Others" and not dd["candidates"][0]["specific"]
              and dd.get("provisional") and isinstance(dd.get("required_logs"), list)
              and any("WRT" in i or "WPP" in i for i in dd["required_logs"]),
              f"dd={dd}")
        check("S4.dec2 issue-time decision: the reader's time with its source, the rule, "
              "gate ok, placement inside the log with the chosen candidate",
              tdn.get("found") and tdn["found"][0]["time"] == "06/20/2026-10:17:30"
              and tdn["found"][0]["source"] == "comment #2" and tdn.get("gate") == "ok"
              and tdn.get("rule") and tdn.get("placements")
              and any(c["inside"] for c in tdn["placements"][0]["candidates"])
              and tdn.get("coverage", {}).get("covered") is True
              and tdn["coverage"]["chosen"]["log"].startswith("06/20/2026-10:17"),
              f"td={tdn}")
        check("S4.dec3 archive + capture decision: uploads listed, the reader's pick matched "
              "exactly, the capture closest at/after the issue time chosen (normalized time), "
              "the chosen capture marked decoded + covering",
              [a["name"] for a in pdn.get("archives", [])] == ["repro_logs.7z", "later_capture.zip"]
              and pdn.get("reader_pick") == "repro_logs.7z" and pdn.get("match") == "exact"
              and pdn.get("chosen_rule") == "reader" and not pdn.get("fallback_used")
              and pdn.get("etl_rule") == "closest_at_or_after_issue"
              and pdn.get("issue_time_used") == "06/20/2026-10:17:30"
              and len(chosen_caps) == 1 and chosen_caps[0]["folder"] == "capture_20-06-2026_10-20-00"
              and chosen_caps[0]["delta_s"] == 150 and chosen_caps[0]["decode"] == "ok"
              and chosen_caps[0]["covered"] is True
              and (pdn.get("contents") or {}).get("wifi_wpp") == 1,
              f"pd={pdn}")
        agent_snaps = [sn["agent_live"] for sn in live_snaps if sn.get("agent_live")]
        running = [a for a in agent_snaps if a.get("status") == "running"]
        final = agent_snaps[-1] if agent_snaps else {}
        check("S4.live2 AI analysis streamed: running snapshots carry the agent's steps "
              "(kind + text), the last one carries the result (confidence 90, root cause); "
              "Echo stage reports its evidence verdict",
              running and all(isinstance(a.get("steps"), list) for a in running)
              and any(st.get("kind") and st.get("text") for a in running for st in a["steps"])
              and final.get("status") == "report" and (final.get("result") or {}).get("confidence") == 90
              and "deauth" in (final.get("result") or {}).get("root_cause_summary", "").lower()
              and live.get("agent_results") and live["agent_results"][0]["confidence"] == 90
              and live["agent_results"][0].get("trace") and live["incidents"][0].get("trace")
              and all("kind" in st and "text" in st for st in live["agent_results"][0]["trace"])
              and live.get("incidents") and live["incidents"][0]["confidence"] == 90
              and live.get("log_path", "").endswith(".log")
              and "echo_live_summary" in live,
              f"running={len(running)} kinds={[st.get('kind') for a in running[:1] for st in a['steps']][:8]} "
              f"final={ {k: final.get(k) for k in ('status', 'result')} } echo={live.get('echo_live_summary')!r}")
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
        check("S4.d2 next action + owner read from the thread, owner normalized",
              analysis.action_owner == "intel"
              and analysis.next_action.startswith("Intel to analyze repro_logs.7z"),
              f"{analysis.action_owner!r} {analysis.next_action!r}")
        check("S4.i2 comment screenshot downloaded, described, fed to the reader "
              "and the customer history; stage recorded OK",
              len(analysis.comment_images) == 1
              and "Dump & Collect" in analysis.comment_images[0]["descriptions"][0]
              and "Dump & Collect" in analysis.customer_history
              and "[Images embedded in the comment of 2026-06-20" in seen_prompts.get("text", "")
              and _FakeIps.fetched == ["https://intel.file.force.com/servlet/rtaImage?eid=1&refid=2"]
              and any(st.name == "read_images" and st.ok for st in analysis.stages)
              and seen_prompts["vision_blocks"][2]["type"] == "image_url",
              f"images={analysis.comment_images} fetched={_FakeIps.fetched} "
              f"stages={[(st.name, st.ok, st.detail) for st in analysis.stages if st.name == 'read_images']}")
        check("S4.h2 customer history keeps Partner comments, drops Intel-side ones",
              "Reproduced today at 10:17:30" in analysis.customer_history
              and "INTERNAL" not in analysis.customer_history,
              analysis.customer_history)

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
        check("S4.f3 private draft states the next action and its owner",
              "Next action — Intel (case owner): Intel to analyze repro_logs.7z"
              in rec["draft_plain"],
              rec["draft_plain"][:300])
        slim = next(i for i in store.list_drafts() if i["draft_id"] == rec["draft_id"])
        check("S4.f4 queue row carries action owner + next action",
              slim["action_owner"] == "intel"
              and slim["next_action"].startswith("Intel to analyze"), str(slim))
        check("S4.f2 minor info gap -> best-effort analysis + clarification section",
              analysis.mode == "full"
              and [m["item"] for m in analysis.missing_info] == ["repro_steps"]
              and "Request for clarification" in rec["draft_plain"]
              and "step-by-step reproduction instructions" in rec["draft_plain"],
              rec["draft_plain"][:300])

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
        check("S4.m2 checklist never ticks the WRT log the reply asks for",
              "provided: repro_logs.7z" not in draft3["plain"]
              and "[ ] 1) WPP driver log" in draft3["plain"],
              draft3["plain"][-700:])
        # Autologger-style archive: firmware + event logs but no driver WPP
        # ETL — the reply says what was found instead of "no WRT logs".
        adc.process_single_zip = lambda *a, **k: ([], [], ["sys.evt"], [], ["wrt-fw.etl"])
        analysis3b = r.analyze_case("01234567")
        draft3b = compose(analysis3b)
        check("S4.m3 archive with fw/event logs only: reply names what it holds and asks "
              "for the WPP driver log",
              analysis3b.mode == "request_logs"
              and analysis3b.log_request_reason == "no_wrt_inside"
              and analysis3b.archive_contents == {"wifi_wpp": 0, "ddd": 0, "event_logs": 1,
                                                  "bt": 0, "fw": 1}
              and "it contains firmware (wrt-fw) traces, Windows event logs but no Intel "
                  "Wi-Fi driver WPP log (WifiDriverIHVSession.etl)" in draft3b["plain"],
              f"reason={analysis3b.log_request_reason} contents={analysis3b.archive_contents} "
              f"{draft3b['plain'][:400]}")

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

        # --- S9: case-information completeness gate --------------------------
        severe_reply = json.dumps({
            "clean_description": "", "issue_times": [], "issue_time_source": "",
            "attachment_name": "", "attachment_reason": "",
            "reasoning": "boilerplate description, nothing concrete",
            "missing_info": [
                {"item": "issue_description",
                 "reason": "no understandable failure statement"},
                {"item": "issue_time", "reason": "no failure time stated"},
                {"item": "repro_steps", "reason": "no repro steps"},
            ],
        })
        fake_llm.chat_text = lambda messages, system_content=None: severe_reply
        analysis5 = r.analyze_case("01234567")
        stage_names5 = [s.name for s in analysis5.stages]
        check("S9.a severe info gap -> request_info, stops before pick_zip",
              analysis5.mode == "request_info"
              and "pick_zip" not in stage_names5
              and "check_case_info" in stage_names5,
              f"mode={analysis5.mode} stages={stage_names5}")
        draft5 = compose(analysis5)
        check("S9.b request-info reply asks for every missing item",
              "Could you please provide:" in draft5["plain"]
              and "clearer description of the issue" in draft5["plain"]
              and "exact date and time (with timezone)" in draft5["plain"]
              and "step-by-step reproduction instructions" in draft5["plain"]
              and draft5["confidence"] is None,
              draft5["plain"][:400])
        # Reader named no domain -> the triage issue type (Connectivity) is
        # the fallback, not Others.
        check("S9.b2 request-info reply carries the pre-filled domain checklist",
              "=== General Info ===" in draft5["plain"]
              and "=== Required Log (Connectivity) ===" in draft5["plain"]
              and "please provide" in draft5["plain"],
              draft5["plain"][-500:])

        # Fallback-found issue time retracts the gap (stage-7 refinement).
        # No minute-precise issue time (the reader only found "June 2nd"):
        # ask for the exact time first — no download, no decode, no agent.
        time_gap_reply = json.dumps({
            "clean_description": "Device disconnects after association",
            "issue_times": ["June 2nd"], "issue_time_source": "comment #3",
            "attachment_name": "repro_logs.7z",
            "attachment_reason": "only driver log upload",
            "reasoning": "only a date was stated",
            "missing_info": [],
        })
        fake_llm.chat_text = lambda messages, system_content=None: time_gap_reply
        adc.process_single_zip = _fake_zip_proc
        analysis6 = r.analyze_case("01234567")
        d6 = compose(analysis6)
        check("S9.c vague issue time only -> request_info before any download; "
              "exact time asked for, nothing ticked as the reproduction time",
              analysis6.mode == "request_info"
              and analysis6.issue_times == []
              and analysis6.case_reader.get("vague_times") == ["June 2nd"]
              and [m["item"] for m in analysis6.missing_info] == ["issue_time"]
              and "download" not in [s.name for s in analysis6.stages]
              and "exact date and time (with timezone)" in d6["plain"]
              and "June 2nd" not in d6["plain"],
              f"mode={analysis6.mode} times={analysis6.issue_times} "
              f"missing={analysis6.missing_info} stages={[s.name for s in analysis6.stages]}")

        # Precise time, but the only log covers another window (fixture log
        # is 06/20 10:16-10:44): public request for a matching log, no agent.
        wrong_time_reply = json.loads(reader_reply)
        wrong_time_reply.update({"issue_times": ["06/20/2026-08:00:00"]})
        fake_llm.chat_text = lambda messages, system_content=None: json.dumps(wrong_time_reply)
        analysis6b = r.analyze_case("01234567")
        d6b = compose(analysis6b)
        check("S9.c2 log does not cover the issue time -> request_logs with the "
              "covering window, no agent / Echo run",
              analysis6b.mode == "request_logs"
              and analysis6b.log_request_reason == "log_not_covering"
              and "agent_analysis" not in [s.name for s in analysis6b.stages]
              and "echo_kb" not in [s.name for s in analysis6b.stages]
              and "covers 06/20/2026-10:16:01" in d6b["plain"]
              and "reported issue time is 06/20/2026-08:00:00" in d6b["plain"]
              and "captured at the issue time" in d6b["plain"],
              f"mode={analysis6b.mode} reason={analysis6b.log_request_reason} "
              f"{d6b['plain'][:500]}")

        # YB exception (2026-10-07): same mismatch, but a yellow-bang case
        # whose log holds an assert continues on the assert evidence.
        etl_yb = case_dir / "capture_yb_20-06-2026_10-20-00_5_5055_0x20101f01_0x0_0x0" / "WifiDriverIHVSession.etl.001"
        etl_yb.parent.mkdir(parents=True, exist_ok=True)
        etl_yb.write_bytes(b"\x00fake")
        Path(str(etl_yb) + ".log").write_text("\n".join(log_lines[:1] + [
            "06/20/2026-10:20:00.000 [17] [NIC_DEBUG] [ERROR] [x]:FATAL_ERROR: uCode ASSERT(UMAC, rtStatus = 0x20101F01, data1 = 0x0, data2 = 0x0)",
        ] + log_lines[-1:]), encoding="utf-8")
        yb_reply = dict(wrong_time_reply, issue_domain="Yellow Bang")
        fake_llm.chat_text = lambda messages, system_content=None: json.dumps(yb_reply)
        adc.process_single_zip = lambda *a, **k: ([str(etl_yb)], [], [], [], [])
        analysis6c = r.analyze_case("01234567")
        adc.process_single_zip = _fake_zip_proc
        d6c = compose(analysis6c)
        check("S9.c3 YB case + assert in the log -> analysis continues despite the "
              "time mismatch; draft explains it",
              analysis6c.yb_case
              and analysis6c.time_mismatch.get("yb_assert_override") == ["0x20101F01"]
              and analysis6c.mode in ("full", "triage_only")
              and "agent_analysis" in [s.name for s in analysis6c.stages]
              and "echo_kb" in [s.name for s in analysis6c.stages]
              and "TIME MISMATCH" in d6c["plain"]
              and "assert(s) 0x20101F01 found in this log" in d6c["plain"]
              and "please help provide a WRT log" not in d6c["plain"],
              f"mode={analysis6c.mode} yb={analysis6c.yb_case} tm={analysis6c.time_mismatch} "
              f"stages={[s.name for s in analysis6c.stages]} {d6c['plain'][:300]}")

        # request_logs + info gaps merge into ONE public reply.
        analysis2.missing_info = [{"item": "issue_time", "reason": ""}]
        draft2b = compose(analysis2)
        check("S9.d request-logs reply merges info asks + checklist body",
              "In addition, to speed up the analysis" in draft2b["plain"]
              and "exact date and time (with timezone)" in draft2b["plain"]
              and "=== General Info ===" in draft2b["plain"],
              draft2b["plain"][:400])
        analysis2.missing_info = []
        draft2c = compose(analysis2)
        check("S9.d2 log-only request still carries the checklist",
              "=== General Info ===" in draft2c["plain"]
              and "In addition, to speed up" not in draft2c["plain"])

        # Env form fills the gap: reader flags repro_steps, but the customer
        # answered "Steps to reproduce" in Environment Details -> retracted.
        def _fake_process_env_steps(case_ctx: CaseContext) -> CaseContext:
            case_ctx = _fake_process(case_ctx)
            case_ctx.env_detail = {
                "Steps to reproduce": "1. Run Burn-in stress 2. Check device status in Device Manager"}
            return case_ctx
        cis.CaseService.process_case = staticmethod(_fake_process_env_steps)
        fake_llm.chat_text = lambda messages, system_content=None: reader_reply
        analysis7 = r.analyze_case("01234567")
        check("S11.e env-form repro steps retract the reader-flagged gap",
              analysis7.mode == "full" and analysis7.missing_info == []
              and analysis7.env_detail.get("Steps to reproduce", "").startswith("1. Run"),
              f"missing={analysis7.missing_info}")

        # A subcategory that is not explicitly Wi-Fi/BT is unsupported, even
        # though CaseService's legacy rule labels it 'bt'.
        def _fake_process_bt_oem(case_ctx: CaseContext) -> CaseContext:
            case_ctx = _fake_process(case_ctx)
            case_ctx.wifi_or_bt = "bt"
            case_ctx.subcategory = "OEM Tools"
            return case_ctx
        cis.CaseService.process_case = staticmethod(_fake_process_bt_oem)
        analysis8 = r.analyze_case("01234567")
        check("S11.f non-Wi-Fi/BT subcategory is unsupported",
              analysis8.mode == "unsupported",
              f"mode={analysis8.mode}")

        # --- S13: failure-path reply policy (2026-10-02) --------------------
        cis.CaseService.process_case = staticmethod(_fake_process)

        # a) pick_zip crash -> ask the customer to re-upload
        from . import case_reader as cr_mod
        orig_find = cr_mod.find_attachment
        def _find_boom(*a, **k):
            raise RuntimeError("boom")
        cr_mod.find_attachment = _find_boom
        analysis9 = r.analyze_case("01234567")
        cr_mod.find_attachment = orig_find
        d9 = compose(analysis9)
        check("S13.a pick_zip crash -> public re-upload request",
              analysis9.mode == "request_logs"
              and analysis9.log_request_reason == "unreadable_archive"
              and "RE-UPLOAD" in d9["plain"],
              f"mode={analysis9.mode} reason={analysis9.log_request_reason}")

        # b) download fails once -> automatic retry recovers
        calls = {"n": 0}
        def _flaky_dload(att_list, download_path, socketio):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("share hiccup")
            return [[str(case_dir / att_list[0][0]), att_list[0][0], True]]
        adl.run_dload_threads = _flaky_dload
        analysis10 = r.analyze_case("01234567")
        check("S13.b download retry recovers to full analysis",
              analysis10.mode == "full" and calls["n"] == 2,
              f"mode={analysis10.mode} attempts={calls['n']}")

        # c) download fails twice -> re-upload request
        def _dead_dload(*a, **k):
            raise RuntimeError("share down")
        adl.run_dload_threads = _dead_dload
        analysis10b = r.analyze_case("01234567")
        adl.run_dload_threads = _fake_dload
        check("S13.c double download failure -> public re-upload request",
              analysis10b.mode == "request_logs"
              and analysis10b.log_request_reason == "unreadable_archive",
              f"mode={analysis10b.mode}")

        # d) decode failure caused by a missing/purged PDB is explained
        etl2 = case_dir / "capture2" / "WifiDriverIHVSession.etl.009"
        etl2.parent.mkdir(parents=True, exist_ok=True)
        etl2.write_bytes(b"x")
        adc.process_single_zip = lambda *a, **k: ([str(etl2)], [], [], [], [])
        orig_decode = runner_mod.HandsfreeRunner._decode_etl
        def _pdb_fail(self, path):
            raise RuntimeError("Netwaw18.pdb could not be extracted")
        runner_mod.HandsfreeRunner._decode_etl = _pdb_fail
        analysis11 = r.analyze_case("01234567")
        runner_mod.HandsfreeRunner._decode_etl = orig_decode
        adc.process_single_zip = _fake_zip_proc
        d11 = compose(analysis11)
        check("S13.d PDB decode failure explained in the draft",
              analysis11.mode == "triage_only"
              and "PDB symbol database" in analysis11.error
              and "PDB symbol database" in d11["plain"],
              str(analysis11.error)[:140])

        # --- S14: what the orchestrator queues per case ----------------------
        from . import orchestrator as orch
        store14 = HandsfreeStore(tmp / "handsfree_s14")
        orch._analyze_and_enqueue(store14, "01234567")
        recs = [store14.get(d["draft_id"])
                for d in store14.list_drafts(include_closed=True)]
        fr = [x for x in recs if x.get("mode") == "first_response"]
        check("S14.a analysis draft + first-response draft queued; the latter's "
              "stored analysis carries mode first_response (UI visibility chip)",
              len(recs) == 2 and len(fr) == 1
              and fr[0]["analysis"].get("mode") == "first_response",
              str([(x.get("mode"), (x.get("analysis") or {}).get("mode"))
                   for x in recs]))

        def _fake_process_empty(case_ctx: CaseContext) -> CaseContext:
            case_ctx = _fake_process(case_ctx)
            case_ctx.subject = case_ctx.description = ""
            return case_ctx
        cis.CaseService.process_case = staticmethod(_fake_process_empty)
        store14b = HandsfreeStore(tmp / "handsfree_s14b")
        orch._analyze_and_enqueue(store14b, "01234567")
        modes = [d.get("mode") for d in store14b.list_drafts(include_closed=True)]
        check("S14.b failed case fetch queues NO customer-facing first response",
              modes == ["error"], str(modes))

        # Ball with the customer: no analysis, no request, no checklist —
        # one private reviewer note only.
        def _fake_process_asked(case_ctx: CaseContext) -> CaseContext:
            case_ctx = _fake_process(case_ctx)
            case_ctx.comments = list(case_ctx.comments) + [
                [datetime(2026, 6, 21, 10, 0), "Agent",
                 "Could you please upload a WRT log that covers the failure?"]]
            return case_ctx
        cis.CaseService.process_case = staticmethod(_fake_process_asked)
        waiting_reply = json.loads(reader_reply)
        waiting_reply.update({
            "action_owner": "customer",
            "next_action": "Customer to upload the WRT log Intel asked for in comment #3"})
        fake_llm.chat_text = lambda messages, system_content=None: json.dumps(waiting_reply)
        store14c = HandsfreeStore(tmp / "handsfree_s14c")
        orch._analyze_and_enqueue(store14c, "01234567")
        drafts = [store14c.get(d["draft_id"])
                  for d in store14c.list_drafts(include_closed=True)]
        a14 = drafts[0]["analysis"] if drafts else {}
        check("S14.c customer owns the action (Intel asked) -> waiting_customer note only, "
              "pipeline stopped before pick_zip",
              [d["mode"] for d in drafts] == ["waiting_customer"]
              and "pick_zip" not in [s["name"] for s in a14.get("stages", [])]
              and "Waiting on the customer" in drafts[0]["draft_plain"]
              and "Customer to upload the WRT log" in drafts[0]["draft_plain"]
              and drafts[0]["confidence"] is None,
              str([(d["mode"], d["draft_plain"][:80]) for d in drafts]))
        # Intel spoke but asked for nothing (internal status note only, as in
        # _fake_process): the reader's "customer owns" is not trusted.
        cis.CaseService.process_case = staticmethod(_fake_process)
        store14c2 = HandsfreeStore(tmp / "handsfree_s14c2")
        rec14c2 = orch._analyze_and_enqueue(store14c2, "01234567")
        from .case_reader import intel_has_asked as iha
        check("S14.c2 an Intel status comment is not an ask: owner corrected to Intel, "
              "analysis runs; the ask heuristic itself",
              rec14c2["mode"] != "waiting_customer"
              and rec14c2["analysis"]["action_owner"] == "intel"
              and iha([["t", "Agent", "We are investigating with the driver team."]]) is False
              and iha([["t", "Agent", "Please share the WRT logs."]]) is True
              and iha([["t", "FAE", "When did it last happen?"]]) is True
              and iha([["t", "Partner", "Could you fix it please?"]]) is False,
              f"mode={rec14c2['mode']} owner={rec14c2['analysis']['action_owner']}")

        # Reader says "customer to provide logs" although NO Intel comment
        # ever asked (only customer rows + our own AI note): Intel owns the
        # next step, the pipeline continues and requests as usual.
        def _fake_process_unasked(case_ctx: CaseContext) -> CaseContext:
            case_ctx = _fake_process(case_ctx)
            case_ctx.comments = [
                [datetime(2026, 6, 19, 9, 0), "Partner", "Initial report: disconnects."],
                [datetime(2026, 6, 20, 9, 0), "Agent",
                 "[AI-Avatar preliminary analysis] Automated triage ..."],
            ]
            case_ctx.attachment_list = []
            return case_ctx
        cis.CaseService.process_case = staticmethod(_fake_process_unasked)
        store14f = HandsfreeStore(tmp / "handsfree_s14f")
        rec14f = orch._analyze_and_enqueue(store14f, "01234567")
        cis.CaseService.process_case = staticmethod(_fake_process)
        check("S14.e 'customer owns' without any Intel ask -> owner corrected to Intel, "
              "request-logs reply drafted",
              rec14f["mode"] == "request_logs"
              and rec14f["analysis"]["action_owner"] == "intel",
              f"mode={rec14f['mode']} owner={rec14f['analysis']['action_owner']}")
        fake_llm.chat_text = lambda messages, system_content=None: reader_reply

        # Later round (first response already sent on the case): no second
        # overview draft, and a request reply carries only its targeted ask.
        store14d = HandsfreeStore(tmp / "handsfree_s14d")
        store14d.mark_posted("01234567", comment_id="CMT-FR", first_response=True)
        orch._analyze_and_enqueue(store14d, "01234567")
        modes14d = [d.get("mode") for d in store14d.list_drafts(include_closed=True)]
        st14d = (orch.get_run_state().get("case_details") or {}).get("01234567", {})
        st14d_copy_ok = True
        if st14d:
            st14d["subject"] = "mutated"
            st14d_copy_ok = (orch.get_run_state()["case_details"]["01234567"].get("subject")
                             != "mutated")
        rec14_live = (store14d.get(store14d.list_drafts(include_closed=True)[0]["draft_id"]) or {}).get("live") or {}
        check("S14.live4 the run's live history is saved with the draft (case_details + "
              "the case's stage events) for replay from the review queue",
              rec14_live.get("case_details", {}).get("01234567", {}).get("finished") is True
              and rec14_live.get("events") and all(e["message"].startswith("[01234567]") for e in rec14_live["events"])
              and rec14_live.get("saved_at"),
              f"keys={sorted(rec14_live)} n_events={len(rec14_live.get('events') or [])}")
        cd14 = st14d.get("checklist_decision") or {}
        check("S14.live3 checklist decision pushed after the fill: required logs with "
              "provided/missing + how they were filled, the asks",
              cd14.get("domain") == "Connectivity" and isinstance(cd14.get("required_logs"), list)
              and cd14["required_logs"] and all("provided" in e and "source" in e for e in cd14["required_logs"])
              and any(e["provided"] and e["source"] == "pipeline" for e in cd14["required_logs"])
              and isinstance(cd14.get("asks"), list) and st14d.get("checklist_summary"),
              f"cd={cd14}")
        check("S14.live run state per case: queued draft id, the agent inputs, finished "
              "outcome; get_run_state hands out a copy",
              bool(st14d.get("draft_id")) and st14d.get("finished") is True
              and (st14d.get("sent_for_analysis") or [{}])[0].get("description")
              and st14d.get("mode") == "full" and st14d_copy_ok,
              f"keys={sorted(st14d)} draft={st14d.get('draft_id')} copy_ok={st14d_copy_ok}")
        cis.CaseService.process_case = staticmethod(_fake_process_no_logs)
        store14e = HandsfreeStore(tmp / "handsfree_s14e")
        store14e.mark_posted("01234567", comment_id="CMT-FR", first_response=True)
        # A single-case run has no owner from a scan: the orchestrator asks
        # IPS for it, so the draft lands in that owner's own-cases view.
        orig_case_owner = orch._case_owner
        owner_asked: list = []
        orch._case_owner = lambda cid: owner_asked.append(cid) or "Owner From IPS"
        try:
            rec14e = orch._analyze_and_enqueue(store14e, "01234567")
        finally:
            orch._case_owner = orig_case_owner
        check("S14.e2 a single-case run records the case owner IPS reports (asked by case id)",
              rec14e is not None and rec14e.get("owner_name") == "Owner From IPS"
              and owner_asked and owner_asked[0] == rec14e.get("case_id")
              and store14e.list_drafts(owner_name="owner from ips"),
              f"owner={rec14e and rec14e.get('owner_name')!r} asked={owner_asked} id={rec14e and rec14e.get('case_id')!r}")
        cis.CaseService.process_case = staticmethod(_fake_process)
        # Trial mode (Settings) + the list-view source of the automatic
        # analysis: already-analyzed cases are skipped, closed cases run only
        # in trial mode, trial drafts are flagged and Approve refuses them.
        from .ips_client import CaseRef
        store14g = HandsfreeStore(tmp / "handsfree_s14g")
        store14g.save_config({"trial_mode": True, "post_backend": "rest"})
        store14g.mark_analyzed("00000001", "old-draft")   # already analyzed -> skipped

        class _ListViewIps:
            def list_view_cases(self, name, limit=200):
                if name == "Second_view":
                    return [CaseRef("00000003", "500C", "open one (also in view 1)",
                                    status="Open", closed=False),
                            CaseRef("00000009", "500D", "only in the second view",
                                    status="Open", closed=False)]
                assert name == "Handsfree_replier_test_case"
                return [CaseRef("00000001", "500A", "done before", status="Closed"),
                        CaseRef("01234567", "500B", "historical closed case", status="Closed"),
                        CaseRef("00000003", "500C", "open one", status="Open", closed=False)]

            def find_customer_updates(self, case_ids, since_iso):
                return {}
        orig_ips_cls = orch.IpsClient
        orch.IpsClient = _ListViewIps
        try:
            summary = orch.run_auto_scan(store14g, "", max_cases=1, trigger="manual",
                                         list_view="Handsfree_replier_test_case")
            store14h = HandsfreeStore(tmp / "handsfree_s14h")      # trial mode OFF
            store14h.save_config({"trial_mode": False, "post_backend": "rest"})
            summary_h = orch.run_auto_scan(store14h, "", max_cases=5, trigger="manual",
                                           list_view="Handsfree_replier_test_case")
            store14m = HandsfreeStore(tmp / "handsfree_s14m")      # two views, union
            store14m.save_config({"trial_mode": False, "post_backend": "rest"})
            summary_m = orch.run_auto_scan(
                store14m, "", max_cases=5, trigger="manual",
                list_views=["Handsfree_replier_test_case", "Second_view"])
            details_m = sorted((orch.get_run_state().get("case_details") or {}).keys())
        finally:
            orch.IpsClient = orig_ips_cls
        trial_drafts = store14g.list_drafts(include_closed=True)
        res_trial = None
        if trial_drafts:
            orig_store_fn, orig_identity = orch._store, orch._signed_in_identity
            orch._store = lambda: store14g
            orch._signed_in_identity = lambda: {"user_id": "005SMOKE", "name": "Smoke Reviewer",
                                                "email": "", "username": ""}
            try:
                res_trial = orch.approve_and_post(trial_drafts[0]["draft_id"])
            finally:
                orch._store = orig_store_fn
                orch._signed_in_identity = orig_identity
        check("S14.f list-view round in trial mode: analyzed case skipped, next (closed) "
              "case run, drafts flagged TRIAL, Approve refuses to post them",
              summary["ok"] and [c["case_nbr"] for c in summary["cases"]] == ["01234567"]
              and trial_drafts and all(d.get("trial") for d in trial_drafts)
              and res_trial is not None and res_trial["ok"] is False
              and "trial draft" in res_trial["error"],
              f"summary={summary} trial={[d.get('trial') for d in trial_drafts]} res={res_trial}")
        # A case whose fetch fails mid-round: the error draft is queued for the
        # reviewer, but the round counts it as failed, keeps the scan window,
        # leaves the ledger alone and does not consume the customer update.
        class _ListViewIpsUpd(_ListViewIps):
            def find_customer_updates(self, case_ids, since_iso):
                return {"500C": "2026-10-05T12:00:00.000+0000"}
        store14i = HandsfreeStore(tmp / "handsfree_s14i")
        store14i.save_config({"trial_mode": False, "post_backend": "rest"})
        store14i.mark_analyzed("00000001", "old-draft")
        cis.CaseService.process_case = staticmethod(_fake_process_empty)
        orch.IpsClient = _ListViewIpsUpd
        started14 = []
        try:
            summary_i = orch.run_auto_scan(store14i, "", max_cases=5, trigger="manual",
                                           list_view="Handsfree_replier_test_case",
                                           on_start=lambda: started14.append(True))
        finally:
            orch.IpsClient = orig_ips_cls
            cis.CaseService.process_case = staticmethod(_fake_process)
        cfg_i = store14i.load_config()
        check("S14.g a failed case fetch is a FAILED round entry: error draft queued, "
              "ledger untouched, update unconsumed, scan window kept, on_start fired",
              summary_i["ok"] and [c["mode"] for c in summary_i["cases"]] == ["error"]
              and [d["mode"] for d in store14i.list_drafts(include_closed=True)] == ["error"]
              and not store14i.is_processed("00000003")
              and store14i.ledger_entry("00000003").get("last_customer_update") is None
              and "window kept" in (cfg_i.get("auto_last_result") or "")
              and "listview:handsfree_replier_test_case"
              not in (cfg_i.get("auto_last_scan_by_owner") or {})
              and started14 == [True],
              f"summary={summary_i} cfg={cfg_i.get('auto_last_result')!r} "
              f"ledger={store14i.ledger_entry('00000003')}")
        check("S14.f2 trial mode off: closed list-view cases skipped, the open one runs, "
              "drafts not flagged; scan stamp kept per list view",
              summary_h["ok"] and [c["case_nbr"] for c in summary_h["cases"]] == ["00000003"]
              and not any(d.get("trial") for d in store14h.list_drafts(include_closed=True))
              and "listview:handsfree_replier_test_case"
              in (store14h.load_config().get("auto_last_scan_by_owner") or {})
              and not any(store14d.get(d['draft_id']).get('trial')
                          for d in store14d.list_drafts(include_closed=True)),
              f"summary={summary_h} cfg={store14h.load_config().get('auto_last_scan_by_owner')}")

        # --- S18 BSOD branch (Charles, 2026-10-14): a BSOD case with a dump
        # upload needs no issue time; the dumps go to the BSOD share and
        # PotatoFarm is triggered (Avatar's BSOD flow); later rounds don't
        # resubmit; without a dump the public reply asks for the dump.
        print("[S18] BSOD branch")
        from . import bsod as bsod_mod
        bsod_dir = tmp / "bsod_share"
        bsod_calls: list = []
        orig_bsod = (bsod_mod.share_case_dir, bsod_mod.submit_to_potatofarm,
                     bsod_mod._submissions_path, bsod_mod.handsfree_config)
        bsod_cfg = {"trial_mode": False, "bsod_email": "owner@example.com",
                    "bsod_result_wait_min": 0.05}
        bsod_mod.share_case_dir = lambda case_nbr, backend_id="": str(bsod_dir / case_nbr)
        parser_json = {"01032878_sku6_0x1a_memory": {
            "bugcheck_str": "MEMORY_MANAGEMENT (1a)     # Any other values ...",
            "bugcheck_code": "1a", "failure_bucket_id": "0x1a_41792_nt!MiDecommitHandlePageFileFormatPte",
            "process_name": "backgroundTaskHost.exe", "dump_time": "2026-09-29T03:09:40.640000",
            "version": "24.70.0.3", "drv_branch": "core24.70", "is_verifier_enabled": False,
            "num_of_fw_asserts": 0, "is_driver_on_stack": False,
            "is_wifi_driver_fault": False, "is_bt_driver_fault": False,
            "stacks": [{"2": ["nt!KeBugCheckEx", "nt!MiDecommitHandlePageFileFormatPte", "nt!MiDeleteVa"]}],
            "jira_id": None, "jira_decision": "no_update:JIRA filing not requested"}}

        def _fake_submit(dumps_path, email, key=None):
            bsod_calls.append((dumps_path, email))
            Path(dumps_path).mkdir(parents=True, exist_ok=True)
            (Path(dumps_path) / bsod_mod.RESULT_JSON).write_text(json.dumps(parser_json), encoding="utf-8")
            (Path(dumps_path) / bsod_mod.RESULT_HTML).write_text("<html>report</html>", encoding="utf-8")
            return {"ok": True, "status": 200, "detail": "queued"}
        bsod_mod.submit_to_potatofarm = _fake_submit
        bsod_mod._submissions_path = lambda: tmp / "bsod_submissions.json"
        bsod_mod.handsfree_config = lambda: dict(bsod_cfg)

        def _fake_process_bsod(case_ctx: CaseContext) -> CaseContext:
            case_ctx = _fake_process(case_ctx)
            case_ctx.subject = "[lenovo_wcl][WCL] BSOD related to WLAN driver"
            case_ctx.description = ("Set Long Run on the test machine, and then "
                                    "encounter BSOD with multiple error code.")
            case_ctx.attachment_list = [
                ["SKU15_0x9F_MEMORY.zip", "https://esft/x?FileName=SKU15_0x9F_MEMORY.zip",
                 ["10/13/2026 11:44", "SKU15_0x9F_MEMORY.zip"]],
                ["SKU6_0x1A_MEMORY.zip", "https://esft/x?FileName=SKU6_0x1A_MEMORY.zip",
                 ["10/13/2026 09:01", "SKU6_0x1A_MEMORY.zip"]],
            ]
            case_ctx.env_detail = {"Computer Model": "WCL",
                                   "Assert Error (32-bit value or NA)": "0x139/0x1E/0x9F/0x1A"}
            return case_ctx

        def _fake_process_bsod_nodump(case_ctx: CaseContext) -> CaseContext:
            case_ctx = _fake_process_bsod(case_ctx)
            case_ctx.attachment_list = []
            return case_ctx

        bsod_reader = json.dumps({
            "clean_description": "BSOD during a long-run test, several bug checks.",
            "issue_times": [], "issue_time_source": "",
            "attachment_name": "SKU15_0x9F_MEMORY.zip", "attachment_reason": "memory dump",
            "reasoning": "", "issue_domain": "BSOD", "issue_subcategory": "",
            "missing_info": [{"item": "issue_time", "reason": "no failure time stated"}],
            "action_owner": "Intel", "next_action": "Intel to analyze the dumps",
        })

        def _fake_dload_bsod(att_list, download_path, socketio):
            Path(download_path).mkdir(parents=True, exist_ok=True)
            for it in att_list:
                target = Path(download_path) / it[0]
                target.write_bytes(b"dump")
                yield [str(target), it[0], False]

        cis.CaseService.process_case = staticmethod(_fake_process_bsod)
        fake_llm.chat_text = lambda messages, system_content=None: bsod_reader
        adl.run_dload_threads = _fake_dload_bsod
        try:
            a_b1 = r.analyze_case("01032878")
            a_b2 = r.analyze_case("01032878")          # same uploads -> no resubmit
            bsod_cfg["trial_mode"] = True
            bsod_mod._submissions_path = lambda: tmp / "bsod_submissions_trial.json"
            a_b3 = r.analyze_case("01032878")          # trial: PotatoFarm not triggered
            bsod_cfg["trial_mode"] = False
            cis.CaseService.process_case = staticmethod(_fake_process_bsod_nodump)
            a_b4 = r.analyze_case("01032878")          # no dump -> ask for it
            # One upload's download stalls every round: retried once, then
            # given up (never a multi-hour fetch on every round).
            bsod_calls_b4 = list(bsod_calls)            # calls so far (S18.a-c)
            cis.CaseService.process_case = staticmethod(_fake_process_bsod)
            bsod_mod._submissions_path = lambda: tmp / "bsod_submissions_fail.json"
            def _fake_dload_sku15_stalls(att_list, download_path, socketio):
                ok_items = [it for it in att_list if "SKU15" not in str(it[0])]
                for item in _fake_dload_bsod(ok_items, download_path, socketio):
                    yield item
            adl.run_dload_threads = _fake_dload_sku15_stalls
            a_b5 = r.analyze_case("01032878")          # SKU6 submitted, SKU15 fails (1)
            a_b6 = r.analyze_case("01032878")          # SKU15 retried, fails (2)
            a_b7 = r.analyze_case("01032878")          # given up -> pending, no fetch
        finally:
            (bsod_mod.share_case_dir, bsod_mod.submit_to_potatofarm,
             bsod_mod._submissions_path, bsod_mod.handsfree_config) = orig_bsod
            cis.CaseService.process_case = staticmethod(_fake_process)
            fake_llm.chat_text = lambda messages, system_content=None: reader_reply
            adl.run_dload_threads = _fake_dload
        from .composer import compose as compose_fn
        from .checklist import build_fills as build_fills_fn
        d_b1, d_b2, d_b3, d_b4 = (compose_fn(a)["plain"] for a in (a_b1, a_b2, a_b3, a_b4))
        fills_b1 = build_fills_fn(a_b1)                  # llm=None -> deterministic only
        dump_item = next((e for e in fills_b1.get("required_log", [])
                          if "Kernel Dump" in e["item"]), {})
        dump_item_b4 = next((e for e in build_fills_fn(a_b4).get("required_log", [])
                             if "Kernel Dump" in e["item"]), {})
        check("S18.a BSOD + dump upload: issue time not required, dumps downloaded to the "
              "BSOD share and submitted to PotatoFarm once, private draft says so",
              a_b1.mode == "bsod_submitted" and a_b1.bsod_case and a_b1.ok
              and not any(m.get("item") == "issue_time" for m in a_b1.missing_info)
              and a_b1.bsod.get("submitted")
              and a_b1.bsod["dumps"] == ["SKU15_0x9F_MEMORY.zip", "SKU6_0x1A_MEMORY.zip"]
              and bsod_calls_b4 == [(str(bsod_dir / "01032878"), "owner@example.com")]
              and (bsod_dir / "01032878" / "SKU6_0x1A_MEMORY.zip").exists()
              and [st.name for st in a_b1.stages if st.name.startswith("bsod")]
              == ["bsod_dumps", "bsod_download", "bsod_submit", "bsod_result"]
              and "PotatoFarm" in d_b1 and "owner@example.com" in d_b1 and "0x139" in d_b1,
              f"mode={a_b1.mode} bsod={a_b1.bsod} calls={bsod_calls} "
              f"missing={a_b1.missing_info} stages={[st.name for st in a_b1.stages]}")
        check("S18.b second round, same uploads: no resubmission (bsod_pending)",
              a_b2.mode == "bsod_pending" and len(bsod_calls_b4) == 1
              and "already submitted" in d_b2.lower(),
              f"mode={a_b2.mode} calls={len(bsod_calls)} draft={d_b2[:300]}")
        check("S18.c trial mode: dumps downloaded but PotatoFarm not triggered",
              a_b3.mode == "bsod_submitted" and not a_b3.bsod.get("submitted")
              and "trial" in a_b3.bsod.get("skipped", "") and len(bsod_calls_b4) == 1
              and "NOT triggered" in d_b3,
              f"mode={a_b3.mode} bsod={a_b3.bsod} calls={len(bsod_calls)}")
        check("S18.d BSOD without a dump: public reply asks for the memory dump (+ the "
              "issue-time ask), not for WRT logs; the dump item is not ticked",
              a_b4.mode == "request_logs" and a_b4.log_request_reason == "no_dump"
              and "MEMORY.DMP" in d_b4 and "exact date and time" in d_b4
              and "Intel wireless driver WRT logs" not in d_b4
              and not dump_item_b4.get("provided"),
              f"mode={a_b4.mode} reason={a_b4.log_request_reason} draft={d_b4[:400]}")
        time_item = next((e for e in fills_b1.get("general_info", [])
                          if "reproduction time" in e["item"].lower()), {})
        check("S18.e first-response checklist ticks 'Kernel Dump' with the upload names and "
              "does not ask for the reproduction time",
              dump_item.get("provided") and "SKU15_0x9F_MEMORY.zip" in dump_item.get("value", "")
              and time_item.get("provided") and "not required" in time_item.get("value", ""),
              f"{dump_item} {time_item}")
        res_b1 = a_b1.bsod.get("results") or []
        check("S18.f PotatoFarm result picked up from the dump folder: bugcheck, bucket, "
              "driver verdict and assessment in the private draft (also on the pending round)",
              len(res_b1) == 1 and res_b1[0]["failure_bucket_id"].startswith("0x1a_41792")
              and res_b1[0]["wifi_fault"] is False
              and [st.name for st in a_b1.stages if st.name.startswith("bsod")]
              == ["bsod_dumps", "bsod_download", "bsod_submit", "bsod_result"]
              and "0x1a_41792_nt!MiDecommitHandlePageFileFormatPte" in d_b1
              and "not at fault" in d_b1 and "not a WLAN driver issue" in d_b1
              and "0x1a_41792" in d_b2 and (a_b2.bsod.get("results") or []),
              f"results={res_b1} stages={[st.name for st in a_b1.stages]} d_b2={d_b2[:400]}")
        d_b7 = compose_fn(a_b7)["plain"]
        check("S18.g stalled download: the other upload is submitted, the stalled one is "
              "retried once, then given up (named in the draft, never fetched again)",
              a_b5.mode == "bsod_submitted" and a_b5.bsod.get("submitted")
              and a_b6.mode == "bsod_pending" and a_b6.bsod.get("failed") == ["SKU15_0x9F_MEMORY.zip"]
              and a_b7.mode == "bsod_pending"
              and a_b7.bsod.get("failed") == ["SKU15_0x9F_MEMORY.zip"]
              and "GAVE UP" in d_b7 and len(bsod_calls) == 2,
              f"modes={a_b5.mode}/{a_b6.mode}/{a_b7.mode} failed6={a_b6.bsod.get('failed')} "
              f"failed7={a_b7.bsod.get('failed')} calls={len(bsod_calls)} draft={d_b7[:300]}")

        check("S14.f3 two list views: union of their cases, each case once, in list order; "
              "scan stamp keyed by both names",
              summary_m["ok"] and [c["case_nbr"] for c in summary_m["cases"]] == ["00000003", "00000009"]
              and "Second_view" in summary_m["source"]
              and "listview:handsfree_replier_test_case|second_view"
              in (store14m.load_config().get("auto_last_scan_by_owner") or {})
              and details_m == ["00000003", "00000009"],      # live panel reset per run
              f"summary={summary_m} cfg={store14m.load_config().get('auto_last_scan_by_owner')} "
              f"details={details_m}")

        check("S14.d later round: analysis only (no second overview); request reply "
              "asks for the logs without the checklist",
              modes14d == ["full"]
              and rec14e["mode"] == "request_logs"
              and "WRT logs" in rec14e["draft_plain"]
              and "=== General Info ===" not in rec14e["draft_plain"]
              and CHECKLIST_TAG in rec14e["draft_plain"],
              f"{modes14d} {rec14e['mode']} {rec14e['draft_plain'][-200:]}")
    finally:
        ipc_mod.IpsClient = orig_ipc
        cis.CaseService.process_case = orig_process
        cis.CaseService.load_case_summary_prompt = orig_prompt
        adl.run_dload_threads = orig_dload
        adc.process_single_zip = orig_zip
        bt_parser.bt_decode_via_cli = orig_bt_decode
        ita.organize_issue_context = orig_org
        app_config.llm_helper = orig_llm
        app_config.log_chatbot_agent = orig_agent
        if not orig_files_dir:
            app_config.avatarfiles_dir = orig_files_dir


# ---------------------------------------------------------------- S5
def smoke_orchestrator(tmp: Path) -> None:
    print("[S5] Approve/post policy (first response once per case; analyses per round)")
    from . import orchestrator as orch
    from .composer import AI_MARKER, CHECKLIST_TAG
    from .ips_client import PostResult
    from .queue import HandsfreeStore

    store = HandsfreeStore(tmp / "handsfree_s5")
    store.save_config({"post_backend": "rest"})   # never the Selenium fallback

    class _Ips:
        """Configurable fake: comments on the case + post outcome."""
        FIELD_RICH_BODY = "Core_IPS_Rich_Comment__c"
        comments: list = []
        scan_raises = False
        post_ok = False
        posted: list = []

        def get_case_comments(self, case_id):
            if self.scan_raises:
                raise RuntimeError("IPS unreachable")
            return list(self.comments)

        def post_comment(self, case_id, rich_body, **kw):
            self.posted.append((case_id, kw.get("private")))
            if self.post_ok:
                return PostResult(ok=True, backend="rest",
                                  comment_id=f"CMT{len(self.posted)}")
            return PostResult(ok=False, backend="rest", error="rest down")

        def comment_author(self, comment_id):
            return {"user_id": "005SMOKE", "name": "Smoke Reviewer"}

    def enqueue(case, mode, body="body", later_round=False):
        tag = ("\n" + CHECKLIST_TAG) if mode in orch._CHECKLIST_MODES else ""
        return store.enqueue(case_nbr=case, case_id="500" + case, subject="s",
                             draft_plain=AI_MARKER + tag + "\n\n" + body,
                             draft_html="<p>b</p>", confidence=None, mode=mode,
                             # what the runner stores: was the overview already
                             # sent when this draft was composed?
                             analysis={"first_response_done": later_round})["draft_id"]

    orig_store_fn, orig_ips = orch._store, orch.IpsClient
    orig_identity = orch._signed_in_identity
    try:
        orch._store = lambda: store
        orch.IpsClient = _Ips

        # No IPS sign-in on this PC: nothing is posted, the draft stays.
        def _no_session():
            raise RuntimeError("Salesforce SSO did not complete")
        orch._signed_in_identity = _no_session
        d0 = enqueue("09999990", "full")
        res0 = orch.approve_and_post(d0)
        check("S5.0 approve refused without an IPS sign-in; draft untouched, nothing posted",
              res0["ok"] is False and "not signed in" in res0["error"]
              and store.get(d0)["status"] == "pending_review" and not _Ips.posted, str(res0))
        orch._signed_in_identity = lambda: {"user_id": "005SMOKE", "name": "Smoke Reviewer",
                                            "email": "smoke@intel.com", "username": "smoke"}
        check("S5.0b claim holder names the signed-in reviewer",
              orch._reviewer_id({"name": "Smoke Reviewer"}).startswith("Smoke Reviewer ("))

        # An existing AI analysis comment no longer blocks another analysis
        # (one per round), and a scan failure never aborts a post.
        _Ips.comments = [{"Id": "C1", "Core_IPS_Rich_Comment__c": AI_MARKER + " earlier"}]
        d1 = enqueue("09999999", "full")
        res = orch.approve_and_post(d1)
        check("S5.a analysis draft posts even though an AI analysis exists on the case",
              "already" not in (res.get("error") or "")
              and store.get(d1)["status"] == "post_failed", str(res))
        _Ips.scan_raises = True
        d2 = enqueue("09999998", "first_response")
        res = orch.approve_and_post(d2, visibility="external")
        check("S5.b first response: a failed IPS scan is skipped, post attempted",
              "scan failed" not in (res.get("error") or "")
              and store.get(d2)["status"] == "post_failed", str(res))
        _Ips.scan_raises = False

        # First response once per case, tracked in OUR ledger (reviewers edit
        # the text, so body markers are not what decides).
        _Ips.comments, _Ips.post_ok = [], True
        # Visibility is the reviewer's pick on each post. No pick = internal,
        # whatever the draft's mode: a request draft posted that way stays
        # inside Intel and does not count as the customer's first response.
        d3i = enqueue("09999997", "request_logs")
        res_i = orch.approve_and_post(d3i)
        rec3i = store.get(d3i)
        check("S5.c0 no visibility given: a request draft posts PRIVATE, recorded as internal, "
              "first response NOT recorded",
              res_i["ok"] and _Ips.posted[-1][1] is True
              and rec3i["post_result"]["visibility"] == "internal"
              and rec3i["status"] == "posted"
              and not store.first_response_posted("09999997"), str(res_i))
        posted_before = len(_Ips.posted)
        d3b = enqueue("09999997", "request_logs")
        res_b = orch.approve_and_post(d3b, visibility="everyone")
        check("S5.c1 unknown visibility refused before anything happens (nothing posted, draft untouched)",
              res_b["ok"] is False and "visibility" in res_b["error"]
              and len(_Ips.posted) == posted_before
              and store.get(d3b)["status"] == "pending_review", str(res_b))
        d3 = enqueue("09999997", "request_logs")
        res = orch.approve_and_post(d3, visibility="External")
        check("S5.c first-round request reply posted EXTERNAL is public and records the first response "
              "(case-insensitive value)",
              res["ok"] and _Ips.posted[-1][1] is False
              and store.get(d3)["post_result"]["visibility"] == "external"
              and store.first_response_posted("09999997"), str(res))
        fr_comment_id = (res.get("result") or {}).get("comment_id")
        rec3 = store.get(d3)
        check("S5.c2 a posted draft records who posted it: draft, post_result (+ IPS "
              "author read-back) and ledger",
              rec3.get("posted_by") == "Smoke Reviewer"
              and (rec3["post_result"].get("posted_by") or {}).get("name") == "Smoke Reviewer"
              and (rec3["post_result"].get("author") or {}).get("name") == "Smoke Reviewer"
              and store.ledger_entry("09999997").get("posted_by") == "Smoke Reviewer",
              str(rec3.get("post_result")))
        d4 = enqueue("09999997", "first_response")
        res = orch.approve_and_post(d4, visibility="external")
        check("S5.d second first-response on the case refused + marked posted",
              res["ok"] is False and "once" in res["error"]
              and store.get(d4)["status"] == "posted", str(res))
        check("S5.d2 second approve of a posted draft refused",
              orch.approve_and_post(d4)["error"] == "draft already posted")
        d5 = enqueue("09999997", "request_logs", later_round=True)
        res = orch.approve_and_post(d5, visibility="external")
        d6 = enqueue("09999997", "full")
        res6 = orch.approve_and_post(d6)
        entry = store.ledger_entry("09999997")
        check("S5.e later-round request reply and analysis both post; first-response "
              "record keeps the ORIGINAL comment id",
              res["ok"] and res6["ok"] and _Ips.posted[-2][1] is False and _Ips.posted[-1][1] is True
              and entry["first_response_comment_id"] == fr_comment_id
              and entry["comment_id"] != fr_comment_id, str(entry))
        # The reviewer may also send an ANALYSIS to the customer: external
        # posts it public; it is never a "first response" (not a request).
        d6x = enqueue("09999979", "full")
        res6x = orch.approve_and_post(d6x, visibility="external")
        check("S5.e1 an analysis draft posted EXTERNAL goes public and leaves the first-response record alone",
              res6x["ok"] and _Ips.posted[-1][1] is False
              and store.get(d6x)["post_result"]["visibility"] == "external"
              and not store.first_response_posted("09999979"), str(res6x))
        # A FIRST-round request draft left over from before the overview went
        # out (manual rerun queued two) embeds the overview again: refused.
        posted_before = len(_Ips.posted)
        d5s = enqueue("09999997", "request_info")
        res_stale = orch.approve_and_post(d5s, visibility="external")
        check("S5.e2 stale first-round request draft refused once the overview is out "
              "(rejected, nothing posted)",
              res_stale["ok"] is False and "embeds it again" in res_stale["error"]
              and store.get(d5s)["status"] == "rejected"
              and len(_Ips.posted) == posted_before, str(res_stale))
        # The same stale draft INTERNAL: the customer never sees it, so the
        # once-per-case rule has nothing to protect — it posts, private.
        d5s_i = enqueue("09999997", "request_info")
        res_stale_i = orch.approve_and_post(d5s_i)
        check("S5.e2b the same first-round request draft posted internal is allowed (private, "
              "first-response record unchanged)",
              res_stale_i["ok"] and _Ips.posted[-1][1] is True
              and store.ledger_entry("09999997")["first_response_comment_id"] == fr_comment_id,
              str(res_stale_i))

        # Reviewer edited both markers away: restored before the post.
        _Ips.post_ok = False
        d7 = enqueue("09999996", "request_logs")
        orch.approve_and_post(d7, edited_plain="Hello,\nplease send logs")
        edited = store.get(d7)["draft_plain"]
        check("S5.f edited checklist draft gets AI marker + family tag restored",
              edited.startswith(AI_MARKER + "\n" + CHECKLIST_TAG + "\n\nHello,"),
              edited[:120])

        # Overview posted before the ledger tracked it (legacy, untagged
        # request template): the IPS scan backfills the ledger once.
        _Ips.comments = [{"Id": "C9", "Core_IPS_Rich_Comment__c":
                          "<p>" + AI_MARKER + "<br/><br/>Hello,<br/>To root-cause it "
                          "we need the Intel wireless driver WRT logs covering the "
                          "failure</p>"}]
        d8 = enqueue("09999995", "first_response")
        res = orch.approve_and_post(d8, visibility="external")
        check("S5.g legacy request comment counts as the first response (ledger backfilled)",
              res["ok"] is False and "once" in res["error"]
              and store.ledger_entry("09999995").get("first_response_comment_id") == "C9",
              str(res))

        # Opt-in auto-post: only a case's first reply that asks for missing
        # items posts unreviewed; every gate leaves the draft for review.
        from types import SimpleNamespace
        _Ips.comments, _Ips.post_ok = [], True
        missing = {"general_info": [{"item": "Driver version", "provided": False}]}
        complete = {"general_info": [{"item": "Driver version", "provided": True,
                                      "value": "23.60"}]}

        def auto(case, mode, *, done=False, fills=None, trial=False):
            rec = store.enqueue(case_nbr=case, case_id="500" + case, subject="s",
                                draft_plain=AI_MARKER + "\n" + CHECKLIST_TAG + "\n\nb",
                                draft_html="<p>b</p>", confidence=None, mode=mode,
                                analysis={"first_response_done": done}, trial=trial)
            before = len(_Ips.posted)
            orch._auto_post_first_response(
                store, rec, SimpleNamespace(first_response_done=done,
                                            checklist_fills=fills or missing))
            return store.get(rec["draft_id"]), len(_Ips.posted) - before

        rec, n = auto("09999990", "request_logs")
        check("S5.h auto-post off by default: first-round request left for review",
              n == 0 and rec["status"] == "pending_review", rec["status"])
        store.save_config({"auto_post_first_response": True, "dry_run": True})
        rec, n = auto("09999989", "request_logs")
        check("S5.h2 dry run blocks auto-post", n == 0 and rec["status"] == "pending_review")
        store.save_config({"dry_run": False})
        rec, n = auto("09999988", "request_info")
        check("S5.h3 no verified rest_field_map: auto-post skipped",
              n == 0 and rec["status"] == "pending_review")
        store.save_config({"rest_field_map": {"body_field": "Core_IPS_Rich_Comment__c"}})
        rec, n = auto("09999987", "request_logs")
        check("S5.h4 first-round request posts PUBLIC, marked auto_posted, first response recorded",
              n == 1 and _Ips.posted[-1][1] is False and rec["status"] == "posted"
              and rec.get("auto_posted") is True
              and store.first_response_posted("09999987"), str(rec.get("post_result")))
        rec, n = auto("09999986", "first_response")
        check("S5.h5 first_response with missing checklist items posts",
              n == 1 and rec["status"] == "posted" and rec.get("auto_posted") is True)
        skipped = [auto("09999985", "first_response", fills=complete),
                   auto("09999984", "request_logs", done=True),
                   auto("09999983", "full"),
                   auto("09999982", "triage_only"),
                   auto("09999981", "request_info", trial=True)]
        check("S5.h6 not auto-posted: nothing missing / later round / analysis / triage / trial",
              all(n == 0 and r["status"] == "pending_review" and not r.get("auto_posted")
                  for r, n in skipped),
              str([(r["mode"], r["status"], n) for r, n in skipped]))
        store.save_config({"auto_post_first_response": False, "rest_field_map": None})
    finally:
        orch._store = orig_store_fn
        orch.IpsClient = orig_ips
        orch._signed_in_identity = orig_identity

    # Typed case numbers go through the repository's one definition
    # (utils/check_ips_utils.normalise_ips) — UI manual run and the CLI alike.
    from . import cli as hf_cli
    bad = [orch.start_case_run(x) for x in ("abc", "123", "01234567x", "")]
    check("S5.i manual run refuses non-case-numbers (letters, too short, mixed, empty)",
          all(r["ok"] is False for r in bad)
          and all("not an IPS case number" in r["error"] for r in bad[:3])
          and bad[3]["error"] == "empty case number"
          and not orch._run_lock.locked(), str(bad))
    import io, contextlib
    with contextlib.redirect_stderr(io.StringIO()) as err:
        rc_bad = hf_cli.main(["--case-number", "abc", "--once"])
        rc_short = hf_cli.main(["--case-number", "12345", "--once"])
    check("S5.j CLI --case-number validated the same way (exit 2, nothing started)",
          rc_bad == 2 and rc_short == 2 and "not an IPS case number" in err.getvalue(),
          err.getvalue()[:200])
    from utils.check_ips_utils import normalise_ips
    check("S5.k normalise_ips pads a typed 7-digit number and strips edge punctuation",
          normalise_ips("1234567") == "01234567" and normalise_ips(" 01234567. ") == "01234567"
          and normalise_ips("abc") == "" and normalise_ips("20260923") == "")

    # Typed case numbers go through the repository's one definition
    # (utils/check_ips_utils.normalise_ips) — UI manual run and the CLI alike.
    from . import cli as hf_cli
    bad = [orch.start_case_run(x) for x in ("abc", "123", "01234567x", "")]
    check("S5.i manual run refuses non-case-numbers (letters, too short, mixed, empty)",
          all(r["ok"] is False for r in bad)
          and all("not an IPS case number" in r["error"] for r in bad[:3])
          and bad[3]["error"] == "empty case number"
          and not orch._run_lock.locked(), str(bad))
    import io, contextlib
    with contextlib.redirect_stderr(io.StringIO()) as err:
        rc_bad = hf_cli.main(["--case-number", "abc", "--once"])
        rc_short = hf_cli.main(["--case-number", "12345", "--once"])
    check("S5.j CLI --case-number validated the same way (exit 2, nothing started)",
          rc_bad == 2 and rc_short == 2 and "not an IPS case number" in err.getvalue(),
          err.getvalue()[:200])
    from utils.check_ips_utils import normalise_ips
    check("S5.k normalise_ips pads a typed 7-digit number and strips edge punctuation",
          normalise_ips("1234567") == "01234567" and normalise_ips(" 01234567. ") == "01234567"
          and normalise_ips("abc") == "" and normalise_ips("20260923") == "")


# ---------------------------------------------------------------- S16
def smoke_auto_scan(tmp: Path) -> None:
    print("[S16] Automatic analysis: work selection + schedule timing")
    from datetime import datetime, timezone
    from .ips_client import (CaseRef, build_open_cases_soql,
                             build_customer_updates_soql)
    from .orchestrator import select_auto_work, _parse_iso
    from .scheduler import next_run_after

    q = build_open_cases_soql("O'Neil")
    check("S16.a open-cases SOQL: owner escaped, open only, newest activity first",
          "Owner.Name = 'O\\'Neil'" in q and "IsClosed = false" in q
          and "ORDER BY LastModifiedDate DESC" in q, q)
    q2 = build_customer_updates_soql(["500A", "500B"], "2026-10-05T00:00:00Z")
    check("S16.b customer-updates SOQL: bounded by case ids, time and customer authors",
          "IN ('500A', '500B')" in q2 and "CreatedDate > 2026-10-05T00:00:00Z" in q2
          and "'Partner'" in q2 and "'Agent'" not in q2, q2)
    try:
        build_customer_updates_soql(["500A"], "yesterday")
        check("S16.c bad since rejected", False)
    except ValueError:
        check("S16.c bad since rejected", True)

    since = "2026-10-05T00:00:00Z"
    cases = [
        CaseRef("1", "500N", "new case", created="2026-10-05T08:00:00.000+0000"),
        CaseRef("2", "500U", "updated", created="2026-09-01T08:00:00.000+0000"),
        CaseRef("3", "500S", "same update again", created="2026-09-01T08:00:00.000+0000"),
        CaseRef("4", "500Q", "quiet old case", created="2026-09-01T08:00:00.000+0000"),
        CaseRef("5", "500O", "update older than our analysis", created="2026-09-01T08:00:00.000+0000"),
    ]
    updates = {"500U": "2026-10-05T12:00:00.000+0000",
               "500S": "2026-10-05T11:00:00.000+0000",
               "500O": "2026-10-05T09:00:00.000+0000"}
    ledger = {
        "2": {"analyzed_at": "2026-10-04T10:00:00+08:00"},
        "3": {"analyzed_at": "2026-10-05T20:00:00+08:00",
              "last_customer_update": "2026-10-05T11:00:00.000+0000"},
        "4": {"analyzed_at": "2026-09-02T10:00:00+08:00"},
        "5": {"analyzed_at": "2026-10-05T19:00:00+08:00"},   # = 11:00Z, after 09:00Z
    }
    work = select_auto_work(cases, updates, lambda n: ledger.get(n), since)
    check("S16.d work = customer update newer than our analysis + new case, newest first; "
          "repeated/old updates and quiet cases skipped",
          [(w[0].case_nbr, w[1].split(" at ")[0]) for w in work]
          == [("2", "customer update"), ("1", "new case")],
          str([(w[0].case_nbr, w[1]) for w in work]))
    check("S16.e ISO parsing: Salesforce +0000, Z, local offsets, garbage",
          _parse_iso("2026-10-05T10:07:12.000+0000") == _parse_iso("2026-10-05T10:07:12Z")
          and _parse_iso("2026-10-05T18:07:12+08:00") == _parse_iso("2026-10-05T10:07:12Z")
          and _parse_iso("nope") is None and _parse_iso("") is None)

    from .orchestrator import select_list_view_work
    lv_cases = [
        CaseRef("1", "500N", "never analyzed, open", status="Open"),
        CaseRef("2", "500U", "analyzed, customer replied", status="Closed", closed=False),
        CaseRef("3", "500Q", "analyzed, quiet", status="Open"),
        CaseRef("4", "500C", "never analyzed, closed", status="Closed"),
        CaseRef("5", "500X", "IsClosed wins over Status text", status="Open", closed=True),
    ]
    lv_ledger = {"2": {"analyzed_at": "2026-10-04T10:00:00+08:00"},
                 "3": {"analyzed_at": "2026-10-04T10:00:00+08:00"}}
    lv_work, lv_skipped = select_list_view_work(
        lv_cases, {"500U": "2026-10-05T12:00:00.000+0000"}, lambda n: lv_ledger.get(n),
        include_closed=False)
    lv_work_all, lv_skipped_all = select_list_view_work(
        lv_cases, {}, lambda n: lv_ledger.get(n), include_closed=True)
    check("S16.d2 list-view source: never-analyzed + customer-updated cases in list order, "
          "closed ones skipped unless trial mode (IsClosed beats the Status text)",
          [(w[0].case_nbr, w[1].split(" at ")[0]) for w in lv_work]
          == [("1", "not analyzed yet"), ("2", "customer update")] and lv_skipped == 2
          and [w[0].case_nbr for w in lv_work_all] == ["1", "4", "5"] and lv_skipped_all == 0,
          f"{[(w[0].case_nbr, w[1]) for w in lv_work]} skipped={lv_skipped} "
          f"all={[w[0].case_nbr for w in lv_work_all]}")
    from .scheduler import scan_target, _settings_updates
    check("S16.d3 scheduler target: owner by default, list views when chosen (legacy "
          "auto_list_view / trial_list_view honoured), errors when the chosen source is unconfigured",
          scan_target({"owner_name": "Charles"})
          == {"owner": "Charles", "owners": ["Charles"], "list_views": [], "list_view": "", "error": ""}
          and scan_target({"auto_source": "list_view", "trial_list_view": "Old_view"})["list_views"] == ["Old_view"]
          and scan_target({"auto_source": "list_view", "auto_list_view": "LV"})
          == {"owner": "", "owners": [], "list_views": ["LV"], "list_view": "LV", "error": ""}
          and scan_target({"auto_source": "list_view", "auto_list_views": ["A", "B"], "auto_list_view": "A"})["list_views"] == ["A", "B"]
          and scan_target({"auto_source": "list_view"})["error"]
          and scan_target({"auto_source": "owner"})["error"])
    check("S16.d4 list-view settings: stripped, deduped, capped at 3, legacy single name kept in sync",
          _settings_updates(list_views=[" A ", "B", "A", "", "C", "D"])
          == {"auto_list_views": ["A", "B", "C"], "auto_list_view": "A"}
          and _settings_updates(list_view="X") == {"auto_list_views": ["X"], "auto_list_view": "X"}
          and _settings_updates(list_views=[]) == {"auto_list_views": [], "auto_list_view": ""}
          and _settings_updates(max_cases=99) == {"auto_check_max_cases": 50},
          str(_settings_updates(list_views=[" A ", "B", "A", "", "C", "D"])))

    from .ips_client import IpsClient

    class _Resp:
        def __init__(self, body): self._body = body
        def raise_for_status(self): pass
        def json(self): return self._body

    class _ListViewsIps(IpsClient):
        pages = {
            "/services/data/v{ver}/sobjects/Case/listviews": {
                "listviews": [{"id": "00B1", "developerName": "My_cases", "label": "My cases"},
                              {"id": "", "developerName": "ghost", "label": "no id"}],
                "nextRecordsUrl": "/services/data/v{ver}/sobjects/Case/listviews?offset=2"},
            "/services/data/v{ver}/sobjects/Case/listviews?offset=2": {
                "listviews": [{"id": "00B2", "developerName": "Handsfree_replier_test_case"},
                              {"id": "00B3", "developerName": "All", "label": "All"}]},
        }
        def __init__(self):
            super().__init__()
            self.calls = []
        def _request(self, method, path, *, params=None, **kw):
            self.calls.append((method, path, params))
            return _Resp(self.pages[path])
    lv_ips = _ListViewsIps()
    views = lv_ips.list_case_views()
    lv_ips2 = _ListViewsIps()
    try:
        lv_ips2.list_view_cases("Nope")
        lv_err = ""
    except LookupError as e:
        lv_err = str(e)
    check("S16.g list_case_views: pages through listviews, label falls back to the developer "
          "name, id-less entries dropped; list_view_cases stops at the page holding the view "
          "and still raises 'list view not found'",
          [v["developer_name"] for v in views] == ["My_cases", "Handsfree_replier_test_case", "All"]
          and views[1]["label"] == "Handsfree_replier_test_case" and views[0]["id"] == "00B1"
          and len(lv_ips.calls) == 2 and lv_ips.calls[0][2] == {"limit": 200}
          and lv_ips.calls[1][2] is None
          and len(_ListViewsIps().list_case_views("My_cases")) == 1
          and "list view not found: Nope" in lv_err and len(lv_ips2.calls) == 2,
          f"views={views} calls={lv_ips.calls} err={lv_err!r}")

    class _PagedIps(IpsClient):
        """Customer-updates query answered in two pages; the second page
        holds the only comment of case 500B."""
        def __init__(self):
            super().__init__()
            self.calls = []
        def _request(self, method, path, *, params=None, **kw):
            self.calls.append((path, params))
            if path == "/services/data/v{ver}/query":
                return _Resp({"done": False, "nextRecordsUrl": "/services/data/v{ver}/query/01g-2000",
                              "records": [{"Core_IPS_Case__c": "500A",
                                           "CreatedDate": "2026-10-05T12:00:00.000+0000"},
                                          {"Core_IPS_Case__c": "500A",
                                           "CreatedDate": "2026-10-05T11:00:00.000+0000"}]})
            return _Resp({"done": True,
                          "records": [{"Core_IPS_Case__c": "500B",
                                       "CreatedDate": "2026-10-05T10:00:00.000+0000"}]})
    paged = _PagedIps()
    upd = paged.find_customer_updates(["500A", "500B"], "2026-10-05T00:00:00Z")
    check("S16.h find_customer_updates consumes every query page (latest per case kept)",
          upd == {"500A": "2026-10-05T12:00:00.000+0000",
                  "500B": "2026-10-05T10:00:00.000+0000"}
          and len(paged.calls) == 2 and paged.calls[1][0].endswith("/01g-2000")
          and paged.calls[1][1] is None, f"{upd} {paged.calls}")

    class _StateFailIps(_ListViewsIps):
        def _request(self, method, path, *, params=None, **kw):
            if path.endswith("/results"):
                return _Resp({"records": [{"columns": [
                    {"fieldNameOrPath": "CaseNumber", "value": "01234567"},
                    {"fieldNameOrPath": "Id", "value": "500B"}]}]})
            if path == "/services/data/v{ver}/query":
                raise RuntimeError("QUERY_TIMEOUT")
            return super()._request(method, path, params=params, **kw)
    try:
        _StateFailIps().list_view_cases("My_cases")
        state_err = ""
    except RuntimeError as e:
        state_err = str(e)
    check("S16.i list view whose open/closed lookup fails aborts the round instead of "
          "treating unknown state as open",
          "case state lookup failed" in state_err and "QUERY_TIMEOUT" in state_err, state_err)

    tz = timezone.utc
    t = datetime(2026, 10, 6, 9, 30, tzinfo=tz)
    check("S16.f schedule: nightly -> next 23:00 (same day before, next day after); "
          "4h/8h start now, then +interval",
          next_run_after("nightly", t) == datetime(2026, 10, 6, 23, 0, tzinfo=tz)
          and next_run_after("nightly", datetime(2026, 10, 6, 23, 5, tzinfo=tz))
          == datetime(2026, 10, 7, 23, 0, tzinfo=tz)
          and next_run_after("4h", t, first=True) == t
          and next_run_after("8h", t) == datetime(2026, 10, 6, 17, 30, tzinfo=tz))


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

    only_one = {"assert_codes": ["0x00001234"], "yellow_bang": False, "source": "wrt_log",
                "asserts": [{"code": "0x00001234", "cpu": "LMAC", "data": {}, "line": "",
                             "source": "wrt_log"}], "ignored_outside_window": 1}
    asked_f: list[str] = []
    ins_f = collect_echo_insights(a, ask=lambda q: (asked_f.append(q) or "ok"),
                                  evidence=only_one)
    check("S7.b2 a pre-filtered evidence set is used as-is (no recompute: the agent-text "
          "code 0x02001234 is never asked about)",
          [i["code"] for i in ins_f] == ["0x00001234"] and len(asked_f) == 1
          and "0x02001234" not in asked_f[0], str(asked_f)[:300])

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


# ---------------------------------------------------------------- S8
def smoke_time_coverage(tmp: Path) -> None:
    print("[S8] Issue-time vs log-window coverage check")
    from .runner import CaseAnalysis, HandsfreeRunner
    from .composer import compose_plain

    log = tmp / "s8" / "x.etl.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text(
        "06/20/2026-10:16:01.000 [I] Connected to BSSID aa:bb:cc:dd:ee:ff\n"
        "06/20/2026-10:44:58.154 [I] last line\n", encoding="utf-8")
    r = HandsfreeRunner(progress_cb=lambda s, d: None)

    a = CaseAnalysis(case_nbr="1", mode="full", log_path=str(log),
                     issue_times=["06/20/2026-08:00:00"])
    r._check_time_coverage(a)
    check("S8.a issue time outside window -> mismatch flagged",
          a.time_mismatch.get("issue_times") == ["06/20/2026-08:00:00"]
          and a.time_mismatch.get("log_first", "").endswith("10:16:01"),
          str(a.time_mismatch))

    b = CaseAnalysis(case_nbr="2", mode="full", log_path=str(log),
                     issue_times=["06/20/2026-10:30:00"])
    r._check_time_coverage(b)
    check("S8.b issue time inside window -> no flag", b.time_mismatch == {})

    c = CaseAnalysis(case_nbr="3", mode="full", log_path=str(log),
                     issue_times=["10:17:30"])
    r._check_time_coverage(c)
    check("S8.c clock-only value placed on the log's date: inside -> no flag",
          c.time_mismatch == {}, str(c.time_mismatch))
    c2 = CaseAnalysis(case_nbr="3b", mode="full", log_path=str(log),
                      issue_times=["12:30"])
    r._check_time_coverage(c2)
    win_c = r._assert_window(c)
    check("S8.c2 clock-only value the log never reaches -> mismatch flagged; the "
          "assert window anchors on the clock placed inside the log",
          c2.time_mismatch.get("issue_times") == ["12:30"]
          and win_c is not None and win_c[0].strftime("%H:%M") == "09:47"
          and win_c[1].strftime("%m/%d %H:%M") == "06/20 10:27"
          and r._assert_window(c2) is None,
          f"{c2.time_mismatch} win={win_c}")

    d = CaseAnalysis(case_nbr="4", mode="full", log_path=str(log),
                     issue_times=["06/20/2026-10:10:00"])
    r._check_time_coverage(d)
    check("S8.d grace window (10 min before start) -> no flag",
          d.time_mismatch == {})

    a.triage = {}
    plain = compose_plain(a)
    check("S8.e mismatch warning composed with request for issue-time log",
          "TIME MISMATCH" in plain
          and "please help provide a WRT log captured at the issue time" in plain,
          plain[:300])

    # Customer / WRT time spellings parse as full datetimes (coverage check
    # and assert window depend on it) and the minute-precision filter.
    from utils.issue_time_utils import parse_issue_time_string as pits
    from .case_reader import has_minute_precision as hmp
    check("S8.f customer time spellings parse as full datetimes",
          pits("16:31/12/13/2025")[0] is not None
          and pits("2026-1-14-13-54-47")[0] is not None
          and pits("14-01-2026_13-54-37")[0] is not None
          and pits("June 2nd") == (None, False))
    check("S8.g minute precision: clock times and WRT stamps yes; dates and words no",
          all(hmp(t) for t in ("04/13/2026 12:14:00", "16:31/12/13/2025",
                               "2026-1-14-13-54-47", "09/04/2026-20:58:52.654", "10:17"))
          and not any(hmp(t) for t in ("June 2nd", "2026-09-04", "yesterday morning",
                                       "2026-06-20", "")))

    # Gate: the decoded log does not cover the issue time -> try the capture
    # closest to it; still no coverage -> public request, no agent, no Echo.
    from .runner import _folder_timestamp
    cap_far = tmp / "s8" / "DUT_27-05-2026_17-51-23_656_4_5002_0x0_0x0_0x0" / "WifiDriverIHVSession.etl.002"
    cap_near = tmp / "s8" / "DUT_13-04-2026_12-10-05_100_9_6050_0x0_0x0_0x0" / "WifiDriverIHVSession.etl.001"
    for p in (cap_far, cap_near):
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"x")
    check("S8.h capture folder stamp parsed from the WRT folder name",
          _folder_timestamp(str(cap_far)).strftime("%Y-%m-%d %H:%M:%S") == "2026-05-27 17:51:23"
          and _folder_timestamp(str(tmp / "s8" / "x.etl")) is None)
    alts = HandsfreeRunner._alternate_etls([str(cap_far), str(cap_near)], [],
                                           ["04/13/2026 12:14:00"], {str(cap_far)})
    check("S8.i alternate captures ordered by closeness to the issue time",
          alts == [str(cap_near)], str(alts))

    # Clock frames: the DUT's folder stamp (13:54:37) and the log's last line
    # (21:55:01) are the same moment, so the issue time 13:54:47 is INSIDE
    # the 21:29–21:55 log window once shifted (00994274).
    fcap = tmp / "s8" / "LAPTOP_14-01-2026_13-54-37_127_9_6050_0x0_0x0_0x0" / "WifiDriverIHVSession.etl.003.log"
    fcap.parent.mkdir(parents=True, exist_ok=True)
    fcap.write_text("01/14/2026-21:29:56.906 [0] first\n01/14/2026-21:55:01.000 [0] last\n",
                    encoding="utf-8")
    e = CaseAnalysis(case_nbr="6", mode="full", log_path=str(fcap),
                     issue_times=["2026-1-14-13-54-47"])
    r._check_time_coverage(e)
    e2 = CaseAnalysis(case_nbr="7", mode="full", log_path=str(fcap),
                      issue_times=["2026-1-14-09-00-00"])
    r._check_time_coverage(e2)
    check("S8.k folder-stamp clock offset: 13:54 DUT time covered by the 21:29–21:55 log; "
          "09:00 still outside",
          e.time_mismatch == {} and e2.time_mismatch.get("issue_times") == ["2026-1-14-09-00-00"],
          f"{e.time_mismatch} {e2.time_mismatch}")

    # Assert window: an assert 2 h before the issue time is not evidence.
    from .echo_client import find_assert_evidence
    wlog = tmp / "s8" / "w.etl.log"
    wlog.write_text(
        "06/20/2026-08:00:00.000 [1] [NIC_DEBUG] [ERROR] [x]:FATAL_ERROR: uCode ASSERT(UMAC, rtStatus = 0x20101F01, data1 = 0x0)\n"
        "06/20/2026-10:16:30.000 [1] [NIC_DEBUG] [ERROR] [x]:FATAL_ERROR: uCode ASSERT(LMAC, rtStatus = 0x00001234, data1 = 0x1)\n",
        encoding="utf-8")
    w = CaseAnalysis(case_nbr="5", mode="full", log_path=str(wlog),
                     issue_times=["06/20/2026-10:17:30"])
    win = HandsfreeRunner(progress_cb=lambda s, d: None)._assert_window(w)
    ev = find_assert_evidence(w, window=win)
    check("S8.j asserts outside the issue-time window ignored, inside kept",
          win is not None and ev["assert_codes"] == ["0x00001234"]
          and ev["ignored_outside_window"] == 1, str(ev))
    # Window excludes EVERY log assert: the agent's write-up (built from the
    # whole log) must not hand the excluded codes back as evidence.
    from .runner import IncidentReport
    w2 = CaseAnalysis(case_nbr="5b", mode="full", log_path=str(wlog),
                      issue_times=["06/20/2026-12:17:30"])
    w2.incidents.append(IncidentReport(
        report_text="Firmware ASSERT 0x20101F01 and 0x00001234 seen in the log."))
    ev2 = find_assert_evidence(w2, window=HandsfreeRunner(progress_cb=lambda s, d: None)
                               ._assert_window(w2))
    ev3 = find_assert_evidence(w2, window=None)
    check("S8.j2 no agent-text fallback once the window excluded log asserts; "
          "without a window the fallback still applies to assert-free logs",
          ev2["assert_codes"] == [] and ev2["ignored_outside_window"] == 2
          and ev2["source"] is None and sorted(ev3["assert_codes"]) == ["0x00001234", "0x20101F01"],
          f"{ev2} {ev3['assert_codes']}")
    from .bsod import assessment as bsod_assessment
    both = bsod_assessment([{"wifi_fault": True, "bt_fault": True},
                            {"wifi_fault": False, "bt_fault": False, "driver_on_stack": True}])
    check("S8.m BSOD assessment names both drivers when one dump blames both",
          "Wi-Fi driver" in both and "BT driver" in both and "1/2" in both
          and "BT" not in bsod_assessment([{"wifi_fault": True}]), both)

    # Two clocks: a customer in UTC-3 reports 15:00; the decoded log (decoder
    # host GMT+8) covers 01:30–02:05 the NEXT day, so 15:00 = 02:00 in the
    # log. The tz comes from system_info.txt beside the capture. Own subtree:
    # the tz walk looks at a dir and its immediate children, so tmp/s8/* logs
    # never see this file.
    from datetime import datetime as _dt
    ar_case = tmp / "s8_ar" / "case"
    ar_log = ar_case / "WRT" / "x.etl.log"
    ar_log.parent.mkdir(parents=True, exist_ok=True)
    (ar_case / "system_info.txt").write_text(
        json.dumps({"System Time Zone": "(UTC-03:00) City of Buenos Aires"}),
        encoding="utf-8")
    ar_log.write_text("06/21/2026-01:30:00.000 [0] first\n"
                      "06/21/2026-02:05:00.000 [0] last\n", encoding="utf-8")
    try:
        n = CaseAnalysis(case_nbr="8", mode="full", wifi_or_bt="wifi",
                         log_path=str(ar_log), issue_times=["06/20/2026 15:00:00"])
        r._check_time_coverage(n)
        check("S8.n UTC-3 customer: 15:00 maps to 02:00 next day inside the log -> "
              "covered, alignment recorded (time zone, +11:00)",
              n.time_mismatch == {}
              and n.time_alignment.get("log_time") == "06/21/2026-02:00:00"
              and n.time_alignment.get("customer_time") == "06/20/2026-15:00:00"
              and n.time_alignment.get("anchor") == "tz_helper"
              and n.time_alignment.get("offset") == "+11:00"
              and "Buenos Aires" in n.time_alignment.get("customer_tz", ""),
              f"{n.time_mismatch} {n.time_alignment}")

        class _StubAgent:
            def __init__(self):
                self.issue_time = None
                self._issue_time_time_only = True
                self.issue_time_customer = None
                self.issue_time_tz = ""
                self.current_log_path = ""
            def prime_with_context(self, **kw):
                self.issue_time = _dt(2026, 6, 20, 15, 0)   # what the UI path would set
            def chat(self, msg, use_tools=True, max_steps=1, step_callback=None):
                return {"type": "report", "data": {
                    "root_cause_summary": "deauth at the reported time",
                    "confidence_score": 80, "recommended_actions": [],
                    "involved_skills": ["Connectivity"]}}
        stub = _StubAgent()
        orig_build = HandsfreeRunner._build_agent
        HandsfreeRunner._build_agent = lambda self, domain="wifi": stub
        try:
            r._run_agent(n, max_steps=1)
        finally:
            HandsfreeRunner._build_agent = orig_build
        inc_n = n.incidents[0] if n.incidents else None
        check("S8.o agent receives the LOG-frame issue time (02:00), incident keeps both clocks",
              stub.issue_time == _dt(2026, 6, 21, 2, 0)
              and stub.issue_time_customer == _dt(2026, 6, 20, 15, 0)
              and stub._issue_time_time_only is False
              and "Buenos Aires" in stub.issue_time_tz
              and inc_n is not None
              and inc_n.issue_time == "06/20/2026 15:00:00"
              and inc_n.issue_time_customer == "06/20/2026-15:00:00"
              and inc_n.issue_time_log == "06/21/2026-02:00:00"
              and inc_n.time_anchor == "tz_helper",
              f"agent={stub.issue_time} inc={inc_n}")
        plain_n = compose_plain(n)
        check("S8.p private draft shows both clocks and how they were aligned",
              "Incident time: 06/20/2026 15:00:00 (customer, (UTC-03:00) City of Buenos Aires) "
              "= 06/21/2026-02:00:00 (in the log)" in plain_n
              and "Clock alignment: customer 06/20/2026-15:00:00" in plain_n
              and "offset +11:00 via time zone" in plain_n,
              plain_n[:600])

        # Not covered: 10:00 customer = 21:00 log, the log runs 01:30–02:05.
        # The public request states the log's range in the CUSTOMER's clock.
        q = CaseAnalysis(case_nbr="9", mode="full", wifi_or_bt="wifi",
                         log_path=str(ar_log), issue_times=["06/20/2026 10:00:00"])
        r._check_time_coverage(q)
        q.mode, q.log_request_reason = "request_logs", "log_not_covering"
        public_q = compose_plain(q)
        q.mode = "full"
        private_q = compose_plain(q)
        check("S8.q non-covering log: range shifted to the customer's clock in the public "
              "request (no Taiwan times), both clocks in the private warning",
              q.time_mismatch.get("log_first_customer") == "06/20/2026-14:30:00"
              and q.time_mismatch.get("log_last_customer") == "06/20/2026-15:05:00"
              and q.time_mismatch.get("issue_times_log") == ["06/20/2026-21:00:00"]
              and q.time_mismatch.get("offset") == "+11:00"
              and q.time_mismatch.get("log_first") == "06/21/2026-01:30:00"
              and "covers 06/20/2026-14:30:00 – 06/20/2026-15:05:00 (your local time, "
                  "(UTC-03:00) City of Buenos Aires)" in public_q
              and "01:30" not in public_q and "02:05" not in public_q
              and "= 06/20/2026-21:00:00 in the log's clock" in private_q
              and "(log clock) = 06/20/2026-14:30:00 – 06/20/2026-15:05:00 (customer clock"
                  in private_q,
              f"{q.time_mismatch}\n{public_q}\n{private_q[:400]}")
    finally:
        shutil.rmtree(tmp / "s8_ar", ignore_errors=True)
    check("S8.r folder-stamp alignment recorded (00994274 pattern: +08:00 via the capture folder)",
          e.time_alignment.get("anchor") == "folder_stamp"
          and e.time_alignment.get("offset") == "+08:00"
          and e.time_alignment.get("log_time") == "01/14/2026-21:55:11",
          str(e.time_alignment))

    # A reported time a minute before the log's first line: covered under the
    # 10-min grace, so the alignment must place it too (inside=True) — the
    # chatbot hand-off carries that value and the Log Chatbot's own range
    # check allows the same grace (01033659, 2026-10-19).
    from . import runner as runner_mod_g
    from datetime import datetime as _dt_g
    r_g = runner_mod_g.HandsfreeRunner()
    first_g, last_g = _dt_g(2026, 6, 20, 10, 17, 0), _dt_g(2026, 6, 20, 10, 30, 0)
    al_in = r_g._align_issue_time("06/20/2026-10:16:30", str(tmp / "nolog.log"), first_g, last_g)
    al_out = r_g._align_issue_time("06/20/2026-09:50:00", str(tmp / "nolog.log"), first_g, last_g)
    # Reader-style times ("16:31/12/13/2025") never parsed inside the capture
    # picker, so it silently took the newest folder; the runner now normalizes
    # the value first.
    fix_wifi = [str(tmp / "x_13-12-2025_16-35-00_1" / "WifiDriverIHVSession.etl.001"),
                str(tmp / "x_13-12-2025_20-00-00_1" / "WifiDriverIHVSession.etl.001")]
    picked, rule, used = r_g._pick_etl(fix_wifi, [], "16:31/12/13/2025")
    picked_none, rule_none, used_none = r_g._pick_etl(fix_wifi, [], "")
    check("S8.o capture pick: a reader-style issue time is normalized and the capture "
          "at/after it wins (not the newest); without a time the newest is taken and says so",
          picked == fix_wifi[0] and rule == "closest_at_or_after_issue" and used == "12/13/2025-16:31:00"
          and picked_none == fix_wifi[1] and rule_none == "newest_wifi" and used_none == "",
          f"picked={picked} rule={rule} used={used} none={picked_none},{rule_none}")
    from .checklist import resolve_subcategory_explained as rse
    check("S8.p sub-category rules explained: explicit / keyword / default / ask / none",
          rse("Connectivity", "Roaming", "") == ("Roaming", "explicit")
          and rse("Connectivity", "", "the client roams badly")[1].startswith("keyword:")
          and rse("Connectivity", "", "") == ("Connectivity", "named_like_domain")
          and rse("OEM Tools", "", "") == ("", "ask")
          and rse("BSOD", "", "") == ("", "none"),
          str([rse("Connectivity", "", "the client roams badly"), rse("OEM Tools", "", "")]))
    check("S8.n alignment uses the coverage grace: 30 s before the first line is inside, "
          "27 min before is not",
          al_in is not None and al_in["inside"] and al_in["log"] == _dt_g(2026, 6, 20, 10, 16, 30)
          and al_out is not None and not al_out["inside"],
          f"in={al_in} out={al_out}")


# ---------------------------------------------------------------- S10
def smoke_yb_etl_pick() -> None:
    print("[S10] YB/assert ETL-folder preference (earliest nonzero error code)")
    from .runner import _pick_etl_yb_earliest, _YB_ASSERT_ISSUE_RE

    base = r"C:\x\case\WRT\Log"
    wifi = [
        base + r"\TEST1-PC_20-08-2026_13-41-21_288_Dump_failed\WifiDriverIHVSession.etl.004",
        base + r"\TEST1-PC_20-08-2026_10-47-05_267_12_5010_0x4222_0x0_0x0\WifiDriverIHVSession.etl.002",
        base + r"\TEST1-PC_20-08-2026_10-46-44_524_12_4222_0xa_0x0_0x1fffff\WifiDriverIHVSession.etl.001",
        base + r"\TEST1-PC_20-08-2026_10-46-44_524_12_4222_0xa_0x0_0x1fffff\WifiDriverIHVSession_History\WifiDriverIHVSession.etl.003",
        base + r"\TEST1-PC_20-08-2026_10-35-05_699_Autologger\WifiDriverIHVSession.etl.001",
        base + r"\TEST1-PC_20-08-2026_10-34-21_993_9_6050_0x0_0x0_0x0\WifiDriverIHVSession.etl.001",
    ]
    picked = _pick_etl_yb_earliest(wifi, [])
    check("S10.a earliest nonzero-code folder wins, non-History ETL",
          picked is not None and "10-46-44" in picked
          and "0xa_0x0_0x1fffff" in picked and "History" not in picked,
          str(picked))
    check("S10.b all-zero / codeless folders never qualify",
          _pick_etl_yb_earliest(
              [base + r"\A_20-08-2026_10-34-21_993_9_6050_0x0_0x0_0x0\x.etl.001",
               base + r"\B_20-08-2026_10-35-05_699_Autologger\x.etl.001"], [])
          is None)
    check("S10.c issue-type trigger matches YB/assert wording",
          bool(_YB_ASSERT_ISSUE_RE.search("Yellow Bang (YB)"))
          and bool(_YB_ASSERT_ISSUE_RE.search("firmware assert during test"))
          and not _YB_ASSERT_ISSUE_RE.search("slow roaming between APs"))


# ---------------------------------------------------------------- S11
def smoke_env_detail() -> None:
    print("[S11] IPS Environment Details wiring")
    from .case_reader import _format_env_detail, READER_PROMPT
    from .runner import CaseAnalysis, _env_repro_steps, _env_brief
    from .echo_client import find_assert_evidence

    env = {"Computer Model": "LvzhouD", "Found In Build": "24.30.1.1",
           "Graphics Driver": "",
           "Steps to reproduce": "[Operation Steps] 1. Burn-in stress 2. Check device status",
           "Assert Error (The value must be a single, 32bit...)": "NA"}
    block = _format_env_detail(env)
    check("S11.a env form rendered as Q:A lines, empty answers skipped",
          "Computer Model: LvzhouD" in block and "Graphics Driver" not in block
          and "{env_block}" in READER_PROMPT, block[:200])

    check("S11.b filled 'Steps to reproduce' detected; brief built",
          _env_repro_steps(env).startswith("[Operation Steps]")
          and "Found In Build: 24.30.1.1" in _env_brief(env)
          and _env_repro_steps({"Steps to reproduce": "NA"}) == "")

    # The "Assert Error" form field is customer-filled from Windows Event
    # Viewer — a DIFFERENT code namespace than driver rtStatus asserts.
    # It must NEVER become assert evidence (same trap as the 0x5002 bug).
    a = CaseAnalysis(case_nbr="1", mode="full",
                     env_detail={"Assert Error (32bit or NA)": "0x2000008A"})
    ev = find_assert_evidence(a)
    check("S11.c customer-filled Assert Error field NEVER becomes evidence",
          ev["assert_codes"] == [] and ev["source"] is None, str(ev))
    check("S11.d reader prompt warns the field is not a firmware assert",
          "NOT a driver/firmware assert code" in READER_PROMPT)


# ---------------------------------------------------------------- S12
def smoke_checklist() -> None:
    print("[S12] Debug-checklist data + first-response rendering")
    from .runner import CaseAnalysis
    from .checklist import (FALLBACK_DOMAIN, _blank_fills, build_fills,
                            deterministic_fills, load_checklist, llm_fill,
                            resolve_domain)
    from .composer import compose, CHECKLIST_TAG, AI_MARKER

    data = load_checklist()
    check("S12.a committed JSON: 14 domains + 10 general questions",
          len(data["domains"]) == 14 and len(data["general_info"]) == 10
          and data["revision"] == "Rev1_0",
          f"domains={len(data['domains'])} rev={data['revision']}")
    w = data["domains"].get("WowLAN", {})
    check("S12.b WowLAN tab has all three sections",
          len(w.get("required_log", [])) >= 4
          and any("wake method" in e["item"].lower() for e in w.get("required_info", []))
          and any("firewall" in e["item"].lower() for e in w.get("initial_triage", [])),
          str({k: len(w.get(k, [])) for k in ("required_log", "required_info", "initial_triage")}))
    check("S12.c domain aliases resolve; unknown -> Others",
          resolve_domain("Yellow Bang (YB)") == "Yellow Bang"
          and resolve_domain("wake on wlan") == "WowLAN"
          and resolve_domain("weird thing") == FALLBACK_DOMAIN
          and resolve_domain("") == FALLBACK_DOMAIN)

    a = CaseAnalysis(case_nbr="1", mode="first_response", ok=True,
                     subject="YB after burn-in", issue_domain="Yellow Bang",
                     chosen_attachment="WRT_0820.zip", log_path="x.log",
                     issue_times=["08/20/2026 13:43"],
                     env_detail={"Steps to reproduce": "1. stress test the DUT 2. check device manager"})
    fills = _blank_fills("Yellow Bang")
    deterministic_fills(a, fills)
    wrt = next(e for e in fills["required_log"] if e["item"].startswith("WRT Log"))
    steps = next(e for e in fills["general_info"] if "reproduction steps" in e["item"].lower())
    check("S12.d deterministic fills: WRT log + env repro steps checked",
          wrt["provided"] and "WRT_0820.zip" in wrt["value"] and steps["provided"])

    def _boom(**kw):
        raise RuntimeError("llm down")
    import types
    llm_fill(types.SimpleNamespace(chat=_boom), fills, "case text")
    check("S12.e llm_fill failure leaves items unfilled, never raises",
          not any(e["provided"] for e in fills["required_info"]))

    a.checklist_fills = fills
    d = compose(a)
    check("S12.f first_response draft: tag, checked value, example hint, triage",
          CHECKLIST_TAG in d["plain"] and AI_MARKER in d["plain"]
          and "[✓] WRT Log, including — provided: WRT_0820.zip" in d["plain"]
          and "please provide (Example: code 10)" in d["plain"]
          and "=== Please verify (initial triage) ===" in d["plain"]
          and d["confidence"] is None,
          d["plain"][:300])

    # LLM fill happy path: answers item 1 of the remaining ones.
    fills2 = _blank_fills("Yellow Bang")
    fake = types.SimpleNamespace(chat=lambda messages, system_content=None:
                                 '{"fills": {"1": {"provided": true, "value": "Yes, regression from 24.30"}}}')
    llm_fill(fake, fills2, "case text")
    check("S12.g llm_fill maps numbered answers onto items",
          fills2["general_info"][0]["provided"]
          and "regression" in fills2["general_info"][0]["value"])

    # Domain fallback chain (finding from the 5-case Lenovo validation:
    # 'OEM Tools' cases are wifi_or_bt='bt' and skipped the reader entirely).
    from .runner import _resolve_issue_domain
    check("S12.h reader's specific pick wins",
          _resolve_issue_domain("Yellow Bang", "OEM Tools", "Connectivity")
          == "Yellow Bang")
    check("S12.i reader Others never shadows a specific subcategory",
          _resolve_issue_domain("Others", "OEM Tools", "") == "OEM Tools"
          and _resolve_issue_domain("", "OEM Tools", "") == "OEM Tools")
    check("S12.j issue-type fallback, then Others",
          _resolve_issue_domain("", "", "Yellow Bang (YB)") == "Yellow Bang"
          and _resolve_issue_domain("", "", "") == "Others"
          and _resolve_issue_domain("Others", "weird", "stranger") == "Others")

    # Sub-categories (Connectivity / P2P tabs use an Issue Type table).
    from .checklist import resolve_subcategory, _blank_fills as _bf
    c = data["domains"]["Connectivity"]
    check("S12.k Connectivity/P2P sub-categories parsed with tagged items",
          list(c["subcategories"]) == ["Connectivity", "Scan", "Roaming"]
          and all(e.get("subcat") for e in c["required_log"])
          and list(data["domains"]["P2P (Miracast)"]["subcategories"])
          == ["Connectivity", "Performance"],
          str(list(c.get("subcategories", {}))))
    check("S12.l sub-category resolution: name, keywords, fallbacks",
          resolve_subcategory("Connectivity", "Roaming", "") == "Roaming"
          and resolve_subcategory("Connectivity", "", "DUT roams from AP1 to AP2") == "Roaming"
          and resolve_subcategory("Connectivity", "", "empty scan list") == "Scan"
          and resolve_subcategory("Connectivity", "", "connect fail after resume") == "Connectivity"
          and resolve_subcategory("WowLAN", "x", "y") == "")
    roam = _bf("Connectivity", "Roaming")
    conn = _bf("Connectivity", "Connectivity")
    check("S12.m fills filtered to the chosen sub-category only",
          any("Issue replication time" == e["item"] for e in roam["required_log"])
          and not any("WRT BIOS settings" in e["item"] for e in roam["required_log"])
          and len(conn["required_log"]) == 4,
          f"roam={len(roam['required_log'])} conn={len(conn['required_log'])}")
    check("S12.n checked items render before unchecked",
          d["plain"].find("provided: WRT_0820.zip")
          < d["plain"].find("[ ] 2) FW usniffer"))

    # CE policy (2026-09-30): Air sniffer / OS log only requested for
    # performance debugging.
    import re as _re
    hits = [(dom, sec, e.get("subcat", ""))
            for dom, dd in data["domains"].items()
            for sec in ("required_log", "required_info")
            for e in dd.get(sec, [])
            if _re.match(r"(?i)(\d+\)\s*)?(air sniffer log|os log)", e["item"])]
    check("S12.o Air sniffer/OS log kept ONLY for Performance",
          hits and all(dom == "Performance" or sub == "Performance"
                       for dom, sec, sub in hits),
          str(hits))
    check("S12.p Power Consumption triage note about OS log preserved",
          any("OS log" in e["item"]
              for e in data["domains"]["Power Consumption (MS)"]["initial_triage"]))

    # OEM Tools: per-tool table (items in col B) -> tool-tagged asks.
    oem = data["domains"]["OEM Tools"]
    drtu = _bf("OEM Tools", "DRTU")
    check("S12.q OEM Tools parsed per tool, no header rows leaking as items",
          list(oem["subcategories"]) == ["ANT", "DRTU", "CITU", "NDT", "GNNR"]
          and [e["item"] for e in drtu["required_log"]]
          == ["WPP driver log", "USC", "Error/Issue Screenshot",
              "DRTU application log (DrtuLog.txt)"]
          and all(e.get("subcat") and e["item"] not in ("Tool", "ANT", "DRTU")
                  for e in oem["initial_triage"]),
          str([e["item"] for e in drtu["required_log"]]))
    check("S12.r tool acronyms match as whole words only",
          resolve_subcategory("OEM Tools", "", "constant failure when running NDT") == "NDT"
          and resolve_subcategory("OEM Tools", "DRTU", "") == "DRTU"
          and resolve_subcategory("OEM Tools", "", "GNNR dump attached") == "GNNR")
    # No tool named -> ask which one, never guess another tool's log list.
    u = CaseAnalysis(case_nbr="4", mode="first_response", ok=True,
                     subject="Test tool crashes on launch", issue_domain="OEM Tools",
                     description="The tool exits right after start.")
    du = compose(u)["plain"]
    check("S12.r2 OEM Tools with no tool named asks which tool",
          resolve_subcategory("OEM Tools", "", "the tool exits right after start") == ""
          and "[ ] Which OEM tool is the issue with (ANT / DRTU / CITU / NDT / GNNR)?" in du
          and "=== Required Info (OEM Tools) ===" in du
          and "Ant debug log" not in du and "=== Required Log" not in du
          and resolve_subcategory("P2P (Miracast)", "", "no hint") == "Connectivity",
          du[du.find("=== Required"):][:300])

    # Deterministic fills must not claim what the pipeline cannot vouch for.
    b = CaseAnalysis(case_nbr="2", mode="request_logs", ok=True,
                     issue_domain="WowLAN", chosen_attachment="shots.zip",
                     attachment_time="08/21/2026 09:00",
                     issue_times=["08/21/2026 09:00"],
                     env_detail={"Steps to reproduce": "N/A", "Frequency": "none"})
    fb = _blank_fills("WowLAN")
    deterministic_fills(b, fb)
    check("S12.s nothing ticked: request_logs archive, upload-time fallback, "
          "wake-trigger time, N/A placeholders",
          not any(e["provided"] for sec in fb.values() for e in sec),
          str([(e["item"][:30], e["value"]) for sec in fb.values()
               for e in sec if e["provided"]]))

    # The LLM fill pass reads customer-authored content only (its output is
    # posted publicly) — never the all-comments clean_description.
    seen = {}
    def _capture(messages, system_content=None):
        seen["prompt"] = messages[0]["content"]
        return '{"fills": {}}'
    p = CaseAnalysis(case_nbr="3", mode="full", ok=True, subject="Roaming fails",
                     issue_domain="Connectivity", description="DUT cannot roam.",
                     clean_description="SYNTH-FROM-PRIVATE-COMMENTS",
                     customer_history="(2026-06-20) regression from driver 23.60")
    build_fills(p, llm=types.SimpleNamespace(chat=_capture))
    check("S12.t fill prompt: customer history in, all-comments synthesis out",
          "regression from driver 23.60" in seen.get("prompt", "")
          and "DUT cannot roam." in seen["prompt"]
          and "SYNTH-FROM-PRIVATE-COMMENTS" not in seen["prompt"],
          seen.get("prompt", "")[-300:])
    # request_logs: the pipeline found no usable WRT log, so the text pass
    # may not tick WRT/WPP items from an attachment label (00991735 did).
    q = CaseAnalysis(case_nbr="5", mode="request_logs", ok=True, subject="YB code 10",
                     issue_domain="Yellow Bang", description="WRT & event logs attached",
                     chosen_attachment="logs.7z")
    seen.clear()
    build_fills(q, llm=types.SimpleNamespace(chat=_capture))
    check("S12.u request_logs locks the WRT/WPP items out of the LLM fill pass",
          "WPP driver log" not in seen.get("prompt", "")
          and "WRT Log" not in seen.get("prompt", "")
          and "FW usniffer" in seen.get("prompt", "")
          , seen.get("prompt", "")[:400])


# ---------------------------------------------------------------- S15
def _mini_xlsx(path: Path, sheets: list) -> None:
    """Minimal workbook from [(sheet_name, [{col: text}, ...])]. Cells
    alternate between sharedStrings and inline strings; sheet 1 uses a
    package-absolute relationship target — both forms occur in real files."""
    import zipfile
    from xml.sax.saxutils import escape
    ns = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
    rns = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
    sst: list[str] = []
    sheet_xml = []
    n = 0
    for _, rows in sheets:
        out = []
        for ri, row in enumerate(rows, 1):
            cells = []
            for col, text in row.items():
                n += 1
                if n % 2:
                    sst.append(text)
                    cells.append(f'<c r="{col}{ri}" t="s"><v>{len(sst) - 1}</v></c>')
                else:
                    cells.append(f'<c r="{col}{ri}" t="inlineStr"><is><t>'
                                 f'{escape(text)}</t></is></c>')
            out.append(f'<row r="{ri}">{"".join(cells)}</row>')
        sheet_xml.append(f'<worksheet xmlns="{ns}"><sheetData>{"".join(out)}'
                         '</sheetData></worksheet>')
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("xl/workbook.xml",
                   f'<workbook xmlns="{ns}" xmlns:r="{rns}"><sheets>'
                   + "".join(f'<sheet name="{name}" sheetId="{i}" r:id="rId{i}"/>'
                             for i, (name, _) in enumerate(sheets, 1))
                   + "</sheets></workbook>")
        z.writestr("xl/_rels/workbook.xml.rels",
                   '<Relationships xmlns="http://schemas.openxmlformats.org/'
                   'package/2006/relationships">'
                   + "".join(f'<Relationship Id="rId{i}" Target="'
                             f'{"/xl/" if i == 1 else ""}worksheets/sheet{i}.xml"/>'
                             for i in range(1, len(sheets) + 1))
                   + "</Relationships>")
        z.writestr("xl/sharedStrings.xml",
                   f'<sst xmlns="{ns}">'
                   + "".join(f"<si><t>{escape(s)}</t></si>" for s in sst) + "</sst>")
        for i, x in enumerate(sheet_xml, 1):
            z.writestr(f"xl/worksheets/sheet{i}.xml", x)


def smoke_checklist_convert(tmp: Path) -> None:
    print("[S15] Checklist xlsx converter (fixture workbook)")
    from .checklist_convert import convert

    table_hdr = {"A": "Issue Type", "B": "Description", "C": "Check Items",
                 "D": "Customer Feedback", "E": "Comments and Feedback Example"}
    tool_hdr = {"B": "Check Items", "C": "Customer Feedback",
                "D": "Comments and Feedback Example"}
    xlsx = tmp / "fixture_Checklist_Rev9_9.xlsx"
    _mini_xlsx(xlsx, [
        ("Main", [
            {"A": "Step1: general info"},
            {"B": "Check Items", "D": "Example"},
            {"B": "Is this a regression issue?", "D": "Example: Y/N"},
            {"A": "Step2: issue domain"},
            {"B": "FlatDom", "C": "flat domain"},
            {"B": "tabledom", "C": "table domain"},
            {"A": "Step3: done"},
            {"B": "ignored after step3"},
        ]),
        ("FlatDom", [
            {"A": "Capture these logs first:"},
            {"A": "Required Log"},
            {"A": "Check Items", "C": "Comments and Feedback Example"},
            {"A": "WRT Log", "C": "Example: Y/N"},
            {"A": "Air sniffer log", "C": "Example: Y/N"},
            {"A": "Required Info"},
            {"A": "What is the wake method?", "C": "Example: magic packet"},
            {"A": "Initial Triage"},
            {"A": "Check if firewall is disabled", "C": "shall be disabled"},
        ]),
        ("TableDom", [
            {"A": "Required Log"},
            table_hdr,
            {"A": "Roaming", "B": "roam issues", "C": "WRT Log", "E": "Example: Y/N"},
            {"C": "Air sniffer log", "E": "Example: Y/N"},
            {"A": "Performance", "B": "throughput", "C": "Air sniffer log",
             "E": "Example: Y/N"},
        ]),
        ("ToolDom", [
            {"A": "Required Log"},
            dict(tool_hdr, A="Issue Type"),
            {"A": "DRTU", "B": "WPP driver log", "D": "Example: Y/N"},
            {"B": "DRTU application log", "D": "Example: Y/N"},
            {"A": "Initial Triage"},
            dict(tool_hdr, A="Tool"),
            {"A": "DRTU", "B": "Ensure test mode is enabled",
             "D": "Test mode shall be enabled"},
        ]),
        ("Revisions", [{"A": "Required Log"}, {"A": "not a domain"}]),
    ])
    data = convert(str(xlsx))
    doms = data["domains"]
    check("S15.a main tab: revision, general info, domain set (abs + rel "
          "sheet targets, shared + inline strings)",
          data["revision"] == "Rev9_9"
          and data["general_info"] == [{"item": "Is this a regression issue?",
                                        "example": "Example: Y/N"}]
          and list(doms) == ["FlatDom", "TableDom", "ToolDom"],
          f"rev={data['revision']} gi={data['general_info']} doms={list(doms)}")
    flat = doms.get("FlatDom", {})
    check("S15.b flat tab: sections, notes, example hints, policy drop",
          flat.get("notes") == ["Capture these logs first:"]
          and flat.get("required_log") == [{"item": "WRT Log", "example": "Example: Y/N"}]
          and flat.get("required_info") == [{"item": "What is the wake method?",
                                             "example": "Example: magic packet"}]
          and flat.get("initial_triage") == [{"item": "Check if firewall is disabled",
                                              "example": "shall be disabled"}]
          and flat.get("description") == "flat domain"
          and "subcategories" not in flat,
          str(flat))
    table = doms.get("TableDom", {})
    check("S15.c table tab (items col C): sub-categories + descriptions; Air "
          "sniffer kept only under Performance; Step-2 name matched caselessly",
          table.get("subcategories") == {"Roaming": "roam issues",
                                         "Performance": "throughput"}
          and table.get("required_log") == [
              {"item": "WRT Log", "example": "Example: Y/N", "subcat": "Roaming"},
              {"item": "Air sniffer log", "example": "Example: Y/N",
               "subcat": "Performance"}]
          and table.get("description") == "table domain",
          str(table))
    tool = doms.get("ToolDom", {})
    check("S15.d tool tab (items col B, 'Tool' header): tagged asks, header "
          "rows never become items",
          tool.get("subcategories") == {"DRTU": ""}
          and tool.get("required_log") == [
              {"item": "WPP driver log", "example": "Example: Y/N", "subcat": "DRTU"},
              {"item": "DRTU application log", "example": "Example: Y/N",
               "subcat": "DRTU"}]
          and tool.get("initial_triage") == [
              {"item": "Ensure test mode is enabled",
               "example": "Test mode shall be enabled", "subcat": "DRTU"}],
          str(tool))


# ---------------------------------------------------------------- S17
def smoke_comment_images(tmp: Path) -> None:
    print("[S17] Comment images: extraction, blocks, annotation, cache")
    import types
    from .comment_images import (annotate_case_comments, extract_image_urls,
                                 image_blocks, MAX_IMAGES_PER_CASE)

    urls = extract_image_urls(
        '<p>x</p><img alt="a" src="https://intel.file.force.com/servlet/rtaImage?eid=1&amp;refid=2">'
        "<IMG SRC='https://evil.example.com/x.png'>")
    check("S17.a img src extracted (both quote styles), entities decoded",
          urls == ["https://intel.file.force.com/servlet/rtaImage?eid=1&refid=2",
                   "https://evil.example.com/x.png"], str(urls))
    b_claude = image_blocks([("image/png", b"abc")], "claude-4-6-sonnet")
    b_gpt = image_blocks([("image/png", b"abc")], "gpt-4.1")
    check("S17.b image blocks: Anthropic base64 for claude-*, data URL otherwise",
          b_claude[1]["type"] == "image" and b_claude[1]["source"]["media_type"] == "image/png"
          and b_gpt[1]["type"] == "image_url"
          and b_gpt[1]["image_url"]["url"].startswith("data:image/png;base64,"))

    calls = {"vision": 0, "fetch": []}

    class _Ips:
        FIELD_RICH_BODY = "Core_IPS_Rich_Comment__c"
        def get_case_comments(self, case_id):
            img = lambda n: f'<img src="https://intel.file.force.com/servlet/rtaImage?refid={n}">'
            return [
                {"Id": "C3", "CreatedDate": "2026-06-22T01:00:00.000+0000",
                 "Core_IPS_Comment_Author_Type__c": "Agent",
                 "Core_IPS_Rich_Comment__c": "[AI-Avatar preliminary analysis]" + img("ai")},
                {"Id": "C2", "CreatedDate": "2026-06-21T01:00:00.000+0000",
                 "Core_IPS_Comment_Author_Type__c": "Agent",
                 "Core_IPS_Rich_Comment__c": "<p>see</p>" + img("a") + img("b")
                 + '<img src="https://evil.example.com/x.png">'},
                {"Id": "C1", "CreatedDate": "2026-06-20T01:00:00.000+0000",
                 "Core_IPS_Comment_Author_Type__c": "Partner",
                 "Core_IPS_Rich_Comment__c": "<p>no images here</p>"},
            ]
        def fetch_binary(self, url, max_bytes=0):
            calls["fetch"].append(url)
            return ("image/png", b"png") if "refid=a" in url else ("text/html", b"nope")

    def _chat(messages, system_content=None):
        calls["vision"] += 1
        return '{"images": ["screen A"]}'
    llm = types.SimpleNamespace(model="claude-4-6-sonnet", chat=_chat)
    cache = tmp / "imgcache" / "image_descriptions.json"
    base = [["2026-06-20 09:00:00", "Partner", "original row"]]
    rows, info = annotate_case_comments(_Ips(), llm, "500X", base, cache_path=cache,
                                        skip_marker="[AI-Avatar preliminary analysis]")
    check("S17.c AI comment skipped, foreign host skipped, non-image download dropped, "
          "one synthetic row appended after the originals",
          len(rows) == 2 and rows[0] == base[0]
          and rows[1][1] == "Agent" and "1) screen A" in rows[1][2]
          and "evil.example.com" not in " ".join(calls["fetch"])
          and len(calls["fetch"]) == 2 and calls["vision"] == 1
          and info[0]["comment_id"] == "C2" and info[0]["count"] == 2,
          f"rows={rows} fetch={calls['fetch']} info={info}")
    rows2, info2 = annotate_case_comments(_Ips(), llm, "500X", base, cache_path=cache,
                                          skip_marker="[AI-Avatar preliminary analysis]")
    check("S17.d second run served from the cache (no download, no vision call)",
          calls["vision"] == 1 and len(calls["fetch"]) == 2 and rows2[1][2] == rows[1][2]
          and cache.exists(), str(calls))
    check("S17.e budget constant sane", 1 <= MAX_IMAGES_PER_CASE <= 20)
    # Images in the case DESCRIPTION (01034043): read first, row placed ahead
    # of the comments, cached under desc:<case id>.
    fetch_before, vision_before = len(calls["fetch"]), calls["vision"]

    class _IpsDesc(_Ips):
        def get_case_rich_description(self, case_id):
            return {"html": '<p>RU/MRU Tx fails, see <img src="https://intel.file.force.com/servlet/rtaImage?eid=desc&amp;refid=a"></p>',
                    "created": "2026-06-18T08:00:00.000+0000"}
    rows3, info3 = annotate_case_comments(_IpsDesc(), llm, "500Y", base, cache_path=cache,
                                          skip_marker="[AI-Avatar preliminary analysis]")
    check("S17.h images embedded in the case DESCRIPTION are read too: one fetch + one vision "
          "call, row placed first (case creation date), details entry 'description', cached "
          "under desc:<id>",
          rows3 and rows3[0][1] == "Customer (case description)"
          and "[Images embedded in the case description" in rows3[0][2]
          and any(i.get("comment_id") == "description" for i in info3)
          and len(calls["fetch"]) == fetch_before + 1 + 0 and calls["vision"] == vision_before + 1
          and "desc:500Y" in json.loads(cache.read_text(encoding="utf-8")),
          f"first={rows3[0] if rows3 else None} info={info3} fetch={calls['fetch'][fetch_before:]} "
          f"vision={calls['vision'] - vision_before}")
    from .comment_images import _host_allowed
    from .ips_client import is_tenant_url
    check("S17.g image URLs: only Intel's own Salesforce hosts over HTTPS get the session "
          "(other tenants, plain HTTP, look-alike hosts, userinfo tricks refused)",
          all(_host_allowed(u) for u in (
              "https://intel.file.force.com/servlet/rtaImage?eid=1&refid=2",
              "https://intel--c.vf.force.com/x.png", "https://intel.my.salesforce.com/x",
              "https://intel.lightning.force.com/"))
          and not any(is_tenant_url(u) for u in (
              "http://intel.file.force.com/servlet/rtaImage?x=1",
              "https://evil.file.force.com/x.png", "https://evil.my.salesforce.com/x",
              "https://intel.file.force.com.evil.com/x", "https://intel.file.force.com@evil.com/x",
              "https://evil.com/?u=intel.file.force.com", "https://intel.file.force.com:8443/x",
              "ftp://intel.file.force.com/x", "")))
    sent: list = []

    class _Sess:
        cookies, proxies = {}, {}

    class _R:
        def __init__(self, status, headers=None, body=b""):
            self.status_code, self.headers, self._body = status, headers or {}, body
        def iter_content(self, n):
            yield self._body
    from .ips_client import IpsClient as _IC
    from . import ips_client as _ipc
    orig_get = _ipc._requests.get
    def _fake_get(url, **kw):
        sent.append((url, kw.get("allow_redirects")))
        if "hop1" in url:
            return _R(302, {"Location": "https://attacker.file.force.com/steal"})
        if "hop2" in url:
            return _R(302, {"Location": "/servlet/final.png"})
        return _R(200, {"Content-Type": "image/png"}, b"PNGDATA")
    ic = _IC()
    ic._auth = lambda: (_Sess(), {"Authorization": "Bearer x"}, "v60.0")
    try:
        _ipc._requests.get = _fake_get
        r_bad = ic.fetch_binary("https://attacker.file.force.com/x.png")
        r_http = ic.fetch_binary("http://intel.file.force.com/x.png")
        r_hop1 = ic.fetch_binary("https://intel.file.force.com/hop1")
        r_hop2 = ic.fetch_binary("https://intel.file.force.com/hop2")
    finally:
        _ipc._requests.get = orig_get
    check("S17.h fetch_binary: credentials never sent to a foreign tenant or over HTTP, "
          "redirects re-checked per hop (foreign hop dropped, same-host hop followed)",
          r_bad is None and r_http is None and r_hop1 is None
          and r_hop2 == ("image/png", b"PNGDATA")
          and [u for u, _ in sent] == ["https://intel.file.force.com/hop1",
                                       "https://intel.file.force.com/hop2",
                                       "https://intel.file.force.com/servlet/final.png"]
          and all(ar is False for _, ar in sent), str(sent))
    from .comment_images import sniff_media_type
    check("S17.f media type sniffed from magic bytes (servlet lies: png header, jpeg bytes)",
          sniff_media_type(b"\xff\xd8\xff\xe0xx", "image/png") == "image/jpeg"
          and sniff_media_type(b"\x89PNG\r\n\x1a\nxx", "image/jpeg") == "image/png"
          and sniff_media_type(b"RIFF....WEBPVP8 ", "") == "image/webp"
          and sniff_media_type(b"nothing", "image/gif") == "image/gif")


# ---------------------------------------------------------------- S19
def smoke_email_intake(tmp: Path) -> None:
    print("[S19] Outlook intake: mail naming a case -> pipeline -> reply draft")
    from datetime import datetime, timedelta
    from . import orchestrator as orch
    from . import outlook_source as ol
    from .composer import AI_MARKER
    from .queue import HandsfreeStore

    # --- a) case numbers in subject / body ---------------------------------
    nbrs = ol.extract_case_numbers("RE: Case 1031658 Wi-Fi drops / also IPS#01032299",
                                   "see #01031658 again, ticket 123456 is not a case, 01034043 bare")
    check("S19.a case numbers: subject first, 'Case 1031658' padded, 'IPS#' form, bare "
          "eight-digit in the body, duplicates and six-digit non-IPS numbers dropped",
          nbrs == ["01031658", "01032299", "01034043"], str(nbrs))
    check("S19.b no case number -> empty", ol.extract_case_numbers("lunch?", "noon") == [])

    # --- fake Outlook ------------------------------------------------------
    now = datetime.now()

    class _Reply:
        def __init__(self, src, all_):
            self.src, self.all = src, all_
            self.HTMLBody = "<html><body><div>quoted thread</div></body></html>"
            self.EntryID, self.To, self.Subject = "DRAFT-" + src.EntryID, "a@x; b@x", "RE: " + src.Subject
            self.saved = self.displayed = self.sent = False
        def Save(self): self.saved = True
        def Display(self): self.displayed = True
        def Send(self): self.sent = True

    class _Att:
        def __init__(self, name, data, type_=1):
            self.FileName, self.data, self.Type = name, data, type_
        def SaveAsFile(self, path):
            Path(path).write_bytes(self.data)

    class _Atts:
        def __init__(self, atts): self.a = list(atts)
        @property
        def Count(self): return len(self.a)
        def Item(self, i): return self.a[i - 1]

    class _Mail:
        Class = 43
        def __init__(self, entry, subject, body, sender, email, received, atts=()):
            self.EntryID, self.Subject, self.Body = entry, subject, body
            self.SenderName, self.SenderEmailAddress, self.SenderEmailType = sender, email, "SMTP"
            self.ReceivedTime, self.ConversationID = received, "conv-" + entry
            self.replies, self.atts = [], list(atts)
        @property
        def Attachments(self): return _Atts(self.atts)
        def ReplyAll(self):
            r = _Reply(self, True); self.replies.append(r); return r
        def Reply(self):
            r = _Reply(self, False); self.replies.append(r); return r

    class _Items:
        def __init__(self, mails): self.m, self.restricted = list(mails), None
        @property
        def Count(self): return len(self.m)
        def Item(self, i): return self.m[i - 1]
        def Sort(self, key, desc): self.m.sort(key=lambda x: x.ReceivedTime, reverse=bool(desc))
        def Restrict(self, q):
            self.restricted = q
            if str(q).startswith("@SQL"):          # the thread search
                import re as _re
                toks = _re.findall(r"%([^%']+)%", str(q))
                return _Items([m for m in self.m if any(t in (m.Subject + " " + m.Body) for t in toks)])
            return self

    class _Folder:
        def __init__(self, name, mails, subs=()):
            self.Name, self.Items, self.StoreID = name, _Items(mails), "STORE1"
            self.Folders = _Items(list(subs))

    class _NS:
        def __init__(self, inbox, sent=None): self.inbox, self.sent = inbox, sent; self.opened = []
        def GetDefaultFolder(self, n): return self.sent if n == 5 else self.inbox
        def GetItemFromID(self, entry_id, store_id=None):
            self.opened.append((entry_id, store_id))
            for f in [self.inbox] + self.inbox.Folders.m + ([self.sent] if self.sent else []):
                for m in f.Items.m:
                    if m.EntryID == entry_id: return m
            raise RuntimeError("not found")

    class _App:
        def __init__(self, ns): self.ns = ns
        def GetNamespace(self, kind): return self.ns

    png = b"\x89PNG\r\n\x1a\n" + b"x" * 40
    m_new = _Mail("E1", "Fwd: IPS 01031658 keeps disconnecting", "please check the attached WRT log", "Ann", "ann@oem.com",
                  now - timedelta(hours=2), atts=[_Att("WRT_logs.zip", b"PK\x03\x04zip"), _Att("screen.png", png),
                                                  _Att("forwarded.msg", b"", type_=5)])
    m_two = _Mail("E2", "two cases: IPS 01032299", "and 01031658 — bluetooth headset drops", "Bob", "bob@oem.com",
                  now - timedelta(hours=5), atts=[_Att("notes.txt", b"hi")])
    m_sent = _Mail("S1", "RE: Case 01031658 keeps disconnecting", "we are looking into it", "Me", "me@intel.com",
                   now - timedelta(hours=1, minutes=30))
    m_none = _Mail("E3", "lunch?", "noon", "Cat", "cat@intel.com", now - timedelta(hours=1))
    m_old = _Mail("E4", "Case 01034043 old", "", "Dan", "dan@oem.com", now - timedelta(days=30))
    m_mine = _Mail("E5", "RE: Case 01031658", "my own reply", "Me", "me@intel.com", now - timedelta(minutes=5))
    m_body = _Mail("E7", "Case 01034043 [AI_test] parameter config", "see 01034043", "Fay", "fay@oem.com",
                   now - timedelta(minutes=40))      # no 'IPS' in the subject: not a trigger
    sub = _Folder("Beacon", [_Mail("E6", "case 01034043 in subfolder", "", "Eve", "eve@oem.com", now - timedelta(hours=3))])
    # Noise that names the case: an IPS notification, an out-of-office reply,
    # and a status digest naming five cases. None is thread mail or a trigger.
    m_ips = _Mail("N1", "IPS has New Private Comment on Case 01031658 - BANCO", "comment on 01031658",
                  "Intel Premier Support", "intel.premier.support@intel.com", now - timedelta(hours=4))
    m_oof = _Mail("N2", "Automatic reply: Case 01031658 keeps disconnecting", "out of office, see 01031658",
                  "Dan", "dan@oem.com", now - timedelta(minutes=50))
    m_dig = _Mail("N3", "Bi-weekly case status", "01031658 01032299 01034043 01030001 01030002",
                  "Lin", "lin@intel.com", now - timedelta(minutes=30))
    inbox = _Folder("Inbox", [m_new, m_two, m_none, m_old, m_mine, m_ips, m_oof, m_dig, m_body], subs=[sub])
    sent = _Folder("Sent Items", [m_sent])
    ns = _NS(inbox, sent)
    ol.Outlook.factory = lambda: _App(ns)
    try:
        with ol.Outlook() as app:
            refs = app.scan("Inbox", now - timedelta(days=7), own_email="ME@intel.com")
            refs_sub = app.scan("Inbox/Beacon", now - timedelta(days=7))
            # a bounded scan: 2 mails per round, the newest mail in the folder is our own
            budget_1 = app.scan("Inbox", now - timedelta(days=7), own_email="ME@intel.com", max_items=2)
            budget_2 = app.scan("Inbox", now - timedelta(days=7), own_email="ME@intel.com", max_items=2,
                                skip_ids={r.entry_id for r in budget_1})
            budget_3 = app.scan("Inbox", now - timedelta(days=7), own_email="ME@intel.com", max_items=2,
                                skip_ids={r.entry_id for r in budget_1 + budget_2})
            try:
                app.folder("Inbox/Nope")
                folder_err = ""
            except ol.OutlookUnavailable as e:
                folder_err = str(e)
        by = {r.entry_id: r for r in refs}
        check("S19.c scan: newest first, 7-day window, own mail skipped, case numbers per mail, "
              "subfolder path resolved, missing folder named in the error",
              [r.entry_id for r in refs] == ["N3", "E7", "N2", "E3", "E1", "N1", "E2"]
              and by["E1"].case_nbrs == ["01031658"] and by["E2"].case_nbrs == ["01032299", "01031658"]
              and by["E3"].case_nbrs == [] and by["E1"].sender_email == "ann@oem.com"
              and by["E1"].store_id == "STORE1" and by["E1"].received.startswith(str(now.year))
              and len(by["N3"].case_nbrs) == 5
              and [r.entry_id for r in refs_sub] == ["E6"] and "Nope" in folder_err,
              f"{[(r.entry_id, r.case_nbrs) for r in refs]} sub={[r.entry_id for r in refs_sub]} err={folder_err}")

        check("S19.s a bounded scan reaches older mail round by round: mail closed in an earlier "
              "round is passed over without using the budget (it used to stop at the newest N for "
              "ever), and our own mail does not use it either",
              [r.entry_id for r in budget_1] == ["N3", "E7"]
              and [r.entry_id for r in budget_2] == ["N2", "E3"]
              and [r.entry_id for r in budget_3] == ["E1", "N1"],
              f"{[r.entry_id for r in budget_1]} {[r.entry_id for r in budget_2]} "
              f"{[r.entry_id for r in budget_3]}")

        # --- l/m/n/o) the email THREAD as the case -----------------------------
        from . import email_case as ec
        with ol.Outlook() as app:
            th = app.thread("01031658", own_email="me@intel.com")
            th_7 = app.thread("01032299")
            th_watch = app.thread("01034043", folders=orch._thread_folders("Inbox/Beacon"))
            th_plain = app.thread("01034043")
            tdir = tmp / "email_01031658"
            for m in th:
                app.save_attachments(m, str(tdir))
            ctx = ec.build_case_context("01031658", th, str(tdir))
        check("S19.l thread(): every mail naming the case across Inbox + Sent Items (DASL on "
              "subject/body, both number forms), oldest first, own/sent mail flagged, the other case untouched",
              [m.entry_id for m in th] == ["E2", "E1", "S1", "E5"] and th[2].is_own and not th[1].is_own
              and th[2].folder == "Sent Items" and th[3].is_own and th[3].folder == "Inbox"
              and [m.entry_id for m in th_7] == ["E2"],
              f"{[(m.entry_id, m.is_own, m.folder) for m in th]} / {[m.entry_id for m in th_7]}")
        check("S19.q thread() searches exactly the folders it is given: the mail in the watched "
              "subfolder Inbox/Beacon is part of the thread only when that folder is passed "
              "(orchestrator._thread_folders adds it for a scan of that folder)",
              "E6" in [m.entry_id for m in th_watch] and "E6" not in [m.entry_id for m in th_plain]
              and {m.entry_id for m in th_plain} <= {m.entry_id for m in th_watch},
              f"watch={[m.entry_id for m in th_watch]} plain={[m.entry_id for m in th_plain]}")
        check("S19.p filters: IPS notifications, auto-replies and multi-case digests are not thread "
              "mail; the reply goes to the triggering mail, or (own trigger) to the newest customer mail "
              "of its conversation, else to the trigger itself; no trigger -> newest customer mail",
              all(m.entry_id not in ("N1", "N2", "N3") for m in th)
              and ec.is_automated("intel.premier.support@intel.com") and ec.is_automated(subject="Automatic reply: x")
              and ec.is_automated("", "sys_mule2hsd (Jira)", "") and ec.is_automated("CNV_Jira@Intel.com")
              and not ec.is_automated("ann@oem.com", "Ann", "RE: Case 1")
              and ec.is_digest(["1", "2", "3", "4"]) and not ec.is_digest(["1", "2", "3", "3"])
              and ec.reply_target(th, trigger_entry_id="E2").entry_id == "E2"
              and ec.reply_target(th, trigger_entry_id="S1").entry_id == "S1"
              and ec.reply_target(th).entry_id == "E1"
              and ec.trigger_cases("Fwd: IPS 01031658 keeps disconnecting") == ["01031658"]
              and ec.trigger_cases("ips-01031658 / IPS 01032299") == ["01031658", "01032299"]
              and ec.trigger_cases("Case 01031658 [AI_test]") == [] and ec.trigger_cases("IPS 1031658") == []
              and ec.trigger_cases("ips https://x/01031658") == ["01031658"],
              f"thread={[m.entry_id for m in th]} tgt_E2={ec.reply_target(th, trigger_entry_id='E2').entry_id} "
              f"tgt_S1={ec.reply_target(th, trigger_entry_id='S1').entry_id} tgt={ec.reply_target(th).entry_id}")
        check("S19.m the thread as a case: earliest mail = description (with a From/subject header), "
              "later mails = comments with Customer/Agent authors, file attachments saved and listed as "
              "uploads with the mail's time (attached mails skipped), images set aside, local paths kept, "
              "BT wording -> bt, source email",
              ctx.subject == "two cases: IPS 01032299" and ctx.description.startswith("[Email from Bob <bob@oem.com>")
              and [r[1] for r in ctx.comments] == ["Customer", "Agent", "Agent"]
              and "[Email from Ann" in ctx.comments[0][2] and "please check" in ctx.comments[0][2]
              and [it[0] for it in ctx.attachment_list] == ["notes.txt", "WRT_logs.zip"]
              and ctx.attachment_list[1][2][1] == "email from Ann"
              # each mail's files live in that mail's own folder under the thread folder
              and (tdir / ol.message_dir_name(th[1]) / "WRT_logs.zip").read_bytes().startswith(b"PK")
              and not list(tdir.rglob("forwarded.msg"))
              and ctx.local_paths["WRT_logs.zip"] == str(tdir / ol.message_dir_name(th[1]) / "WRT_logs.zip")
              and [a["name"] for _m, a in ctx.image_files] == ["screen.png"]
              and ctx.wifi_or_bt == "bt" and ctx.source == "email" and ctx.subcategory == "Email thread"
              and ctx.id == "" and ec.reply_target(th).entry_id == "E1"
              and "messages" not in ctx.to_dict(),
              f"subject={ctx.subject!r} comments={[(r[1], r[2][:40]) for r in ctx.comments]} "
              f"atts={ctx.attachment_list} imgs={[a['name'] for _m, a in ctx.image_files]} bt={ctx.wifi_or_bt}")
        from models.models import CaseContext as _CC
        ips_ctx = _CC(case_nbr="01031658", id="500X", subject="IPS subject", description="IPS description",
                      comments=[[(now - timedelta(hours=3)).isoformat(timespec="seconds"), "Partner", "ips comment"]],
                      attachment_list=[["WRT_logs.zip", "https://esft/x", ["10/01/2026 10:00", ""]],
                                       ["other.zip", "https://esft/y", ["10/02/2026 10:00", ""]]],
                      case_download_dir=str(tmp / "ips"), wifi_or_bt="wifi")
        merged = ec.merge_contexts(ips_ctx, ctx)
        check("S19.n stage-2 hook merge_contexts: comments interleaved by time (email rows tagged), "
              "archives of both with IPS winning a name clash, local paths + messages carried, source ips+email",
              [r[2][:7] for r in merged.comments] == ["ips com", "[email]", "[email]", "[email]"]
              and [it[0] for it in merged.attachment_list] == ["WRT_logs.zip", "other.zip", "notes.txt"]
              and merged.attachment_list[0][1] == "https://esft/x"
              and merged.source == "ips+email" and merged.local_paths == ctx.local_paths
              and merged.description.startswith("IPS description") and "[Email thread]" in merged.description
              and merged.id == "500X" and not hasattr(ips_ctx, "source"),   # the IPS context is left alone
              f"{[(r[1], r[2][:12]) for r in merged.comments]} {[it[0] for it in merged.attachment_list]}")
        img_cache = tmp / "email_img_cache.json"
        rows0, info0 = ec.annotate_email_images(None, ctx, cache_path=img_cache)
        img_cache.write_text(json.dumps({"email:E1": {"descriptions": ["a DRTU power-limit screenshot"],
                                                      "url_count": 1}}), encoding="utf-8")
        rows1, info1 = ec.annotate_email_images(None, ctx, cache_path=img_cache)
        check("S19.o thread images: no vision available -> rows unchanged; a cached description becomes a "
              "comment row in time order with the mail's author, details like the IPS image reader",
              rows0 == ctx.comments and info0 == []
              and len(rows1) == len(ctx.comments) + 1
              and [r[1] for r in rows1] == ["Customer", "Customer", "Agent", "Agent"]
              and rows1[1][2].startswith("[Images attached to the mail of") and "DRTU" in rows1[1][2]
              and info1 == [{"comment_id": "E1", "date": ctx.messages[1].received, "author": "Customer (Ann)",
                             "count": 1, "descriptions": ["a DRTU power-limit screenshot"]}],
              f"{[(r[1], r[2][:30]) for r in rows1]} {info1}")

        # --- d) the round: fake pipeline, drafts carry the mail --------------
        store = HandsfreeStore(tmp / "handsfree_s19")
        ran: list = []
        ctxs: list = []

        def _fake_analyze(store_, case_nbr, case_id="", subject="", owner_name="", trial=None,
                          case_ctx=None, source="ips"):
            ran.append(case_nbr)
            ctxs.append((case_nbr, source, case_ctx))
            rec = store_.enqueue(case_nbr=case_nbr, case_id="", subject=subject or "IPS subject " + case_nbr,
                                 draft_plain=AI_MARKER + "\n\nanalysis of " + case_nbr, draft_html="<p>a</p>",
                                 confidence=70, mode="full", analysis={}, source=source)
            fr = store_.enqueue(case_nbr=case_nbr, case_id="", subject=subject or "IPS subject " + case_nbr,
                                draft_plain=AI_MARKER + "\n\nchecklist", draft_html="<p>c</p>",
                                confidence=None, mode="first_response", analysis={}, source=source)
            orch._update_case_details(case_nbr, {"fr_draft_id": fr["draft_id"], "draft_id": rec["draft_id"]})
            return rec

        orig_analyze, orig_store = orch._analyze_and_enqueue, orch._store
        orch._analyze_and_enqueue = _fake_analyze
        orch._store = lambda: store
        try:
            res = orch.run_email_scan(store, folder="Inbox", max_cases=3, lookback_days=7, trigger="manual",
                                      own_email="ME@intel.com")
            drafts = store.list_drafts()
            e_drafts = [d for d in drafts if d.get("email")]
            full = [d for d in e_drafts if d["mode"] == "full"]
            st = ol.load_state(store.root)
            check("S19.d round: cases from the newest mail first, one run per case (01031658 once "
                  "although two mails name it), both drafts of a case carry the mail, no-case mail "
                  "skipped and remembered, state + config stamped",
                  res["ok"] and ran == ["01031658", "01032299"]
                  and [c["case_nbr"] for c in res["cases"]] == ["01031658", "01032299"]
                  and res["cases"][0]["sender"] == "Ann"
                  and len(e_drafts) == 4 and all(d["email"]["sender_name"] in ("Ann", "Bob") for d in e_drafts)
                  and next(d for d in full if d["case_nbr"] == "01031658")["email"]["subject"].startswith("Fwd: IPS")
                  and [s_["why"][:9] for s_ in res["skipped"]] == ["names 5 c", "the subje", "automated", "no IPS ca", "automated"]
                  and "01034043" in res["skipped"][1]["why"]
                  and set(st["processed"]) == {"E1", "E2", "E3", "E7", "N1", "N2", "N3"}
                  and st["processed"]["E2"]["case_nbrs"] == ["01032299"]   # 01031658 is only in E2's body: no trigger
                  and store.load_config().get("email_last_result", "").startswith("2 case(s) run")
                  and orch.get_run_state()["status"] == "done",
                  f"ran={ran} cases={res.get('cases')} skipped={res.get('skipped')} "
                  f"drafts={[(d['case_nbr'], d['mode'], bool(d.get('email'))) for d in drafts]} state={sorted(st['processed'])} err={res.get('error')}")
            check("S19.d2 each case ran on its EMAIL THREAD: source email, the context holds the thread "
                  "(oldest first, Sent Items included), its files are local, the draft records say source "
                  "email, the reply target is the newest customer mail, the IPS ledger is NOT marked analyzed",
                  [(c, s_) for c, s_, _ in ctxs] == [("01031658", "email"), ("01032299", "email")]
                  and [m.entry_id for m in ctxs[0][2].messages] == ["E2", "E1", "S1", "E5"]
                  and ctxs[0][2].local_paths.get("WRT_logs.zip", "").endswith("WRT_logs.zip")
                  and [m.entry_id for m in ctxs[1][2].messages] == ["E2"]
                  and all(d.get("source") == "email" for d in e_drafts)
                  and next(d for d in full if d["case_nbr"] == "01031658")["email"]["thread_size"] == 4
                  and not store.is_processed("01031658") and not store.is_processed("01032299")
                  and res["cases"][0]["thread"] == 4,
                  f"ctxs={[(c, s_, [m.entry_id for m in x.messages] if x else None) for c, s_, x in ctxs]} "
                  f"processed={store.is_processed('01031658')} src={[d.get('source') for d in e_drafts]}")
            res2 = orch.run_email_scan(store, folder="Inbox", max_cases=3, lookback_days=7,
                                       own_email="ME@intel.com")
            closed_after_2 = ol.closed_ids(ol.load_state(store.root))
            check("S19.e second round: every mail already handled -> nothing runs; by then every "
                  "mail of the window is closed, so a later scan passes over all of them",
                  res2["ok"] and res2["cases"] == [] and ran == ["01031658", "01032299"]
                  and store.load_config().get("email_last_result") == "no new mail naming a case"
                  and closed_after_2 == {"E1", "E2", "E3", "E7", "N1", "N2", "N3"},
                  f"{res2} closed={sorted(closed_after_2)}")

            # --- f) approve = Outlook reply draft, never sent, IPS untouched ---
            d_full = next(d for d in full if d["case_nbr"] == "01031658")
            rec_full = store.get(d_full["draft_id"])
            store.save_config({"post_backend": "rest", "dry_run": True})
            r_dry = orch.approve_and_post(d_full["draft_id"])
            store.save_config({"dry_run": False})
            r_ok = orch.approve_and_post(d_full["draft_id"], edited_plain="edited analysis text")
            reply = m_new.replies[-1] if m_new.replies else None
            after = store.get(d_full["draft_id"])
            check("S19.f approve on a mail draft: dry run blocks; then ReplyAll().Save() on the "
                  "original mail with the edited text above the quoted thread, never Send, status "
                  "posted with backend outlook, the IPS ledger never marked posted",
                  not r_dry["ok"] and "dry_run" in r_dry["error"]
                  and r_ok["ok"] and reply is not None and reply.all and reply.saved and not reply.sent
                  and "edited analysis text" in reply.HTMLBody and "quoted thread" in reply.HTMLBody
                  and reply.HTMLBody.index("edited analysis text") < reply.HTMLBody.index("quoted thread")
                  and after["status"] == "posted" and after["post_result"]["backend"] == "outlook"
                  and after["post_result"]["comment_id"] == "DRAFT-E1"
                  and after["post_result"]["detail"]["to"] == "a@x; b@x"
                  and ns.opened[-1] == ("E1", "STORE1")
                  and not (store.ledger_entry("01031658") or {}).get("posted_at"),
                  f"dry={r_dry} ok={r_ok.get('ok')} err={r_ok.get('error')} reply={reply and reply.__dict__}")
            d_fr = next(d for d in e_drafts if d["mode"] == "first_response" and d["case_nbr"] == "01032299")
            store.update(d_fr["draft_id"], email={**store.get(d_fr["draft_id"])["email"], "reply_all": False})
            r_fr = orch.approve_and_post(d_fr["draft_id"])
            check("S19.g reply_all False -> Reply() (sender only); a second draft of the case "
                  "is independent (no 'already posted' coupling through IPS)",
                  r_fr["ok"] and m_two.replies and not m_two.replies[-1].all and m_two.replies[-1].saved,
                  str(r_fr))

            # --- r) manual: the email thread of a typed case number ------------
            ran.clear()
            r_man = orch.run_email_case(store, "01034043", own_email="ME@intel.com")
            man = [d for d in store.list_drafts() if d["case_nbr"] == "01034043" and d["mode"] == "full"]
            r_none = orch.run_email_case(store, "01039999", own_email="ME@intel.com")
            check("S19.r manual 'analyze the email thread' of a typed case: the thread is found even "
                  "when no subject says 'IPS' (E4 + E7), runs with source email, the reply answers the "
                  "newest customer mail; an unknown case reports 'no mail names case'",
                  r_man["ok"] and r_man["thread"] == 2 and ran == ["01034043"]
                  and man and man[0]["source"] == "email" and man[0]["email"]["sender_name"] == "Fay"
                  and orch.get_run_state()["mode"] == "email-case",
                  f"man={r_man} none={r_none} drafts={[(d['case_nbr'], d.get('email', {}) or {}) for d in man]}")
            check("S19.r2 unknown case -> clear error, state error",
                  not r_none["ok"] and "names case 01039999" in r_none["error"]
                  and orch.get_run_state()["status"] == "error", str(r_none))

            # --- h) Outlook gone -> post_failed with the message ------------
            ol.Outlook.factory = lambda: (_ for _ in ()).throw(RuntimeError("COM down"))
            d_other = next(d for d in full if d["case_nbr"] == "01032299")
            r_bad = orch.approve_and_post(d_other["draft_id"])
            check("S19.h Outlook unavailable at approve: post_failed, reviewer-readable error, "
                  "draft stays in the queue",
                  not r_bad["ok"] and store.get(d_other["draft_id"])["status"] == "post_failed"
                  and "COM down" in store.get(d_other["draft_id"])["post_result"]["error"],
                  str(r_bad))
            r_scan = orch.run_email_scan(store, folder="Inbox")
            check("S19.i Outlook unavailable at scan: round reports the error, state says error",
                  not r_scan["ok"] and "COM down" in r_scan["error"]
                  and orch.get_run_state()["status"] == "error", str(r_scan))
        finally:
            orch._analyze_and_enqueue, orch._store = orig_analyze, orig_store
    finally:
        ol.Outlook.factory = None

    # --- j) scheduler settings round-trip ------------------------------------
    from . import scheduler as sch
    orig_s = sch._store
    store_s = HandsfreeStore(tmp / "handsfree_s19s")
    sch._store = lambda: store_s
    try:
        store_s.save_config({"owner_name": "Charles"})
        st = sch.configure(False, "4h", max_cases=5, email_watch=True, email_folder=" Inbox/Beacon ",
                           email_lookback_days=99, email_max=0)
        check("S19.j /auto settings: email keys saved and clamped, reported by status()",
              st["email_watch_enabled"] is True and st["email_folder"] == "Inbox/Beacon"
              and st["email_lookback_days"] == 60 and st["email_max"] == 1 and st["email_reply_all"] is True
              and sch.email_settings(store_s.load_config()) == {"folder": "Inbox/Beacon", "max_cases": 1,
                                                                 "lookback_days": 60, "reply_all": True},
              str({k: v for k, v in st.items() if k.startswith("email")}))
        st2 = sch.save_settings(max_cases=4)
        check("S19.k settings absent from a request are kept", st2.get("email_folder") == "Inbox/Beacon"
              and st2.get("email_watch_enabled") is True)
    finally:
        sch._store = orig_s
def smoke_server_role(tmp: Path) -> None:
    print("[S21] Server role: the in-app schedule runs only on the server that pressed Start; "
          "several owners per round")
    import os
    from datetime import datetime, timedelta, timezone
    from configs.global_configs import app_config
    from . import scheduler as sch
    from . import orchestrator as orch
    from .ips_client import CaseRef
    from .queue import HandsfreeStore

    orig_role = getattr(app_config, "handsfree_role", "client")
    orig_env = os.environ.get("INTELAVATAR_HANDSFREE_ROLE")
    orig_store_s, orig_started, orig_run_once = sch._store, sch.ensure_started, sch.run_once
    orig_orch_store, orig_analyze, orig_ips = orch._store, orch._analyze_and_enqueue, orch.IpsClient
    store = HandsfreeStore(tmp / "handsfree_s20")
    sch._store = lambda: store
    orch._store = lambda: store

    def set_role(value, env=None):
        app_config.handsfree_role = value
        if env is None:
            os.environ.pop("INTELAVATAR_HANDSFREE_ROLE", None)
        else:
            os.environ["INTELAVATAR_HANDSFREE_ROLE"] = env

    try:
        # --- a) role resolution ---------------------------------------------
        set_role("server")
        r1 = sch.role()
        set_role("", "SERVER")
        r2 = sch.role()
        set_role("bogus", "server")
        r3 = sch.role()
        set_role("")
        r4 = sch.role()
        check("S21.a role(): app_config wins, env var is the fallback, unknown values are a client",
              (r1, r2, r3, r4) == ("server", "server", "client", "client"), str((r1, r2, r3, r4)))
        # a packaged Client / Server build carries its role in the bundle
        bundle = tmp / "bundle_s21"
        bundle.mkdir(exist_ok=True)
        no_file = sch.bundled_role(str(bundle))
        (bundle / sch.BUNDLED_ROLE_FILE).write_text("Server\n", encoding="utf-8")
        from_file = sch.bundled_role(str(bundle))
        (bundle / sch.BUNDLED_ROLE_FILE).write_text("admin\n", encoding="utf-8")
        unknown = sch.bundled_role(str(bundle))
        check("S21.a2 bundled_role(): handsfree_role.txt in the bundle names the role; missing, "
              "unknown or no bundle (a source checkout) -> ''",
              (no_file, from_file, unknown, sch.bundled_role("")) == ("", "server", "", ""),
              str((no_file, from_file, unknown, sch.bundled_role(""))))

        # --- b) a client cannot start the schedule --------------------------
        set_role("client")
        store.save_config({"owner_name": "Charles"})
        try:
            sch.configure(True, "4h")
            check("S21.b client: Start refused", False, "no ValueError")
        except ValueError as e:
            check("S21.b client: Start refused with the server hint",
                  "client" in str(e) and "--handsfree-role server" in str(e), str(e))
        st_c = sch.status()
        check("S21.b2 client status: can_schedule False, no host yet, Stop still allowed",
              st_c["role"] == "client" and st_c["can_schedule"] is False and st_c["host"] == ""
              and sch.configure(False, "4h")["enabled"] is False)

        # --- c) the server starts it: host pinned, first interval run due now ---
        set_role("server")
        started: list = []
        sch.ensure_started = lambda: started.append(True)
        before = datetime.now().astimezone()
        st = sch.configure(True, "4h", owners=["Ann", " bob ", "ann", ""])
        cfg = store.load_config()
        due = datetime.fromisoformat(cfg["auto_next_run_at"])
        check("S21.c server Start: enabled, host = this machine, owners cleaned, due now, thread asked",
              st["enabled"] is True and st["can_schedule"] is True and st["runs_here"] is True
              and cfg["auto_check_host"] == sch._this_host() and st["host"] == sch._this_host()
              and cfg["auto_owners"] == ["Ann", "bob"] and st["owners"] == ["Ann", "bob"]
              and st["max_owners"] == 10
              and before - timedelta(seconds=2) <= due <= before + timedelta(seconds=30)
              and started == [True],
              f"{ {k: v for k, v in st.items() if k in ('enabled', 'host', 'runs_here', 'owners')} } "
              f"host={cfg.get('auto_check_host')} due={cfg.get('auto_next_run_at')} started={started}")
        st_off = sch.configure(False, "4h")
        check("S21.c2 Stop clears the due time and starts nothing",
              st_off["enabled"] is False and st_off["next_run_at"] == "" and started == [True])

        # --- c3) one scheduler per server, both ways --------------------------
        orig_files_dir = getattr(app_config, "avatarfiles_dir", None)
        files_dir = tmp / "s21files"
        status_file = files_dir / "logs" / "handsfree_server" / "last_status.txt"
        status_file.parent.mkdir(parents=True, exist_ok=True)
        app_config.avatarfiles_dir = str(files_dir)
        try:
            status_file.write_text("2026-11-04 02:00 OK\nlog: x.log\n", encoding="utf-8")
            try:
                sch.configure(True, "4h", owners=["Ann"])
                check("S21.c3 Start refused while the CLI status file is fresh", False, "no ValueError")
            except ValueError as e:
                check("S21.c3 Start refused while the CLI status file is fresh (names the run)",
                      "Task Scheduler CLI" in str(e) and "2026-11-04 02:00 OK" in str(e)
                      and store.load_config().get("auto_check_enabled") is False
                      and sch.status()["cli_last_run"] == "2026-11-04 02:00 OK", str(e))
            import os as _os, time as _time
            old = _time.time() - 2 * 24 * 3600
            _os.utime(status_file, (old, old))
            st_old = sch.configure(True, "4h", owners=["Ann"])
            check("S21.c4 a status file older than a day no longer blocks Start",
                  st_old["enabled"] is True and st_old["cli_last_run"] == "")
            # the CLI side: an owner round steps aside while the page owns the schedule here
            from .scheduler import scheduled_in_app_here
            cfg_on = store.load_config()
            reason_here = scheduled_in_app_here(cfg_on)
            reason_other = scheduled_in_app_here({**cfg_on, "auto_check_host": "another-box"})
            reason_off = scheduled_in_app_here({**cfg_on, "auto_check_enabled": False})
            from . import cli as cli_mod
            check("S21.c5 CLI guard: refuses only when the schedule is ON and pinned to this host; "
                  "exit code 3 is reserved for it",
                  "this server" in reason_here and cfg_on["auto_check_host"] in reason_here
                  and reason_other == "" and reason_off == "" and cli_mod.EXIT_SCHEDULED_IN_APP == 3,
                  f"here={reason_here!r} other={reason_other!r} off={reason_off!r}")
            sch.configure(False, "4h")
        finally:
            app_config.avatarfiles_dir = orig_files_dir

        # --- d) _tick: only the pinned host runs the due round --------------
        ran: list = []
        sch.run_once = lambda trigger="manual", on_start=None: ran.append(trigger) or {"ok": True}
        past = (datetime.now().astimezone() - timedelta(minutes=1)).isoformat(timespec="seconds")
        future = (datetime.now().astimezone() + timedelta(hours=1)).isoformat(timespec="seconds")
        store.save_config({"auto_check_enabled": True, "auto_next_run_at": past,
                           "auto_check_host": "some-other-server"})
        t_other = sch._tick()
        store.save_config({"auto_check_host": sch._this_host()})
        set_role("client")
        t_client = sch._tick()
        set_role("server")
        t_here = sch._tick()
        store.save_config({"auto_next_run_at": future})
        t_early = sch._tick()
        store.save_config({"auto_next_run_at": past, "auto_check_enabled": False})
        t_off = sch._tick()
        check("S21.d _tick: another host / a client / not yet due / disabled -> nothing; "
              "this server with a due round -> one scheduled run",
              (t_other, t_client, t_here, t_early, t_off) == (False, False, True, False, False)
              and ran == ["scheduled"], f"{(t_other, t_client, t_here, t_early, t_off)} ran={ran}")
        sch.run_once = orig_run_once

        # --- e) owners in the target and the settings -----------------------
        from .scheduler import scan_target, _settings_updates
        many = [f"Owner {i}" for i in range(12)]
        check("S21.e scan_target: auto_owners when set (deduped, first = legacy owner), else the "
              "Settings owner; list-view source has none; settings cap at 10",
              scan_target({"owner_name": "C", "auto_owners": ["A", "B", "a"]})
              == {"owner": "A", "owners": ["A", "B"], "list_views": [], "list_view": "", "error": ""}
              and scan_target({"owner_name": "C", "auto_owners": []})["owners"] == ["C"]
              and scan_target({"auto_source": "list_view", "auto_list_views": ["LV"],
                               "auto_owners": ["A"]})["owners"] == []
              and scan_target({"auto_owners": []})["error"] == "no owner name configured"
              and _settings_updates(owners=many) == {"auto_owners": many[:10]}
              and _settings_updates(owners=[]) == {"auto_owners": []},
              str(scan_target({"owner_name": "C", "auto_owners": ["A", "B", "a"]})))

        # --- f) run_once hands the owners to the round ----------------------
        got: dict = {}

        def _fake_scan(store_, owner, max_cases, trigger="scheduled", list_view="",
                       on_start=None, list_views=None, owners=None):
            got.update(owner=owner, owners=owners, list_views=list_views, max_cases=max_cases)
            return {"ok": True, "cases": []}
        orig_scan = orch.run_auto_scan
        orch.run_auto_scan = _fake_scan
        try:
            store.save_config({"auto_check_enabled": False, "auto_source": "owner",
                               "auto_owners": ["Ann", "Bob"], "auto_check_max_cases": 7})
            sch.run_once(trigger="manual")
        finally:
            orch.run_auto_scan = orig_scan
        check("S21.f run_once passes every owner (first one as the legacy owner) and the cap",
              got == {"owner": "Ann", "owners": ["Ann", "Bob"], "list_views": [], "max_cases": 7}, str(got))

        # --- g) one round over two owners: each owner's own window, a shared case once ---
        recent = (datetime.now(timezone.utc) - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%S.000+0000")
        asked: list = []

        class _OwnersIps:
            def find_open_cases(self, owner):
                asked.append(owner)
                if owner == "Ann":
                    return [CaseRef("00000011", "511", "ann's new case", created=recent, owner_name="Ann"),
                            CaseRef("00000012", "512", "shared case", created=recent, owner_name="Ann")]
                assert owner == "Bob", owner
                return [CaseRef("00000012", "512", "shared case", created=recent, owner_name="Bob"),
                        CaseRef("00000013", "513", "bob's new case", created=recent, owner_name="Bob")]

            def find_customer_updates(self, case_ids, since_iso):
                return {}

        analyzed: list = []

        def _fake_analyze(store_, case_nbr, case_id="", subject="", owner_name="", trial=None,
                          case_ctx=None, source="ips"):
            analyzed.append((case_nbr, owner_name))
            return store_.enqueue(case_nbr=case_nbr, case_id=case_id, subject=subject,
                                  draft_plain="x", draft_html="<p>x</p>", confidence=50,
                                  mode="full", analysis={})
        orch.IpsClient = _OwnersIps
        orch._analyze_and_enqueue = _fake_analyze
        store_g = HandsfreeStore(tmp / "handsfree_s20g")
        store_g.save_config({"owner_name": "Ann", "post_backend": "rest",
                             "auto_last_scan_by_owner": {"ann": "2026-10-01T00:00:00Z"},
                             "auto_last_scan_at": "2026-09-01T00:00:00Z"})
        try:
            res = orch.run_auto_scan(store_g, "", max_cases=5, trigger="manual", owners=["Ann", "Bob"])
            events = " | ".join(e["message"] for e in orch.get_run_state().get("events", []))
            cfg_g = store_g.load_config()
            analyzed_g, asked_g = list(analyzed), list(asked)
            analyzed.clear(); asked.clear()
            store_c = HandsfreeStore(tmp / "handsfree_s20c")
            store_c.save_config({"owner_name": "Ann", "post_backend": "rest"})
            res_c = orch.run_auto_scan(store_c, "", max_cases=2, trigger="manual", owners=["Ann", "Bob"])
        finally:
            orch.IpsClient, orch._analyze_and_enqueue = orig_ips, orig_analyze
        stamps = cfg_g.get("auto_last_scan_by_owner") or {}
        check("S21.g two owners, one round: both asked, the shared case runs once under the first owner, "
              "the source names both, each owner gets a fresh stamp, the legacy single stamp is left alone",
              res["ok"] and asked_g == ["Ann", "Bob"]
              and analyzed_g == [("00000011", "Ann"), ("00000012", "Ann"), ("00000013", "Bob")]
              and res["source"] == "open cases of 'Ann', 'Bob'"
              and "owner 'Ann':" in events and "owner 'Bob':" in events
              and "1 already listed under an earlier owner" in events
              and "Ann: 2026-10-01T00:00:00Z" in events
              and stamps.get("ann", "").startswith("20") and stamps.get("ann") != "2026-10-01T00:00:00Z"
              and stamps.get("bob", "").startswith("20")
              and cfg_g.get("auto_last_scan_at") == "2026-09-01T00:00:00Z"
              and orch.get_run_state().get("owner") == "Ann",
              f"asked={asked_g} analyzed={analyzed_g} source={res.get('source')!r} stamps={stamps} "
              f"events={events[:400]}")
        check("S21.g2 the cap applies to the combined list and keeps the window",
              res_c["ok"] and len(res_c["cases"]) == 2 and len(analyzed) == 2
              and "capped" in (store_c.load_config().get("auto_last_result") or ""),
              f"cases={[c['case_nbr'] for c in res_c['cases']]} result={store_c.load_config().get('auto_last_result')}")

        # --- k) owners file source: the CLI's --owners-file, read by the page ---
        from .scheduler import read_owners_file
        of = tmp / "owners.txt"
        of.write_text("\ufeff# team\nAnn\n\n  bob  \nann\nCara\n", encoding="utf-8")
        cfg_f = {"auto_source": "owners_file", "auto_owners_file": str(of), "owner_name": "Zed"}
        t_f = scan_target(cfg_f)
        t_missing = scan_target({**cfg_f, "auto_owners_file": str(tmp / "nope.txt")})
        t_unset = scan_target({"auto_source": "owners_file"})
        st_f = sch.status(cfg_f)
        upd = _settings_updates(source="owners_file", owners_file=" " + str(of) + " ")
        check("S21.k owners file source: BOM, comments and blanks skipped, deduped in file order; "
              "missing / unset file is an error; status and settings carry it",
              read_owners_file(of) == ["Ann", "bob", "Cara"]
              and t_f["owners"] == ["Ann", "bob", "Cara"] and t_f["owner"] == "Ann" and t_f["error"] == ""
              and "not found" in t_missing["error"] and t_unset["error"] == "no owners file configured"
              and st_f["source"] == "owners_file" and st_f["owners_file"] == str(of)
              and st_f["owners_file_owners"] == ["Ann", "bob", "Cara"] and st_f["owners_file_error"] == ""
              and upd == {"auto_source": "owners_file", "auto_owners_file": str(of)}
              and _settings_updates(source="bogus") == {"auto_source": "owner"},
              f"t_f={t_f} missing={t_missing['error']!r} unset={t_unset['error']!r} upd={upd}")
        # a scheduled round over the file: owners handed over, cleanup afterwards; a
        # vanished file moves the schedule on instead of retrying every tick
        cleaned: list = []
        orig_cleanup = orch.cleanup_queue
        orch.cleanup_queue = lambda store_: cleaned.append(True) or {"drafts_removed": 0, "log_folders_removed": 0}
        orch.run_auto_scan = _fake_scan
        try:
            got.clear()
            store.save_config({"auto_check_enabled": True, "auto_check_mode": "4h",
                               "auto_check_host": sch._this_host(), "auto_source": "owners_file",
                               "auto_owners_file": str(of), "auto_next_run_at": past})
            res_f = sch.run_once(trigger="scheduled")
            cfg_after = store.load_config()
            store.save_config({"auto_owners_file": str(tmp / "gone.txt"), "auto_next_run_at": past})
            res_gone = sch.run_once(trigger="scheduled")
            cfg_gone = store.load_config()
            got_manual = {}
            store.save_config({"auto_owners_file": str(of)})
            cleaned_before = len(cleaned)
            sch.run_once(trigger="manual")
            cleaned_manual = len(cleaned) - cleaned_before
        finally:
            orch.run_auto_scan, orch.cleanup_queue = orig_scan, orig_cleanup
            store.save_config({"auto_check_enabled": False, "auto_source": "owner", "auto_next_run_at": ""})
        now_local = datetime.now().astimezone()
        check("S21.k2 scheduled round over the owners file: every owner handed to the round, cleanup once "
              "(not for Run now); a missing file -> failed result and the next slot, no hot retry",
              res_f.get("ok") and got.get("owners") == ["Ann", "bob", "Cara"] and got.get("owner") == "Ann"
              and res_f.get("cleanup") == {"drafts_removed": 0, "log_folders_removed": 0} and len(cleaned) == 1
              and datetime.fromisoformat(cfg_after["auto_next_run_at"]) > now_local
              and res_gone.get("ok") is False and "not found" in res_gone.get("error", "")
              and str(cfg_gone.get("auto_last_result", "")).startswith("failed: owners file not found")
              and datetime.fromisoformat(cfg_gone["auto_next_run_at"]) > now_local
              and cleaned_manual == 0,
              f"res_f={res_f} got={got} cleaned={cleaned} next={cfg_after.get('auto_next_run_at')} "
              f"gone={res_gone} last={cfg_gone.get('auto_last_result')!r} manual_cleanups={cleaned_manual}")

        # --- h) the route: 403 for a client, the server starts it -----------
        try:
            from flask import Flask
            from blueprints.handsfree.handsfree_routes import handsfree_bp
            set_role("client")
            app = Flask("handsfree_smoke")
            app.register_blueprint(handsfree_bp)
            client = app.test_client()
            r_client = client.post("/handsfree/auto", json={"enabled": True, "mode": "4h"})
            r_settings = client.post("/handsfree/auto", json={"mode": "4h", "max_cases": 3})
            set_role("server")
            store.save_config({"owner_name": "Charles", "auto_owners": []})
            r_server = client.post("/handsfree/auto", json={"enabled": True, "mode": "4h",
                                                             "owners": ["Charles", "Dana"]})
            r_get = client.get("/handsfree/auto")
            sch.configure(False, "4h")
            check("S21.h /auto: a client gets 403 on Start (settings-only POST still fine); the server "
                  "starts it and GET reports role/host/owners",
                  r_client.status_code == 403 and "client" in r_client.get_json()["error"]
                  and r_settings.status_code == 200
                  and r_server.status_code == 200 and r_server.get_json()["enabled"] is True
                  and r_get.get_json()["role"] == "server" and r_get.get_json()["owners"] == ["Charles", "Dana"]
                  and r_get.get_json()["host"] == sch._this_host(),
                  f"client={r_client.status_code} settings={r_settings.status_code} "
                  f"server={r_server.status_code} {r_server.get_json()} get={r_get.get_json().get('role')}")
        except Exception as e:
            check("S21.h /auto route role check", False, f"{type(e).__name__}: {e}")
    finally:
        app_config.handsfree_role = orig_role
        if orig_env is None:
            os.environ.pop("INTELAVATAR_HANDSFREE_ROLE", None)
        else:
            os.environ["INTELAVATAR_HANDSFREE_ROLE"] = orig_env
        sch._store, sch.ensure_started, sch.run_once = orig_store_s, orig_started, orig_run_once
        orch._store, orch._analyze_and_enqueue, orch.IpsClient = orig_orch_store, orig_analyze, orig_ips


def smoke_identity() -> None:
    print("[S19] IPS sign-in identity: whoami, parsing, author read-back, login state")
    from .ips_client import IpsClient, parse_identity

    oidc = parse_identity({"user_id": "005A", "name": "Charles P Chu", "email": "c@intel.com",
                           "preferred_username": "c@intel.com.prod"})
    chatter = parse_identity({"id": "005B", "displayName": "Wesley Kuo", "email": "w@intel.com",
                              "username": "w@intel.com"})
    partial = parse_identity({"user_id": "005C", "given_name": "Ching-Fang", "family_name": "Lai"})
    check("S19.a userinfo, chatter and name-part answers normalise to one shape",
          oidc == {"user_id": "005A", "name": "Charles P Chu", "email": "c@intel.com",
                   "username": "c@intel.com.prod"}
          and chatter["name"] == "Wesley Kuo" and chatter["user_id"] == "005B"
          and chatter["username"] == "w@intel.com" and partial["name"] == "Ching-Fang Lai",
          str((oidc, chatter, partial)))
    check("S19.b empty answers carry no name",
          parse_identity({})["name"] == "" and parse_identity(None)["name"] == "")

    class _Resp:
        def __init__(self, status, data):
            self.status_code, self._d = status, data

        def json(self):
            return self._d

        def raise_for_status(self):
            if self.status_code >= 400:
                raise RuntimeError(f"HTTP {self.status_code}")

    calls: list = []

    class _Client(IpsClient):
        header = "Bearer sid-1"
        answers: dict = {}

        def _auth(self):
            return None, {"Authorization": self.header}, "62.0"

        def _request(self, method, path, **kw):
            calls.append(path)
            return self.answers.get(path) or _Resp(404, {})

    IpsClient._identity_cache = None
    c = _Client()
    _Client.answers = {"/services/oauth2/userinfo":
                       _Resp(200, {"user_id": "005A", "name": "Charles P Chu", "email": "c@intel.com"})}
    who, who2 = c.whoami(), c.whoami()
    check("S19.c whoami: userinfo answers; cached per session (one GET for two calls)",
          who["name"] == "Charles P Chu" and who2 == who
          and calls.count("/services/oauth2/userinfo") == 1, str(calls))
    _Client.header = "Bearer sid-2"
    _Client.answers = {"/services/data/v{ver}/chatter/users/me":
                       _Resp(200, {"id": "005B", "displayName": "Wesley Kuo"})}
    who3 = c.whoami()
    check("S19.d a new session is re-resolved; chatter/users/me is the fallback",
          who3["name"] == "Wesley Kuo"
          and calls.count("/services/data/v{ver}/chatter/users/me") == 1, str(calls))
    _Client.header = "Bearer sid-3"
    _Client.answers = {"/services/oauth2/userinfo": _Resp(200, {"user_id": "005X"})}
    try:
        c.whoami()
        check("S19.e an answer without a user name is rejected", False)
    except RuntimeError:
        check("S19.e an answer without a user name is rejected", True)

    seen: dict = {}

    class _Author(_Client):
        def _request(self, method, path, **kw):
            seen["q"] = (kw.get("params") or {}).get("q", "")
            return _Resp(200, {"records": [{"Id": "a0X", "CreatedById": "005A",
                                            "CreatedBy": {"Name": "Charles P Chu"}}]})

    author = _Author().comment_author("a0X'1")
    check("S19.f comment_author reads CreatedBy back; the id is SOQL-escaped",
          author == {"user_id": "005A", "name": "Charles P Chu"}
          and "Id = 'a0X\\'1'" in seen["q"] and "CreatedBy.Name" in seen["q"], seen.get("q"))
    check("S19.g comment_author without an id is {}", _Author().comment_author("") == {})
    IpsClient._identity_cache = None

    from . import login
    orig_whoami = login._whoami
    try:
        login._whoami = lambda: {"user_id": "005A", "name": "Charles P Chu", "email": "", "username": ""}
        st = login.start_sign_in(False)
        login._worker.join(5)
        st2 = login.status()
        check("S19.h silent sign-in: signing_in -> signed_in with the identity",
              st["status"] in ("signing_in", "signed_in") and st2["status"] == "signed_in"
              and st2["identity"]["name"] == "Charles P Chu", str(st2))

        def _boom():
            raise RuntimeError("Salesforce SSO did not complete")
        login._whoami = _boom
        login.start_sign_in(False)
        login._worker.join(5)
        st3 = login.status()
        check("S19.i failed sign-in: status failed, the error kept, no identity",
              st3["status"] == "failed" and "SSO" in st3["error"] and st3["identity"] is None,
              str(st3))
        try:
            login.current_identity()
            check("S19.j current_identity raises when there is no session", False)
        except RuntimeError:
            check("S19.j current_identity raises when there is no session", True)
    finally:
        login._whoami = orig_whoami
        with login._lock:
            login._state.update(status="signed_out", identity=None, error="")


# ---------------------------------------------------------------- S20
def smoke_snowflake_reconnect() -> None:
    print("[S20] Snowflake: an expired session is re-opened once, other errors propagate")
    import services.snowflake_service as sf
    try:
        from snowflake.connector.errors import ProgrammingError
    except Exception:  # the connector is an app dependency; stand in if absent
        class ProgrammingError(Exception):
            def __init__(self, msg="", errno=None, sqlstate=None):
                super().__init__(msg)
                self.msg, self.errno, self.sqlstate = msg, errno, sqlstate

    expired = ProgrammingError(msg="Authentication token has expired.  The user must "
                                   "authenticate again.", errno=390114, sqlstate="08001")
    check("S20.a 390114 / 'token has expired' count as an expired session; a SQL "
          "error does not",
          sf._session_expired(expired)
          and sf._session_expired(RuntimeError("Session no longer exists"))
          and not sf._session_expired(ProgrammingError(msg="SQL compilation error",
                                                       errno=1003, sqlstate="42000")))

    class _Cursor:
        def __init__(self, conn):
            self.conn = conn

        def execute(self, sql):
            self.conn.executed.append(sql)
            if self.conn.fail_with is not None:
                exc, self.conn.fail_with = self.conn.fail_with, None
                raise exc

        def fetchone(self):
            return ("row-from", self.conn.n)

        def fetchall(self):
            return [("row-from", self.conn.n)]

        def close(self):
            pass

    class _Conn:
        def __init__(self, n, fail_with):
            self.n, self.fail_with, self.executed, self.closed = n, fail_with, [], False

        def cursor(self):
            return _Cursor(self)

        def is_closed(self):
            return self.closed

        def close(self):
            self.closed = True

    made: list = []
    plan: list = []

    def _fake_connect(passwd, use_privatelink):
        made.append(_Conn(len(made) + 1, plan.pop(0) if plan else None))
        return made[-1]

    saved = (sf._connect, sf._snowflake_conn, sf._snowflake_passwd, sf._snowflake_conn_mode)
    try:
        sf._connect = _fake_connect
        sf._snowflake_conn = sf._snowflake_passwd = sf._snowflake_conn_mode = None

        plan[:] = [expired]           # first connection: first query hits an expired session
        row = sf.snowflake_query("pw", "select 1", "S", fetch_mode="one")
        check("S20.b the query is answered by a fresh connection after the expired one is dropped",
              row == ("row-from", 2) and len(made) == 2 and made[0].closed
              and sf._snowflake_conn is made[1], str((row, len(made))))
        row2 = sf.snowflake_query("pw", "select 2", "S", fetch_mode="one")
        check("S20.c the fresh connection is the cached one afterwards",
              row2 == ("row-from", 2) and len(made) == 2 and made[1].executed == ["select 1", "select 2"])

        made[1].fail_with = ProgrammingError(msg="SQL compilation error", errno=1003, sqlstate="42000")
        try:
            sf.snowflake_query("pw", "select bad", "S", fetch_mode="one")
            check("S20.d a non-session error propagates without a reconnect", False)
        except ProgrammingError as e:
            check("S20.d a non-session error propagates without a reconnect",
                  e.errno == 1003 and len(made) == 2 and sf._snowflake_conn is made[1])

        plan[:] = [expired]           # expired twice in a row: the retry's error surfaces
        made[1].fail_with = expired
        try:
            sf.snowflake_query("pw", "select 3", "S", fetch_mode="one")
            check("S20.e only one reconnect is attempted; a second expiry surfaces", False)
        except ProgrammingError as e:
            check("S20.e only one reconnect is attempted; a second expiry surfaces",
                  e.errno == 390114 and len(made) == 3, str(len(made)))
    finally:
        sf._connect, sf._snowflake_conn, sf._snowflake_passwd, sf._snowflake_conn_mode = saved


# ---------------------------------------------------------------- S22
def smoke_chatbot_handoff(tmp: Path) -> None:
    print("[S22] Hand-off to Avatar: the Log Chatbot on the decoded log (shared copy first); "
          "no log -> 404 'no log to analysis'; localhost only")
    from flask import Flask
    from blueprints.handsfree import handsfree_routes as routes
    from .queue import HandsfreeStore

    store = HandsfreeStore(tmp / "handsfree_s22")
    orig = (routes._store, routes._handoff_to_log_chatbot)
    calls: list = []
    routes._store = lambda: store
    routes._handoff_to_log_chatbot = lambda case_nbr, src, log_path: (
        calls.append(("log", case_nbr, log_path)) or {"ok": True, "mode": "log", "url": "/log_chatbot/?x"})
    try:
        app = Flask("handsfree_smoke_s22")
        app.secret_key = "s22"
        app.register_blueprint(routes.handsfree_bp)
        client = app.test_client()
        post = lambda body, addr="127.0.0.1": client.post(
            "/handsfree/chatbot_handoff", json=body, environ_base={"REMOTE_ADDR": addr})

        # a) a waiting-on-the-customer draft: no log at all -> the case page
        rec_a = store.enqueue(case_nbr="01000001", case_id="500A", subject="s",
                              draft_plain="b", draft_html="<p>b</p>", confidence=None,
                              mode="waiting_customer", analysis={"log_path": "", "case_id": "500A"})
        r = post({"case_nbr": "01000001", "draft_id": rec_a["draft_id"]})
        check("S22.a no decoded log -> 404 'no log to analysis', nothing seeded",
              r.status_code == 404 and r.get_json()["ok"] is False
              and r.get_json()["error"] == routes.NO_LOG_TO_ANALYZE and calls == [],
              f"{r.status_code} {r.get_json()} {calls}")

        # b) the analysis' log is on the server's disk (gone here); the shared copy wins
        shared = tmp / "handsfree_s22" / "logs" / "01000003" / "decoded.txt"
        shared.parent.mkdir(parents=True, exist_ok=True)
        shared.write_text("log", encoding="utf-8")
        rec_b = store.enqueue(case_nbr="01000003", case_id="500C", subject="s",
                              draft_plain="b", draft_html="<p>b</p>", confidence=80, mode="full",
                              analysis={"log_path": r"C:\server-only\decoded.txt", "case_id": "500C",
                                        "incidents": [{"sent_for_analysis": {"issue_time_log": "2026-11-01 10:00:00"}}]},
                              shared_log_path=str(shared))
        r = post({"case_nbr": "01000003", "draft_id": rec_b["draft_id"]})
        check("S22.b decoded log (shared copy, not the server-local path) -> the Log Chatbot",
              r.status_code == 200 and r.get_json()["mode"] == "log"
              and calls[-1] == ("log", "01000003", str(shared)),
              f"{r.status_code} {r.get_json()} {calls[-1]}")
        src = routes._handoff_source("01000003", rec_b["draft_id"])
        check("S22.b2 the hand-off source carries the issue time in the log's clock",
              src["issue_time_log"] == "2026-11-01 10:00:00" and src["case_id"] == "500C", str(src))

        # c) a log path that no longer exists anywhere -> the case page, not a 404
        rec_c = store.enqueue(case_nbr="01000004", case_id="500D", subject="s",
                              draft_plain="b", draft_html="<p>b</p>", confidence=80, mode="full",
                              analysis={"log_path": str(tmp / "gone" / "decoded.txt")})
        n_before = len(calls)
        r = post({"case_nbr": "01000004", "draft_id": rec_c["draft_id"]})
        check("S22.c a decoded log that is gone -> 404 'no log to analysis'",
              r.status_code == 404 and r.get_json()["error"] == routes.NO_LOG_TO_ANALYZE
              and len(calls) == n_before, f"{r.status_code} {r.get_json()}")

        # d) a run that failed before any log -> the same answer
        rec_d = store.enqueue(case_nbr="01000002", case_id="", subject="s",
                              draft_plain="b", draft_html="<p>b</p>", confidence=None,
                              mode="error", analysis={})
        r = post({"case_nbr": "01000002", "draft_id": rec_d["draft_id"]})
        check("S22.d an error draft (no log) -> 404 'no log to analysis'",
              r.status_code == 404 and r.get_json()["ok"] is False
              and r.get_json()["error"] == routes.NO_LOG_TO_ANALYZE, f"{r.status_code} {r.get_json()}")

        # e) another machine's browser cannot seed this app's session
        n = len(calls)
        r = post({"case_nbr": "01000001", "draft_id": rec_a["draft_id"]}, addr="10.1.2.3")
        check("S22.e not from localhost -> 403, nothing seeded",
              r.status_code == 403 and len(calls) == n, f"{r.status_code} {len(calls) - n}")
        r = post({"case_nbr": "nope"})
        check("S22.f a non-case number -> 400", r.status_code == 400, str(r.status_code))
    finally:
        routes._store, routes._handoff_to_log_chatbot = orig


# ---------------------------------------------------------------- S23
def smoke_case_requests(tmp: Path) -> None:
    print("[S23] Case requests: a client asks, the server runs it on its tick, the draft lands in the queue")
    import socket
    import time as _time
    from flask import Flask
    from configs.global_configs import app_config
    from blueprints.handsfree import handsfree_routes as routes
    from . import scheduler as sch
    from . import orchestrator as orch
    from .queue import HandsfreeStore

    store = HandsfreeStore(tmp / "handsfree_s23")
    orig_role = getattr(app_config, "handsfree_role", "client")
    orig = (routes._store, sch._store, orch.start_case_run, orch.get_run_state)
    routes._store = lambda: store
    sch._store = lambda: store
    try:
        # a) the store: pending -> running (one winner) -> done; a repeat request after done
        req = store.request_case("01000011", requested_by="Ann")
        twice = store.request_case("01000011", requested_by="Bob")
        pend = store.pending_requests()
        c1 = store.claim_request("01000011", "srv-a")
        c2 = store.claim_request("01000011", "srv-b")
        again = store.request_case("01000011", requested_by="Cara")   # running: not queued twice
        fin = store.finish_request("01000011", ok=True, draft_id="01000011__x")
        after = store.list_requests()
        re_req = store.request_case("01000011", requested_by="Dan")
        check("S23.a request lifecycle on the share: one pending file per case, the claim rename has one "
              "winner, a running case is not queued twice, done keeps the result, a new request replaces it",
              req["status"] == "pending" and twice["status"] == "pending"
              and [r["case_nbr"] for r in pend] == ["01000011"]
              and c1 and c1["status"] == "running" and c1["host"] == "srv-a" and c2 is None
              and again["status"] == "running" and again["host"] == "srv-a"
              and fin["status"] == "done" and fin["ok"] is True and fin["draft_id"] == "01000011__x"
              and [(r["case_nbr"], r["status"]) for r in after] == [("01000011", "done")]
              and re_req["status"] == "pending" and store.pending_requests()[0]["requested_by"] == "Dan"
              and not store._request_file("01000011", "done").exists(),
              f"req={req} c1={c1} c2={c2} again={again} fin={fin} after={after} re={re_req}")
        store.unclaim_request("01000011")        # pending stays pending
        store.claim_request("01000011", "srv-a")
        store.unclaim_request("01000011")
        check("S23.a2 unclaim puts a running request back to pending",
              [r["status"] for r in store.list_requests()] == ["pending"]
              and not store._request_file("01000011", "running").exists())
        store._request_file("01000011", "pending").unlink()     # not part of the runs below
        old = store._request_file("01000099", "done")
        old.write_text('{"case_nbr": "01000099", "requested_at": "2026-01-01T00:00:00+00:00"}', encoding="utf-8")
        past = _time.time() - 8 * 24 * 3600
        import os as _os
        _os.utime(old, (past, past))
        check("S23.a3 finished requests older than a week are dropped from the listing and the share",
              all(r["case_nbr"] != "01000099" for r in store.list_requests()) and not old.exists())

        # a4) the server finishes while a client's page is reading the running record
        #     (Windows: the delete fails) — what left 00997200 "running" forever on 6 Nov 2026
        import threading as _thr
        from . import queue as _queue
        store.request_case("01000021", requested_by="YI-YUN CHIEN")
        store.claim_request("01000021", "srv-a")
        held = open(store._request_file("01000021", "running"), "r", encoding="utf-8")
        try:
            fin = store.finish_request("01000021", ok=True, draft_id="01000021__x")
            raised = ""
        except Exception as e:                        # the old code: PermissionError out of the tick
            fin, raised = None, f"{type(e).__name__}: {e}"
        leftover = store._request_file("01000021", "running").exists()
        listed_open = [(r["status"], r.get("ok")) for r in store.list_requests()
                       if r["case_nbr"] == "01000021"]
        re_req_open = store.request_case("01000021", requested_by="YI-YUN CHIEN")
        held.close()
        listed = [(r["status"], r.get("ok")) for r in store.list_requests() if r["case_nbr"] == "01000021"]
        check("S23.a4 a running record the server could not delete (a reader held it open) is stale: "
              "finish_request does not raise, the listing shows the done result only, and the case "
              "can be requested again",
              fin is not None and raised == "" and fin["ok"] is True and leftover
              and listed_open == [("done", True)]
              and re_req_open["status"] == "pending"
              and listed == [("pending", None)]
              and not store._request_file("01000021", "running").exists(),
              f"raised={raised!r} leftover={leftover} open={listed_open} re={re_req_open.get('status')} after={listed}")
        store._request_file("01000021", "pending").unlink()

        # a5) a running record with no result for over two hours: the server stopped mid-run
        store.request_case("01000022", requested_by="Bob")
        store.claim_request("01000022", "srv-a")
        lost_path = store._request_file("01000022", "running")
        rec = json.loads(lost_path.read_text(encoding="utf-8"))
        rec["started_at"] = "2026-01-01T00:00:00+00:00"
        lost_path.write_text(json.dumps(rec), encoding="utf-8")
        lost = [r for r in store.list_requests() if r["case_nbr"] == "01000022"]
        check("S23.a5 a running record older than two hours without a result becomes a failed done "
              "record (the requester sees why) and its running file goes",
              [(r["status"], r.get("ok")) for r in lost] == [("done", False)]
              and lost[0]["error"] == store.REQUEST_LOST_ERROR and lost[0]["host"] == "srv-a"
              and not lost_path.exists() and store._request_file("01000022", "done").exists(),
              str(lost))
        live = store.request_case("01000023", requested_by="Cara")
        store.claim_request("01000023", "srv-a")
        same = store.request_case("01000023", requested_by="Dan")
        check("S23.a6 a live running record (fresh, no result yet) is still honoured: not queued twice",
              same["status"] == "running" and same["requested_by"] == "Cara"
              and [r["status"] for r in store.list_requests() if r["case_nbr"] == "01000023"] == ["running"],
              str(same))
        store._request_file("01000023", "running").unlink()
        store._request_file("01000022", "done").unlink()

        # a7) _atomic_write: a reader holding the target open for a moment no longer fails the write
        target = store.requests_dir / "retry_probe.json"
        target.write_text("old", encoding="utf-8")
        reader = open(target, "r", encoding="utf-8")
        _thr.Timer(0.5, reader.close).start()
        try:
            _queue._atomic_write(target, "new")
            wrote = target.read_text(encoding="utf-8")
        except Exception as e:
            wrote = f"{type(e).__name__}: {e}"
        finally:
            if not reader.closed:
                reader.close()
        check("S23.a7 _atomic_write retries while another process has the target open, "
              "and leaves no temp file behind",
              wrote == "new" and not list(store.requests_dir.glob(".retry_probe.json.*.tmp")), wrote)
        target.unlink()

        # b) the server serves pending requests on its tick, through the single-case run
        app_config.handsfree_role = "server"
        runs: list = []
        states: list = []

        def fake_start(case_nbr):
            runs.append(case_nbr)
            if case_nbr == "01000013":
                return {"ok": False, "error": "a run is already in progress"}
            if case_nbr == "01000014":
                return {"ok": False, "error": "'x' is not an IPS case number"}
            states[:] = [{"status": "running"},
                         {"status": "done", "case_details": {case_nbr: {"draft_id": case_nbr + "__d"}}}]
            return {"ok": True, "case_nbr": case_nbr}

        def fake_state():
            return states.pop(0) if len(states) > 1 else (states[0] if states else {"status": "idle"})
        orch.start_case_run, orch.get_run_state = fake_start, fake_state
        # the owner lookup itself: IPS answers, or cannot be asked
        class _OwnerIps:
            def __init__(self, *a, **k): pass
            def case_owner_name(self, case_id):
                if case_id == "500X":
                    raise RuntimeError("IPS down")
                return "Zed Owner" if case_id == "500Z" else ""
        orig_ips = orch.IpsClient
        orch.IpsClient = _OwnerIps
        try:
            got_owner = (orch._case_owner("500Z"), orch._case_owner("500Q"),
                         orch._case_owner("500X"), orch._case_owner(""))
        finally:
            orch.IpsClient = orig_ips
        check("S23.d _case_owner: IPS's Owner.Name by case id; unknown, failing or empty id -> ''",
              got_owner == ("Zed Owner", "", "", ""), str(got_owner))

        store.request_case("01000012", requested_by="Ann")
        _time.sleep(0.01)
        store.request_case("01000014", requested_by="Ann")
        n = sch.run_requests()
        done = {r["case_nbr"]: r for r in store.list_requests()}
        check("S23.b run_requests (server): oldest first, each through start_case_run, waits for the run, "
              "records ok + draft id, a bad case number fails its request",
              n == 1 and runs == ["01000012", "01000014"]
              and done["01000012"]["status"] == "done" and done["01000012"]["ok"] is True
              and done["01000012"]["draft_id"] == "01000012__d" and done["01000012"]["host"] == socket.gethostname().casefold()
              and done["01000014"]["status"] == "done" and done["01000014"]["ok"] is False
              and "not an IPS case number" in done["01000014"]["error"],
              f"n={n} runs={runs} done={ {k: (v['status'], v.get('ok'), v.get('error')) for k, v in done.items()} }")
        runs.clear()
        store.request_case("01000013", requested_by="Bob")   # the run lock is busy -> back to pending
        n2 = sch.run_requests()
        check("S23.b2 a request the run lock refuses goes back to pending for the next tick",
              n2 == 0 and runs == ["01000013"]
              and [(r["case_nbr"], r["status"]) for r in store.pending_requests()] == [("01000013", "pending")],
              f"n2={n2} runs={runs} pend={store.pending_requests()}")
        store._request_file("01000013", "pending").unlink()
        app_config.handsfree_role = "client"
        store.request_case("01000015", requested_by="Ann")
        runs.clear()
        check("S23.b3 a client never serves requests", sch.run_requests() == 0 and runs == [])
        store._request_file("01000015", "pending").unlink()

        # c) the routes: a client's "Analyze this case" becomes a request; the server runs it
        app = Flask("handsfree_smoke_s23")
        app.register_blueprint(routes.handsfree_bp)
        client = app.test_client()
        app_config.handsfree_role = "client"
        r_c = client.post("/handsfree/run_case", json={"case_nbr": "1000016"})
        r_bad = client.post("/handsfree/run_case", json={"case_nbr": "abc"})
        r_list = client.get("/handsfree/requests")
        app_config.handsfree_role = "server"
        runs.clear()
        r_s = client.post("/handsfree/run_case", json={"case_nbr": "01000017"})
        check("S23.c /run_case: client -> request on the share (padded case number, who asked), "
              "bad number refused; server -> runs here; /requests lists them",
              r_c.status_code == 200 and r_c.get_json()["requested"] is True
              and r_c.get_json()["request"]["case_nbr"] == "01000016"
              and store._request_file("01000016", "pending").exists()
              and r_bad.get_json()["ok"] is False and "not an IPS case number" in r_bad.get_json()["error"]
              and [i["case_nbr"] for i in r_list.get_json()["items"] if i["status"] == "pending"] == ["01000016"]
              and r_list.get_json()["role"] == "client"
              and r_s.get_json().get("ok") is True and runs == ["01000017"]
              and not store._request_file("01000017", "pending").exists(),
              f"c={r_c.get_json()} bad={r_bad.get_json()} list={r_list.get_json()} s={r_s.get_json()} runs={runs}")

        # d) the approve route passes the reviewer's visibility choice through
        seen: list = []
        orig_approve = orch.approve_and_post
        orch.approve_and_post = lambda draft_id, edited_plain=None, visibility=None: (
            seen.append((draft_id, edited_plain, visibility)) or {"ok": True})
        try:
            client.post("/handsfree/queue/D1/approve", json={"draft_plain": "t", "visibility": "external"})
            client.post("/handsfree/queue/D2/approve", json={"draft_plain": "t"})
        finally:
            orch.approve_and_post = orig_approve
        check("S23.e /queue/<id>/approve: visibility from the body reaches approve_and_post; "
              "absent -> None (internal)",
              seen == [("D1", "t", "external"), ("D2", "t", None)], str(seen))
    finally:
        app_config.handsfree_role = orig_role
        routes._store, sch._store, orch.start_case_run, orch.get_run_state = orig


def smoke_driver_release(tmp: Path) -> None:
    print("[S24] Latest driver: the report's PV release vs the customer's driver; the drafts "
          "ask an outdated customer to validate on it")
    import os
    import types
    from datetime import date, datetime, timedelta
    from configs.global_configs import app_config
    from . import driver_release as dr
    from . import runner as runner_mod
    from . import scheduler as sch
    from . import orchestrator as orch
    from .checklist import _blank_fills, build_fills, deterministic_fills, llm_fill
    from .composer import compose
    from .queue import HandsfreeStore
    from .runner import CaseAnalysis, HandsfreeRunner

    # --- a) Intel work week -------------------------------------------------
    ww = (dr.intel_ww(date(2026, 11, 9)), dr.intel_ww(date(2025, 1, 1)),
          dr.intel_ww(date(2024, 12, 29)), dr.intel_ww(date(2024, 12, 28)),
          dr.intel_ww(date(2024, 11, 13)))
    check("S24.a intel_ww: Sunday-start weeks, WW1 holds Jan 1 (2026-11-09 -> WW46'26, "
          "2024-12-29 -> WW1'25, 2024-12-28 -> WW52'24, 2024-11-13 -> WW46'24)",
          ww == ((2026, 46), (2025, 1), (2025, 1), (2024, 52), (2024, 46)), str(ww))

    # --- b) versions and branches ------------------------------------------
    pv = (dr.parse_version("MHWFW000813_24.90.0.3"), dr.parse_version("Beta 24.95"),
          dr.parse_version("Beta 24.95", 4), dr.parse_version("no version"),
          dr.format_version(dr.parse_version("24.90.0.1")),
          dr.branch_of(dr.parse_version("24.90.0.1")) == dr.branch_of(dr.parse_version("24.90.0.3")))
    check("S24.b parse_version / branch_of: first x.y[.z[.w]] in the text, 4 parts when asked, "
          "the branch is the first two numbers",
          pv == ((24, 90, 0, 3), (24, 95, 0, 0), None, None, "24.90.0.1", True), str(pv))

    # --- c) pick_latest on report rows -------------------------------------
    def _row(s):
        return dict(zip(dr.COLUMNS, [c.strip() for c in s.split("|")]))
    rows = [_row(s) for s in (
        "45 | 2026 | 24.90.0.3 |  | PV | MHWFW000813_24.90.0.3 | Complete |  | All-OEMs | ",
        "45 | 2026 | 24.90.0.1 |  | Beta | MHWFW000694_24.90.0.1 | Complete |  | All-OEMs | ",
        "46 | 2024 | 23.90.1.3 | 91 | PV | SBHWFW01547_23.90.1.3 | Progress |  | All-OEMs | YES",
        "31 | 2024 | 23.60.5.14 | 88 | PV | SBHWFW01244_23.60.5.14 | Complete |  | All-OEMs | YES",
        "30 | 2024 | 23.70.0.6 | 89 | PV | SBHWFW01258_23.70.0.6 | Complete |  | All-OEMs | YES",
        "43 | 2025 | 24.100.0.1 | 98 | PV Candidate | MHWFW00101_24.100.0.1 | Complete |  | All-OEMs | YES",
        "50 | 2026 | 25.10.0.1 |  | PV | X_25.10.0.1 | Complete |  | All-OEMs | ",
        "44 | 2026 | 24.80.1.9 |  | PV-HF | X_24.80.1.9 | Cmplete |  | All-OEMs | ",
        "09 | 2026 | 99.0.102.55 | 102 | Alpha | CRWFW11879_99.0.102.55 | Complete |  | All-OEMs | YES",
        "xx | 2026 | 24.95.0.1 |  | PV | X | Complete |  | All-OEMs | ",
    )]
    p_now = dr.pick_latest(rows, date(2026, 11, 9))
    p_2024_08 = dr.pick_latest(rows, date(2024, 8, 1))
    p_2024_ww46 = dr.pick_latest(rows, date(2024, 11, 13))
    p_2024_ww42 = dr.pick_latest(rows, date(2024, 10, 16))
    p_none = dr.pick_latest([_row("01 | 2026 | 1.2.3.4 |  | Beta | x | Complete |  |  | ")], date(2026, 11, 9))
    check("S24.c pick_latest: PV / PV-HF rows up to the current work week, highest version wins "
          "(not the latest week: 23.60.5.14 re-released WW31'24 never beats 23.70); Beta / Alpha / "
          "'PV Candidate' / future / unparsable rows are excluded; Progress counts",
          p_now and p_now["version"] == "24.90.0.3" and p_now["candidates"] == 5
          and p_now["excluded"] == {"quality": 3, "future": 1, "no_week": 1, "no_version": 0}
          and p_2024_08["version"] == "23.70.0.6" and p_2024_ww46["version"] == "23.90.1.3"
          and p_2024_ww42["version"] == "23.70.0.6" and p_none is None
          and dr.pick_latest([], date(2026, 11, 9)) is None,
          f"now={p_now} 2024-08={p_2024_08 and p_2024_08['version']} "
          f"ww46'24={p_2024_ww46 and p_2024_ww46['version']} ww42'24={p_2024_ww42 and p_2024_ww42['version']}")

    # --- d) compare_branch -------------------------------------------------
    cmp = (dr.compare_branch("24.90.0.1", "24.90.0.3"), dr.compare_branch("24.80.3.1", "24.90.0.3"),
           dr.compare_branch("24.95", "24.90.0.3"), dr.compare_branch("", "24.90.0.3"),
           dr.compare_branch("24.1.1.1", ""), dr.compare_branch("23.150.0.4", "24.90.0.3"))
    check("S24.d compare_branch: same / newer branch ok, older outdated, no customer version "
          "unknown, no latest skipped",
          cmp == ("ok", "outdated", "ok", "unknown", "skipped", "outdated"), str(cmp))

    # --- e) the customer's driver: capture > IPS form > thread ---------------
    cap = tmp / "cap_s24"
    cap.mkdir(exist_ok=True)
    (cap / "system_info.txt").write_text(
        json.dumps({"Versions": {"Wi-Fi Driver Version": "23.150.0.4", "BT Driver Version": "23.150.0.1"}}),
        encoding="utf-8")
    log_in_cap = str(cap / "WifiDriverIHVSession.etl.002.log")
    orig_cfg_fn = runner_mod._bsod.handsfree_config
    runner_mod._bsod.handsfree_config = lambda: {"latest_driver": "24.90.0.3"}
    try:
        rn = HandsfreeRunner()
        a1 = CaseAnalysis(case_nbr="1", log_path=log_in_cap,
                          env_detail={"Found In Build": "24.30.1.1"},
                          case_reader={"driver_version": "24.80.0.1"})
        rn._settle_driver_version(a1)
        a2 = CaseAnalysis(case_nbr="1", env_detail={"Found In Build": "24.30.1.1 (WW11)"},
                          case_reader={"driver_version": "24.80.0.1"})
        rn._settle_driver_version(a2)
        a3 = CaseAnalysis(case_nbr="1", case_reader={"driver_version": "24.95.0.1"})
        rn._settle_driver_version(a3)
        a4 = CaseAnalysis(case_nbr="1", env_detail={"Found In Build": "N/A"})
        rn._settle_driver_version(a4)
        runner_mod._bsod.handsfree_config = lambda: {}
        a5 = CaseAnalysis(case_nbr="1", env_detail={"Found In Build": "24.90.0.1"})
        rn._settle_driver_version(a5)
        runner_mod._bsod.handsfree_config = lambda: {"latest_driver": "24.90.0.3",
                                                     "latest_driver_manual": "25.0.0.2"}
        a6 = CaseAnalysis(case_nbr="1", env_detail={"Found In Build": "24.90.0.1"})
        rn._settle_driver_version(a6)
    finally:
        runner_mod._bsod.handsfree_config = orig_cfg_fn
    check("S24.e _settle_driver_version: the capture's system_info.txt wins (and a newer version "
          "elsewhere is only flagged), then Found In Build, then the thread; nothing -> unknown; "
          "no latest -> skipped; the Settings override replaces the report's value",
          (a1.driver_version, a1.driver_version_source, a1.driver_check, a1.driver_decision["newer_mentioned"])
          == ("23.150.0.4", "system_info", "outdated", "24.30.1.1")
          and (a2.driver_version, a2.driver_version_source, a2.driver_check) == ("24.30.1.1", "env_detail", "outdated")
          and (a3.driver_version, a3.driver_version_source, a3.driver_check) == ("24.95.0.1", "reader", "ok")
          and (a4.driver_version, a4.driver_check, a4.latest_driver) == ("", "unknown", "24.90.0.3")
          and (a5.driver_check, a5.latest_driver) == ("skipped", "")
          and (a6.latest_driver, a6.latest_driver_source, a6.driver_check) == ("25.0.0.2", "manual", "outdated")
          and len(a1.driver_decision["candidates"]) == 3 and "outdated" in a1.driver_decision["summary"],
          f"a1={a1.driver_version}/{a1.driver_version_source}/{a1.driver_check}/{a1.driver_decision.get('newer_mentioned')} "
          f"a2={a2.driver_version}/{a2.driver_version_source}/{a2.driver_check} a3={a3.driver_version}/{a3.driver_check} "
          f"a4={a4.driver_version!r}/{a4.driver_check} a5={a5.driver_check} a6={a6.latest_driver}/{a6.latest_driver_source}")

    # --- f) the drafts -------------------------------------------------------
    base = dict(case_nbr="1", ok=True, subject="[X] p2p set channel failed",
                issue_domain="P2P (Miracast)", issue_subcategory="Connectivity",
                missing_info=[{"item": "issue_time", "reason": "none stated"}])
    ask = "validate the issue on the latest release 24.90.0.3"
    state_it = "Please state the driver version the issue was observed on"
    got = {}
    for mode in ("first_response", "request_info", "request_logs"):
        for chk in ("outdated", "unknown", "ok"):
            a = CaseAnalysis(mode=mode, driver_check=chk,
                             driver_version="" if chk == "unknown" else "23.150.0.4",
                             driver_version_source="" if chk == "unknown" else "env_detail",
                             latest_driver="24.90.0.3", latest_driver_source="report", **base)
            a.checklist_fills = build_fills(a)
            p = compose(a)["plain"]
            got[(mode, chk)] = (ask in p, state_it in p,
                                "[ ] Can the issue be replicated using the latest driver release" in p,
                                "[✓] Can the issue be replicated using the latest driver release? — provided: "
                                "driver 23.150.0.4 is on the latest released branch (24.90.0.3)" in p)
    ok_all = all(got[(m, "outdated")] == (True, False, True, False)
                 and got[(m, "unknown")] == (True, True, True, False)
                 and got[(m, "ok")] == (False, False, False, True)
                 for m in ("first_response", "request_info", "request_logs"))
    check("S24.f customer drafts: outdated -> 'validate on the latest release <v>' and the checklist "
          "item stays open; unknown -> also 'state the driver version'; ok -> no ask, the item is "
          "ticked by the pipeline (first_response, request_info, request_logs alike)", ok_all, str(got))
    a_later = CaseAnalysis(mode="request_logs", driver_check="outdated", driver_version="23.150.0.4",
                           driver_version_source="system_info", latest_driver="24.90.0.3",
                           first_response_done=True, **base)
    p_later = compose(a_later)["plain"]
    a_dump = CaseAnalysis(mode="request_logs", log_request_reason="no_dump", bsod_case=True,
                          driver_check="outdated", driver_version="23.150.0.4",
                          driver_version_source="env_detail", latest_driver="24.90.0.3", **base)
    p_dump = compose(a_dump)["plain"]
    a_skip = CaseAnalysis(mode="request_info", driver_check="skipped", **base)
    p_skip = compose(a_skip)["plain"]
    check("S24.f2 the ask lives outside the one-time checklist: a later-round request_logs draft "
          "(checklist already sent) still carries it; the BSOD dump request carries it; no latest "
          "release known -> nothing asked",
          ask in p_later and "=== General Info ===" not in p_later
          and ask in p_dump and "from the Found In Build field of the case" in p_dump
          and "Driver version" not in p_skip,
          f"later={ask in p_later}/{'=== General Info ===' in p_later} dump={ask in p_dump} skip={'Driver version' in p_skip}")
    a_full = CaseAnalysis(mode="full", driver_check="outdated", driver_version="23.150.0.4",
                          driver_version_source="system_info", latest_driver="24.90.0.3",
                          latest_driver_source="report",
                          driver_decision={"winner_label": "system_info.txt of the analysed WRT capture",
                                           "newer_mentioned": "24.30.1.1"}, **base)
    p_full = compose(a_full)["plain"]
    a_full_ok = CaseAnalysis(mode="full", driver_check="ok", driver_version="24.90.0.1",
                             driver_version_source="env_detail", latest_driver="24.90.0.3", **base)
    p_full_ok = compose(a_full_ok)["plain"]
    a_wait = CaseAnalysis(mode="waiting_customer", driver_check="outdated", driver_version="23.150.0.4",
                          latest_driver="24.90.0.3", **base)
    p_wait = compose(a_wait)["plain"]
    check("S24.f3 internal drafts: a reviewer note states the verdict (and a newer driver the thread "
          "mentions); nothing in a waiting_customer note",
          "Driver version check: customer driver 23.150.0.4 (system_info.txt of the analysed WRT capture) "
          "is NOT on the latest released branch — latest release 24.90.0.3 (report)." in p_full
          and "The thread also mentions 24.30.1.1" in p_full
          and "Driver version check: customer driver 24.90.0.1 (" in p_full_ok
          and "is on the latest released branch (24.90.0.3)" in p_full_ok
          and "Driver version check" not in p_wait,
          f"full={'Driver version check' in p_full} ok={'Driver version check' in p_full_ok} wait={'Driver version check' in p_wait}")

    # --- g) the LLM fill cannot tick the item the reply asks about ---------
    a_lock = CaseAnalysis(mode="first_response", driver_check="outdated", driver_version="23.150.0.4",
                          latest_driver="24.90.0.3", **base)
    fills = _blank_fills("P2P (Miracast)", "Connectivity")
    deterministic_fills(a_lock, fills)
    item = next(e for e in fills["general_info"] if "latest driver release" in e["item"])
    idx = [e for sec in ("general_info", "required_log", "required_info") for e in fills.get(sec, [])
           if not e["provided"]].index(item) + 1
    fake = types.SimpleNamespace(chat=lambda messages, system_content=None:
                                 json.dumps({"fills": {str(idx): {"provided": True, "value": "yes, on latest"}}}))
    llm_fill(fake, fills, "customer says it also fails on the latest driver", lock_driver_item=True)
    fills_ok = _blank_fills("P2P (Miracast)", "Connectivity")
    a_ok = CaseAnalysis(mode="first_response", driver_check="ok", driver_version="24.90.0.1",
                        latest_driver="24.90.0.3", **base)
    deterministic_fills(a_ok, fills_ok)
    item_ok = next(e for e in fills_ok["general_info"] if "latest driver release" in e["item"])
    check("S24.g checklist item 'replicated on the latest driver release': locked against the text "
          "pass when the reply asks for validation; ticked by the pipeline when the customer is current",
          item["provided"] is False and item["source"] == "locked"
          and item_ok["provided"] is True and item_ok["source"] == "pipeline"
          and "24.90.0.1" in item_ok["value"],
          f"locked={item.get('source')}/{item['provided']} ok={item_ok.get('source')}/{item_ok['value']}")

    # --- h) the reader returns the driver under test -----------------------
    from .case_reader import READER_PROMPT, read_case_history
    reader_json = ('{"clean_description": "x", "issue_times": [], "issue_time_source": "", '
                   '"attachment_name": "", "attachment_reason": "", "reasoning": "", "missing_info": [], '
                   '"issue_domain": "Connectivity", "issue_subcategory": "", "action_owner": "intel", '
                   '"next_action": "n", "driver_version": "%s"}')
    def _reader(v):
        fake_llm = types.SimpleNamespace(chat=lambda messages, system_content=None: reader_json % v)
        return read_case_history(fake_llm, subject="s", description="d", comments=[], attachment_list=[])
    r_ver = _reader("24.30.1.1")
    r_bad = _reader("latest")
    check("S24.h case reader: asks for the driver the failure was seen on; x.y.z.w kept, anything "
          "else -> ''",
          "driver_version" in READER_PROMPT and "DRIVER VERSION" in READER_PROMPT
          and r_ver and r_ver["driver_version"] == "24.30.1.1" and r_bad and r_bad["driver_version"] == "",
          f"ver={r_ver and r_ver.get('driver_version')!r} bad={r_bad and r_bad.get('driver_version')!r}")

    # --- i) refresh_latest_driver / effective_latest_driver -----------------
    store = HandsfreeStore(tmp / "handsfree_s24")
    res_ok = dr.refresh_latest_driver(store, today=date(2026, 11, 9), fetch=lambda: (rows, "Last Updated: WW 45'2026"))
    cfg1 = store.load_config()
    def _boom():
        raise RuntimeError("Chrome exploded")
    res_bad = dr.refresh_latest_driver(store, today=date(2026, 11, 9), fetch=_boom)
    cfg2 = store.load_config()
    res_empty = dr.refresh_latest_driver(store, today=date(2026, 11, 9), fetch=lambda: [])
    cfg3 = store.load_config()
    eff = (dr.effective_latest_driver(cfg3),
           dr.effective_latest_driver({**cfg3, "latest_driver_manual": "25.0.0.1"}),
           dr.effective_latest_driver({**cfg3, "latest_driver_manual": "abc"}),
           dr.effective_latest_driver({}))
    check("S24.i refresh_latest_driver: a good fetch stores the pick + checked_at + rows; a failing "
          "or empty fetch keeps the value and records the error; the Settings override wins only "
          "when it is a full version",
          res_ok["ok"] and cfg1["latest_driver"] == "24.90.0.3" and cfg1["latest_driver_checked_at"]
          and cfg1["latest_driver_rows_count"] == len(rows) and cfg1["latest_driver_error"] == ""
          and cfg1["latest_driver_report_updated"] == "Last Updated: WW 45'2026"
          and cfg1["latest_driver_row"]["tic"] == "MHWFW000813_24.90.0.3"
          and not res_bad["ok"] and cfg2["latest_driver"] == "24.90.0.3"
          and "Chrome exploded" in cfg2["latest_driver_error"]
          and cfg2["latest_driver_checked_at"] == cfg1["latest_driver_checked_at"]
          and not res_empty["ok"] and "no PV" in cfg3["latest_driver_error"]
          and eff == (("24.90.0.3", "report"), ("25.0.0.1", "manual"), ("24.90.0.3", "report"), ("", "")),
          f"ok={res_ok} bad={res_bad} empty={res_empty} eff={eff}")

    # --- j) is_refresh_due ---------------------------------------------------
    now = datetime(2026, 11, 9, 12, 0).astimezone()
    iso = lambda dt: dt.isoformat(timespec="seconds")
    due = (dr.is_refresh_due({}, now),
           dr.is_refresh_due({"latest_driver_last_attempt_at": iso(now - timedelta(hours=1)),
                              "latest_driver_checked_at": iso(now - timedelta(hours=1))}, now),
           dr.is_refresh_due({"latest_driver_last_attempt_at": iso(now - timedelta(hours=25)),
                              "latest_driver_checked_at": iso(now - timedelta(hours=25))}, now),
           dr.is_refresh_due({"latest_driver_last_attempt_at": iso(now - timedelta(hours=1)),
                              "latest_driver_checked_at": iso(now - timedelta(hours=1)),
                              "latest_driver_refresh_requested_at": iso(now - timedelta(minutes=5))}, now),
           dr.is_refresh_due({"latest_driver_last_attempt_at": iso(now - timedelta(hours=1)),
                              "latest_driver_error": "boom"}, now),
           dr.is_refresh_due({"latest_driver_last_attempt_at": iso(now - timedelta(hours=3)),
                              "latest_driver_error": "boom"}, now))
    check("S24.j is_refresh_due: never fetched / a day old / Refresh pressed since / a failure 2 h+ "
          "ago -> due; fresh or a failure 1 h ago -> not",
          due == (True, False, True, True, False, True), str(due))

    # --- k) the scheduler hook: server only, when due, never over a UI run --
    orig_role = getattr(app_config, "handsfree_role", "client")
    orig_env = os.environ.get("INTELAVATAR_HANDSFREE_ROLE")
    orig_dm = getattr(app_config, "driver_manager", None)
    orig_store_s, orig_refresh = sch._store, sch.refresh_latest_driver
    calls = []
    try:
        store_k = HandsfreeStore(tmp / "handsfree_s24k")
        sch._store = lambda: store_k
        sch.refresh_latest_driver = lambda st: calls.append(st) or {"ok": True}
        app_config.driver_manager = object()        # "Chrome is available here"
        os.environ.pop("INTELAVATAR_HANDSFREE_ROLE", None)
        app_config.handsfree_role = "client"
        k_client = sch.maybe_refresh_latest_driver()
        app_config.handsfree_role = "server"
        k_due = sch.maybe_refresh_latest_driver()
        store_k.save_config({"latest_driver_last_attempt_at": iso(datetime.now().astimezone()),
                             "latest_driver_checked_at": iso(datetime.now().astimezone())})
        k_fresh = sch.maybe_refresh_latest_driver()
        store_k.save_config({"latest_driver_refresh_requested_at":
                             iso(datetime.now().astimezone() + timedelta(seconds=5))})
        orch._run_lock.acquire()
        try:
            k_busy = sch.maybe_refresh_latest_driver()
        finally:
            orch._run_lock.release()
        k_requested = sch.maybe_refresh_latest_driver()
        app_config.driver_manager = None
        k_no_chrome = sch.maybe_refresh_latest_driver()
    finally:
        app_config.handsfree_role = orig_role
        app_config.driver_manager = orig_dm
        if orig_env is None:
            os.environ.pop("INTELAVATAR_HANDSFREE_ROLE", None)
        else:
            os.environ["INTELAVATAR_HANDSFREE_ROLE"] = orig_env
        sch._store, sch.refresh_latest_driver = orig_store_s, orig_refresh
    check("S24.k maybe_refresh_latest_driver: a client never fetches; the server fetches when due "
          "or when Refresh was pressed, not when fresh, not while a run holds the lock, not without "
          "Chrome in this process",
          (k_client, k_due, k_fresh, k_busy, k_requested, k_no_chrome) == (False, True, False, False, True, False)
          and len(calls) == 2,
          str((k_client, k_due, k_fresh, k_busy, k_requested, k_no_chrome, len(calls))))

    # --- l) routes: the override is validated; Refresh now leaves a request --
    try:
        from flask import Flask
        from blueprints.handsfree import handsfree_routes as routes
        orig_routes_store = routes._store
        store_l = HandsfreeStore(tmp / "handsfree_s24l")
        routes._store = lambda: store_l
        try:
            app = Flask("handsfree_smoke_s24")
            app.register_blueprint(routes.handsfree_bp)
            client = app.test_client()
            r_ok = client.post("/handsfree/config", json={"latest_driver_manual": " 24.90.0.3 "})
            r_bad = client.post("/handsfree/config", json={"latest_driver_manual": "nope"})
            r_clear = client.post("/handsfree/config", json={"latest_driver_manual": ""})
            r_ref = client.post("/handsfree/latest_driver/refresh")
            cfg_l = store_l.load_config()
        finally:
            routes._store = orig_routes_store
        check("S24.l routes: /config stores a full x.y.z.w override (normalised) and refuses anything "
              "else with 400; clearing works; POST /latest_driver/refresh stamps the request",
              r_ok.status_code == 200 and r_ok.get_json()["latest_driver_manual"] == "24.90.0.3"
              and r_bad.status_code == 400 and "24.90.0.3" in r_bad.get_json()["error"]
              and r_clear.status_code == 200 and r_clear.get_json()["latest_driver_manual"] == ""
              and r_ref.status_code == 200 and r_ref.get_json()["ok"] is True
              and cfg_l["latest_driver_refresh_requested_at"]
              and "latest_driver" in r_ref.get_json(),
              f"ok={r_ok.status_code}/{r_ok.get_json().get('latest_driver_manual')!r} bad={r_bad.status_code} "
              f"clear={r_clear.status_code} ref={r_ref.status_code} req={cfg_l.get('latest_driver_refresh_requested_at')!r}")
    except Exception as e:
        check("S24.l latest-driver routes", False, f"{type(e).__name__}: {e}")


# ---------------------------------------------------------------- S25
def smoke_echo_isolation(tmp: Path) -> None:
    print("[S25] Echo text: last in the internal draft, between markers; never in a "
          "customer-facing draft or a public post")
    import types
    from . import orchestrator as orch
    from .composer import (AI_MARKER, CHECKLIST_TAG, ECHO_SECTION_END, ECHO_SECTION_START,
                           _FOOTER, compose, strip_echo_section)
    from .ips_client import PostResult
    from .queue import HandsfreeStore
    from .runner import CaseAnalysis, IncidentReport

    advice = ("- Ask for the AP model and a sniffer capture during the drop.\n"
              "- INTERNAL: known FW race in scan abort, sighting 7771 — do not share.")
    insight = "Known race in the scan abort path (fixed in FW core 95, internal JIRA WIFI-1234)."
    echo = dict(echo_advice={"status": "answered", "answer": advice, "question": "q"},
                echo_insights=[{"kind": "assert", "code": "0x02001234", "answer": insight,
                                "error": None},
                               {"kind": "assert", "code": "0xDEAD", "answer": None,
                                "error": "backend down"}])
    base = dict(case_nbr="1", ok=True, subject="[X] roaming drop", issue_domain="Connectivity",
                issue_subcategory="Roaming", driver_check="outdated", driver_version="23.150.0.4",
                driver_version_source="env_detail", latest_driver="24.90.0.3",
                missing_info=[{"item": "issue_time", "reason": "none stated"}])

    # --- a) the internal (full) draft ----------------------------------------
    full = CaseAnalysis(mode="full", **base, **echo)
    full.incidents = [IncidentReport(issue_time="08/20/2026 13:43",
                                     report={"root_cause_summary": "assert during scan",
                                             "confidence_score": 80,
                                             "recommended_actions": ["collect a sniffer"]})]
    p = compose(full)["plain"]
    i_start, i_end, i_footer = p.find(ECHO_SECTION_START), p.find(ECHO_SECTION_END), p.find(_FOOTER)
    before = p[:i_start]
    inside = p[i_start:i_end]
    check("S25.a full draft: one Echo section, after the footer, closed by the end marker which "
          "is the draft's last line; both the next-step advice and the insights live inside it; "
          "nothing Echo before the start marker",
          p.count(ECHO_SECTION_START) == 1 and p.count(ECHO_SECTION_END) == 1
          and 0 < i_footer < i_start < i_end
          and p.rstrip().endswith(ECHO_SECTION_END)
          and "sighting 7771" in inside and "WIFI-1234" in inside
          and "suggested next steps" in inside and "Knowledge base insights (Echo):" in inside
          and "do not paste to customer" in inside and "backend down" not in p
          and "Echo" not in before and "sighting 7771" not in before and "WIFI-1234" not in before
          and "assert during scan" in before and "collect a sniffer" in before,
          f"footer={i_footer} start={i_start} end={i_end} tail={p[-120:]!r}")
    tri = CaseAnalysis(mode="triage", **base, **echo)
    p_tri = compose(tri)["plain"]
    check("S25.a2 triage-only internal draft: same trailing section",
          p_tri.count(ECHO_SECTION_START) == 1 and p_tri.rstrip().endswith(ECHO_SECTION_END)
          and p_tri.find(_FOOTER) < p_tri.find(ECHO_SECTION_START)
          and "sighting 7771" in p_tri[p_tri.find(ECHO_SECTION_START):],
          p_tri[-200:])
    quiet = CaseAnalysis(mode="full", **base)
    quiet.incidents = list(full.incidents)
    p_quiet = compose(quiet)["plain"]
    check("S25.a3 nothing from Echo -> no section at all, the footer closes the draft",
          ECHO_SECTION_START not in p_quiet and "Echo" not in p_quiet
          and p_quiet.rstrip().endswith(_FOOTER), p_quiet[-160:])

    # --- b) customer-facing drafts never carry it ----------------------------
    leaks = {}
    for mode in ("first_response", "request_info", "request_logs"):
        for extra in ({}, {"log_request_reason": "no_dump", "bsod_case": True},
                      {"first_response_done": True}):
            if extra and mode != "request_logs":
                continue
            a = CaseAnalysis(mode=mode, **base, **echo, **extra)
            pc = compose(a)["plain"]
            leaks[(mode, tuple(extra))] = (ECHO_SECTION_START in pc, ECHO_SECTION_END in pc,
                                           "Echo" in pc, "sighting 7771" in pc, "WIFI-1234" in pc,
                                           "sniffer capture during the drop" in pc)
    check("S25.b customer drafts (first_response, request_info, request_logs incl. the dump "
          "request and a later round): no marker, no Echo word, none of Echo's text",
          all(v == (False,) * 6 for v in leaks.values()), str(leaks))

    # --- c) strip_echo_section -------------------------------------------------
    stripped = strip_echo_section(p)
    no_end = strip_echo_section(p.replace(ECHO_SECTION_END, ""))
    edited = strip_echo_section(p + "\nPS: reviewer line after the section\n")
    check("S25.c strip_echo_section: drops the section (end marker present, deleted by the "
          "reviewer, or text after it kept); idempotent; untouched when absent",
          ECHO_SECTION_START not in stripped and "sighting 7771" not in stripped
          and "WIFI-1234" not in stripped and stripped.rstrip().endswith(_FOOTER)
          and "assert during scan" in stripped
          and ECHO_SECTION_START not in no_end and "sighting 7771" not in no_end
          and no_end.rstrip().endswith(_FOOTER)
          and "sighting 7771" not in edited and edited.rstrip().endswith("after the section")
          and strip_echo_section(stripped) == stripped
          and strip_echo_section(p_quiet) == p_quiet and strip_echo_section("") == "",
          f"stripped_tail={stripped[-100:]!r} edited_tail={edited[-80:]!r}")

    # --- d) a public post strips it, an internal post keeps it ---------------
    store = HandsfreeStore(tmp / "handsfree_s25")
    store.save_config({"post_backend": "rest"})

    class _Ips:
        FIELD_RICH_BODY = "Core_IPS_Rich_Comment__c"
        posted: list = []

        def get_case_comments(self, case_id):
            return []

        def post_comment(self, case_id, rich_body, **kw):
            self.posted.append((kw.get("plain_body") or "", rich_body, kw.get("private")))
            return PostResult(ok=True, backend="rest", comment_id=f"CMT{len(self.posted)}")

        def comment_author(self, comment_id):
            return {"user_id": "005SMOKE", "name": "Smoke Reviewer"}

    orig = (orch._store, orch.IpsClient, orch._signed_in_identity)
    try:
        orch._store, orch.IpsClient = (lambda: store), _Ips
        orch._signed_in_identity = lambda: {"user_id": "005SMOKE", "name": "Smoke Reviewer",
                                            "email": "smoke@intel.com", "username": "smoke"}
        def _enq(case):
            return store.enqueue(case_nbr=case, case_id="500" + case, subject="s",
                                 draft_plain=p, draft_html=compose(full)["html"],
                                 confidence=80, mode="full", analysis={})["draft_id"]
        d_pub = _enq("08888801")
        r_pub = orch.approve_and_post(d_pub, visibility="external")
        rec_pub = store.get(d_pub)
        d_int = _enq("08888802")
        r_int = orch.approve_and_post(d_int)
        d_edit = _enq("08888803")
        r_edit = orch.approve_and_post(d_edit, edited_plain=p.replace(ECHO_SECTION_END, "")
                                       + "\nPS kept\n", visibility="external")
        pub_plain, pub_html, pub_private = _Ips.posted[0]
        int_plain, _int_html, int_private = _Ips.posted[1]
        edit_plain, edit_html, _ = _Ips.posted[2]
        check("S25.d approve: a customer-visible post drops the Echo section from the plain and "
              "rich bodies (end marker deleted by the reviewer: everything after the start "
              "marker goes) and the stored draft shows what went out; an internal post keeps it",
              r_pub["ok"] and pub_private is False
              and ECHO_SECTION_START not in pub_plain and "sighting 7771" not in pub_plain
              and "WIFI-1234" not in pub_plain and "sighting 7771" not in pub_html
              and "Echo" not in pub_html and AI_MARKER in pub_plain
              and "assert during scan" in pub_plain
              and ECHO_SECTION_START not in rec_pub["draft_plain"]
              and r_int["ok"] and int_private is True
              and ECHO_SECTION_START in int_plain and "sighting 7771" in int_plain
              and r_edit["ok"] and "sighting 7771" not in edit_plain
              and "sighting 7771" not in edit_html and "PS kept" not in edit_plain
              and edit_plain.rstrip().endswith(_FOOTER),
              f"pub={r_pub.get('error')} int={r_int.get('error')} edit={r_edit.get('error')} "
              f"pub_tail={pub_plain[-100:]!r} edit_tail={edit_plain[-80:]!r}")
    finally:
        orch._store, orch.IpsClient, orch._signed_in_identity = orig
    check("S25.e the family tag is not confused by the section (an analysis draft stays an "
          "analysis draft)", CHECKLIST_TAG not in p, "")


# ---------------------------------------------------------------- S26
def smoke_regression_item() -> None:
    print("[S26] Checklist 'Is this a regression issue?': ticked only on a stated passing "
          "version; a reproducing version alone leaves it open and the reply asks which "
          "earlier version passed")
    import types
    from .case_reader import READER_PROMPT, read_case_history
    from .checklist import (REGRESSION_ASK, _REGRESSION_ITEM_RE, _blank_fills, build_fills,
                            deterministic_fills, llm_fill)
    from .composer import compose
    from .runner import CaseAnalysis, live_fields

    def _item(fills):
        return next(e for e in fills["general_info"] if _REGRESSION_ITEM_RE.search(e["item"]))

    def _idx(fills, item):
        return [e for sec in ("general_info", "required_log", "required_info")
                for e in fills.get(sec, []) if not e["provided"]].index(item) + 1

    base = dict(case_nbr="1", ok=True, mode="first_response", subject="[X] roaming drop",
                issue_domain="Connectivity", issue_subcategory="Roaming",
                description="Roaming drops on driver 24.30.1.1 since the upgrade.",
                customer_history="(2026-06-20) Found on 24.30.1.1; worked before.",
                driver_check="ok", driver_version="24.30.1.1", driver_version_source="env_detail",
                latest_driver="24.30.0.5")

    # --- a) repro version only: the LLM pass is locked out, the reply asks --
    a = CaseAnalysis(**base)
    fills_a = _blank_fills("Connectivity", "Roaming")
    deterministic_fills(a, fills_a)
    item_a = _item(fills_a)
    idx = _idx(fills_a, item_a)
    eager = types.SimpleNamespace(chat=lambda messages, system_content=None: json.dumps(
        {"fills": {str(idx): {"provided": True, "value": "Yes, regression on 24.30.1.1"}}}))
    llm_fill(eager, fills_a, "x", lock_regression_item=True)
    fills_a2 = build_fills(a, llm=eager)
    item_a2 = _item(fills_a2)
    a.checklist_fills = fills_a2
    p_a = compose(a)["plain"]
    line_a = next(ln for ln in p_a.splitlines() if "regression issue" in ln)
    check("S26.a repro version only: the item stays [ ] (locked against the text pass, directly "
          "and via build_fills), the rendered line asks for the last passing driver version, "
          "the decision's ask list carries it",
          item_a["provided"] is False and item_a["source"] == "locked"
          and item_a2["provided"] is False and item_a2["source"] == "locked"
          and line_a.startswith("  [ ] Is this a regression issue?")
          and line_a.endswith("— please provide " + REGRESSION_ASK)
          and "Example: Yes, the delta" not in line_a
          and REGRESSION_ASK in a.checklist_decision["asks"]
          and a.checklist_decision["last_good_version"] == ""
          and "did NOT reproduce (regression test)" in p_a,
          f"a={item_a.get('source')}/{item_a['provided']} a2={item_a2.get('source')} line={line_a!r} "
          f"asks={a.checklist_decision.get('asks')}")

    # --- b) a stated last-known-good version ticks it ------------------------
    b = CaseAnalysis(last_good_version="23.60.1.2", **base)
    fills_b = build_fills(b, llm=eager)
    item_b = _item(fills_b)
    b.checklist_fills = fills_b
    p_b = compose(b)["plain"]
    check("S26.b last-good stated: ticked by the pipeline with the passing and the failing "
          "version; no regression ask",
          item_b["provided"] is True and item_b["source"] == "pipeline"
          and item_b["value"] == "Yes — the issue does not reproduce on driver 23.60.1.2; "
                                 "reproduces on 24.30.1.1"
          and "[✓] Is this a regression issue?" in p_b
          and "provided: Yes — the issue does not reproduce on driver 23.60.1.2" in p_b
          and REGRESSION_ASK not in b.checklist_decision["asks"] and REGRESSION_ASK not in p_b
          and b.checklist_decision["last_good_version"] == "23.60.1.2",
          f"b={item_b.get('source')}/{item_b['value']!r} asks={b.checklist_decision.get('asks')}")
    c = CaseAnalysis(**dict(base, driver_version="", driver_check="unknown",
                            last_good_version="23.60"))
    fills_c = build_fills(c)
    check("S26.b2 failing version unknown: the value names the passing driver only",
          _item(fills_c)["value"] == "Yes — the issue does not reproduce on driver 23.60",
          _item(fills_c)["value"])
    # The other request modes render the same checklist line.
    for mode in ("request_info", "request_logs"):
        m = CaseAnalysis(**dict(base, mode=mode, missing_info=[{"item": "issue_time",
                                                                 "reason": "none"}]))
        pm = compose(m)["plain"]
        check(f"S26.b3 {mode}: the open regression item asks for the passing version",
              "— please provide " + REGRESSION_ASK in pm, pm[pm.find("regression"):][:200])

    # --- c) the reader returns the last-known-good version ------------------
    reader_json = ('{"clean_description": "x", "issue_times": [], "issue_time_source": "", '
                   '"attachment_name": "", "attachment_reason": "", "reasoning": "", "missing_info": [], '
                   '"issue_domain": "Connectivity", "issue_subcategory": "", "action_owner": "intel", '
                   '"next_action": "n", "driver_version": "24.30.1.1", "last_good_version": "%s"}')
    def _reader(v):
        fake_llm = types.SimpleNamespace(chat=lambda messages, system_content=None: reader_json % v)
        return read_case_history(fake_llm, subject="s", description="d", comments=[], attachment_list=[])
    got = tuple(_reader(v)["last_good_version"] for v in
                ("23.60.1.2", "23.60", "worked before", "", "24.30.1.1", "driver 24.20.0.6 (WW40)"))
    check("S26.c case reader: asks for the last-known-good driver; x.y[.z[.w]] kept, a statement "
          "without a version -> '', the failing version itself -> ''",
          "last_good_version" in READER_PROMPT and "LAST-KNOWN-GOOD" in READER_PROMPT
          and got == ("23.60.1.2", "23.60", "", "", "", "24.20.0.6"), str(got))

    # --- d) runner wiring -----------------------------------------------------
    an = CaseAnalysis(case_nbr="1", last_good_version="23.60.1.2")
    check("S26.d CaseAnalysis.last_good_version reaches the live snapshot",
          live_fields(an).get("last_good_version") == "23.60.1.2",
          str(live_fields(an).get("last_good_version")))


# ---------------------------------------------------------------- S27
def smoke_review_fixes(tmp: Path) -> None:
    print("[S27] PR 190 review: case radio, Wi-Fi-only driver check, description-image cache, "
          "BSOD ledger across machines, Outlook per-case state and per-mail files, CLI schedule guards")
    import importlib.util
    import json as _json
    import os as _os
    import socket
    import threading as _threading
    import time as _time
    import types
    from . import bsod as bsod_mod
    from . import cli as cli_mod
    from . import comment_images as ci
    from . import email_case as ec
    from . import outlook_source as ol
    from . import runner as runner_mod
    from .composer import _driver_review_lines, latest_driver_lines
    from .runner import CaseAnalysis, HandsfreeRunner

    # --- a) the radio of a case: IPS stays strict, an email thread is honoured ---
    mail = ec.EmailMessage(entry_id="M1", store_id="S", folder="Inbox",
                           subject="IPS 01031658 Wi-Fi keeps disconnecting", sender_name="Ann",
                           sender_email="ann@oem.com", received="2026-10-22T10:15:00+08:00",
                           body="the AX211 drops every hour")
    ctx_wifi = ec.build_case_context("01031658", [mail], str(tmp / "s27_mail"))
    mail_bt = ec.EmailMessage(entry_id="M2", store_id="S", folder="Inbox",
                              subject="IPS 01032299 bluetooth headset drops", sender_name="Bob",
                              sender_email="bob@oem.com", received="2026-10-22T11:00:00+08:00",
                              body="BT audio stutters")
    ctx_bt = ec.build_case_context("01032299", [mail_bt], str(tmp / "s27_mail"))
    radio = runner_mod._case_radio
    check("S27.a an email thread is analysed on the radio its context resolved (it has no IPS "
          "subcategory: 'Email thread' alone used to make every email case unsupported); a case "
          "fetched from IPS still needs an explicit Wi-Fi/BT subcategory; an explicit subcategory wins",
          ctx_wifi.subcategory == "Email thread" and ctx_wifi.source == "email"
          and radio(ctx_wifi.subcategory, ctx_wifi.source, ctx_wifi.wifi_or_bt) == "wifi"
          and radio(ctx_bt.subcategory, ctx_bt.source, ctx_bt.wifi_or_bt) == "bt"
          and radio("Email thread", "ips", "wifi") == ""
          and radio("Email thread", "ips+email", "wifi") == ""
          and radio("Email thread", "email", "") == ""
          and radio("Email thread", "email", "unknown") == ""
          and radio("Wi-Fi Connectivity", "email", "bt") == "wifi"
          and radio("Bluetooth Audio", "ips", "") == "bt"
          and radio("", "ips", "bt") == "",
          f"{ctx_wifi.subcategory}/{ctx_wifi.wifi_or_bt} {ctx_bt.wifi_or_bt}")

    # --- b) the latest-release check is the Wi-Fi driver's --------------------
    orig_cfg_fn = runner_mod._bsod.handsfree_config
    runner_mod._bsod.handsfree_config = lambda: {"latest_driver": "24.90.0.3"}
    try:
        rn = HandsfreeRunner()
        a_bt = CaseAnalysis(case_nbr="1", wifi_or_bt="bt",
                            env_detail={"Found In Build": "23.150.0.4"})
        rn._settle_driver_version(a_bt)
        a_bt_unknown = CaseAnalysis(case_nbr="1", wifi_or_bt="bt")
        rn._settle_driver_version(a_bt_unknown, quiet=True)
        a_wifi = CaseAnalysis(case_nbr="1", wifi_or_bt="wifi",
                              env_detail={"Found In Build": "23.150.0.4"})
        rn._settle_driver_version(a_wifi)
    finally:
        runner_mod._bsod.handsfree_config = orig_cfg_fn
    # a stored analysis from before the gate: outdated + bt must not ask either
    a_old = CaseAnalysis(case_nbr="1", wifi_or_bt="bt", driver_check="outdated",
                         driver_version="23.150.0.4", latest_driver="24.90.0.3")
    check("S27.b a Bluetooth case is not compared against the Wi-Fi release table: check skipped "
          "and marked not applicable, no customer paragraph (old or unknown version alike), the "
          "reviewer note says why; a Wi-Fi case on the same build is still asked to validate",
          a_bt.driver_check == "skipped" and a_bt.driver_decision.get("not_applicable") == "bt"
          and a_bt.latest_driver == "" and a_bt.driver_version == ""
          and latest_driver_lines(a_bt) == [] and latest_driver_lines(a_bt_unknown) == []
          and latest_driver_lines(a_old) == []
          and _driver_review_lines(a_bt) == ["Driver version check not applied: Bluetooth case "
                                             "(the latest-release table covers the Wi-Fi driver only)."]
          and a_wifi.driver_check == "outdated" and "24.90.0.3" in " ".join(latest_driver_lines(a_wifi)),
          f"bt={a_bt.driver_check}/{a_bt.driver_decision} wifi={a_wifi.driver_check} "
          f"{latest_driver_lines(a_bt)} {_driver_review_lines(a_bt)}")

    # --- c) description images: the cache follows the images, not their count --
    state = {"refid": "one", "vision": 0}

    class _Ips:
        FIELD_RICH_BODY = "Core_IPS_Rich_Comment__c"
        def get_case_comments(self, case_id):
            return []
        def get_case_rich_description(self, case_id):
            return {"created": "2026-06-20T01:00:00.000+0000",
                    "html": '<p>see</p><img src="https://intel.file.force.com/servlet/rtaImage'
                            f'?eid=500X&amp;refid={state["refid"]}">'}
        def fetch_binary(self, url, max_bytes=0):
            return ("image/png", b"\x89PNG\r\n\x1a\n" + url.encode())

    def _chat(messages, system_content=None):
        state["vision"] += 1
        return _json.dumps({"images": [f"screenshot {state['refid']}"]})
    llm = types.SimpleNamespace(model="claude-4-6-sonnet", chat=_chat)
    cache = tmp / "s27_img" / "image_descriptions.json"
    run_imgs = lambda: ci.annotate_case_comments(_Ips(), llm, "500X", [], cache_path=cache)
    _r1, info1 = run_imgs()
    _r2, info2 = run_imgs()                         # unchanged description: cached
    v_after_cached = state["vision"]
    state["refid"] = "two"                          # the customer swapped the screenshot
    _r3, info3 = run_imgs()
    v_after_swap = state["vision"]
    entry = _json.loads(cache.read_text(encoding="utf-8"))["desc:500X"]
    # an entry written before fingerprints existed is adopted, not thrown away
    legacy = dict(entry)
    legacy.pop("urls_fp")
    cache.write_text(_json.dumps({"desc:500X": legacy}), encoding="utf-8")
    _r4, info4 = run_imgs()
    adopted = _json.loads(cache.read_text(encoding="utf-8"))["desc:500X"].get("urls_fp")
    check("S27.c the case description is editable: the same NUMBER of images with a different image "
          "is read again (one screenshot swapped for another), an unchanged description stays cached, "
          "the fingerprint ignores the host, an older cache entry is adopted",
          info1[0]["descriptions"] == ["screenshot one"] and info2[0]["descriptions"] == ["screenshot one"]
          and v_after_cached == 1 and info3[0]["descriptions"] == ["screenshot two"]
          and v_after_swap == 2 and entry["url_count"] == 1
          and entry["urls_fp"] == ci._urls_fingerprint(
              ["https://intel.my.salesforce.com/servlet/rtaImage?eid=500X&refid=two"])
          and entry["urls_fp"] != ci._urls_fingerprint(
              ["https://intel.file.force.com/servlet/rtaImage?eid=500X&refid=one"])
          and info4[0]["descriptions"] == ["screenshot two"] and state["vision"] == 2
          and adopted == entry["urls_fp"],
          f"vision={state['vision']} info={info1} {info2} {info3} {info4} entry={entry} adopted={adopted}")

    # --- d) BSOD submission ledger: one writer at a time, across machines ------
    ledger = tmp / "s27_bsod" / "bsod_submissions.json"
    lock = ledger.with_name(ledger.name + ".lock")
    orig_bsod = (bsod_mod._submissions_path, bsod_mod._LEDGER_LOCK_WAIT_S)
    bsod_mod._submissions_path = lambda: ledger
    try:
        errors: list = []

        def _writer(k):
            try:
                for j in range(6):
                    bsod_mod.record_submission(f"0100{k:04d}", [f"dump{j}.zip"], "\\\\share\\x", "a@b", 200)
            except Exception as e:
                errors.append(repr(e))
        threads = [_threading.Thread(target=_writer, args=(k,)) for k in range(5)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        data = _json.loads(ledger.read_text(encoding="utf-8"))
        all_kept = (sorted(data) == [f"0100{k:04d}" for k in range(5)]
                    and all(len(v) == 6 for v in data.values()))
        leftovers = sorted(p.name for p in ledger.parent.iterdir() if p.name != ledger.name)

        # another machine holds the lock right now: wait, then refuse - never write past it
        bsod_mod._LEDGER_LOCK_WAIT_S = 0.4
        lock.write_text("other-pc", encoding="utf-8")
        t0 = _time.monotonic()
        try:
            bsod_mod.record_submission("01009999", ["late.zip"], "\\\\share\\x", "a@b", 200)
            held_err = ""
        except TimeoutError as e:
            held_err = str(e)
        waited = _time.monotonic() - t0
        untouched = "01009999" not in _json.loads(ledger.read_text(encoding="utf-8"))
        # a lock left by a process that died is taken over
        old = _time.time() - bsod_mod._LEDGER_LOCK_STALE_S - 5
        _os.utime(lock, (old, old))
        bsod_mod.record_submission("01009999", ["late.zip"], "\\\\share\\x", "a@b", 200)
        took_over = bsod_mod.submitted_names("01009999") == {"late.zip"} and not lock.exists()
        forgotten = bsod_mod.forget_case("01000002")
        after_forget = _json.loads(ledger.read_text(encoding="utf-8"))
        # an unparseable ledger is set aside, never silently replaced by a shorter one
        ledger.write_text("{not json", encoding="utf-8")
        bsod_mod.record_submission("01008888", ["x.zip"], "\\\\share\\x", "a@b", 200)
        aside = [p.name for p in ledger.parent.iterdir() if ".corrupt-" in p.name]
        check("S27.d ledger writes are serialised with a lock file next to it (5 threads x 6 records "
              "all kept, no temp or lock file left); a lock held elsewhere is waited for and then "
              "refused without writing; a stale lock is taken over; reset-case drops only its case; "
              "an unparseable ledger is set aside and said so",
              not errors and all_kept and leftovers == []
              and "busy on another machine" in held_err and 0.3 <= waited < 5 and untouched
              and took_over and forgotten == 6 and "01000002" not in after_forget
              and len(after_forget) == 5 and len(aside) == 1
              and bsod_mod.submitted_names("01008888") == {"x.zip"},
              f"errors={errors} kept={all_kept} leftovers={leftovers} held={held_err!r} waited={waited:.2f} "
              f"untouched={untouched} took_over={took_over} forgotten={forgotten} aside={aside}")
    finally:
        bsod_mod._submissions_path, bsod_mod._LEDGER_LOCK_WAIT_S = orig_bsod

    # --- e) Outlook state: work is tracked per (mail, case) ---------------------
    root = tmp / "s27_outlook"
    root.mkdir(parents=True, exist_ok=True)
    two = ["01031658", "01032299"]
    st0 = ol.load_state(root)
    p_unseen = ol.pending_cases(st0, "E9", two)
    ol.mark_processed(root, "E9", "01031658", "draft-1")       # one of its two cases ran
    st1 = ol.load_state(root)
    p_half = ol.pending_cases(st1, "E9", two)
    ol.mark_processed(root, "E9", "01032299")
    p_done = ol.pending_cases(ol.load_state(root), "E9", two)
    ol.mark_processed(root, "E3", complete=True)                # a skipped mail: closed as a whole
    st3 = ol.load_state(root)
    check("S27.e a mail naming two cases keeps the one that has not run (capped out or failed) "
          "pending after the other succeeded; it is done only when both ran; a skipped mail is "
          "closed as a whole",
          p_unseen == two and ol.is_processed(st1, "E9") and p_half == ["01032299"]
          and p_done == [] and ol.pending_cases(st3, "E3", two) == []
          and st3["processed"]["E3"].get("complete") is True
          and st3["processed"]["E9"]["case_nbrs"] == two
          and st3["processed"]["E9"]["draft_ids"] == ["draft-1"],
          f"{p_unseen} {p_half} {p_done} {st3['processed']}")

    # --- e2) the round itself: the capped-out case of a two-case mail is retried
    from datetime import datetime, timedelta
    from . import orchestrator as orch
    from .composer import AI_MARKER
    from .queue import HandsfreeStore
    now = datetime.now()

    class _Atts:
        def __init__(self, atts): self.a = list(atts)
        @property
        def Count(self): return len(self.a)
        def Item(self, i): return self.a[i - 1]

    class _Att:
        def __init__(self, name, data, type_=1):
            self.FileName, self.data, self.Type = name, data, type_
        def SaveAsFile(self, path):
            Path(path).write_bytes(self.data)

    class _Mail:
        Class = 43
        def __init__(self, entry, subject, body, sender, email, received, atts=()):
            self.EntryID, self.Subject, self.Body = entry, subject, body
            self.SenderName, self.SenderEmailAddress, self.SenderEmailType = sender, email, "SMTP"
            self.ReceivedTime, self.ConversationID = received, "conv-" + entry
            self.atts = list(atts)
        @property
        def Attachments(self): return _Atts(self.atts)

    class _Items:
        def __init__(self, mails): self.m = list(mails)
        @property
        def Count(self): return len(self.m)
        def Item(self, i): return self.m[i - 1]
        def Sort(self, key, desc): self.m.sort(key=lambda x: x.ReceivedTime, reverse=bool(desc))
        def Restrict(self, q):
            if str(q).startswith("@SQL"):
                import re as _re
                toks = _re.findall(r"%([^%']+)%", str(q))
                return _Items([m for m in self.m if any(t in (m.Subject + " " + m.Body) for t in toks)])
            return self

    class _Folder:
        def __init__(self, name, mails):
            self.Name, self.Items, self.StoreID = name, _Items(mails), "STORE1"
            self.Folders = _Items([])

    class _NS:
        def __init__(self, inbox, sent): self.inbox, self.sent = inbox, sent
        def GetDefaultFolder(self, n): return self.sent if n == 5 else self.inbox
        def GetItemFromID(self, entry_id, store_id=None):
            for f in (self.inbox, self.sent):
                for m in f.Items.m:
                    if m.EntryID == entry_id: return m
            raise RuntimeError("not found")

    class _App:
        def __init__(self, ns): self.ns = ns
        def GetNamespace(self, kind): return self.ns

    m_both = _Mail("T1", "IPS 01040001 and IPS 01040002 both fail after resume", "see both",
                   "Ann", "ann@oem.com", now - timedelta(hours=2))
    ns = _NS(_Folder("Inbox", [m_both]), _Folder("Sent Items", []))
    orig_factory = ol.Outlook.factory
    ol.Outlook.factory = lambda: _App(ns)
    store = HandsfreeStore(tmp / "handsfree_s27")
    ran: list = []
    fail_once = {"01040002"}

    def _fake_analyze(store_, case_nbr, case_id="", subject="", owner_name="", trial=None,
                      case_ctx=None, source="ips"):
        ran.append(case_nbr)
        if case_nbr in fail_once:
            fail_once.discard(case_nbr)
            raise RuntimeError("IPS briefly unreachable")
        return store_.enqueue(case_nbr=case_nbr, case_id="", subject=subject,
                              draft_plain=AI_MARKER + "\n\nanalysis of " + case_nbr,
                              draft_html="<p>a</p>", confidence=70, mode="full", analysis={},
                              source=source)
    orig_orch = (orch._analyze_and_enqueue, orch._store)
    orch._analyze_and_enqueue = _fake_analyze
    orch._store = lambda: store
    try:
        r1 = orch.run_email_scan(store, folder="Inbox", max_cases=1, own_email="me@intel.com")
        st_a = ol.load_state(store.root)["processed"].get("T1", {})
        r2 = orch.run_email_scan(store, folder="Inbox", max_cases=1, own_email="me@intel.com")
        st_b = ol.load_state(store.root)["processed"].get("T1", {})
        r3 = orch.run_email_scan(store, folder="Inbox", max_cases=1, own_email="me@intel.com")
        st_c = ol.load_state(store.root)["processed"].get("T1", {})
        r4 = orch.run_email_scan(store, folder="Inbox", max_cases=1, own_email="me@intel.com")
        check("S27.e2 one mail, two cases, one case per round: round 1 runs the first and leaves the "
              "second pending (the mail used to be dropped as handled here); round 2 runs the second, "
              "which fails and stays pending; round 3 retries it; round 4 has nothing left",
              ran == ["01040001", "01040002", "01040002"]
              and [c["case_nbr"] for c in r1["cases"]] == ["01040001"]
              and st_a.get("case_nbrs") == ["01040001"]
              and [(c["case_nbr"], c["mode"]) for c in r2["cases"]] == [("01040002", "error")]
              and st_b.get("case_nbrs") == ["01040001"]
              and [(c["case_nbr"], c["mode"]) for c in r3["cases"]] == [("01040002", "full")]
              and st_c.get("case_nbrs") == ["01040001", "01040002"]
              and r4["ok"] and r4["cases"] == []
              # every email draft records whose Outlook it came from
              and len(store.list_drafts()) == 2
              and all((d.get("email") or {}).get("mailbox") == "me@intel.com" for d in store.list_drafts())
              and len(store.list_drafts(mailbox="ME@intel.com")) == 2
              and store.list_drafts(owner_name="Somebody Else") == [],
              f"ran={ran} r1={r1.get('cases')} r2={r2.get('cases')} r3={r3.get('cases')} "
              f"r4={r4.get('cases')} state={st_a} / {st_b} / {st_c} err={r1.get('error')}")
    finally:
        orch._analyze_and_enqueue, orch._store = orig_orch

    # --- f) attachments: every mail keeps its own bytes -------------------------
    try:
        png_a = b"\x89PNG\r\n\x1a\n" + b"first screenshot"
        png_b = b"\x89PNG\r\n\x1a\n" + b"second, different screenshot"
        t_old = _Mail("A1", "IPS 01050001 scan fails", "log attached", "Ann", "ann@oem.com",
                      now - timedelta(days=2),
                      atts=[_Att("WRT.zip", b"PK\x03\x04 first capture"), _Att("image001.png", png_a),
                            _Att("notes.txt", b"same"), _Att("notes.txt", b"same name, other file")])
        t_new = _Mail("A2", "RE: IPS 01050001 scan fails", "new log after your request", "Ann",
                      "ann@oem.com", now - timedelta(hours=3),
                      atts=[_Att("WRT.zip", b"PK\x03\x04 second, longer capture"),
                            _Att("image001.png", png_b)])
        t_fwd = _Mail("A3", "FW: IPS 01050001 scan fails", "fyi", "Bob", "bob@oem.com",
                      now - timedelta(hours=1), atts=[_Att("WRT.zip", b"PK\x03\x04 second, longer capture")])
        ns_f = _NS(_Folder("Inbox", [t_old, t_new, t_fwd]), _Folder("Sent Items", []))
        ol.Outlook.factory = lambda: _App(ns_f)
        tdir = tmp / "email_01050001"
        with ol.Outlook() as app:
            th = app.thread("01050001", own_email="me@intel.com")
            for m in th:
                app.save_attachments(m, str(tdir))
            saved_again = app.save_attachments(th[0], str(tdir))      # a later round: files kept
        ctx = ec.build_case_context("01050001", th, str(tdir))
        names = [it[0] for it in ctx.attachment_list]
        read = lambda n: Path(ctx.local_paths[n]).read_bytes()
        imgs = {m.entry_id: Path(a["path"]).read_bytes() for m, a in ctx.image_files}
        dirs = sorted(p.name for p in tdir.iterdir())
        stamps = {it[0]: it[2][0] for it in ctx.attachment_list}
        check("S27.f each mail's files go to a folder of its own, so a name reused later in the thread "
              "keeps ITS bytes (image001.png, a second WRT.zip); the later archive is its own candidate "
              "with its own mail's time, the very same file forwarded again is listed once, two files "
              "of one mail with one name are both kept, a second round reuses the saved files",
              [m.entry_id for m in th] == ["A1", "A2", "A3"] and len(dirs) == 3
              and dirs[0] == ol.message_dir_name(th[0])
              and names == ["WRT.zip", "notes.txt", "(2) notes.txt", "(2) WRT.zip"]
              and read("WRT.zip") == b"PK\x03\x04 first capture"
              and read("(2) WRT.zip") == b"PK\x03\x04 second, longer capture"
              and read("notes.txt") == b"same" and read("(2) notes.txt") == b"same name, other file"
              and stamps["(2) WRT.zip"] > stamps["WRT.zip"]
              and imgs == {"A1": png_a, "A2": png_b}
              and [a["name"] for a in saved_again] == [a["name"] for a in th[0].attachments]
              and ol.unique_name("a.tar.gz", {"a.tar.gz"}) == "(2) a.tar.gz"
              and ol.unique_name("A.zip", {"a.zip", "(2) a.zip"}) == "(3) A.zip",
              f"dirs={dirs} names={names} stamps={stamps} imgs={ {k: v[-12:] for k, v in imgs.items()} }")
    finally:
        ol.Outlook.factory = orig_factory

    # --- g) one scheduler per server, also for a CLI that is already running ----
    here = socket.gethostname()
    cfgs = [{"auto_check_enabled": False},
            {"auto_check_enabled": True, "auto_check_host": here, "auto_check_mode": "4h"},
            {"auto_check_enabled": True, "auto_check_host": "another-host", "auto_check_mode": "4h"},
            {"auto_check_enabled": False}]
    turn = {"n": 0}

    class _CfgStore:
        def load_config(self):
            c = cfgs[min(turn["n"], len(cfgs) - 1)]
            turn["n"] += 1
            return c
    rounds: list = []
    slept: list = []
    ran_n = cli_mod._poll(lambda: rounds.append(turn["n"]), _CfgStore(), 7,
                          rounds=4, sleep=slept.append)
    orig_cli_store = cli_mod._store
    try:
        cli_mod._store = lambda: types.SimpleNamespace(load_config=lambda: cfgs[1])
        rc_on = cli_mod.main(["--schedule-check"])
        cli_mod._store = lambda: types.SimpleNamespace(load_config=lambda: cfgs[2])
        rc_other = cli_mod.main(["--schedule-check"])
    finally:
        cli_mod._store = orig_cli_store
    check("S27.g the --interval loop reads the shared settings before EVERY round: it skips the round "
          "while the Beacon page schedules the rounds on this host and resumes afterwards (a schedule "
          "on another host does not stop it); --schedule-check exits 3 / 0 without starting services",
          ran_n == 3 and rounds == [1, 3, 4] and slept == [7, 7, 7, 7]
          and rc_on == cli_mod.EXIT_SCHEDULED_IN_APP == 3 and rc_other == 0,
          f"ran={ran_n} rounds={rounds} slept={slept} rc={rc_on}/{rc_other}")

    # --- h) the Task Scheduler wrapper asks before it opens Chrome ---------------
    spec = importlib.util.spec_from_file_location(
        "handsfree_server_run_s27", Path(__file__).resolve().parents[2] / "tools" / "handsfree_server_run.py")
    wrapper = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(wrapper)
    seq: list = []
    answers: dict = {}

    def _fake_cli(log, *args):
        seq.append(args[0])
        return answers.get(args[0], 0)
    wrapper._cli = _fake_cli
    answers.update({"--schedule-check": 3})
    res_skip, seq_skip = wrapper._round(Path("owners.txt"), None), list(seq)
    seq.clear(); answers.clear()
    res_ok, seq_ok = wrapper._round(Path("owners.txt"), None), list(seq)
    seq.clear(); answers.update({"--schedule-check": 1})
    res_share, seq_share = wrapper._round(Path("owners.txt"), None), list(seq)
    seq.clear(); answers.clear(); answers.update({"--owners-file": 3})
    res_late = wrapper._round(Path("owners.txt"), None)
    check("S27.h the scheduled round asks --schedule-check FIRST: while the Beacon page schedules the "
          "rounds here it is skipped (exit 4) before --sso-check opens the shared Chrome profile; "
          "unreadable settings stop it too; otherwise sign-in check then the round; a schedule switched "
          "on in between is still a skip",
          res_skip[0] == 4 and res_skip[1].startswith("SKIPPED") and seq_skip == ["--schedule-check"]
          and res_ok == (0, "OK") and seq_ok == ["--schedule-check", "--sso-check", "--owners-file"]
          and res_share[0] == 1 and seq_share == ["--schedule-check"]
          and res_late[0] == 4,
          f"skip={res_skip} {seq_skip} ok={res_ok} {seq_ok} share={res_share} {seq_share} late={res_late}")


# ---------------------------------------------------------------- S28
def smoke_review_fixes_2(tmp: Path) -> None:
    print("[S28] PR 190 review, second pass: shared config and scan state across machines, email "
          "drafts in the reviewer's queue, case numbers vs dates, image size, watch folder, skipped task")
    import importlib.util
    import json as _json
    import os as _os
    import subprocess
    import time as _time
    import types
    from . import email_case as ec
    from . import orchestrator as orch
    from . import outlook_source as ol
    from . import queue as queue_mod
    from .queue import HandsfreeStore

    repo = str(Path(__file__).resolve().parents[2])

    def _race(code: str, n_procs: int, *argv) -> list:
        """Run `code` in n_procs separate Python processes at the same time
        (each one a different "machine": its own locks and caches)."""
        procs = [subprocess.Popen([sys.executable, "-c", code, repo, str(i), *map(str, argv)],
                                  stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
                 for i in range(n_procs)]
        return [(p.wait(120), (p.stdout.read() or "").strip()[-300:]) for p in procs]

    # --- a) the shared config: partial updates from different machines all survive
    root = tmp / "handsfree_s28"
    store = HandsfreeStore(root)
    store.save_config({"dry_run": False, "owner_name": "Ann"})
    code_cfg = (
        "import sys; sys.path.insert(0, sys.argv[1])\n"
        "from pathlib import Path\n"
        "from services.handsfree.queue import HandsfreeStore\n"
        "s = HandsfreeStore(Path(sys.argv[3]))\n"
        "for j in range(12):\n"
        "    s.save_config({f'k_{sys.argv[2]}_{j}': j})\n")
    outs = _race(code_cfg, 3, root)
    cfg = _json.loads((root / "config.json").read_text(encoding="utf-8"))
    kept = sum(1 for i in range(3) for j in range(12) if cfg.get(f"k_{i}_{j}") == j)
    leftovers = sorted(p.name for p in root.iterdir() if p.is_file() and p.name != "config.json")

    # a reviewer switches dry-run ON from one PC while the server saves something else
    client, server = HandsfreeStore(root), HandsfreeStore(root)
    server.load_config()                                   # the server's snapshot: dry_run False
    client.save_config({"dry_run": True})
    server.save_config({"latest_driver": "24.90.0.3"})     # must not put dry_run back to False
    after = HandsfreeStore(root).load_config()

    # a lock held by another machine: wait, then refuse - and write nothing
    orig_wait = queue_mod.SHARED_LOCK_WAIT_S
    lock = root / "config.json.lock"
    before_bytes = (root / "config.json").read_bytes()
    queue_mod.SHARED_LOCK_WAIT_S = 0.4
    lock.write_text("other-pc", encoding="utf-8")
    try:
        try:
            store.save_config({"dry_run": False})
            held = ""
        except TimeoutError as e:
            held = str(e)
        unchanged = (root / "config.json").read_bytes() == before_bytes
        old = _time.time() - queue_mod.SHARED_LOCK_STALE_S - 5
        _os.utime(lock, (old, old))                        # left by a process that died
        took_over = store.save_config({"max_cases_per_run": 4})["max_cases_per_run"] == 4
    finally:
        queue_mod.SHARED_LOCK_WAIT_S = orig_wait
    # an unparseable config is set aside, never silently merged into the defaults
    (root / "config.json").write_text("{broken", encoding="utf-8")
    recovered = store.save_config({"owner_name": "Bob"})
    aside = [p.name for p in root.iterdir() if ".corrupt-" in p.name]
    check("S28.a save_config is one transaction across machines: 3 processes x 12 partial updates all "
          "kept (no lock or temp file left); a reviewer's dry_run=True survives the server's next save; "
          "a held lock is waited for and then refused without writing; a stale lock is taken over; an "
          "unparseable config is set aside",
          all(rc == 0 for rc, _ in outs) and kept == 36 and leftovers == []
          and cfg.get("dry_run") is False and cfg.get("owner_name") == "Ann"
          and after["dry_run"] is True and after["latest_driver"] == "24.90.0.3"
          and after["owner_name"] == "Ann"
          and "busy on another machine" in held and unchanged and took_over and not lock.exists()
          and recovered["owner_name"] == "Bob" and len(aside) == 1,
          f"outs={outs} kept={kept}/36 leftovers={leftovers} dry_run_after={after.get('dry_run')} "
          f"held={held!r} unchanged={unchanged} took_over={took_over} aside={aside}")

    # --- b) "my" drafts: IPS case owner OR this PC's mailbox ---------------------
    qstore = HandsfreeStore(tmp / "handsfree_s28_queue")
    mk = lambda nbr, owner, email=None: qstore.enqueue(
        case_nbr=nbr, case_id="", subject="s " + nbr, draft_plain="b", draft_html="<p>b</p>",
        confidence=None, mode="full", analysis={}, owner_name=owner,
        source="email" if email else "ips")
    d_ips = mk("01060001", "Ann")
    d_mine = mk("01060002", "")
    d_other = mk("01060003", "")
    qstore.update(d_mine["draft_id"], email={"sender_name": "Cust", "mailbox": "me@intel.com"})
    qstore.update(d_other["draft_id"], email={"sender_name": "Cust", "mailbox": "lin@intel.com"})
    ids = lambda **kw: sorted(d["case_nbr"] for d in qstore.list_drafts(**kw))
    check("S28.b an Outlook-intake draft has no IPS case owner: the queue lists it for the mailbox it "
          "came from, next to the cases the signed-in reviewer owns; another mailbox's draft stays out; "
          "without any filter (everyone) all are listed",
          ids(owner_name="Ann", mailbox="ME@intel.com") == ["01060001", "01060002"]
          and ids(owner_name="ann") == ["01060001"]
          and ids(mailbox="me@intel.com") == ["01060002"]
          and ids(owner_name="Nobody", mailbox="nobody@intel.com") == []
          and ids() == ["01060001", "01060002", "01060003"]
          and next(d for d in qstore.list_drafts() if d["case_nbr"] == "01060002")["email"]["mailbox"]
          == "me@intel.com",
          f"{ids(owner_name='Ann', mailbox='ME@intel.com')} {ids(mailbox='me@intel.com')} {ids()}")

    # --- c) a date in the subject is not a case ---------------------------------
    check("S28.c trigger_cases takes the canonical case number only (eight digits with the leading "
          "zero): a date after it is not scheduled, where it used to stay pending forever and take a "
          "slot of the case limit on every scan",
          ec.trigger_cases("IPS 01031658 update 20261110") == ["01031658"]
          and ec.trigger_cases("RE: IPS 20261110 status") == []
          and ec.trigger_cases("IPS 01031658 and IPS 01032299 on 20261110") == ["01031658", "01032299"]
          and ec.trigger_cases("Case 01031658 update") == []
          and ec.trigger_cases("Fwd: IPS 01031658 keeps disconnecting") == ["01031658"],
          f"{ec.trigger_cases('IPS 01031658 update 20261110')} {ec.trigger_cases('RE: IPS 20261110 status')}")

    # --- d) an oversized image is skipped before it is read ---------------------
    img_dir = tmp / "s28_img"
    img_dir.mkdir(parents=True, exist_ok=True)
    png = b"\x89PNG\r\n\x1a\n"
    small, edge, big = img_dir / "small.png", img_dir / "edge.png", img_dir / "big.png"
    small.write_bytes(png + b"x" * 40)
    edge.write_bytes(png + b"x" * (64 - len(png)))
    with open(big, "wb") as f:                              # sparse: 5 MB + 1 byte, named .png
        f.write(png)
        f.seek(ec.MAX_IMAGE_BYTES)
        f.write(b"\0")
    opened: list = []
    real_open = open

    def _spy_open(path, *a, **kw):
        opened.append(str(path))
        return real_open(path, *a, **kw)
    ec.open = _spy_open                                     # module-level name used by read_image_file
    try:
        got_small = ec.read_image_file(small)
        got_edge = ec.read_image_file(edge, limit=64)
        got_over = ec.read_image_file(edge, limit=63)
        got_big = ec.read_image_file(big)
    finally:
        del ec.open
    msg = ec.EmailMessage(entry_id="I1", store_id="S", folder="Inbox", subject="IPS 01031658 x",
                          sender_name="Ann", sender_email="ann@oem.com",
                          received="2026-10-22T10:15:00+08:00", body="see screenshots")
    ctx_i = types.SimpleNamespace(
        comments=[], messages=[msg],
        image_files=[(msg, {"name": "big.png", "path": str(big)}),
                     (msg, {"name": "small.png", "path": str(small)})])
    vision = {"n": 0, "images": 0}
    from . import comment_images as ci
    orig_describe = ci.describe_images

    def _describe(llm, images, **kw):
        vision["n"] += 1
        vision["images"] += len(images)
        return ["a small screenshot"] * len(images)
    said: list = []
    ci.describe_images = _describe
    try:
        _rows, info = ec.annotate_email_images(
            types.SimpleNamespace(model="claude-4-6-sonnet"), ctx_i,
            cache_path=img_dir / "cache.json", progress=said.append)
    finally:
        ci.describe_images = orig_describe
    check("S28.d an image attachment larger than the limit is never loaded: its size is checked first "
          "(the file is not even opened) and the read is capped; it is skipped, not truncated, and the "
          "run says so; the other images of the mail are still read",
          got_small == small.read_bytes() and got_edge == edge.read_bytes()
          and got_over is None and got_big is None
          and str(big) not in opened and str(small) in opened
          and vision == {"n": 1, "images": 1}
          and info and info[0]["descriptions"] == ["a small screenshot"]
          and any("larger than 5 MB" in s_ for s_ in said),
          f"over={got_over} big={got_big is None} opened={[Path(o).name for o in opened]} "
          f"vision={vision} info={info} said={said}")

    # --- e) the Outlook scan state: marks from different machines all survive ----
    sroot = tmp / "s28_state"
    sroot.mkdir(parents=True, exist_ok=True)
    code_state = (
        "import sys; sys.path.insert(0, sys.argv[1])\n"
        "from services.handsfree import outlook_source as ol\n"
        "for j in range(12):\n"
        "    ol.mark_processed(sys.argv[3], f'E_{sys.argv[2]}_{j}', '0100000' + sys.argv[2])\n")
    outs_s = _race(code_state, 3, sroot)
    st = ol.load_state(sroot)
    marks = sum(1 for i in range(3) for j in range(12)
                if st["processed"].get(f"E_{i}_{j}", {}).get("case_nbrs") == [f"0100000{i}"])
    ol.set_last_scan(sroot, "2026-11-10T18:00:00+08:00")
    st2 = ol.load_state(sroot)
    slock = ol.state_path(sroot).with_name(ol.STATE_FILE + ".lock")
    queue_mod.SHARED_LOCK_WAIT_S = 0.4
    slock.write_text("other-pc", encoding="utf-8")
    try:
        try:
            ol.mark_processed(sroot, "E_late", "01009999")
            s_held = ""
        except TimeoutError as e:
            s_held = str(e)
    finally:
        queue_mod.SHARED_LOCK_WAIT_S = orig_wait
        slock.unlink()
    s_left = sorted(p.name for p in sroot.iterdir() if p.name != ol.STATE_FILE)
    check("S28.e the processed-mail record is one transaction across machines: 3 processes x 12 marks "
          "all kept; stamping the scan time keeps every mark; a held lock is refused without writing; "
          "no temp file left",
          all(rc == 0 for rc, _ in outs_s) and marks == 36
          and st2["last_scan_at"] == "2026-11-10T18:00:00+08:00" and len(st2["processed"]) == 36
          and "busy on another machine" in s_held and "E_late" not in ol.load_state(sroot)["processed"]
          and s_left == [],
          f"outs={outs_s} marks={marks}/36 processed={len(st2['processed'])} held={s_held!r} left={s_left}")

    # --- f) the watched folder is where the thread is collected from -------------
    seen_folders: list = []

    class _NoThread:
        def thread(self, nbr, *, folders=("Inbox", "Sent Items"), own_email=""):
            seen_folders.append(tuple(folders))
            return []
    try:
        orch._email_case_pass(store, _NoThread(), "01050002", own="me@intel.com",
                              reply_all=True, folder="Inbox/Beacon")
        f_err = ""
    except RuntimeError as e:
        f_err = str(e)
    check("S28.f a case triggered from a watched subfolder collects its thread there too (thread() "
          "does not descend into subfolders): the watch folder first, then Inbox and Sent Items, no "
          "folder twice; the error names the folders searched",
          orch._thread_folders("Inbox/Beacon") == ("Inbox/Beacon", "Inbox", "Sent Items")
          and orch._thread_folders("inbox") == ("inbox", "Sent Items")
          and orch._thread_folders("") == ("Inbox", "Sent Items")
          and orch._thread_folders(" Sent Items ") == ("Sent Items", "Inbox")
          and seen_folders == [("Inbox/Beacon", "Inbox", "Sent Items")]
          and f_err == "no mail in the Inbox/Beacon, Inbox or Sent Items names case 01050002",
          f"{orch._thread_folders('Inbox/Beacon')} {seen_folders} {f_err!r}")

    # --- g) a skipped scheduled task is not CLI activity -------------------------
    spec = importlib.util.spec_from_file_location(
        "handsfree_server_run_s28", Path(repo) / "tools" / "handsfree_server_run.py")
    wrapper = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(wrapper)
    from datetime import datetime
    ldir = tmp / "s28_logs"
    ldir.mkdir(parents=True, exist_ok=True)
    t_ok = wrapper._record(ldir, datetime(2026, 11, 9, 2, 0), 0, "OK", ldir / "a.log")
    old = _time.time() - 30 * 3600                          # that real round was 30 hours ago
    _os.utime(t_ok, (old, old))
    t_skip = wrapper._record(ldir, datetime(2026, 11, 10, 2, 0), wrapper._SKIPPED[0],
                             wrapper._SKIPPED[1], ldir / "b.log")
    marker = ldir / "last_status.txt"
    age_h = (_time.time() - marker.stat().st_mtime) / 3600
    t_fail = wrapper._record(ldir, datetime(2026, 11, 10, 6, 0), 1, "scan failed (cli exit 1)", ldir / "c.log")
    check("S28.g a round skipped because the Beacon page schedules the rounds writes last_skipped.txt "
          "and leaves last_status.txt - the CLI-activity marker the page reads - untouched, so the page "
          "is not locked out for 24 hours; a round that really ran (OK or failed) refreshes the marker",
          t_ok.name == "last_status.txt" and t_skip.name == "last_skipped.txt"
          and age_h > 29 and (ldir / "last_skipped.txt").read_text(encoding="utf-8").startswith("2026-11-10 02:00 SKIPPED")
          and t_fail.name == "last_status.txt"
          and marker.read_text(encoding="utf-8").startswith("2026-11-10 06:00 scan failed"),
          f"{t_ok.name} {t_skip.name} age_h={age_h:.1f} {t_fail.name}")


# ---------------------------------------------------------------- S29
def smoke_review_fixes_3(tmp: Path) -> None:
    print("[S29] PR 190 review, third pass: cross-site requests, Echo in Outlook replies, the posting "
          "browser's identity, a server start onto a running client")
    import os as _os
    import types
    from flask import Flask
    from blueprints.handsfree import handsfree_routes as routes
    from . import orchestrator as orch
    from . import outlook_source as ol
    from . import scheduler as sch
    from . import ui_commenter as uic
    from .composer import AI_MARKER, ECHO_SECTION_END, ECHO_SECTION_START
    from .queue import HandsfreeStore

    # --- a) a page of another site cannot change the shared state ---------------
    store = HandsfreeStore(tmp / "handsfree_s29")
    store.save_config({"dry_run": False, "owner_name": "Ann"})
    rec = store.enqueue(case_nbr="01070001", case_id="500A", subject="s", draft_plain="b",
                        draft_html="<p>b</p>", confidence=None, mode="full", analysis={})
    from . import bsod as bsod_mod
    orig_store, orig_sub = routes._store, bsod_mod._submissions_path
    routes._store = lambda: store
    bsod_mod._submissions_path = lambda: tmp / "handsfree_s29" / "bsod_submissions.json"
    try:
        app = Flask("handsfree_smoke_s29")
        app.secret_key = "s29"
        app.register_blueprint(routes.handsfree_bp)
        client = app.test_client()
        reset = "/handsfree/case/01070001/reset"
        local = {"REMOTE_ADDR": "127.0.0.1"}
        # what a cross-site HTML form does: urlencoded body, the other site's Origin
        r_form = client.post(reset, data={"purge_downloads": "1"}, environ_base=local,
                             headers={"Origin": "https://evil.example"})
        # a form without an Origin header (older browser): still not JSON
        r_form_no_origin = client.post(reset, data={"purge_downloads": "1"}, environ_base=local)
        r_empty = client.post(reset, environ_base=local)
        # JSON, but from another site (a fetch that got past the browser somehow)
        r_json_evil = client.post(reset, json={}, environ_base=local,
                                  headers={"Origin": "https://evil.example"})
        r_cfg_evil = client.post("/handsfree/config", json={"dry_run": True, "owner_name": "Mallory"},
                                 headers={"Origin": "https://evil.example"})
        r_reject_evil = client.post(f"/handsfree/queue/{rec['draft_id']}/reject", json={"reason": "x"},
                                    headers={"Origin": "null"})
        untouched = (store.get(rec["draft_id"])["status"] == "pending_review"
                     and store.load_config()["owner_name"] == "Ann"
                     and store.load_config()["dry_run"] is False
                     and len(store.list_drafts()) == 1)
        # GET is never refused; the app's own pages are same-origin
        r_get_evil = client.get("/handsfree/config", headers={"Origin": "https://evil.example"})
        r_cfg_same = client.post("/handsfree/config", json={"max_cases_per_run": 5},
                                 headers={"Origin": "http://localhost"})
        r_cfg_tool = client.post("/handsfree/config", json={"max_cases_per_run": 6})   # a script: no Origin
        r_reset_same = client.post(reset, json={"purge_downloads": False}, environ_base=local,
                                   headers={"Origin": "http://localhost"})
        check("S29.a cross-site requests cannot change shared state: an HTML form POST to the reset "
              "route is refused (other Origin -> 403; no JSON body -> 415) and nothing is archived; "
              "JSON from another origin is refused on every write route (reset, config, reject); "
              "reading, the app's own pages and scripts without an Origin still work",
              r_form.status_code == 403 and r_form_no_origin.status_code == 415
              and r_empty.status_code == 415 and r_json_evil.status_code == 403
              and r_cfg_evil.status_code == 403 and r_reject_evil.status_code == 403 and untouched
              and r_get_evil.status_code == 200
              and r_cfg_same.status_code == 200 and r_cfg_tool.status_code == 200
              and store.load_config()["max_cases_per_run"] == 6
              and r_reset_same.status_code == 200 and r_reset_same.get_json()["ok"] is True
              and store.list_drafts() == [],
              f"form={r_form.status_code} no_origin={r_form_no_origin.status_code} empty={r_empty.status_code} "
              f"json_evil={r_json_evil.status_code} cfg_evil={r_cfg_evil.status_code} "
              f"reject_evil={r_reject_evil.status_code} untouched={untouched} get={r_get_evil.status_code} "
              f"same={r_cfg_same.status_code}/{r_cfg_tool.status_code} reset_same={r_reset_same.status_code} "
              f"{r_reset_same.get_json()}")
    finally:
        routes._store, bsod_mod._submissions_path = orig_store, orig_sub

    # --- b) the Echo section never goes into an Outlook reply --------------------
    estore = HandsfreeStore(tmp / "handsfree_s29_mail")
    estore.save_config({"dry_run": False})
    body = (AI_MARKER + "\n\nAnalysis: the AP rejected the association.\n\n"
            + ECHO_SECTION_START + "\nINTERNAL ROOT CAUSE: firmware bug HSD-123, do not share\n"
            + ECHO_SECTION_END + "\n")
    from .composer import compose_html
    mail = {"entry_id": "E1", "store_id": "S1", "subject": "IPS 01070002 drops", "sender_name": "Ann",
            "sender_email": "ann@oem.com", "received": "2026-11-10T10:00:00+08:00",
            "reply_all": True, "thread_size": 1, "mailbox": "me@intel.com"}
    e_rec = estore.enqueue(case_nbr="01070002", case_id="", subject="IPS 01070002 drops",
                           draft_plain=body, draft_html=compose_html(body), confidence=70,
                           mode="full", analysis={}, source="email")
    estore.update(e_rec["draft_id"], email=mail)
    saved: list = []

    class _FakeOutlook:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def save_reply_draft(self, ref, html_fragment, *, reply_all=True, display=False):
            saved.append(html_fragment)
            return {"entry_id": "DRAFT-1", "to": "ann@oem.com", "subject": "RE: " + ref["subject"]}
    orig = (orch._store, ol.Outlook)
    orch._store = lambda: estore
    ol.Outlook = _FakeOutlook
    try:
        # the reviewer leaves "Post as: Internal" untouched - the default for every draft
        res = orch.approve_and_post(e_rec["draft_id"], visibility="internal")
        res_none = None
        e_rec2 = estore.enqueue(case_nbr="01070003", case_id="", subject="IPS 01070003",
                                draft_plain=body, draft_html=compose_html(body), confidence=70,
                                mode="full", analysis={}, source="email")
        estore.update(e_rec2["draft_id"], email=dict(mail, subject="IPS 01070003"))
        res_none = orch.approve_and_post(e_rec2["draft_id"])          # no visibility at all
    finally:
        orch._store, ol.Outlook = orig
    kept = estore.get(e_rec["draft_id"])
    check("S29.b an email draft's reply is addressed to the customer whatever 'Post as' says, so the "
          "INTERNAL Echo section is left out of the Outlook reply on every approval (internal or no "
          "visibility) while the analysis itself goes in; the review record keeps the full text",
          res["ok"] and res_none and res_none["ok"] and len(saved) == 2
          and all("Echo (INTERNAL" not in s_ and "HSD-123" not in s_ and "End of Echo" not in s_
                  and "the AP rejected the association" in s_ for s_ in saved)
          and "HSD-123" in kept["draft_plain"] and ECHO_SECTION_START in kept["draft_plain"]
          and kept["status"] == "posted" and kept["post_result"]["backend"] == "outlook",
          f"res={res.get('ok')}/{res.get('error')} saved={[s_[:160] for s_ in saved]} "
          f"kept_has_echo={'HSD-123' in kept['draft_plain']}")

    # --- c) the browser that posts must be the reviewer ---------------------------
    ann = {"user_id": "005A000000ABCDEFGH", "name": "Ann Reviewer"}
    ip = uic.identity_problem
    pure_ok = (ip(ann, {"user_id": "005A000000ABCDE", "name": "Ann R."}) == ""        # 15 vs 18 chars
               and ip(ann, {"user_id": "005A000000ABCDEFGH", "name": "x"}) == ""
               and "not as the reviewer 'Ann Reviewer'" in ip(ann, {"user_id": "005B000000ZZZZZ", "name": "Svc Account"})
               and "Svc Account" in ip(ann, {"user_id": "005B000000ZZZZZ", "name": "Svc Account"})
               and "could not confirm" in ip(ann, None) and "could not confirm" in ip(ann, {})
               and ip({"name": "Ann Reviewer"}, {"name": "ann reviewer"}) == ""       # no ids: by name
               and ip({"name": "Ann Reviewer"}, {"name": "Bob"}) != ""
               and ip(None, {"name": "anyone"}) == "" and ip({}, None) == "")

    class _Resp:
        def __init__(self, code, data): self.status_code, self._d = code, data
        def json(self): return self._d
    calls: list = []

    class _Driver:
        def __init__(self, cookies):
            self.cookies, self.current_url = cookies, "https://intel.lightning.force.com/lightning/r/Case/500A/view"
            self.looked_up, self.quit_called, self.opened = 0, False, ""
        def get(self, url): self.opened = url
        def execute_cdp_cmd(self, cmd, params): return {"cookies": self.cookies}
        def find_element(self, *a, **k): self.looked_up += 1; raise RuntimeError("no page")
        def find_elements(self, *a, **k): self.looked_up += 1; return []
        def save_screenshot(self, p): return False
        page_source = ""
        def quit(self): self.quit_called = True
    sid_cookies = [{"name": "sid", "value": "OTHER", "domain": "intel.lightning.force.com"},
                   {"name": "sid", "value": "TOKEN-1", "domain": "intel.my.salesforce.com"}]
    orig_get = uic._http_get

    def _fake_get(url, **kw):
        calls.append((url, kw.get("headers", {}).get("Authorization")))
        return _Resp(200, {"user_id": "005B000000ZZZZZAAA", "name": "Svc Account", "email": "svc@intel.com"})
    uic._http_get = _fake_get
    try:
        who = uic.browser_identity(_Driver(sid_cookies))
        who_none = uic.browser_identity(_Driver([{"name": "sid", "value": "X", "domain": "other.force.com"}]))
        uic._http_get = lambda url, **kw: _Resp(401, {})
        who_401 = uic.browser_identity(_Driver(sid_cookies))
        uic._http_get = _fake_get

        # the whole post: saved profile used, identity checked before any typing
        from configs.global_configs import app_config
        from services.case_info_service import CaseService
        made: list = []

        class _DM:
            all_drivers: list = []
            def create_download_driver(self, path, **kw):
                d = _Driver(sid_cookies)
                made.append((d, kw))
                return d
        saved_cfg = (getattr(app_config, "driver_manager", None), getattr(app_config, "avatarfiles_dir", None))
        app_config.driver_manager = _DM()
        app_config.avatarfiles_dir = str(tmp / "s29_avatarfiles")
        profile_dir = CaseService._sso_profile_dir()
        _os.makedirs(profile_dir, exist_ok=True)                          # this PC has a saved sign-in
        try:
            res_ui = uic.UiCommenter().post_comment("500A", "text", expected_user=ann)
            lock_free = CaseService._sso_lock.acquire(blocking=False)
            if lock_free:
                CaseService._sso_lock.release()
            uic._http_get = lambda url, **kw: _Resp(401, {})
            res_unknown = uic.UiCommenter().post_comment("500A", "text", expected_user=ann)
        finally:
            app_config.driver_manager, app_config.avatarfiles_dir = saved_cfg
    finally:
        uic._http_get = orig_get
    d0, kw0 = made[0]
    check("S29.c the UI poster opens the SAME sign-in as the REST session (the saved profile when there "
          "is one) and reads the browser's own IPS user (its my.salesforce.com sid on /userinfo) before "
          "typing: another user, or one that cannot be read, posts nothing - no element is touched, the "
          "browser is closed and the profile lock released",
          pure_ok and who and who["name"] == "Svc Account"
          and calls[0] == ("https://intel.my.salesforce.com/services/oauth2/userinfo", "Bearer TOKEN-1")
          and who_none is None and who_401 is None
          and kw0.get("user_data_dir") == profile_dir
          and res_ui.ok is False and res_ui.backend == "ui"
          and "signed in to IPS as 'Svc Account'" in res_ui.error and "nothing was posted" in res_ui.error
          and d0.looked_up == 0 and d0.quit_called and lock_free
          and res_unknown.ok is False and "could not confirm" in res_unknown.error
          and made[1][0].looked_up == 0,
          f"pure={pure_ok} who={who} calls={calls[:1]} none={who_none} 401={who_401} kw={kw0} "
          f"ui={res_ui.error!r} looked_up={d0.looked_up} lock_free={lock_free} unknown={res_unknown.error!r}")

    # --- d) starting the server onto a running client ------------------------------
    rm = sch.role_mismatch
    msg = rm("server", "client", "http://127.0.0.1:48599")
    check("S29.d a server start does not settle for an Avatar that is running as a client (or an older "
          "build): it says so and how to fix it instead of opening that instance's page; a client start "
          "accepts any instance; an instance that cannot be asked reports no role",
          "as the Beacon CLIENT" in msg and "was not started" in msg and "stop that Avatar first" in msg
          and "http://127.0.0.1:48599" in msg
          and "older build" in rm("server", "") and rm("server", "server") == ""
          and rm("client", "server") == "" and rm("client", "client") == "" and rm("client", "") == ""
          and sch.running_role("http://127.0.0.1:9", timeout=0.5) == "",
          f"{msg!r} / {rm('server', '')!r}")


# ---------------------------------------------------------------- S30
def smoke_review_fixes_4(tmp: Path) -> None:
    print("[S30] PR 190 review, fourth pass: an approval is reconciled by its own attempt record; "
          "a CLI-only server keeps the latest driver current")
    import types
    from . import cli as cli_mod
    from . import driver_release as dr
    from . import orchestrator as orch
    from . import outlook_source as ol
    from .composer import AI_MARKER, compose_html
    from .queue import HandsfreeStore

    # --- a) each approval leaves its own record on the draft ----------------------
    store = HandsfreeStore(tmp / "handsfree_s30")
    store.save_config({"dry_run": False})
    body = AI_MARKER + "\n\nanalysis"
    mail = {"entry_id": "E1", "store_id": "S1", "subject": "IPS 01080001 x", "sender_name": "Ann",
            "sender_email": "ann@oem.com", "received": "2026-11-10T10:00:00+08:00",
            "reply_all": True, "thread_size": 1, "mailbox": "me@intel.com"}
    rec = store.enqueue(case_nbr="01080001", case_id="", subject="IPS 01080001 x", draft_plain=body,
                        draft_html=compose_html(body), confidence=70, mode="full", analysis={},
                        source="email")
    # an earlier attempt failed: the draft carries THAT failure until a retry finishes
    store.update(rec["draft_id"], email=mail, status="post_failed",
                 post_result={"ok": False, "backend": "outlook", "error": "Outlook was closed"},
                 approve_attempt={"id": "a-earlier-attempt", "state": "done", "ok": False,
                                  "error": "Outlook was closed"})
    seen_mid: dict = {}

    class _FakeOutlook:
        fail = False
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def save_reply_draft(self, ref, html_fragment, *, reply_all=True, display=False):
            # what a page polling the queue sees WHILE the retry is posting
            mid = store.get(rec["draft_id"])
            seen_mid.update(status=mid["status"], error=(mid.get("post_result") or {}).get("error"),
                            attempt=dict(mid.get("approve_attempt") or {}))
            if _FakeOutlook.fail:
                raise ol.OutlookUnavailable("Outlook is not running")
            return {"entry_id": "DRAFT-1", "to": "ann@oem.com", "subject": "RE: x"}
    orig = (orch._store, ol.Outlook)
    orch._store = lambda: store
    ol.Outlook = _FakeOutlook
    try:
        _FakeOutlook.fail = True
        r_fail = orch.approve_and_post(rec["draft_id"], attempt_id="a-retry-0001")
        after_fail = store.get(rec["draft_id"])
        mid_fail = dict(seen_mid)
        _FakeOutlook.fail = False
        r_ok = orch.approve_and_post(rec["draft_id"], attempt_id="a-retry-0002")
        after_ok = store.get(rec["draft_id"])
        mid_ok = dict(seen_mid)
        # refused before anything is posted (dry run): still this attempt's own answer
        store.save_config({"dry_run": True})
        rec2 = store.enqueue(case_nbr="01080002", case_id="", subject="s", draft_plain=body,
                             draft_html=compose_html(body), confidence=70, mode="full", analysis={},
                             source="email")
        store.update(rec2["draft_id"], email=mail)
        r_dry = orch.approve_and_post(rec2["draft_id"], attempt_id="a-dryrun-01")
        after_dry = store.get(rec2["draft_id"])
        # no id (the Beacon page, the CLI, auto-post): nothing extra is recorded
        store.save_config({"dry_run": False})
        rec3 = store.enqueue(case_nbr="01080003", case_id="", subject="s", draft_plain=body,
                             draft_html=compose_html(body), confidence=70, mode="full", analysis={},
                             source="email")
        store.update(rec3["draft_id"], email=mail)
        orch.approve_and_post(rec3["draft_id"])
        orch.approve_and_post(rec3["draft_id"], attempt_id="no good; drop table")     # not a token
        after_plain = store.get(rec3["draft_id"])
    finally:
        orch._store, ol.Outlook = orig
    clean = orch._clean_attempt_id
    check("S30.a an approval with an attempt id records it on the draft: 'running' while it posts - and "
          "at that moment the draft still shows the EARLIER attempt's post_failed and error, which is "
          "why the page must not read the status - then 'done' with its own ok/error; a refusal before "
          "posting is recorded too; without an id (or with a malformed one) nothing is recorded",
          r_fail["ok"] is False
          and mid_fail["status"] == "post_failed" and mid_fail["error"] == "Outlook was closed"
          and mid_fail["attempt"].get("id") == "a-retry-0001" and mid_fail["attempt"].get("state") == "running"
          and after_fail["approve_attempt"]["id"] == "a-retry-0001"
          and after_fail["approve_attempt"]["state"] == "done" and after_fail["approve_attempt"]["ok"] is False
          and "Outlook is not running" in after_fail["approve_attempt"]["error"]
          and r_ok["ok"] is True and mid_ok["status"] == "post_failed"
          and mid_ok["attempt"] == {"id": "a-retry-0002", "state": "running", "at": mid_ok["attempt"].get("at")}
          and after_ok["status"] == "posted"
          and {k: after_ok["approve_attempt"][k] for k in ("id", "state", "ok", "error")}
          == {"id": "a-retry-0002", "state": "done", "ok": True, "error": ""}
          and r_dry["ok"] is False and after_dry["approve_attempt"]["state"] == "done"
          and after_dry["approve_attempt"]["ok"] is False and "dry_run" in after_dry["approve_attempt"]["error"]
          and "approve_attempt" not in after_plain
          and clean("a-retry-0001") == "a-retry-0001" and clean("short") == "" and clean("x" * 65) == ""
          and clean("no good; drop table") == "" and clean(None) == "" and clean("ünïcödé-token") == "",
          f"fail={r_fail.get('error')!r} mid_fail={mid_fail} after_fail={after_fail.get('approve_attempt')} "
          f"mid_ok={mid_ok} after_ok={after_ok.get('status')}/{after_ok.get('approve_attempt')} "
          f"dry={after_dry.get('approve_attempt')} plain={after_plain.get('approve_attempt')}")

    # --- b) the CLI keeps the latest driver current --------------------------------
    from configs.global_configs import app_config
    cfg_store = HandsfreeStore(tmp / "handsfree_s30_cli")
    events: list = []
    orig_dr = (dr.is_refresh_due, dr.refresh_latest_driver)
    orig_cli = (cli_mod._store, orch.run_auto_scan)
    saved_dm = getattr(app_config, "driver_manager", None)
    due = {"v": True}
    dr.is_refresh_due = lambda cfg, now=None: due["v"]

    def _fake_refresh(store_, **kw):
        events.append("refresh")
        store_.save_config({"latest_driver": "24.90.0.3"})
        return {"ok": True, "version": "24.90.0.3", "error": "", "rows": 136}
    dr.refresh_latest_driver = _fake_refresh
    cli_mod._store = lambda: cfg_store

    def _fake_scan(store_, owner, max_cases, trigger="cli", list_views=None):
        events.append(f"scan:{owner}:{store_.load_config().get('latest_driver')}")
        return {"ok": True, "cases": []}
    orch.run_auto_scan = _fake_scan
    try:
        app_config.driver_manager = object()             # this process can open Chrome
        rc = cli_mod._run_once("Ann")                    # a fresh share: never fetched
        due["v"] = False
        rc2 = cli_mod._run_once("Bob")                   # fetched a moment ago: nothing to do
        first_two = list(events)
        due["v"] = True
        app_config.driver_manager = None                 # no Chrome here: skip quietly
        no_chrome = cli_mod._refresh_latest_driver_if_due(cfg_store)
        app_config.driver_manager = object()
        dr.refresh_latest_driver = lambda store_, **kw: {"ok": False, "version": "", "error": "401", "rows": 0}
        failed = cli_mod._refresh_latest_driver_if_due(cfg_store)

        def _boom(store_, **kw):
            raise RuntimeError("Chrome profile in use")
        dr.refresh_latest_driver = _boom
        raised = cli_mod._refresh_latest_driver_if_due(cfg_store)
    finally:
        dr.is_refresh_due, dr.refresh_latest_driver = orig_dr
        cli_mod._store, orch.run_auto_scan = orig_cli
        app_config.driver_manager = saved_dm
    check("S30.b a CLI round asks whether the latest released driver is due BEFORE it scans and fetches "
          "it then (the scan already sees the value) - a Task Scheduler-only server has no in-app "
          "scheduler to do it; nothing is fetched when it is not due or this process has no Chrome; a "
          "failed or crashing fetch never fails the round",
          rc == 0 and rc2 == 0
          and first_two == ["refresh", "scan:Ann:24.90.0.3", "scan:Bob:24.90.0.3"]
          and no_chrome is False and failed is True and raised is False,
          f"rc={rc}/{rc2} events={events} no_chrome={no_chrome} failed={failed} raised={raised}")


def run_smoke() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="handsfree_smoke_"))
    try:
        smoke_soql()
        smoke_composer()
        from . import runner as _runner_mod
        _runner_mod.HandsfreeRunner.ECHO_ADVICE_ENABLED = False   # no network in the smoke
        smoke_queue(tmp)
        smoke_runner(tmp)
        smoke_orchestrator(tmp)
        smoke_ui_commenter()
        smoke_echo_kb()
        smoke_time_coverage(tmp)
        smoke_yb_etl_pick()
        smoke_env_detail()
        smoke_checklist()
        smoke_checklist_convert(tmp)
        smoke_auto_scan(tmp)
        smoke_comment_images(tmp)
        smoke_email_intake(tmp)
        smoke_identity()
        smoke_snowflake_reconnect()
        smoke_server_role(tmp)
        smoke_chatbot_handoff(tmp)
        smoke_case_requests(tmp)
        smoke_driver_release(tmp)
        smoke_echo_isolation(tmp)
        smoke_regression_item()
        smoke_review_fixes(tmp)
        smoke_review_fixes_2(tmp)
        smoke_review_fixes_3(tmp)
        smoke_review_fixes_4(tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print(f"\nsmoke result: {'ALL PASS' if not PASS_FAIL else 'FAILURES: ' + ', '.join(PASS_FAIL)}")
    return 0 if not PASS_FAIL else 1


if __name__ == "__main__":
    sys.exit(run_smoke())
