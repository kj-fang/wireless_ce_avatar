from flask import Blueprint, render_template, request, session, jsonify, Response, copy_current_request_context, redirect, url_for
import json
import re
import traceback
import uuid
import os
import threading
from datetime import datetime
import tkinter as tk
from tkinter import filedialog

from configs.global_configs import app_config
from models.models import CaseContext
from services.bt_chatbot_service import BtLogAgentSystem, WifiLogAgentSystem, load_skills_from_data_dir, get_builtin_skills, build_skill_file_map, load_skills_from_yaml
from utils.etl_utils import extract_time_from_description
from utils.issue_time_utils import (
    parse_issue_time_string,
    read_log_time_range,
    resolve_issue_time,
    format_issue_time,
)
from utils.issue_time_ai import build_issue_time_suggestions, organize_issue_context, realign_times_to_log, find_nearest_event_error
from services import feedback_service

bt_chatbot_bp = Blueprint("bt_chatbot", __name__, url_prefix="/bt_chatbot")

# Server-side store: session_id -> WifiLogAgentSystem instance
_chatbot_instances: dict = {}

# File names recognised as System Event logs (case-insensitive comparison)
_EVT_FILENAMES = {"raweventviewersystemlogs.evt", "system.evtx"}

# Upper bound on System-Event-Log rows pulled to anchor an AI issue-time
# suggestion. build_event_log_digest keeps at most 40 rows (by severity) and
# find_nearest_event_error only needs a representative pool, so this cap keeps
# /suggest_issue_times fast and bounded even on very large .evtx captures.
_EVENT_ANCHOR_MAX = 500


def _find_evt_path_for_log(log_path: str) -> str:
    """Return the path to a System Event log file associated with a BT log.

    A case can contain several capture folders, each with its own
    ``rawEventViewerSystemLogs.evt`` / ``System.evtx``. We must return the evt
    that belongs to the SAME capture folder the user opened the chatbot from —
    not just the first one we stumble on — otherwise the event log shown in the
    chatbot panel comes from a different (e.g. older) capture than the BT log.

    Strategy:
      1. Collect every evt candidate from the case download results ('ddd'
         dict), then pick the one whose folder shares the DEEPEST path with the
         BT log's folder (same capture folder wins; nearest sibling otherwise).
      2. Fallback: search relative to the given BT log path:
         - the log's own directory
         - grandparent dir for rawEventViewerSystemLogs.evt
         - sibling 'Event logs/' folder for System.evtx
    Returns empty string if nothing found.
    """
    log_dir = os.path.dirname(os.path.abspath(log_path)) if log_path else ""

    def _common_len(evt_path: str) -> int:
        """Length of the shared directory prefix between the BT log and an evt
        candidate. Higher means the evt sits closer to (ideally in the same
        folder as) the BT log the user opened."""
        if not log_dir:
            return -1
        try:
            common = os.path.commonpath(
                [log_dir, os.path.dirname(os.path.abspath(evt_path))]
            )
            return len(common)
        except ValueError:
            # Different drives -> no shared path
            return -1

    # --- Strategy 1: from download results, choose the closest folder ---
    case_ctx = session.get("case_context", {})
    case_nbr = case_ctx.get("case_nbr", "") if isinstance(case_ctx, dict) else ""
    if case_nbr:
        results = app_config.get_download_results(case_nbr)
        ddd_dict = results.get("ddd", {})
        candidates = [
            fpath
            for file_list in ddd_dict.values()
            for fpath in file_list
            if os.path.basename(fpath).lower() in _EVT_FILENAMES
            and os.path.isfile(fpath)
        ]
        if candidates:
            # Prefer the evt sharing the deepest folder with the BT log. On
            # Windows ANY two paths on the same drive share the drive root
            # (e.g. 'C:\\'), so a positive common length alone is meaningless
            # and would wrongly trust the heuristic. Only accept the proximity
            # match when the shared prefix goes DEEPER than the drive root;
            # otherwise fall back to the first candidate (previous behaviour).
            best = max(candidates, key=_common_len)
            if log_dir:
                try:
                    best_common = os.path.commonpath(
                        [log_dir, os.path.dirname(os.path.abspath(best))]
                    )
                    drive_root = os.path.splitdrive(log_dir)[0] + os.sep
                    if os.path.normcase(best_common) != os.path.normcase(drive_root):
                        return best
                except ValueError:
                    pass
            return candidates[0]

    # --- Strategy 2: relative path search from BT log ---
    if not log_path or not os.path.isfile(log_path):
        return ""

    # rawEventViewerSystemLogs.evt / System.evtx in the log's OWN directory
    if os.path.isdir(log_dir):
        for fname in os.listdir(log_dir):
            if fname.lower() in _EVT_FILENAMES:
                candidate = os.path.join(log_dir, fname)
                if os.path.isfile(candidate):
                    return candidate

    # rawEventViewerSystemLogs.evt in grandparent directory
    grandparent = os.path.dirname(os.path.dirname(log_dir))
    if os.path.isdir(grandparent):
        for fname in os.listdir(grandparent):
            if fname.lower() in _EVT_FILENAMES:
                candidate = os.path.join(grandparent, fname)
                if os.path.isfile(candidate):
                    return candidate

    # System.evtx in "Event logs" subfolder of log's directory
    event_logs_dir = os.path.join(log_dir, "Event logs")
    if os.path.isdir(event_logs_dir):
        for fname in os.listdir(event_logs_dir):
            if fname.lower() in _EVT_FILENAMES:
                candidate = os.path.join(event_logs_dir, fname)
                if os.path.isfile(candidate):
                    return candidate

    return ""


# ------------------------------------------------------------------
# Feedback sidecar helpers (anonymous, side-car, never blocks chat)
# ------------------------------------------------------------------
def _ensure_feedback_conversation_id(*, rotate: bool = False) -> str:
    """
    Return the current feedback conversation_id, creating one if missing
    or if `rotate=True` (e.g. on set_log / prepare — a new log = new case).
    Stored in Flask session so it persists across requests.
    """
    if rotate or not session.get("feedback_conversation_id"):
        session["feedback_conversation_id"] = str(uuid.uuid4())
    return session["feedback_conversation_id"]


def _extract_issue_context() -> dict:
    """
    Consolidate issue context from session into a single dict with keys:
      case_nbr, subject, description, issue_type

    Sources (in priority order):
      - session['ai_ips_analysis']  : LLM-generated analysis dict (richest)
      - session['classification']   : issue_type + confidence
      - session['case_context']     : raw Salesforce case fields
    """
    # --- raw case fields ---
    raw_ctx = session.get("case_context", {})
    ctx = CaseContext.from_session(raw_ctx) if raw_ctx else CaseContext()

    # --- classification ---
    classification = session.get("classification", {})
    issue_type = (classification.get("issue_type", "")
                  if isinstance(classification, dict) else "")

    # --- ai_ips_analysis: LLM-generated structured summary ---
    ai_analysis = session.get("ai_ips_analysis", {})
    if not isinstance(ai_analysis, dict):
        ai_analysis = {}

    # Build a rich description: start with the LLM summary if available,
    # fall back to the raw Salesforce description.
    description_parts = []
    if ctx.description:
        description_parts.append(ctx.description)

    # Append key fields from the LLM analysis (skip Classification sub-dict
    # and nested dicts that are not human-readable strings)
    for key, val in ai_analysis.items():
        if key.lower() == "classification":
            continue
        if isinstance(val, str) and val.strip():
            description_parts.append(f"{key}: {val.strip()}")
        elif isinstance(val, list):
            flat = "; ".join(str(v) for v in val if v)
            if flat:
                description_parts.append(f"{key}: {flat}")

    attachment_time = ""

    # Return cached value if already computed this session (avoids re-parsing on every request)
    cached = session.get("_attachment_time_cache")
    if cached is not None:
        attachment_time = cached
    else:
        def _desc_time_to_str(desc: str) -> str:
            """Parse issue time from attachment gray subtitle text and normalize to MM/DD/YYYY-HH:MM:SS."""
            parsed = extract_time_from_description(desc)
            if hasattr(parsed, 'strftime'):
                return parsed.strftime('%m/%d/%Y-%H:%M:%S')
            if isinstance(parsed, str) and parsed.strip():
                # time-only case: keep as HH:MM:SS so agent can still apply segment2 on log date.
                return parsed.strip()
            return ""

        # Step 1: Get the list of selected file names
        selected_files = session.get("selected_files", [])
        selected_names = set()
        for sf in selected_files:
            if isinstance(sf, (list, tuple)) and len(sf) >= 1:
                selected_names.add(sf[0])

        # Step 2: Read from case_context.attachment_list (same data source as the template).
        # Heavy fields like attachment_list are stashed on disk for big
        # cases — go through from_session() so the sidecar is loaded.
        raw_ctx_dict = session.get("case_context", {})
        if isinstance(raw_ctx_dict, dict) and raw_ctx_dict:
            raw_ctx_dict = CaseContext.from_session(raw_ctx_dict).to_dict()
        att_list = raw_ctx_dict.get("attachment_list", []) if isinstance(raw_ctx_dict, dict) else []

        # Step 3: Prefer user-selected attachments; if selected_names is empty, take the first one
        candidates = [item for item in att_list
                      if isinstance(item, (list, tuple)) and len(item) >= 3
                      and (not selected_names or item[0] in selected_names)]
        if not candidates:
            candidates = [item for item in att_list if isinstance(item, (list, tuple)) and len(item) >= 3]

        # Step 4 (PRIMARY): parse from gray subtitle description (item[2][1])
        for item in candidates:
            desc_raw = item[2][1] if isinstance(item[2], (list, tuple)) and len(item[2]) >= 2 else None
            result = _desc_time_to_str(desc_raw)
            if result:
                attachment_time = result
                print(f"[DEBUG] attachment_time from attachment description['{item[0]}']: {attachment_time}")
                break

        # Step 5: Fallback — try directly from selected_files
        if not attachment_time:
            for file_info in selected_files:
                if isinstance(file_info, (list, tuple)) and len(file_info) >= 3:
                    desc_raw = file_info[2][1] if isinstance(file_info[2], (list, tuple)) and len(file_info[2]) >= 2 else None
                    result = _desc_time_to_str(desc_raw)
                    if result:
                        attachment_time = result
                        print(f"[DEBUG] attachment_time from selected_files description['{file_info[0]}']: {attachment_time}")
                        break

        if not attachment_time:
            print(f"[DEBUG] attachment_time: NOT FOUND. selected_names={selected_names}, att_list len={len(att_list)}")

        # Cache in session so subsequent requests in the same flow skip re-parsing
        session["_attachment_time_cache"] = attachment_time

    return {
        "case_nbr":    ctx.case_nbr or "",
        "subject":     ctx.subject or "",
        "description": "\n".join(description_parts),
        "issue_type":  issue_type,
        "attachment_time": attachment_time,
    }


def _resolved_issue_time_for(log_path: str, attachment_time: str) -> str:
    """
    Session-level cache for the canonical sidebar-prefill issue_time.
    Mirrors `_attachment_time_cache`: keyed by log_path so reloading a
    different log naturally invalidates. Lets /get_issue_context return
    the same value prime_with_context resolved without re-reading the
    log file's first/last timestamps every time.
    """
    cache = session.get("_resolved_issue_time_cache") or {}
    cache_key = log_path or "__nolog__"
    if cache_key in cache:
        return cache[cache_key]
    dt, _ = resolve_issue_time(attachment_time, log_path)
    formatted = format_issue_time(dt)
    cache[cache_key] = formatted
    session["_resolved_issue_time_cache"] = cache
    return formatted


