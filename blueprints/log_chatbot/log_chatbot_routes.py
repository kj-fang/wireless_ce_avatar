from flask import Blueprint, render_template, request, session, jsonify, Response
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

    # Build Issue_summary + Symptom string for chatbot auto-fill
    issue_summary_obj = ai_analysis.get("Issue_summary", {})
    if isinstance(issue_summary_obj, dict):
        symptom = issue_summary_obj.get("Symptom") or issue_summary_obj.get("Symptoms", "")
        if isinstance(symptom, list):
            symptom = "; ".join(str(s) for s in symptom if s)
    else:
        symptom = str(issue_summary_obj) if issue_summary_obj else ""

    return {
        "case_nbr":    ctx.case_nbr or "",
        "subject":     ctx.subject or "",
        "description": "\n".join(description_parts),
        "issue_type":  issue_type,
        "ai_summary":  symptom,
    }


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


def _extract_symptom_keywords(ai_summary: str) -> str:
    """
    Extract concise technical keywords from the AI symptom summary.
    Returns a short keyword hint string like:
      ' [Keywords: ANT Tool, MCC, beacon loss, weak signal]'
    These keywords guide the agent's background investigation pass.
    """
    if not ai_summary:
        return ""
    # Common noise words to filter out
    NOISE = {
        'the', 'a', 'an', 'is', 'was', 'were', 'are', 'been', 'be',
        'to', 'of', 'in', 'on', 'at', 'for', 'and', 'or', 'but',
        'with', 'from', 'by', 'not', 'no', 'this', 'that', 'it',
        'its', 'has', 'had', 'have', 'will', 'would', 'could',
        'should', 'may', 'might', 'can', 'do', 'does', 'did',
        'after', 'before', 'when', 'while', 'if', 'then',
        'also', 'which', 'than', 'other', 'such', 'some',
        'issue', 'error', 'problem', 'failure', 'log', 'user',
        'reported', 'observed', 'occurred', 'during', 'about',
    }
    # Extract multi-word technical terms first (e.g. "ANT Tool", "Weak Signal")
    tech_terms = re.findall(
        r'\b(?:ANT Tool|MCC|DFS|SAR|RSSI|SNR|beacon loss|missed beacon|'
        r'weak signal|low signal|channel switch|roaming|deauth|'
        r'power save|WoWLAN|D0i3|NLO scan|firmware crash|'
        r'yellow bang|blue screen|BSOD|hang|freeze)\b',
        ai_summary, re.IGNORECASE
    )
    # Also extract capitalized words (likely technical terms)
    caps = re.findall(r'\b[A-Z][A-Za-z0-9_]{2,}\b', ai_summary)
    # Combine, deduplicate, limit
    all_kw = []
    seen = set()
    for kw in tech_terms + caps:
        kw_lower = kw.lower()
        if kw_lower not in seen and kw_lower not in NOISE:
            seen.add(kw_lower)
            all_kw.append(kw)
    if not all_kw:
        return ""
    return f" [Keywords: {', '.join(all_kw[:8])}]"


def _compose_concise_description() -> str:
    """
    Auto-compose the most effective issue description for analyze_all.

    Engineer's ideal input: "What happened + When + Technical Keywords"
    e.g. "6G Weak Signal browser web page disconnected at around 10/28/2025-11:25:49
          [Keywords: ANT Tool, MCC, beacon loss]"

    Sources:
      - subject: concise problem statement (strip ALL bracket tags + F/R suffix)
      - description / ai_summary / subject: extract precise timestamp
      - ai_summary: symptom keywords extracted for background investigation
    """
    try:
        ctx = _extract_issue_context()
    except Exception:
        return "Perform full multi-skill log analysis"

    subject = ctx.get("subject", "")
    desc_raw = ctx.get("description", "")
    ai_summary = ctx.get("ai_summary", "")

    # Extract timestamp from ALL available text sources
    time_hint = _extract_disconnect_time(subject, desc_raw, ai_summary)
    # Extract symptom keywords for background investigation
    keyword_hint = _extract_symptom_keywords(ai_summary)

    # Best: clean subject line — strip ALL leading [tag] groups
    if subject:
        clean = re.sub(r'^(\[.*?\]\s*)+', '', subject).strip()    # remove ALL [xxx] tags
        clean = re.sub(r'\s*\(F/R.*?\)\s*$', '', clean).strip()   # remove (F/R：1/1u,40/200C)
        if clean:
            return f"{clean}{time_hint}{keyword_hint}"

    # Fallback: AI summary first sentence + keywords
    if ai_summary:
        first_sentence = ai_summary.split('.')[0].strip()
        if first_sentence:
            return f"{first_sentence}{time_hint}{keyword_hint}"

    # Last resort: first 200 chars of description
    if desc_raw:
        return desc_raw[:200].strip() + time_hint + keyword_hint

    return "Perform full multi-skill log analysis"


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
    issue_desc = ""
    ai_summary = ""
    try:
        ctx = _extract_issue_context()
        issue_desc = ctx.get("description", "")
        # Auto-compose concise description for the hidden input
        ai_summary = _compose_concise_description()
    except Exception:
        pass
    return render_template("log_chatbot.html", suggested_log=suggested_log, issue_description=issue_desc, ai_summary=ai_summary)


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
    issue_description = data.get("issue_description", "").strip()
    if not issue_description:
        issue_description = _compose_concise_description()
    
    # Extract COMPLETE context for LLM background
    try:
        full_context = _extract_issue_context()
    except Exception:
        full_context = {}

    try:
        agent = _get_or_create_agent()
        if not agent.current_log_path:
            return jsonify({
                "success": False,
                "error": "No log file loaded. Please set a log file first."
            }), 400

        result = agent.analyze_all(issue_description, issue_context=full_context)
        return Response(
            json.dumps({"success": True, "result": result}, ensure_ascii=False, indent=2),
            mimetype="application/json",
        )
    except Exception as e:
        error_traceback = traceback.format_exc()
        print(f"❌ Analyze all error:\n{error_traceback}")
        return jsonify({"success": False, "error": str(e)}), 500


