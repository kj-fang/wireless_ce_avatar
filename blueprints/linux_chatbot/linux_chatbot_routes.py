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
from services.linux_chatbot_service import LinuxWifiLogAgentSystem, load_skills_from_data_dir, get_builtin_skills, build_skill_file_map, load_skills_from_yaml
from utils.etl_utils import extract_time_from_description
from utils.issue_time_utils import (
    parse_issue_time_string,
    read_log_time_range,
    resolve_issue_time,
    format_issue_time,
)
from utils.issue_time_ai import build_issue_time_suggestions, organize_issue_context, realign_times_to_log, find_nearest_event_error
from services import feedback_service
from services import gather_service
from services import history_service
from services import chat_jobs

linux_chatbot_bp = Blueprint("linux_chatbot", __name__, url_prefix="/linux_chatbot")

# Server-side store: session_id -> LinuxWifiLogAgentSystem instance
_chatbot_instances: dict = {}


# ------------------------------------------------------------------
# Feedback sidecar helpers (anonymous, side-car, never blocks chat)
# ------------------------------------------------------------------
def _ensure_feedback_conversation_id(*, rotate: bool = False) -> str:
    if rotate or not session.get("linux_feedback_conversation_id"):
        session["linux_feedback_conversation_id"] = str(uuid.uuid4())
    return session["linux_feedback_conversation_id"]


def _export_agent_context(agent) -> list:
    try:
        return agent.export_conversation_context()
    except Exception as e:
        print(f"[linux_history] context export failed: {e}")
        return []


def _extract_issue_context() -> dict:
    raw_ctx = session.get("case_context", {})
    ctx = CaseContext.from_session(raw_ctx) if raw_ctx else CaseContext()

    classification = session.get("classification", {})
    issue_type = (classification.get("issue_type", "")
                  if isinstance(classification, dict) else "")

    ai_analysis = session.get("ai_ips_analysis", {})
    if not isinstance(ai_analysis, dict):
        ai_analysis = {}

    description_parts = []
    if ctx.description:
        description_parts.append(ctx.description)

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
    cached = session.get("_attachment_time_cache")
    if cached is not None:
        attachment_time = cached
    else:
        def _desc_time_to_str(desc: str) -> str:
            parsed = extract_time_from_description(desc)
            if hasattr(parsed, 'strftime'):
                return parsed.strftime('%m/%d/%Y-%H:%M:%S')
            if isinstance(parsed, str) and parsed.strip():
                return parsed.strip()
            return ""

        selected_files = session.get("selected_files", [])
        selected_names = set()
        for sf in selected_files:
            if isinstance(sf, (list, tuple)) and len(sf) >= 1:
                selected_names.add(sf[0])

        raw_ctx_dict = session.get("case_context", {})
        if isinstance(raw_ctx_dict, dict) and raw_ctx_dict:
            raw_ctx_dict = CaseContext.from_session(raw_ctx_dict).to_dict()
        att_list = raw_ctx_dict.get("attachment_list", []) if isinstance(raw_ctx_dict, dict) else []

        candidates = [item for item in att_list
                      if isinstance(item, (list, tuple)) and len(item) >= 3
                      and (not selected_names or item[0] in selected_names)]
        if not candidates:
            candidates = [item for item in att_list if isinstance(item, (list, tuple)) and len(item) >= 3]

        for item in candidates:
            desc_raw = item[2][1] if isinstance(item[2], (list, tuple)) and len(item[2]) >= 2 else None
            result = _desc_time_to_str(desc_raw)
            if result:
                attachment_time = result
                break

        if not attachment_time:
            for file_info in selected_files:
                if isinstance(file_info, (list, tuple)) and len(file_info) >= 3:
                    desc_raw = file_info[2][1] if isinstance(file_info[2], (list, tuple)) and len(file_info[2]) >= 2 else None
                    result = _desc_time_to_str(desc_raw)
                    if result:
                        attachment_time = result
                        break

        session["_attachment_time_cache"] = attachment_time

    return {
        "case_nbr":    ctx.case_nbr or "",
        "subject":     ctx.subject or "",
        "description": "\n".join(description_parts),
        "issue_type":  issue_type,
        "attachment_time": attachment_time,
    }


def _resolved_issue_time_for(log_path: str, attachment_time: str) -> str:
    cache = session.get("_linux_resolved_issue_time_cache") or {}
    cache_key = log_path or "__nolog__"
    if cache_key in cache:
        return cache[cache_key]
    dt, _ = resolve_issue_time(attachment_time, log_path)
    formatted = format_issue_time(dt)
    cache[cache_key] = formatted
    session["_linux_resolved_issue_time_cache"] = cache
    return formatted


