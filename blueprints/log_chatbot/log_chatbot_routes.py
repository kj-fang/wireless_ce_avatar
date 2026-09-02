from flask import render_template, request, session, jsonify, Response, copy_current_request_context, redirect, url_for
from services.skill_editor.controller import (
    SkillEditorContext,
    build_profile_yaml_helpers,
    build_skill_editor_handlers,
)
from services.skill_editor.yaml_service import (
    read_yaml_file as _read_yaml_file,
    sanitise_skill_payload as _sanitise_skill_payload,
    scan_disabled_comments as _scan_disabled_comments,
    write_yaml_file as _write_yaml_file,
)
from services.chatbot.job_runtime import (
    job_sse as _job_sse,
    terminal_sse as _terminal_sse,
)
from services.chatbot.session import (
    ensure_feedback_conversation_id as _shared_feedback_conversation_id,
    resume_agent_for as _shared_resume_agent_for,
)
from services.chatbot.issue_context import (
    compose_concise_description as _compose_concise_description,
    extract_disconnect_time as _extract_disconnect_time,
    extract_issue_context as _extract_issue_context,
    organized_issue_context as _organized_issue_context,
    resolved_issue_time_for as _resolved_issue_time_for,
)
from services.chatbot.factory import (
    ChatbotBlueprintConfig,
    create_chatbot_blueprint,
    handler_map,
)
from services.chatbot.shared_routes import (
    SharedRouteContext,
    build_shared_handlers,
    llm_client_model as _llm_client_model,
)
from utils.event_log_utils import find_event_log_for_log
import json
import re
import traceback
import uuid
import os
import threading
from datetime import datetime

from configs.chatbot_ui import LOG_CHATBOT_UI
from configs.global_configs import app_config
from models.models import CaseContext
from services.chatbot.engine.system import WifiLogAgentSystem, load_skills_from_yaml
from utils.etl_utils import extract_time_from_description
from utils.issue_time_utils import (
    parse_issue_time_string,
    read_log_time_range,
    resolve_issue_time,
    format_issue_time,
    validate_issue_time_in_log_range,
)
from utils.issue_time_ai import build_issue_time_suggestions, realign_times_to_log
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
from services.chatbot import job_runtime as chat_jobs
from services import gather_service


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
    return _shared_feedback_conversation_id(rotate=rotate)


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
        "_carried_issue_time",         # download_result -> chatbot hand-off
        "_carried_issue_time_warning", # failed hand-off range validation
        "_carried_issue_time_present", # blocks a second date guess on failure
    ):
        session.pop(key, None)










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


def _resume_agent_for(conversation_id: str):
    """
    Return the agent to use for ``conversation_id``.

    A finished background analysis keeps its own (detached) agent, which holds
    the full tool-grounded conversation history. When the user continues that
    same conversation we adopt that agent — far higher fidelity than rebuilding
    context from saved text. Running jobs are NOT adopted (their agent is busy
    on a background thread); the caller falls back to a fresh session agent.
    """
    return _shared_resume_agent_for(
        conversation_id,
        _chatbot_instances,
        _get_or_create_agent,
    )






# ------------------------------------------------------------------
# Pages
# ------------------------------------------------------------------
def index():
    suggested_log = app_config.last_analyzed_log_path or ""
    issue_desc = ""
    try:
        ctx = _extract_issue_context()
        issue_desc = ctx.get("description", "")
    except Exception:
        pass
    return render_template(
        "chatbot/page.html",
        ui=LOG_CHATBOT_UI,
        suggested_log=suggested_log,
        issue_description=issue_desc,
    )


