"""
Headless Beacon (handsfree replyer) simulation — run real IPS cases through the full
pipeline WITHOUT launching the Avatar web app.

Boots app_config exactly like the app (configs.set_up_app.set_up with a no-op
SocketIO stub so the ETL decoder can't crash on socketio.emit), then runs the
orchestrator's per-case body synchronously. Drafts land in the REAL review
queue (<avatarfiles>/handsfree/queue) as pending_review — NOTHING posts;
posting always requires an explicit Approve in the /handsfree UI.

Run with the app's Python environment (the intel_ava venv), from repo root:

    # one or more specific cases
    python tools/handsfree_sim.py 01031455 01029146

    # enumerate an IPS list view (e.g. the validation filter), no runs
    python tools/handsfree_sim.py --list Handsfree_replier_test_case

    # run every case from a list view (mind the LLM token cost!)
    python tools/handsfree_sim.py --list Handsfree_replier_test_case --run-all

    # include the full draft texts in the output
    python tools/handsfree_sim.py 01031455 --show-drafts

Requirements: corporate network (Snowflake + key share + Salesforce SSO
reachable); each case run spends real LLM tokens (triage + reader + agent +
checklist fill, plus Echo KB when asserts are found).
"""

from __future__ import annotations

import argparse
import sys
import time
import traceback
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))


class _NullSocketIO:
    """set_up() stores this; wpp_ddd_parser calls .emit() unguarded."""

    def emit(self, *args, **kwargs):
        pass

    def start_background_task(self, target, *args, **kwargs):
        return None


def _boot():
    import os
    os.chdir(REPO_ROOT)
    print("=== boot: set_up(app_config) — same init the app performs ===",
          flush=True)
    from configs.set_up_app import set_up
    set_up(_NullSocketIO())
    from configs.global_configs import app_config
    if getattr(app_config, "key", None) is None:
        sys.exit("KEY NOT LOADED — the key share is unreachable (open "
                 r"\\infs089.iil.intel.com\HOME\WirelessCE\Intel_WirelessCE_Avatar\key "
                 "in Explorer to re-authenticate, then retry).")
    return app_config


def list_view_cases(list_view_name: str) -> list[dict]:
    """[{CaseNumber, Subject, ...}] from a Salesforce Case list view."""
    from services.handsfree.ips_client import IpsClient
    ips = IpsClient()
    lv_id, url, params = None, "/services/data/v{ver}/sobjects/Case/listviews", \
        {"limit": 200}
    while url and lv_id is None:
        r = ips._request("GET", url, params=params)
        r.raise_for_status()
        d = r.json()
        for lv in d.get("listviews", []):
            if lv.get("developerName") == list_view_name:
                lv_id = lv.get("id")
                break
        url, params = d.get("nextRecordsUrl"), None
    if not lv_id:
        sys.exit(f"list view not found: {list_view_name}")
    r = ips._request(
        "GET",
        "/services/data/v{ver}/sobjects/Case/listviews/" + lv_id + "/results",
        params={"limit": 200})
    r.raise_for_status()
    return [{c.get("fieldNameOrPath"): c.get("value")
             for c in rec.get("columns", [])}
            for rec in r.json().get("records", [])]


def _live_poc_case_context(case_nbr: str):
    """Build a case context from live Salesforce + IPS PDF, bypassing Snowflake lag."""
    from models.models import CaseContext
    from services.case_info_service import CaseService
    from services.handsfree.ips_client import IpsClient
    from utils import case_utils

    ref = IpsClient().case_by_number(str(case_nbr))
    if ref is None:
        raise LookupError(f"Salesforce did not return case {case_nbr}")
    context = CaseContext(case_nbr=str(case_nbr), id=ref.case_id, subject=ref.subject)
    CaseService._prepare_case_download_dir(context)
    context.ips_pdf_path = CaseService._download_pdf_by_url(
        context.id, context.case_download_dir)
    context = case_utils.parse_pdf_for_all_info(context.ips_pdf_path, context)
    CaseService._finalize_case_context(context)
    context.attachment_list = case_utils.parse_pdf_for_attachments(
        context.ips_pdf_path, context.attachment_info)
    return context