def _extract_disconnect_time(*text_sources: str) -> str:
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
    try:
        if ctx is None:
            ctx = _extract_issue_context()
    except Exception:
        return "Perform full multi-skill Linux WiFi log analysis"

    subject = ctx.get("subject", "")
    desc_raw = ctx.get("description", "")
    attachment_time = ctx.get("attachment_time", "")
    if attachment_time:
        time_hint = f" at around {attachment_time}"
    else:
        time_hint = _extract_disconnect_time(subject, desc_raw)

    if subject:
        clean = re.sub(r'^(\[.*?\]\s*)+', '', subject).strip()
        clean = re.sub(r'\s*\(F/R.*?\)\s*$', '', clean).strip()
        if clean:
            return f"{clean}{time_hint}"

    if desc_raw:
        return desc_raw[:200].strip() + time_hint

    return "Perform full multi-skill Linux WiFi log analysis"


def _get_or_create_agent(skip_prime: bool = False) -> LinuxWifiLogAgentSystem:
    sid = session.get("linux_chatbot_session_id")
    if not sid or sid not in _chatbot_instances:
        sid = str(uuid.uuid4())
        session["linux_chatbot_session_id"] = sid

    if sid not in _chatbot_instances:
        base = app_config.linux_chatbot_agent
        if base is None:
            llm_helper = app_config.llm_helper
            if llm_helper is None or llm_helper.client is None:
                raise RuntimeError(
                    "Linux WiFi Chatbot Agent is not available. "
                    "The app may not have an API key configured."
                )
            base = LinuxWifiLogAgentSystem(
                client=llm_helper.client,
                model=getattr(llm_helper, "model", "gpt-4.1"),
                skills=getattr(llm_helper, "skills", None),
            )
        agent = type(base)(
            client=base.client,
            model=base.model,
            skills=base.skills,
        )
        ace_runner = getattr(base, "ace_runner", None)
        if ace_runner is not None:
            agent.attach_ace(ace_runner)

        restored_log = (session.get("linux_chatbot_log_path")
                        or app_config.last_analyzed_log_path or "")
        if restored_log:
            agent.current_log_path = restored_log

        if not skip_prime:
            try:
                ctx = _extract_issue_context()
                if any(ctx.values()):
                    agent.prime_with_context(**ctx)
            except Exception:
                pass
        _chatbot_instances[sid] = agent

    return _chatbot_instances[sid]


def _resume_agent_for(conversation_id: str):
    job = chat_jobs.get_job(conversation_id)
    if job is not None and getattr(job, "agent", None) is not None and job.status != "running":
        sid = session.get("linux_chatbot_session_id")
        if not sid:
            sid = str(uuid.uuid4())
            session["linux_chatbot_session_id"] = sid
        _chatbot_instances[sid] = job.agent
        return job.agent
    return _get_or_create_agent()


def _terminal_sse(job, kind: str, payload) -> str:
    if kind == "done":
        return ("data: " + json.dumps(
            {"type": "done", "turn_id": job.turn_id,
             "conversation_id": job.conversation_id, "result": payload},
            ensure_ascii=False) + "\n\n")
    return ("data: " + json.dumps(
        {"type": "error", "content": payload}, ensure_ascii=False) + "\n\n")


def _job_sse(job):
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
@linux_chatbot_bp.route("/", methods=["GET"])
def index():
    suggested_log = app_config.last_analyzed_log_path or ""
    issue_desc = ""
    try:
        ctx = _extract_issue_context()
        issue_desc = ctx.get("description", "")
    except Exception:
        ctx = {}

    try:
        if suggested_log and not session.get("_linux_issue_ai_quick"):
            _first_ts, _last_ts = read_log_time_range(suggested_log)
            _issue_context_organized(ctx.get("description", "") or "", _first_ts, _last_ts)
    except Exception as _warm_err:
        print(f"⚠️ Linux chatbot index pre-warm skipped: {_warm_err}")

    return render_template("linux_chatbot.html", suggested_log=suggested_log, issue_description=issue_desc)


