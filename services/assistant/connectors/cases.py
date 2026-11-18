"""
Case connectors: the user's IPS cases, joined with what Beacon did with them.

    list_my_cases         which of my cases moved since <when>, and where each stands
    get_case              one case: IPS state, latest comments, Beacon's latest draft
    start_case_analysis   have Beacon analyse one case (a draft for review; no post)
    analysis_status       is that analysis queued / running / done

IPS is read live over the user's own session (services/handsfree/ips_client);
Beacon's side comes from the shared store (drafts and case requests). The
store sits on a network share where every access costs about a second, so a
listing reads it once - one draft listing, one request listing - and joins in
memory. When the share is unreachable the IPS half is still returned, with a
warning.

Nothing here writes to IPS. start_case_analysis only asks Beacon to analyse:
on the server PC it runs there, on a client it becomes a request file the
server picks up, exactly as the Beacon page's "Analyze this case" does.
"""

from __future__ import annotations

import html
import os
import re
import threading
import time
from datetime import datetime
from typing import Optional

from services.assistant import actions, timeutil
from services.assistant.registry import Connector, Ctx, object_schema

LIST_CACHE_S = 60
MAX_ROWS = 40
DRAFT_LIMIT = 2000
START_KIND = "start_case_analysis"

_cache: dict[tuple, tuple[float, dict]] = {}
_cache_lock = threading.Lock()


# ---- seams (tests replace these) ---------------------------------------------

def _ips():
    from services.handsfree.ips_client import IpsClient
    return IpsClient()


def _store():
    from services.handsfree.queue import open_store
    return open_store()


def _can_run_here() -> bool:
    from services.handsfree import scheduler
    return scheduler.can_schedule_here()


def _start_local_run(case_nbr: str) -> dict:
    from services.handsfree import orchestrator
    return orchestrator.start_case_run(case_nbr)


def _local_run_state() -> dict:
    from services.handsfree import orchestrator
    return orchestrator.get_run_state()


def _normalise(raw) -> str:
    from utils.check_ips_utils import normalise_ips
    return normalise_ips(raw)


def clear_cache() -> None:
    with _cache_lock:
        _cache.clear()


# ---- shared helpers ------------------------------------------------------------

def _not_signed_in() -> dict:
    """The result when Beacon holds no IPS identity yet: kick the silent
    sign-in (never a visible browser) and say what to do."""
    try:
        from services.assistant import runner
        runner.start_silent_sign_in(retry_failed=True)
    except Exception:
        pass
    return {"error": "not_signed_in",
            "message": "This PC is not signed in to IPS yet. A silent sign-in has been started; "
                       "ask again in a moment. If it keeps failing, sign in on the Beacon queue page."}


def named_in(text: str, case_nbr: str) -> bool:
    """Whether the user's own words contain this case number (leading zeros
    optional): the test for running an action without a confirming click."""
    return any(_normalise(run) == case_nbr for run in re.findall(r"\d{5,8}", str(text or "")))


def _plain(rich: str, limit: int) -> str:
    text = re.sub(r"(?is)<(script|style).*?</\1>", " ", str(rich or ""))
    text = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</li>", "\n", text)
    text = html.unescape(re.sub(r"<[^>]+>", " ", text))
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    text = re.sub(r"\s*\n\s*", "\n", text).strip()
    return text if len(text) <= limit else text[:limit].rstrip() + " ..."


def latest_drafts(store) -> dict[str, dict]:
    """{case number: the newest draft summary of that case}. One listing."""
    out: dict[str, dict] = {}
    for slim in store.list_drafts(include_closed=True, limit=DRAFT_LIMIT):
        nbr = str(slim.get("case_nbr") or "")
        if nbr and nbr not in out:            # the listing is newest first
            out[nbr] = slim
    return out


def latest_draft(case_nbr: str) -> Optional[dict]:
    """The full newest draft record of one case, or None."""
    store = _store()
    slim = latest_drafts(store).get(case_nbr)
    return store.get(slim["draft_id"]) if slim else None


