"""
Beacon assistant routes - the chat page and its API.

    GET  /assistant/                 the page
    POST /assistant/chat             one turn, streamed as Server-Sent Events
    POST /assistant/chat/stop        stop the running turn
    POST /assistant/reset            start a new conversation
    GET  /assistant/state            who the user is, what is running
    GET  /assistant/connectors       what the assistant can reach
    /assistant/history/...           saved conversations (list, get, stream,
                                     load, rename, pin, delete)

THIS MACHINE ONLY. The assistant reads the user's mailbox and their IPS
cases, and the app listens on every network interface. So every route here,
reads included, answers only a request that comes from this PC, to a loopback
host name, from this app's own pages (see _this_machine_only).

Nothing here shares state with the log chatbots: the page has its own session
keys, its own agents and its own history folder. Opening it must not disturb
a Wi-Fi analysis running in another tab of the same browser.
"""

from __future__ import annotations

import re
import threading
import time
import traceback
import uuid
from collections import OrderedDict
from urllib.parse import urlsplit

from flask import Blueprint, Response, jsonify, render_template, request, session

from services import gather_service, history_service
from services.assistant import DOMAIN, runner
from services.assistant.agent import AssistantAgent
from services.chatbot import job_runtime as chat_jobs
from services.chatbot.job_runtime import job_sse
from services.chatbot.shared_routes import SharedRouteContext, build_shared_handlers

assistant_bp = Blueprint("assistant", __name__, url_prefix="/assistant")

SESSION_CONVERSATION = "assistant_conversation_id"
MAX_MESSAGE_CHARS = 8000
MAX_AGENTS = 24                 # conversations kept warm in memory
_LOOPBACK_ADDRS = ("127.0.0.1", "::1")
_LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "::1")
_ID_RE = re.compile(r"^[A-Za-z0-9-]{8,64}$")
_SSE_HEADERS = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}


# ---------- guard ----------

def _cross_origin(req) -> bool:
    """A browser sent this from another site's page (same rule as Beacon's
    routes): its Origin names a host other than the one the request went to."""
    origin = (req.headers.get("Origin") or "").strip()
    if not origin or origin == "null":
        return origin == "null"
    return urlsplit(origin).netloc.casefold() != (req.host or "").casefold()


@assistant_bp.before_request
def _this_machine_only():
    """Refuse everything that is not this PC's own browser on this app's own
    page. Three separate holes, three checks:

    * another machine on the network           -> the peer address
    * a web page of another site, open in this
      PC's browser, calling these routes       -> Origin / Sec-Fetch-Site
    * such a page that re-points its own host
      name at 127.0.0.1 (DNS rebinding), which
      makes its requests same-origin           -> the Host header

    Following a link to the page itself is allowed from anywhere; the page
    carries no data, its API calls do."""
    if request.remote_addr not in _LOOPBACK_ADDRS:
        return jsonify({"success": False, "error": "the assistant answers this PC only"}), 403
    host = (urlsplit("//" + (request.host or "")).hostname or "").casefold()
    if host not in _LOOPBACK_HOSTS:
        return jsonify({"success": False, "error": "unexpected host name"}), 403
    if _cross_origin(request):
        return jsonify({"success": False, "error": "cross-origin request refused"}), 403
    site = (request.headers.get("Sec-Fetch-Site") or "").strip().lower()
    if site in ("cross-site", "same-site"):
        navigating = (request.headers.get("Sec-Fetch-Mode") or "").strip().lower() == "navigate"
        if not (navigating and request.method == "GET" and request.endpoint == "assistant.index"):
            return jsonify({"success": False, "error": "cross-site request refused"}), 403
    return None


# ---------- agents: one per conversation ----------

_agents: "OrderedDict[str, AssistantAgent]" = OrderedDict()
_agents_lock = threading.Lock()


def _clean_id(value) -> str:
    value = str(value or "").strip()
    return value if _ID_RE.match(value) else ""