def _extract_disconnect_time(*text_sources: str) -> str:
    """
    Search multiple text sources for the most precise disconnect/event
    timestamp.  Returns a string like ' at around 10/28/2025-11:25:49'
    or '' if nothing found.

    Tries several common formats:
      MM/DD/YYYY-HH:MM:SS(.mmm)
      MM/DD/YYYY HH:MM:SS
      YYYY-MM-DD HH:MM:SS
      YYYY/MM/DD HH:MM:SS
    """
    patterns = [
        r'(\d{1,2}/\d{1,2}/\d{4}[\s-]\d{1,2}:\d{2}:\d{2}(?:\.\d{1,3})?)',
        r'(\d{4}[-/]\d{1,2}[-/]\d{1,2}[\sT]\d{1,2}:\d{2}:\d{2})',
    ]
    for src in text_sources:
        if not src:
            continue
        for pat in patterns:
            m = re.search(pat, src)
            if m:
                return f" at around {m.group(1)}"
    return ""


def _compose_concise_description(ctx: dict = None) -> str:
    """
    Auto-compose the most effective issue description for auto-analysis via chat.

        Format: "<problem statement> <timestamp>"
        e.g. "6G Weak Signal disconnected at around 10/28/2025-11:25:49"
    """
    try:
        if ctx is None:
            ctx = _extract_issue_context()
    except Exception:
        return "Perform full multi-skill log analysis"

    subject = ctx.get("subject", "")
    desc_raw = ctx.get("description", "")
    attachment_time = ctx.get("attachment_time", "")
    # Prefer attachment time from selected file; fall back to text extraction
    if attachment_time:
        time_hint = f" at around {attachment_time}"
    else:
        time_hint = _extract_disconnect_time(subject, desc_raw)

    # Best: clean subject line — strip ALL leading [tag] groups
    if subject:
        clean = re.sub(r'^(\[.*?\]\s*)+', '', subject).strip()    # remove ALL [xxx] tags
        clean = re.sub(r'\s*\(F/R.*?\)\s*$', '', clean).strip()   # remove (F/R：1/1u,40/200C)
        if clean:
            return f"{clean}{time_hint}"

    # Last resort: first 200 chars of description
    if desc_raw:
        return desc_raw[:200].strip() + time_hint

    return "Perform full multi-skill log analysis"


def _get_or_create_agent(skip_prime: bool = False) -> WifiLogAgentSystem:
    """
    Return a per-session WifiLogAgentSystem.
    Borrows client/model from app_config.bt_chatbot_agent which is
    initialised at app startup (set_up_app.py -> set_up()).

    skip_prime: if True, skip the auto prime_with_context on new session creation.
                Use this when the caller will immediately call prime_with_context itself.
    """
    sid = session.get("chatbot_session_id")
    if not sid or sid not in _chatbot_instances:
        sid = str(uuid.uuid4())
        session["chatbot_session_id"] = sid

    if sid not in _chatbot_instances:
        base = app_config.bt_chatbot_agent
        if base is None:
            # Fallback: try to build from llm_helper directly
            llm_helper = app_config.llm_helper
            if llm_helper is None or llm_helper.client is None:
                raise RuntimeError(
                    "Log Chatbot Agent is not available. "
                    "The app may not have an API key configured."
                )
            base = BtLogAgentSystem(
                client=llm_helper.client,
                model=getattr(llm_helper, "model", "gpt-4.1"),
                skills=getattr(llm_helper, "skills", None),
            )
        # Create a fresh per-session instance sharing the same client + skills.
        # Use type(base) so BtLogAgentSystem overrides (SCOPE_FULL_LOG_WHEN_EMPTY,
        # empty DRIVER_ADD_MARKER/RESET_MARKER) are preserved in the clone.
        agent = type(base)(
            client=base.client,
            model=base.model,
            skills=base.skills,   # reuse pre-loaded skills, no disk re-read
        )
        # Auto-populate log path so a freshly-(re)created per-session agent
        # still knows which log to use. Two sources, in order:
        #   1. session["chatbot_log_path"] — set by set_log when the user
        #      loads a BT log DIRECTLY (this is the only record for that flow;
        #      set_log does NOT touch app_config.last_analyzed_log_path).
        #   2. app_config.last_analyzed_log_path — the LogParser→chatbot
        #      (Wi-Fi) hand-off path.
        # Restoring from the session is what lets the agent survive an
        # in-memory _chatbot_instances wipe (e.g. Flask debug auto-reload or
        # a worker restart): without it a directly-loaded BT log produced
        # "No log file loaded" on the next /chat even though the user had
        # already set it. Wi-Fi didn't hit this because it had the app_config
        # fallback.
        restored_log = (session.get("chatbot_log_path")
                        or app_config.last_analyzed_log_path or "")
        if restored_log:
            agent.current_log_path = restored_log

        # Prime with session issue context so every new session is context-aware
        # (skipped when caller will immediately call prime_with_context itself)
        if not skip_prime:
            try:
                ctx = _extract_issue_context()
                if any(ctx.values()):
                    agent.prime_with_context(**ctx)
            except Exception:
                pass  # session may not have case context (standalone chatbot)
        _chatbot_instances[sid] = agent

    return _chatbot_instances[sid]


# ------------------------------------------------------------------
# Pages
# ------------------------------------------------------------------
@bt_chatbot_bp.route("/", methods=["GET"])
def index():
    suggested_log = app_config.last_analyzed_log_path or ""
    issue_desc = ""
    try:
        ctx = _extract_issue_context()
        issue_desc = ctx.get("description", "")
    except Exception:
        ctx = {}

    # Pre-warm the issue-AI cache BEFORE rendering so the page's
    # /get_issue_context AJAX hits a hot cache and returns instantly —
    # otherwise it does a synchronous LLM organize on page load and the
    # sidebar "spins" while the user waits.
    #
    # In the normal BT button flow prepare() already warmed _issue_ai_quick,
    # so this is a cheap cache hit. It only does real work when the page is
    # reached WITHOUT going through prepare() (direct nav, browser back, or a
    # stale last_analyzed_log_path) — exactly the path that was slow. The LLM
    # wait (if any) now happens during the page-render request (browser shows
    # its native loading bar) instead of as a post-load spinner. Best-effort:
    # never let a warm failure block the page.
    try:
        if suggested_log and not session.get("_issue_ai_quick"):
            _first_ts, _last_ts = read_log_time_range(suggested_log)
            _issue_context_organized(ctx.get("description", "") or "", _first_ts, _last_ts)
    except Exception as _warm_err:
        print(f"⚠️ BT chatbot index pre-warm skipped: {_warm_err}")

    return render_template("bt_chatbot.html", suggested_log=suggested_log, issue_description=issue_desc)


# ------------------------------------------------------------------
# API: open native file browser and return selected path
# ------------------------------------------------------------------
@bt_chatbot_bp.route("/browse", methods=["GET"])
def browse():
    """Open a native Windows file dialog and return the selected .hci.txt path."""
    result = {"path": ""}

    def _open_dialog():
        root = tk.Tk()
        root.withdraw()
        root.wm_attributes("-topmost", True)
        path = filedialog.askopenfilename(
            title="Select log file",
            filetypes=[("hci.txt files", "*.hci.txt"), ("All files", "*.*")],
        )
        root.destroy()
        result["path"] = path or ""

    # tkinter must run on the main thread on Windows;
    # since Flask dev server is single-threaded this is fine,
    # but we guard with a threading.Event to make it safe.
    t = threading.Thread(target=_open_dialog)
    t.start()
    t.join(timeout=60)

    return jsonify({"success": True, "path": result["path"]})


# ------------------------------------------------------------------
# API: set log file path
# ------------------------------------------------------------------
@bt_chatbot_bp.route("/set_log", methods=["POST"])
def set_log():
    data = request.get_json(silent=True) or {}
    log_path = data.get("log_path", "").strip()
    if not log_path:
        return jsonify({"success": False, "error": "log_path is required"}), 400

    try:
        # Capture the previous log_path + conv_id BEFORE we rotate, so the
        # client can show a "log switched, chat cleared" toast and offer
        # undo within a short window. `rotated` is True only when this
        # genuinely replaces a different log (not the first load).
        prev_log_path = (session.get("chatbot_log_path") or "").strip()
        rotated = bool(prev_log_path) and prev_log_path != log_path
        prev_conv_id = (session.get("feedback_conversation_id") or "") if rotated else ""

        agent = _get_or_create_agent(skip_prime=True)
        agent.current_log_path = log_path
        agent.reset_conversation()          # fresh conversation for a new file
        ctx = _extract_issue_context()      # re-extract context in case session was updated after agent creation
        # Always prime: prime_with_context falls back to the log file's latest
        # timestamp when ctx has no usable issue time, so the sidebar always
        # gets an issue_time to display (covers the no-session entry path).
        agent.prime_with_context(**ctx)

        session["chatbot_log_path"] = log_path

        # Sidecar: a new log file = a new conversation. Rotate the id and
        # eagerly create the snapshot file so issue context is captured even
        # if the user never sends a message.
        new_conv_id = _ensure_feedback_conversation_id(rotate=True)
        feedback_service.ensure_conversation(
            conversation_id=new_conv_id,
            session_id=session.get("chatbot_session_id", ""),
            issue=ctx,
            log_path=log_path,
            domain="bt",
        )

        # Whole-minute span of the log so the sidebar can cap the
        # issue-time capture window at the log's actual length. 0 means
        # "unknown" (no parseable timestamps) — client falls back to a
        # generic cap.
        try:
            log_span_minutes = agent.get_log_span_minutes()
        except Exception:
            log_span_minutes = 0

        # Log's last parseable timestamp — offered in the "no issue time" prompt
        # as a one-click anchor ("Use log's last time").
        try:
            _first_ts, _last_ts = read_log_time_range(log_path)
            log_last_time = format_issue_time(_last_ts) if _last_ts else ""
        except Exception:
            log_last_time = ""

        # Whether this log carries dates. Time-only logs (e.g. DDD) let the
        # sidebar leave the date fields blank and match Segment2 by time-of-day.
        try:
            log_has_date = agent._log_has_date()
        except Exception:
            log_has_date = True

        # Locate associated System Event log (.evtx / .evt)
        try:
            evtx_path = _find_evt_path_for_log(log_path)
        except Exception:
            evtx_path = ""

        return jsonify({
            "success": True,
            "message": f"Log file set: {log_path}",
            "skills": agent.get_skill_descriptions(),
            "issue_time": format_issue_time(agent.issue_time),
            "log_span_minutes": log_span_minutes,
            "log_last_time": log_last_time,
            "log_has_date": log_has_date,
            "evtx_path": evtx_path,
            # Hints for the client to clear chat history + show the toast.
            "rotated": rotated,
            "previous_log_path": prev_log_path if rotated else "",
            "previous_conversation_id": prev_conv_id if rotated else "",
            "new_conversation_id": new_conv_id,
        })
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


