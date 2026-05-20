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
from services import feedback_service

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

        # Step 2: Read from case_context.attachment_list (same data source as the template)
        raw_ctx_dict = session.get("case_context", {})
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
        # Auto-populate log path from last LogParser analysis if available
        if app_config.last_analyzed_log_path:
            agent.current_log_path = app_config.last_analyzed_log_path
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
    data = request.get_json(silent=True) or {}
    log_path = data.get("log_path", "").strip()
    if not log_path:
        return jsonify({"success": False, "error": "log_path is required"}), 400

    try:
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
        )

        return jsonify({
            "success": True,
            "message": f"Log file set: {log_path}",
            "skills": agent.get_skill_descriptions(),
            "issue_time": format_issue_time(agent.issue_time),
        })
    except Exception as e:
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

    try:
        agent = _get_or_create_agent()
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
@log_chatbot_bp.route("/reset", methods=["POST"])
def reset():
    try:
        agent = _get_or_create_agent()
        agent.reset_conversation()
        return jsonify({"success": True, "message": "Conversation reset."})
    except Exception as e:
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
        "feedback_conversation_id",   # next /log_chatbot/ visit starts a fresh conversation
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
        # Pull consolidated issue context from all session sources
        ctx = _extract_issue_context()

        # Update shared last_analyzed_log_path so the chatbot index page pre-fills it
        app_config.last_analyzed_log_path = log_path

        # Get/create per-session agent and prime it
        # skip_prime=True: we call prime_with_context explicitly below (after setting log path)
        agent = _get_or_create_agent(skip_prime=True)
        agent.current_log_path = log_path
        agent.prime_with_context(**ctx)

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
        agent.skills = skills
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
        agent.skills = skills
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
        agent.skills = skills
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


@log_chatbot_bp.route("/get_issue_context", methods=["GET"])
def get_issue_context():
    try:
        ctx = _extract_issue_context()
        attachment_time = ctx.get("attachment_time", "")
    except Exception:
        ctx = {}
        attachment_time = ""
    concise_desc = _compose_concise_description(ctx)

    # Resolve a final issue_time for sidebar display: prefer attachment_time,
    # fall back to the loaded log's latest timestamp so direct chatbot entry
    # (no session) still gets a usable value. Cached by log_path so a
    # repeat call from the frontend doesn't re-read the log's timestamp
    # range — same canonical value prime_with_context already computed.
    log_path = session.get("chatbot_log_path") or app_config.last_analyzed_log_path or ""
    issue_time_str = _resolved_issue_time_for(log_path, attachment_time)

    return jsonify({
        "description": concise_desc,
        "attachment_time": attachment_time,
        "issue_time": issue_time_str,
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
                        "resolved_issue_time": ""})

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
        # Priority 1: log file whose [first_ts, last_ts] contains issue_time
        for c in candidates:
            if c["first_ts"] and c["last_ts"]:
                if c["first_ts"] <= issue_time <= c["last_ts"]:
                    return jsonify({
                        "best_path": c["etl_path"],
                        "reason": f"Log covers issue time {issue_time_str} "
                                  f"(range: {c['first_ts']} ~ {c['last_ts']})",
                        "resolved_issue_time": resolved_issue_time_str,
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
                          f"delta: {best_delta:.0f}s)",
                "resolved_issue_time": resolved_issue_time_str,
            })

    # --- Fallback: pick the log with the latest last_ts ---
    candidates_with_ts = [c for c in candidates if c["last_ts"]]
    if candidates_with_ts:
        latest = max(candidates_with_ts, key=lambda c: c["last_ts"])
        return jsonify({
            "best_path": latest["etl_path"],
            "reason": f"No issue time provided; picked latest log "
                      f"(range: {latest['first_ts']} ~ {latest['last_ts']})",
            "resolved_issue_time": resolved_issue_time_str,
        })

    # --- Ultimate fallback ---
    return jsonify({
        "best_path": candidates[0]["etl_path"],
        "reason": "Could not determine timestamps; defaulting to first.",
        "resolved_issue_time": "",
    })