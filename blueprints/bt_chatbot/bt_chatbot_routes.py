from flask import render_template, request, session, jsonify, Response, copy_current_request_context, redirect, url_for
from services.skill_editor.controller import (
    SkillEditorContext,
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
from services.chatbot.web_session import (
    ensure_feedback_conversation_id as _shared_feedback_conversation_id,
    resume_agent_for as _shared_resume_agent_for,
)
from services.chatbot.issue_context import (
    compose_concise_description as _compose_concise_description,
    extract_disconnect_time as _extract_disconnect_time,
    extract_issue_context as _extract_issue_context,
    resolved_issue_time_for as _resolved_issue_time_for,
)
from services.chatbot.factory import (
    ChatbotBlueprintConfig,
    create_chatbot_blueprint,
    handler_map,
)
import json
import re
import traceback
import uuid
import os
import threading
from datetime import datetime
import tkinter as tk
from tkinter import filedialog

from configs.chatbot_ui import BT_UI
from configs.global_configs import app_config
from models.models import CaseContext
from services.chatbot.agent.bluetooth import BtLogAgentSystem, WifiLogAgentSystem, load_skills_from_data_dir, get_builtin_skills, build_skill_file_map, load_skills_from_yaml
from utils.etl_utils import extract_time_from_description
from utils.issue_time_utils import (
    parse_issue_time_string,
    read_log_time_range,
    resolve_issue_time,
    format_issue_time,
)
from utils.issue_time_ai import build_issue_time_suggestions, organize_issue_context, realign_times_to_log, find_nearest_event_error
from utils.event_log_utils import find_event_log_for_log
from services import feedback_service
from services import history_service
from services.chatbot import job_runtime as chat_jobs


# Server-side store: session_id -> WifiLogAgentSystem instance
_chatbot_instances: dict = {}

# Upper bound on System-Event-Log rows pulled to anchor an AI issue-time
# suggestion. build_event_log_digest keeps at most 40 rows (by severity) and
# find_nearest_event_error only needs a representative pool, so this cap keeps
# /suggest_issue_times fast and bounded even on very large .evtx captures.
_EVENT_ANCHOR_MAX = 500


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
        # Inherit ACE runner from the boot-time base agent so playbook blocks
        # are injected into per-session prompts (BT's own "bt" sync namespace —
        # see configs/set_up_app.py).
        ace_runner = getattr(base, "ace_runner", None)
        if ace_runner is not None:
            agent.attach_ace(ace_runner)
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

    return render_template(
        "chatbot/page.html",
        ui=BT_UI,
        suggested_log=suggested_log,
        issue_description=issue_desc,
    )


# ------------------------------------------------------------------
# API: open native file browser and return selected path
# ------------------------------------------------------------------
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
            evtx_path = find_event_log_for_log(log_path) if log_path else ""
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
        # Resolve the conversation first so we can adopt a finished job's agent
        # (full tool-grounded history) when the user continues a just-analysed
        # thread WITHOUT going through the History sidebar's /history/load —
        # otherwise the run_chat_with_tools background job detaches the agent
        # from the session slot and a bare _get_or_create_agent() would hand
        # the very next follow-up a brand-new, context-less agent.
        conversation_id = _ensure_feedback_conversation_id()
        agent = _resume_agent_for(conversation_id)
        if issue_time_window_minutes is not None:
            agent.issue_time_window_minutes = issue_time_window_minutes
        # Backstop: if the resolved agent lost its log path (e.g. a fresh agent
        # rebuilt on a browser-back re-run where prepare()/set_log() didn't
        # run), recover it from the same sources _get_or_create_agent() uses.
        if not agent.current_log_path:
            agent.current_log_path = (
                session.get("chatbot_log_path")
                or app_config.last_analyzed_log_path or ""
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
                domain="bt",
            )
            if session_id:
                _chatbot_instances.pop(session_id, None)

            def step_cb(step):
                try:
                    if isinstance(step, dict):
                        collected_steps.append(step)
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
                        domain="bt",
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
                        domain="bt",
                    )
                    chat_jobs.finish_job(job, result)
                except Exception as exc:
                    error_tb = traceback.format_exc()
                    print(f"❌ Chat-with-tools thread error:\n{error_tb}")
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
            # Local browsable history (always persists, unlike feedback which
            # is vote-gated). Stored under the "bt" domain — a different
            # folder from the WiFi bot's history (see history_service).
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
# ------------------------------------------------------------------
# API: local conversation history (Gemini / Claude style sidebar)
#
# Every chat turn is persisted to <avatarfiles_dir>/bt_history/bt-<id>.json
# by history_service (domain="bt") — a DIFFERENT folder from the WiFi
# chatbot's <avatarfiles_dir>/history/<id>.json, and additionally prefixed
# so the two are still trivially distinguishable by filename alone even if
# they ever ended up sharing a folder. These endpoints let the sidebar
# list / load / delete BT conversations. All are read/written locally only.
# ------------------------------------------------------------------
def history_list():
    try:
        conversations = history_service.list_conversations(domain="bt")
        # Merge in-memory running jobs so the sidebar can show a ⏳ marker:
        #   * a persisted conversation that's mid-analysis  -> running: True
        #   * a brand-new first analysis not yet on disk     -> synthetic entry
        # Scoped to domain="bt" so a WiFi analysis running at the same time
        # never leaks into this list.
        try:
            running = {j["conversation_id"]: j for j in chat_jobs.active_summaries(domain="bt")}
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
    # chat_jobs is a single registry shared by both bots (keyed by conversation_id
    # only), so a WiFi job id handed to this BT endpoint would otherwise resolve
    # and stream that WiFi job's steps/result here. Reject anything not tagged bt.
    if job is None or getattr(job, "domain", "") != "bt":
        def _idle():
            yield "data: " + json.dumps({"type": "idle"}) + "\n\n"
        return Response(_idle(), mimetype="text/event-stream", headers=headers)
    return Response(_job_sse(job), mimetype="text/event-stream", headers=headers)


