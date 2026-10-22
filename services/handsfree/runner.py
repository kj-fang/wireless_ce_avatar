"""
Headless per-case analysis pipeline for Beacon (the handsfree replyer).

Replays the full avatar user journey without the web UI:

    fetch case (Snowflake/Salesforce) → description triage → pick newest ZIP
    → download → decompose → pick ETL by issue time → decode ETL→.log
    → issue-time organization → agentic root-cause analysis (one run per
      detected incident time, like the UI's multi-time chaining)

Design notes:
  * Every stage records a StageStatus so the review UI can show exactly how
    far a case got and why it stopped.
  * No usable log ⇒ mode="triage_only": the description-level triage
    (analyze_desc) becomes the comment draft (missing-info questions,
    "request logs" recommendations).
  * The ETL decoder (wpp_ddd_parser_run) calls sys.exit() on some failure
    paths and opens TextAnalysisTool/Explorer windows on success — we catch
    SystemExit and suppress the GUI via the AVATAR_HEADLESS_DECODE env flag
    honored in parse_single_binary.
  * The agent run mirrors services/ace/eval/replay.py: fresh
    WifiLogAgentSystem, current_log_path BEFORE prime_with_context,
    chat(use_tools=True) → {"type": "report", "data": {...}}.
"""

from __future__ import annotations

import json
import re
import os
import threading
from pathlib import Path
import time
import traceback
from dataclasses import dataclass, field, asdict
from typing import Callable, Optional

from . import bsod as _bsod

# Cap agent runs per case: the UI chains one run per detected incident time;
# headless we keep the two most relevant to bound cost.
MAX_INCIDENTS = 2
MAX_AGENT_STEPS = 6
# Cap captured trace rows per incident (for the review UI).
_STEP_CONTENT_CAP = 300
_MAX_STEPS_KEPT = 60
# Every archive type attachment_decompose.extract_archive can open.
_ARCHIVE_EXTS = (".zip", ".rar", ".7z")


def _pick_latest_archive(attachment_list):
    """Newest log archive by upload-timestamp metadata (generalizes
    etl_utils.pick_latest_zip_attachment, which is .zip-only, to every
    archive type the decomposer supports). Falls back to the first archive
    when no timestamp parses."""
    items = [it for it in (attachment_list or [])
             if it and str(it[0]).lower().endswith(_ARCHIVE_EXTS)]
    if not items:
        return None

    def _ts(item):
        try:
            meta = item[2] if len(item) > 2 else None
            raw = str(meta[0]) if meta else ""
        except Exception:
            raw = ""
        from datetime import datetime
        for fmt in ("%m/%d/%Y %H:%M", "%m/%d/%Y %I:%M %p", "%m/%d/%Y",
                    "%Y-%m-%d %H:%M", "%d-%m-%Y %H:%M"):
            try:
                return datetime.strptime(raw.strip(), fmt)
            except Exception:
                continue
        return None

    dated = [(it, t) for it in items if (t := _ts(it)) is not None]
    if dated:
        dated.sort(key=lambda p: p[1], reverse=True)
        return dated[0][0]
    return items[0]


# YB / assert issue detection for the ETL-folder preference below.
_YB_ASSERT_ISSUE_RE = re.compile(
    r"(?i)yellow[ _-]?bang|\bYB\b|assert|device (?:lost|drop)|\bCode 10\b")
_HEX_TOKEN_RE = re.compile(r"0x[0-9A-Fa-f]+")
_ETL_FILE_RE = re.compile(r"\.etl(\.\d+)?$", re.IGNORECASE)


def _yb_capture_folder(path: str):
    """Return (folder_component, timestamp) for the ancestor capture folder
    whose name carries at least one NONZERO 0x error token (autologger names
    captures like HOST_20-08-2026_10-46-44_524_12_4222_0xa_0x0_0x1fffff when
    a failure fired; all-zero tokens mean a clean capture). None when the
    path has no such ancestor."""
    from utils.etl_utils import extract_timestamp_from_folder
    for comp in str(path).replace("\\", "/").split("/"):
        toks = _HEX_TOKEN_RE.findall(comp)
        if not toks:
            continue
        try:
            if any(int(t, 16) != 0 for t in toks):
                return comp, extract_timestamp_from_folder(comp)
        except ValueError:
            continue
    return None


def _pick_etl_yb_earliest(wifi_files: list, ddd_files: list):
    """For YB/assert issues: among captures whose folder name carries a
    nonzero error code, pick the EARLIEST folder (by name timestamp) and
    return the best ETL inside it (non-History preferred, highest segment
    number). None when no capture qualifies — callers fall back to the
    normal AI-time / newest heuristics."""
    groups: dict = {}
    for p in list(ddd_files or []) + list(wifi_files or []):
        p = str(p)
        if not _ETL_FILE_RE.search(p):
            continue
        hit = _yb_capture_folder(p)
        if hit is None:
            continue
        comp, ts = hit
        groups.setdefault(comp, {"ts": ts, "paths": []})["paths"].append(p)
    if not groups:
        return None
    # Earliest first; unparseable timestamps sort last.
    comp = min(groups, key=lambda c: (groups[c]["ts"] is None,
                                      groups[c]["ts"] or 0, c))
    paths = groups[comp]["paths"]
    non_history = [p for p in paths if "history" not in p.lower()]
    return sorted(non_history or paths)[-1]


_ENV_REPRO_KEY_RE = re.compile(r"(?i)steps to reproduce|repro")
_ENV_BRIEF_KEY_RE = re.compile(
    r"(?i)platform|found in build|operating system|frequency|tested hardware|computer model")


def _classify_wireless_domain(subcategory: str) -> str:
    """Return wifi/bt only when the IPS subcategory explicitly identifies it."""
    value = str(subcategory or "").strip().lower()
    if "wifi" in value or "wi-fi" in value:
        return "wifi"
    if "bluetooth" in value or re.search(r"\bbt\b", value):
        return "bt"
    return ""


class _Prefetch(threading.Thread):
    """Background download of one attachment (same run_dload_threads call
    the download stage uses). Results in .items, failure in .error."""

    def __init__(self, zip_item, download_dir: str):
        super().__init__(daemon=True, name="handsfree-prefetch")
        self.zip_item = zip_item
        self.attachment_name = str(zip_item[0])
        self.download_dir = download_dir
        self.items: list = []
        self.error = None

    def run(self) -> None:
        try:
            from utils.attachment_download import run_dload_threads
            for item in run_dload_threads([self.zip_item], self.download_dir,
                                          socketio=None):
                self.items.append(item)
        except Exception as e:   # reported by the download stage
            self.error = e


_FOLDER_STAMP_RE = re.compile(r"(\d{2})-(\d{2})-(\d{4})_(\d{2})-(\d{2})-(\d{2})")


def _folder_timestamp(etl_path: str):
    """Capture time from the WRT folder name of an ETL
    (..._27-05-2026_17-51-23_656_...) -> datetime, or None."""
    from datetime import datetime
    m = _FOLDER_STAMP_RE.search(os.path.basename(os.path.dirname(str(etl_path))))
    if not m:
        return None
    d, mo, y, h, mi, s = (int(x) for x in m.groups())
    try:
        return datetime(y, mo, d, h, mi, s)
    except ValueError:
        return None


def _fmt_offset(delta) -> str:
    """A clock offset as '+11:00' / '-03:30' (rounded to the minute)."""
    minutes = int(round(delta.total_seconds() / 60.0))
    sign = "-" if minutes < 0 else "+"
    minutes = abs(minutes)
    return f"{sign}{minutes // 60:02d}:{minutes % 60:02d}"


# How the customer's issue time was placed in the decoded log's clock.
ANCHOR_AS_IS = "as_is"            # same clock (or nothing known): value taken as given
ANCHOR_TZ = "tz_helper"           # system_info.txt time zone vs the decode host (GMT+8)
ANCHOR_FOLDER = "folder_stamp"    # WRT folder stamp (DUT clock) vs the log's last line
ANCHOR_LABELS = {ANCHOR_AS_IS: "same clock", ANCHOR_TZ: "time zone",
                 ANCHOR_FOLDER: "capture-folder stamp"}



_DOMAIN_SOURCE_LABELS = {"reader": "reader of the IPS thread",
                         "ips_subcategory": "IPS subcategory",
                         "triage": "triage of the description",
                         "fallback": "fallback"}


def _explain_domain(reader_domain: str, subcategory: str, issue_type: str) -> dict:
    """_resolve_issue_domain, with its working shown: every candidate in
    priority order, its resolved domain, and which one won."""
    from .checklist import FALLBACK_DOMAIN, resolve_domain
    cands = []
    for source, raw in (("reader", reader_domain), ("ips_subcategory", subcategory),
                        ("triage", issue_type)):
        raw = str(raw or "").strip()
        resolved = resolve_domain(raw) if raw else ""
        cands.append({"source": source, "label": _DOMAIN_SOURCE_LABELS[source],
                      "raw": raw, "resolved": resolved,
                      "specific": bool(resolved and resolved != FALLBACK_DOMAIN)})
    winner = next((c for c in cands if c["specific"]), None)
    return {"candidates": cands,
            "winner": winner["resolved"] if winner else FALLBACK_DOMAIN,
            "winner_source": winner["source"] if winner else "fallback",
            "winner_label": _DOMAIN_SOURCE_LABELS[winner["source"] if winner else "fallback"]}


def _required_log_items(domain: str, subcat: str = "") -> list:
    """The domain's required-log items (names) from the debug checklist,
    narrowed to the sub-category when one is known."""
    try:
        from .checklist import load_checklist, _entry_in_subcat
        dom = (load_checklist().get("domains") or {}).get(domain) or {}
        subs = list((dom.get("subcategories") or {}).keys())
        sub = subcat if subcat in subs else ""
        return [str(e.get("item") or "") for e in dom.get("required_log", [])
                if (sub and _entry_in_subcat(e, sub)) or (not sub)][:20]
    except Exception:
        return []


def _resolve_issue_domain(reader_domain: str, subcategory: str,
                          issue_type: str) -> str:
    """Debug-checklist domain with graceful fallbacks: the reader's informed
    pick wins, but a fallback 'Others' never shadows a specific domain from
    the IPS subcategory (customer-selected, e.g. 'OEM Tools') or the triage
    issue type."""
    from .checklist import FALLBACK_DOMAIN, resolve_domain
    for cand in (reader_domain,
                 resolve_domain(subcategory) if subcategory else "",
                 resolve_domain(issue_type) if issue_type else ""):
        if cand and cand != FALLBACK_DOMAIN:
            return cand
    return FALLBACK_DOMAIN


def _env_repro_steps(env: dict) -> str:
    """Non-trivial 'Steps to reproduce' answer from the Environment Details
    form, or '' — a filled form field means repro steps are NOT missing."""
    for q, a in (env or {}).items():
        if not _ENV_REPRO_KEY_RE.search(str(q)):
            continue
        ans = str(a or "").strip()
        if not ans or ans.lower() in {"na", "n/a", "none"}:
            continue
        return ans
    return ""


def _env_brief(env: dict, limit: int = 300) -> str:
    """Compact one-line environment summary for the agent context."""
    parts = []
    for q, a in (env or {}).items():
        q = " ".join(str(q or "").split())
        ans = " ".join(str(a or "").split())
        if not ans or ans.lower() in {"na", "n/a", "none"}:
            continue
        if _ENV_BRIEF_KEY_RE.search(q):
            parts.append(f"{q}: {ans}")
    return "; ".join(parts)[:limit]


@dataclass
class StageStatus:
    name: str
    ok: bool
    detail: str = ""
    duration_ms: int = 0


@dataclass
class IncidentReport:
    issue_time: str = ""             # as reported (the customer's clock)
    issue_time_customer: str = ""    # same, as a full datetime in the customer's clock
    issue_time_log: str = ""         # the same moment in the decoded log's clock
    time_anchor: str = ""            # how the two were aligned: tz_helper |
                                     #   folder_stamp | as_is
    user_message: str = ""
    sent_for_analysis: dict = field(default_factory=dict)  # exact agent inputs (live
                                     #   panel + review): description, both clocks, log
    result_type: str = ""            # report | partial_report | text | error
    report: Optional[dict] = None    # submit_final_report args
    report_text: str = ""
    steps: list[dict] = field(default_factory=list)
    error: str = ""

    @property
    def confidence(self) -> Optional[int]:
        if isinstance(self.report, dict):
            v = self.report.get("confidence_score")
            return int(v) if isinstance(v, (int, float)) else None
        return None


