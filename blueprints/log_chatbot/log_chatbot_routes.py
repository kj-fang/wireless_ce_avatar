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
from services.log_chatbot_service import WifiLogAgentSystem, load_skills_from_data_dir, get_builtin_skills, build_skill_file_map, load_skills_from_yaml
from utils.etl_utils import extract_time_from_description
from utils.issue_time_utils import (
    parse_issue_time_string,
    read_log_time_range,
    resolve_issue_time,
    format_issue_time,
)
from utils.issue_time_ai import build_issue_time_suggestions, organize_issue_context, realign_times_to_log
from utils.timezone_utils import (
    get_effective_timezone,
    taiwan_to_local,
    format_tz_label,
    to_iana_timezone,
    set_manual_override,
    get_manual_override,
    get_system_timezone,
    get_issue_time_basis,
    VALID_ISSUE_TIME_BASES,
)
from services import feedback_service
from services import history_service
from services import chat_jobs
from services import gather_service

log_chatbot_bp = Blueprint("log_chatbot", __name__, url_prefix="/log_chatbot")

# Server-side store: session_id -> WifiLogAgentSystem instance
_chatbot_instances: dict = {}


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


def _invalidate_issue_context_caches() -> None:
    """Drop the DERIVED issue-context caches so they get recomputed from the
    current ``selected_files`` / ``case_context``.

    These three caches are computed FROM the raw case sources but live
    independently in the session, so they outlive the data they were derived
    from. Without this, starting a SECOND analysis (download_result -> /prepare
    -> /log_chatbot/?auto_run=analyze_all) without first clicking "Back to
    Avatar" makes the new run inherit the PREVIOUS run's attachment time,
    resolved issue time and LLM-organized description. Call this whenever a new
    analysis is entered so the caches are rebuilt from the fresh case data.
    """
    for key in (
        "_attachment_time_cache",     # parsed attachment subtitle time
        "_resolved_issue_time_cache",  # log_path -> resolved issue_time
        "_issue_ai_quick",            # LLM-organized description + issue times
    ):
        session.pop(key, None)


def _export_agent_context(agent) -> list:
    """Snapshot the agent's model-facing conversation. Never raises.

    Persisting the context is a convenience — without it a conversation still
    resumes, just from result text. The caller runs inside the turn's worker
    try/except, where an exception would mark an already-successful turn as
    failed, so this swallows its own errors rather than costing the user a
    finished analysis.
    """
    try:
        return agent.export_conversation_context()
    except Exception as e:
        print(f"[history] context export failed: {e}")
        return []


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
    Borrows client/model from app_config.log_chatbot_agent which is
    initialised at app startup (set_up_app.py -> set_up()).

    skip_prime: if True, skip the auto prime_with_context on new session creation.
                Use this when the caller will immediately call prime_with_context itself.
    """
    sid = session.get("chatbot_session_id")
    if not sid or sid not in _chatbot_instances:
        sid = str(uuid.uuid4())
        session["chatbot_session_id"] = sid

    if sid not in _chatbot_instances:
        base = app_config.log_chatbot_agent
        if base is None:
            # Fallback: try to build from llm_helper directly
            llm_helper = app_config.llm_helper
            if llm_helper is None or llm_helper.client is None:
                raise RuntimeError(
                    "Log Chatbot Agent is not available. "
                    "The app may not have an API key configured."
                )
            base = WifiLogAgentSystem(
                client=llm_helper.client,
                model=getattr(llm_helper, "model", "gpt-4.1"),
                skills=getattr(llm_helper, "skills", None),
            )
        # Create a fresh per-session instance sharing the same client + skills
        agent = WifiLogAgentSystem(
            client=base.client,
            model=base.model,
            skills=base.skills,   # reuse pre-loaded skills, no disk re-read
        )
        # Inherit ACE runner from the boot-time base agent so playbook blocks
        # are injected into per-session prompts.
        ace_runner = getattr(base, "ace_runner", None)
        if ace_runner is not None:
            agent.attach_ace(ace_runner)
        # Auto-populate log path from the last analysis. Use the SAME sources the
        # sidebar reads (session first, then the process-global), so the two
        # can't diverge. On a browser-back / bfcache re-run prepare()/set_log()
        # don't re-execute, and run-1's chat() popped this agent out of
        # _chatbot_instances — so this lazy rebuild is the only path source. If
        # it consulted only the process-global (which back_to_avatar / another
        # tab / a restart can empty) while session['chatbot_log_path'] still
        # held the file, the sidebar would look right but the agent would have
        # no log → "No log file loaded". Reading the session key too keeps them
        # in agreement. The session value wins so a per-session log can't be
        # clobbered by another tab / case that moved the process-global on.
        restored_log_path = (
            session.get("chatbot_log_path")
            or app_config.last_analyzed_log_path
        )
        if restored_log_path:
            agent.current_log_path = restored_log_path
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


def _resume_agent_for(conversation_id: str):
    """
    Return the agent to use for ``conversation_id``.

    A finished background analysis keeps its own (detached) agent, which holds
    the full tool-grounded conversation history. When the user continues that
    same conversation we adopt that agent — far higher fidelity than rebuilding
    context from saved text. Running jobs are NOT adopted (their agent is busy
    on a background thread); the caller falls back to a fresh session agent.
    """
    job = chat_jobs.get_job(conversation_id)
    if job is not None and getattr(job, "agent", None) is not None and job.status != "running":
        sid = session.get("chatbot_session_id")
        if not sid:
            sid = str(uuid.uuid4())
            session["chatbot_session_id"] = sid
        _chatbot_instances[sid] = job.agent
        return job.agent
    return _get_or_create_agent()


def _terminal_sse(job, kind: str, payload) -> str:
    """Format a job's terminal event (done / error) as one SSE frame."""
    if kind == "done":
        return ("data: " + json.dumps(
            {"type": "done", "turn_id": job.turn_id,
             "conversation_id": job.conversation_id, "result": payload},
            ensure_ascii=False) + "\n\n")
    return ("data: " + json.dumps(
        {"type": "error", "content": payload}, ensure_ascii=False) + "\n\n")