# ------------------------------------------------------------------
# API: open native file browser and return selected path
# ------------------------------------------------------------------
@linux_chatbot_bp.route("/browse", methods=["GET"])
def browse():
    """Open a native file dialog to select a Linux WiFi log file."""
    result = {"path": ""}

    def _open_dialog():
        root = tk.Tk()
        root.withdraw()
        root.wm_attributes("-topmost", True)
        path = filedialog.askopenfilename(
            title="Select Linux WiFi log file",
            filetypes=[("Log files", "*.log *.txt"), ("All files", "*.*")],
        )
        root.destroy()
        result["path"] = path or ""

    t = threading.Thread(target=_open_dialog)
    t.start()
    t.join(timeout=60)

    return jsonify({"success": True, "path": result["path"]})


# ------------------------------------------------------------------
# API: set log file path
# ------------------------------------------------------------------
@linux_chatbot_bp.route("/set_log", methods=["POST"])
def set_log():
    data = request.get_json(silent=True) or {}
    log_path = data.get("log_path", "").strip()
    if not log_path:
        return jsonify({"success": False, "error": "log_path is required"}), 400

    try:
        prev_log_path = (session.get("linux_chatbot_log_path") or "").strip()
        rotated = bool(prev_log_path) and prev_log_path != log_path
        prev_conv_id = (session.get("linux_feedback_conversation_id") or "") if rotated else ""

        same_log = bool(prev_log_path) and prev_log_path == log_path

        agent = _get_or_create_agent(skip_prime=True)
        agent.current_log_path = log_path
        if not same_log:
            agent.reset_conversation()
        ctx = _extract_issue_context()
        preserved_history = list(agent.conversation_history or []) if same_log else []
        agent.prime_with_context(**ctx)

        session["linux_chatbot_log_path"] = log_path

        new_conv_id = _ensure_feedback_conversation_id(rotate=not same_log)

        if same_log:
            if preserved_history:
                agent.conversation_history = preserved_history
            else:
                try:
                    stored = history_service.get_context(new_conv_id, domain="linux")
                    if stored:
                        agent.import_conversation_context(stored)
                except Exception as _e:
                    print(f"[linux set_log] context restore skipped: {_e}")
        feedback_service.ensure_conversation(
            conversation_id=new_conv_id,
            session_id=session.get("linux_chatbot_session_id", ""),
            issue=ctx,
            log_path=log_path,
            domain="linux",
        )

        try:
            log_span_minutes = agent.get_log_span_minutes()
        except Exception:
            log_span_minutes = 0

        try:
            _first_ts, _last_ts = read_log_time_range(log_path)
            log_last_time = format_issue_time(_last_ts) if _last_ts else ""
        except Exception:
            log_last_time = ""

        try:
            log_has_date = agent._log_has_date()
        except Exception:
            log_has_date = True

        return jsonify({
            "success": True,
            "message": f"Log file set: {log_path}",
            "skills": agent.get_skill_descriptions(),
            "issue_time": format_issue_time(agent.issue_time),
            "log_span_minutes": log_span_minutes,
            "log_last_time": log_last_time,
            "log_has_date": log_has_date,
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
@linux_chatbot_bp.route("/suggest_issue_times", methods=["POST"])
def suggest_issue_times():
    data = request.get_json(silent=True) or {}
    text = (data.get("text") or "").strip()
    try:
        agent = _get_or_create_agent()
        log_path = agent.current_log_path or ""
        first_ts, last_ts = read_log_time_range(log_path) if log_path else (None, None)

        log_lines = []
        try:
            if not agent._ensure_raw_log_cache():
                log_lines = agent._raw_log_cache or []
        except Exception:
            log_lines = []

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
            event_log_events=[],
        )
        return jsonify(payload), (200 if payload.get("success") else 503)
    except Exception as e:
        traceback.print_exc()
        return jsonify({"success": False, "error": str(e)}), 500


# ------------------------------------------------------------------
# API: chat
# ------------------------------------------------------------------
@linux_chatbot_bp.route("/chat", methods=["POST"])
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

    parent_message_id = (data.get("parent_message_id") or "").strip()

    issue_time_window_minutes = None
    if "issue_time_window_minutes" in data:
        try:
            issue_time_window_minutes = max(0, min(100000, int(data.get("issue_time_window_minutes"))))
        except (TypeError, ValueError):
            issue_time_window_minutes = None

    try:
        conversation_id = _ensure_feedback_conversation_id()
        agent = _resume_agent_for(conversation_id)
        if issue_time_window_minutes is not None:
            agent.issue_time_window_minutes = issue_time_window_minutes
        if not agent.current_log_path:
            agent.current_log_path = (
                session.get("linux_chatbot_log_path")
                or app_config.last_analyzed_log_path or ""
            )
        if not agent.current_log_path:
            def _no_log():
                yield f"data: {json.dumps({'type': 'error', 'content': 'No log file loaded. Please set a log file first.'})}\n\n"
            return Response(_no_log(), mimetype="text/event-stream")

        if "issue_time" in data:
            raw_it = (data.get("issue_time") or "").strip()
            explicitly_cleared = bool(data.get("issue_time_cleared", False))
            if raw_it:
                if isinstance(agent.issue_context, dict):
                    agent.issue_context.pop("attachment_time", None)
                parsed, is_time_only = parse_issue_time_string(raw_it)
                agent.issue_time = parsed
                agent._issue_time_time_only = is_time_only
            elif explicitly_cleared:
                agent.issue_time = None
                if isinstance(agent.issue_context, dict):
                    agent.issue_context.pop("attachment_time", None)
                    for k in ("description", "subject"):
                        v = agent.issue_context.get(k)
                        if isinstance(v, str) and v:
                            v = re.sub(r'\d{1,2}/\d{1,2}/\d{4}[\s-]\d{1,2}:\d{2}:\d{2}(?:\.\d{1,3})?', '', v)
                            v = re.sub(r'\d{4}[-/]\d{1,2}[-/]\d{1,2}[\sT]\d{1,2}:\d{2}:\d{2}', '', v)
                            v = re.sub(r'\b\d{1,2}:\d{2}:\d{2}(?:\.\d{1,3})?\b', '', v)
                            agent.issue_context[k] = re.sub(r'\s+', ' ', v).strip()

        session_id = session.get("linux_chatbot_session_id", "")
        turn_id = str(uuid.uuid4())
        turn_started_at = datetime.now()
        try:
            _issue_ctx_for_snapshot = _extract_issue_context()
        except Exception:
            _issue_ctx_for_snapshot = {}

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
                domain="linux",
                turn_id=turn_id,
            )
        except Exception:
            pass

        use_tools = bool(data.get("use_tools", False))

        if use_tools:
            collected_steps: list = []

            job = chat_jobs.start_job(
                conversation_id=conversation_id,
                turn_id=turn_id,
                title=user_message,
                agent=agent,
                domain="linux",
            )
            if session_id:
                _chatbot_instances.pop(session_id, None)

            def step_cb(step):
                try:
                    if isinstance(step, dict):
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
                    try:
                        gather_service.record_usage(
                            conversation_id=conversation_id,
                            workflow_id=session.get("gather_workflow_id", ""),
                            model=getattr(agent, "model", "") or "",
                            usage=getattr(agent, "last_turn_usage", None),
                            issue=_issue_ctx_for_snapshot,
                            domain="linux",
                            turn_id=turn_id,
                            latency_ms=int((datetime.now() - turn_started_at).total_seconds() * 1000),
                        )
                    except Exception:
                        pass
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
                        domain="linux",
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
                        domain="linux",
                        steps=collected_steps,
                        agent_context=_export_agent_context(agent),
                    )
                    chat_jobs.finish_job(job, result)
                except Exception as exc:
                    error_tb = traceback.format_exc()
                    print(f"\u274c Linux Chat-with-tools thread error:\n{error_tb}")
                    try:
                        gather_service.record_turn_status(
                            conversation_id=conversation_id,
                            turn_id=turn_id,
                            status="failed",
                            workflow_id=session.get("gather_workflow_id", ""),
                            issue=_issue_ctx_for_snapshot,
                            domain="linux",
                            latency_ms=int((datetime.now() - turn_started_at).total_seconds() * 1000),
                            error_code=type(exc).__name__,
                        )
                    except Exception:
                        pass
                    chat_jobs.fail_job(job, str(exc))

            t = threading.Thread(target=run_chat_with_tools, daemon=True)
            t.start()

            return Response(
                _job_sse(job),
                mimetype="text/event-stream",
                headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
            )
        else:
            result = agent.chat(
                user_message,
                use_tools=False,
                temperature=temperature,
                max_tokens=max_tokens,
            )
            try:
                gather_service.record_usage(
                    conversation_id=conversation_id,
                    workflow_id=session.get("gather_workflow_id", ""),
                    model=getattr(agent, "model", "") or "",
                    usage=getattr(agent, "last_turn_usage", None),
                    issue=_issue_ctx_for_snapshot,
                    domain="linux",
                    turn_id=turn_id,
                    latency_ms=int((datetime.now() - turn_started_at).total_seconds() * 1000),
                )
            except Exception:
                pass

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
                domain="linux",
            )
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
                domain="linux",
            )

            def generate():
                yield f"data: {json.dumps({'type': 'done', 'turn_id': turn_id, 'conversation_id': conversation_id, 'result': result}, ensure_ascii=False)}\n\n"

            return Response(generate(), mimetype="text/event-stream")
    except Exception as e:
        error_traceback = traceback.format_exc()
        print(f"\u274c Linux Chatbot error:\n{error_traceback}")
        try:
            if conversation_id and turn_id:
                gather_service.record_turn_status(
                    conversation_id=conversation_id,
                    turn_id=turn_id,
                    status="failed",
                    workflow_id=session.get("gather_workflow_id", ""),
                    issue=locals().get("_issue_ctx_for_snapshot") or {},
                    domain="linux",
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
@linux_chatbot_bp.route("/reset", methods=["POST"])
def reset():
    try:
        agent = _get_or_create_agent()
        agent.reset_conversation()
        return jsonify({"success": True, "message": "Conversation reset."})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


# ------------------------------------------------------------------
# API: stop the in-flight tools-mode analysis
# ------------------------------------------------------------------
@linux_chatbot_bp.route("/chat/stop", methods=["POST"])
def chat_stop():
    try:
        data = request.get_json(silent=True) or {}
        conversation_id = (data.get("conversation_id") or "").strip()
        if not conversation_id:
            conversation_id = (session.get("linux_feedback_conversation_id") or "").strip()
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
                    domain="linux",
                )
            except Exception:
                pass
        return jsonify({"success": True, "stopped": bool(stopped)})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