@dataclass
class CaseAnalysis:
    case_nbr: str
    case_id: str = ""                # Salesforce 18-char id (for posting)
    source: str = "ips"              # ips | email | ips+email — where the case thread came from
    ok: bool = False
    mode: str = ""                   # full | triage_only | unsupported | error |
                                     # request_logs | request_info | first_response |
                                     # waiting_customer
    subject: str = ""
    description: str = ""
    clean_description: str = ""
    issue_type: str = ""
    wifi_or_bt: str = ""
    bt_case_valid: Optional[bool] = None  # True when archive contains BT ETLs
    triage: dict = field(default_factory=dict)       # analyze_desc output
    classification: dict = field(default_factory=dict)
    case_reader: dict = field(default_factory=dict)  # comment-aware reader output
    attachment_time: str = ""
    chosen_attachment: str = ""      # filename the reader picked (or fallback)
    issue_times: list[str] = field(default_factory=list)
    etl_path: str = ""
    log_path: str = ""
    incidents: list[IncidentReport] = field(default_factory=list)
    stages: list[StageStatus] = field(default_factory=list)
    echo_insights: list = field(default_factory=list)  # Echo KB root-cause answers
    time_mismatch: dict = field(default_factory=dict)  # log doesn't cover issue time
    time_alignment: dict = field(default_factory=dict) # how the customer's issue time was
                                                       #   placed in the log's clock:
                                                       #   customer_time, log_time,
                                                       #   customer_tz, anchor, offset
    missing_info: list = field(default_factory=list)   # [{item, reason}] case-info gaps
    env_detail: dict = field(default_factory=dict)     # IPS Environment Details Q&A form
    issue_domain: str = ""                             # debug-checklist domain (tab name)
    issue_subcategory: str = ""                        # checklist sub-category (e.g. Roaming)
    log_request_reason: str = ""                       # request_logs wording: no_archive |
                                                       #   no_wrt_inside | unreadable_archive
    subcategory: str = ""                              # IPS Core issue subcategory (customer-selected)
    checklist_fills: dict = field(default_factory=dict)  # pre-filled first-response items
    customer_history: str = ""                         # customer-authored comments only
                                                       #   (safe source for public fills)
    action_owner: str = ""                             # who acts next: intel | customer | ""
    next_action: str = ""                              # reader's one-sentence next step
    yb_case: bool = False                              # yellow bang / assert issue:
                                                       #   any assert in the log counts
    archive_contents: dict = field(default_factory=dict)  # decompose counts: wifi_wpp,
                                                       #   ddd, event_logs, bt, fw
    comment_images: list = field(default_factory=list)  # [{comment_id, date, author,
                                                       #   count, descriptions}]
    first_response_done: bool = False                  # overview checklist already sent on
                                                       #   this case (later rounds skip it)
    bsod_case: bool = False                            # BSOD issue: the crash dump replaces
                                                       #   the issue time (no WRT pipeline)
    bsod: dict = field(default_factory=dict)           # dumps, dumps_path, email, submitted,
                                                       #   status/detail, skipped, previous
    # How the run decided (live panel): the issue category, the issue time,
    # the archive + capture — candidates and the rule that won, not just the
    # winners. checklist_decision is filled after the run by checklist.build_fills.
    domain_decision: dict = field(default_factory=dict)
    time_decision: dict = field(default_factory=dict)
    pick_decision: dict = field(default_factory=dict)
    checklist_decision: dict = field(default_factory=dict)
    # Echo knowledge-base advice asked right after the thread is read (what
    # to request from the customer next / next debug steps); answered in the
    # background while the log pipeline runs. {status, question, answer, error, ...}
    echo_advice: dict = field(default_factory=dict)
    error: str = ""

    @property
    def best_incident(self) -> Optional[IncidentReport]:
        with_reports = [i for i in self.incidents if i.report]
        if not with_reports:
            return None
        return max(with_reports, key=lambda i: i.confidence or 0)

    def to_dict(self) -> dict:
        return json.loads(json.dumps(asdict(self), default=str))


def _noop(_stage: str, _detail: str = "") -> None:
    pass


def _describe_attachments(attachment_list, limit: int = 25) -> list:
    """The case's uploads as the live panel lists them (name, upload stamp,
    subtitle, archive?)."""
    out = []
    for it in (attachment_list or [])[:limit]:
        try:
            meta = it[2] if len(it) > 2 and it[2] else ["", ""]
            name = str(it[0])
            out.append({"name": name,
                        "uploaded": str(meta[0] or "")[:40] if len(meta) > 0 else "",
                        "subtitle": str(meta[1] or "")[:120] if len(meta) > 1 else "",
                        "is_archive": name.lower().endswith(_ARCHIVE_EXTS)})
        except Exception:
            continue
    return out


def _match_kind(found, wanted: str) -> str:
    """How case_reader.find_attachment matched the reader's nomination."""
    if found is None:
        return "not_found"
    name = str(found[0])
    if name == wanted:
        return "exact"
    if name.lower() == wanted.lower():
        return "case_insensitive"
    return "substring"


def _describe_captures(paths: list, issue_time_raw: str = "", limit: int = 30) -> list:
    """One row per capture folder among the extracted ETLs: folder name, its
    stamp (DUT clock), the YB error token when the name carries one, and the
    signed distance to the reported issue time."""
    from utils.issue_time_utils import parse_issue_time_string
    issue_dt = None
    if issue_time_raw:
        dt, time_only = parse_issue_time_string(str(issue_time_raw))
        if dt is not None and not time_only:
            issue_dt = dt
    rows, seen = [], set()
    for p in paths or []:
        p = str(p)
        folder = os.path.basename(os.path.dirname(p))
        if not folder or folder in seen:
            continue
        seen.add(folder)
        stamp = _folder_timestamp(p)
        yb = ""
        toks = _HEX_TOKEN_RE.findall(folder)
        try:
            yb = next((t for t in toks if int(t, 16) != 0), "")
        except ValueError:
            yb = ""
        delta = None
        if stamp is not None and issue_dt is not None:
            delta = int((stamp - issue_dt).total_seconds())
        rows.append({"folder": folder,
                     "stamp": stamp.strftime("%m/%d/%Y-%H:%M:%S") if stamp else "",
                     "yb_code": yb, "delta_s": delta, "role": "", "decode": "",
                     "covered": None, "kind": "bt" if re.search(r"(?i)ibt(pci|usb)", p) else
                                             ("ddd" if re.search(r"(?i)ddd", p) else "wifi")})
        if len(rows) >= limit:
            break
    return rows


def _set_capture(analysis: "CaseAnalysis", etl_path: str, **fields) -> None:
    folder = os.path.basename(os.path.dirname(str(etl_path or "")))
    for c in (analysis.pick_decision or {}).get("captures") or []:
        if c.get("folder") == folder:
            c.update({k: v for k, v in fields.items() if v is not None})
            return


def _fmt_dt(dt) -> str:
    if dt is None:
        return ""
    try:
        from utils.issue_time_utils import format_issue_time
        return format_issue_time(dt, with_ms=False)
    except Exception:
        return str(dt)


def _sent_for_analysis(inc, agent, description: str, customer_tz: str,
                       log_path: str, log_first, log_last) -> dict:
    """What the analysis agent actually receives for one incident."""
    log_time = inc.issue_time_log or _fmt_dt(getattr(agent, "issue_time", None))
    return {
        "issue_time_reported": inc.issue_time,
        "issue_time_customer": inc.issue_time_customer or inc.issue_time,
        "issue_time_log": log_time,
        "time_anchor": inc.time_anchor,
        "customer_tz": customer_tz or "",
        "description": description,
        "user_message": inc.user_message,
        "log_file": os.path.basename(log_path or ""),
        "log_range": [_fmt_dt(log_first), _fmt_dt(log_last)],
    }


_STEP_KINDS = (("🧠 Thinking", "thinking"), ("🧠", "playbook"), ("💭", "step"),
               ("🔍 Fetching", "fetch"), ("🔍 Invoking", "tool"), ("🔍 Pre-Analysis", "prescan"),
               ("🔍", "tool"), ("🧭", "plan"), ("📄", "result"), ("✅", "conclusion"),
               ("⚠️", "warning"), ("⏹", "stopped"), ("🕒", "time"))


def _compact_step(step: dict, limit: int = 300) -> dict:
    """One agent step as the live panel shows it: a kind (icon), short text."""
    role = str(step.get("role") or "")
    # The agent writes markdown ("🧠 **Thinking:**", "🔍 **Fetching filtered
    # logs** for `X`"): drop the bold markers so the kind markers below match.
    text = str(step.get("content") or "").replace("**", "").strip()
    kind = {"tool": "result", "error": "error", "token_usage": "usage"}.get(role, "agent")
    for marker, name in _STEP_KINDS:
        if text.startswith(marker):
            kind = name
            text = text[len(marker):].lstrip(" :\n")
            break
    text = " ".join(text.split())
    if len(text) > limit:
        text = text[:limit] + " …"
    return {"ts": time.strftime("%H:%M:%S"), "role": role, "kind": kind, "text": text}


def _result_summary(inc: "IncidentReport") -> dict:
    rep = inc.report if isinstance(inc.report, dict) else {}
    return {
        "issue_time": inc.issue_time,
        "issue_time_log": inc.issue_time_log,
        "result_type": inc.result_type,
        "confidence": inc.confidence,
        "root_cause_summary": str(rep.get("root_cause_summary") or "")[:600],
        "recommended_actions": [str(x) for x in (rep.get("recommended_actions") or [])][:5],
        "involved_skills": [str(x) for x in (rep.get("involved_skills") or [])][:6],
        "markdown_summary": str(rep.get("markdown_summary") or "")[:1500],
        "text": "" if rep else str(inc.report_text or "")[:600],
        "error": inc.error,
        "steps": len(inc.steps or []),
        # The analysis trace: which skills / steps the agent went through, in
        # order (the live panel shows it instead of the priority action list).
        "trace": [_compact_step(st) for st in (inc.steps or [])][-40:],
    }


def _compact_insight(ins: dict) -> dict:
    return {"kind": str(ins.get("kind") or ""), "code": ins.get("code"),
            "question": str(ins.get("question") or "")[:400],
            "answer": str(ins.get("answer") or "")[:900],
            "error": str(ins.get("error") or "")[:300]}


def live_fields(a: "CaseAnalysis") -> dict:
    """Compact, JSON-safe view of what is known about a case so far (the
    page's live panel renders it while the run is in flight)."""
    reader = a.case_reader or {}
    bsod = a.bsod or {}
    return {
        "case_id": a.case_id,
        "source": a.source,
        "subject": a.subject,
        "subcategory": a.subcategory,
        "wifi_or_bt": a.wifi_or_bt,
        "issue_type": a.issue_type,
        "issue_domain": a.issue_domain,
        "issue_subcategory": a.issue_subcategory,
        "clean_description": (a.clean_description or "")[:2000],
        "issue_times": list(a.issue_times or []),
        "vague_times": list(reader.get("vague_times") or []),
        "issue_time_source": str(reader.get("issue_time_source") or ""),
        "reader_attachment": str(reader.get("attachment_name") or ""),
        "reader_attachment_reason": str(reader.get("attachment_reason") or ""),
        "action_owner": a.action_owner,
        "next_action": a.next_action,
        "missing_info": [m.get("item") for m in (a.missing_info or []) if isinstance(m, dict)],
        "chosen_attachment": a.chosen_attachment,
        "attachment_time": a.attachment_time,
        "archive_contents": dict(a.archive_contents or {}),
        "etl_capture": os.path.basename(os.path.dirname(a.etl_path)) if a.etl_path else "",
        "etl_file": os.path.basename(a.etl_path or ""),
        "log_file": os.path.basename(a.log_path or ""),
        "log_path": a.log_path or "",
        "comment_images": [{"comment_id": i.get("comment_id"), "date": str(i.get("date") or "")[:16],
                            "author": i.get("author"), "count": i.get("count"),
                            "descriptions": [str(x)[:900] for x in (i.get("descriptions") or [])]}
                           for i in (a.comment_images or []) if isinstance(i, dict)][:10],
        "domain_decision": dict(a.domain_decision or {}),
        "time_decision": dict(a.time_decision or {}),
        "pick_decision": dict(a.pick_decision or {}),
        "checklist_decision": dict(a.checklist_decision or {}),
        "echo_advice": {k: (v if k != "answer" else str(v)[:6000])
                        for k, v in (a.echo_advice or {}).items()},
        "incidents": [_result_summary(i) for i in (a.incidents or [])],
        "echo_insights": [_compact_insight(i) for i in (a.echo_insights or [])
                          if isinstance(i, dict)],
        "time_mismatch": {k: v for k, v in (a.time_mismatch or {}).items()
                          if k in ("issue_times", "log_first", "log_last",
                                   "log_first_customer", "log_last_customer",
                                   "also_tried", "yb_assert_override")},
        "time_alignment": dict(getattr(a, "time_alignment", None) or {}),
        "yb_case": a.yb_case,
        "bsod_case": a.bsod_case,
        "bsod": {
            "dumps": list(bsod.get("dumps") or []),
            "dumps_path": bsod.get("dumps_path") or "",
            "submitted": bool(bsod.get("submitted")),
            "skipped": bsod.get("skipped") or "",
            "failed": list(bsod.get("failed") or []),
            "results": [{"dump": r.get("dump"), "bugcheck_code": r.get("bugcheck_code"),
                         "bugcheck_str": r.get("bugcheck_str"),
                         "failure_bucket_id": r.get("failure_bucket_id"),
                         "wifi_fault": r.get("wifi_fault"), "bt_fault": r.get("bt_fault")}
                        for r in (bsod.get("results") or [])],
        } if (a.bsod_case or bsod) else {},
        "stages": [{"name": st.name, "ok": st.ok, "duration_ms": st.duration_ms,
                    "detail": (st.detail or "")[:200]} for st in (a.stages or [])],
        "mode": a.mode,
        "log_request_reason": a.log_request_reason,
        "error": (a.error or "")[:400],
    }