# ------------------------------------------------------------------
# API: suggest issue time(s) via LLM
# ------------------------------------------------------------------
@bt_chatbot_bp.route("/suggest_issue_times", methods=["POST"])
def suggest_issue_times():
    """Suggest issue time(s) from the user's typed description + a rough browse
    of the loaded log. User-first (explicit times bypass the LLM). The frontend
    must obtain the user's consent before calling this route. The heavy lifting
    lives in ``utils.issue_time_ai``; this handler just marshals request/agent
    state in and jsonifies the result out."""
    data = request.get_json(silent=True) or {}
    text = (data.get("text") or "").strip()
    # The page's event-log dropdowns are forwarded so the AI uses the SAME
    # Warn+Err selection the user sees (level defaults to 'warning_error',
    # source defaults to 'all' so we don't silently exclude the relevant bus).
    source_filter = str(data.get("source_filter") or "all").strip() or "all"
    level_filter = str(data.get("level_filter") or "warning_error").strip() or "warning_error"
    try:
        agent = _get_or_create_agent()
        log_path = agent.current_log_path or ""
        first_ts, last_ts = read_log_time_range(log_path) if log_path else (None, None)

        # Cached raw log lines feed the rough-browse digest (best-effort).
        log_lines = []
        try:
            if not agent._ensure_raw_log_cache():
                log_lines = agent._raw_log_cache or []
        except Exception:
            log_lines = []

        # High-priority anchor: the loaded capture's System Event Log
        # Warning/Error rows. On huge BT logs the rough raw-log browse alone
        # is imprecise, so these pre-filtered fault entries strongly anchor
        # the issue time. Best-effort — any failure just omits the section.
        event_log_events = []
        try:
            evtx_path = _find_evt_path_for_log(log_path) if log_path else ""
            if evtx_path:
                from services import event_log_service
                # Bound the pull so a huge .evtx can't balloon latency/memory:
                # build_event_log_digest keeps at most 40 rows (picked by
                # severity) and find_nearest_event_error only needs a
                # representative pool, so a generous cap is plenty while
                # staying safe on very large captures.
                page = event_log_service.get_paged_events(
                    evtx_path, offset=0, limit=_EVENT_ANCHOR_MAX,
                    source_filter=source_filter, level_filter=level_filter,
                )
                event_log_events = page.get("events", []) if isinstance(page, dict) else []
        except Exception as _evt_err:
            print(f"⚠️ BT issue-time event-log anchor skipped: {_evt_err}")
            event_log_events = []

        # An empty description is allowed — the AI can still infer the issue
        # time from the log alone (a description just improves accuracy). Only
        # block when there's truly nothing to analyze (no text AND no log).
        if not text and not log_lines:
            return jsonify({"success": False,
                            "error": "Type a problem description or load a log first."}), 400

        payload = build_issue_time_suggestions(
            text=text,
            log_lines=log_lines,
            first_ts=first_ts,
            last_ts=last_ts,
            llm_client=getattr(agent, "client", None),
            llm_model=getattr(agent, "model", None),
            event_log_events=event_log_events,
        )

        # Link the sidebar refine picker to the SAME events the AI used:
        # attach the nearest Error/Critical event to each AI suggestion so the
        # frontend can drive "Found a nearby system error" WITHOUT a second
        # /parse_event_log fetch. Skipped for user-explicit times and undated
        # (time-only) suggestions where a date-based distance is meaningless.
        if (event_log_events and isinstance(payload, dict)
                and not payload.get("user_explicit")):
            for s in payload.get("suggestions", []) or []:
                try:
                    sdt, _ = parse_issue_time_string((s.get("issue_time") or "").strip())
                    if sdt and sdt.year >= 2000:
                        ne = find_nearest_event_error(event_log_events, sdt)
                        if ne:
                            s["nearest_error"] = ne
                except Exception:
                    pass

        return jsonify(payload), (200 if payload.get("success") else 503)
    except Exception as e:
        traceback.print_exc()
        return jsonify({"success": False, "error": str(e)}), 500


# ------------------------------------------------------------------
# API: chat
# ------------------------------------------------------------------
@bt_chatbot_bp.route("/chat", methods=["POST"])
def chat():
    data = request.get_json(silent=True) or {}
    user_message = (data.get("message") or "").strip()
    if not user_message:
        return jsonify({"success": False, "error": "message is required"}), 400

    mode = str(data.get("mode", "tools")).strip().lower()
    if mode not in ("simple", "tools"):
        mode = "tools"

    try:
        temperature = float(data.get("temperature", 0.2))
    except Exception:
        temperature = 0.2
    temperature = max(0.0, min(1.0, temperature))

    try:
        max_tokens = int(data.get("max_tokens", 4000))
    except Exception:
        max_tokens = 4000
    max_tokens = max(256, min(8000, max_tokens))

    try:
        max_steps = int(data.get("max_steps", 6))
    except Exception:
        max_steps = 6
    max_steps = max(1, min(12, max_steps))

    # Parent-message id: client-generated UUID stamped on every iteration
    # of a single Send click. When the user types one message that yields
    # multiple incident analyses (multi-time chained calls), every
    # resulting turn shares this id, so downstream ETL can recover the
    # co-firing relationship from the bronze layer.
    parent_message_id = (data.get("parent_message_id") or "").strip()

    # Issue-time window (minutes before/after issue_time captured for the
    # Segment2 log slice). Sidebar-adjustable; default ±5. Allowed range
    # is 0..log-span; the frontend enforces the log-span cap, here we
    # just clamp to a generous hard bound so a stray value can't blow up
    # the pre-scan. 0 is valid (capture only the exact issue instant).
    issue_time_window_minutes = None
    if "issue_time_window_minutes" in data:
        try:
            issue_time_window_minutes = max(0, min(100000, int(data.get("issue_time_window_minutes"))))
        except (TypeError, ValueError):
            issue_time_window_minutes = None

    try:
        agent = _get_or_create_agent()
        if issue_time_window_minutes is not None:
            agent.issue_time_window_minutes = issue_time_window_minutes
        if not agent.current_log_path:
            def _no_log():
                yield f"data: {json.dumps({'type': 'error', 'content': 'No log file loaded. Please set a log file first.'})}\n\n"
            return Response(_no_log(), mimetype="text/event-stream")

        # ------------------------------------------------------------------
        # Issue Time: the sidebar field is the SINGLE source of truth.
        # When the frontend includes the `issue_time` key, override whatever
        # was pre-populated by prime_with_context (e.g. attachment_time).
        # An empty string with `issue_time_cleared=True` means "user explicitly
        # chose no time" — clear the agent's issue_time AND any backup sources.
        # An empty string WITHOUT that flag means the sidebar had only time
        # fields filled (time-only, no date) — keep the sentinel so pre-scan
        # can align the date to the log file range.
        # ------------------------------------------------------------------
        if "issue_time" in data:
            raw_it = (data.get("issue_time") or "").strip()
            explicitly_cleared = bool(data.get("issue_time_cleared", False))
            if raw_it:
                # Full datetime from sidebar — override agent's issue_time
                if isinstance(agent.issue_context, dict):
                    agent.issue_context.pop("attachment_time", None)
                parsed, is_time_only = parse_issue_time_string(raw_it)
                agent.issue_time = parsed
                agent._issue_time_time_only = is_time_only
            elif explicitly_cleared:
                # Explicit "no time": clear agent state and neutralise
                # description/subject so the fallback chain can't re-extract one.
                agent.issue_time = None
                if isinstance(agent.issue_context, dict):
                    agent.issue_context.pop("attachment_time", None)
                    for k in ("description", "subject"):
                        v = agent.issue_context.get(k)
                        if isinstance(v, str) and v:
                            # Strip recognisable timestamp fragments.
                            v = re.sub(r'\d{1,2}/\d{1,2}/\d{4}[\s-]\d{1,2}:\d{2}:\d{2}(?:\.\d{1,3})?', '', v)
                            v = re.sub(r'\d{4}[-/]\d{1,2}[-/]\d{1,2}[\sT]\d{1,2}:\d{2}:\d{2}', '', v)
                            v = re.sub(r'\b\d{1,2}:\d{2}:\d{2}(?:\.\d{1,3})?\b', '', v)
                            agent.issue_context[k] = re.sub(r'\s+', ' ', v).strip()
            # else: empty but not explicitly cleared (time-only in sidebar, date blank)
            # → keep agent.issue_time as-is (sentinel 0001-01-01) so pre-scan aligns date

        # ------------------------------------------------------------------
        # Feedback sidecar: identify this turn so the frontend can attach
        # 👍/👎 to it, and so the conversation snapshot can record skill
        # invocations. Both IDs are anonymous (no auth).
        # ------------------------------------------------------------------
        session_id = session.get("chatbot_session_id", "")
        conversation_id = _ensure_feedback_conversation_id()
        turn_id = str(uuid.uuid4())
        turn_started_at = datetime.now()
        try:
            _issue_ctx_for_snapshot = _extract_issue_context()
        except Exception:
            _issue_ctx_for_snapshot = {}

        # Use the mode flag sent by the frontend toggle.
        use_tools = bool(data.get("use_tools", False))

        if use_tools:
            import queue as _queue
            step_queue = _queue.Queue()
            collected_steps: list = []

            def step_cb(step):
                # Collect for snapshot, then forward to SSE stream.
                try:
                    if isinstance(step, dict):
                        collected_steps.append(step)
                except Exception:
                    pass
                step_queue.put(("step", step))

            @copy_current_request_context
            def run_chat_with_tools():
                try:
                    result = agent.chat(
                        user_message,
                        use_tools=True,
                        max_steps=max_steps,
                        temperature=temperature,
                        max_tokens=max_tokens,
                        step_callback=step_cb,
                    )
                    step_queue.put(("done", result))
                except Exception as exc:
                    error_tb = traceback.format_exc()
                    print(f"❌ Chat-with-tools thread error:\n{error_tb}")
                    step_queue.put(("error", str(exc)))

            t = threading.Thread(target=run_chat_with_tools, daemon=True)
            t.start()

            def event_stream():
                while True:
                    try:
                        msg_type, payload = step_queue.get(timeout=120)
                    except _queue.Empty:
                        yield f"data: {json.dumps({'type': 'error', 'content': 'Chat timed out.'})}\n\n"
                        break
                    if msg_type == "step":
                        yield f"data: {json.dumps({'type': 'step', 'step': payload}, ensure_ascii=False)}\n\n"
                    elif msg_type == "done":
                        # Sidecar: persist the turn before signalling done.
                        # Failures here are swallowed inside feedback_service.
                        feedback_service.record_turn(
                            session_id=session_id,
                            conversation_id=conversation_id,
                            turn_id=turn_id,
                            user_message=user_message,
                            agent_result=payload,
                            steps=collected_steps,
                            mode="tools",
                            duration_ms=int((datetime.now() - turn_started_at).total_seconds() * 1000),
                            issue=_issue_ctx_for_snapshot,
                            log_path=getattr(agent, "current_log_path", "") or "",
                            parent_message_id=parent_message_id,
                            domain="bt",
                        )
                        yield f"data: {json.dumps({'type': 'done', 'turn_id': turn_id, 'conversation_id': conversation_id, 'result': payload}, ensure_ascii=False)}\n\n"
                        break
                    elif msg_type == "error":
                        yield f"data: {json.dumps({'type': 'error', 'content': payload})}\n\n"
                        break

            return Response(
                event_stream(),
                mimetype="text/event-stream",
                headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
            )
        else:
            # No prior analysis — simple direct chat, wrapped in SSE
            result = agent.chat(
                user_message,
                use_tools=False,
                temperature=temperature,
                max_tokens=max_tokens,
            )

            # Sidecar: persist the turn (no step trace in simple mode).
            feedback_service.record_turn(
                session_id=session_id,
                conversation_id=conversation_id,
                turn_id=turn_id,
                user_message=user_message,
                agent_result=result,
                steps=[],
                mode="simple",
                duration_ms=int((datetime.now() - turn_started_at).total_seconds() * 1000),
                issue=_issue_ctx_for_snapshot,
                log_path=getattr(agent, "current_log_path", "") or "",
                parent_message_id=parent_message_id,
                domain="bt",
            )

            def generate():
                yield f"data: {json.dumps({'type': 'done', 'turn_id': turn_id, 'conversation_id': conversation_id, 'result': result}, ensure_ascii=False)}\n\n"

            return Response(generate(), mimetype="text/event-stream")
    except Exception as e:
        error_traceback = traceback.format_exc()
        print(f"❌ Chatbot error:\n{error_traceback}")

        def generate_error():
            yield f"data: {json.dumps({'type': 'error', 'content': str(e)}, ensure_ascii=False)}\n\n"

        return Response(generate_error(), mimetype="text/event-stream")


# ------------------------------------------------------------------
# API: reset conversation
# ------------------------------------------------------------------
@bt_chatbot_bp.route("/reset", methods=["POST"])
def reset():
    try:
        agent = _get_or_create_agent()
        agent.reset_conversation()
        return jsonify({"success": True, "message": "Conversation reset."})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