# ------------------------------------------------------------------
# API: local conversation history
# ------------------------------------------------------------------
@linux_chatbot_bp.route("/history/list", methods=["GET"])
def history_list():
    try:
        conversations = history_service.list_conversations(domain="linux")
        try:
            running = {j["conversation_id"]: j for j in chat_jobs.active_summaries(domain="linux")}
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
                            "updated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
                            "turn_count": j.get("step_count", 0),
                            "log_path": "",
                            "pinned": False,
                            "running": True,
                        })
            conversations.sort(key=lambda c: c.get("updated_at") or "", reverse=True)
            conversations.sort(key=lambda c: bool(c.get("running")), reverse=True)
            conversations.sort(key=lambda c: bool(c.get("pinned")), reverse=True)
        except Exception:
            pass
        return jsonify({"success": True, "conversations": conversations})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@linux_chatbot_bp.route("/history/stream", methods=["GET"])
def history_stream():
    conversation_id = (request.args.get("conversation_id") or "").strip()
    headers = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}
    job = chat_jobs.get_job(conversation_id) if conversation_id else None
    if job is None or getattr(job, "domain", "") != "linux":
        def _idle():
            yield "data: " + json.dumps({"type": "idle"}) + "\n\n"
        return Response(_idle(), mimetype="text/event-stream", headers=headers)
    return Response(_job_sse(job), mimetype="text/event-stream", headers=headers)