def _running(conversation_id: str) -> bool:
    job = chat_jobs.get_job(conversation_id) if conversation_id else None
    return bool(job is not None and job.domain == DOMAIN and job.status == "running")


def _agent_for(conversation_id: str) -> AssistantAgent:
    """The conversation's agent, rebuilt from the saved conversation when the
    app restarted (or the agent fell out of memory) since its last turn."""
    with _agents_lock:
        agent = _agents.get(conversation_id)
        if agent is not None:
            _agents.move_to_end(conversation_id)
            return agent
    agent = runner.new_agent()
    _restore(agent, conversation_id)
    with _agents_lock:
        _agents[conversation_id] = agent
        # Oldest first; a conversation that is answering right now is kept.
        for old in [k for k in _agents if k != conversation_id]:
            if len(_agents) <= MAX_AGENTS:
                break
            if not _running(old):
                _agents.pop(old, None)
    return agent


def _restore(agent: AssistantAgent, conversation_id: str) -> None:
    """Give a fresh agent the conversation's past: the stored model-facing
    context when there is one, else the question/answer text of each turn."""
    try:
        context = history_service.get_context(conversation_id, domain=DOMAIN)
        if context:
            agent.restore_conversation_context(context)
            if agent.conversation_history:
                return
        conv = history_service.get_conversation(conversation_id, domain=DOMAIN)
        pairs = []
        for turn in (conv or {}).get("turns") or []:
            asked = str(turn.get("user_message") or "").strip()
            answer = history_service.assistant_text_from_result(turn.get("result"))
            if asked and answer:
                pairs += [{"role": "user", "content": asked},
                          {"role": "assistant", "content": answer}]
        agent.restore_conversation_context(pairs)
    except Exception as e:
        print(f"[assistant] could not restore conversation {conversation_id}: {e}")


def _drop_agent(conversation_id: str) -> None:
    with _agents_lock:
        _agents.pop(conversation_id, None)


# ---------- page + state ----------

@assistant_bp.route("/")
def index():
    runner.start_silent_sign_in()
    return render_template("assistant/page.html", asset_version=_asset_version())


def _asset_version() -> int:
    """Newest change time of the page's own script and stylesheet, so an
    edited file is fetched instead of the browser's cached copy."""
    import os
    from flask import current_app
    newest = 0
    for name in ("assistant.js", "assistant.css"):
        try:
            newest = max(newest, int(os.path.getmtime(
                os.path.join(current_app.static_folder or "", "assistant", name))))
        except OSError:
            pass
    return newest


@assistant_bp.route("/state")
def state():
    conversation_id = _clean_id(session.get(SESSION_CONVERSATION))
    try:
        runner.llm()
        llm_error = ""
    except runner.AssistantUnavailable as e:
        llm_error = str(e)
    return jsonify({"success": True, "identity": runner.identity(),
                    "conversation_id": conversation_id,
                    "running": _running(conversation_id),
                    "llm_error": llm_error})


@assistant_bp.route("/sign_in", methods=["POST"])
def sign_in():
    """Retry the silent IPS sign-in (never opens a browser; the visible
    sign-in lives on the Beacon page)."""
    runner.start_silent_sign_in(retry_failed=True)
    return jsonify({"success": True, "identity": runner.identity()})


@assistant_bp.route("/connectors")
def connectors():
    from services.assistant.connectors import default_registry, load_problems
    registry = default_registry()
    return jsonify({"success": True, "connectors": registry.describe(),
                    "problems": load_problems()})


# ---------- chat ----------

def _error_stream(text: str, status: int = 200) -> Response:
    import json

    def _one():
        yield "data: " + json.dumps({"type": "error", "content": text}, ensure_ascii=False) + "\n\n"
    return Response(_one(), status=status, mimetype="text/event-stream", headers=_SSE_HEADERS)


def _friendly(exc: Exception) -> str:
    text = f"{type(exc).__name__}: {exc}"
    return text if len(text) <= 600 else text[:600] + " ..."