def _requests_by_case(store) -> dict[str, dict]:
    """{case number: its live request (running, else pending, else the last
    finished one)}."""
    order = {"running": 0, "pending": 1, "done": 2}
    out: dict[str, dict] = {}
    for req in store.list_requests():
        nbr = str(req.get("case_nbr") or "")
        if nbr and (nbr not in out
                    or order.get(req.get("status"), 9) < order.get(out[nbr].get("status"), 9)):
            out[nbr] = req
    return out


def beacon_view(draft: Optional[dict], request: Optional[dict], now: datetime,
                last_customer_comment: Optional[datetime] = None) -> dict:
    """Where Beacon stands on one case, as {"state", "text", ...}.

    state: needs_review | posted | post_failed | rejected | skipped |
           analysis_failed | running | requested | not_analysed"""
    req_status = str((request or {}).get("status") or "")
    if req_status == "running":
        return {"state": "running", "text": "Beacon is analysing it now"
                + (f" (on {request.get('host')})" if request.get("host") else "")}
    if req_status == "pending":
        return {"state": "requested",
                "text": "Analysis requested " + timeutil.label(request.get("requested_at"), now)
                        + "; waiting for the Beacon server"}
    if not draft:
        if req_status == "done" and request.get("ok") is False:
            return {"state": "analysis_failed",
                    "text": "Requested analysis failed: " + str(request.get("error") or "")[:160]}
        return {"state": "not_analysed", "text": "Not analysed by Beacon"}

    status, mode = str(draft.get("status") or ""), str(draft.get("mode") or "")
    updated = timeutil.parse_stamp(draft.get("updated_at"))
    when = timeutil.label(updated, now) if updated else ""
    view = {"draft_id": draft.get("draft_id") or "", "mode": mode, "draft_updated": when}
    if draft.get("confidence") is not None:
        view["confidence"] = draft.get("confidence")
    for key in ("next_action", "action_owner"):
        if draft.get(key):
            view[key] = str(draft[key])[:240]
    if status == "skipped":
        view.update(state="skipped",
                    text="Beacon skipped it: " + str(draft.get("skip_reason") or "")[:160])
    elif mode == "error":
        view.update(state="analysis_failed", text=f"Beacon's analysis failed ({when})")
    elif status == "posted":
        how = " automatically" if draft.get("auto_posted") else ""
        view.update(state="posted", text=f"Reply posted{how} ({when})")
    elif status == "post_failed":
        err = str((draft.get("post_result") or {}).get("error") or "")[:160]
        view.update(state="post_failed", text=f"Posting failed ({when}): {err}".rstrip(": "))
    elif status == "rejected":
        view.update(state="rejected", text=f"Draft rejected by the reviewer ({when})")
    else:
        kind = {"full": "full analysis", "triage_only": "triage only",
                "request_logs": "asks the customer for logs",
                "request_info": "asks the customer for information",
                "first_response": "first response"}.get(mode, mode or "draft")
        view.update(state="needs_review", text=f"Draft waiting for your review: {kind} ({when})")
    if (last_customer_comment and updated and last_customer_comment > updated
            and view["state"] in ("needs_review", "posted", "rejected")):
        view["stale"] = True
        view["text"] += "; the customer has commented since"
    return view


# ---- list_my_cases -------------------------------------------------------------

def _list_my_cases(args: dict, ctx: Ctx) -> dict:
    if not ctx.owner_name:
        return _not_signed_in()
    now = ctx.now()
    try:
        start = timeutil.window_start(args.get("updated_since"), now)
    except ValueError as e:
        return {"error": str(e)}
    key = (ctx.owner_name.casefold(), timeutil.soql_utc(start), bool(args.get("include_quiet")))
    with _cache_lock:
        hit = _cache.get(key)
        if hit and time.time() - hit[0] <= LIST_CACHE_S:
            return dict(hit[1], cached=True)
    result = build_case_list(ctx.owner_name, ctx.mailbox, start, now,
                             include_quiet=bool(args.get("include_quiet")), emit=ctx.emit)
    if not result.get("error"):
        with _cache_lock:
            _cache[key] = (time.time(), result)
    return result