# ------------------------------------------------------------------
# Back to Avatar: drop the chatbot session entirely so the next visit
# to /bt_chatbot/ starts with a fresh conversation (no prior analysis).
# ------------------------------------------------------------------
@bt_chatbot_bp.route("/back_to_avatar", methods=["GET"])
def back_to_avatar():
    # 1) Discard the per-session WifiLogAgentSystem instance (chat history,
    #    skill cache, primed context, issue_time, etc.).
    sid = session.pop("chatbot_session_id", None)
    if sid and sid in _chatbot_instances:
        try:
            _chatbot_instances.pop(sid, None)
        except Exception:
            pass

    # 2) Drop every Flask-session key that would otherwise re-seed a new
    #    agent via prime_with_context() the next time /bt_chatbot/ is
    #    visited (case context, AI analysis, classification, selected
    #    attachments, cached log path, etc.).
    for key in (
        "chatbot_log_path",
        "case_context",
        "ai_ips_analysis",
        "classification",
        "selected_files",
        "attachment_list",
        "issue_time",
        "_attachment_time_cache",
        "_resolved_issue_time_cache",
        "_issue_ai_quick",            # LLM-organized description + issue times
        "feedback_conversation_id",   # next /bt_chatbot/ visit starts a fresh conversation
    ):
        session.pop(key, None)

    # 3) Clear the global "last analyzed log" hint so the chatbot page
    #    doesn't pre-fill the previous run's log path.
    try:
        app_config.last_analyzed_log_path = ""
    except Exception:
        pass

    return redirect(url_for("main.index"))


# ------------------------------------------------------------------
# API: prepare chatbot from download_result (set log path + case context)
# ------------------------------------------------------------------
@bt_chatbot_bp.route("/prepare", methods=["POST"])
def prepare():
    """
    Called from download_result when user clicks "Chatbot Analysis".
    1. Derives the .log path from the given etl_path.
    2. Sets the agent's current_log_path.
    3. Primes conversation history with case description + classification.
    Returns {"success": True} — JS then redirects to /bt_chatbot/.
    """
    data = request.get_json(silent=True) or {}
    etl_path = data.get("etl_path", "").strip()
    if not etl_path:
        return jsonify({"success": False, "error": "etl_path is required"}), 400

    # BT HCI decode produces .hci.txt directly (no separate .log file),
    # unlike the Wi-Fi wpp_ddd flow which produces <etl>.log. Mirror the
    # log_parser entry handling: if the caller already passed a direct log
    # file (.log / .txt — incl. .hci.txt from the BT decode), use it as-is;
    # otherwise fall back to the WiFi-style "<etl>.log" convention.
    lower = etl_path.lower()
    if os.path.exists(etl_path) and (lower.endswith(".log") or lower.endswith(".txt")):
        log_path = etl_path
    else:
        log_path = etl_path + ".log"
    if not os.path.exists(log_path):
        return jsonify({"success": False, "error": f"Log file not found: {log_path}"}), 404

    try:
        # Pull consolidated issue context from all session sources
        ctx = _extract_issue_context()

        # Update shared last_analyzed_log_path so the chatbot index page pre-fills it
        app_config.last_analyzed_log_path = log_path

        # Get/create per-session agent and prime it
        # skip_prime=True: we call prime_with_context explicitly below (after setting log path)
        agent = _get_or_create_agent(skip_prime=True)
        agent.current_log_path = log_path
        agent.prime_with_context(**ctx)

        # Run the token-frugal LLM issue-time + description organize NOW, on the
        # button click (deterministic pre-filter trims noise first). Cached in
        # session so the chatbot page's /get_issue_context reuses it rather than
        # calling the LLM a second time. Never let it fail the prepare step.
        try:
            _first_ts, _last_ts = read_log_time_range(log_path)
            _issue_context_organized(ctx.get("description", "") or "", _first_ts, _last_ts)
        except Exception as _org_err:
            print(f"⚠️ Chatbot prepare: issue-context organize skipped: {_org_err}")

        # Sidecar: prepare = entering a new analysis = new conversation.
        new_conv_id = _ensure_feedback_conversation_id(rotate=True)
        feedback_service.ensure_conversation(
            conversation_id=new_conv_id,
            session_id=session.get("chatbot_session_id", ""),
            issue=ctx,
            log_path=log_path,
            domain="bt",
        )

        return jsonify({"success": True})
    except Exception as e:
        error_traceback = traceback.format_exc()
        print(f"❌ Chatbot prepare error:\n{error_traceback}")
        return jsonify({"success": False, "error": str(e)}), 500


# ------------------------------------------------------------------
# API: open native directory browser and return selected path
# ------------------------------------------------------------------
@bt_chatbot_bp.route("/browse_dir", methods=["GET"])
def browse_dir():
    """Open a native Windows folder dialog and return the selected directory path."""
    result = {"path": ""}

    def _open_dialog():
        root = tk.Tk()
        root.withdraw()
        root.wm_attributes("-topmost", True)
        path = filedialog.askdirectory(title="Select skills data directory")
        root.destroy()
        result["path"] = path or ""

    t = threading.Thread(target=_open_dialog)
    t.start()
    t.join(timeout=60)

    return jsonify({"success": True, "path": result["path"]})


# ------------------------------------------------------------------
# API: reload skills from a directory and apply to current agent
# ------------------------------------------------------------------
@bt_chatbot_bp.route("/reload_skills", methods=["POST"])
def reload_skills():
    """
    Reload skills from the given data_dir (must contain prompt/ and filter/
    sub-folders) and apply them to the current session agent.
    If data_dir is omitted or invalid the builtin fallback skills are used.
    """
    data = request.get_json(silent=True) or {}
    data_dir = data.get("data_dir", "").strip()

    try:
        from pathlib import Path
        warning = None
        if data_dir and Path(data_dir).exists():
            skill_map = build_skill_file_map(data_dir)
            if skill_map is None:
                skills = get_builtin_skills()
                warning = f"No prompt/filter files found in '{data_dir}'. Using built-in skills."
            else:
                skills = load_skills_from_data_dir(data_dir)
        elif data_dir:
            skills = get_builtin_skills()
            warning = f"Directory '{data_dir}' not found. Using built-in skills."
        else:
            skills = get_builtin_skills()
            warning = f"No directory specified. Using built-in skills."

        agent = _get_or_create_agent()
        # Mid-conversation skill edit: swap skills AND clear the rule/filter
        # caches so the edit actually takes effect, while keeping history.
        agent.apply_updated_skills(skills)
        # Also update the app-level agent so future sessions share the new skills
        if app_config.bt_chatbot_agent:
            app_config.bt_chatbot_agent.skills = skills
        if app_config.llm_helper:
            app_config.llm_helper.skills = skills

        return jsonify({
            "success": True,
            "message": f"{len(skills)} skills loaded from {data_dir}",
            "warning": warning,
            "skills": agent.get_skill_descriptions(),
        })
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


# ------------------------------------------------------------------
# API: browse for a YAML file (native file dialog)
# ------------------------------------------------------------------
@bt_chatbot_bp.route("/browse_yaml", methods=["GET"])
def browse_yaml():
    """Open a native file dialog to select a skills .yaml file."""
    result = {"path": ""}

    def _open_dialog():
        root = tk.Tk()
        root.withdraw()
        root.wm_attributes("-topmost", True)
        path = filedialog.askopenfilename(
            title="Select skills YAML file",
            filetypes=[("YAML files", "*.yaml *.yml"), ("All files", "*.*")],
        )
        root.destroy()
        result["path"] = path or ""

    t = threading.Thread(target=_open_dialog)
    t.start()
    t.join(timeout=60)

    return jsonify({"success": True, "path": result["path"]})


# ------------------------------------------------------------------
# API: load skills from a YAML file (standalone, no prompt/filter dirs)
# ------------------------------------------------------------------
@bt_chatbot_bp.route("/load_skills_yaml", methods=["POST"])
def load_skills_yaml_route():
    """
    Load skills directly from a .yaml file.
    Request JSON: { "yaml_path": "/path/to/skills.yaml" }
    """
    data = request.get_json(silent=True) or {}
    yaml_path = data.get("yaml_path", "").strip()

    if not yaml_path:
        return jsonify({"success": False, "error": "yaml_path is required."}), 400

    try:
        skills = load_skills_from_yaml(yaml_path)

        agent = _get_or_create_agent()
        # Mid-conversation skill edit: swap skills AND clear the rule/filter
        # caches so the edit actually takes effect, while keeping history.
        agent.apply_updated_skills(skills)
        # Also update app-level so future sessions share the new skills
        if app_config.bt_chatbot_agent:
            app_config.bt_chatbot_agent.skills = skills
        if app_config.llm_helper:
            app_config.llm_helper.skills = skills

        return jsonify({
            "success": True,
            "message": f"{len(skills)} skills loaded from YAML",
            "source": yaml_path,
            "skills": agent.get_skill_descriptions(),
        })
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"success": False, "error": str(e)}), 500


# ------------------------------------------------------------------
# API: Reload skills from shared folder (auto-discovery)
# ------------------------------------------------------------------
@bt_chatbot_bp.route("/reload_from_shared", methods=["POST"])
def reload_from_shared():
    """
    Reload skills from the shared YAML location.
    Used for development/testing without restarting the app.
    """
    from configs.path_configs import SKILLS_CONFIG_DIR_prim, SKILLS_CONFIG_DIR_bkup, SKILLS_YAML_FILENAME
    from pathlib import Path
    from utils import helpers as _helpers
    
    try:
        # Try to find shared YAML location
        yaml_shared = _helpers.get_load_path(
            str(Path(SKILLS_CONFIG_DIR_prim) / SKILLS_YAML_FILENAME),
            str(Path(SKILLS_CONFIG_DIR_bkup) / SKILLS_YAML_FILENAME)
        )
        
        if not yaml_shared or not Path(yaml_shared).exists():
            return jsonify({
                "success": False,
                "error": f"Shared YAML not found at {SKILLS_CONFIG_DIR_prim} or {SKILLS_CONFIG_DIR_bkup}"
            }), 400
        
        # Load skills from shared YAML
        skills = load_skills_from_yaml(yaml_shared)
        
        # Update all instances
        agent = _get_or_create_agent()
        # Mid-conversation skill edit: swap skills AND clear the rule/filter
        # caches so the edit actually takes effect, while keeping history.
        agent.apply_updated_skills(skills)
        if app_config.bt_chatbot_agent:
            app_config.bt_chatbot_agent.skills = skills
        if app_config.llm_helper:
            app_config.llm_helper.skills = skills
        
        return jsonify({
            "success": True,
            "message": f"{len(skills)} skills reloaded from shared folder",
            "source": yaml_shared,
            "skills": agent.get_skill_descriptions(),
        })
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"success": False, "error": str(e)}), 500


# ------------------------------------------------------------------
# API: get available skills
# ------------------------------------------------------------------
@bt_chatbot_bp.route("/skills", methods=["GET"])
def get_skills():
    try:
        agent = _get_or_create_agent()
        return jsonify({
            "success": True,
            "skills": agent.get_skill_descriptions(),
        })
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


def _get_llm_client_model():
    """Return (client, model) for one-shot LLM calls, borrowing from the
    pre-initialised chatbot agent or the llm_helper. (None, None) when the app
    has no API key configured — callers then fall back to deterministic logic."""
    base = getattr(app_config, "bt_chatbot_agent", None)
    if base is not None and getattr(base, "client", None) is not None:
        return base.client, getattr(base, "model", None)
    helper = getattr(app_config, "llm_helper", None)
    if helper is not None and getattr(helper, "client", None) is not None:
        return helper.client, getattr(helper, "model", "gpt-4.1")
    return None, None