@assistant_bp.route("/chat", methods=["POST"])
def chat():
    data = request.get_json(silent=True) or {}
    message = str(data.get("message") or "").strip()
    if not message:
        return jsonify({"success": False, "error": "message is required"}), 400
    if len(message) > MAX_MESSAGE_CHARS:
        return jsonify({"success": False,
                        "error": f"message is longer than {MAX_MESSAGE_CHARS} characters"}), 400
    try:
        temperature = max(0.0, min(1.0, float(data.get("temperature", 0.2))))
    except (TypeError, ValueError):
        temperature = 0.2
    try:
        max_steps = max(1, min(12, int(data.get("max_steps", 8))))
    except (TypeError, ValueError):
        max_steps = 8

    conversation_id = (_clean_id(data.get("conversation_id"))
                       or _clean_id(session.get(SESSION_CONVERSATION)) or str(uuid.uuid4()))
    if _running(conversation_id):
        return jsonify({"success": False,
                        "error": "this conversation is still answering the previous message"}), 409
    try:
        agent = _agent_for(conversation_id)
    except runner.AssistantUnavailable as e:
        return _error_stream(str(e))
    session[SESSION_CONVERSATION] = conversation_id

    turn_id = str(uuid.uuid4())
    job = chat_jobs.start_job(conversation_id=conversation_id, turn_id=turn_id,
                              title=message[:60], agent=agent, domain=DOMAIN)
    workflow_id = session.get("gather_workflow_id", "")
    try:
        # The first question of a conversation is written to the team's shared
        # analytics folder. An assistant question can quote a mail or a
        # customer, so only a label goes there, never the text.
        gather_service.record_send(conversation_id=conversation_id, workflow_id=workflow_id,
                                   session_id=conversation_id, user_message="[assistant chat]",
                                   domain=DOMAIN, turn_id=turn_id)
    except Exception:
        pass

    def work() -> None:
        started = time.monotonic()
        steps: list = []
        status, error_code = "completed", ""

        def on_step(step: dict) -> None:
            step = dict(step, ts_ms=int((time.monotonic() - started) * 1000))
            steps.append(step)
            chat_jobs.publish_step(job, step)

        try:
            result = agent.chat(message, max_steps=max_steps, temperature=temperature,
                                step_callback=on_step)
            if result.get("stopped"):
                status = "cancelled"
            history_service.record_turn(
                conversation_id=conversation_id, session_id=conversation_id, turn_id=turn_id,
                user_message=message, agent_result=result, mode="assistant", domain=DOMAIN,
                steps=steps, agent_context=agent.export_conversation_context())
            chat_jobs.finish_job(job, result)
        except Exception as e:
            status, error_code = "failed", type(e).__name__
            print(f"[assistant] turn failed:\n{traceback.format_exc()}")
            chat_jobs.fail_job(job, _friendly(e))
        finally:
            try:
                gather_service.record_usage(
                    conversation_id=conversation_id, workflow_id=workflow_id,
                    model=getattr(agent, "model", "") or "",
                    usage=getattr(agent, "last_turn_usage", None), domain=DOMAIN,
                    turn_id=turn_id, latency_ms=int((time.monotonic() - started) * 1000),
                    status=status, error_code=error_code)
            except Exception:
                pass

    worker = threading.Thread(target=work, daemon=True, name="assistant-turn")
    job.worker_thread = worker
    worker.start()
    return Response(job_sse(job), mimetype="text/event-stream", headers=_SSE_HEADERS)


@assistant_bp.route("/chat/stop", methods=["POST"])
def chat_stop():
    """Stop this page's running turn. The conversation comes from the body or
    from the assistant's own session key - never from the log chatbots'."""
    data = request.get_json(silent=True) or {}
    conversation_id = (_clean_id(data.get("conversation_id"))
                       or _clean_id(session.get(SESSION_CONVERSATION)))
    stopped = bool(_running(conversation_id) and chat_jobs.request_cancel(conversation_id))
    return jsonify({"success": True, "stopped": stopped})