def build_case_list(owner: str, mailbox: str, start: datetime, now: datetime, *,
                    include_quiet: bool = False, emit=lambda text: None) -> dict:
    since = timeutil.soql_utc(start)
    warnings: list[str] = []
    emit("⏳ Reading your cases from IPS ...")
    try:
        ips = _ips()
        cases = ips.find_my_cases(owner, since)
        activity = ips.find_comment_activity([c["case_id"] for c in cases], since) if cases else {}
    except Exception as e:
        return {"error": f"IPS could not be read: {type(e).__name__}: {e}"}

    drafts: dict[str, dict] = {}
    requests: dict[str, dict] = {}
    try:
        emit("⏳ Reading the Beacon queue ...")
        store = _store()
        drafts = latest_drafts(store)
        requests = _requests_by_case(store)
    except Exception as e:
        warnings.append("The Beacon shared folder could not be read, so Beacon's status is "
                        f"missing from this list ({type(e).__name__}: {str(e)[:160]}).")

    rows, quiet = [], []
    seen = set()
    for case in cases:
        nbr = case["case_nbr"]
        seen.add(nbr)
        comments = activity.get(case["case_id"], [])
        from_customer = [c for c in comments if c["customer"]]
        from_intel = [c for c in comments if not c["customer"]]
        last_customer = timeutil.parse_stamp(from_customer[0]["at"]) if from_customer else None
        draft = drafts.get(nbr)
        draft_at = timeutil.parse_stamp((draft or {}).get("updated_at"))
        modified = timeutil.parse_stamp(case["last_modified"])
        why = []
        if from_customer:
            why.append(f"{len(from_customer)} customer comment(s), latest "
                       + timeutil.label(last_customer, now))
        if from_intel:
            why.append(f"{len(from_intel)} Intel comment(s), latest "
                       + timeutil.label(from_intel[0]["at"], now))
        if draft_at and draft_at >= start:
            why.append("Beacon draft " + timeutil.label(draft_at, now))
        if case["closed"] and (timeutil.parse_stamp(case["closed_date"]) or start) >= start:
            why.append("closed " + timeutil.label(case["closed_date"], now))
        if not why and modified and modified >= start:
            why.append("case record changed " + timeutil.label(modified, now))
        if not why:
            quiet.append(nbr)
            if not include_quiet:
                continue
        beacon = beacon_view(draft, requests.get(nbr), now, last_customer)
        last = max([d for d in (last_customer, draft_at, modified) if d] or [start])
        rows.append({
            "case": nbr,
            "subject": case["subject"][:110],
            "ips_status": case["status"] + (" (closed)" if case["closed"] else ""),
            "updated_because": why or ["no change in this period"],
            "beacon": beacon,
            "_last": last,
            "_rank": _rank(beacon, bool(from_customer)),
        })

    # Beacon drafts of mine that moved in the window but whose case IPS did
    # not list (owner changed, or a draft made from a mail thread).
    for nbr, draft in drafts.items():
        if nbr in seen:
            continue
        mine = (str(draft.get("owner_name") or "").casefold() == owner.casefold()
                or (mailbox and str((draft.get("email") or {}).get("mailbox") or "").casefold()
                    == mailbox.casefold()))
        draft_at = timeutil.parse_stamp(draft.get("updated_at"))
        if not (mine and draft_at and draft_at >= start):
            continue
        beacon = beacon_view(draft, requests.get(nbr), now)
        source = "mail thread" if str(draft.get("source") or "") == "email" else "not in your IPS list"
        rows.append({
            "case": nbr, "subject": str(draft.get("subject") or "")[:110],
            "ips_status": f"({source})",
            "updated_because": ["Beacon draft " + timeutil.label(draft_at, now)],
            "beacon": beacon, "_last": draft_at, "_rank": _rank(beacon, False),
        })

    rows.sort(key=lambda r: (r["_rank"], -r["_last"].timestamp()))
    total = len(rows)
    shown = rows[:MAX_ROWS]
    for row in shown:
        row["last_activity"] = timeutil.label(row.pop("_last"), now)
        row.pop("_rank")
    need_review = sum(1 for r in rows if r["beacon"].get("state") == "needs_review")
    result = {
        "owner": owner,
        "since": timeutil.stamp(start),
        "as_of": timeutil.stamp(now),
        "counts": {"updated": total if not include_quiet else total - len(quiet),
                   "shown": len(shown),
                   "open_without_change": len(quiet),
                   "drafts_waiting_for_review": need_review},
        "cases": shown,
        "summary": f"{total} case(s) for {owner} since {start.strftime('%m-%d %H:%M')}",
    }
    if total > len(shown):
        warnings.append(f"Only the first {len(shown)} of {total} cases are listed.")
    if warnings:
        result["warnings"] = warnings
    return result