def _job_sse(job):
    """
    Shared SSE generator for a chat job: emit the steps buffered so far, then
    follow live steps until the job reaches a terminal state. Used by both the
    original /chat request and the /history/stream reconnect endpoint, so the
    two render identically.
    """
    import queue as _q
    q, replay, terminal = chat_jobs.subscribe(job)
    try:
        for step in replay:
            yield "data: " + json.dumps({"type": "step", "step": step}, ensure_ascii=False) + "\n\n"
        if terminal is not None:
            yield _terminal_sse(job, terminal[0], terminal[1])
            return
        while True:
            try:
                kind, payload = q.get(timeout=120)
				
				# --------------------------------------------------
                # Auto-save LLM report to JSON next to the source file.
                # Triggered whenever the agent returns a final report
                # (both CLI --auto-llm and manual web UI flows).
                # Save location priority:
                #   1) folder of sendto_report_path  (CLI flow)
                #   2) folder of current_log_path    (web UI flow)
                # --------------------------------------------------
                if isinstance(payload, dict) and payload.get("type") == "report":
                    try:
                        import os as _os
                        from datetime import datetime as _dt
                        _report_dir = ""
                        _sendto_rp = _save_sendto_report_path
                        _log_path  = _save_log_path
                        if _sendto_rp:
                            _report_dir = _os.path.dirname(_sendto_rp)
                        elif _log_path:
                            _report_dir = _os.path.dirname(_log_path)
                        if _report_dir and _os.path.isdir(_report_dir):
                            _ts = _dt.now().strftime("%Y%m%d_%H%M%S")
                            _out_path = _os.path.join(_report_dir, f"llm_report_{_ts}.json")
                            _save_data = {
                                "turn_id": turn_id,
                                "conversation_id": conversation_id,
                                "user_message": user_message,
                                "issue_time": payload.get("issue_time"),
                                "report": payload.get("data", {}),
                                "log_path": _log_path,
                                "saved_at": _dt.now().isoformat(),
                            }
                            with open(_out_path, "w", encoding="utf-8") as _f:
                                json.dump(_save_data, _f, ensure_ascii=False, indent=2)
                            print(f"💾 [llm-report] Saved → {_out_path}")
                        else:
                            print(f"⚠️ [llm-report] No valid directory to save report (sendto_rp={_sendto_rp!r}, log_path={_log_path!r})")
                    except Exception as _e:
                        print(f"⚠️ [llm-report] Save failed: {_e}")
            except _q.Empty:
                yield "data: " + json.dumps({"type": "error", "content": "Chat timed out."}) + "\n\n"
                return
            if kind == "step":
                yield "data: " + json.dumps({"type": "step", "step": payload}, ensure_ascii=False) + "\n\n"
            else:
                yield _terminal_sse(job, kind, payload)
                return
    finally:
        chat_jobs.unsubscribe(job, q)


# ------------------------------------------------------------------
# Pages
# ------------------------------------------------------------------
@log_chatbot_bp.route("/", methods=["GET"])
def index():
    suggested_log = app_config.last_analyzed_log_path or ""
    issue_desc = ""
    try:
        ctx = _extract_issue_context()
        issue_desc = ctx.get("description", "")
    except Exception:
        pass
    return render_template("log_chatbot.html", suggested_log=suggested_log, issue_description=issue_desc)


