from flask import render_template, request, session, jsonify, Response, copy_current_request_context
from services.chatbot.issue_context import extract_disconnect_time as _extract_disconnect_time
from services.chatbot.shared_routes import leave_chatbot as _leave_chatbot
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

from configs.chatbot_ui import WIFI_UI
from configs.global_configs import app_config
from models.models import CaseContext
from services.chatbot.engine.network_experience import NwAnalysisAgentSystem
from services.chatbot.engine.system import load_skills_from_yaml
from services.sleepstudy_analyzer import analyze_sleepstudy_stream
from services import gather_service
from utils.etl_utils import extract_time_from_description
from utils.event_log_utils import find_event_log_for_log


# Server-side store: session_id -> NwAnalysisAgentSystem instance
_chatbot_instances: dict = {}

# Gather analytics domain for this blueprint. Kept as a constant so the NW
# records stay distinguishable from the wifi ones even though both are driven
# by a class named WifiLogAgentSystem.
GATHER_DOMAIN = "nw"


def _ensure_nw_conversation_id(*, rotate: bool = False) -> str:
    """
    Return the current NW conversation_id, creating one if missing.

    Every Send in one conversation shares this id, so the Gather session
    record accumulates turns instead of fragmenting into one record per
    message. Rotated by /reset, which starts a genuinely new conversation.
    """
    if rotate or not session.get("nw_conversation_id"):
        session["nw_conversation_id"] = str(uuid.uuid4())
    return session["nw_conversation_id"]


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

    return {
        "case_nbr":    ctx.case_nbr or "",
        "subject":     ctx.subject or "",
        "description": "\n".join(description_parts),
        "issue_type":  issue_type,
        "attachment_time": attachment_time,
    }




def _compose_concise_description() -> str:
    """
    Auto-compose the most effective issue description for auto-analysis via chat.

        Format: "<problem statement> <timestamp>"
        e.g. "6G Weak Signal disconnected at around 10/28/2025-11:25:49"
    """
    try:
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


def _get_or_create_agent() -> NwAnalysisAgentSystem:
    """
    Return a per-session NwAnalysisAgentSystem.
    Borrows client/model from app_config.nw_analysis_agent which is
    initialised at app startup (set_up_app.py -> set_up()).
    """
    sid = session.get("chatbot_session_id")
    if not sid or sid not in _chatbot_instances:
        sid = str(uuid.uuid4())
        session["chatbot_session_id"] = sid

    if sid not in _chatbot_instances:
        base = app_config.nw_analysis_agent
        if base is None:
            # Fallback: try to build from llm_helper directly
            llm_helper = app_config.llm_helper
            if llm_helper is None or llm_helper.client is None:
                raise RuntimeError(
                    "Log Chatbot Agent is not available. "
                    "The app may not have an API key configured."
                )
            base = NwAnalysisAgentSystem(
                client=llm_helper.client,
                model=getattr(llm_helper, "model", "gpt-4.1"),
                skills=getattr(llm_helper, "skills", None),
            )
        # Create a fresh per-session instance sharing the same client + skills
        agent = NwAnalysisAgentSystem(
            client=base.client,
            model=base.model,
            skills=base.skills,   # reuse pre-loaded skills, no disk re-read
        )
        # Auto-populate log path from last LogParser analysis if available
        if app_config.last_analyzed_log_path:
            agent.current_log_path = app_config.last_analyzed_log_path
        # Prime with session issue context so every new session is context-aware
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
        ui=WIFI_UI,
        suggested_log=suggested_log,
        issue_description=issue_desc,
    )


# ------------------------------------------------------------------
# API: open native file browser and return selected path
# ------------------------------------------------------------------
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
def set_log():
    data = request.get_json(silent=True) or {}
    log_path = data.get("log_path", "").strip()
    if not log_path:
        return jsonify({"success": False, "error": "log_path is required"}), 400

    try:
        agent = _get_or_create_agent()
        agent.current_log_path = log_path
        agent.reset_conversation()          # fresh conversation for a new file
        ctx = _extract_issue_context()      # re-extract context in case session was updated after agent creation
        if any(ctx.values()):
            agent.prime_with_context(**ctx)

        session["chatbot_log_path"] = log_path
        try:
            event_log_path = find_event_log_for_log(log_path)
        except Exception:
            event_log_path = ""
        return jsonify({
            "success": True,
            "message": f"Log file set: {log_path}",
            "skills": agent.get_skill_descriptions(),
            "evtx_path": event_log_path,
        })
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