# ------------------------------------------------------------------
# API: set log file path
# ------------------------------------------------------------------
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

        try:
            evtx_path = find_event_log_for_log(log_path)
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
# chat_stop and the history list/stream/get/delete/rename/pin endpoints are
# built from services.chatbot.shared_routes (see _SHARED_HANDLERS below) —
# they only differed from the BT bot by the history domain key. history_load
# stays here because the Wi-Fi resume path is domain-specific.


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
# ------------------------------------------------------------------
# API: prepare chatbot from download_result (set log path + case context)
# ------------------------------------------------------------------
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
    carried_issue_time = str(data.get("issue_time") or "").strip()
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

        # ``download_result`` has already resolved a time-only case timestamp
        # against the selected capture folder. Carry that exact value forward;
        # asking the LLM to choose a date again is both redundant and unstable
        # when a log spans midnight. The selected log is now available, so this
        # is also the right place to reject an impossible/out-of-range value.
        if carried_issue_time:
            session["_carried_issue_time_present"] = True
            _carried_dt, _range_first, _range_last, _range_error = (
                validate_issue_time_in_log_range(carried_issue_time, log_path)
            )
            if _carried_dt is not None:
                session["_carried_issue_time"] = format_issue_time(_carried_dt)
                session["_carried_issue_time_warning"] = ""
            else:
                session["_carried_issue_time"] = ""
                if _range_first and _range_last:
                    _range_text = (
                        f"{format_issue_time(_range_first)} to "
                        f"{format_issue_time(_range_last)}"
                    )
                    session["_carried_issue_time_warning"] = (
                        f"Auto-detected issue time {carried_issue_time} was not used: "
                        f"{_range_error} Log range: {_range_text}. Please confirm the issue time."
                    )
                else:
                    session["_carried_issue_time_warning"] = (
                        f"Auto-detected issue time {carried_issue_time} was not used: "
                        f"{_range_error} Please confirm the issue time."
                    )

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


# ------------------------------------------------------------------
# API: load_skills_yaml_route / reload_from_shared and the
# YAML-browse dialog are built from services.chatbot.shared_routes (see
# _SHARED_HANDLERS below) — they only differed from the BT bot by which
# app_config attribute holds the app-level agent.
# ------------------------------------------------------------------


# ------------------------------------------------------------------
# API: get available skills
# ------------------------------------------------------------------
def _get_llm_client_model():
    return _llm_client_model("log_chatbot_agent")


def _issue_context_organized(raw_desc: str, first_ts, last_ts, log_path: str = "") -> dict:
    return _organized_issue_context(raw_desc, first_ts, last_ts, log_path,
                                    llm_client_model=_get_llm_client_model,
                                    domain="wifi")


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

    # A case-number hand-off is authoritative for this transition. It either
    # supplies the already-resolved, range-checked value from download_result,
    # or deliberately supplies no value after validation failed. In the latter
    # case do not silently fall back to a fresh LLM/attachment/log-latest guess.
    carried_present = bool(session.get("_carried_issue_time_present"))
    carried_issue_time = (session.get("_carried_issue_time") or "").strip()
    carried_warning = (session.get("_carried_issue_time_warning") or "").strip()
    if carried_present:
        issue_times = [carried_issue_time] if carried_issue_time else []
        attachment_time = carried_issue_time

    # Back-compat single issue_time: prefer the first organized time, else the
    # previous attachment_time / log-latest resolution (cached by log_path).
    if issue_times:
        issue_time_str = issue_times[0]
    elif carried_present:
        issue_time_str = ""
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

    # Final safety net for every auto-filled source, not only the explicit
    # download_result hand-off. An LLM can choose the wrong date when a log
    # spans midnight; never place such a value in the picker unless it actually
    # falls inside the selected log. Manual entry remains available so the user
    # can correct the time or deliberately choose another log.
    range_blocked = False
    range_warning = ""
    if first_ts and last_ts:
        def _is_inside_log_range(s: str) -> bool:
            parsed, is_time_only = parse_issue_time_string(s)
            return bool(parsed and not is_time_only and first_ts <= parsed <= last_ts)

        had_auto_candidate = bool(issue_times or attachment_time or issue_time_str)
        issue_times = [s for s in issue_times if _is_inside_log_range(s)]
        attachment_time = attachment_time if _is_inside_log_range(attachment_time) else ""
        if issue_times:
            issue_time_str = issue_times[0]
        elif attachment_time:
            issue_time_str = attachment_time
        elif _is_inside_log_range(issue_time_str):
            # Deterministic log-latest fallback is already safe.
            pass
        elif carried_present or had_auto_candidate:
            issue_time_str = ""
            range_blocked = True
            range_warning = (
                "The auto-detected issue time was not used because it is outside "
                f"the selected log range ({format_issue_time(first_ts)} to "
                f"{format_issue_time(last_ts)}). Please confirm the issue time."
            )

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
        "issue_time_blocked": bool(
            (carried_present and not carried_issue_time) or range_blocked
        ),
        "issue_time_warning": carried_warning or range_warning,
        "log_first_time": format_issue_time(first_ts),
        "log_last_time": format_issue_time(last_ts),
    })