def _issue_context_organized(raw_desc: str, first_ts, last_ts) -> dict:
    """Return the organized issue context (clean description + issue time list).

    Prefers the quick pre-pass cached at the select-attachments step
    (``_issue_ai_quick``) so the whole flow makes a SINGLE LLM call — its
    (possibly undated) times are just re-aligned to the loaded log's date here.
    Falls back to organizing now (e.g. direct chatbot entry with no prior step).
    """
    quick = session.get("_issue_ai_quick")
    if isinstance(quick, dict) and isinstance(quick.get("data"), dict):
        d = quick["data"]
    else:
        client, model = _get_llm_client_model()
        d = organize_issue_context(raw_desc, first_ts=first_ts, last_ts=last_ts,
                                   llm_client=client, llm_model=model)
        session["_issue_ai_quick"] = {"data": d}
    return {
        "clean_description": d.get("clean_description") or raw_desc,
        "issue_times": realign_times_to_log(d.get("issue_times") or [], first_ts, last_ts),
        "interpretation": d.get("interpretation", ""),
    }


@bt_chatbot_bp.route("/get_issue_context", methods=["GET"])
def get_issue_context():
    try:
        ctx = _extract_issue_context()
        attachment_time = ctx.get("attachment_time", "")
    except Exception:
        ctx = {}
        attachment_time = ""

    log_path = session.get("chatbot_log_path") or app_config.last_analyzed_log_path or ""
    first_ts, last_ts = read_log_time_range(log_path) if log_path else (None, None)

    # Smart pass: let the LLM organize the raw case Issue Description into a
    # clean problem statement + (possibly multiple) issue time points. Cached
    # per-description so repeat fetches don't re-call the LLM; falls back to
    # the regex extractor + concise composer when no LLM is configured.
    organized = _issue_context_organized(ctx.get("description", "") or "", first_ts, last_ts)
    clean_desc = organized.get("clean_description") or _compose_concise_description(ctx)
    issue_times = organized.get("issue_times") or []

    # Back-compat single issue_time: prefer the first organized time, else the
    # previous attachment_time / log-latest resolution (cached by log_path).
    if issue_times:
        issue_time_str = issue_times[0]
    else:
        issue_time_str = _resolved_issue_time_for(log_path, attachment_time)

    return jsonify({
        "description": clean_desc,
        "attachment_time": attachment_time,
        "issue_time": issue_time_str,
        "issue_times": issue_times,
        "interpretation": organized.get("interpretation", ""),
    })


# ==================================================================
# BT log-selection policy  (DATA — extend these lists, not the logic)
# ==================================================================
# Everything BT-specific about how we time-stamp, classify and rank the
# candidate ETLs lives here as plain lists, so supporting a new collector
# layout / driver naming / artifact type is an edit to a list rather than a
# change to the selection logic below. None of these are assumed complete —
# append freely.

# Capture-timestamp formats seen in BT collection folder / file names. Each
# regex must expose 6 numeric groups in (Y, M, D, h, m, s) order.
#   e.g. ".../DESKTOP-8ED6JMJ-2026-04-08-00-16-16Z/ibtpci-...-boot.etl"
BT_PATH_TS_PATTERNS = [
    re.compile(r'(\d{4})-(\d{2})-(\d{2})-(\d{2})-(\d{2})-(\d{2})'),   # 2026-04-08-00-16-16[Z]
    re.compile(r'(\d{4})-(\d{2})-(\d{2})[ _](\d{2})-(\d{2})-(\d{2})'),  # 2026-04-08_00-16-16
    re.compile(r'(\d{4})(\d{2})(\d{2})[ _T-](\d{2})(\d{2})(\d{2})'),    # 20260408T001616
]

# Log-artifact nouns. Used only to recognise an EXPLICIT "use file X" request
# in free text: a distinguishing filename token tied to one of these nouns
# ("<token> log", "<token>.etl", "<token> capture") means the user is naming a
# specific log artifact — as opposed to describing a test/event (e.g. "cold
# boot", "CB/S4"). Domain-neutral words, not BT-case-specific.
BT_LOG_ARTIFACT_NOUNS = ["log", "etl", "trace", "capture", "dump", "buffer", "hci", "file"]

# Suffixes probed (in order) to size a candidate as a content-richness proxy.
# "" means the etl path itself (the raw .etl).
BT_SIZE_PROBE_SUFFIXES = [".hci.txt", "", ".log"]


def _parse_path_timestamp(path: str):
    """Extract a capture datetime from a BT ETL path / folder name, or None.
    Tries each pattern in BT_PATH_TS_PATTERNS — extend that list for new
    collector layouts."""
    if not path:
        return None
    for rx in BT_PATH_TS_PATTERNS:
        m = rx.search(path)
        if not m:
            continue
        try:
            y, mo, d, hh, mm, ss = (int(g) for g in m.groups())
            return datetime(y, mo, d, hh, mm, ss)
        except (ValueError, TypeError):
            continue
    return None


# ------------------------------------------------------------------
# BT log selection — domain-neutral tiebreak.
#
# When several candidates match the issue time equally well (e.g. multiple
# ETLs in the same capture folder share the folder timestamp), we choose
# WITHOUT assuming anything about specific filenames or log types:
#
#   1. If the issue text explicitly NAMES a candidate (a token unique to that
#      file, used as a log artifact — "<token> log/etl/capture/…"), honor it.
#   2. Otherwise prefer the LARGER file — more captured content is more likely
#      to contain the failure. This subsumes the common boot-vs-runtime case
#      (boot circular buffers are smaller) without hard-coding "boot".
#
# All judgments are general — they hold for any BT collector / driver / log
# family, not a single scenario.
# ------------------------------------------------------------------

def _etl_size(etl_path: str) -> int:
    """Best-available size proxy for richness, probing BT_SIZE_PROBE_SUFFIXES
    in order. 0 when nothing is found."""
    for suffix in BT_SIZE_PROBE_SUFFIXES:
        p = etl_path + suffix
        try:
            if os.path.exists(p):
                return os.path.getsize(p)
        except OSError:
            pass
    return 0


def _filename_tokens(path: str) -> set:
    """Alphanumeric tokens of a file's basename, lowercased."""
    base = os.path.basename(path or "").lower()
    return {t for t in re.split(r'[^a-z0-9]+', base) if t}


def _distinctive_tokens(path: str, all_paths: list) -> set:
    """Tokens in this filename that DON'T appear in any other candidate —
    i.e. what a user would say to refer to *this* file specifically. Numeric
    and very short tokens are dropped (not distinctive enough to name)."""
    others = set()
    for p in all_paths:
        if p != path:
            others |= _filename_tokens(p)
    distinct = _filename_tokens(path) - others
    return {t for t in distinct if len(t) >= 3 and not t.isdigit()}


# A distinctive token counts as an explicit pick only when it's used AS A LOG
# ARTIFACT — tied to one of BT_LOG_ARTIFACT_NOUNS ("<tok> log", "<tok>.etl",
# "capture <tok>"). This generalises the old boot-specific rule to ANY token
# while still ignoring test/event mentions (e.g. "cold boot", "CB/S4").
_ARTIFACT_ALT = "|".join(re.escape(n) for n in BT_LOG_ARTIFACT_NOUNS)


def _token_named_as_artifact(token: str, text: str) -> bool:
    if not token or not text:
        return False
    t = re.escape(token)
    # Separator is one-or-more space/underscore/dash ("boot log", "boot-etl")
    # OR a single dot with NOTHING after it but the noun ("boot.etl"). The
    # dot form deliberately forbids a trailing space so a sentence break like
    # "…cold boot. Logs show" is NOT read as naming the boot log.
    sep = r'(?:[\s_\-]+|\.)'
    pat = re.compile(
        t + sep + r'(?:' + _ARTIFACT_ALT + r')'          # "<tok> log" / "<tok>.etl"
        r'|(?:' + _ARTIFACT_ALT + r')' + sep + t,        # "capture <tok>"
        re.IGNORECASE,
    )
    return bool(pat.search(text))


def _explicitly_named(cands: list, intent_text: str):
    """Return the candidate the user explicitly named (distinctive token used
    as a log artifact), preferring the larger one on ties — or None."""
    intent = (intent_text or "").strip()
    if not intent:
        return None
    paths = [c["etl_path"] for c in cands]
    named = []
    for c in cands:
        toks = _distinctive_tokens(c["etl_path"], paths)
        if any(_token_named_as_artifact(t, intent) for t in toks):
            named.append(c)
    if not named:
        return None
    return max(named, key=lambda c: (int(c.get("size", 0) or 0), c["etl_path"]))


def _pick_preferred(cands: list, intent_text: str = "") -> dict:
    """
    Choose among candidates that already match the issue time equally well:
      1. one the user explicitly named (if any),
      2. else the larger file (richer content),
      3. path as a deterministic final tiebreak.
    No assumptions about specific filenames or log types.
    """
    named = _explicitly_named(cands, intent_text)
    if named is not None:
        return named
    return max(cands, key=lambda c: (int(c.get("size", 0) or 0), c["etl_path"]))


def _pref_note(c: dict, named: bool = False) -> str:
    """Short human-readable note on why a candidate won the tiebreak."""
    size_mb = (int(c.get("size", 0) or 0)) / (1024 * 1024)
    return f"{size_mb:.0f} MB, " + ("explicitly named" if named else "largest (richest) capture")