# ---------- actions the user confirms ----------
# An action exists only because a connector proposed it during a turn (see
# services/assistant/actions). The page sends its id; what it does was fixed
# on the server when it was proposed.

@assistant_bp.route("/actions/confirm", methods=["POST"])
def action_confirm():
    from services.assistant import actions
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        # A JSON body cannot come from a plain cross-site form: a second lock
        # on the one route here that starts something.
        return jsonify({"success": False, "error": "a JSON request body is required"}), 415
    return jsonify(actions.confirm(str(data.get("action_id") or "")))


@assistant_bp.route("/actions/dismiss", methods=["POST"])
def action_dismiss():
    from services.assistant import actions
    data = request.get_json(silent=True) or {}
    return jsonify({"success": True, "dismissed": actions.dismiss(str(data.get("action_id") or ""))})


# ---------- scheduled tasks + notifications ----------

def _task_view(task: dict) -> dict:
    return {k: task.get(k, "") for k in ("id", "instruction", "run_at", "status", "finished_at",
                                         "result_preview", "error", "conversation_id")}


@assistant_bp.route("/tasks")
def tasks_list():
    from services.assistant import tasks
    rows = tasks.list_tasks(include_finished=True)
    return jsonify({"success": True,
                    "open": [_task_view(t) for t in rows if t.get("status") in tasks.OPEN_STATES],
                    "recent": [_task_view(t) for t in rows
                               if t.get("status") not in tasks.OPEN_STATES][:8],
                    "ticker_running": tasks.ticker_running()})


@assistant_bp.route("/tasks/add", methods=["POST"])
def tasks_add():
    """The sidebar's own form: the user typed the time and the instruction
    themself, so there is nothing to confirm."""
    from datetime import datetime
    from services.assistant import tasks
    data = request.get_json(silent=True) or {}
    now = datetime.now().astimezone()
    try:
        task = tasks.schedule(str(data.get("instruction") or ""),
                              tasks.parse_when(str(data.get("when") or ""), now), now)
    except tasks.TaskError as e:
        return jsonify({"success": False, "error": str(e), "code": e.code}), 400
    return jsonify({"success": True, "task": _task_view(task)})


@assistant_bp.route("/tasks/<action>", methods=["POST"])
def tasks_change(action: str):
    """cancel | skip | run_now, each on {"id": ...}."""
    from services.assistant import tasks
    data = request.get_json(silent=True) or {}
    task_id = str(data.get("id") or "")
    if action == "cancel":
        ok = tasks.cancel(task_id)
    elif action == "skip":
        ok = tasks.skip(task_id)
    elif action == "run_now":
        task = tasks.get_task(task_id)
        ok = bool(task and task.get("status") in ("scheduled", "missed"))
        if ok:
            threading.Thread(target=tasks.run_task, args=(task_id,), daemon=True,
                             name=f"assistant-task-run-{task_id}").start()
    else:
        return jsonify({"success": False, "error": "unknown task action"}), 404
    if not ok:
        return jsonify({"success": False,
                        "error": "that task does not exist or is not in a state for this"}), 409
    return jsonify({"success": True})


@assistant_bp.route("/notifications")
def notifications_list():
    from services.assistant import tasks
    return jsonify(dict(tasks.notifications(), success=True))


@assistant_bp.route("/notifications/read", methods=["POST"])
def notifications_read():
    from services.assistant import tasks
    data = request.get_json(silent=True) or {}
    ids = data.get("ids")
    changed = tasks.mark_read(ids if isinstance(ids, list) else None)
    return jsonify({"success": True, "changed": changed})


# ---------- voice input ----------
# The clip is recognised on this PC and dropped (services/assistant/voice).
# The text goes back to the page's input box; nothing is sent from here.

@assistant_bp.route("/voice/status")
def voice_status():
    from services.assistant import voice
    return jsonify(dict(voice.status(), success=True))