# ------------------------------------------------------------------
# API: find best matching log by reading actual file timestamps
# ------------------------------------------------------------------
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





_USER_YAML_WRITE_LOCK = threading.Lock()




_YAML_HELPERS = build_profile_yaml_helpers(
    user_local_dir=_user_local_dir,
    today_yaml_filename=_today_yaml_filename,
    user_yaml_prefix="skills_",
    latest_cloud_baseline=_latest_cloud_baseline,
    latest_user_yaml=_latest_user_yaml,
    write_yaml_file=_write_yaml_file,
    load_skills_from_yaml=load_skills_from_yaml,
    get_agent=_get_or_create_agent,
    agent_config_attr="log_chatbot_agent",
)
_gather_disabled_comments = _YAML_HELPERS["gather_disabled_comments"]
_persist_user_yaml_snapshot = _YAML_HELPERS["persist_user_yaml_snapshot"]
_refresh_loaded_skills = _YAML_HELPERS["refresh_loaded_skills"]
_activate_yaml = _YAML_HELPERS["activate_yaml"]


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

# Top-level skill header (column 0, ends with bare ":"). Widened from
# the original `[A-Za-z_]\w*` so it accepts the real skill IDs in this
# codebase that contain "/" (e.g. "VLP/UHB/AFC", "WRDS/WGDS/EWRD/SGOM"
# — see services/chatbot/engine/system.py:SKILL_FILE_MAP). The previous
# regex silently failed on those, dropping their `# - "..."` disabled
# entries on every save round-trip. The first char is anchored to
# [A-Za-z0-9_] so list items ("- foo:") and comment lines ("# x:")
# are still rejected, and `\s*$` guarantees we only match bare key
# headers — not inline mappings like `Foo: bar`.






# The module above is now a domain adapter: its functions retain BT/Wi-Fi/NW
# policy, while the factory owns the public route table and shared use cases.
_LOG_CHATBOT_CAPABILITIES = {
    key for key, enabled in LOG_CHATBOT_UI["features"].items() if enabled
}
_SKILL_EDITOR_HANDLERS = build_skill_editor_handlers(SkillEditorContext(
    activate_yaml=_activate_yaml,
    get_active_source=_get_active_source,
    get_or_create_agent=_get_or_create_agent,
    latest_cloud_baseline=_latest_cloud_baseline,
    latest_user_yaml=_latest_user_yaml,
    persist_user_yaml_snapshot=_persist_user_yaml_snapshot,
    read_yaml_file=_read_yaml_file,
    refresh_cloud_baseline=_refresh_cloud_baseline,
    resolve_cloud_skills_dir=_resolve_cloud_skills_dir,
    sanitise_skill_payload=_sanitise_skill_payload,
    set_active_source=_set_active_source,
    skills_yaml_status_payload=_skills_yaml_status_payload,
))
_SHARED_HANDLERS = build_shared_handlers(SharedRouteContext(
    domain="",
    agent_config_attr="log_chatbot_agent",
    get_agent=_get_or_create_agent,
    session_agents=_chatbot_instances,
    browse_filetypes=(("Log files", "*.log"), ("All files", "*.*")),
    load_skills_from_yaml=load_skills_from_yaml,
))
_CHATBOT_ADAPTER_NAMESPACE = {**globals(), **_SHARED_HANDLERS, **_SKILL_EDITOR_HANDLERS}
_LOG_CHATBOT_HANDLERS = handler_map(_CHATBOT_ADAPTER_NAMESPACE, _LOG_CHATBOT_CAPABILITIES)
log_chatbot_bp = create_chatbot_blueprint(ChatbotBlueprintConfig(
    name="log_chatbot",
    import_name=__name__,
    url_prefix="/log_chatbot",
    capabilities=_LOG_CHATBOT_CAPABILITIES,
    get_agent=_get_or_create_agent,
    handlers=_LOG_CHATBOT_HANDLERS,
))