# ------------------------------------------------------------------
# API: find best matching log by reading actual file timestamps
# ------------------------------------------------------------------
@bt_chatbot_bp.route("/find_best_log", methods=["POST"])
def find_best_log():
    """
    Given a list of ETL paths and an issue time string, read the first/last
    timestamps from each corresponding .log file and return the ETL path
    whose log file time range covers (or is closest to) the issue time.

    Request JSON:
      { "etl_paths": ["path1", "path2", ...], "issue_time_str": "10/28/2025-11:25:49" }

    Response JSON:
      { "best_path": "path2", "reason": "...", "details": [...] }
    """
    data = request.get_json(silent=True) or {}
    etl_paths = data.get("etl_paths", [])
    issue_time_str = data.get("issue_time_str", "").strip()

    if not etl_paths:
        return jsonify({"best_path": None, "reason": "No ETL paths provided."})

    # --- Parse issue time from the provided string ---
    # Try strict canonical parse first (sidebar/auto-extract path), then
    # fall back to the looser description scanner for legacy free-form input.
    issue_time, is_time_only = parse_issue_time_string(issue_time_str)
    issue_time_only_str = None
    if issue_time and is_time_only:
        issue_time_only_str = issue_time.strftime("%H:%M:%S")
        issue_time = None
    if issue_time is None and issue_time_only_str is None:
        _parsed = extract_time_from_description(issue_time_str)
        if isinstance(_parsed, datetime):
            issue_time = _parsed
        elif isinstance(_parsed, str):
            issue_time_only_str = _parsed  # e.g. '14:50:51'

    # --- Scan each candidate for its time range ---
    # BT differs from Wi-Fi here: the .hci.txt log is only produced AFTER the
    # user clicks the agent button (HCI decode), so at selection time there is
    # NO decoded log to read. We therefore use the capture timestamp embedded
    # in the BT collection FOLDER name as a single-point anchor
    # (e.g. ".../DESKTOP-8ED6JMJ-2026-04-08-00-16-16Z/ibtpci-...etl"
    #  → 2026-04-08 00:16:16). If a decoded log happens to already exist we
    # still prefer reading its real first/last range.
    candidates = []
    for etl_path in etl_paths:
        log_path = etl_path + ".log"
        first_ts = last_ts = None
        if os.path.exists(log_path):
            first_ts, last_ts = read_log_time_range(log_path)
        if first_ts is None and last_ts is None:
            # Pre-decode (or unreadable) BT case → fall back to the folder
            # timestamp as a point anchor (first_ts == last_ts).
            folder_ts = _parse_path_timestamp(etl_path)
            if folder_ts is not None:
                first_ts = last_ts = folder_ts
        if first_ts is None and last_ts is None:
            continue
        candidates.append({
            "etl_path": etl_path,
            "log_path": log_path,
            "first_ts": first_ts,
            "last_ts": last_ts,
            "size": _etl_size(etl_path),
        })

    if not candidates:
        return jsonify({"best_path": etl_paths[0] if etl_paths else None,
                        "reason": "No readable log files or path timestamps; defaulting to first.",
                        "resolved_issue_time": ""})

    # Free-text intent used only to honor an EXPLICIT "use file X" request.
    # An explicit `intent` in the request wins; otherwise we read the case
    # issue context. With no explicit naming, selection falls back to the
    # domain-neutral "largest (richest) capture" rule — no filename or
    # log-type assumptions.
    intent_text = (data.get("intent") or "").strip()
    if not intent_text:
        try:
            _ctx = _extract_issue_context()
            intent_text = f"{_ctx.get('subject', '')} {_ctx.get('description', '')}"
        except Exception:
            intent_text = ""

    print(f"[find_best_log] candidates={[c['etl_path'] for c in candidates]}, issue_time={issue_time}, issue_time_only_str={issue_time_only_str}")

    # --- Resolve time-only issue_time_str using log file dates ---
    # e.g. '14:50:51' -> combine with the date from the log's first/last timestamp
    if not issue_time and issue_time_only_str and candidates:
        try:
            ih, im, is_ = map(int, issue_time_only_str.split(':'))
            for c in candidates:
                ref_ts = c["last_ts"] or c["first_ts"]
                if ref_ts:
                    issue_time = ref_ts.replace(hour=ih, minute=im, second=is_, microsecond=0)
                    break
        except Exception:
            pass

    # Serialize the resolved issue_time so the frontend can use the full datetime
    resolved_issue_time_str = issue_time.strftime("%m/%d/%Y-%H:%M:%S") if issue_time else ""

    # --- If we have an issue time, pick the log whose range covers it ---
    if issue_time:
        # Priority 1: logs whose [first_ts, last_ts] contains issue_time.
        # Multiple may qualify (esp. same-folder captures sharing the folder
        # timestamp) — break the tie by explicit-name then size.
        covering = [c for c in candidates
                    if c["first_ts"] and c["last_ts"]
                    and c["first_ts"] <= issue_time <= c["last_ts"]]
        if covering:
            chosen = _pick_preferred(covering, intent_text)
            named = _explicitly_named(covering, intent_text) is chosen
            return jsonify({
                "best_path": chosen["etl_path"],
                "reason": f"Log covers issue time {issue_time_str} "
                          f"(range: {chosen['first_ts']} ~ {chosen['last_ts']}; "
                          f"{_pref_note(chosen, named)})",
                "resolved_issue_time": resolved_issue_time_str,
            })

        # Priority 2: logs closest to issue_time. Collect all within ~1s of the
        # minimum delta (exact ties are normal when candidates share a folder
        # timestamp), then apply the explicit-name/size tiebreak.
        withdelta = []
        for c in candidates:
            ts = c["last_ts"] or c["first_ts"]
            if ts:
                withdelta.append((c, abs((issue_time - ts).total_seconds())))
        if withdelta:
            min_delta = min(d for _, d in withdelta)
            tied = [c for c, d in withdelta if d <= min_delta + 1.0]
            chosen = _pick_preferred(tied, intent_text)
            named = _explicitly_named(tied, intent_text) is chosen
            return jsonify({
                "best_path": chosen["etl_path"],
                "reason": f"Closest log to issue time {issue_time_str} "
                          f"(range: {chosen['first_ts']} ~ {chosen['last_ts']}, "
                          f"delta: {min_delta:.0f}s; {_pref_note(chosen, named)})",
                "resolved_issue_time": resolved_issue_time_str,
            })

    # --- Fallback: latest last_ts; tie-break same-time logs by name/size ---
    candidates_with_ts = [c for c in candidates if c["last_ts"]]
    if candidates_with_ts:
        latest_ts = max(c["last_ts"] for c in candidates_with_ts)
        tied = [c for c in candidates_with_ts if c["last_ts"] == latest_ts]
        chosen = _pick_preferred(tied, intent_text)
        named = _explicitly_named(tied, intent_text) is chosen
        return jsonify({
            "best_path": chosen["etl_path"],
            "reason": f"No issue time provided; picked latest log "
                      f"(range: {chosen['first_ts']} ~ {chosen['last_ts']}; "
                      f"{_pref_note(chosen, named)})",
            "resolved_issue_time": resolved_issue_time_str,
        })

    # --- Ultimate fallback ---
    return jsonify({
        "best_path": candidates[0]["etl_path"],
        "reason": "Could not determine timestamps; defaulting to first.",
        "resolved_issue_time": "",
    })


# ==================================================================
# Skills YAML lifecycle — dated filenames (skills_YYYY-MM-DD.yaml)
# ==================================================================
#
# Endpoints below implement the SVG v2 "Skill" column: detect cloud
# revisions newer than the local cache, let the user opt in to replace
# the local copy, edit individual skills through a structured side panel,
# and upload a user-tuned local file back to the share folder.
# ------------------------------------------------------------------

from utils.bt_skills_yaml_utils import (
    current_active_yaml as _current_active_yaml,
    find_latest_cloud_baseline_yaml as _latest_cloud_baseline,
    find_latest_share_yaml as _latest_share_yaml,
    find_latest_user_yaml as _latest_user_yaml,
    get_active_source as _get_active_source,
    local_cloud_baseline_dir as _cloud_local_dir,
    local_user_overrides_dir as _user_local_dir,
    refresh_local_cloud_baseline as _refresh_cloud_baseline,
    resolve_cloud_skills_dir as _resolve_cloud_skills_dir,
    set_active_source as _set_active_source,
    skills_yaml_status as _skills_yaml_status_payload,
    today_dated_filename as _today_yaml_filename,
)


def _read_yaml_file(path) -> dict:
    """Load a YAML file as a plain dict. Raises on parse failure."""
    import yaml
    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    if not isinstance(data, dict):
        raise ValueError(
            f"Invalid YAML structure: expected a dict, got {type(data).__name__}"
        )
    return data


_CLOUD_YAML_HEADER = (
    "# skill features:\n"
    "#   name: skill name\n"
    "#   description: a brief description of the skill\n"
    "#   keywords: use \"-\" to represent each keyword\n"
    "#   expert_rules: use \"|\" to start a multi-line string\n"
    "\n"
)

_USER_YAML_WRITE_LOCK = threading.Lock()