def classify_case(case_nbr: str, include_intel_comments: bool = False,
                  ask_echo: bool = False, route: bool = False) -> dict:
    """Classify one case, then optionally route its question or debug path."""
    from configs.global_configs import app_config
    from models.models import CaseContext
    from services.case_info_service import CaseService
    from services.handsfree.case_reader import read_case_history

    print(f"\n{'#' * 70}\n### CLASSIFY {case_nbr}\n{'#' * 70}", flush=True)
    t0 = time.time()
    try:
        if include_intel_comments:
            print("  loading live Salesforce/IPS context for POC (Snowflake bypassed)",
                  flush=True)
            case_ctx = _live_poc_case_context(case_nbr)
        else:
            case_ctx = CaseService.process_case(CaseContext(case_nbr=str(case_nbr)))
        if case_ctx.error_message or not case_ctx.id:
            return {"case": case_nbr, "request_type": "FETCH_ERROR",
                    "customer_question": "",
                    "error": (case_ctx.error_message
                              or "case fetch returned no IPS id"),
                    "source": "", "mins": round((time.time() - t0) / 60, 1)}
        comments = case_ctx.comments or []
        if case_ctx.id:
            from bs4 import BeautifulSoup
            from services.handsfree.ips_client import IpsClient

            live = IpsClient().get_case_comments(case_ctx.id)
            if live:
                comments = []
                for record in live:
                    rich = str(record.get(IpsClient.FIELD_RICH_BODY) or "")
                    comments.append([
                        record.get("CreatedDate") or "",
                        record.get("Core_IPS_Comment_Author_Type__c") or "",
                        BeautifulSoup(rich, "html.parser").get_text(" ", strip=True),
                    ])
                print(f"  using {len(comments)} live Salesforce comment(s) for POC",
                      flush=True)
        reader = read_case_history(
            app_config.llm_helper,
            subject=case_ctx.subject or "",
            description=case_ctx.description or "",
            comments=comments,
            attachment_list=case_ctx.attachment_list or [],
            env_detail=case_ctx.env_detail or {},
            include_intel_comments=include_intel_comments,
        )
        if not reader:
            return {"case": case_nbr, "request_type": "ERROR",
                    "customer_question": "", "error": "reader failed",
                    "source": "", "mins": round((time.time() - t0) / 60, 1)}
        source = reader.get("request_source") or {}
        request_type = reader.get("request_type") or "debug"
        echo_status = "skipped"
        echo_answer = ""
        echo_error = ""
        if (ask_echo or route) and request_type == "question":
            from services.handsfree.echo_client import answer_customer_question
            echo_result = answer_customer_question(
                request_type=request_type,
                question=reader.get("customer_question") or "",
                subject=case_ctx.subject or "",
                radio=case_ctx.wifi_or_bt or "")
            echo_status = echo_result["status"]
            echo_answer = echo_result["answer"]
            echo_error = echo_result["error"]

        analysis = None
        if route and request_type in {"debug", "mixed"}:
            from services.handsfree.runner import HandsfreeRunner

            print("  routed debug/mixed case to the existing Handsfree log pipeline",
                  flush=True)
            runner = HandsfreeRunner(progress_cb=lambda stage, detail:
                                     print(f"  [{stage}] {detail}", flush=True))
            analysis = runner.analyze_case(case_nbr, case_ctx=case_ctx)

        return {
            "case": case_nbr,
            "request_type": request_type,
            "customer_question": reader.get("customer_question") or "",
            "source": source.get("kind") or "",
            "source_author": source.get("author") or "",
            "source_text": source.get("text") or "",
            "echo_status": echo_status,
            "echo_answer": echo_answer,
            "echo_error": echo_error,
            "analysis_mode": analysis.mode if analysis else "",
            "analysis_error": analysis.error if analysis else "",
            "failed_stages": ([stage.name for stage in (analysis.stages or [])
                               if not stage.ok] if analysis else []),
            "mins": round((time.time() - t0) / 60, 1),
        }
    except Exception as e:
        traceback.print_exc()
        return {"case": case_nbr, "request_type": "CRASH",
                "customer_question": "", "error": f"{type(e).__name__}: {e}",
                "source": "", "mins": round((time.time() - t0) / 60, 1)}