# ------------------------------------------------------------------
# API: set sleepstudy file path (same as set_log but no session write
# and no skills payload in response)
# ------------------------------------------------------------------
def set_log_sleepstudy():
    data = request.get_json(silent=True) or {}
    log_path = data.get("log_path", "").strip()
    if not log_path:
        return jsonify({"success": False, "error": "log_path is required"}), 400

    try:
        agent = _get_or_create_agent()
        agent.current_log_path = log_path
        agent.reset_conversation()          # fresh conversation for a new file
        ctx = _extract_issue_context()      # re-extract context in case session was updated after agent creation
        if any(ctx.values()):
            agent.prime_with_context(**ctx)

        return jsonify({
            "success": True,
            "message": f"Sleepstudy file set: {log_path}",
        })
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


# ------------------------------------------------------------------
# API: analyze sleepstudy via the script (SSE stream)
# ------------------------------------------------------------------
def analyze_sleepstudy():
    """
    Run the sleepstudy_analyzer.py pipeline against the given .html report
    and stream progress + per-session reports back as SSE events compatible
    with the existing chat UI consumer.

    Request JSON: { "log_path": "<absolute path to sleepstudy-report.html>" }
    """
    data = request.get_json(silent=True) or {}
    sleep_path = (data.get("log_path") or "").strip()
    if not sleep_path:
        def _err():
            yield f"data: {json.dumps({'type': 'error', 'content': 'log_path is required'})}\n\n"
        return Response(_err(), mimetype="text/event-stream")

    if not os.path.exists(sleep_path):
        def _missing():
            yield f"data: {json.dumps({'type': 'error', 'content': f'File not found: {sleep_path}'})}\n\n"
        return Response(_missing(), mimetype="text/event-stream")

    @copy_current_request_context
    def event_stream():
        # Build an llm_call adapter from the configured OpenAI-style client.
        llm_helper = app_config.llm_helper
        llm_client = getattr(llm_helper, "client", None) if llm_helper else None
        llm_model  = getattr(llm_helper, "model", "gpt-4.1") if llm_helper else "gpt-4.1"

        # This route drives the LLM through a local closure rather than the
        # agent, so it keeps its own counters. One analysis makes many calls.
        spend = {"llm_calls": 0, "input_tokens": 0, "cache_read_tokens": 0,
                 "cache_write_tokens": 0, "output_tokens": 0, "total_tokens": 0}
        started = datetime.now()

        if llm_client is None:
            llm_call = None
        else:
            def llm_call(system_prompt: str, user_message: str) -> str:
                resp = llm_client.chat.completions.create(
                    model=llm_model,
                    messages=[
                        {"role": "system", "content": system_prompt},
                        {"role": "user",   "content": user_message},
                    ],
                    temperature=0.2,
                    max_tokens=1500,
                )
                try:
                    u = getattr(resp, "usage", None)
                    if u is not None:
                        def _n(name: str) -> int:
                            try:
                                return max(0, int(getattr(u, name, 0) or 0))
                            except (TypeError, ValueError):
                                return 0
                        prompt_t, out_t = _n("prompt_tokens"), _n("completion_tokens")
                        cr, cw = _n("cache_read_input_tokens"), _n("cache_creation_input_tokens")
                        spend["llm_calls"] += 1
                        spend["input_tokens"] += prompt_t
                        spend["output_tokens"] += out_t
                        spend["cache_read_tokens"] += cr
                        spend["cache_write_tokens"] += cw
                        spend["total_tokens"] += prompt_t + out_t + cr + cw
                except Exception:
                    pass
                return resp.choices[0].message.content or ""

        status, error_code = "success", ""
        try:
            for event in analyze_sleepstudy_stream(sleep_path, llm_call=llm_call):
                kind = event["type"]
                if kind == "step":
                    sse = {"type": "step", "step": {"content": event["content"]}}
                elif kind == "done":
                    sse = {"type": "done", "result": {"type": "text", "data": event["data"]}}
                elif kind == "error":
                    sse = {"type": "error", "content": event["content"]}
                    status, error_code = "failed", "analyzer_error"
                else:
                    continue
                yield f"data: {json.dumps(sse, ensure_ascii=False)}\n\n"

        except Exception as exc:
            tb = traceback.format_exc()
            print(f"analyze_sleepstudy error:\n{tb}")
            status, error_code = "failed", type(exc).__name__
            err_payload = {"type": "error", "content": str(exc)}
            yield f"data: {json.dumps(err_payload, ensure_ascii=False)}\n\n"
        finally:
            # Book the spend regardless of outcome — the tokens were billed.
            try:
                gather_service.record_feature_usage(
                    workflow_id=session.get("gather_workflow_id", ""),
                    feature_code="sleepstudy_analysis",
                    model=llm_model,
                    usage=spend,
                    issue=_extract_issue_context(),
                    domain=GATHER_DOMAIN,
                    trigger="click_ai",
                    status=status,
                    latency_ms=int((datetime.now() - started).total_seconds() * 1000),
                    error_code=error_code,
                )
            except Exception:
                pass

    return Response(
        event_stream(),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


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

    try:
        agent = _get_or_create_agent()
        if not agent.current_log_path:
            def _no_log():
                yield f"data: {json.dumps({'type': 'error', 'content': 'No log file loaded. Please set a log file first.'})}\n\n"
            return Response(_no_log(), mimetype="text/event-stream")

        conversation_id = _ensure_nw_conversation_id()
        turn_id = str(uuid.uuid4())
        session["nw_active_conversation_id"] = conversation_id
        session["nw_active_turn_id"] = turn_id
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
                # _get_or_create_agent() stores the NW runtime identifier under
                # chatbot_session_id, matching the Wi-Fi and BT chatbots.
                session_id=session.get("chatbot_session_id", "") or "",
                user_message=user_message,
                issue=_issue_ctx_for_snapshot,
                log_path=getattr(agent, "current_log_path", "") or "",
                issue_time="",
                issue_time_window_minutes=None,
                domain=GATHER_DOMAIN,
                turn_id=turn_id,
            )
        except Exception:
            pass

        def _record_turn_cost(status: str = "completed", error_code: str = "") -> None:
            """Settle this turn's tokens + USD. Never raises."""
            try:
                gather_service.record_usage(
                    conversation_id=conversation_id,
                    workflow_id=session.get("gather_workflow_id", ""),
                    model=getattr(agent, "model", "") or "",
                    usage=getattr(agent, "last_turn_usage", None),
                    issue=_issue_ctx_for_snapshot,
                    domain=GATHER_DOMAIN,
                    turn_id=turn_id,
                    latency_ms=int((datetime.now() - turn_started_at).total_seconds() * 1000),
                    status=status,
                    error_code=error_code,
                )
            except Exception:
                pass

        # Use the mode flag sent by the frontend toggle.
        use_tools = bool(data.get("use_tools", False))

        if use_tools:
            import queue as _queue
            step_queue = _queue.Queue()

            def step_cb(step):
                step_queue.put(("step", step))

            @copy_current_request_context
            def run_chat_with_tools():
                turn_status = "completed"
                error_code = ""
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
                    turn_status = "failed"
                    error_code = type(exc).__name__
                    error_tb = traceback.format_exc()
                    print(f"❌ Chat-with-tools thread error:\n{error_tb}")
                    step_queue.put(("error", str(exc)))
                finally:
                    # Book the spend even when the turn errored or was
                    # cancelled — those tokens were still billed.
                    _record_turn_cost(turn_status, error_code)

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
                        yield f"data: {json.dumps({'type': 'done', 'result': payload}, ensure_ascii=False)}\n\n"
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
            # Cost accounting — see the tools branch above.
            _record_turn_cost()

            def generate():
                yield f"data: {json.dumps({'type': 'done', 'result': result}, ensure_ascii=False)}\n\n"

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
                    domain=GATHER_DOMAIN,
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
# ------------------------------------------------------------------
# API: stop the in-flight tools-mode analysis
#
# NW analysis runs its tools loop on a per-request background thread using the
# per-session agent (no chat_jobs registry). Setting that agent's cancel_event
# makes the loop bail out at the next reasoning-step boundary; the stream then
# emits its terminal "done" with a "stopped" notice. Idempotent no-op when
# nothing is running.
# ------------------------------------------------------------------
def chat_stop():
    try:
        sid = session.get("chatbot_session_id", "")
        agent = _chatbot_instances.get(sid) if sid else None
        stopped = False
        if agent is not None and hasattr(agent, "cancel_event"):
            agent.cancel_event.set()
            stopped = True
        if stopped:
            try:
                gather_service.record_turn_status(
                    conversation_id=session.get("nw_active_conversation_id", ""),
                    turn_id=session.get("nw_active_turn_id", ""),
                    status="cancelled",
                    workflow_id=session.get("gather_workflow_id", ""),
                    issue=_extract_issue_context(),
                    domain=GATHER_DOMAIN,
                )
            except Exception:
                pass
        return jsonify({"success": True, "stopped": stopped})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


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
        agent = _get_or_create_agent()
        agent.current_log_path = log_path
        agent.prime_with_context(**ctx)

        return jsonify({"success": True})
    except Exception as e:
        error_traceback = traceback.format_exc()
        print(f"❌ Chatbot prepare error:\n{error_traceback}")
        return jsonify({"success": False, "error": str(e)}), 500