def _rank(beacon: dict, customer_spoke: bool) -> int:
    """Listing order: what needs the user first."""
    state = beacon.get("state")
    if beacon.get("stale") or (customer_spoke and state in ("not_analysed", "analysis_failed")):
        return 0                                   # the customer is waiting
    return {"post_failed": 1, "needs_review": 2, "analysis_failed": 3, "running": 4,
            "requested": 4, "not_analysed": 5, "skipped": 6, "rejected": 7,
            "posted": 8}.get(state, 5)


# ---- get_case ------------------------------------------------------------------

def _get_case(args: dict, ctx: Ctx) -> dict:
    case_nbr = _normalise(args.get("case_nbr"))
    if not case_nbr:
        return {"error": f"{args.get('case_nbr')!r} is not an IPS case number "
                         "(six to eight digits, e.g. 01009610)"}
    now = ctx.now()
    out: dict = {"case": case_nbr}
    warnings: list[str] = []

    last_customer = None
    try:
        ips = _ips()
        case = ips.find_case(case_nbr)
        if case:
            out["ips"] = {"subject": case["subject"], "status": case["status"],
                          "closed": case["closed"], "owner": case["owner_name"],
                          "created": timeutil.label(case["created"], now),
                          "last_modified": timeutil.label(case["last_modified"], now)}
            from services.handsfree.ips_client import CUSTOMER_AUTHOR_TYPES
            comments = []
            wanted = max(1, min(8, int(args.get("comments") or 3)))
            for rec in (ips.get_case_comments(case["case_id"]) or [])[:wanted]:
                author = str(rec.get("Core_IPS_Comment_Author_Type__c") or "")
                at = timeutil.parse_stamp(rec.get("CreatedDate"))
                if author in CUSTOMER_AUTHOR_TYPES and at and (last_customer is None
                                                               or at > last_customer):
                    last_customer = at
                comments.append({
                    "at": timeutil.label(at, now) if at else "",
                    "by": author or "?",
                    "visibility": str(rec.get("Core_IPS_Case_Comment_Type__c") or ""),
                    "text": _plain(rec.get("Core_IPS_Rich_Comment__c"), 500),
                })
            out["latest_comments"] = comments
        else:
            warnings.append("IPS has no case with this number (or it is not visible to you).")
    except Exception as e:
        warnings.append(f"IPS could not be read: {type(e).__name__}: {str(e)[:160]}")

    try:
        store = _store()
        slim = latest_drafts(store).get(case_nbr)
        request = _requests_by_case(store).get(case_nbr)
        out["beacon"] = beacon_view(slim, request, now, last_customer)
        record = store.get(slim["draft_id"]) if slim else None
        if record:
            out["beacon_draft"] = _draft_digest(record)
    except Exception as e:
        warnings.append("The Beacon shared folder could not be read: "
                        f"{type(e).__name__}: {str(e)[:160]}")
    if warnings:
        out["warnings"] = warnings
    out["summary"] = f"case {case_nbr}: " + str((out.get("beacon") or {}).get("text") or "IPS only")
    return out