class HandsfreeRunner:
    """One instance per run; stateless between cases."""

    # How long the end of a run waits for a background prefetch nobody
    # consumed (the reader chose another archive, or the run stopped at an
    # early gate) before giving up on it.
    PREFETCH_JOIN_TIMEOUT_S = 15 * 60
    # Echo next-step advice: asked in a background thread after the reader;
    # the run waits at most this long for it before the draft is composed.
    ECHO_ADVICE_ENABLED = True
    ECHO_ADVICE_TIMEOUT_S = 90
    ECHO_ADVICE_JOIN_S = 75      # Echo answers in ~70 s; a down Echo must not hold every run hostage
    # Minutes of slack on either side of the decoded log when deciding whether
    # a reported issue time is covered / can be placed in the log's clock.
    TIME_GRACE_MIN = 10

    def __init__(self, progress_cb: Optional[Callable[[str, str], None]] = None,
                 snapshot_cb: Optional[Callable[[dict], None]] = None):
        self.progress = progress_cb or _noop
        # snapshot_cb(fields): structured facts about the case as they become
        # known (live panel on the page). Optional; never allowed to break a run.
        self.snapshot_cb = snapshot_cb
        self._prefetch: Optional[_Prefetch] = None
        self._echo_thread: Optional[threading.Thread] = None

    def _snap(self, **fields) -> None:
        if self.snapshot_cb is None:
            return
        try:
            self.snapshot_cb(fields)
        except Exception as e:
            print(f"[handsfree.runner] snapshot failed (ignored): {e}")

    def _snap_analysis(self, analysis: "CaseAnalysis", **extra) -> None:
        if self.snapshot_cb is None:
            return
        try:
            fields = live_fields(analysis)
        except Exception as e:
            print(f"[handsfree.runner] live fields failed (ignored): {e}")
            fields = {}
        fields.update(extra)
        self._snap(**fields)

    # ---- stage helper ----
    def _stage(self, analysis: CaseAnalysis, name: str):
        runner = self

        class _Ctx:
            def __enter__(ctx):
                ctx.t0 = time.time()
                runner.progress(name, "started")
                runner._snap(current_stage=name)
                return ctx

            def __exit__(ctx, exc_type, exc, tb):
                dur = int((time.time() - ctx.t0) * 1000)
                if exc is None:
                    analysis.stages.append(StageStatus(name, True, duration_ms=dur))
                    runner.progress(name, "done")
                else:
                    detail = f"{type(exc).__name__}: {exc}"
                    analysis.stages.append(StageStatus(name, False, detail, dur))
                    runner.progress(name, f"failed: {detail}")
                runner._snap_analysis(analysis, current_stage="")
                # Never propagate — callers check the returned ok flags.
                return True

        return _Ctx()

    # ------------------------------------------------------------------
    def analyze_case(self, case_nbr: str, *, max_steps: int = MAX_AGENT_STEPS,
                     max_incidents: int = MAX_INCIDENTS, case_ctx=None) -> CaseAnalysis:
        """Run the pipeline for one case. Whatever path the run takes, a
        background archive prefetch it started is waited for before this
        returns: the caller releases the run lock right after, and an orphan
        download would overlap the next run's use of the shared
        DriverManager / download globals."""
        self._prefetch = None
        self._echo_thread = None
        try:
            analysis = self._analyze_case(case_nbr, max_steps=max_steps,
                                          max_incidents=max_incidents, case_ctx=case_ctx)
            self._join_echo_advice(analysis)
            best = analysis.best_incident
            self._snap_analysis(analysis, current_stage="", finished=True,
                                confidence=best.confidence if best else None)
            return analysis
        finally:
            pf, self._prefetch = self._prefetch, None
            if pf is not None and pf.is_alive():
                self.progress("download",
                              f"waiting for the background prefetch of {pf.attachment_name} "
                              "to finish before the run ends")
                pf.join(timeout=self.PREFETCH_JOIN_TIMEOUT_S)
                if pf.is_alive():
                    print(f"[handsfree.runner] prefetch of {pf.attachment_name} still "
                          "running after the join timeout — left to finish on its own")

    def _analyze_case(self, case_nbr: str, *, max_steps: int,
                      max_incidents: int, case_ctx=None) -> CaseAnalysis:
        from configs.global_configs import app_config

        analysis = CaseAnalysis(case_nbr=str(case_nbr))
        given_ctx, case_ctx = case_ctx, None

        # -- 1. fetch case info ------------------------------------------------
        # A prebuilt context replaces the IPS fetch: the email thread of the
        # case (stage 1 of the Outlook intake), later the IPS+email merge.
        # Every stage after this one runs on the context unchanged.
        with self._stage(analysis, "fetch_case"):
            if given_ctx is not None:
                case_ctx = given_ctx
                analysis.source = str(getattr(case_ctx, "source", "") or "ips")
                msgs = getattr(case_ctx, "messages", None) or []
                if msgs:
                    self.progress("fetch_case",
                                  f"email thread of case {case_nbr}: {len(msgs)} mail(s) "
                                  f"({msgs[0].received[:10]} → {msgs[-1].received[:10]}), "
                                  f"{len(case_ctx.attachment_list or [])} file attachment(s), "
                                  f"{len(getattr(case_ctx, 'image_files', []) or [])} image(s)")
            else:
                from models.models import CaseContext
                from services.case_info_service import CaseService
                case_ctx = CaseService.process_case(CaseContext(case_nbr=str(case_nbr)))
            analysis.case_id = case_ctx.id or ""
            analysis.subject = case_ctx.subject or ""
            analysis.description = case_ctx.description or ""
            analysis.subcategory = str(case_ctx.subcategory or "")
            # Explicit IPS subcategory wins; otherwise keep CaseService's
            # legacy derivation (non-"wifi" -> bt) so cases such as "OEM
            # Tools" still get the BT-gated triage + first-response checklist
            # validated on 2026-10-01 instead of being skipped as unsupported.
            analysis.wifi_or_bt = (_classify_wireless_domain(analysis.subcategory)
                                   or str(case_ctx.wifi_or_bt or ""))
            analysis.env_detail = dict(case_ctx.env_detail or {})
            from .case_reader import customer_visible_history
            analysis.customer_history = customer_visible_history(case_ctx.comments)
        if case_ctx is None or not (analysis.subject or analysis.description):
            analysis.mode = "error"
            analysis.error = "case fetch failed — no subject/description"
            return analysis
        if not analysis.wifi_or_bt:
            analysis.mode = "unsupported"
            analysis.error = (f"unsupported IPS subcategory: "
                              f"{analysis.subcategory or '(empty)'}")
            self.progress("fetch_case", analysis.error)
            return analysis

        # Start fetching the newest log archive now: triage + reader take
        # ~40 s of LLM time during which the download would otherwise wait.
        # The reader usually nominates the newest upload; when it picks
        # another file the normal download runs and this one is discarded.
        prefetch = None
        if _bsod.quick_hint(analysis.subject, analysis.subcategory, analysis.description):
            # A BSOD case's newest archive is a memory dump (GBs), not a WRT
            # capture — the BSOD branch downloads it to the BSOD share instead.
            self.progress("fetch_case", "BSOD wording in the case — WRT prefetch skipped")
        elif analysis.wifi_or_bt == "wifi" and not getattr(case_ctx, "local_paths", None):
            try:
                newest = _pick_latest_archive(case_ctx.attachment_list)
                if newest is not None:
                    prefetch = _Prefetch(newest, case_ctx.case_download_dir)
                    prefetch.start()
                    self._prefetch = prefetch     # joined by analyze_case on every exit
                    self.progress("fetch_case",
                                  f"prefetching {prefetch.attachment_name} in the background")
            except Exception as e:
                print(f"[handsfree.runner] prefetch not started: {e}")
                prefetch = None

        # -- 2. description triage (always runs; also the no-log fallback) ----
        with self._stage(analysis, "triage"):
            from services.case_info_service import CaseService
            llm = app_config.llm_helper
            prompt_path = CaseService.load_case_summary_prompt(analysis.wifi_or_bt)
            ctx_dict = case_ctx.to_dict()
            triage = llm.analyze_desc(prompt_path, ctx_dict)
            if isinstance(triage, dict):
                analysis.triage = triage
                analysis.classification = triage.get("Classification") or {}
                analysis.issue_type = analysis.classification.get("issue_type", "") or ""

        # Provisional checklist domain before any early exit (the reader can
        # upgrade it later): IPS subcategory is customer-selected and often
        # exact ("OEM Tools"); note wifi_or_bt is derived as "bt" whenever
        # the subcategory lacks the word "wifi", so BT-gated cases MUST get
        # their domain here or the first-response checklist falls to Others.
        analysis.issue_domain = _resolve_issue_domain(
            "", analysis.subcategory, analysis.issue_type)
        analysis.domain_decision = _explain_domain("", analysis.subcategory, analysis.issue_type)
        analysis.domain_decision["provisional"] = analysis.issue_domain

        # -- 2b. images embedded in comments (screenshots) ----------------------
        # Snowflake keeps the plain text only; the rich-text comments in IPS
        # still link the images. Download + describe them (vision) and add
        # the descriptions as comment rows, so the reader, the checklist
        # fill and the customer history see what the pictures say. Failure
        # or no images: the rows stay as they were.
        with self._stage(analysis, "read_images"):
            from .comment_images import annotate_case_comments
            from .composer import AI_MARKER
            from .ips_client import IpsClient
            cache = (Path(app_config.avatarfiles_dir) / "handsfree"
                     / "image_descriptions.json"
                     if getattr(app_config, "avatarfiles_dir", None) else None)
            if analysis.source == "email":
                # The thread's image attachments (screenshots sent by mail).
                from .email_case import annotate_email_images
                rows, info = annotate_email_images(
                    app_config.llm_helper, case_ctx, cache_path=cache,
                    progress=lambda m: self.progress("read_images", m))
            else:
                rows, info = annotate_case_comments(
                    IpsClient(), app_config.llm_helper, analysis.case_id,
                    case_ctx.comments, cache_path=cache, skip_marker=AI_MARKER,
                    progress=lambda m: self.progress("read_images", m))
            analysis.comment_images = info
            if info:
                case_ctx.comments = rows
                from .case_reader import customer_visible_history
                analysis.customer_history = customer_visible_history(rows)
                self.progress("read_images",
                              f"{sum(len(i['descriptions']) for i in info)} image(s) "
                              f"described across {len(info)} comment(s)")
            else:
                self.progress("read_images", "no images in the comments")

        # -- 3. read the case history (description + comments, chronological) --
        # The reader mirrors how an engineer works the case: description
        # first, then comments oldest→newest (later comments supersede),
        # extracting the issue time and nominating the attachment whose log
        # most likely covers it.
        with self._stage(analysis, "read_case_history"):
            from .case_reader import read_case_history
            llm = app_config.llm_helper
            reader = read_case_history(
                llm,
                subject=analysis.subject,
                description=analysis.description,
                comments=case_ctx.comments,
                attachment_list=case_ctx.attachment_list,
                env_detail=case_ctx.env_detail,
            )
            if reader:
                analysis.case_reader = reader
                if reader.get("clean_description"):
                    analysis.clean_description = reader["clean_description"]
                if reader.get("issue_times"):
                    analysis.issue_times = reader["issue_times"][:max_incidents]
                analysis.action_owner = reader.get("action_owner") or ""
                analysis.next_action = reader.get("next_action") or ""
                src = str(reader.get("issue_time_source") or "")
                analysis.time_decision = {
                    "found": [{"time": str(t), "source": src, "used": i < max_incidents}
                              for i, t in enumerate(reader.get("issue_times") or [])],
                    "ignored_vague": [str(t) for t in (reader.get("vague_times") or [])],
                    "rule": "only times stated to the minute count; a bare date, "
                            "'yesterday' or an upload time is not an issue time",
                    "reasoning": str(reader.get("reasoning") or "")[:600],
                    "gaps": [{"item": m.get("item"), "action": "reader",
                              "reason": m.get("reason", "")}
                             for m in (reader.get("missing_info") or [])],
                    "gate": "", "organizer_used": False, "placements": [], "coverage": {},
                }
                self.progress(
                    "read_case_history",
                    f"issue_times={reader.get('issue_times')} "
                    f"attachment={reader.get('attachment_name') or '(none)'} "
                    f"({reader.get('issue_time_source') or 'no source'})")
                if analysis.next_action:
                    self.progress(
                        "read_case_history",
                        f"next action [{analysis.action_owner or 'owner unknown'}]: "
                        f"{analysis.next_action}")

        # Upgrade the provisional domain with the reader's informed pick
        # (a reader "Others" never shadows a specific subcategory domain).
        analysis.issue_domain = _resolve_issue_domain(
            (analysis.case_reader or {}).get("issue_domain") or "",
            analysis.subcategory, analysis.issue_type)
        # Sub-category (Connectivity -> Connectivity/Scan/Roaming, P2P ->
        # Connectivity/Performance): reader's raw pick, normalized/fallback-
        # resolved inside checklist.build_fills at compose time.
        analysis.issue_subcategory = str(
            (analysis.case_reader or {}).get("issue_subcategory") or "")
        provisional = (analysis.domain_decision or {}).get("provisional", "")
        analysis.domain_decision = _explain_domain(
            (analysis.case_reader or {}).get("issue_domain") or "",
            analysis.subcategory, analysis.issue_type)
        analysis.domain_decision.update({
            "provisional": provisional,
            "subcategory_raw": analysis.issue_subcategory,
            "required_logs": _required_log_items(analysis.issue_domain,
                                                 analysis.issue_subcategory)})
        self.progress("read_case_history",
                      f"issue category: {analysis.issue_domain} "
                      f"(from the {analysis.domain_decision['winner_label']})"
                      + (f", sub-category {analysis.issue_subcategory}"
                         if analysis.issue_subcategory else ""))
        # Charles (2026-10-20): alongside the analysis, ask the Echo knowledge
        # base what to request from the customer next / the next debug steps
        # for this kind of issue — in the background, shown in its own panel.
        self._start_echo_advice(analysis, case_ctx)

        # Ball with the customer (Intel already asked for logs / info / repro
        # and nothing came back): nothing to analyze and nothing to ask.
        # Policy (2026-10-06): note it for the reviewer and stop here — no
        # analysis, no request reply, no first-response checklist.
        if analysis.action_owner == "customer":
            # Backstop: the reader sometimes says "customer to provide logs"
            # when nobody has asked yet (seen on 01022663: no attachments, no
            # Intel comment). Waiting is only possible after Intel spoke —
            # otherwise Intel owns the next step (the request below).
            from .case_reader import intel_has_asked
            from .composer import AI_MARKER
            if not intel_has_asked(case_ctx.comments, ai_marker=AI_MARKER):
                self.progress("read_case_history",
                              "owner corrected to Intel: no Intel comment has asked "
                              "the customer for anything yet")
                analysis.action_owner = "intel"
        if analysis.action_owner == "customer":
            with self._stage(analysis, "check_case_info"):
                self.progress("check_case_info",
                              "waiting on the customer — analysis and "
                              f"requests skipped ({analysis.next_action})")
            analysis.mode = "waiting_customer"
            analysis.ok = True
            analysis.error = ("waiting on the customer — analysis and "
                              "request replies skipped")
            return analysis

        # -- 3a. is the case information usable? -------------------------------
        # LLM-judged completeness (description clarity / issue time / repro
        # steps) with deterministic backstops for outright-empty fields.
        dump_items: list = []
        td = analysis.time_decision if isinstance(analysis.time_decision, dict) else {}
        analysis.time_decision = td
        gaps = td.setdefault("gaps", [])
        with self._stage(analysis, "check_case_info"):
            missing = list((analysis.case_reader or {}).get("missing_info") or [])
            flagged = {m.get("item") for m in missing}
            if (not (analysis.description or "").strip()
                    and not analysis.clean_description
                    and "issue_description" not in flagged):
                missing.append({"item": "issue_description",
                                "reason": "the case has no issue description"})
                gaps.append({"item": "issue_description", "action": "backstop",
                             "reason": "the case has no issue description"})
            if not analysis.issue_times and "issue_time" not in flagged:
                missing.append({"item": "issue_time",
                                "reason": "no failure time stated anywhere in the case"})
                gaps.append({"item": "issue_time", "action": "backstop",
                             "reason": "no minute-precise failure time anywhere in the case"})
            # A filled "Steps to reproduce" form field IS the repro steps —
            # retract a reader-flagged gap so we never ask for what the
            # customer already provided in the Environment Details form.
            if _env_repro_steps(analysis.env_detail):
                if any(m.get("item") == "repro_steps" for m in missing):
                    missing = [m for m in missing
                               if m.get("item") != "repro_steps"]
                    gaps.append({"item": "repro_steps", "action": "retracted",
                                 "reason": "the Environment Details form has 'Steps to reproduce'"})
                    self.progress("check_case_info",
                                  "repro steps found in the Environment "
                                  "Details form — gap retracted")
            # BSOD with a crash dump uploaded: the dump carries the crash, so
            # no issue time is needed (Charles, 2026-10-14) — retract that gap
            # instead of asking the customer; the BSOD branch below runs.
            analysis.bsod_case = _bsod.is_bsod_case(
                issue_domain=analysis.issue_domain, issue_type=analysis.issue_type,
                subject=analysis.subject, description=analysis.description,
                subcategory=analysis.subcategory)
            if analysis.bsod_case:
                dump_items = _bsod.find_dump_attachments(case_ctx.attachment_list)
                if dump_items and any(m.get("item") == "issue_time" for m in missing):
                    missing = [m for m in missing if m.get("item") != "issue_time"]
                    gaps.append({"item": "issue_time", "action": "retracted",
                                 "reason": "BSOD case: the crash dump carries the crash time"})
                    self.progress("check_case_info",
                                  "BSOD case with dump upload(s) "
                                  f"{', '.join(str(i[0]) for i in dump_items)} — "
                                  "issue time not required, gap retracted")
            analysis.missing_info = missing
            td["gate"] = ("request_info" if any(m.get("item") == "issue_time" for m in missing)
                          and not analysis.bsod_case else "ok")
            if analysis.bsod_case:
                td["gate"] = "bsod_no_time_needed" if dump_items else "bsod_no_dump"
            self.progress(
                "check_case_info",
                ("missing/unclear: " + ", ".join(m["item"] for m in missing))
                if missing else "all case info present")
            # Ball with Intel (customer provided logs/info, or asked): say
            # explicitly what the LATEST provided material is before the
            # gates below — any gap falls into the same request_info /
            # request_logs rules as a fresh case.
            if analysis.action_owner == "intel":
                archives = [str(a[0]) for a in (case_ctx.attachment_list or [])
                            if str(a[0]).lower().endswith(_ARCHIVE_EXTS)]
                nominated = (analysis.case_reader or {}).get("attachment_name") or ""
                self.progress(
                    "check_case_info",
                    "Intel to act — latest provided info: issue time "
                    + (f"{analysis.issue_times[0]}" if analysis.issue_times else "MISSING")
                    + "; log archive "
                    + (f"{nominated}" if nominated else
                       (f"newest of {len(archives)}" if archives else "MISSING")))

        # No minute-precise issue time anywhere (policy 2026-10-07: the issue
        # time is what the analysis anchors on; a bare date or an upload time
        # is not one) — or no understandable description either: ask for it
        # first and stop. Mode "request_info" (human-approved, posted PUBLIC
        # like request_logs); the log pipeline runs on the next round.
        # -- 3b. BSOD: dumps to the BSOD share + PotatoFarm (Avatar's flow) ----
        # Before the info gate: a BSOD case without a dump asks for the DUMP
        # (the info asks ride along in that same reply), not for a time.
        if analysis.bsod_case:
            return self._run_bsod_branch(analysis, case_ctx, dump_items)

        gap_items = {m.get("item") for m in analysis.missing_info}
        if "issue_time" in gap_items:
            analysis.mode = "request_info"
            analysis.ok = True
            analysis.error = (
                "case lacks both an understandable description and an issue time"
                if "issue_description" in gap_items else
                "no minute-precise issue time stated anywhere in the case"
            ) + " — drafted a request-info reply"
            return analysis

        # -- 4. pick the log-archive attachment --------------------------------
        # Reader's nomination first; newest-archive heuristic as fallback.
        # Accept every archive type the decomposer can extract — OEMs upload
        # .7z and .rar captures as often as .zip (case 00993799 was a .7z).
        zip_item = None
        pick_ok = False   # distinguishes "no archive found" from "stage crashed"
        with self._stage(analysis, "pick_zip"):
            from utils.etl_utils import extract_time_from_description
            from .case_reader import find_attachment
            chosen_name = (analysis.case_reader or {}).get("attachment_name") or ""
            pd = analysis.pick_decision = {
                "archives": _describe_attachments(case_ctx.attachment_list),
                "reader_pick": chosen_name,
                "reader_reason": str((analysis.case_reader or {}).get("attachment_reason") or "")[:300],
                "match": "", "fallback_used": False, "chosen": "", "chosen_rule": "",
                "prefetch": {"name": prefetch.attachment_name if prefetch is not None else "",
                             "used": False},
                "download_attempts": 0, "contents": {}, "captures": [],
                "etl_rule": "", "issue_time_used": "", "alternates_tried": 0,
            }
            if chosen_name:
                zip_item = find_attachment(case_ctx.attachment_list, chosen_name)
                pd["match"] = _match_kind(zip_item, chosen_name)
                if zip_item is not None and not str(zip_item[0]).lower().endswith(_ARCHIVE_EXTS):
                    self.progress("pick_zip",
                                  f"reader chose non-archive '{zip_item[0]}' — falling back")
                    pd["match"] = "non_archive"
                    zip_item = None
                elif zip_item is not None:
                    self.progress("pick_zip",
                                  f"reader chose '{zip_item[0]}' ({pd['match']} match)"
                                  + (f" — {pd['reader_reason'][:120]}" if pd["reader_reason"] else ""))
            if zip_item is None:
                zip_item = _pick_latest_archive(case_ctx.attachment_list)
                pd["fallback_used"] = bool(chosen_name)
                if zip_item is not None:
                    n_arch = sum(1 for a in pd["archives"] if a["is_archive"])
                    pd["chosen_rule"] = "only_archive" if n_arch == 1 else "newest_upload"
                    self.progress("pick_zip",
                                  (f"fallback to newest archive: {zip_item[0]}" if chosen_name
                                   else f"newest upload of {n_arch} archive(s): {zip_item[0]}"))
            else:
                pd["chosen_rule"] = "reader"
            if zip_item is not None:
                analysis.chosen_attachment = str(zip_item[0])
                pd["chosen"] = str(zip_item[0])
                try:
                    subtitle = (zip_item[2] or ["", ""])[1] if len(zip_item) > 2 else ""
                    analysis.attachment_time = extract_time_from_description(subtitle) or ""
                except Exception:
                    pass
            pick_ok = True
        if not pick_ok:
            # pick_zip CRASHED (_stage swallowed the exception) — the attached
            # archive could not be processed. Policy (2026-10-02): ask the
            # customer to RE-UPLOAD rather than silently falling to triage.
            analysis.mode = "request_logs"
            analysis.log_request_reason = "unreadable_archive"
            analysis.ok = True
            analysis.error = ("attached archive could not be processed — "
                              "drafted a re-upload request to the customer")
            return analysis
        if zip_item is None:
            # No archive at all — certainly no WRT logs. Record the check
            # stage explicitly so the stage table tells the story, then draft
            # a request-logs reply (mode "request_logs": human-approved,
            # posted as a PUBLIC comment so the customer sees it).
            with self._stage(analysis, "check_wrt_log"):
                self.progress("check_wrt_log",
                              "no log archive attached — WRT logs missing")
            analysis.mode = "request_logs"
            analysis.ok = True
            analysis.error = ("no WRT log archive (.zip/.rar/.7z) attached — "
                              "drafted a request-logs reply to the customer")
            return analysis

        # -- 5. download (one automatic retry) -----------------------------------
        downloaded = []   # [file_path, name, already_dload]
        with self._stage(analysis, "download"):
            from utils.attachment_download import run_dload_threads
            # The newest archive was prefetched during triage + reader; use
            # it when the reader chose that same file.
            if prefetch is not None and prefetch.attachment_name == str(zip_item[0]):
                self.progress("download",
                              f"waiting for the prefetched {prefetch.attachment_name}")
                prefetch.join()
                if prefetch.items and not prefetch.error:
                    downloaded.extend(prefetch.items)
                    analysis.pick_decision.get("prefetch", {})["used"] = True
                    self.progress("download", "prefetched during triage/reader")
                else:
                    self.progress("download",
                                  f"prefetch failed ({prefetch.error}); downloading normally")
            # A file that came with the mail is already on disk.
            local_path = (getattr(case_ctx, "local_paths", None) or {}).get(str(zip_item[0]))
            if not downloaded and local_path and Path(local_path).exists():
                downloaded.append([local_path, str(zip_item[0]), True])
                self.progress("download", "attached to the mail — already on disk, nothing to download")
            for attempt in (1, 2):
                if downloaded:
                    break
                analysis.pick_decision["download_attempts"] = attempt
                try:
                    for item in run_dload_threads([zip_item],
                                                  case_ctx.case_download_dir,
                                                  socketio=None):
                        downloaded.append(item)
                except Exception as e:
                    self.progress("download",
                                  f"attempt {attempt} failed: "
                                  f"{type(e).__name__}: {e}")
                if downloaded:
                    break
                if attempt == 1:
                    self.progress("download", "retrying download once…")
        if not downloaded:
            # Both attempts failed. Policy (2026-10-02): ask the customer to
            # re-upload the archive (it may be corrupted on the share).
            analysis.mode = "request_logs"
            analysis.log_request_reason = "unreadable_archive"
            analysis.ok = True
            analysis.error = ("attachment download failed twice — "
                              "drafted a re-upload request to the customer")
            return analysis

        # -- 6. decompose ------------------------------------------------------
        wifi_files: list = []
        ddd_files: list = []
        bt_files: list = []
        decompose_ok = False
        with self._stage(analysis, "decompose"):
            from utils.attachment_decompose import process_single_zip
            file_path, _name, already = downloaded[0]
            wifi_files, ddd_files, _evt, bt_files, _fw = process_single_zip(
                file_path, case_ctx.case_download_dir, already)
            analysis.archive_contents = {
                "wifi_wpp": len(wifi_files or []), "ddd": len(ddd_files or []),
                "event_logs": len(_evt or []), "bt": len(bt_files or []),
                "fw": len(_fw or [])}
            analysis.pick_decision["contents"] = dict(analysis.archive_contents)
            decompose_ok = True
        if not decompose_ok:
            # decompose CRASHED (_stage swallowed the exception): the archive
            # may well contain WRT logs we simply failed to extract — asking
            # the customer to re-upload would be wrong AND public. Empty file
            # lists mean "no logs" ONLY when decompose itself succeeded.
            analysis.mode = "triage_only"
            analysis.ok = bool(analysis.triage)
            analysis.error = "attachment decompose failed"
            return analysis
        # BT classification mirrors Avatar's local-upload rule: an extracted
        # ibtpci-*.etl or ibtusb-*.etl is returned in bt_files.
        if analysis.wifi_or_bt == "bt":
            with self._stage(analysis, "check_bt_log"):
                analysis.bt_case_valid = bool(bt_files)
                if bt_files:
                    self.progress("check_bt_log",
                                  f"valid BT case: recognized {len(bt_files)} "
                                  "BT ETL(s)")
                else:
                    self.progress("check_bt_log",
                                  f"'{analysis.chosen_attachment}' contains no "
                                  "recognized BT ETL (ibtpci-/ibtusb-)")
            if not analysis.bt_case_valid:
                analysis.mode = "triage_only"
                analysis.ok = bool(analysis.triage)
                analysis.error = "BT case archive contains no recognized BT ETL"
                return analysis

        if analysis.wifi_or_bt == "wifi":
            # -- 6b. confirm WRT logs exist in the unzipped archive ------------
            # The archive downloaded and decomposed — now verify it actually
            # contains driver ETL traces (WRT wifi ETLs / DDD). An archive of
            # screenshots or dmesg dumps is not analyzable: ask for WRT logs.
            with self._stage(analysis, "check_wrt_log"):
                if wifi_files or ddd_files:
                    self.progress("check_wrt_log",
                                  f"ETLs found — wifi(WRT): {len(wifi_files)}, "
                                  f"DDD: {len(ddd_files)}")
                else:
                    self.progress("check_wrt_log",
                                  f"'{analysis.chosen_attachment}' contains no "
                                  "WRT/DDD ETL logs")
            if not wifi_files and not ddd_files:
                analysis.mode = "request_logs"
                analysis.log_request_reason = "no_wrt_inside"
                analysis.ok = True
                analysis.error = (f"attachment '{analysis.chosen_attachment}' contains "
                                  "no WRT driver (WPP) ETL logs — drafted a request-logs reply")
                return analysis

        # -- 7. issue times -----------------------------------------------------
        # The reader (description + comments) is the only source of issue
        # times: a case without a minute-precise one stopped at 3a. The
        # description-only organizer is kept just as a clean_description
        # fallback when the reader failed; an attachment upload time is
        # never used as the issue time.
        with self._stage(analysis, "issue_time"):
            if not analysis.clean_description:
                from utils.issue_time_ai import organize_issue_context
                llm = app_config.llm_helper
                organized = organize_issue_context(
                    analysis.description,
                    llm_client=getattr(llm, "client", None),
                    llm_model=getattr(llm, "model", None),
                ) or {}
                analysis.clean_description = (organized.get("clean_description")
                                              or "").strip()
                analysis.time_decision["organizer_used"] = True
            self.progress("issue_time",
                          "anchoring on " + ", ".join(analysis.issue_times[:3]))
        if not analysis.clean_description:
            analysis.clean_description = analysis.description or analysis.subject

        # -- 8. pick the ETL ----------------------------------------------------
        etl_path = None
        with self._stage(analysis, "pick_etl"):
            pd = analysis.pick_decision
            pd["captures"] = _describe_captures(
                list(wifi_files or []) + list(ddd_files or []) + list(bt_files or []),
                analysis.issue_times[0] if analysis.issue_times else "")
            if analysis.wifi_or_bt == "bt":
                etl_path = self._pick_bt_etl(bt_files)
                pd["etl_rule"] = "bt_newest"
            else:
                # YB / assert issues: the capture folder whose NAME carries a
                # nonzero error code (e.g. ..._12_4222_0xa_0x0_0x1fffff) is the
                # one that recorded the failure — prefer the EARLIEST such folder
                # (first occurrence) over the AI-time / newest heuristics.
                yb_assert = bool(_YB_ASSERT_ISSUE_RE.search(
                    " ".join([analysis.issue_type or "", analysis.subject or "",
                              analysis.clean_description or "",
                              analysis.description or ""])))
                analysis.yb_case = yb_assert or analysis.issue_domain == "Yellow Bang"
                if yb_assert:
                    etl_path = _pick_etl_yb_earliest(wifi_files, ddd_files)
                    if etl_path:
                        pd["etl_rule"] = "yb_earliest"
                        self.progress(
                            "pick_etl",
                            "YB/assert issue — earliest capture with nonzero "
                            f"error code in folder name: {os.path.basename(os.path.dirname(etl_path))}")
                if not etl_path:
                    etl_path, rule, used = self._pick_etl(
                        wifi_files, ddd_files,
                        analysis.issue_times[0] if analysis.issue_times else "")
                    pd["etl_rule"], pd["issue_time_used"] = rule, used
                    if etl_path:
                        folder = os.path.basename(os.path.dirname(etl_path))
                        self.progress("pick_etl", {
                            "closest_at_or_after_issue": f"closest capture at/after the issue time {used}: {folder}",
                            "newest_ddd": f"newest DDD capture: {folder}"
                                          + ("" if used else " (no usable issue time)"),
                            "newest_wifi": f"newest WRT capture: {folder}"
                                           + ("" if used else " (no usable issue time)"),
                        }.get(rule, f"capture: {folder}"))
            analysis.etl_path = etl_path or ""
            if etl_path:
                for c in pd["captures"]:
                    if c["folder"] == os.path.basename(os.path.dirname(etl_path)):
                        c["role"] = "chosen"
        if not etl_path:
            analysis.mode = "triage_only"
            analysis.ok = bool(analysis.triage)
            analysis.error = "could not select an ETL file"
            return analysis

        # -- 9. decode ETL -> analysis log --------------------------------------
        if analysis.wifi_or_bt == "bt":
            # Same as the Wi-Fi branch: an already-decoded HCI is a fast
            # "done", not a skip, or the stage table shows decode_bt pending
            # forever on re-runs.
            log_path = ""
            with self._stage(analysis, "decode_bt"):
                from services.etl_parser.bt_parser import find_ready_hci
                log_path = find_ready_hci(etl_path) or ""
                if log_path:
                    self.progress("decode_bt",
                                  f"reusing existing decoded HCI ({os.path.basename(log_path)})")
                else:
                    log_path = self._decode_bt(etl_path) or ""
        else:
            # Always record the stage: a cached .log (this case analyzed before)
            # is a fast "done", not a skip — otherwise the stage table shows a
            # confusing forever-"pending" row on re-runs.
            log_path = etl_path + ".log"
            with self._stage(analysis, "decode_etl"):
                if os.path.exists(log_path):
                    self.progress("decode_etl",
                                  f"reusing existing decoded log "
                                  f"({os.path.basename(log_path)})")
                else:
                    self._decode_etl(etl_path)
        if not os.path.exists(log_path):
            analysis.mode = "triage_only"
            analysis.ok = bool(analysis.triage)
            # When the decoder failed because the PDB symbol database for this
            # driver build is missing/purged (e.g. "Netwaw18.pdb could not be
            # extracted"), say so explicitly — the customer's logs are FINE,
            # the decode environment needs a PDB refresh.
            decode_detail = next((s.detail for s in reversed(analysis.stages)
                                  if s.name == "decode_etl"), "")
            if re.search(r"(?i)\bpdb\b", decode_detail or ""):
                analysis.error = (
                    "failed to decompose/decode the WRT log because the PDB "
                    "symbol database for this driver build is out of date or "
                    "unavailable — the uploaded logs are fine; analysis can "
                    f"re-run after a PDB refresh ({decode_detail[:160]})")
            else:
                analysis.error = (
                    f"BT HCI decode did not produce {os.path.basename(log_path)}"
                    if analysis.wifi_or_bt == "bt" else
                    f"ETL decode did not produce {os.path.basename(log_path)}")
            return analysis
        analysis.log_path = log_path

        # -- 9b. does the log actually cover the reported issue time? ----------
        # e.g. case 01025350: issue stated at 17:14, capture covered
        # 17:39–17:44. The agent may still find an assert, but the reviewer
        # and customer must know when the WRT capture does not cover the issue.
        # This policy relies on WRT .log ranges and must not inspect BT HCI logs.
        if analysis.wifi_or_bt == "wifi":
            try:
                self._check_time_coverage(analysis)
            except Exception as e:
                print(f"[handsfree.runner] time-coverage check failed (non-fatal): {e}")
            _set_capture(analysis, etl_path, decode="ok",
                         covered=not analysis.time_mismatch)
            also_tried = 0
            if analysis.time_mismatch:
                for alt in self._alternate_etls(
                        wifi_files, ddd_files, analysis.issue_times, {etl_path})[:2]:
                    alt_log = alt + ".log"
                    _set_capture(analysis, alt, role="alternate")
                    with self._stage(analysis, "decode_etl"):
                        self.progress("decode_etl",
                                      "trying the capture closest to the issue time: "
                                      f"{os.path.basename(os.path.dirname(alt))}")
                        if not os.path.exists(alt_log):
                            self._decode_etl(alt)
                    if not os.path.exists(alt_log):
                        _set_capture(analysis, alt, decode="failed")
                        continue
                    also_tried += 1
                    analysis.time_mismatch = {}
                    analysis.etl_path, analysis.log_path = alt, alt_log
                    try:
                        self._check_time_coverage(analysis)
                    except Exception as e:
                        print(f"[handsfree.runner] time-coverage check failed (non-fatal): {e}")
                    _set_capture(analysis, alt, decode="ok", covered=not analysis.time_mismatch)
                    if not analysis.time_mismatch:
                        _set_capture(analysis, alt, role="chosen")
                        break
                analysis.pick_decision["alternates_tried"] = also_tried
                if analysis.time_mismatch and also_tried:
                    analysis.time_mismatch["also_tried"] = also_tried
            if analysis.time_mismatch and analysis.yb_case:
                # Yellow-bang/assert logs can be useful even when their timestamps
                # differ, provided the log contains the relevant assert.
                from .echo_client import scan_wrt_log_for_asserts
                codes = [a["code"] for a in
                         scan_wrt_log_for_asserts(analysis.log_path)]
                if codes:
                    analysis.time_mismatch["yb_assert_override"] = codes
                    self.progress("decode_etl",
                                  f"YB case: assert(s) {', '.join(codes)} found in the "
                                  "log — continuing despite the issue-time mismatch")
            if (analysis.time_mismatch
                    and not analysis.time_mismatch.get("yb_assert_override")):
                analysis.mode = "request_logs"
                analysis.log_request_reason = "log_not_covering"
                analysis.ok = True
                analysis.error = (
                    f"attached WRT log covers {analysis.time_mismatch['log_first']} – "
                    f"{analysis.time_mismatch['log_last']}, the reported issue time "
                    f"{', '.join(analysis.time_mismatch['issue_times'])} is outside — "
                    "drafted a request for a log captured at the issue time")
                self.progress("decode_etl", analysis.error)
                return analysis

        # -- 10. agentic analysis (one run per incident time) --------------------
        with self._stage(analysis, "agent_analysis"):
            self._run_agent(analysis, max_steps=max_steps)

        # -- 11. Echo knowledge base: assert / yellow-bang root cause ------------
        # When the agent surfaced a firmware assert (or yellow-bang evidence),
        # resolve the assert to its driver-header entry and ask the Echo
        # knowledge base (MCP, KB-only mode — no credentials) for the known
        # root cause. Echo being down never affects the pipeline: failures are
        # recorded per-insight, and _stage() contains anything unexpected.
        with self._stage(analysis, "echo_kb"):
            from .echo_client import (EchoUnavailable, ask_echo_kb,
                                      collect_echo_insights, find_assert_evidence)
            # Only asserts around the issue time are this issue's evidence.
            evidence = find_assert_evidence(
                analysis, window=self._assert_window(analysis))
            if evidence.get("ignored_outside_window"):
                self.progress("echo_kb",
                              f"ignored {evidence['ignored_outside_window']} assert(s) "
                              "outside the issue-time window")
            if evidence["assert_codes"] or evidence["yellow_bang"]:
                src = {"wrt_log": "from WRT log", "agent_text": "from agent text",
                       "env_detail": "from IPS Environment Details form",
                       None: ""}.get(evidence.get("source"), "")
                echo_summary = (f"asking Echo KB — asserts: {evidence['assert_codes'] or 'none'}"
                                + (f" ({src})" if src else "")
                                + f", yellow bang: {evidence['yellow_bang']}")
                self.progress("echo_kb", echo_summary)
                echo_live: list[dict] = []
                self._snap(echo_live_summary=echo_summary, echo_live=[])

                def _clip(text: object, limit: int) -> str:
                    s = str(text).strip()
                    return s if len(s) <= limit else s[:limit] + " …[truncated]"

                def _ask_logged(question: str) -> str:
                    # Surface the exchange in the run log (full text stays in
                    # analysis.echo_insights → draft's Analysis details) and
                    # in the live panel as each answer arrives.
                    self.progress("echo_kb", "Q → Echo:\n" + _clip(question, 700))
                    entry = {"kind": "", "code": None, "question": _clip(question, 400),
                             "answer": "", "error": ""}
                    echo_live.append(entry)
                    self._snap(echo_live=[dict(e) for e in echo_live])
                    try:
                        answer = ask_echo_kb(question)
                    except EchoUnavailable as e:
                        self.progress("echo_kb", f"Echo unavailable: {_clip(e, 300)}")
                        entry["error"] = _clip(e, 300)
                        self._snap(echo_live=[dict(e) for e in echo_live])
                        raise
                    self.progress("echo_kb", "A ← Echo:\n" + _clip(answer, 900))
                    entry["answer"] = _clip(answer, 900)
                    self._snap(echo_live=[dict(e) for e in echo_live])
                    return answer

                # The same windowed evidence drives the questions: a
                # recompute without the window would ask about the asserts
                # just ignored as another incident's.
                analysis.echo_insights = collect_echo_insights(analysis,
                                                               ask=_ask_logged,
                                                               evidence=evidence)
                answered = sum(1 for i in analysis.echo_insights if i.get("answer"))
                self.progress(
                    "echo_kb",
                    f"{answered}/{len(analysis.echo_insights)} insight(s) answered")
                # Final form: kind + code per insight (the live entries only
                # knew the question text).
                self._snap(echo_live=[_compact_insight(i) for i in analysis.echo_insights
                                      if isinstance(i, dict)],
                           echo_live_summary=echo_summary
                           + f" — {answered}/{len(analysis.echo_insights)} answered")
            else:
                self.progress("echo_kb", "no assert / yellow-bang evidence — skipped")
                self._snap(echo_live_summary="no assert / yellow-bang evidence — skipped",
                           echo_live=[])

        got_report = any(i.report for i in analysis.incidents)
        analysis.mode = "full" if got_report else "triage_only"
        analysis.ok = got_report or bool(analysis.triage)
        if not got_report:
            analysis.error = analysis.error or "agent produced no report; triage used instead"
        return analysis

    # ------------------------------------------------------------------
    def _check_time_coverage(self, analysis: CaseAnalysis,
                             grace_minutes: int = TIME_GRACE_MIN) -> None:
        """Flag when NO reported issue time falls inside the decoded log's
        time range (±grace). Full datetimes are mapped into the log's clock;
        a clock-only value ("10:17", accepted as minute-precise) is placed
        on the dates the log spans — a capture of 08:00–08:10 does not cover
        an issue reported at 10:17 just because the date was not stated.
        Sets analysis.time_mismatch and emits a WARNING progress line."""
        from datetime import timedelta
        from utils.issue_time_utils import format_issue_time, read_log_time_range

        if not analysis.issue_times or not analysis.log_path:
            return
        first, last = read_log_time_range(analysis.log_path)
        if not first or not last:
            return

        grace = timedelta(minutes=grace_minutes)
        checked: list[str] = []
        mapped: list[str] = []       # each checked value in the log's clock
        covered = None               # (customer_dt, log_dt, anchor) that matched
        customer_tz = self._customer_tz(analysis.log_path)
        placements: list[dict] = []  # live panel: every candidate tried
        for raw in analysis.issue_times:
            cands = self._issue_time_candidates_tagged(raw, analysis.log_path, first, last)
            if not cands:
                placements.append({"time": str(raw), "candidates": [],
                                   "note": "clock-only time occurs on no date the log spans"})
                continue
            checked.append(str(raw))
            rows = []
            for cust, cand, anchor in cands[:8]:
                inside = first - grace <= cand <= last + grace
                delta = 0 if inside else int((first - cand).total_seconds() if cand < first
                                              else (cand - last).total_seconds())
                rows.append({"customer": format_issue_time(cust, with_ms=False),
                             "log": format_issue_time(cand, with_ms=False),
                             "anchor": anchor, "inside": inside, "outside_s": delta})
            placements.append({"time": str(raw), "candidates": rows})
            for cust, cand, anchor in cands:
                if first - grace <= cand <= last + grace:
                    covered = (cust, cand, anchor)
                    break
            if covered:
                break
            best = self._preferred_candidate(cands)
            mapped.append(format_issue_time(best[1], with_ms=False) if best else "")
        td = analysis.time_decision if isinstance(analysis.time_decision, dict) else {}
        analysis.time_decision = td
        td["placements"] = placements
        td["coverage"] = {"log_file": os.path.basename(analysis.log_path or ""),
                          "log_first": format_issue_time(first, with_ms=False),
                          "log_last": format_issue_time(last, with_ms=False),
                          "grace_min": grace_minutes, "covered": bool(covered),
                          "customer_tz": customer_tz}
        if covered:
            td["coverage"]["chosen"] = {"customer": format_issue_time(covered[0], with_ms=False),
                                       "log": format_issue_time(covered[1], with_ms=False),
                                       "anchor": covered[2]}

        if covered:
            cust, cand, anchor = covered
            analysis.time_alignment = {
                "customer_time": format_issue_time(cust, with_ms=False),
                "log_time": format_issue_time(cand, with_ms=False),
                "customer_tz": customer_tz,
                "anchor": anchor,
                "offset": _fmt_offset(cand - cust),
            }
            if anchor != ANCHOR_AS_IS:
                self.progress(
                    "decode_etl",
                    f"issue time {analysis.time_alignment['customer_time']} (customer) = "
                    f"{analysis.time_alignment['log_time']} (log clock, "
                    f"{analysis.time_alignment['offset']} via {ANCHOR_LABELS[anchor]})")
        elif checked:
            analysis.time_mismatch = {
                "issue_times": checked,
                "log_first": format_issue_time(first, with_ms=False),
                "log_last": format_issue_time(last, with_ms=False),
                "grace_minutes": grace_minutes,
                "customer_tz": customer_tz,
            }
            if any(mapped):
                analysis.time_mismatch["issue_times_log"] = mapped
            # The log's range in the customer's clock, so the public request
            # never shows the customer Taiwan times.
            shift = self._log_to_customer_shift(analysis.log_path, first, last, customer_tz)
            if shift:
                cust_first, cust_last, anchor, offset = shift
                analysis.time_mismatch.update({
                    "log_first_customer": format_issue_time(cust_first, with_ms=False),
                    "log_last_customer": format_issue_time(cust_last, with_ms=False),
                    "anchor": anchor,
                    "offset": _fmt_offset(offset),
                })
            self.progress(
                "decode_etl",
                f"WARNING: log covers {analysis.time_mismatch['log_first']} – "
                f"{analysis.time_mismatch['log_last']} but the reported issue "
                f"time(s) {', '.join(checked)} are OUTSIDE this window")

    @staticmethod
    def _customer_tz(log_path: str) -> str:
        """The customer's time zone name (system_info.txt / sidecar), '' when unknown."""
        try:
            from utils.timezone_utils import get_effective_timezone
            return get_effective_timezone(log_path) or ""
        except Exception:
            return ""

    @staticmethod
    def _log_to_customer_shift(log_path: str, first, last, customer_tz: str):
        """(first, last, anchor, offset) with the log's range expressed in the
        customer's clock: the tz helper when it moves the time, else the
        capture-folder stamp, else None (same clock as far as we know)."""
        from datetime import timedelta
        if first is None or last is None:
            return None
        if customer_tz:
            try:
                from utils.timezone_utils import taiwan_to_local
                cf, cl = taiwan_to_local(first, customer_tz), taiwan_to_local(last, customer_tz)
                if cf is not None and cl is not None and cf != first:
                    return cf, cl, ANCHOR_TZ, first - cf
            except Exception:
                pass
        folder_ts = _folder_timestamp(log_path)
        if folder_ts is not None:
            offset = last - folder_ts
            if timedelta(minutes=20) <= abs(offset) <= timedelta(hours=26):
                return first - offset, last - offset, ANCHOR_FOLDER, offset
        return None

    @staticmethod
    def _preferred_candidate(cands: list):
        """Of the tagged candidates for one value, the one to quote when none
        falls inside the log: tz helper, then folder stamp, then as-is."""
        for want in (ANCHOR_TZ, ANCHOR_FOLDER, ANCHOR_AS_IS):
            for c in cands:
                if c[2] == want:
                    return c
        return cands[0] if cands else None

    # Asserts count as evidence for THIS issue only inside this window
    # around the reported time (minutes before / after).
    ASSERT_WINDOW_BEFORE_MIN = 30
    ASSERT_WINDOW_AFTER_MIN = 10

    @staticmethod
    def _log_frame_candidates(dt, log_path: str, first, last) -> list:
        """The reported issue time expressed in the decoded log's clock.

        Two anchors, both kept as candidates (a match on either counts):
          * the tz helper (system_info.txt time zone vs the decode host) —
            a no-op when both are GMT+8;
          * the capture folder stamp: WRT names the folder with the DUT's
            clock at dump time, and the log's last line is that same moment
            in the log's clock, so (last line − folder stamp) is the actual
            offset between the two clocks for THIS capture. Seen on
            00994274: tz says GMT+8 = GMT+8, yet the log ran 8 h ahead of
            the DUT clock; the helper alone rejected a log that covered the
            issue.
        """
        return [c for c, _anchor in
                HandsfreeRunner._log_frame_candidates_tagged(dt, log_path, first, last)]

    @staticmethod
    def _log_frame_candidates_tagged(dt, log_path: str, first, last) -> list:
        """_log_frame_candidates with the anchor that produced each value:
        [(log_dt, ANCHOR_AS_IS | ANCHOR_TZ | ANCHOR_FOLDER)], as-is first."""
        from datetime import timedelta
        cands = [(dt, ANCHOR_AS_IS)]
        seen = [dt]
        try:
            from utils.issue_time_ai import determine_issue_time_frames
            mapped = determine_issue_time_frames(
                dt, [log_path], first, last).get("log_frame")
            if mapped and mapped not in seen:
                cands.append((mapped, ANCHOR_TZ))
                seen.append(mapped)
        except Exception:
            pass
        folder_ts = _folder_timestamp(log_path)
        if folder_ts is not None and last is not None:
            offset = last - folder_ts
            if abs(offset) <= timedelta(hours=26) and abs(offset) >= timedelta(minutes=20):
                shifted = dt + offset
                if shifted not in seen:
                    cands.append((shifted, ANCHOR_FOLDER))
        return cands

    @staticmethod
    def _issue_time_candidates(raw: str, log_path: str, first, last) -> list:
        """The reported issue time as datetimes in the log's clock. A full
        datetime goes through _log_frame_candidates; a clock-only value is
        placed on every date the log spans (one day either side too, for
        the clock offsets _log_frame_candidates applies) and each placement
        mapped the same way. [] when the value does not parse, or when a
        clock-only value has no log range to be placed on."""
        return [c for _cust, c, _anchor in
                HandsfreeRunner._issue_time_candidates_tagged(raw, log_path, first, last)]

    @staticmethod
    def _issue_time_candidates_tagged(raw: str, log_path: str, first, last) -> list:
        """_issue_time_candidates keeping the customer-clock value each log
        candidate came from: [(customer_dt, log_dt, anchor)]. For a clock-only
        value customer_dt is the clock placed on a date the log spans."""
        from datetime import datetime, timedelta
        from utils.issue_time_utils import parse_issue_time_string
        dt, time_only = parse_issue_time_string(str(raw))
        if dt is None:
            return []
        if not time_only:
            return [(dt, c, a) for c, a in
                    HandsfreeRunner._log_frame_candidates_tagged(dt, log_path, first, last)]
        if first is None or last is None:
            return []
        out: list = []
        seen: list = []
        day0 = first.date() - timedelta(days=1)
        days = min((last.date() - first.date()).days, 7) + 2
        for n in range(days + 1):
            placed = datetime.combine(day0 + timedelta(days=n), dt.time())
            for cand, anchor in HandsfreeRunner._log_frame_candidates_tagged(
                    placed, log_path, first, last):
                if cand not in seen:
                    seen.append(cand)
                    out.append((placed, cand, anchor))
        return out

    def _align_issue_time(self, raw: str, log_path: str, first, last) -> Optional[dict]:
        """One reported issue time placed in the log's clock:
        {customer: dt, log: dt, anchor, inside: bool}. The candidate that
        falls inside the log window wins (inside=True); otherwise, full
        datetimes only, the tz helper's mapping, then the folder stamp's,
        then the value as-is (inside=False). A clock-only value that occurs
        nowhere in the log gives None, as does an unparseable one."""
        from utils.issue_time_utils import parse_issue_time_string
        cands = self._issue_time_candidates_tagged(raw, log_path, first, last)
        if not cands:
            return None
        if first and last:
            # Same grace as the coverage check: a capture usually starts
            # seconds to minutes after the reported moment (01033659: the
            # reported 20:10:00 vs a log starting 20:11:13).
            from datetime import timedelta
            grace = timedelta(minutes=self.TIME_GRACE_MIN)
            for cust, cand, anchor in cands:
                if first - grace <= cand <= last + grace:
                    return {"customer": cust, "log": cand, "anchor": anchor, "inside": True}
        if parse_issue_time_string(str(raw))[1]:
            return None          # clock-only, never inside the log: no anchor
        cust, cand, anchor = self._preferred_candidate(cands)
        return {"customer": cust, "log": cand, "anchor": anchor, "inside": False}

    def _issue_datetime_in_log_frame(self, analysis: CaseAnalysis):
        """First reported issue time as a full datetime in the log's clock
        (see _align_issue_time), or None."""
        from utils.issue_time_utils import read_log_time_range
        if not analysis.issue_times or not analysis.log_path:
            return None
        first, last = read_log_time_range(analysis.log_path)
        for raw in analysis.issue_times:
            if not self._issue_time_candidates_tagged(raw, analysis.log_path, first, last):
                continue
            align = self._align_issue_time(raw, analysis.log_path, first, last)
            return align["log"] if align else None
        return None

    def _run_bsod_branch(self, analysis: CaseAnalysis, case_ctx,
                         dump_items: list) -> CaseAnalysis:
        """BSOD case: no WRT pipeline. The dump upload(s) go to the BSOD
        share and the PotatoFarm parser is triggered — what Avatar's manual
        BSOD flow does (index toggle -> download -> bsod_submit). PotatoFarm
        e-mails the result; the draft tells the reviewer what was submitted.
        Uploads already submitted on an earlier round are not resubmitted."""
        names = [str(it[0]) for it in dump_items]
        codes = ""
        for q, a in (analysis.env_detail or {}).items():
            if (re.search(r"assert error", str(q), re.IGNORECASE)
                    and str(a or "").strip().lower() not in ("", "na", "n/a", "none")):
                codes = str(a).strip()
        analysis.bsod = {"dumps": names, "bugcheck_codes": codes, "dumps_path": "",
                         "email": "", "submitted": False, "status": "", "detail": "",
                         "skipped": "", "previous": [], "failed": [],
                         "results": [], "result_html": "", "result_waited_s": 0}
        prev: list = []
        with self._stage(analysis, "bsod_dumps"):
            prev = _bsod.submissions(analysis.case_nbr)
            analysis.bsod["previous"] = [{"submitted_at": p_.get("submitted_at"),
                                         "attachments": p_.get("attachments")}
                                        for p_ in prev]
            self.progress("bsod_dumps",
                          f"dump upload(s): {', '.join(names) or 'none'}"
                          + (f"; bug check codes from the IPS form: {codes}" if codes else "")
                          + (f"; {len(prev)} earlier submission(s)" if prev else ""))
        if not names:
            analysis.mode = "request_logs"
            analysis.log_request_reason = "no_dump"
            analysis.ok = True
            analysis.error = ("BSOD case without a kernel/memory dump attached — "
                              "drafted a request for the dump")
            return analysis
        skip = _bsod.names_to_skip(analysis.case_nbr)
        new_items = [it for it in dump_items if str(it[0]) not in skip]
        analysis.bsod["failed"] = sorted(
            n for n, c in _bsod.failed_names(analysis.case_nbr).items()
            if c >= _bsod.MAX_DOWNLOAD_ROUNDS and n in names)
        if not new_items:
            return self._bsod_pending(analysis, case_ctx, prev,
                                      "no new dump upload since")

        downloaded: list = []
        with self._stage(analysis, "bsod_download"):
            dest = _bsod.share_case_dir(analysis.case_nbr,
                                        getattr(case_ctx, "backend_id", "") or "")
            analysis.bsod["dumps_path"] = dest
            if not dest:
                raise RuntimeError("BSOD dump share unreachable")
            from utils.attachment_download import run_dload_threads
            # Dumps that came with the mail: copy them to the share.
            local = getattr(case_ctx, "local_paths", None) or {}
            if local:
                import shutil
                remaining = []
                for it in new_items:
                    src = local.get(str(it[0]))
                    if src and Path(src).exists():
                        target = str(Path(dest) / str(it[0]))
                        if not Path(target).exists():
                            shutil.copy2(src, target)
                        downloaded.append([target, str(it[0]), True])
                        self.progress("bsod_download", f"{it[0]}: copied from the mail attachment")
                    else:
                        remaining.append(it)
                new_items = remaining
            for attempt in (1, 2):
                if not new_items:
                    break
                try:
                    for item in run_dload_threads(new_items, dest, socketio=None):
                        downloaded.append(item)
                except Exception as e:
                    self.progress("bsod_download",
                                  f"attempt {attempt} failed: {type(e).__name__}: {e}")
                if downloaded:
                    break
            self.progress("bsod_download",
                          f"{len(downloaded)}/{len(new_items)} dump upload(s) in {dest}")
        if not analysis.bsod["dumps_path"]:
            analysis.mode = "triage_only"
            analysis.ok = bool(analysis.triage)
            analysis.error = ("BSOD dump share unreachable — dumps not submitted to "
                              "PotatoFarm (submit manually via Avatar's BSOD flow)")
            return analysis
        got_names = [str(it[1]) for it in downloaded]
        failed_now = [str(it[0]) for it in new_items if str(it[0]) not in got_names]
        if failed_now:
            self.progress("bsod_download",
                          f"download FAILED for {', '.join(failed_now)} — retried on the "
                          f"next round, given up after {_bsod.MAX_DOWNLOAD_ROUNDS} rounds")
        if not downloaded:
            if prev:
                # Earlier rounds already submitted the other upload(s): just
                # remember this failure (no resubmission of an unchanged folder).
                _bsod.record_submission(analysis.case_nbr, [], analysis.bsod["dumps_path"],
                                        "", "download_failed", failed=failed_now)
                analysis.bsod["failed"] = sorted(
                    n for n, c in _bsod.failed_names(analysis.case_nbr).items()
                    if c >= _bsod.MAX_DOWNLOAD_ROUNDS and n in names)
                return self._bsod_pending(analysis, case_ctx, prev,
                                          f"download failed for {', '.join(failed_now)}")
            analysis.mode = "request_logs"
            analysis.log_request_reason = "unreadable_archive"
            analysis.chosen_attachment = str(new_items[0][0])
            analysis.ok = True
            analysis.error = "dump download failed twice — drafted a re-upload request"
            return analysis

        cfg = {}
        prev_mtime = 0.0
        with self._stage(analysis, "bsod_submit"):
            cfg = _bsod.handsfree_config()
            email = _bsod.resolve_email(cfg)
            analysis.bsod["email"] = email
            prev_mtime = _bsod.result_mtime(dest)
            if cfg.get("trial_mode"):
                analysis.bsod["skipped"] = "trial mode — PotatoFarm not triggered"
            elif not email:
                analysis.bsod["skipped"] = ("no e-mail for the PotatoFarm result (set "
                                            "bsod_email in the handsfree config)")
            if analysis.bsod["skipped"]:
                self.progress("bsod_submit", analysis.bsod["skipped"])
            else:
                res = _bsod.submit_to_potatofarm(dest, email)
                analysis.bsod.update(submitted=bool(res.get("ok")),
                                     status=str(res.get("status") or ""),
                                     detail=str(res.get("detail") or ""))
                if res.get("ok"):
                    _bsod.record_submission(analysis.case_nbr, got_names, dest, email,
                                            res.get("status"), failed=failed_now)
                    self.progress("bsod_submit",
                                  f"PotatoFarm accepted {dest} — result goes to {email}")
                else:
                    self.progress("bsod_submit",
                                  f"PotatoFarm refused: HTTP {res.get('status')} "
                                  f"{res.get('detail')}")
        if analysis.bsod.get("submitted"):
            # The parser writes bsod_parse_output.json / bsod_report.html into
            # the same folder a few minutes later: wait for it so the draft
            # carries the verdict (the e-mail is only a copy).
            with self._stage(analysis, "bsod_result"):
                wait_min = float(cfg.get("bsod_result_wait_min", 10) or 0)
                self._collect_bsod_results(analysis, dest, newer_than=prev_mtime,
                                           wait_s=wait_min * 60)
        analysis.mode = "bsod_submitted"
        analysis.ok = True
        if analysis.bsod.get("submitted"):
            analysis.error = ""
        else:
            analysis.error = (analysis.bsod.get("skipped")
                              or f"PotatoFarm submission failed: {analysis.bsod.get('detail')}")
        return analysis

    def _bsod_pending(self, analysis: CaseAnalysis, case_ctx, prev: list,
                      why: str) -> CaseAnalysis:
        """Nothing new to submit this round: report the last submission and
        pick up the parser result if it has landed in the folder since."""
        last = next((p_ for p_ in reversed(prev) if p_.get("attachments")),
                    prev[-1] if prev else {})
        analysis.mode = "bsod_pending"
        analysis.ok = True
        analysis.error = (f"dump(s) already submitted to PotatoFarm on "
                          f"{last.get('submitted_at', '?')} — {why}")
        with self._stage(analysis, "bsod_result"):
            dest = str(last.get("dumps_path") or "") or _bsod.share_case_dir(
                analysis.case_nbr, getattr(case_ctx, "backend_id", "") or "")
            analysis.bsod["dumps_path"] = dest
            self.progress("bsod_result", analysis.error)
            self._collect_bsod_results(analysis, dest, newer_than=0.0, wait_s=0)
        return analysis

    def _collect_bsod_results(self, analysis: CaseAnalysis, dest: str,
                              newer_than: float, wait_s: float) -> None:
        t0 = time.time()
        results = _bsod.wait_for_results(
            dest, newer_than, wait_s,
            progress=lambda msg: self.progress("bsod_result", msg)) if dest else []
        analysis.bsod["result_waited_s"] = int(time.time() - t0)
        analysis.bsod["results"] = results
        html = os.path.join(dest, _bsod.RESULT_HTML) if dest else ""
        analysis.bsod["result_html"] = html if html and os.path.exists(html) else ""
        if results:
            for r_ in results:
                self.progress("bsod_result",
                              f"{r_['dump']}: bugcheck 0x{r_['bugcheck_code']} "
                              f"{r_['bugcheck_str']}; bucket {r_['failure_bucket_id']}; "
                              f"Wi-Fi driver fault={r_['wifi_fault']} BT fault={r_['bt_fault']} "
                              f"on stack={r_['driver_on_stack']}")
            self.progress("bsod_result", _bsod.assessment(results))
        else:
            self.progress("bsod_result",
                          "no PotatoFarm result in the folder yet — the next round "
                          "picks it up (the parser also e-mails it)")

    def _start_echo_advice(self, analysis: CaseAnalysis, case_ctx) -> None:
        if not self.ECHO_ADVICE_ENABLED:
            return
        try:
            from .case_reader import _normalize_comments, _CUSTOMER_AUTHOR_PREFIXES
            from .echo_client import build_next_steps_question
            latest = ""
            for r_ in reversed(_normalize_comments(getattr(case_ctx, "comments", None))):
                if r_["author"].strip().lower().startswith(_CUSTOMER_AUTHOR_PREFIXES):
                    latest = r_["text"]
                    break
            question = build_next_steps_question(
                subject=analysis.subject, description=analysis.clean_description or analysis.description,
                domain=analysis.issue_domain, subcategory=analysis.issue_subcategory,
                issue_times=analysis.issue_times,
                missing_info=(analysis.case_reader or {}).get("missing_info"),
                attachments=getattr(case_ctx, "attachment_list", None),
                latest_customer_comment=latest, reader_next_action=analysis.next_action)
        except Exception as e:
            print(f"[handsfree.runner] echo advice question not built: {e}")
            return
        analysis.echo_advice = {"status": "asking", "question": question, "answer": "",
                                "error": "", "started_at": time.time(), "elapsed_s": 0}
        self._snap(echo_advice=dict(analysis.echo_advice))
        self.progress("read_case_history",
                      "asking the Echo knowledge base for next-step suggestions (in the background)")

        def _run(adv=analysis.echo_advice):
            from .echo_client import EchoUnavailable, ask_echo_kb
            try:
                adv["answer"] = str(ask_echo_kb(question, timeout=self.ECHO_ADVICE_TIMEOUT_S) or "")
                adv["status"] = "answered" if adv["answer"].strip() else "empty"
            except EchoUnavailable as e:
                adv["status"], adv["error"] = "unavailable", str(e)[:300]
            except Exception as e:
                adv["status"], adv["error"] = "unavailable", f"{type(e).__name__}: {e}"[:300]
            adv["elapsed_s"] = int(time.time() - adv["started_at"])
            self._snap(echo_advice=dict(adv))
            self.progress("read_case_history",
                          f"Echo next-step suggestions: {adv['status']}"
                          + (f" ({len(adv['answer'])} chars, {adv['elapsed_s']} s)"
                             if adv["status"] == "answered" else
                             (f" — {adv['error']}" if adv["error"] else "")))

        self._echo_thread = threading.Thread(target=_run, daemon=True,
                                             name="handsfree-echo-advice")
        self._echo_thread.start()

    def _join_echo_advice(self, analysis: CaseAnalysis) -> None:
        t, self._echo_thread = self._echo_thread, None
        if t is None:
            return
        if t.is_alive():
            self.progress("read_case_history",
                          "waiting for the Echo next-step suggestions before composing the draft")
            t.join(timeout=self.ECHO_ADVICE_JOIN_S)
        if t.is_alive() and analysis.echo_advice.get("status") == "asking":
            # Still no answer: the draft is composed without it; the thread
            # keeps running and the live panel shows the answer when it lands.
            analysis.echo_advice.update(
                note=f"no answer within {self.ECHO_ADVICE_JOIN_S} s — the draft was composed "
                     "without it; the live panel updates when Echo answers",
                elapsed_s=int(time.time() - analysis.echo_advice["started_at"]))
            self._snap(echo_advice=dict(analysis.echo_advice))
            self.progress("read_case_history",
                          f"Echo next-step suggestions not in within {self.ECHO_ADVICE_JOIN_S} s — "
                          "continuing; they show in the live panel when they arrive")

    def _assert_window(self, analysis: CaseAnalysis):
        from datetime import timedelta
        if getattr(analysis, "yb_case", False):
            return None          # YB / assert issue: every assert in the log counts
        dt = self._issue_datetime_in_log_frame(analysis)
        if dt is None:
            return None
        return (dt - timedelta(minutes=self.ASSERT_WINDOW_BEFORE_MIN),
                dt + timedelta(minutes=self.ASSERT_WINDOW_AFTER_MIN))

    @staticmethod
    def _alternate_etls(wifi_files: list, ddd_files: list, issue_times: list,
                        tried: set) -> list:
        """Other ETLs in the archive, closest capture-folder stamp first
        (WRT folders are named ..._DD-MM-YYYY_HH-MM-SS_...). Files whose
        folder carries no stamp go last."""
        from utils.issue_time_utils import parse_issue_time_string
        issue_dt = None
        for raw in issue_times or []:
            dt, time_only = parse_issue_time_string(str(raw))
            if dt is not None and not time_only:
                issue_dt = dt
                break
        stamped, unstamped = [], []
        for p in list(wifi_files or []) + list(ddd_files or []):
            if p in tried:
                continue
            ts = _folder_timestamp(p)
            if ts is None or issue_dt is None:
                unstamped.append(p)
            else:
                stamped.append((abs((ts - issue_dt).total_seconds()), p))
        stamped.sort(key=lambda x: x[0])
        return [p for _, p in stamped] + unstamped

    # ------------------------------------------------------------------
    def _pick_bt_etl(self, bt_files: list) -> Optional[str]:
        """Pick the newest BT capture, matching the UI's folder-time rule."""
        if not bt_files:
            return None
        try:
            from blueprints.bt_chatbot.bt_chatbot_routes import _parse_path_timestamp
            dated = [(path, _parse_path_timestamp(path)) for path in bt_files]
            dated = [(path, ts) for path, ts in dated if ts is not None]
            if dated:
                return max(dated, key=lambda item: item[1])[0]
        except Exception as e:
            print(f"[handsfree.runner] BT capture-time pick failed: {e}")
        return sorted(bt_files)[-1]

    def _pick_etl(self, wifi_files: list, ddd_files: list,
                  issue_time_str: str) -> tuple:
        """AI-time pick first; fallback = newest DDD, then newest Wi-Fi ETL.
        (Deliberately NOT get_auto_analysis_etl — that reads the Flask session.)
        Returns (path, rule, issue_time_used): rule is
        closest_at_or_after_issue | newest_ddd | newest_wifi | none.

        The reader copies the issue time "exactly as written" ("16:31/12/13/2025",
        "2026-1-14-13-54-47"); pick_etl_by_ai_time only parses MM/DD/YYYY-HH:MM:SS
        / ISO / HH:MM:SS, so the value is normalized first — otherwise the
        pick silently fell back to the newest capture."""
        from utils.issue_time_ai import pick_etl_by_ai_time
        from utils.issue_time_utils import format_issue_time, parse_issue_time_string

        used = ""
        if issue_time_str:
            dt, time_only = parse_issue_time_string(str(issue_time_str))
            if dt is not None:
                used = dt.strftime("%H:%M:%S") if time_only else format_issue_time(dt, with_ms=False)
        file_dicts = {"wifi_dict": {"zip": list(wifi_files or [])},
                      "ddd_dict": {"zip": list(ddd_files or [])}}
        if used:
            try:
                picked = pick_etl_by_ai_time(file_dicts, used)
                if picked:
                    return picked, "closest_at_or_after_issue", used
            except Exception as e:
                print(f"[handsfree.runner] AI-time ETL pick failed: {e}")

        def _newest(paths: list) -> Optional[str]:
            import re as _re
            etls = [p for p in (paths or [])
                    if str(p).lower().endswith(".etl")
                    or _re.search(r"\.etl\.\d+$", str(p), _re.IGNORECASE)]
            if not etls:
                return None
            try:
                from utils.etl_utils import extract_timestamp_from_folder
                def _key(p):
                    ts = extract_timestamp_from_folder(str(p))
                    return (ts is not None, ts, str(p))
                return max(etls, key=_key)
            except Exception:
                return sorted(etls)[-1]

        picked = _newest(ddd_files)
        if picked:
            return picked, "newest_ddd", used
        picked = _newest(wifi_files)
        return picked, ("newest_wifi" if picked else "none"), used

    def _decode_etl(self, etl_path: str) -> None:
        """Run the WPP/DDD decoder headlessly.

        parse_single_binary honors AVATAR_HEADLESS_DECODE=1 to skip opening
        TextAnalysisTool + Explorer. SystemExit (the decoder's failure exits)
        is caught so it cannot kill the app process.
        """
        prev = os.environ.get("AVATAR_HEADLESS_DECODE")
        os.environ["AVATAR_HEADLESS_DECODE"] = "1"
        try:
            from services.etl_parser.wpp_ddd_parser import wpp_ddd_parser_run
            wpp_ddd_parser_run(etl_path)
        except SystemExit as e:
            raise RuntimeError(f"decoder aborted (sys.exit {e.code})") from None
        finally:
            if prev is None:
                os.environ.pop("AVATAR_HEADLESS_DECODE", None)
            else:
                os.environ["AVATAR_HEADLESS_DECODE"] = prev

    def _decode_bt(self, etl_path: str) -> Optional[str]:
        """Decode one BT ETL with the existing headless CLI decoder."""
        from services.etl_parser.bt_parser import bt_decode_via_cli
        return bt_decode_via_cli(
            os.path.dirname(etl_path), etl_path,
            emit_callback=lambda message: self.progress("decode_bt", message),
        )

    def _build_agent(self, domain: str = "wifi"):
        """Fresh agent per case; borrows client/model/skills (and the ACE
        runner, so learned playbooks apply) from the boot-time base agent."""
        from configs.global_configs import app_config
        from services.log_chatbot_service import WifiLogAgentSystem

        base = getattr(app_config, "log_chatbot_agent", None)
        llm = app_config.llm_helper
        skills = getattr(base, "skills", None) or getattr(llm, "skills", None) or {}
        agent_cls = WifiLogAgentSystem
        if domain == "bt":
            from services.bt_chatbot_service import BtLogAgentSystem
            agent_cls = BtLogAgentSystem
        agent = agent_cls(client=llm.client,
                          model=getattr(llm, "model", "gpt-4.1"),
                          skills=skills)
        ace_runner = getattr(base, "ace_runner", None)
        if ace_runner is not None:
            agent.attach_ace(ace_runner)
        return agent

    def _run_agent(self, analysis: CaseAnalysis, *, max_steps: int) -> None:
        agent = self._build_agent(analysis.wifi_or_bt)
        agent.current_log_path = analysis.log_path
        env_brief = _env_brief(analysis.env_detail)
        primed_description = (analysis.clean_description
                              + (f"\n[Environment] {env_brief}" if env_brief else ""))
        agent.prime_with_context(
            case_nbr=analysis.case_nbr,
            subject=analysis.subject,
            description=primed_description,
            issue_type=analysis.issue_type,
            attachment_time=analysis.attachment_time,
        )

        # The log's clock (decoder host, GMT+8) vs the customer's: the agent's
        # PreScan window must sit on the reported moment AS WRITTEN IN THE LOG.
        log_first = log_last = None
        customer_tz = ""
        if analysis.wifi_or_bt == "wifi" and analysis.log_path:
            try:
                from utils.issue_time_utils import read_log_time_range
                log_first, log_last = read_log_time_range(analysis.log_path)
                customer_tz = self._customer_tz(analysis.log_path)
            except Exception as e:
                print(f"[handsfree.runner] log time range unavailable: {e}")

        times = analysis.issue_times or [""]
        sent_all: list[dict] = []
        agent_results: list[dict] = []
        for idx, t in enumerate(times):
            inc = IncidentReport(issue_time=t)
            captured: list[dict] = []
            live_steps: list[dict] = []
            live_head = {"incident": t, "index": idx + 1, "total": len(times),
                         "status": "running"}

            def _capture(step, _cap=captured, _live=live_steps, _head=live_head):
                if not isinstance(step, dict):
                    return
                if len(_cap) < _MAX_STEPS_KEPT:
                    _cap.append({
                        "role": str(step.get("role") or ""),
                        "content": str(step.get("content") or "")[:_STEP_CONTENT_CAP],
                    })
                # Live panel: the last steps as they happen (bounded).
                _live.append(_compact_step(step))
                del _live[:-40]
                self._snap(agent_live=dict(_head, steps=list(_live), result=None))

            # Mirror the UI: per-incident issue-time override on the agent —
            # in the LOG's clock when the reported time maps inside the log
            # (the same alignment the coverage check accepted), else as before.
            if t:
                try:
                    from utils.issue_time_utils import (format_issue_time,
                                                        parse_issue_time_string,
                                                        resolve_issue_time)
                    align = None
                    if log_first is not None and log_last is not None:
                        align = self._align_issue_time(t, analysis.log_path,
                                                       log_first, log_last)
                    if align and align["inside"]:
                        agent.issue_time = align["log"]
                        for attr, val in (("_issue_time_time_only", False),
                                          ("issue_time_customer", align["customer"]),
                                          ("issue_time_tz", customer_tz)):
                            if hasattr(agent, attr):
                                setattr(agent, attr, val)
                        inc.issue_time_customer = format_issue_time(align["customer"], with_ms=False)
                        inc.issue_time_log = format_issue_time(align["log"], with_ms=False)
                        inc.time_anchor = align["anchor"]
                        if align["anchor"] != ANCHOR_AS_IS:
                            self.progress(
                                "agent_analysis",
                                f"issue time {inc.issue_time_customer} (customer) = "
                                f"{inc.issue_time_log} (log, "
                                f"{_fmt_offset(align['log'] - align['customer'])} via "
                                f"{ANCHOR_LABELS[align['anchor']]})")
                    else:
                        dt, _time_only = parse_issue_time_string(t)
                        if dt is not None:
                            resolved, _src = resolve_issue_time(t, analysis.log_path)
                            agent.issue_time = resolved or dt
                except Exception as e:
                    print(f"[handsfree.runner] issue-time override failed ({t}): {e}")

            inc.user_message = (
                f"{analysis.clean_description} at around {t}" if t
                else analysis.clean_description
            )
            inc.sent_for_analysis = _sent_for_analysis(
                inc, agent, primed_description, customer_tz,
                analysis.log_path, log_first, log_last)
            sent_all.append(inc.sent_for_analysis)
            self._snap(sent_for_analysis=list(sent_all))
            self.progress("agent_analysis", f"incident @ {t or 'unspecified time'}")
            try:
                res = agent.chat(inc.user_message, use_tools=True,
                                 max_steps=max_steps, step_callback=_capture)
                inc.result_type = (res or {}).get("type", "") if isinstance(res, dict) else ""
                data = (res or {}).get("data") if isinstance(res, dict) else None
                if inc.result_type in ("report", "partial_report") and isinstance(data, dict):
                    inc.report = data
                    parts = [str(data.get("root_cause_summary") or "")]
                    if data.get("markdown_summary"):
                        parts.append(str(data["markdown_summary"]))
                    inc.report_text = "\n".join(p for p in parts if p)
                elif data is not None:
                    inc.report_text = data if isinstance(data, str) else json.dumps(data, default=str)
            except Exception as e:
                inc.result_type = "error"
                inc.error = f"{type(e).__name__}: {e}"
                print(f"[handsfree.runner] agent run failed:\n{traceback.format_exc()}")
            inc.steps = captured
            analysis.incidents.append(inc)
            summary = _result_summary(inc)
            agent_results.append(summary)
            self._snap(agent_live=dict(live_head, status=inc.result_type or "error",
                                       steps=list(live_steps), result=summary),
                       agent_results=list(agent_results))
