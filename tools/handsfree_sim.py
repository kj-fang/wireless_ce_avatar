"""
Headless Handsfree Replyer simulation — run real IPS cases through the full
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
    args = ap.parse_args()
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