def _draft_digest(record: dict) -> dict:
    """The parts of a Beacon draft worth a model's attention. The Echo section
    of the draft text is left out: Echo content reaches an answer only through
    the Echo connectors, which mark it internal."""
    from services.handsfree.composer import strip_echo_section
    analysis = record.get("analysis") or {}
    digest: dict = {}
    for key in ("issue_type", "issue_domain", "issue_subcategory", "wifi_or_bt",
                "driver_version", "latest_driver", "driver_check", "last_good_version",
                "action_owner", "next_action"):
        if analysis.get(key):
            digest[key] = str(analysis[key])[:300]
    if analysis.get("clean_description"):
        digest["issue_as_understood"] = str(analysis["clean_description"])[:700]
    if analysis.get("issue_times"):
        digest["issue_times"] = [str(t) for t in analysis["issue_times"][:3]]
    missing = [str(m.get("item") if isinstance(m, dict) else m)
               for m in (analysis.get("missing_info") or [])]
    if missing:
        digest["missing_info"] = missing[:10]
    findings = []
    for inc in (analysis.get("incidents") or [])[:2]:
        report = (inc or {}).get("report") or {}
        text = report.get("root_cause_summary") or (inc or {}).get("report_text") or ""
        if text:
            findings.append(str(text)[:600])
    if findings:
        digest["analysis_findings"] = findings
    advice = analysis.get("echo_advice") or {}
    digest["echo_advice_available"] = bool(advice.get("answer"))
    digest["draft_reply_text"] = _plain(strip_echo_section(str(record.get("draft_plain") or "")), 1800)
    return digest


# ---- start_case_analysis / analysis_status ---------------------------------------

def start_analysis(case_nbr: str, requested_by: str = "") -> dict:
    """Have Beacon analyse one case. On the server PC the run starts here; on
    a client it is queued on the share for the server. Never posts."""
    case_nbr = _normalise(case_nbr)
    if not case_nbr:
        return {"error": "not an IPS case number"}
    clear_cache()
    if _can_run_here():
        res = _start_local_run(case_nbr) or {}
        if not res.get("ok"):
            return {"error": str(res.get("error") or "the run could not be started")}
        return {"started": True, "case": case_nbr, "where": "this PC (Beacon server)",
                "message": f"Analysis of {case_nbr} started on this PC. The draft will appear in "
                           "the Beacon queue for review; nothing is posted."}
    import socket
    who = requested_by or f"{os.environ.get('USERNAME', '?')}@{socket.gethostname()}"
    req = _store().request_case(case_nbr, requested_by=f"{who} (assistant)") or {}
    if req.get("status") == "running":
        return {"started": True, "case": case_nbr, "where": "Beacon server",
                "message": f"The Beacon server is already analysing {case_nbr}."}
    return {"started": True, "case": case_nbr, "where": "queued for the Beacon server",
            "message": f"Analysis of {case_nbr} is queued for the Beacon server. The draft will "
                       "appear in the Beacon queue for review; nothing is posted."}


actions.register(START_KIND, lambda params: start_analysis(params.get("case_nbr"),
                                                           params.get("requested_by", "")))


def _start_case_analysis(args: dict, ctx: Ctx) -> dict:
    case_nbr = _normalise(args.get("case_nbr"))
    if not case_nbr:
        return {"error": f"{args.get('case_nbr')!r} is not an IPS case number "
                         "(six to eight digits, e.g. 01009610)"}
    if named_in(ctx.user_text, case_nbr):
        return start_analysis(case_nbr, ctx.owner_name)
    if ctx.headless:
        return {"error": "refused: a scheduled task may start an analysis only for a case "
                         "number that is written in the task's own instruction"}
    # The number came from somewhere other than the user's message (a list
    # the assistant read, a mail, a comment): the user confirms with a click.
    action = actions.propose(
        START_KIND, f"Have Beacon analyse case {case_nbr} (a draft for review; nothing is posted)",
        {"case_nbr": case_nbr, "requested_by": ctx.owner_name}, confirm_label="Start analysis")
    ctx.pending_actions.append(action)
    return {"needs_confirmation": True, "case": case_nbr,
            "message": "Not started. The engineer did not name this case number in their own "
                       "message, so a 'Start analysis' button is shown under your answer. Tell "
                       "them to click it; do not say the analysis has started."}