# ------------------------------------------------------------------
# API: analyze all skills (SSE streaming — real-time step output)
# ------------------------------------------------------------------
@log_chatbot_bp.route("/analyze_all_stream", methods=["POST"])
def analyze_all_stream():
    """
    Same as /analyze_all but streams each reasoning step via Server-Sent Events.
    Frontend uses EventSource to render steps in real-time as they happen,
    then receives the final report at the end.
    """
    data = request.get_json(silent=True) or {}
    issue_description = data.get("issue_description", "").strip()
    if not issue_description:
        issue_description = _compose_concise_description()

    try:
        agent = _get_or_create_agent()
        if not agent.current_log_path:
            def err_gen():
                yield f"data: {json.dumps({'type': 'error', 'content': 'No log file loaded.'})}\n\n"
            return Response(err_gen(), mimetype='text/event-stream')
    except Exception as e:
        def err_gen():
            yield f"data: {json.dumps({'type': 'error', 'content': str(e)})}\n\n"
        return Response(err_gen(), mimetype='text/event-stream')

    # Use a thread + queue to bridge the synchronous analyze_all with SSE
    import queue
    step_queue = queue.Queue()

    def step_callback(step):
        """Called by analyze_all._emit() for each new step."""
        step_queue.put(("step", step))

    def run_analysis():
        try:
            result = agent.analyze_all(issue_description, step_callback=step_callback)
            step_queue.put(("done", result))
        except Exception as e:
            step_queue.put(("error", str(e)))

    # Start analysis in background thread
    thread = threading.Thread(target=run_analysis, daemon=True)
    thread.start()

    def event_stream():
        while True:
            try:
                msg_type, payload = step_queue.get(timeout=120)
            except queue.Empty:
                yield f"data: {json.dumps({'type': 'error', 'content': 'Analysis timed out.'})}\n\n"
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
        mimetype='text/event-stream',
        headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'}
    )


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

# ------------------------------------------------------------------
# API: Skills Agent Analyze (Time-Agentic Loop)
# ------------------------------------------------------------------
@log_chatbot_bp.route("/agent_analyze", methods=["POST"])
def agent_analyze():
    """Trigger the multi-step agentic reasoning process (full analysis with LLM)."""
    try:
        # Retrieve the existing Chatbot Agent
        agent = _get_or_create_agent()
        if not agent.current_log_path:
            return jsonify({"success": False, "error": "No log file loaded."}), 400

        # Retrieve the previously stored Issue Description from the Session
        ctx = _extract_issue_context()
        issue_description = ctx.get("description", "")
        if not issue_description:
            issue_description = "Perform full log analysis"

        # Execute the WifiLogAgentSystem's analyze_all (agentic loop with LLM)
        result = agent.analyze_all(issue_description)

        return Response(
            json.dumps(result, ensure_ascii=False, indent=2),
            mimetype="application/json",
        )
    except Exception as e:
        import traceback
        error_traceback = traceback.format_exc()
        print(f"❌ Agent analysis error:\n{error_traceback}")
        return jsonify({"success": False, "error": str(e)}), 500

@log_chatbot_bp.route("/get_issue_context", methods=["GET"])
def get_issue_context():
    """Provide the original Issue Description for frontend script to compare log file timestamps."""
    ctx = _extract_issue_context()
    return jsonify({"description": ctx.get("description", "No description found.")})


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