@linux_chatbot_bp.route("/history/get", methods=["GET"])
def history_get():
    conversation_id = (request.args.get("conversation_id") or "").strip()
    if not conversation_id:
        return jsonify({"success": False, "error": "conversation_id is required"}), 400
    conv = history_service.get_conversation(conversation_id, domain="linux", with_steps=True)
    if conv is None:
        return jsonify({"success": False, "error": "Conversation not found"}), 404
    return jsonify({"success": True, "conversation": conv})


@linux_chatbot_bp.route("/history/delete", methods=["POST"])
def history_delete():
    data = request.get_json(silent=True) or {}
    conversation_id = (data.get("conversation_id") or "").strip()
    if not conversation_id:
        return jsonify({"success": False, "error": "conversation_id is required"}), 400
    removed = history_service.delete_conversation(conversation_id, domain="linux")
    if removed and (session.get("linux_feedback_conversation_id") or "") == conversation_id:
        session.pop("linux_feedback_conversation_id", None)
    return jsonify({"success": bool(removed)})


@linux_chatbot_bp.route("/history/rename", methods=["POST"])
def history_rename():
    data = request.get_json(silent=True) or {}
    conversation_id = (data.get("conversation_id") or "").strip()
    title = (data.get("title") or "").strip()
    if not conversation_id:
        return jsonify({"success": False, "error": "conversation_id is required"}), 400
    if not title:
        return jsonify({"success": False, "error": "title is required"}), 400
    ok = history_service.rename_conversation(conversation_id, title, domain="linux")
    return jsonify({"success": bool(ok)})