def _analysis_status(args: dict, ctx: Ctx) -> dict:
    case_nbr = _normalise(args.get("case_nbr")) if args.get("case_nbr") else ""
    if args.get("case_nbr") and not case_nbr:
        return {"error": f"{args.get('case_nbr')!r} is not an IPS case number"}
    now = ctx.now()
    out: dict = {}
    try:
        state = _local_run_state() or {}
        if state.get("status") in ("running", "done", "error") and (
                not case_nbr or state.get("case_nbr") == case_nbr or state.get("mode") != "single-case"):
            events = [f"{e.get('ts') or ''} {e.get('message') or ''}".strip()
                      for e in (state.get("events") or [])[-4:] if isinstance(e, dict)]
            out["this_pc"] = {"status": state.get("status"), "mode": state.get("mode"),
                              "case": state.get("case_nbr") or "",
                              "last_events": [e[:200] for e in events if e],
                              "error": str(state.get("error") or "")[:200]}
    except Exception:
        pass
    try:
        store = _store()
        reqs = store.list_requests()
        if case_nbr:
            reqs = [r for r in reqs if r.get("case_nbr") == case_nbr]
        out["requests"] = [{
            "case": r.get("case_nbr"), "status": r.get("status"),
            "requested": timeutil.label(r.get("requested_at"), now),
            "requested_by": r.get("requested_by") or "",
            "host": r.get("host") or "",
            "finished": timeutil.label(r.get("finished_at"), now),
            "ok": r.get("ok"), "error": str(r.get("error") or "")[:200],
            "draft_id": r.get("draft_id") or "",
        } for r in reqs[:15]]
        if case_nbr:
            slim = latest_drafts(store).get(case_nbr)
            out["beacon"] = beacon_view(slim, _requests_by_case(store).get(case_nbr), now)
    except Exception as e:
        out["warnings"] = ["The Beacon shared folder could not be read: "
                           f"{type(e).__name__}: {str(e)[:160]}"]
    if not out.get("requests") and not out.get("this_pc") and not out.get("beacon"):
        out["message"] = "No analysis is queued or running."
    out["summary"] = (f"status of {case_nbr}: " + str((out.get("beacon") or {}).get("text") or "")
                      if case_nbr else f"{len(out.get('requests') or [])} request(s)")
    return out


_CASE_NBR = {"type": "string", "description": "IPS case number, six to eight digits, e.g. 01036217."}


def connectors() -> list[Connector]:
    return [
        Connector(
            name="list_my_cases",
            description=(
                "List the engineer's own IPS cases that were updated since a point in time, each "
                "with why it counts as updated (customer comment, Intel comment, Beacon draft, "
                "closed) and where Beacon's handling stands (draft waiting for review, reply "
                "posted, analysing, not analysed ...). Cases the customer is waiting on come "
                "first. Use it for 'which of my cases were updated today', 'what is waiting for "
                "my review', morning overviews."),
            parameters=object_schema({
                "updated_since": {"type": "string",
                                  "description": "Start of the period, in the engineer's local "
                                                 "time: today (default), yesterday, 24h, 7d, "
                                                 "this week, or an ISO date / date-time."},
                "include_quiet": {"type": "boolean",
                                  "description": "Also list open cases with no change in the "
                                                 "period. Default false."},
            }),
            run=_list_my_cases, timeout_s=120, untrusted=True, result_chars=14000),
        Connector(
            name="get_case",
            description=(
                "Details of one IPS case: its IPS status and owner, the latest comments (who, "
                "when, text), and Beacon's latest draft for it - the issue as understood, "
                "missing information, driver check, analysis findings, the next action and the "
                "draft reply text. Use it before answering anything about a specific case."),
            parameters=object_schema({
                "case_nbr": _CASE_NBR,
                "comments": {"type": "integer",
                             "description": "How many of the latest comments to include (default 3, max 8)."},
            }, ["case_nbr"]),
            run=_get_case, timeout_s=120, untrusted=True, result_chars=9000),
        Connector(
            name="start_case_analysis",
            description=(
                "Ask Beacon to analyse one IPS case now: it reads the case, downloads and "
                "analyses the logs and writes a DRAFT reply into the Beacon review queue. "
                "Nothing is posted to IPS. Takes several minutes; check with analysis_status. "
                "Only call it when the engineer asked for an analysis."),
            parameters=object_schema({"case_nbr": _CASE_NBR}, ["case_nbr"]),
            run=_start_case_analysis, kind="act", timeout_s=60),
        Connector(
            name="analysis_status",
            description=(
                "Whether a Beacon analysis is queued, running or finished: for one case when "
                "case_nbr is given, otherwise every request of the last week."),
            parameters=object_schema({"case_nbr": _CASE_NBR}),
            run=_analysis_status, timeout_s=60),
    ]