def run_case(store, case_nbr: str, show_drafts: bool) -> dict:
    from services.handsfree import orchestrator
    print(f"\n{'#' * 70}\n### CASE {case_nbr}\n{'#' * 70}", flush=True)
    t0 = time.time()
    try:
        rec = orchestrator._analyze_and_enqueue(store, case_nbr)
    except Exception as e:
        traceback.print_exc()
        return {"case": case_nbr, "mode": "CRASH", "domain": str(e)[:80],
                "mins": round((time.time() - t0) / 60, 1)}

    a = rec.get("analysis") or {}
    print("\n--- stage results ---")
    for st in a.get("stages") or []:
        mark = "OK " if st.get("ok") else "FAIL"
        detail = f"  [{st.get('detail')}]" if st.get("detail") else ""
        print(f"  {mark} {st.get('name'):18s} "
              f"{st.get('duration_ms', 0):>7}ms{detail}")

    if show_drafts:
        for d in store.list_drafts(include_closed=True):
            if d.get("case_nbr") != case_nbr:
                continue
            full = store.get(d["draft_id"]) or {}
            print(f"\n----- draft {d['draft_id']}  mode={d.get('mode')} -----")
            print(full.get("draft_plain", "(no text)"))

    fills = a.get("checklist_fills") or {}
    filled = sum(1 for sec in fills.values() if isinstance(sec, list)
                 for e in sec if e.get("provided"))
    total = sum(len(sec) for sec in fills.values() if isinstance(sec, list))
    return {
        "case": case_nbr,
        "mode": rec.get("mode"),
        "domain": (a.get("issue_domain") or "")
                  + (f" · {a.get('issue_subcategory')}"
                     if a.get("issue_subcategory") else ""),
        "prefill": f"{filled}/{total}",
        "missing": [m.get("item") for m in a.get("missing_info") or []],
        "failed_stages": [s["name"] for s in a.get("stages", [])
                          if not s.get("ok")],
        "confidence": rec.get("confidence"),
        "mins": round((time.time() - t0) / 60, 1),
    }


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cases", nargs="*", help="IPS case numbers to run")
    ap.add_argument("--list", metavar="LIST_VIEW",
                    help="enumerate a Salesforce Case list view by developer name")
    ap.add_argument("--run-all", action="store_true",
                    help="with --list: run every listed case (token cost!)")
    ap.add_argument("--show-drafts", action="store_true",
                    help="print the queued draft texts for each case")
    ap.add_argument("--classify-only", action="store_true",
                    help="classify without enqueueing drafts; add --ask-echo or --route "
                         "to continue into a POC question/debug path")
    ap.add_argument("--include-intel-comments", action="store_true",
                    help="classification-only POC: allow the latest Agent/FAE "
                         "comment to be the classification source")
    ap.add_argument("--ask-echo", action="store_true",
                    help="classification-only POC: ask Echo when classified as question")
    ap.add_argument("--route", action="store_true",
                    help="POC routing: questions go to Echo; debug/mixed cases run the "
                         "existing Handsfree log pipeline without enqueueing drafts")
    args = ap.parse_args()
    if args.include_intel_comments and not args.classify_only:
        ap.error("--include-intel-comments requires --classify-only")
    if args.ask_echo and not args.classify_only:
        ap.error("--ask-echo requires --classify-only")
    if args.route and not args.classify_only:
        ap.error("--route requires --classify-only")
    if args.route and args.ask_echo:
        ap.error("--route already asks Echo for questions; do not also use --ask-echo")
    if not args.cases and not args.list:
        ap.error("give case numbers, or --list <list view name>")

    app_config = _boot()

    cases = list(args.cases)
    if args.list:
        rows = list_view_cases(args.list)
        print(f"\n=== list view '{args.list}': {len(rows)} case(s) ===")
        for row in rows:
            print(f"  {row.get('CaseNumber')}  {str(row.get('Subject'))[:90]}")
        if args.run_all:
            cases += [str(r.get("CaseNumber")) for r in rows
                      if r.get("CaseNumber")]
    if not cases:
        return 0

    if args.classify_only:
        results = [classify_case(nbr, args.include_intel_comments, args.ask_echo,
                                 args.route) for nbr in cases]
        print(f"\n{'=' * 70}\n=== CLASSIFICATION SUMMARY ===")
        for result in results:
            source = str(result.get("source") or "-")
            if result.get("source_author"):
                source += f"/{result['source_author']}"
            question = str(result.get("customer_question") or "-")
            print(f"  {result['case']}  request_type={str(result.get('request_type')):8s} "
                  f"source={source:24s} question={question[:100]}  "
                  f"echo={str(result.get('echo_status', 'skipped')):11s} "
                  f"({result['mins']}m)")
            if result.get("error"):
                print(f"  Error: {result['error']}")
            if result.get("echo_answer"):
                print("  Echo answer:")
                for line in str(result["echo_answer"]).splitlines():
                    print(f"    {line}")
            elif result.get("echo_error"):
                print(f"  Echo error: {result['echo_error']}")
            if result.get("analysis_mode"):
                print(f"  Analysis mode: {result['analysis_mode']}"
                      f"  failed_stages={result.get('failed_stages') or []}")
                if result.get("analysis_error"):
                    print(f"  Analysis note: {result['analysis_error']}")
        print("\nPOC completed: question routing uses Echo; debug/mixed routing "
              "uses analysis without enqueueing or posting drafts.")
        return 0

    from services.handsfree.queue import HandsfreeStore
    store = HandsfreeStore(Path(app_config.avatarfiles_dir) / "handsfree")

    results = [run_case(store, nbr, args.show_drafts) for nbr in cases]
    print(f"\n{'=' * 70}\n=== SUMMARY ===")
    for r in results:
        print(f"  {r['case']}  mode={str(r.get('mode')):14s} "
              f"domain={str(r.get('domain')):28s} "
              f"prefill={str(r.get('prefill', '-')):7s} "
              f"conf={str(r.get('confidence')):5s} "
              f"failed={r.get('failed_stages', '-')} "
              f"missing={r.get('missing', '-')}  ({r['mins']}m)")
    print("\nDrafts are pending_review in the /handsfree queue — nothing was "
          "posted.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
