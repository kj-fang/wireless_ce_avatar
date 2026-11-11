from flask import render_template, request, session, jsonify, Response, copy_current_request_context
from services.chatbot.issue_context import extract_disconnect_time as _extract_disconnect_time
from services.chatbot.shared_routes import SharedRouteContext, build_shared_handlers
from services.chatbot.session import register_session_agent_store
from services.skill_editor.controller import (
    SkillAppendContext,
    build_profile_yaml_helpers,
    build_skill_append_handlers,
)
from services.skill_editor.yaml_service import (
    read_yaml_file as _read_yaml_file,
    write_yaml_file as _write_yaml_file,
)
from utils.skills_yaml_utils import (
    find_latest_cloud_baseline_yaml as _latest_cloud_baseline,
    find_latest_user_yaml as _latest_user_yaml,
    local_user_overrides_dir as _user_local_dir,
    set_active_source as _set_active_source,
    today_dated_filename as _today_yaml_filename,
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

from configs.chatbot_ui import NW_UI
from configs.global_configs import app_config
from models.models import CaseContext
from services.chatbot.engine.network_experience import NwAnalysisAgentSystem
from services.chatbot.engine.system import load_skills_from_yaml
from services.sleepstudy_analyzer import analyze_sleepstudy_stream
from services import sleepstudy_pipeline
from services import gather_service
from services import check_ips_service
from services import history_service
from utils.etl_utils import extract_time_from_description


# Server-side store: session_id -> NwAnalysisAgentSystem instance
_chatbot_instances: dict = {}
register_session_agent_store("nw_analysis", _chatbot_instances)

# Gather analytics domain for this blueprint. Kept as a constant so the NW
# records stay distinguishable from the wifi ones even though both are driven
# by a class named WifiLogAgentSystem.
GATHER_DOMAIN = "nw"
# History store (configs/chat_domains.py): NW's own, not Wi-Fi's.
HISTORY_DOMAIN = "nw"


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
    issue_desc = ""
    try:
        ctx = _extract_issue_context()
        issue_desc = ctx.get("description", "")
    except Exception:
        pass
    return render_template(
        "chatbot/page.html",
        ui=NW_UI,
        issue_description=issue_desc,
    )


# ------------------------------------------------------------------
# API: load a Sleep Study report
#
# NW's /set_log takes the Sleep Study HTML, not a Wi-Fi log: the report is
# what the page analyses, and the driver log is passed per analysis. A new
# report is a new conversation, so it gets its own History entry.
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
        _ensure_nw_conversation_id(rotate=True)

        return jsonify({
            "success": True,
            "message": f"Sleepstudy file set: {log_path}",
            "skills": agent.get_skill_descriptions(),
            **check_ips_service.prompt_state(log_path),
        })
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


# ------------------------------------------------------------------
# History: one conversation per loaded report
#
# The analysis is the conversation's first turn and follow-up questions are
# the next ones, all under nw_conversation_id. The report path and the driver
# log chosen for each time point are kept on the snapshot.
# ------------------------------------------------------------------
def _sleepstudy_request_text(sleep_path: str, driver_logs: dict | None = None) -> str:
    """The user turn a Sleep Study analysis is saved as. nw.js shows the same."""
    title = "Sleep Study: " + os.path.basename(sleep_path)
    if driver_logs:
        title += f" — {len(driver_logs)} time point(s)"
    lines = [title, f"Report: `{sleep_path}`"]
    for index, path in sorted((driver_logs or {}).items(), key=lambda kv: int(kv[0])):
        lines.append(f"#{index} → `{path}`" if path else f"#{index} → (no driver log)")
    return "\n".join(lines)


def _record_nw_turn(user_message: str, result: dict, steps: list,
                    sleep_path: str = "", driver_logs: dict | None = None) -> str:
    """Save one turn to NW's History and return its conversation id. Never raises."""
    conversation_id = _ensure_nw_conversation_id()
    try:
        history_service.record_turn(
            conversation_id=conversation_id,
            session_id=session.get("chatbot_session_id", "") or "",
            turn_id=str(uuid.uuid4()),
            user_message=user_message,
            agent_result=result,
            issue=_extract_issue_context(),
            log_path=sleep_path,
            domain=HISTORY_DOMAIN,
            steps=steps,
            extra=None if driver_logs is None else {"driver_logs": driver_logs},
        )
    except Exception as exc:
        print(f"[nw history] record failed: {exc}")
    return conversation_id


def _remember_analysis(user_message: str, report: str) -> None:
    """Put an analysis into the session agent's conversation, so a follow-up
    question is answered by something that has read the report."""
    try:
        agent = _get_or_create_agent()
        agent.conversation_history.append({"role": "user", "content": user_message})
        agent.conversation_history.append({"role": "assistant", "content": report})
    except Exception:
        pass


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

    # 428 Precondition Required. set_log_sleepstudy only advertises needs_ips,
    # and the page starts this analysis straight afterwards, so without a gate
    # here a case-less sleepstudy runs anyway -- and so does any direct caller
    # or a client whose prompt never loaded. Checked before the SSE stream
    # opens, because a 428 inside an event stream is not a status the client
    # can act on.
    blocked = check_ips_service.blocking_state(sleep_path)
    if blocked:
        return jsonify(blocked), 428

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
        steps = []
        try:
            for event in analyze_sleepstudy_stream(sleep_path, llm_call=llm_call):
                kind = event["type"]
                if kind == "step":
                    sse = {"type": "step", "step": {"content": event["content"]}}
                    steps.append(sse["step"])
                elif kind == "done":
                    request_text = _sleepstudy_request_text(sleep_path)
                    result = {"type": "text", "data": event["data"]}
                    _remember_analysis(request_text, event["data"])
                    result["conversation_id"] = _record_nw_turn(
                        request_text, dict(result), steps, sleep_path)
                    sse = {"type": "done", "result": result}
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
# API: Sleep Study time points + suggested driver logs (no LLM)
#
# Step one of two: the page shows these so the user can confirm or change
# the driver log of each time point before /analyze_sleepstudy_driver runs.
# ------------------------------------------------------------------
def sleepstudy_plan():
    """Request JSON: { "log_path": "<sleepstudy-report.html>" }"""
    data = request.get_json(silent=True) or {}
    sleep_path = (data.get("log_path") or "").strip()
    if not sleep_path or not os.path.exists(sleep_path):
        return jsonify({"success": False, "error": f"File not found: {sleep_path}"}), 400
    blocked = check_ips_service.blocking_state(sleep_path)
    if blocked:
        return jsonify(blocked), 428
    try:
        return jsonify({"success": True, **sleepstudy_pipeline.plan(sleep_path)})
    except Exception as exc:
        traceback.print_exc()
        return jsonify({"success": False, "error": f"{type(exc).__name__}: {exc}"}), 500


# ------------------------------------------------------------------
# API: sleepstudy + driver logs (SSE stream)
#
# The HTTP adapter for services/sleepstudy_pipeline.py, which picks the worst
# sessions, cuts each one's driver log around it and runs the Wi-Fi or BT
# agent on it. Everything that is not HTTP lives there, so another entry
# point (an MCP server) can drive the same pipeline.
# ------------------------------------------------------------------
def analyze_sleepstudy_driver():
    """
    Request JSON: {
      "log_path":    "<sleepstudy-report.html>",
      "driver_logs": {"<point index>": "<WRT folder, a WiFiLog file, a decoded
                      Wi-Fi log, or '' for none>", ...}
    }
    """
    data = request.get_json(silent=True) or {}
    sleep_path = (data.get("log_path") or "").strip()
    raw_logs = data.get("driver_logs") if isinstance(data.get("driver_logs"), dict) else {}
    driver_logs = {str(k): str(v or "").strip() for k, v in raw_logs.items()}

    def _err(message):
        yield f"data: {json.dumps({'type': 'error', 'content': message}, ensure_ascii=False)}\n\n"

    if not sleep_path:
        return Response(_err("log_path is required"), mimetype="text/event-stream")

    # Same gate as analyze_sleepstudy, for the same reason: checked before
    # the stream opens, where a 428 is still a status the client can act on.
    blocked = check_ips_service.blocking_state(sleep_path)
    if blocked:
        return jsonify(blocked), 428

    for path in [sleep_path, *driver_logs.values()]:
        if path and not os.path.exists(path):
            return Response(_err(f"File not found: {path}"), mimetype="text/event-stream")

    out_dir = os.path.join(
        app_config.avatarfiles_dir or os.path.expanduser("~"),
        "sleepstudy", datetime.now().strftime("%Y%m%d_%H%M%S"),
    )

    @copy_current_request_context
    def event_stream():
        spend = {"llm_calls": 0, "input_tokens": 0, "cache_read_tokens": 0,
                 "cache_write_tokens": 0, "output_tokens": 0, "total_tokens": 0}
        llm_model = getattr(app_config.llm_helper, "model", "") if app_config.llm_helper else ""
        started = datetime.now()
        status, error_code = "success", ""
        steps = []
        try:
            for event in sleepstudy_pipeline.run_pipeline(sleep_path, driver_logs, out_dir=out_dir):
                kind = event["type"]
                if kind == "step":
                    sse = {"type": "step", "step": {"content": event["content"]}}
                    steps.append(sse["step"])
                elif kind == "point":
                    for key, value in (event["data"].get("agent_usage") or {}).items():
                        if key in spend:
                            spend[key] += value
                    continue
                elif kind == "done":
                    request_text = _sleepstudy_request_text(sleep_path, driver_logs)
                    report = event["data"]["markdown"]
                    _remember_analysis(request_text, report)
                    conversation_id = _record_nw_turn(
                        request_text, {"type": "text", "data": report},
                        steps, sleep_path, driver_logs)
                    sse = {"type": "done", "result": {
                        "type": "text",
                        "data": report,
                        "pipeline": event["data"],
                        "conversation_id": conversation_id,
                    }}
                elif kind == "error":
                    sse = {"type": "error", "content": event["content"]}
                    status, error_code = "failed", "pipeline_error"
                else:
                    continue
                yield f"data: {json.dumps(sse, ensure_ascii=False, default=str)}\n\n"
        except Exception as exc:
            print(f"analyze_sleepstudy_driver error:\n{traceback.format_exc()}")
            status, error_code = "failed", type(exc).__name__
            yield f"data: {json.dumps({'type': 'error', 'content': str(exc)}, ensure_ascii=False)}\n\n"
        finally:
            # Book the spend regardless of outcome — the tokens were billed.
            try:
                gather_service.record_feature_usage(
                    workflow_id=session.get("gather_workflow_id", ""),
                    feature_code="sleepstudy_driver_analysis",
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
#
# The HTTP adapter, deliberately NOT shared: request parsing, issue-time
# policy, background-job bookkeeping and Gather accounting all differ per
# profile, so Wi-Fi and BT keep their own copies of this. NW's is the
# shortest of the three — it publishes steps through a request-local
# queue.Queue rather than registering with chat_jobs, saves each turn to
# NW's History and has no feedback sidecar.
#
# What IS shared is what the worker below eventually calls — agent.chat(),
# i.e. ConversationMixin.chat in services/chatbot/engine/conversation.py,
# one implementation inherited by all three agents. Same name, two layers:
# seeing agent.chat() in here does not mean this function is shared.
# ------------------------------------------------------------------
def chat():
    data = request.get_json(silent=True) or {}
    user_message = (data.get("message") or "").strip()
    if not user_message:
        return jsonify({"success": False, "error": "message is required"}), 400

    # Gate against this profile's actual log, not a stale shared session path.
    sid = session.get("chatbot_session_id", "")
    nw_agent = _chatbot_instances.get(sid) if sid else None
    nw_log = str(getattr(nw_agent, "current_log_path", "") or "")
    blocked = check_ips_service.blocking_state(nw_log or None)
    if blocked:
        return jsonify(blocked), 428

    try:
        temperature = float(data.get("temperature", 0.2))
    except Exception:
        temperature = 0.2
    temperature = max(0.0, min(1.0, temperature))

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
                    max_steps=max_steps,
                    temperature=temperature,
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

        # Read now: the generator below runs after the request context is gone.
        history_session_id = session.get("chatbot_session_id", "") or ""
        history_log_path = getattr(agent, "current_log_path", "") or ""

        def event_stream():
            steps = []
            while True:
                try:
                    msg_type, payload = step_queue.get(timeout=120)
                except _queue.Empty:
                    yield f"data: {json.dumps({'type': 'error', 'content': 'Chat timed out.'})}\n\n"
                    break
                if msg_type == "step":
                    steps.append(payload)
                    yield f"data: {json.dumps({'type': 'step', 'step': payload}, ensure_ascii=False)}\n\n"
                elif msg_type == "done":
                    history_service.record_turn(
                        conversation_id=conversation_id,
                        session_id=history_session_id,
                        turn_id=turn_id,
                        user_message=user_message,
                        agent_result=payload,
                        issue=_issue_ctx_for_snapshot,
                        log_path=history_log_path,
                        domain=HISTORY_DOMAIN,
                        steps=steps,
                    )
                    if isinstance(payload, dict):
                        payload = {**payload, "conversation_id": conversation_id}
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

        # Python unbinds `e` when this except block ends, and the generator
        # below only runs once Flask starts streaming -- so read the message now.
        error_text = str(e)

        def generate_error():
            yield f"data: {json.dumps({'type': 'error', 'content': error_text}, ensure_ascii=False)}\n\n"

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
# API: the case's issue context
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
# API: reopen a saved conversation
#
# The list / rename / pin / delete endpoints are the shared ones; loading is
# NW's own because it restores a report and a driver log, not a Wi-Fi log and
# an issue time. history.js reads the common fields and nw.js the two paths.
# ------------------------------------------------------------------
def history_load():
    data = request.get_json(silent=True) or {}
    conversation_id = (data.get("conversation_id") or "").strip()
    if not conversation_id:
        return jsonify({"success": False, "error": "conversation_id is required"}), 400
    conv = history_service.get_conversation(
        conversation_id, domain=HISTORY_DOMAIN, with_steps=True)
    if conv is None:
        return jsonify({"success": False, "error": "Conversation not found"}), 404

    try:
        session["nw_conversation_id"] = conversation_id
        turns = conv.get("turns") or []
        sleep_path = (conv.get("log_path") or "").strip()
        log_exists = bool(sleep_path) and os.path.exists(sleep_path)

        agent = _get_or_create_agent()
        agent.reset_conversation()
        skills = []
        if log_exists:
            agent.current_log_path = sleep_path
            issue = conv.get("issue") if isinstance(conv.get("issue"), dict) else {}
            allowed = {"case_nbr", "subject", "description", "issue_type", "attachment_time"}
            try:
                agent.prime_with_context(**{k: v for k, v in issue.items()
                                            if k in allowed and isinstance(v, str)})
            except Exception as exc:
                print(f"[nw history] prime_with_context skipped: {exc}")
            skills = agent.get_skill_descriptions()
        # Follow-ups continue from what was said, the analysis report included.
        for turn in turns:
            asked = (turn.get("user_message") or "").strip()
            if asked:
                agent.conversation_history.append({"role": "user", "content": asked})
            answer = history_service.assistant_text_from_result(turn.get("result"))
            if answer:
                agent.conversation_history.append({"role": "assistant", "content": answer})

        return jsonify({
            "success": True,
            "conversation_id": conversation_id,
            "title": conv.get("title") or "Conversation",
            "turns": turns,
            "running": False,
            "log_path": sleep_path,
            "log_exists": log_exists,
            "driver_logs": conv.get("driver_logs") or {},
            "skills": skills,
        })
    except Exception as e:
        traceback.print_exc()
        return jsonify({"success": False, "error": str(e)}), 500


# The module above is now a domain adapter: its functions retain BT/Wi-Fi/NW
# policy, while the factory owns the public route table and shared use cases.
_NW_ANALYSIS_CAPABILITIES = {
    key for key, enabled in NW_UI["features"].items() if enabled
}

# NW gains the YAML import button without the rest of the skill editor -- the
# same shape main shipped. The four helpers and the six-field context are the
# shared ones; only the dated filename prefix and the app-level agent slot are
# NW's own.
_YAML_HELPERS = build_profile_yaml_helpers(
    user_local_dir=_user_local_dir,
    today_yaml_filename=_today_yaml_filename,
    user_yaml_prefix="skills_",
    latest_cloud_baseline=_latest_cloud_baseline,
    latest_user_yaml=_latest_user_yaml,
    write_yaml_file=_write_yaml_file,
    load_skills_from_yaml=load_skills_from_yaml,
    get_agent=_get_or_create_agent,
    agent_config_attr="nw_analysis_agent",
)
_SKILL_APPEND_HANDLERS = build_skill_append_handlers(SkillAppendContext(
    activate_yaml=_YAML_HELPERS["activate_yaml"],
    latest_cloud_baseline=_latest_cloud_baseline,
    latest_user_yaml=_latest_user_yaml,
    persist_user_yaml_snapshot=_YAML_HELPERS["persist_user_yaml_snapshot"],
    read_yaml_file=_read_yaml_file,
    set_active_source=_set_active_source,
))

# Browse, skill reload, Back to Avatar and the History list/rename/pin/delete
# endpoints are the shared ones. NW's own chat_stop and history_load (above)
# win over the shared versions.
_SHARED_HANDLERS = build_shared_handlers(SharedRouteContext(
    domain=HISTORY_DOMAIN,
    agent_config_attr="nw_analysis_agent",
    get_agent=_get_or_create_agent,
    session_agents=_chatbot_instances,
    browse_filetypes=(
        ("Sleep Study report / driver log", "*.html *.htm *.log *.LOG"),
        ("All files", "*.*"),
    ),
    load_skills_from_yaml=load_skills_from_yaml,
    gather_domain=GATHER_DOMAIN,
))

_NW_ANALYSIS_HANDLERS = handler_map(
    {**_SHARED_HANDLERS, **globals(), **_SKILL_APPEND_HANDLERS},
    _NW_ANALYSIS_CAPABILITIES)
nw_analysis_bp = create_chatbot_blueprint(ChatbotBlueprintConfig(
    name="nw_analysis",
    import_name=__name__,
    url_prefix="/nw_analysis",
    capabilities=_NW_ANALYSIS_CAPABILITIES,
    get_agent=_get_or_create_agent,
    handlers=_NW_ANALYSIS_HANDLERS,
    # Gather records are keyed by nw_conversation_id, so a reset must start a
    # new one instead of appending this turn to the previous conversation.
    on_reset=lambda: (
        _ensure_nw_conversation_id(rotate=True),
        check_ips_service.start_new_session(),
    ),
))