@assistant_bp.route("/voice/prepare", methods=["POST"])
def voice_prepare():
    """Download the speech model (the user pressed Download)."""
    from services.assistant import voice
    if not isinstance(request.get_json(silent=True), dict):
        return jsonify({"success": False, "error": "a JSON request body is required"}), 415
    return jsonify(dict(voice.start_download(), success=True))


@assistant_bp.route("/transcribe", methods=["POST"])
def transcribe():
    from services.assistant import voice
    declared = request.content_length or 0
    if declared > voice.MAX_AUDIO_BYTES:
        return jsonify({"success": False, "code": "bad_audio",
                        "error": "the recording is too large (keep it under two minutes)"}), 413
    audio = request.get_data(cache=False)
    try:
        result = voice.transcribe(audio, language=request.args.get("lang") or None)
    except voice.VoiceUnavailable as e:
        status = 400 if e.code == "bad_audio" else 409
        return jsonify({"success": False, "code": e.code, "error": str(e)}), status
    return jsonify(dict(result, success=True))


@assistant_bp.route("/reset", methods=["POST"])
def reset():
    """New chat: the next message starts a new conversation. A turn still
    running in the old one is left to finish and stays in History."""
    session.pop(SESSION_CONVERSATION, None)
    return jsonify({"success": True})


# ---------- history ----------
# List / get / stream / rename / pin are the shared handlers, bound to the
# assistant's own history domain. Load and delete are the assistant's own:
# the shared ones move the log chatbots' session pointer.

_SHARED = build_shared_handlers(SharedRouteContext(
    domain=DOMAIN, agent_config_attr="", get_agent=lambda: None, session_agents={},
    browse_filetypes=(), load_skills_from_yaml=lambda path: [], gather_domain=DOMAIN))

assistant_bp.add_url_rule("/history/list", "history_list", _SHARED["history_list"])
assistant_bp.add_url_rule("/history/get", "history_get", _SHARED["history_get"])
assistant_bp.add_url_rule("/history/stream", "history_stream", _SHARED["history_stream"])
assistant_bp.add_url_rule("/history/rename", "history_rename", _SHARED["history_rename"],
                          methods=["POST"])
assistant_bp.add_url_rule("/history/pin", "history_pin", _SHARED["history_pin"],
                          methods=["POST"])


@assistant_bp.route("/history/load", methods=["POST"])
def history_load():
    """Reopen a saved conversation: its turns for the page, and the session
    pointer so the next message continues it."""
    data = request.get_json(silent=True) or {}
    conversation_id = _clean_id(data.get("conversation_id"))
    if not conversation_id:
        return jsonify({"success": False, "error": "conversation_id is required"}), 400
    conv = history_service.get_conversation(conversation_id, domain=DOMAIN, with_steps=True)
    running = _running(conversation_id)
    if conv is None and not running:
        return jsonify({"success": False, "error": "Conversation not found"}), 404
    session[SESSION_CONVERSATION] = conversation_id
    job = chat_jobs.get_job(conversation_id) if running else None
    return jsonify({"success": True, "conversation_id": conversation_id,
                    "title": (conv or {}).get("title") or getattr(job, "title", "") or "Conversation",
                    "turns": (conv or {}).get("turns") or [],
                    "running": running,
                    # The message being answered right now (its first 60
                    # characters): it is not among the saved turns yet.
                    "running_message": getattr(job, "title", "") if running else ""})


@assistant_bp.route("/history/delete", methods=["POST"])
def history_delete():
    data = request.get_json(silent=True) or {}
    conversation_id = _clean_id(data.get("conversation_id"))
    if not conversation_id:
        return jsonify({"success": False, "error": "conversation_id is required"}), 400
    if _running(conversation_id):
        return jsonify({"success": False,
                        "error": "this conversation is still running - stop it first"}), 409
    removed = history_service.delete_conversation(conversation_id, domain=DOMAIN)
    _drop_agent(conversation_id)
    if session.get(SESSION_CONVERSATION) == conversation_id:
        session.pop(SESSION_CONVERSATION, None)
    return jsonify({"success": bool(removed)})