def history_get():
    conversation_id = (request.args.get("conversation_id") or "").strip()
    if not conversation_id:
        return jsonify({"success": False, "error": "conversation_id is required"}), 400
    conv = history_service.get_conversation(conversation_id, domain="bt")
    if conv is None:
        return jsonify({"success": False, "error": "Conversation not found"}), 404
    return jsonify({"success": True, "conversation": conv})


def history_delete():
    data = request.get_json(silent=True) or {}
    conversation_id = (data.get("conversation_id") or "").strip()
    if not conversation_id:
        return jsonify({"success": False, "error": "conversation_id is required"}), 400
    removed = history_service.delete_conversation(conversation_id, domain="bt")
    # If the deleted conversation is the one currently active, drop the
    # session pointer so the next turn starts a brand-new conversation.
    if removed and (session.get("feedback_conversation_id") or "") == conversation_id:
        session.pop("feedback_conversation_id", None)
    return jsonify({"success": bool(removed)})


def history_rename():
    data = request.get_json(silent=True) or {}
    conversation_id = (data.get("conversation_id") or "").strip()
    title = (data.get("title") or "").strip()
    if not conversation_id:
        return jsonify({"success": False, "error": "conversation_id is required"}), 400
    if not title:
        return jsonify({"success": False, "error": "title is required"}), 400
    ok = history_service.rename_conversation(conversation_id, title, domain="bt")
    return jsonify({"success": bool(ok)})


def history_pin():
    data = request.get_json(silent=True) or {}
    conversation_id = (data.get("conversation_id") or "").strip()
    pinned = bool(data.get("pinned"))
    if not conversation_id:
        return jsonify({"success": False, "error": "conversation_id is required"}), 400
    ok = history_service.set_pinned(conversation_id, pinned, domain="bt")
    return jsonify({"success": bool(ok), "pinned": pinned})


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
    # in-memory job so the sidebar's ⏳ entry is still openable. chat_jobs is a
    # single registry shared by both bots, so reject anything not tagged bt —
    # otherwise a WiFi conversation id would adopt a WiFi job/agent here.
    job = chat_jobs.get_job(conversation_id)
    if job is not None and getattr(job, "domain", "") != "bt":
        job = None
    conv = history_service.get_conversation(conversation_id, domain="bt")
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
            # get_skill_descriptions() only reads self.skills (set once at
            # construction, never reassigned mid-run) and get_log_span_minutes()
            # does its own independent file read keyed off current_log_path —
            # both are safe to call even while the job's background thread is
            # still analysing.
            try:
                skills = agent.get_skill_descriptions()
            except Exception:
                skills = []
            try:
                log_span_minutes = agent.get_log_span_minutes()
            except Exception:
                log_span_minutes = 0
            # _log_has_date() reads self._raw_log_cache / _raw_log_cache_path,
            # which the background analysis thread actively mutates via
            # _ensure_raw_log_cache() while running=True — skip it for a live
            # job to avoid reading that pair mid-write; the cheap default (True)
            # just means the sidebar briefly uses the original datetime
            # windowing until the job finishes and the page is reloaded.
            if not running:
                try:
                    log_has_date = agent._log_has_date()
                except Exception:
                    log_has_date = True

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

        # Rebuild the agent's textual conversation history ONLY when we didn't
        # adopt a live agent (which already holds the real history). Plain
        # user/assistant text pairs — no tool_use blocks, so the tool loop's
        # pairing invariants stay intact.
        if not adopted:
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
# to /bt_chatbot/ starts with a fresh conversation (no prior analysis).
# ------------------------------------------------------------------
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


# ------------------------------------------------------------------
# API: reload skills from a directory and apply to current agent
# ------------------------------------------------------------------
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


# ------------------------------------------------------------------
# API: load skills from a YAML file (standalone, no prompt/filter dirs)
# ------------------------------------------------------------------
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





_USER_YAML_WRITE_LOCK = threading.Lock()




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

# Top-level skill header (column 0, ends with bare ":"). Widened from
# the original `[A-Za-z_]\w*` so it accepts the real skill IDs in this
# codebase that contain "/" (e.g. "VLP/UHB/AFC", "WRDS/WGDS/EWRD/SGOM"
# — see services/chatbot/agent/bluetooth.py:SKILL_FILE_MAP). The previous
# regex silently failed on those, dropping their `# - "..."` disabled
# entries on every save round-trip. The first char is anchored to
# [A-Za-z0-9_] so list items ("- foo:") and comment lines ("# x:")
# are still rejected, and `\s*$` guarantees we only match bare key
# headers — not inline mappings like `Foo: bar`.






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



















# The module above is now a domain adapter: its functions retain BT/Wi-Fi/NW
# policy, while the factory owns the public route table and shared use cases.
_BT_CHATBOT_CAPABILITIES = {
    key for key, enabled in BT_UI["features"].items() if enabled
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
_CHATBOT_ADAPTER_NAMESPACE = {**globals(), **_SKILL_EDITOR_HANDLERS}
_BT_CHATBOT_HANDLERS = handler_map(_CHATBOT_ADAPTER_NAMESPACE, _BT_CHATBOT_CAPABILITIES)
bt_chatbot_bp = create_chatbot_blueprint(ChatbotBlueprintConfig(
    name="bt_chatbot",
    import_name=__name__,
    url_prefix="/bt_chatbot",
    capabilities=_BT_CHATBOT_CAPABILITIES,
    get_agent=_get_or_create_agent,
    handlers=_BT_CHATBOT_HANDLERS,
))