def _write_yaml_file(path, data: dict, disabled_comments: dict | None = None) -> None:
    """
    Write a dict to a YAML file using the same hand-authored layout the
    cloud baseline file uses, so files written from the side-panel editor
    are visually consistent with files maintained by the Wireless CE team.

    Conventions copied from the cloud `skills_<date>.yaml`:
      * Top-of-file schema comment block.
      * Scalar VALUES are double-quoted (mapping KEYS stay unquoted).
      * Lists indent one level deeper than their parent key
        (`  keywords:\\n    - "..."`).
      * Multi-line strings use the literal block scalar `|`.
      * Top-level skills are separated by a blank line.

    ``disabled_comments`` (optional) re-injects commented-out keyword /
    exclusive entries — yaml.safe_load drops comments on load, so this
    parameter is the bridge that keeps cloud-baseline "historically used
    but disabled" entries from disappearing on round-trip.
    Shape: ``{skill_key: {'keywords' | 'exclusive': [str, ...]}}``.
    """
    import yaml
    from pathlib import Path as _P

    class _CloudDumper(yaml.SafeDumper):
        # Track whether we're currently emitting a mapping KEY vs a VALUE
        # so the str representer can quote values without quoting keys.
        pass

    _CloudDumper._cloud_in_key = False  # type: ignore[attr-defined]

    def _str_representer(dumper, value):
        # Multi-line text → literal block style for readability.
        #
        # PyYAML silently FALLS BACK to a double-quoted scalar (with
        # embedded "\n" / "\t" escapes) whenever the input contains
        # characters the literal block style can't represent safely:
        #
        #   * line-internal trailing whitespace → rstrip each line
        #   * tab characters anywhere           → convert to 4 spaces
        #     (cloud-baseline `[ALON \t\t]` cosmetic alignment survives
        #      with spaces and looks the same in a monospace editor)
        #
        # Also append a final "\n" so the emitter uses "|" (clip) instead
        # of "|-" (strip), matching the hand-authored cloud baseline.
        if isinstance(value, str) and "\n" in value:
            value = value.replace("\t", "    ")
            value = "\n".join(line.rstrip() for line in value.split("\n"))
            if not value.endswith("\n"):
                value = value + "\n"
            return dumper.represent_scalar("tag:yaml.org,2002:str", value, style="|")
        # Plain scalar for keys, quoted for values.
        if getattr(dumper, "_cloud_in_key", False):
            return dumper.represent_scalar("tag:yaml.org,2002:str", value)
        # For value scalars, flatten stray tabs so the YAML emitter never
        # has to fall back to escape-heavy quoting.
        cleaned = value.replace("\t", "    ")
        # Smart quote pick: when the content already contains double
        # quotes (e.g. PDF examples pasted by the user) but no single
        # quotes, use single-quoted YAML so we don't litter the output
        # with `\"...\"` escapes. Default to double-quoted otherwise to
        # match the cloud baseline's hand-authored convention.
        if '"' in cleaned and "'" not in cleaned:
            style = "'"
        else:
            style = '"'
        return dumper.represent_scalar("tag:yaml.org,2002:str", cleaned, style=style)

    _CloudDumper.add_representer(str, _str_representer)

    # Re-implement represent_mapping so KEYs go through the unquoted
    # path while VALUEs get the double-quote treatment.
    def _represent_mapping(self, tag, mapping, flow_style=None):
        value = []
        node = yaml.MappingNode(tag, value, flow_style=flow_style)
        if self.alias_key is not None:
            self.represented_objects[self.alias_key] = node
        best_style = True
        if hasattr(mapping, "items"):
            mapping = list(mapping.items())
        for item_key, item_value in mapping:
            self._cloud_in_key = True
            node_key = self.represent_data(item_key)
            self._cloud_in_key = False
            node_value = self.represent_data(item_value)
            if not (isinstance(node_key, yaml.ScalarNode) and not node_key.style):
                best_style = False
            if not (isinstance(node_value, yaml.ScalarNode) and not node_value.style):
                best_style = False
            value.append((node_key, node_value))
        if flow_style is None:
            if self.default_flow_style is not None:
                node.flow_style = self.default_flow_style
            else:
                node.flow_style = best_style
        return node
    _CloudDumper.represent_mapping = _represent_mapping

    # Indent list items so they sit ONE level deeper than the parent key
    # (i.e. never use indentless sequences).
    def _increase_indent(self, flow=False, indentless=False):
        return yaml.SafeDumper.increase_indent(self, flow, False)
    _CloudDumper.increase_indent = _increase_indent

    def _dump_one(skill_key: str, skill_val) -> str:
        return yaml.dump(
            {skill_key: skill_val},
            Dumper=_CloudDumper,
            allow_unicode=True,
            sort_keys=False,
            default_flow_style=False,
            width=1000,
        ).rstrip("\n")

    if isinstance(data, dict):
        blocks = [_dump_one(k, v) for k, v in data.items()]
    else:
        blocks = [yaml.dump(
            data, Dumper=_CloudDumper, allow_unicode=True,
            sort_keys=False, default_flow_style=False, width=1000,
        ).rstrip("\n")]

    content = _CLOUD_YAML_HEADER + "\n\n".join(blocks) + "\n"

    # Re-inject any commented-out keyword / exclusive entries that the
    # caller asked us to preserve (`disabled_comments`). yaml.safe_load
    # drops comments on load, so we scan the cloud baseline / previous
    # user file separately and stitch them back in here.
    if disabled_comments:
        content = _inject_disabled_comments(content, disabled_comments)

    p = _P(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(f"{p.name}.{uuid.uuid4().hex}.tmp")
    try:
        tmp.write_text(content, encoding="utf-8")
        tmp.replace(p)
    finally:
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:
            pass


def _persist_user_yaml_snapshot(data: dict) -> object:
    """
    Persist the current user-edited YAML under today's dated filename.

    The file name is date-based, so repeated saves on the same day target the
    same path. Serialise writes in-process so overlapping save/delete requests
    do not race on the same target and temp file.
    """
    with _USER_YAML_WRITE_LOCK:
        target_dir = _user_local_dir()
        target = target_dir / _today_yaml_filename()
        _write_yaml_file(target, data, _gather_disabled_comments(data))

        # Keep only today's active revision in the user/ dir so lookup stays
        # unambiguous.
        for entry in target_dir.iterdir():
            if entry.is_file() and entry.name != target.name \
                    and entry.name.startswith("bt_skills_") and entry.suffix == ".yaml":
                try:
                    entry.unlink()
                except OSError:
                    pass

        return target


# ---- Disabled-comment scanning + injection -------------------------------
#
# The cloud baseline uses lines like:
#
#     keywords:
#       - "TASK_DISCONNECT"
#       - "CNCT_FLOW"
#       # - "Got Command"
#       - "candidate grade"
#
# to mark "historically used but currently disabled" entries. yaml.safe_load
# discards those comments. The two helpers below let us scan a YAML file
# for such commented entries (grouped by skill + list key) and then inject
# them back into a freshly-written YAML so a round-trip through the editor
# doesn't lose them.

_DISABLED_COMMENT_RE = __import__("re").compile(
    r"""^\s*\#\s*-\s*(['"])(?P<val>.+?)\1\s*$"""
)
# Top-level skill header (column 0, ends with bare ":"). Widened from
# the original `[A-Za-z_]\w*` so it accepts the real skill IDs in this
# codebase that contain "/" (e.g. "VLP/UHB/AFC", "WRDS/WGDS/EWRD/SGOM"
# — see services/bt_chatbot_service.py:SKILL_FILE_MAP). The previous
# regex silently failed on those, dropping their `# - "..."` disabled
# entries on every save round-trip. The first char is anchored to
# [A-Za-z0-9_] so list items ("- foo:") and comment lines ("# x:")
# are still rejected, and `\s*$` guarantees we only match bare key
# headers — not inline mappings like `Foo: bar`.
_DISABLED_SKILL_RE = __import__("re").compile(r"^([A-Za-z0-9_][^:]*):\s*$")
_DISABLED_LIST_HEADER_RE = __import__("re").compile(
    r"^  (keywords|exclusive):\s*$"
)
_DISABLED_DEPTH2_RE = __import__("re").compile(r"^  \w+\s*:")


def _scan_disabled_comments(text: str) -> dict:
    """
    Walk a YAML text and pull out commented-out entries inside each skill's
    ``keywords:`` and ``exclusive:`` block. Returns:

        { skill_key: { 'keywords' | 'exclusive': [str, ...] } }
    """
    if not text:
        return {}
    result: dict = {}
    current_skill = None
    current_list = None
    for line in text.split("\n"):
        m = _DISABLED_SKILL_RE.match(line)
        if m:
            current_skill = m.group(1)
            current_list = None
            continue
        m = _DISABLED_LIST_HEADER_RE.match(line)
        if m:
            current_list = m.group(1)
            continue
        # Any other depth-2 mapping key terminates the current list block
        # so a comment far away isn't misattributed.
        if (current_list is not None
                and _DISABLED_DEPTH2_RE.match(line)
                and not _DISABLED_LIST_HEADER_RE.match(line)):
            current_list = None
            continue
        if current_skill and current_list:
            m = _DISABLED_COMMENT_RE.match(line)
            if m:
                result.setdefault(current_skill, {}) \
                      .setdefault(current_list, []) \
                      .append(m.group("val"))
    return result


def _inject_disabled_comments(content: str, disabled: dict) -> str:
    """
    Walk the freshly-rendered YAML text and append ``# - "..."`` comment
    lines AFTER the last list item of each (skill, list_key) block whose
    disabled entries are still meaningful. Lines we know how to recognise:

      * skill header        — column-0 ``key:``  → starts a new skill
      * list header         — depth-2 ``keywords:`` / ``exclusive:``
      * list item           — depth-4 ``- "..."`` (current dumper uses 4)
      * any other depth-2 key — ends the current list block
    """
    if not disabled:
        return content

    lines = content.split("\n")
    # Pass 1: figure out, for each (skill, list_key) we have disabled
    # entries for, the line index AFTER which we should insert comments.
    insertions: dict = {}   # line_idx -> [str, ...]
    current_skill = None
    current_list = None
    last_list_item_idx = -1

    def _commit():
        nonlocal current_list, last_list_item_idx
        if current_skill and current_list:
            entries = disabled.get(current_skill, {}).get(current_list) or []
            if entries and last_list_item_idx >= 0:
                comments = [f'    # - "{v}"' for v in entries]
                insertions.setdefault(last_list_item_idx, []).extend(comments)
        current_list = None
        last_list_item_idx = -1

    for i, line in enumerate(lines):
        if _DISABLED_SKILL_RE.match(line):
            _commit()
            current_skill = _DISABLED_SKILL_RE.match(line).group(1)
            continue
        m_list = _DISABLED_LIST_HEADER_RE.match(line)
        if m_list:
            _commit()
            current_list = m_list.group(1)
            continue
        if (current_list is not None
                and _DISABLED_DEPTH2_RE.match(line)
                and not _DISABLED_LIST_HEADER_RE.match(line)):
            _commit()
            continue
        if current_list is not None and line.startswith("    - "):
            last_list_item_idx = i
    _commit()

    # Pass 2: rebuild text with the comments stitched in.
    if not insertions:
        return content
    out = []
    for i, line in enumerate(lines):
        out.append(line)
        if i in insertions:
            out.extend(insertions[i])
    return "\n".join(out)


def _gather_disabled_comments(active_data: dict) -> dict:
    """
    Build the `disabled_comments` map for the save path: scan the cloud
    baseline and the current user file (whichever exist) for commented
    `# - "..."` keyword / exclusive entries, MERGE them per skill +
    list-key, and strip any entry that the editor is about to write as an
    ACTIVE keyword (so re-enabling something through the UI doesn't leave
    a phantom commented duplicate behind).
    """
    merged: dict = {}

    def _absorb(path):
        if not path:
            return
        try:
            text = path.read_text(encoding="utf-8")
        except Exception:
            return
        for skill_key, blocks in _scan_disabled_comments(text).items():
            for list_key, vals in blocks.items():
                bucket = merged.setdefault(skill_key, {}).setdefault(list_key, [])
                for v in vals:
                    if v not in bucket:
                        bucket.append(v)

    try:
        cloud_path, _ = _latest_cloud_baseline()
        _absorb(cloud_path)
    except Exception:
        pass
    try:
        user_path, _ = _latest_user_yaml()
        _absorb(user_path)
    except Exception:
        pass

    # Drop entries that are now active in the about-to-be-saved data.
    if isinstance(active_data, dict):
        for skill_key, blocks in list(merged.items()):
            skill_row = active_data.get(skill_key)
            if not isinstance(skill_row, dict):
                continue
            for list_key in ("keywords", "exclusive"):
                if list_key not in blocks:
                    continue
                active_vals = set(skill_row.get(list_key) or [])
                blocks[list_key] = [
                    v for v in blocks[list_key] if v not in active_vals
                ]
                if not blocks[list_key]:
                    blocks.pop(list_key, None)
            if not blocks:
                merged.pop(skill_key, None)

    return merged


def _refresh_loaded_skills(yaml_path: str) -> dict:
    """Re-load skills from `yaml_path` into the live agent and llm_helper."""
    skills = load_skills_from_yaml(yaml_path)
    agent = _get_or_create_agent()
    # Keep history; clear rule/filter caches so the reloaded skills apply.
    agent.apply_updated_skills(skills)
    if app_config.bt_chatbot_agent:
        app_config.bt_chatbot_agent.skills = skills
    if app_config.llm_helper:
        app_config.llm_helper.skills = skills
    return skills


def _activate_yaml(path) -> dict:
    """Re-load skills from `path` and return the chatbot's descriptions."""
    _refresh_loaded_skills(str(path))
    agent = _get_or_create_agent()
    return agent.get_skill_descriptions()


@bt_chatbot_bp.route("/skills_yaml_status", methods=["GET"])
def skills_yaml_status():
    """
    Report the cloud-baseline vs user-overrides state for the side panel.

    Response JSON:
      {
        "success":          True,
        "active_source":    "cloud" | "user",
        "effective_source": "cloud" | "user",
        "cloud_local":      {path, date, filename},     # local cloud/ mirror
        "user_local":       {path, date, filename},     # local user/ overrides
        "share_remote":     {path, date, filename, reachable},
      }
    """
    try:
        return jsonify({"success": True, **_skills_yaml_status_payload()})
    except Exception as e:
        traceback.print_exc()
        return jsonify({"success": False, "error": str(e)}), 500


@bt_chatbot_bp.route("/skills_yaml_use_cloud", methods=["POST"])
def skills_yaml_use_cloud():
    """
    Switch the running agent to the local cloud/ baseline (the latest file
    pulled from the share folder). The user/ overrides on disk are kept
    intact so the user can toggle back later via /skills_yaml_use_user.
    """
    try:
        c_path, c_date = _latest_cloud_baseline()
        if c_path is None:
            return jsonify({
                "success": False,
                "error":   "No cloud baseline found. Connect to VPN and retry "
                           "so the baseline can be refreshed from the share folder.",
            }), 404
        _set_active_source("cloud")
        skills = _activate_yaml(c_path)
        return jsonify({
            "success":         True,
            "active_source":   "cloud",
            "local_path":      str(c_path),
            "local_date":      c_date.isoformat() if c_date else None,
            "filename":        c_path.name,
            "message":         "Now using the cloud baseline configuration.",
            "skills":          skills,
        })
    except Exception as e:
        traceback.print_exc()
        return jsonify({"success": False, "error": str(e)}), 500


@bt_chatbot_bp.route("/skills_yaml_use_user", methods=["POST"])
def skills_yaml_use_user():
    """
    Switch the running agent to the user's local overrides. Returns 404 when
    the user has not yet edited the configuration this session — the toggle
    is only meaningful once a user override exists.
    """
    try:
        u_path, u_date = _latest_user_yaml()
        if u_path is None:
            return jsonify({
                "success": False,
                "error":   "No customised configuration found yet. Edit a "
                           "skill via 'Edit Skills Configuration' first.",
            }), 404
        _set_active_source("user")
        skills = _activate_yaml(u_path)
        return jsonify({
            "success":         True,
            "active_source":   "user",
            "local_path":      str(u_path),
            "local_date":      u_date.isoformat() if u_date else None,
            "filename":        u_path.name,
            "message":         "Now using your customised configuration.",
            "skills":          skills,
        })
    except Exception as e:
        traceback.print_exc()
        return jsonify({"success": False, "error": str(e)}), 500


@bt_chatbot_bp.route("/refresh_cloud_baseline", methods=["POST"])
def refresh_cloud_baseline_route():
    """
    Force-refresh the local cloud/ mirror from the share folder. Idempotent —
    safe to call from a "retry" button when the user reconnects to VPN.
    """
    try:
        path, dt = _refresh_cloud_baseline()
        if path is None:
            return jsonify({
                "success": False,
                "error":   "Share folder is unreachable; please retry on VPN.",
            }), 503

        # If the agent is currently running on the cloud baseline, reload it
        # with the freshly pulled file so the user immediately sees the new
        # skills without having to click the toggle.
        if _get_active_source() == "cloud":
            _activate_yaml(path)

        agent = _get_or_create_agent()
        return jsonify({
            "success":     True,
            "local_path":  str(path),
            "local_date":  dt.isoformat() if dt else None,
            "filename":    path.name,
            "message":     "Cloud baseline refreshed from the share folder.",
            "skills":      agent.get_skill_descriptions(),
        })
    except Exception as e:
        traceback.print_exc()
        return jsonify({"success": False, "error": str(e)}), 500


@bt_chatbot_bp.route("/load_local_skills_yaml", methods=["GET"])
def load_local_skills_yaml():
    """
    Return the raw contents of a local YAML for the side-panel editor.

    Query string:
      ?source=cloud|user   (default = current active source)

    The editor uses `source=user` to pre-fill from the user's previous
    edits, and `source=cloud` to start from the pristine baseline.
    """
    requested = (request.args.get("source") or "").strip().lower() or _get_active_source()
    try:
        if requested == "user":
            local_path, local_date = _latest_user_yaml()
        else:
            requested = "cloud"
            local_path, local_date = _latest_cloud_baseline()

        if local_path is None:
            return jsonify({
                "success":   False,
                "error":     ("No customised configuration on disk yet."
                              if requested == "user"
                              else "Cloud baseline not present locally. "
                                   "Connect to VPN and use 'Refresh from share folder'."),
                "source":    requested,
            }), 404

        data = _read_yaml_file(local_path)
        return jsonify({
            "success":    True,
            "source":     requested,
            "local_path": str(local_path),
            "local_date": local_date.isoformat() if local_date else None,
            "filename":   local_path.name,
            "skills":     data,
        })
    except Exception as e:
        traceback.print_exc()
        return jsonify({"success": False, "error": str(e)}), 500


def _sanitise_skill_payload(skills_dict) -> tuple[dict, str]:
    """
    Validate the per-skill payload sent by the editor. Returns (cleaned, "")
    on success or ({}, error_message) on validation failure. Only known
    fields are persisted; the top-level skill key plus name + description
    must be non-empty strings; lists are coerced.
    """
    if not isinstance(skills_dict, dict) or not skills_dict:
        return ({}, "Request body must contain a non-empty 'skills' object.")

    allowed_keys = {"name", "description", "keywords", "exclusive", "expert_rules"}
    cleaned: dict = {}
    for key, val in skills_dict.items():
        if not isinstance(key, str) or not key.strip():
            return ({}, "Skill ID is required.")
        if not isinstance(val, dict):
            return ({}, f"Skill '{key}' must be an object.")

        row = {k: v for k, v in val.items() if k in allowed_keys}

        name = (row.get("name") or "").strip() if isinstance(row.get("name"), str) else ""
        desc = (row.get("description") or "").strip() if isinstance(row.get("description"), str) else ""
        if not name:
            return ({}, f"Skill '{key}': display name is required.")
        if not desc:
            return ({}, f"Skill '{key}': description is required.")
        row["name"] = name
        row["description"] = desc

        for list_key in ("keywords", "exclusive"):
            if list_key in row:
                v = row[list_key]
                # rstrip ONLY: leading whitespace can be load-matching-
                # critical (the cloud baseline uses entries like
                # " ------- RESUME FLOW" or " [prvDpTlcConfigSendTlcConfigCmd]"
                # where the leading space is part of the literal log
                # prefix). Trailing whitespace is almost always accidental
                # (user typed a trailing space after the keyword) and is
                # still cleaned.
                if isinstance(v, list):
                    raw_items = (str(x).rstrip() for x in v)
                elif v:
                    raw_items = (str(v).rstrip(),)
                else:
                    raw_items = ()
                cleaned_list = [s for s in raw_items if s]
                # Match the cloud baseline: omit the field entirely when
                # it has no entries (no `exclusive: []` placeholder).
                if cleaned_list:
                    row[list_key] = cleaned_list
                else:
                    row.pop(list_key, None)

        # expert_rules is stored in YAML as a single string. The structured
        # editor sends EITHER:
        #
        #   {"preamble": "free-form text", "items": ["1st", "2nd"]}
        #     → joined as:
        #         <preamble>
        #         1. 1st
        #         2. 2nd
        #
        #   "raw string"            (legacy — passed through verbatim)
        #   ["item1", "item2"]      (legacy — flat numbered list, no preamble)
        rules = row.get("expert_rules")
        if isinstance(rules, dict):
            # Items can be either:
            #   * a plain string  → auto-numbered with the next integer
            #   * a dict {prefix, text} → emitted with the original prefix
            #     verbatim (preserves cloud-baseline numbering such as
            #     "2-1.", "2-2.", "3-1." for section sub-steps)
            preamble = str(rules.get("preamble", "") or "")
            raw_items = rules.get("items") or []
            items: list[tuple[str | None, str]] = []
            if isinstance(raw_items, list):
                for entry in raw_items:
                    if isinstance(entry, dict):
                        pref = entry.get("prefix")
                        pref_str = str(pref).strip() if pref is not None else None
                        text = str(entry.get("text", "") or "")
                    else:
                        pref_str = None
                        text = str(entry)
                    if text.strip():
                        items.append((pref_str or None, text))

            # Assign auto-numbered integer prefixes to items that came in
            # without one. The next-int pool starts above the largest
            # explicit integer prefix already in use, so a list mixing
            # "1, 2-1, 2-2, 3" with one fresh entry will yield "4" — not
            # collide with an existing "2".
            max_int = 0
            for pref_str, _ in items:
                if pref_str and pref_str.isdigit():
                    try:
                        max_int = max(max_int, int(pref_str))
                    except ValueError:
                        pass

            parts: list[str] = []
            if preamble.strip():
                parts.append(preamble)
            for pref_str, text in items:
                if not pref_str:
                    max_int += 1
                    pref_str = str(max_int)
                parts.append(f"{pref_str}. {text}")
            joined = "\n".join(parts)
        elif isinstance(rules, list):
            items_str = [str(s).strip() for s in rules if str(s).strip()]
            joined = "\n".join(
                f"{i}. {item}" for i, item in enumerate(items_str, start=1)
            )
        elif isinstance(rules, str):
            joined = rules.strip()
        else:
            joined = ""
        # Expert rules are required. Reject the whole save if any skill
        # would end up with an empty rules block.
        if not joined.strip():
            return ({}, f"Skill '{key}': expert rules are required.")
        # Append a trailing newline whenever there is any content so the
        # str representer sees "\n" and emits the YAML literal block "|"
        # style — even when there's only a single short item. Without
        # this, "1. rfe" would round-trip as `expert_rules: "1. rfe"`
        # (double-quoted), inconsistent with every other skill.
        if not joined.endswith("\n"):
            joined += "\n"
        row["expert_rules"] = joined

        cleaned[key.strip()] = row

    if not cleaned:
        return ({}, "No valid skills found in the request body.")
    return (cleaned, "")


@bt_chatbot_bp.route("/save_local_skills_yaml", methods=["POST"])
def save_local_skills_yaml():
    """
    Persist edits made in the side-panel structured form to the local
    `user/` overrides directory (a NEW dated file for today). The
    `cloud/` baseline is NEVER modified; uploads back to the share
    folder happen only when the user explicitly clicks "Upload".

    Saving always switches the active source to "user" so the agent
    starts using the edits immediately.

    Request JSON:
      { "skills": { "<skill_key>": { name, description, keywords, exclusive, expert_rules } } }
    """
    data = request.get_json(silent=True) or {}
    cleaned, err = _sanitise_skill_payload(data.get("skills"))
    if err:
        return jsonify({"success": False, "error": err}), 400

    try:
        target = _persist_user_yaml_snapshot(cleaned)

        _set_active_source("user")
        skills = _activate_yaml(target)
        session["yaml_modified"] = True
        session["yaml_modified_path"] = str(target)

        return jsonify({
            "success":       True,
            "active_source": "user",
            "local_path":    str(target),
            "local_date":    None,  # filename carries the date
            "filename":      target.name,
            "message":       f"Saved {len(cleaned)} skill(s) to {target.name}.",
            "skills":        skills,
        })
    except Exception as e:
        traceback.print_exc()
        return jsonify({"success": False, "error": str(e)}), 500


@bt_chatbot_bp.route("/delete_local_skill", methods=["POST"])
def delete_local_skill():
    """
    Remove a single skill from whichever local source is currently active
    and save the result under today's dated filename in `user/`. Always
    flips the active source to "user".

    Request JSON: { "skill_key": "Roaming" }
    """
    data = request.get_json(silent=True) or {}
    skill_key = (data.get("skill_key") or "").strip()
    if not skill_key:
        return jsonify({
            "success": False,
            "error":   "skill_key is required.",
        }), 400

    try:
        # Start from the user copy if it exists, otherwise from the cloud
        # baseline — the resulting file always lands in user/ and becomes
        # the new active configuration.
        src_path, _ = _latest_user_yaml()
        if src_path is None:
            src_path, _ = _latest_cloud_baseline()
        if src_path is None:
            return jsonify({
                "success": False,
                "error":   "No local skill YAML to edit.",
            }), 404

        existing = _read_yaml_file(src_path)
        if skill_key not in existing:
            return jsonify({
                "success": False,
                "error":   f"Skill '{skill_key}' is not present in the active configuration.",
            }), 404

        existing.pop(skill_key, None)
        target = _persist_user_yaml_snapshot(existing)

        _set_active_source("user")
        skills = _activate_yaml(target)
        session["yaml_modified"] = True
        session["yaml_modified_path"] = str(target)

        return jsonify({
            "success":       True,
            "active_source": "user",
            "local_path":    str(target),
            "filename":      target.name,
            "message":       f"Removed skill '{skill_key}'.",
            "skills":        skills,
        })
    except Exception as e:
        traceback.print_exc()
        return jsonify({"success": False, "error": str(e)}), 500


@bt_chatbot_bp.route("/upload_modified_yaml", methods=["POST"])
def upload_modified_yaml():
    """
    Push the user's customised YAML to the share folder so the Wireless CE
    team can incorporate the tuning. Uploads ONLY ever come from the
    `user/` overrides directory — the cloud baseline is never re-uploaded
    back to itself.
    """
    import shutil as _shutil
    from pathlib import Path as _P

    try:
        local_path_str = session.get("yaml_modified_path") or ""
        local_path = _P(local_path_str) if local_path_str else None
        if local_path is None or not local_path.exists():
            latest, _ = _latest_user_yaml()
            local_path = latest
        if local_path is None or not local_path.exists():
            return jsonify({
                "success": False,
                "error":   "No customised skill YAML was found to upload.",
            }), 404

        cloud_dir_str = _resolve_cloud_skills_dir()
        if not cloud_dir_str:
            return jsonify({
                "success": False,
                "error":   "Shared skill folder is unreachable; please retry on VPN.",
            }), 503

        # Upload under a contributions sub-folder so cloud "latest" detection
        # still ranks team-approved revisions; reviewers promote files to the
        # top-level skills_config folder once vetted.
        contrib_dir = _P(cloud_dir_str) / "user_contributions"
        try:
            contrib_dir.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            return jsonify({
                "success": False,
                "error":   f"Cannot create contributions folder on share: {e}",
            }), 500

        import getpass
        import re as _re
        user = _re.sub(r"[^A-Za-z0-9_.-]+", "_",
                       (getpass.getuser() or os.environ.get("USERNAME") or "anon"))
        target = contrib_dir / f"{user}__{local_path.name}"
        _shutil.copy2(str(local_path), str(target))

        return jsonify({
            "success":     True,
            "uploaded_to": str(target),
            "message":     "Thank you. Your modified configuration has been "
                           "uploaded for review.",
        })
    except Exception as e:
        traceback.print_exc()
        return jsonify({"success": False, "error": str(e)}), 500