# ------------------------------------------------------------------
# API: open native directory browser and return selected path
# ------------------------------------------------------------------


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
        agent.skills = skills
        # Also update app-level so future sessions share the new skills
        if app_config.nw_analysis_agent:
            app_config.nw_analysis_agent.skills = skills
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
        agent.skills = skills
        if app_config.nw_analysis_agent:
            app_config.nw_analysis_agent.skills = skills
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
def get_issue_context():
    # Use _compose_concise_description() instead to get a cleaned and concise title/description.
    concise_desc = _compose_concise_description()
    try:
        ctx = _extract_issue_context()
        attachment_time = ctx.get("attachment_time", "")
    except Exception:
        attachment_time = ""
    return jsonify({"description": concise_desc, "attachment_time": attachment_time})


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
    issue_time = _parse_issue_time(issue_time_str)

    # --- Scan each log file for its time range ---
    TIME_PATTERN = re.compile(r'(\d{2}/\d{2}/\d{4}-\d{2}:\d{2}:\d{2}\.\d{3})')
    candidates = []

    for etl_path in etl_paths:
        log_path = etl_path + ".log"
        if not os.path.exists(log_path):
            continue

        first_ts, last_ts = None, None
        try:
            with open(log_path, "r", encoding="utf-8", errors="ignore") as f:
                # Read first timestamp from the beginning (first 200 lines)
                for i, line in enumerate(f):
                    if i > 200:
                        break
                    m = TIME_PATTERN.search(line)
                    if m:
                        first_ts = datetime.strptime(m.group(1), "%m/%d/%Y-%H:%M:%S.%f")
                        break

                # Read last timestamp by scanning from the end
                # (seek backwards for large files, or just read through)
                f.seek(0, 2)  # go to end
                file_size = f.tell()
                # Read last 64KB to find the last timestamp
                read_size = min(file_size, 65536)
                f.seek(file_size - read_size)
                tail_chunk = f.read()
                matches = TIME_PATTERN.findall(tail_chunk)
                if matches:
                    last_ts = datetime.strptime(matches[-1], "%m/%d/%Y-%H:%M:%S.%f")
        except Exception as e:
            print(f"[find_best_log] Error reading {log_path}: {e}")
            continue

        candidates.append({
            "etl_path": etl_path,
            "log_path": log_path,
            "first_ts": first_ts,
            "last_ts": last_ts,
        })

    if not candidates:
        return jsonify({"best_path": etl_paths[0] if etl_paths else None,
                        "reason": "No readable log files found; defaulting to first."})

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
            })

    # --- Fallback: pick the log with the latest last_ts ---
    candidates_with_ts = [c for c in candidates if c["last_ts"]]
    if candidates_with_ts:
        latest = max(candidates_with_ts, key=lambda c: c["last_ts"])
        return jsonify({
            "best_path": latest["etl_path"],
            "reason": f"No issue time provided; picked latest log "
                      f"(range: {latest['first_ts']} ~ {latest['last_ts']})",
        })

    # --- Ultimate fallback ---
    return jsonify({
        "best_path": candidates[0]["etl_path"],
        "reason": "Could not determine timestamps; defaulting to first.",
    })