@linux_chatbot_bp.route("/history/pin", methods=["POST"])
def history_pin():
    data = request.get_json(silent=True) or {}
    conversation_id = (data.get("conversation_id") or "").strip()
    pinned = bool(data.get("pinned"))
    if not conversation_id:
        return jsonify({"success": False, "error": "conversation_id is required"}), 400
    ok = history_service.set_pinned(conversation_id, pinned, domain="linux")
    return jsonify({"success": bool(ok), "pinned": pinned})


@linux_chatbot_bp.route("/history/load", methods=["POST"])
def history_load():
    data = request.get_json(silent=True) or {}
    conversation_id = (data.get("conversation_id") or "").strip()
    if not conversation_id:
        return jsonify({"success": False, "error": "conversation_id is required"}), 400

    job = chat_jobs.get_job(conversation_id)
    if job is not None and getattr(job, "domain", "") != "linux":
        job = None
    conv = history_service.get_conversation(conversation_id, domain="linux", with_steps=True)
    if conv is None and job is None:
        return jsonify({"success": False, "error": "Conversation not found"}), 404

    try:
        session["linux_feedback_conversation_id"] = conversation_id

        conv = conv or {}
        running = bool(job is not None and job.status == "running")
        issue = conv.get("issue") if isinstance(conv.get("issue"), dict) else {}
        turns = conv.get("turns") or []
        log_path = (conv.get("log_path") or "").strip()

        adopted = False
        if job is not None and getattr(job, "agent", None) is not None:
            agent = job.agent
            adopted = True
            if not log_path:
                log_path = (getattr(agent, "current_log_path", "") or "").strip()
            if not running:
                sid = session.get("linux_chatbot_session_id")
                if not sid:
                    sid = str(uuid.uuid4())
                    session["linux_chatbot_session_id"] = sid
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
                allowed = {"case_nbr", "subject", "description", "issue_type", "attachment_time"}
                try:
                    agent.prime_with_context(**{k: v for k, v in issue.items()
                                                if k in allowed and isinstance(v, str)})
                except Exception as _e:
                    print(f"[linux history] prime_with_context skipped: {_e}")
            try:
                skills = agent.get_skill_descriptions()
            except Exception:
                skills = []
            try:
                log_span_minutes = agent.get_log_span_minutes()
            except Exception:
                log_span_minutes = 0
            if not running:
                try:
                    log_has_date = agent._log_has_date()
                except Exception:
                    log_has_date = True

        issue_time_str = (conv.get("issue_time") or "").strip()
        if adopted:
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

        context_restored = 0
        if not adopted:
            stored_context = history_service.get_context(conversation_id, domain="linux")
            if stored_context:
                try:
                    context_restored = agent.import_conversation_context(stored_context)
                except Exception as _e:
                    print(f"[linux history] context restore failed: {_e}")
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
            session["linux_chatbot_log_path"] = log_path

        return jsonify({
            "success": True,
            "conversation_id": conversation_id,
            "title": conv.get("title") or (job.title if job else "") or "Conversation",
            "turns": turns,
            "context_restored": context_restored,
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
# Back to Avatar
# ------------------------------------------------------------------
@linux_chatbot_bp.route("/back_to_avatar", methods=["GET"])
def back_to_avatar():
    sid = session.pop("linux_chatbot_session_id", None)
    if sid and sid in _chatbot_instances:
        try:
            _chatbot_instances.pop(sid, None)
        except Exception:
            pass

    for key in (
        "linux_chatbot_log_path",
        "case_context",
        "ai_ips_analysis",
        "classification",
        "selected_files",
        "attachment_list",
        "issue_time",
        "_attachment_time_cache",
        "_linux_resolved_issue_time_cache",
        "_linux_issue_ai_quick",
        "linux_feedback_conversation_id",
        "gather_workflow_id",
    ):
        session.pop(key, None)

    try:
        app_config.last_analyzed_log_path = ""
    except Exception:
        pass

    return redirect(url_for("main.index"))


# ------------------------------------------------------------------
# API: prepare chatbot from download_result
# ------------------------------------------------------------------
@linux_chatbot_bp.route("/prepare", methods=["POST"])
def prepare():
    data = request.get_json(silent=True) or {}
    etl_path = data.get("etl_path", "").strip()
    if not etl_path:
        return jsonify({"success": False, "error": "etl_path is required"}), 400

    lower = etl_path.lower()
    if os.path.exists(etl_path) and (lower.endswith(".log") or lower.endswith(".txt")):
        log_path = etl_path
    else:
        log_path = etl_path + ".log"
    if not os.path.exists(log_path):
        return jsonify({"success": False, "error": f"Log file not found: {log_path}"}), 404

    try:
        ctx = _extract_issue_context()
        app_config.last_analyzed_log_path = log_path
        agent = _get_or_create_agent(skip_prime=True)
        agent.current_log_path = log_path
        agent.prime_with_context(**ctx)

        try:
            _first_ts, _last_ts = read_log_time_range(log_path)
            _issue_context_organized(ctx.get("description", "") or "", _first_ts, _last_ts)
        except Exception as _org_err:
            print(f"\u26a0\ufe0f Linux chatbot prepare: issue-context organize skipped: {_org_err}")

        new_conv_id = _ensure_feedback_conversation_id(rotate=True)
        feedback_service.ensure_conversation(
            conversation_id=new_conv_id,
            session_id=session.get("linux_chatbot_session_id", ""),
            issue=ctx,
            log_path=log_path,
            domain="linux",
        )

        return jsonify({"success": True})
    except Exception as e:
        error_traceback = traceback.format_exc()
        print(f"\u274c Linux chatbot prepare error:\n{error_traceback}")
        return jsonify({"success": False, "error": str(e)}), 500


# ------------------------------------------------------------------
# API: open native directory browser
# ------------------------------------------------------------------
@linux_chatbot_bp.route("/browse_dir", methods=["GET"])
def browse_dir():
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
# API: reload skills from a directory
# ------------------------------------------------------------------
@linux_chatbot_bp.route("/reload_skills", methods=["POST"])
def reload_skills():
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
            warning = "No directory specified. Using built-in skills."

        agent = _get_or_create_agent()
        agent.apply_updated_skills(skills)
        if app_config.linux_chatbot_agent:
            app_config.linux_chatbot_agent.skills = skills
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
# API: browse for a YAML file
# ------------------------------------------------------------------
@linux_chatbot_bp.route("/browse_yaml", methods=["GET"])
def browse_yaml():
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
# API: load skills from a YAML file
# ------------------------------------------------------------------
@linux_chatbot_bp.route("/load_skills_yaml", methods=["POST"])
def load_skills_yaml_route():
    data = request.get_json(silent=True) or {}
    yaml_path = data.get("yaml_path", "").strip()

    if not yaml_path:
        return jsonify({"success": False, "error": "yaml_path is required."}), 400

    try:
        skills = load_skills_from_yaml(yaml_path)

        agent = _get_or_create_agent()
        agent.apply_updated_skills(skills)
        if app_config.linux_chatbot_agent:
            app_config.linux_chatbot_agent.skills = skills
        if app_config.llm_helper:
            app_config.llm_helper.skills = skills

        return jsonify({
            "success": True,
            "message": f"{len(skills)} skills loaded from YAML",
            "source": yaml_path,
            "skills": agent.get_skill_descriptions(),
        })
    except Exception as e:
        traceback.print_exc()
        return jsonify({"success": False, "error": str(e)}), 500


# ------------------------------------------------------------------
# API: get available skills
# ------------------------------------------------------------------
@linux_chatbot_bp.route("/skills", methods=["GET"])
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
    base = getattr(app_config, "linux_chatbot_agent", None)
    if base is not None and getattr(base, "client", None) is not None:
        return base.client, getattr(base, "model", None)
    helper = getattr(app_config, "llm_helper", None)
    if helper is not None and getattr(helper, "client", None) is not None:
        return helper.client, getattr(helper, "model", "gpt-4.1")
    return None, None


def _issue_context_organized(raw_desc: str, first_ts, last_ts) -> dict:
    quick = session.get("_linux_issue_ai_quick")
    if isinstance(quick, dict) and isinstance(quick.get("data"), dict):
        d = quick["data"]
    else:
        client, model = _get_llm_client_model()
        started_at = datetime.now()
        d, usage = organize_issue_context(
            raw_desc, first_ts=first_ts, last_ts=last_ts,
            llm_client=client, llm_model=model, return_usage=True,
        )
        session["_linux_issue_ai_quick"] = {"data": d}
        if int(usage.get("llm_calls") or 0) > 0:
            try:
                issue = CaseContext.from_session(session.get("case_context") or {}).to_dict()
                gather_service.record_feature_usage(
                    workflow_id=session.get("gather_workflow_id", ""),
                    feature_code="issue_time_prepass",
                    model=model or "", usage=usage, issue=issue, domain="linux",
                    trigger="direct_chatbot_context",
                    latency_ms=int((datetime.now() - started_at).total_seconds() * 1000),
                )
            except Exception:
                pass
    return {
        "clean_description": d.get("clean_description") or raw_desc,
        "issue_times": realign_times_to_log(d.get("issue_times") or [], first_ts, last_ts),
        "interpretation": d.get("interpretation", ""),
    }


@linux_chatbot_bp.route("/get_issue_context", methods=["GET"])
def get_issue_context():
    try:
        ctx = _extract_issue_context()
        attachment_time = ctx.get("attachment_time", "")
    except Exception:
        ctx = {}
        attachment_time = ""

    log_path = session.get("linux_chatbot_log_path") or app_config.last_analyzed_log_path or ""
    first_ts, last_ts = read_log_time_range(log_path) if log_path else (None, None)

    organized = _issue_context_organized(ctx.get("description", "") or "", first_ts, last_ts)
    clean_desc = organized.get("clean_description") or _compose_concise_description(ctx)
    issue_times = organized.get("issue_times") or []

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


# ------------------------------------------------------------------
# Skills YAML lifecycle endpoints
# ------------------------------------------------------------------
from utils.linux_skills_yaml_utils import (
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


@linux_chatbot_bp.route("/skills_yaml/status", methods=["GET"])
def skills_yaml_status():
    try:
        payload = _skills_yaml_status_payload()
        return jsonify({"success": True, **payload})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@linux_chatbot_bp.route("/skills_yaml/set_source", methods=["POST"])
def skills_yaml_set_source():
    data = request.get_json(silent=True) or {}
    source = (data.get("source") or "cloud").strip().lower()
    effective = _set_active_source(source)
    chosen_yaml, chosen_date, chosen_source = _current_active_yaml()
    if chosen_yaml is not None and chosen_yaml.exists():
        try:
            skills = load_skills_from_yaml(str(chosen_yaml))
            agent = _get_or_create_agent()
            agent.apply_updated_skills(skills)
            if app_config.linux_chatbot_agent:
                app_config.linux_chatbot_agent.skills = skills
            return jsonify({
                "success": True,
                "active_source": effective,
                "skills": agent.get_skill_descriptions(),
            })
        except Exception as e:
            return jsonify({"success": False, "error": str(e)}), 500
    return jsonify({"success": True, "active_source": effective, "skills": []})


@linux_chatbot_bp.route("/skills_yaml/read", methods=["GET"])
def skills_yaml_read():
    source = (request.args.get("source") or _get_active_source()).strip().lower()
    if source == "user":
        path, _ = _latest_user_yaml()
    else:
        path, _ = _latest_cloud_baseline()
    if path is None or not path.exists():
        return jsonify({"success": False, "error": f"No {source} YAML file found."}), 404
    try:
        import yaml
        with open(path, "r", encoding="utf-8") as f:
            content = f.read()
            data = yaml.safe_load(content) or {}
        return jsonify({"success": True, "source": source, "path": str(path), "data": data, "raw": content})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@linux_chatbot_bp.route("/skills_yaml/save", methods=["POST"])
def skills_yaml_save():
    """Save an edited skills dict as a new dated user YAML and reload."""
    import yaml
    data = request.get_json(silent=True) or {}
    skills_data = data.get("skills")
    if not isinstance(skills_data, dict):
        return jsonify({"success": False, "error": "skills must be a dict"}), 400

    user_dir = _user_local_dir()
    user_dir.mkdir(parents=True, exist_ok=True)
    filename = _today_yaml_filename()
    save_path = user_dir / filename

    try:
        with open(save_path, "w", encoding="utf-8") as f:
            f.write(yaml.dump(skills_data, allow_unicode=True, sort_keys=False))
        _set_active_source("user")
        skills = load_skills_from_yaml(str(save_path))
        agent = _get_or_create_agent()
        agent.apply_updated_skills(skills)
        if app_config.linux_chatbot_agent:
            app_config.linux_chatbot_agent.skills = skills
        return jsonify({
            "success": True,
            "path": str(save_path),
            "skills": agent.get_skill_descriptions(),
        })
    except Exception as e:
        traceback.print_exc()
        return jsonify({"success": False, "error": str(e)}), 500
