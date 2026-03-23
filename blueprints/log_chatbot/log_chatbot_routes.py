from flask import Blueprint, render_template, request, session, jsonify, Response
import json
import traceback
import uuid
import os
import threading
import tkinter as tk
from tkinter import filedialog

from configs.global_configs import app_config
from models.models import CaseContext
from services.log_chatbot_service import WifiLogAgentSystem, load_skills_from_data_dir, get_builtin_skills, build_skill_file_map

log_chatbot_bp = Blueprint("log_chatbot", __name__, url_prefix="/log_chatbot")

# Server-side store: session_id -> WifiLogAgentSystem instance
_chatbot_instances: dict = {}


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

    return {
        "case_nbr":    ctx.case_nbr or "",
        "subject":     ctx.subject or "",
        "description": "\n".join(description_parts),
        "issue_type":  issue_type,
        "ai_summary": ai_analysis.get("Summary", "") if isinstance(ai_analysis, dict) else "",
    }


def _get_or_create_agent() -> WifiLogAgentSystem:
    """
    Return a per-session WifiLogAgentSystem.
    Borrows client/model from app_config.log_chatbot_agent which is
    initialised at app startup (set_up_app.py -> set_up()).
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
    return render_template("log_chatbot.html", suggested_log=suggested_log)


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
        agent = _get_or_create_agent()
        agent.current_log_path = log_path
        agent.reset_conversation()          # fresh conversation for a new file
        ctx = _extract_issue_context()      # re-extract context in case session was updated after agent creation
        if any(ctx.values()):
            agent.prime_with_context(**ctx)

        session["chatbot_log_path"] = log_path
        return jsonify({
            "success": True,
            "message": f"Log file set: {log_path}",
            "skills": agent.get_skill_descriptions(),
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

    try:
        agent = _get_or_create_agent()
        if not agent.current_log_path:
            return jsonify({
                "success": False,
                "error": "No log file loaded. Please set a log file first."
            }), 400

        result = agent.chat(user_message)
        return Response(
            json.dumps({"success": True, "result": result}, ensure_ascii=False, indent=2),
            mimetype="application/json",
        )
    except Exception as e:
        error_traceback = traceback.format_exc()
        print(f"❌ Chatbot error:\n{error_traceback}")
        return jsonify({"success": False, "error": str(e)}), 500


# ------------------------------------------------------------------
# API: analyze all skills
# ------------------------------------------------------------------
@log_chatbot_bp.route("/analyze_all", methods=["POST"])
def analyze_all():
    data = request.get_json(silent=True) or {}
    # Use caller-supplied description, fall back to session ai_ips_analysis summary
    issue_description = data.get("issue_description", "").strip()
    if not issue_description:
        try:
            ctx = _extract_issue_context()
            issue_description = ctx["description"] or "Perform full multi-skill log analysis"
        except Exception:
            issue_description = "Perform full multi-skill log analysis"

    try:
        agent = _get_or_create_agent()
        if not agent.current_log_path:
            return jsonify({
                "success": False,
                "error": "No log file loaded. Please set a log file first."
            }), 400

        result = agent.analyze_all(issue_description)
        return Response(
            json.dumps({"success": True, "result": result}, ensure_ascii=False, indent=2),
            mimetype="application/json",
        )
    except Exception as e:
        error_traceback = traceback.format_exc()
        print(f"❌ Analyze all error:\n{error_traceback}")
        return jsonify({"success": False, "error": str(e)}), 500


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