def _parse_issue_time(time_str: str):
    """
    Try to parse an issue time string in various common formats.
    Returns a datetime object or None.
    """
    if not time_str:
        return None

    # Try common patterns
    patterns = [
        (r'(\d{2}/\d{2}/\d{4}-\d{2}:\d{2}:\d{2})', "%m/%d/%Y-%H:%M:%S"),
        (r'(\d{2}/\d{2}/\d{4}\s+\d{2}:\d{2}:\d{2})', "%m/%d/%Y %H:%M:%S"),
        (r'(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2})', "%Y-%m-%d %H:%M:%S"),
        (r'(\d{4}/\d{2}/\d{2}-\d{2}:\d{2}:\d{2})', "%Y/%m/%d-%H:%M:%S"),
    ]
    for regex, fmt in patterns:
        m = re.search(regex, time_str)
        if m:
            try:
                return datetime.strptime(m.group(1), fmt)
            except ValueError:
                continue
    return None

def back_to_avatar():
    return _leave_chatbot(_chatbot_instances)


# The module above is now a domain adapter: its functions retain BT/Wi-Fi/NW
# policy, while the factory owns the public route table and shared use cases.
_NW_ANALYSIS_CAPABILITIES = {
    key for key, enabled in WIFI_UI["features"].items() if enabled
}
_NW_ANALYSIS_HANDLERS = handler_map(globals(), _NW_ANALYSIS_CAPABILITIES)
nw_analysis_bp = create_chatbot_blueprint(ChatbotBlueprintConfig(
    name="nw_analysis",
    import_name=__name__,
    url_prefix="/nw_analysis",
    capabilities=_NW_ANALYSIS_CAPABILITIES,
    get_agent=_get_or_create_agent,
    handlers=_NW_ANALYSIS_HANDLERS,
    # Gather records are keyed by nw_conversation_id, so a reset must start a
    # new one instead of appending this turn to the previous conversation.
    on_reset=lambda: _ensure_nw_conversation_id(rotate=True),
))