# ------------------------------------------------------------------
# API: open native file browser and return selected path
# ------------------------------------------------------------------
@log_chatbot_bp.route("/browse", methods=["GET"])
def browse():
    """Open a native Windows file dialog and return the selected .log path."""
    result = {"path": ""}

    def _open_dialog():
        root = tk.Tk()
        root.withdraw()
        root.wm_attributes("-topmost", True)
        path = filedialog.askopenfilename(
            title="Select log file",
            filetypes=[("Log files", "*.log"), ("All files", "*.*")],
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
@log_chatbot_bp.route("/set_log", methods=["POST"])
def set_log():
    # These paths are auto-detected from the report zip structure after extraction
    # (report_zip: <stem>/<stem>/artifacts/*.json  +  <stem>/<stem>/*report*.txt).
    is_agent_zip = session.get('is_agent_zip', False)
    if is_agent_zip:
        _report_path = (session.get("sendto_report_path") or "").strip()
        _artifacts_path = (session.get("sendto_artifacts_path") or "").strip()
        if _report_path:
            print(f'[Agent-zip] Report txt detected: {_report_path}')
            try:
                with open(_report_path, 'r', encoding='utf-8') as _f:
                    _preview = [next(_f).rstrip() for _ in range(3)]
                print(f'[Agent-zip] Report preview (first 3 lines):')
                for _i, _line in enumerate(_preview, 1):
                    print(f'  {_i}: {_line}')
            except StopIteration:
                print(f'[Agent-zip] Report has fewer than 3 lines.')
            except Exception as _e:
                print(f'[Agent-zip] Failed to read report: {_e}')

        if _artifacts_path:
            print(f'[Agent-zip] Artifacts folder detected: {_artifacts_path}')
            try:
                _ext_counts: dict = {}
                for _fname in os.listdir(_artifacts_path):
                    _fpath = os.path.join(_artifacts_path, _fname)
                    if not os.path.isfile(_fpath):
                        continue
                    _ext = os.path.splitext(_fname)[1].lower() or '(no ext)'
                    _ext_counts[_ext] = _ext_counts.get(_ext, 0) + 1
                _total = sum(_ext_counts.values())
                _summary = ', '.join(f'{cnt} {ext}' for ext, cnt in sorted(_ext_counts.items()))
                print(f'[Agent-zip] Artifacts summary: {_total} file(s) — {_summary}')
            except Exception as _e:
                print(f'[Agent-zip] Failed to summarise artifacts folder: {_e}')

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

        # Re-loading the SAME file is not a new case. It used to be treated as
        # one anyway — a fresh conversation id and a wiped agent — so anything
        # that incidentally re-loaded the log (the path field losing focus, a
        # draft restore, returning to the live session) silently split a case
        # into another one-turn conversation and made the next question start
        # from nothing. Continue the thread when the file has not changed.
        same_log = bool(prev_log_path) and prev_log_path == log_path

        agent = _get_or_create_agent(skip_prime=True)
        agent.current_log_path = log_path
        if not same_log:
            agent.reset_conversation()      # fresh conversation for a new file
        ctx = _extract_issue_context()      # re-extract context in case session was updated after agent creation
        # Always prime: prime_with_context falls back to the log file's latest
        # timestamp when ctx has no usable issue time, so the sidebar always
        # gets an issue_time to display (covers the no-session entry path).
        # It also clears conversation_history, so on a same-file re-load the
        # thread is put back afterwards — priming is wanted for its caches and
        # issue-time resolution, not for its side effect on the conversation.
        preserved_history = list(agent.conversation_history or []) if same_log else []
        agent.prime_with_context(**ctx)

        session["chatbot_log_path"] = log_path

        # Sidecar: a NEW log file = a new conversation. Rotate the id (and
        # eagerly create the snapshot file so issue context is captured even
        # if the user never sends a message); keep it for a same-file re-load
        # so the next turn appends to the conversation already on screen.
        new_conv_id = _ensure_feedback_conversation_id(rotate=not same_log)

        if same_log:
            if preserved_history:
                agent.conversation_history = preserved_history
            else:
                # The per-conversation agent is detached from the session slot
                # at the start of every tools run, so by now this is usually a
                # fresh instance with nothing to preserve. Fall back to the
                # conversation's stored context for the same continuity a
                # History click gets.
                try:
                    stored = history_service.get_context(new_conv_id)
                    if stored:
                        agent.import_conversation_context(stored)
                except Exception as _e:
                    print(f"[set_log] context restore skipped: {_e}")
        feedback_service.ensure_conversation(
            conversation_id=new_conv_id,
            session_id=session.get("chatbot_session_id", ""),
            issue=ctx,
            log_path=log_path,
        )

        # Whole-minute span of the log so the sidebar can cap the
        # issue-time capture window at the log's actual length. 0 means
        # "unknown" (no parseable timestamps) — client falls back to a
        # generic cap.
        try:
            log_span_minutes = agent.get_log_span_minutes()
        except Exception:
            log_span_minutes = 0

        # Whether this log carries dates. Time-only logs (e.g. DDD) let the
        # sidebar leave the date fields blank and match Segment2 by time-of-day.
        # Computed first so the log_last_time fallback below knows which format
        # to look for.
        try:
            log_has_date = agent._log_has_date()
        except Exception:
            log_has_date = True

        # Log's last parseable timestamp — offered in the "no issue time" prompt
        # as a one-click anchor ("Use log's last time"). Two paths:
        #   * Dated logs: read_log_time_range (MM/DD/YYYY-HH:MM:SS.fff).
        #   * Time-only logs (DDD/tracefmt): scan the agent's raw cache from
        #     the tail backwards for the last HH:MM:SS occurrence; emit as
        #     "HH:MM:SS.mmm" (no date). read_log_time_range's regex doesn't
        #     match DDD, so without this fallback the button silently no-ops
        #     for every DDD upload.
        log_last_time = ""
        try:
            _first_ts, _last_ts = read_log_time_range(log_path)
            if _last_ts:
                log_last_time = format_issue_time(_last_ts)
            elif log_has_date is False:
                cache = getattr(agent, "_raw_log_cache", None) or []
                _time_re = re.compile(
                    r'(?<!\d)(\d{1,2}):(\d{2}):(\d{2})(?:[:.](\d{1,6}))?(?!\d)'
                )
                for _line in reversed(cache):
                    _m = _time_re.search(_line or "")
                    if not _m:
                        continue
                    _hh, _mm, _ss = (int(_m.group(i)) for i in (1, 2, 3))
                    if not (0 <= _hh <= 23 and 0 <= _mm <= 59 and 0 <= _ss <= 59):
                        continue
                    _raw_ms = _m.group(4)
                    if _raw_ms:
                        _ms = int(_raw_ms.ljust(6, "0")[:6]) // 1000
                        log_last_time = f"{_hh:02d}:{_mm:02d}:{_ss:02d}.{_ms:03d}"
                    else:
                        log_last_time = f"{_hh:02d}:{_mm:02d}:{_ss:02d}"
                    break
        except Exception as _e:
            print(f"⚠️  log_last_time lookup failed: {_e}")
            log_last_time = ""

        return jsonify({
            "success": True,
            "message": f"Log file set: {log_path}",
            "skills": agent.get_skill_descriptions(),
            "issue_time": format_issue_time(agent.issue_time),
            "log_span_minutes": log_span_minutes,
            "log_last_time": log_last_time,
            "log_has_date": log_has_date,
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
@log_chatbot_bp.route("/suggest_issue_times", methods=["POST"])
def suggest_issue_times():
    """Suggest issue time(s) from the user's typed description + a rough browse
    of the loaded log. User-first (explicit times bypass the LLM). The frontend
    must obtain the user's consent before calling this route. The heavy lifting
    lives in ``utils.issue_time_ai``; this handler just marshals request/agent
    state in and jsonifies the result out."""
    data = request.get_json(silent=True) or {}
    text = (data.get("text") or "").strip()
    try:
        agent = _get_or_create_agent()
        log_path = agent.current_log_path or ""
        first_ts, last_ts = read_log_time_range(log_path) if log_path else (None, None)

        # The log's first/last timestamps stay in the log frame (decoder
        # host clock) — same frame the LLM sees in the digest. No shift
        # needed before the model call.
        local_tz_name = get_effective_timezone(log_path) if log_path else ""
        tz_label = format_tz_label(local_tz_name) if local_tz_name else ""
        log_frame_first_ts = None
        log_frame_last_ts = None

        # Cached raw log lines feed the rough-browse digest (best-effort).
        log_lines = []
        try:
            if not agent._ensure_raw_log_cache():
                log_lines = agent._raw_log_cache or []
        except Exception:
            log_lines = []

        # An empty description is allowed — the AI can still infer the issue
        # time from the log alone (a description just improves accuracy). Only
        # block when there's truly nothing to analyze (no text AND no log).
        if not text and not log_lines:
            return jsonify({"success": False,
                            "error": "Type a problem description or load a log first."}), 400

        # Detect whether the loaded log carries dates (Wi-Fi ETL) or is
        # time-only (DDD / tracefmt). Threading this into
        # build_issue_time_suggestions makes the LLM prompt + the returned
        # suggestion shape honest about it: time-only logs yield time-only
        # suggestions with no fabricated date placeholder.
        try:
            log_has_date = agent._log_has_date()
        except Exception:
            log_has_date = None

        payload = build_issue_time_suggestions(
            text=text,
            log_lines=log_lines,
            first_ts=first_ts,
            last_ts=last_ts,
            llm_client=getattr(agent, "client", None),
            llm_model=getattr(agent, "model", None),
            log_has_date=log_has_date,
            log_frame_first_ts=log_frame_first_ts,
            log_frame_last_ts=log_frame_last_ts,
            tz_label=tz_label,
        )
        return jsonify(payload), (200 if payload.get("success") else 503)
    except Exception as e:
        traceback.print_exc()
        return jsonify({"success": False, "error": str(e)}), 500


# ------------------------------------------------------------------
# API: chat
# ------------------------------------------------------------------
@log_chatbot_bp.route("/chat", methods=["POST"])
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
    # Cap at 1440 (24 h): the frontend already caps the slider at the actual
    # log span, so the only way a larger value reaches this route is a
    # bypassed / buggy / malicious client. 24 h is well above any realistic
    # single-event window, so the tighter server-side hard cap doesn't
    # constrain legitimate use.
    issue_time_window_minutes = None
    if "issue_time_window_minutes" in data:
        try:
            issue_time_window_minutes = max(0, min(1440, int(data.get("issue_time_window_minutes"))))
        except (TypeError, ValueError):
            issue_time_window_minutes = None

    try:
        # Resolve the conversation first so we can adopt a finished job's agent
        # (full tool context) when the user continues a just-analysed thread.
        conversation_id = _ensure_feedback_conversation_id()
        agent = _resume_agent_for(conversation_id)
        if issue_time_window_minutes is not None:
            agent.issue_time_window_minutes = issue_time_window_minutes
        # Backstop: if the resolved agent lost its log path (e.g. a fresh agent
        # rebuilt on a browser-back re-run where prepare()/set_log() didn't
        # run), recover it from the SAME sources the sidebar uses before the
        # guard below, so a valid in-session log isn't reported as missing.
        if not agent.current_log_path:
            agent.current_log_path = (
                session.get("chatbot_log_path")
                or app_config.last_analyzed_log_path
                or ""
            )
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
                # Keep the customer-tz annotation in sync with the new
                # sidebar value. The picker always shows the log-frame
                # value (matches .log content), so the same instant on the
                # customer's wall clock is just taiwan_to_local at the
                # detected tz. Skip on time-only or when no tz is known.
                if parsed and not is_time_only and agent.current_log_path:
                    try:
                        log_tz = get_effective_timezone(agent.current_log_path) or ""
                        if log_tz:
                            agent.issue_time_tz = log_tz
                            agent.issue_time_customer = taiwan_to_local(parsed, log_tz)
                        else:
                            agent.issue_time_tz = ""
                            agent.issue_time_customer = None
                    except Exception as _e:
                        print(f"[chat] sidebar issue_time customer refresh skipped ({_e})")
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
        turn_id = str(uuid.uuid4())
        turn_started_at = datetime.now()
        try:
            _issue_ctx_for_snapshot = _extract_issue_context()
        except Exception:
            _issue_ctx_for_snapshot = {}

        # Usage analytics: on every Send, capture the entry session (user name,
        # date, CASE NUMBER + case summary) and the asked question into the
        # shared Gather folder for later DB ingestion. Non-blocking; never
        # raises, so it can't affect the chat path.
        try:
            gather_service.record_send(
                conversation_id=conversation_id,
                workflow_id=session.get("gather_workflow_id", ""),
                session_id=session_id,
                user_message=user_message,
                issue=_issue_ctx_for_snapshot,
                log_path=getattr(agent, "current_log_path", "") or "",
                issue_time=format_issue_time(agent.issue_time),
                issue_time_window_minutes=getattr(agent, "issue_time_window_minutes", None),
                domain="wifi",
                turn_id=turn_id,
            )
        except Exception:
            pass

        # Use the mode flag sent by the frontend toggle.
        use_tools = bool(data.get("use_tools", False))

        if use_tools:
            collected_steps: list = []

            # Register a background job that OWNS this analysis, then detach the
            # agent from the session slot. The run keeps going — and stays
            # uncorrupted — even if the user switches to another conversation
            # mid-analysis (any later session use just creates a fresh agent).
            # The job buffers every step so a reconnecting client can replay +
            # follow it via /history/stream.
            job = chat_jobs.start_job(
                conversation_id=conversation_id,
                turn_id=turn_id,
                title=user_message,
                agent=agent,
                domain="",
            )
            if session_id:
                _chatbot_instances.pop(session_id, None)

            def step_cb(step):
                try:
                    if isinstance(step, dict):
                        # Stamp how far into the turn this step arrived. The
                        # live card times itself from the browser clock; a
                        # replay months later cannot, so the offset travels
                        # with the step into history. The copy keeps the
                        # object published to live subscribers untouched.
                        elapsed_ms = int(
                            (datetime.now() - turn_started_at).total_seconds() * 1000)
                        collected_steps.append({**step, "ts_ms": elapsed_ms})
                except Exception:
                    pass
                chat_jobs.publish_step(job, step)

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
                    # Cost accounting: token counts only exist once the LLM has
                    # finished, so this is a second Gather write on top of the
                    # record_send() that opened this turn.
                    try:
                        gather_service.record_usage(
                            conversation_id=conversation_id,
                            workflow_id=session.get("gather_workflow_id", ""),
                            model=getattr(agent, "model", "") or "",
                            usage=getattr(agent, "last_turn_usage", None),
                            issue=_issue_ctx_for_snapshot,
                            domain="wifi",
                            turn_id=turn_id,
                            latency_ms=int((datetime.now() - turn_started_at).total_seconds() * 1000),
                        )
                    except Exception:
                        pass
                    # Persist BEFORE signalling done so any subscriber that
                    # refreshes its history list on 'done' already sees this
                    # turn. feedback is vote-gated; history always persists.
                    feedback_service.record_turn(
                        session_id=session_id,
                        conversation_id=conversation_id,
                        turn_id=turn_id,
                        user_message=user_message,
                        agent_result=result,
                        steps=collected_steps,
                        mode="tools",
                        duration_ms=int((datetime.now() - turn_started_at).total_seconds() * 1000),
                        issue=_issue_ctx_for_snapshot,
                        log_path=getattr(agent, "current_log_path", "") or "",
                        parent_message_id=parent_message_id,
                    )
                    history_service.record_turn(
                        conversation_id=conversation_id,
                        session_id=session_id,
                        turn_id=turn_id,
                        user_message=user_message,
                        agent_result=result,
                        mode="tools",
                        issue=_issue_ctx_for_snapshot,
                        log_path=getattr(agent, "current_log_path", "") or "",
                        issue_time=format_issue_time(agent.issue_time),
                        # Keep the reasoning trace too, so reopening this
                        # conversation shows how the answer was reached and
                        # not only what it was.
                        steps=collected_steps,
                        # And the model-facing conversation, so a follow-up
                        # asked tomorrow is answered by something that still
                        # has the evidence, not just the conclusions.
                        agent_context=_export_agent_context(agent),
                    )
                    chat_jobs.finish_job(job, result)
                except Exception as exc:
                    error_tb = traceback.format_exc()
                    print(f"❌ Chat-with-tools thread error:\n{error_tb}")
                    try:
                        gather_service.record_turn_status(
                            conversation_id=conversation_id,
                            turn_id=turn_id,
                            status="failed",
                            workflow_id=session.get("gather_workflow_id", ""),
                            issue=_issue_ctx_for_snapshot,
                            domain="wifi",
                            latency_ms=int((datetime.now() - turn_started_at).total_seconds() * 1000),
                            error_code=type(exc).__name__,
                        )
                    except Exception:
                        pass
                    chat_jobs.fail_job(job, str(exc))

            t = threading.Thread(target=run_chat_with_tools, daemon=True)
            t.start()

			# Capture session values NOW (inside request context) so the
            # event_stream generator can use them after the context ends.
            _save_sendto_report_path = (session.get("sendto_report_path") or "").strip()
            _save_log_path = getattr(agent, "current_log_path", "") or ""

            # The original request streams the job exactly like a reconnect
            # would (replay buffered steps, then follow to done/error).
            return Response(
                _job_sse(job),
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
            # Cost accounting — see the tools branch above.
            try:
                gather_service.record_usage(
                    conversation_id=conversation_id,
                    workflow_id=session.get("gather_workflow_id", ""),
                    model=getattr(agent, "model", "") or "",
                    usage=getattr(agent, "last_turn_usage", None),
                    issue=_issue_ctx_for_snapshot,
                    domain="wifi",
                    turn_id=turn_id,
                    latency_ms=int((datetime.now() - turn_started_at).total_seconds() * 1000),
                )
            except Exception:
                pass

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
            )
            # Local browsable history (always persists).
            history_service.record_turn(
                conversation_id=conversation_id,
                session_id=session_id,
                turn_id=turn_id,
                user_message=user_message,
                agent_result=result,
                mode="simple",
                issue=_issue_ctx_for_snapshot,
                log_path=getattr(agent, "current_log_path", "") or "",
                issue_time=format_issue_time(agent.issue_time),
            )

            def generate():
                yield f"data: {json.dumps({'type': 'done', 'turn_id': turn_id, 'conversation_id': conversation_id, 'result': result}, ensure_ascii=False)}\n\n"

            return Response(generate(), mimetype="text/event-stream")
    except Exception as e:
        error_traceback = traceback.format_exc()
        print(f"❌ Chatbot error:\n{error_traceback}")
        try:
            if conversation_id and turn_id:
                gather_service.record_turn_status(
                    conversation_id=conversation_id,
                    turn_id=turn_id,
                    status="failed",
                    workflow_id=session.get("gather_workflow_id", ""),
                    issue=locals().get("_issue_ctx_for_snapshot") or {},
                    domain="wifi",
                    error_code=type(e).__name__,
                )
        except Exception:
            pass

        def generate_error():
            yield f"data: {json.dumps({'type': 'error', 'content': str(e)}, ensure_ascii=False)}\n\n"

        return Response(generate_error(), mimetype="text/event-stream")


# ------------------------------------------------------------------
# API: reset conversation
# ------------------------------------------------------------------
@log_chatbot_bp.route("/reset", methods=["POST"])
def reset():
    try:
        agent = _get_or_create_agent()
        agent.reset_conversation()
        return jsonify({"success": True, "message": "Conversation reset."})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


# ------------------------------------------------------------------
# API: stop the in-flight tools-mode analysis
#
# Signals the running background job's agent (via its cancel_event) to bail
# out at the next reasoning-step boundary. The job then finishes normally and
# its SSE stream emits a terminal "done" with a "stopped" notice. Idempotent:
# a no-op when nothing is running.
#
# The target conversation is resolved from the caller's OWN session (the
# frontend only learns conversation_id on the terminal 'done' event, so a Stop
# clicked mid-stream usually sends an empty id). This scopes cancellation to
# this session's job only — never other tabs'/users' running analyses.
# ------------------------------------------------------------------
@log_chatbot_bp.route("/chat/stop", methods=["POST"])
def chat_stop():
    try:
        data = request.get_json(silent=True) or {}
        conversation_id = (data.get("conversation_id") or "").strip()
        if not conversation_id:
            conversation_id = (session.get("feedback_conversation_id") or "").strip()
        job = chat_jobs.get_job(conversation_id) if conversation_id else None
        stopped = chat_jobs.request_cancel(conversation_id) if conversation_id else False
        if stopped and job is not None:
            try:
                gather_service.record_turn_status(
                    conversation_id=conversation_id,
                    turn_id=getattr(job, "turn_id", ""),
                    status="cancelled",
                    workflow_id=session.get("gather_workflow_id", ""),
                    issue=_extract_issue_context(),
                    domain="wifi",
                )
            except Exception:
                pass
        return jsonify({"success": True, "stopped": bool(stopped)})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


# ------------------------------------------------------------------
# API: local conversation history (Gemini / Claude style sidebar)
#
# Every chat turn is persisted to <avatarfiles_dir>/history/<id>.json by
# history_service. These endpoints let the sidebar list / load / delete
# those conversations. All are read/written on the local machine only.
# ------------------------------------------------------------------
@log_chatbot_bp.route("/history/list", methods=["GET"])
def history_list():
    try:
        conversations = history_service.list_conversations()
        # Merge in-memory running jobs so the sidebar can show a ⏳ marker:
        #   * a persisted conversation that's mid-analysis  -> running: True
        #   * a brand-new first analysis not yet on disk     -> synthetic entry
        try:
            running = {j["conversation_id"]: j for j in chat_jobs.active_summaries(domain="")}
            if running:
                seen = set()
                for c in conversations:
                    cid = c.get("conversation_id")
                    seen.add(cid)
                    if cid in running:
                        c["running"] = True
                for cid, j in running.items():
                    if cid not in seen:
                        conversations.append({
                            "conversation_id": cid,
                            "title": j.get("title") or "New conversation",
                            "created_at": "",
                            # Stamp "now" so a brand-new running conversation
                            # sorts to the top of the running group.
                            "updated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
                            "turn_count": j.get("step_count", 0),
                            "log_path": "",
                            "pinned": False,
                            "running": True,
                        })
            # Final ordering (highest priority last in this stable-sort chain):
            #   1. Pinned conversations at the very top.
            #   2. Still-running conversations next (below pins, above the rest).
            #   3. Everyone else — all newest-first within each group.
            conversations.sort(key=lambda c: c.get("updated_at") or "", reverse=True)
            conversations.sort(key=lambda c: bool(c.get("running")), reverse=True)
            conversations.sort(key=lambda c: bool(c.get("pinned")), reverse=True)
        except Exception:
            pass
        return jsonify({"success": True, "conversations": conversations})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@log_chatbot_bp.route("/history/stream", methods=["GET"])
def history_stream():
    """
    Re-attach to a conversation's live analysis (Server-Sent Events).

    Replays the steps buffered so far, then follows new steps until the job
    reaches done/error — so switching back to a running conversation shows its
    progress catching up in real time. If there's no active/recent job for the
    conversation, emits a single 'idle' event and closes (the client then just
    renders the saved turns).
    """
    conversation_id = (request.args.get("conversation_id") or "").strip()
    headers = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}
    job = chat_jobs.get_job(conversation_id) if conversation_id else None
    if job is None:
        def _idle():
            yield "data: " + json.dumps({"type": "idle"}) + "\n\n"
        return Response(_idle(), mimetype="text/event-stream", headers=headers)
    return Response(_job_sse(job), mimetype="text/event-stream", headers=headers)


@log_chatbot_bp.route("/history/get", methods=["GET"])
def history_get():
    conversation_id = (request.args.get("conversation_id") or "").strip()
    if not conversation_id:
        return jsonify({"success": False, "error": "conversation_id is required"}), 400
    conv = history_service.get_conversation(conversation_id, with_steps=True)
    if conv is None:
        return jsonify({"success": False, "error": "Conversation not found"}), 404
    return jsonify({"success": True, "conversation": conv})


@log_chatbot_bp.route("/history/delete", methods=["POST"])
def history_delete():
    data = request.get_json(silent=True) or {}
    conversation_id = (data.get("conversation_id") or "").strip()
    if not conversation_id:
        return jsonify({"success": False, "error": "conversation_id is required"}), 400
    removed = history_service.delete_conversation(conversation_id)
    # If the deleted conversation is the one currently active, drop the
    # session pointer so the next turn starts a brand-new conversation.
    if removed and (session.get("feedback_conversation_id") or "") == conversation_id:
        session.pop("feedback_conversation_id", None)
    return jsonify({"success": bool(removed)})


@log_chatbot_bp.route("/history/rename", methods=["POST"])
def history_rename():
    data = request.get_json(silent=True) or {}
    conversation_id = (data.get("conversation_id") or "").strip()
    title = (data.get("title") or "").strip()
    if not conversation_id:
        return jsonify({"success": False, "error": "conversation_id is required"}), 400
    if not title:
        return jsonify({"success": False, "error": "title is required"}), 400
    ok = history_service.rename_conversation(conversation_id, title)
    return jsonify({"success": bool(ok)})


@log_chatbot_bp.route("/history/pin", methods=["POST"])
def history_pin():
    data = request.get_json(silent=True) or {}
    conversation_id = (data.get("conversation_id") or "").strip()
    pinned = bool(data.get("pinned"))
    if not conversation_id:
        return jsonify({"success": False, "error": "conversation_id is required"}), 400
    ok = history_service.set_pinned(conversation_id, pinned)
    return jsonify({"success": bool(ok), "pinned": pinned})



@log_chatbot_bp.route("/history/load", methods=["POST"])
def history_load():
    """
    Resume a saved conversation: re-point the session at its id, restore the
    log file + issue context into the per-session agent (so follow-up
    questions keep working), rebuild the agent's textual conversation history,
    and return the stored turns for the frontend to re-render.
    """
    data = request.get_json(silent=True) or {}
    conversation_id = (data.get("conversation_id") or "").strip()
    if not conversation_id:
        return jsonify({"success": False, "error": "conversation_id is required"}), 400

    # A first analysis still in flight has no disk file yet — fall back to its
    # in-memory job so the sidebar's ⏳ entry is still openable.
    job = chat_jobs.get_job(conversation_id)
    # with_steps: the client re-renders each saved turn's reasoning trace, the
    # same card the live stream drew while the turn was running.
    conv = history_service.get_conversation(conversation_id, with_steps=True)
    if conv is None and job is None:
        return jsonify({"success": False, "error": "Conversation not found"}), 404

    try:
        # Re-point BOTH sidecars at this conversation so new turns + feedback
        # continue appending here instead of spawning a fresh conversation.
        session["feedback_conversation_id"] = conversation_id

        conv = conv or {}
        running = bool(job is not None and job.status == "running")
        issue = conv.get("issue") if isinstance(conv.get("issue"), dict) else {}
        turns = conv.get("turns") or []

        log_path = (conv.get("log_path") or "").strip()

        # Prefer adopting the conversation's in-memory agent (it holds the full
        # tool-grounded history) over rebuilding context from saved text.
        adopted = False
        if job is not None and getattr(job, "agent", None) is not None:
            agent = job.agent
            adopted = True
            if not log_path:
                log_path = (getattr(agent, "current_log_path", "") or "").strip()
            # Don't pull a RUNNING job's agent into the session slot — it's busy
            # on a background thread. Reinstate only finished ones for follow-ups.
            if not running:
                sid = session.get("chatbot_session_id")
                if not sid:
                    sid = str(uuid.uuid4())
                    session["chatbot_session_id"] = sid
                _chatbot_instances[sid] = agent
        else:
            agent = _get_or_create_agent(skip_prime=True)
            agent.reset_conversation()

        log_exists = bool(log_path) and os.path.exists(log_path)

        skills = []
        log_has_date = True
        log_span_minutes = 0
        if log_exists:
            if not adopted:
                agent.current_log_path = log_path
                # Prime context (also resets conversation_history) BEFORE we
                # rebuild the textual turn history below.
                allowed = {"case_nbr", "subject", "description", "issue_type", "attachment_time"}
                try:
                    agent.prime_with_context(**{k: v for k, v in issue.items()
                                                if k in allowed and isinstance(v, str)})
                except Exception as _e:
                    print(f"[history] prime_with_context skipped: {_e}")
            # Read-only lookups — safe even while a run is in flight.
            try:
                skills = agent.get_skill_descriptions()
            except Exception:
                skills = []
            try:
                log_has_date = agent._log_has_date()
            except Exception:
                log_has_date = True
            try:
                log_span_minutes = agent.get_log_span_minutes()
            except Exception:
                log_span_minutes = 0

        # Restore the issue time the conversation was anchored on.
        issue_time_str = (conv.get("issue_time") or "").strip()
        if adopted:
            # The adopted agent already carries the right issue_time; just
            # surface it to the client when the snapshot didn't record one.
            if not issue_time_str:
                try:
                    issue_time_str = format_issue_time(agent.issue_time) or ""
                except Exception:
                    issue_time_str = ""
        elif issue_time_str:
            try:
                parsed, is_time_only = parse_issue_time_string(issue_time_str)
                agent.issue_time = parsed
                agent._issue_time_time_only = is_time_only
            except Exception:
                pass

        # Restore the agent's conversation ONLY when we didn't adopt a live
        # agent (which already holds the real history).
        #
        # Preferred: the stored model-facing context — the same messages the
        # agent last sent, tool results included — so a follow-up is answered
        # by something that still has the evidence. It is applied AFTER
        # prime_with_context, which resets conversation_history along with the
        # agent's caches; the stored context already carries its own priming
        # head, so replacing wholesale avoids a duplicate one.
        #
        # Fallback: the pre-existing rebuild from result text. Plain
        # user/assistant pairs, no tool_use blocks, so the tool loop's pairing
        # invariants stay intact. Conversations saved before contexts were
        # stored land here, and behave exactly as they did before.
        context_restored = 0
        if not adopted:
            stored_context = history_service.get_context(conversation_id)
            if stored_context:
                try:
                    context_restored = agent.import_conversation_context(stored_context)
                except Exception as _e:
                    print(f"[history] context restore failed: {_e}")
                    context_restored = 0
            if not context_restored:
                for turn in turns:
                    um = (turn.get("user_message") or "").strip()
                    if um:
                        agent.conversation_history.append({"role": "user", "content": um})
                    at = history_service.assistant_text_from_result(turn.get("result"))
                    if at:
                        agent.conversation_history.append({"role": "assistant", "content": at})

        if log_exists:
            session["chatbot_log_path"] = log_path

        return jsonify({
            "success": True,
            "conversation_id": conversation_id,
            "title": conv.get("title") or (job.title if job else "") or "Conversation",
            "turns": turns,
            # How grounded the resumed agent is, so the UI can say so rather
            # than leaving the user to guess whether a follow-up will still
            # know what the analysis found. 0 = rebuilt from result text.
            "context_restored": context_restored,
            # Live-analysis hand-off: when running, the client renders these
            # buffered steps and then opens /history/stream to follow the rest.
            "running": running,
            "running_user_message": (job.title if running else ""),
            "steps": list(job.steps) if (job is not None and running) else [],
            "log_path": log_path,
            "log_exists": log_exists,
            "issue_time": issue_time_str,
            "log_has_date": log_has_date,
            "log_span_minutes": log_span_minutes,
            "skills": skills,
        })
    except Exception as e:
        traceback.print_exc()
        return jsonify({"success": False, "error": str(e)}), 500


# ------------------------------------------------------------------
# Back to Avatar: drop the chatbot session entirely so the next visit
# to /log_chatbot/ starts with a fresh conversation (no prior analysis).
# ------------------------------------------------------------------
@log_chatbot_bp.route("/back_to_avatar", methods=["GET"])
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
    #    agent via prime_with_context() the next time /log_chatbot/ is
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
        "feedback_conversation_id",   # next /log_chatbot/ visit starts a fresh conversation
        "gather_workflow_id",         # next case starts a fresh v5 workflow
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
@log_chatbot_bp.route("/prepare", methods=["POST"])
def prepare():
    """
    Called from download_result when user clicks "Chatbot Analysis".
    1. Derives the .log path from the given etl_path.
    2. Sets the agent's current_log_path.
    3. Primes conversation history with case description + classification.
    Returns {"success": True} — JS then redirects to /log_chatbot/.
    """
    data = request.get_json(silent=True) or {}
    etl_path = data.get("etl_path", "").strip()
    if not etl_path:
         return jsonify({"success": False, "error": "etl_path is required"}), 400

    log_path = etl_path + ".log"
    if not os.path.exists(log_path):
         return jsonify({"success": False, "error": f".log file not found: {log_path}"}), 404

    try:
        # Entering a NEW analysis from download_result. Purge the derived
        # issue-context caches FIRST so the context below is rebuilt from this
        # run's selected_files / case_context — not a previous run's leftovers.
        # (Fixes stale attachment time / description when a second analysis is
        # started without going through "Back to Avatar".)
        _invalidate_issue_context_caches()

        # Pull consolidated issue context from all session sources
        ctx = _extract_issue_context()

        # Update shared last_analyzed_log_path so the chatbot index page pre-fills it
        app_config.last_analyzed_log_path = log_path

        # Get/create per-session agent and prime it
        # skip_prime=True: we call prime_with_context explicitly below (after setting log path)
        agent = _get_or_create_agent(skip_prime=True)
        agent.current_log_path = log_path
        agent.reset_conversation()          # fresh conversation for a new file
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
        )

        return jsonify({"success": True})
    except Exception as e:
        error_traceback = traceback.format_exc()
        print(f"❌ Chatbot prepare error:\n{error_traceback}")
        return jsonify({"success": False, "error": str(e)}), 500


# ------------------------------------------------------------------
# API: open native directory browser and return selected path
# ------------------------------------------------------------------
@log_chatbot_bp.route("/browse_dir", methods=["GET"])
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
@log_chatbot_bp.route("/reload_skills", methods=["POST"])
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
        if app_config.log_chatbot_agent:
            app_config.log_chatbot_agent.skills = skills
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
@log_chatbot_bp.route("/browse_yaml", methods=["GET"])
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
@log_chatbot_bp.route("/load_skills_yaml", methods=["POST"])
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
        if app_config.log_chatbot_agent:
            app_config.log_chatbot_agent.skills = skills
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
@log_chatbot_bp.route("/reload_from_shared", methods=["POST"])
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
        if app_config.log_chatbot_agent:
            app_config.log_chatbot_agent.skills = skills
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
@log_chatbot_bp.route("/skills", methods=["GET"])
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
    base = getattr(app_config, "log_chatbot_agent", None)
    if base is not None and getattr(base, "client", None) is not None:
        return base.client, getattr(base, "model", None)
    helper = getattr(app_config, "llm_helper", None)
    if helper is not None and getattr(helper, "client", None) is not None:
        return helper.client, getattr(helper, "model", "gpt-4.1")
    return None, None


def _issue_context_organized(raw_desc: str, first_ts, last_ts, log_path: str = "") -> dict:
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
        started_at = datetime.now()
        d, usage = organize_issue_context(
            raw_desc, first_ts=first_ts, last_ts=last_ts,
            llm_client=client, llm_model=model, return_usage=True,
        )
        session["_issue_ai_quick"] = {"data": d}
        if int(usage.get("llm_calls") or 0) > 0:
            try:
                issue = CaseContext.from_session(session.get("case_context") or {}).to_dict()
                gather_service.record_feature_usage(
                    workflow_id=session.get("gather_workflow_id", ""),
                    feature_code="issue_time_prepass",
                    model=model or "", usage=usage, issue=issue, domain="wifi",
                    trigger="direct_chatbot_context",
                    latency_ms=int((datetime.now() - started_at).total_seconds() * 1000),
                )
            except Exception:
                pass
    return {
        "clean_description": d.get("clean_description") or raw_desc,
        "issue_times": realign_times_to_log(d.get("issue_times") or [], first_ts, last_ts, log_path),
        "interpretation": d.get("interpretation", ""),
    }


@log_chatbot_bp.route("/get_issue_context", methods=["GET"])
def get_issue_context():
    try:
        ctx = _extract_issue_context()
        attachment_time = ctx.get("attachment_time", "")
    except Exception:
        ctx = {}
        attachment_time = ""

    log_path = session.get("chatbot_log_path") or app_config.last_analyzed_log_path or ""
    first_ts, last_ts = read_log_time_range(log_path) if log_path else (None, None)

    # Frame-correct a time-only attachment_time up front. A bare clock like
    # "16:45:00" (the customer wall clock parsed from the attachment subtitle)
    # must be anchored to the customer capture date and converted to the log
    # frame HERE. Otherwise the frontend picker — which prefers attachment_time
    # over the resolved issue_time — stamps the clock straight onto the log date
    # and mixes frames (showing e.g. 06/03 16:45 instead of log-frame 06/03
    # 05:45). Full datetimes pass through unchanged (handled by _to_log_frame).
    if attachment_time:
        _aligned_at = realign_times_to_log([attachment_time], first_ts, last_ts, log_path)
        if _aligned_at:
            attachment_time = _aligned_at[0]

    # Smart pass: let the LLM organize the raw case Issue Description into a
    # clean problem statement + (possibly multiple) issue time points. Cached
    # per-description so repeat fetches don't re-call the LLM; falls back to
    # the regex extractor + concise composer when no LLM is configured.
    organized = _issue_context_organized(ctx.get("description", "") or "", first_ts, last_ts, log_path)
    clean_desc = organized.get("clean_description") or _compose_concise_description(ctx)
    issue_times = organized.get("issue_times") or []

    # Back-compat single issue_time: prefer the first organized time, else the
    # previous attachment_time / log-latest resolution (cached by log_path).
    if issue_times:
        issue_time_str = issue_times[0]
    else:
        # attachment_time is already frame-corrected above; use it directly.
        # When absent, fall back to the cached log-latest resolution.
        issue_time_str = attachment_time or _resolved_issue_time_for(log_path, attachment_time)

    # Align every surfaced time to the LOG frame so the picker drives
    # PreScan / Segment-2 against the raw .log content (which the decoder
    # writes in the log host's clock). Source values are usually log-frame
    # strings already, but a customer-typed description ("at 12:26 PM CST")
    # would land in customer frame and needs shifting back. The
    # ``determine_issue_time_frames`` helper picks which interpretation
    # applies per string and returns both frames so we can also surface a
    # customer-tz annotation for the UI.
    customer_annotations = {}
    customer_tz_for_ui = ""
    if log_path:
        try:
            from utils.issue_time_ai import determine_issue_time_frames

            def _to_log_frame(s: str) -> str:
                nonlocal customer_tz_for_ui
                if not isinstance(s, str) or not s:
                    return s
                parsed, is_time_only = parse_issue_time_string(s)
                if not parsed or is_time_only:
                    return s
                # Pass the log content range (GMT+8 engineer frame) as the
                # second anchor so an ATTACH/issue time mistakenly entered in
                # our engineer clock — rather than the customer's packed time —
                # is detected and shifted back to the customer frame.
                frames = determine_issue_time_frames(
                    parsed, [log_path],
                    log_first_ts=first_ts, log_last_ts=last_ts,
                )
                if frames.get("customer_tz") and not customer_tz_for_ui:
                    customer_tz_for_ui = frames["customer_tz"]
                log_dt = frames.get("log_frame") or parsed
                cust_dt = frames.get("customer_frame")
                log_str = format_issue_time(log_dt) if log_dt != parsed else s
                if cust_dt and frames.get("customer_tz"):
                    customer_annotations[log_str] = format_issue_time(cust_dt)
                return log_str

            attachment_time = _to_log_frame(attachment_time)
            issue_time_str = _to_log_frame(issue_time_str)
            issue_times = [_to_log_frame(s) for s in issue_times]
        except Exception as e:
            print(f"[get_issue_context] issue-time frame detect skipped ({e})")

    return jsonify({
        "description": clean_desc,
        "attachment_time": attachment_time,
        "issue_time": issue_time_str,
        "issue_times": issue_times,
        "interpretation": organized.get("interpretation", ""),
        # Customer-tz annotation: same instant viewed from the customer's
        # wall clock. The picker shows the log-frame value (matches .log
        # content) and surfaces this map underneath so the engineer also
        # sees what time it was on the customer's side. tz label is the
        # detected system_info / sidecar value; empty string when nothing
        # could be detected (chatbot then hides the annotation row).
        "customer_tz": customer_tz_for_ui,
        "customer_annotations": customer_annotations,
        # IANA id for the customer tz (e.g. "America/Los_Angeles") so the
        # browser can recompute the customer wall clock DST-correctly for any
        # date typed into the picker. Empty when only a fixed offset is known —
        # the frontend then falls back to the label's standard offset.
        "customer_iana": to_iana_timezone(customer_tz_for_ui) if customer_tz_for_ui else "",
    })


# ------------------------------------------------------------------
# API: find best matching log by reading actual file timestamps
# ------------------------------------------------------------------
@log_chatbot_bp.route("/find_best_log", methods=["POST"])
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

    # --- Detect customer's timezone for the response note. The picker now
    # compares ``issue_time`` against the .log's own first/last timestamps
    # directly (both in the log frame); customer-tz only matters for the
    # user-facing "this is X in customer time" annotation surfaced by the
    # caller — they'll have shifted the typed issue time into log frame
    # already via ``determine_issue_time_frames``.
    tz_name = ""
    for p in etl_paths:
        tz_name = get_effective_timezone(p)
        if tz_name:
            break
    tz_label = format_tz_label(tz_name) if tz_name else ""

    # --- Scan each log file for its time range (shared helper) ---
    candidates = []
    for etl_path in etl_paths:
        log_path = etl_path + ".log"
        if not os.path.exists(log_path):
            continue
        first_ts, last_ts = read_log_time_range(log_path)
        candidates.append({
            "etl_path": etl_path,
            "log_path": log_path,
            "first_ts": first_ts,
            "last_ts": last_ts,
        })

    if not candidates:
        return jsonify({"best_path": etl_paths[0] if etl_paths else None,
                        "reason": "No readable log files found; defaulting to first.",
                        "resolved_issue_time": "",
                        "tz_used": tz_label})

    print(f"[find_best_log] tz={tz_name!r} candidates={[c['etl_path'] for c in candidates]}, "
          f"issue_time={issue_time}, issue_time_only_str={issue_time_only_str}")

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

    tz_note = f" [tz: {tz_label}]" if tz_label else ""

    # --- If we have an issue time, pick the log whose range covers it ---
    if issue_time:
        # Priority 1: log file whose [first_ts, last_ts] contains issue_time
        for c in candidates:
            if c["first_ts"] and c["last_ts"]:
                if c["first_ts"] <= issue_time <= c["last_ts"]:
                    return jsonify({
                        "best_path": c["etl_path"],
                        "reason": f"Log covers issue time {issue_time_str} "
                                  f"(range: {c['first_ts']} ~ {c['last_ts']}){tz_note}",
                        "resolved_issue_time": resolved_issue_time_str,
                        "tz_used": tz_label,
                    })

        # Priority 2: log file whose last_ts is closest to (but before) issue_time
        best, best_delta = None, None
        for c in candidates:
            ts = c["last_ts"] or c["first_ts"]
            if ts:
                delta = abs((issue_time - ts).total_seconds())
                if best_delta is None or delta < best_delta:
                    best_delta = delta
                    best = c
        if best:
            return jsonify({
                "best_path": best["etl_path"],
                "reason": f"Closest log to issue time {issue_time_str} "
                          f"(range: {best['first_ts']} ~ {best['last_ts']}, "
                          f"delta: {best_delta:.0f}s){tz_note}",
                "resolved_issue_time": resolved_issue_time_str,
                "tz_used": tz_label,
            })

    # --- Fallback: pick the log with the latest last_ts ---
    candidates_with_ts = [c for c in candidates if c["last_ts"]]
    if candidates_with_ts:
        latest = max(candidates_with_ts, key=lambda c: c["last_ts"])
        return jsonify({
            "best_path": latest["etl_path"],
            "reason": f"No issue time provided; picked latest log "
                      f"(range: {latest['first_ts']} ~ {latest['last_ts']}){tz_note}",
            "resolved_issue_time": resolved_issue_time_str,
            "tz_used": tz_label,
        })

    # --- Ultimate fallback ---
    return jsonify({
        "best_path": candidates[0]["etl_path"],
        "reason": "Could not determine timestamps; defaulting to first.",
        "resolved_issue_time": "",
        "tz_used": tz_label,
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

from configs.path_configs import (
    LOCAL_SKILLS_YAML as _LOCAL_SKILLS_YAML,
    SKILLS_CONFIG_DIR_prim as _SK_DIR_prim,
    SKILLS_CONFIG_DIR_bkup as _SK_DIR_bkup,
)
from utils.skills_yaml_utils import (
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
                    and entry.name.startswith("skills_") and entry.suffix == ".yaml":
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
# — see services/log_chatbot_service.py:SKILL_FILE_MAP). The previous
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
    if app_config.log_chatbot_agent:
        app_config.log_chatbot_agent.skills = skills
    if app_config.llm_helper:
        app_config.llm_helper.skills = skills
    return skills


def _activate_yaml(path) -> dict:
    """Re-load skills from `path` and return the chatbot's descriptions."""
    _refresh_loaded_skills(str(path))
    agent = _get_or_create_agent()
    return agent.get_skill_descriptions()


@log_chatbot_bp.route("/skills_yaml_status", methods=["GET"])
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


@log_chatbot_bp.route("/skills_yaml_use_cloud", methods=["POST"])
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


@log_chatbot_bp.route("/skills_yaml_use_user", methods=["POST"])
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


@log_chatbot_bp.route("/refresh_cloud_baseline", methods=["POST"])
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


@log_chatbot_bp.route("/load_local_skills_yaml", methods=["GET"])
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


@log_chatbot_bp.route("/save_local_skills_yaml", methods=["POST"])
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


@log_chatbot_bp.route("/delete_local_skill", methods=["POST"])
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


@log_chatbot_bp.route("/upload_modified_yaml", methods=["POST"])
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
