"""Route handlers shared verbatim by the Wi-Fi and Bluetooth chatbot blueprints.

The two profiles used to carry byte-identical copies of the file-browse dialog,
the Stop endpoint, the whole conversation-history CRUD group and the three
skill-reload endpoints. Everything that actually differed between them was a
handful of values — the history domain key, the ``app_config`` attribute holding
the app-level agent, the native dialog's file filter and the profile's skill
loader functions — so they are collected in :class:`SharedRouteContext` and the
handlers are built from it.

The resulting mapping is merged into the blueprint's adapter namespace exactly
like ``build_skill_editor_handlers``.
"""

from __future__ import annotations

import json
import threading
import traceback
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Sequence

from flask import Response, jsonify, redirect, request, session, url_for

from configs.global_configs import app_config
from services import history_service
from services import gather_service
from services.chatbot.issue_context import extract_issue_context
from services.chatbot import job_runtime as chat_jobs
from services.chatbot.job_runtime import job_sse


@dataclass(frozen=True)
class SharedRouteContext:
    """Per-profile values the shared handlers need."""

    #: History/job partition key. ``""`` = Wi-Fi (legacy default), ``"bt"`` = Bluetooth.
    domain: str
    #: Name of the ``app_config`` attribute holding this profile's app-level agent.
    agent_config_attr: str
    #: Returns the current session's agent (the profile's ``_get_or_create_agent``).
    get_agent: Callable[[], Any]
    #: The profile's ``_chatbot_instances`` map, so leaving the page can drop
    #: this session's agent instead of leaking it until the process restarts.
    session_agents: dict
    #: ``filetypes`` passed to the native open dialog.
    browse_filetypes: Sequence[tuple[str, str]]
    #: YAML loader shared by the remaining skill-source endpoints.
    load_skills_from_yaml: Callable[[str], Any]
    #: Gather's domain label for this profile. NOT the same string as ``domain``
    #: above: that one partitions history ("" for Wi-Fi), this one labels
    #: analytics rows ("wifi"). Keeping them separate preserves both the legacy
    #: history layout and the analytics values main already writes.
    gather_domain: str = "wifi"


def llm_client_model(agent_config_attr: str):
    """Return (client, model) for one-shot LLM calls, borrowing from the
    pre-initialised chatbot agent or the llm_helper. (None, None) when the app
    has no API key configured — callers then fall back to deterministic logic."""
    base = getattr(app_config, agent_config_attr, None)
    if base is not None and getattr(base, "client", None) is not None:
        return base.client, getattr(base, "model", None)
    helper = getattr(app_config, "llm_helper", None)
    if helper is not None and getattr(helper, "client", None) is not None:
        return helper.client, getattr(helper, "model", "gpt-4.1")
    return None, None


def leave_chatbot(session_agents: dict):
    """Tear down this browser session's chatbot state and go back to Avatar.

    Every profile needs this — a page reloaded after "Back to Avatar" must not
    show anything from the previous run — so it is a plain function rather than
    part of :func:`build_shared_handlers`, which only the two full agents use.
    """
    # 1) Discard the per-session agent instance (chat history, skill cache,
    #    primed context, issue_time, etc.).
    sid = session.pop("chatbot_session_id", None)
    if sid and sid in session_agents:
        try:
            session_agents.pop(sid, None)
        except Exception:
            pass

    # 2) Drop every Flask-session key that would otherwise re-seed a new agent
    #    via prime_with_context() the next time this profile's page is visited
    #    (case context, AI analysis, classification, selected attachments,
    #    cached log path, etc.).
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
        "feedback_conversation_id",   # the next visit starts a fresh conversation
    ):
        session.pop(key, None)

    # 3) Clear the global "last analyzed log" hint so the chatbot page doesn't
    #    pre-fill the previous run's log path.
    try:
        app_config.last_analyzed_log_path = ""
    except Exception:
        pass

    return redirect(url_for("main.index"))


def build_shared_handlers(ctx: SharedRouteContext) -> dict[str, Callable[..., Any]]:
    """Build the profile-bound copies of every shared route handler."""

    def _apply_skills_everywhere(skills):
        """Swap skills on the session agent AND the app-level ones.

        Mid-conversation skill edit: ``apply_updated_skills`` also clears the
        rule/filter caches so the edit actually takes effect, while keeping
        the conversation history.
        """
        agent = ctx.get_agent()
        agent.apply_updated_skills(skills)
        app_agent = getattr(app_config, ctx.agent_config_attr, None)
        if app_agent:
            app_agent.skills = skills
        if app_config.llm_helper:
            app_config.llm_helper.skills = skills
        return agent

    # ------------------------------------------------------------------
    # API: open native file browser and return selected path
    # ------------------------------------------------------------------
    def browse():
        """Open a native Windows file dialog and return the selected log path."""
        import tkinter as tk
        from tkinter import filedialog

        result = {"path": ""}

        def _open_dialog():
            root = tk.Tk()
            root.withdraw()
            root.wm_attributes("-topmost", True)
            path = filedialog.askopenfilename(
                title="Select log file",
                filetypes=list(ctx.browse_filetypes),
            )
            root.destroy()
            result["path"] = path or ""

        # tkinter must run on the main thread on Windows;
        # since Flask dev server is single-threaded this is fine,
        # but we guard with a thread join timeout to make it safe.
        t = threading.Thread(target=_open_dialog)
        t.start()
        t.join(timeout=60)

        return jsonify({"success": True, "path": result["path"]})

    # ------------------------------------------------------------------
    # API: stop the running analysis.
    #
    # The target conversation is resolved from the caller's OWN session (the
    # frontend only learns conversation_id on the terminal 'done' event, so a
    # Stop clicked mid-stream usually sends an empty id). This scopes
    # cancellation to this session's job only — never other tabs'/users'
    # running analyses.
    # ------------------------------------------------------------------
    def chat_stop():
        try:
            data = request.get_json(silent=True) or {}
            conversation_id = (data.get("conversation_id") or "").strip()
            if not conversation_id:
                conversation_id = (session.get("feedback_conversation_id") or "").strip()
            job = chat_jobs.get_job(conversation_id) if conversation_id else None
            stopped = chat_jobs.request_cancel(conversation_id) if conversation_id else False
            # A cancelled turn still spent tokens and still says something about
            # how the agent is doing, so Gather gets a row for it. Non-blocking
            # and swallowed: analytics must never turn a successful Stop into an
            # error the user sees.
            if stopped and job is not None:
                try:
                    gather_service.record_turn_status(
                        conversation_id=conversation_id,
                        turn_id=getattr(job, "turn_id", ""),
                        status="cancelled",
                        workflow_id=session.get("gather_workflow_id", ""),
                        issue=extract_issue_context(),
                        domain=ctx.gather_domain,
                    )
                except Exception:
                    pass
            return jsonify({"success": True, "stopped": bool(stopped)})
        except Exception as e:
            return jsonify({"success": False, "error": str(e)}), 500

    # ------------------------------------------------------------------
    # API: local conversation history (Gemini / Claude style sidebar)
    #
    # Every chat turn is persisted by history_service into this profile's own
    # domain folder, so the two bots' conversations never mix. These endpoints
    # let the sidebar list / load / delete them. All are read/written locally.
    # ------------------------------------------------------------------
    def history_list():
        try:
            conversations = history_service.list_conversations(domain=ctx.domain)
            # Merge in-memory running jobs so the sidebar can show a ⏳ marker:
            #   * a persisted conversation that's mid-analysis  -> running: True
            #   * a brand-new first analysis not yet on disk     -> synthetic entry
            # Scoped to this profile's domain so the other bot's concurrent
            # analysis never leaks into this list.
            try:
                running = {
                    j["conversation_id"]: j
                    for j in chat_jobs.active_summaries(domain=ctx.domain)
                }
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
        # chat_jobs is a single registry shared by both bots (keyed by
        # conversation_id only), so the other bot's job id handed to this
        # endpoint would otherwise resolve and stream its steps/result here.
        # Reject anything not tagged with this profile's domain.
        if job is None or getattr(job, "domain", "") != ctx.domain:
            def _idle():
                yield "data: " + json.dumps({"type": "idle"}) + "\n\n"
            return Response(_idle(), mimetype="text/event-stream", headers=headers)
        return Response(job_sse(job), mimetype="text/event-stream", headers=headers)

    def history_get():
        conversation_id = (request.args.get("conversation_id") or "").strip()
        if not conversation_id:
            return jsonify({"success": False, "error": "conversation_id is required"}), 400
        # with_steps: the client re-renders each saved turn's reasoning trace,
        # so the stored steps have to travel with the conversation. Without it
        # a replayed turn shows only its answer and the Agent Processing Steps
        # card never appears.
        conv = history_service.get_conversation(
            conversation_id, domain=ctx.domain, with_steps=True
        )
        if conv is None:
            return jsonify({"success": False, "error": "Conversation not found"}), 404
        return jsonify({"success": True, "conversation": conv})

    def history_delete():
        data = request.get_json(silent=True) or {}
        conversation_id = (data.get("conversation_id") or "").strip()
        if not conversation_id:
            return jsonify({"success": False, "error": "conversation_id is required"}), 400
        removed = history_service.delete_conversation(conversation_id, domain=ctx.domain)
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
        ok = history_service.rename_conversation(conversation_id, title, domain=ctx.domain)
        return jsonify({"success": bool(ok)})

    def history_pin():
        data = request.get_json(silent=True) or {}
        conversation_id = (data.get("conversation_id") or "").strip()
        pinned = bool(data.get("pinned"))
        if not conversation_id:
            return jsonify({"success": False, "error": "conversation_id is required"}), 400
        ok = history_service.set_pinned(conversation_id, pinned, domain=ctx.domain)
        return jsonify({"success": bool(ok), "pinned": pinned})

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
            skills = ctx.load_skills_from_yaml(yaml_path)
            agent = _apply_skills_everywhere(skills)

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
    # API: Reload skills from shared folder (auto-discovery)
    # ------------------------------------------------------------------
    def reload_from_shared():
        """
        Reload skills from the shared YAML location.
        Used for development/testing without restarting the app.
        """
        from configs.path_configs import (
            SKILLS_CONFIG_DIR_prim,
            SKILLS_CONFIG_DIR_bkup,
            SKILLS_YAML_FILENAME,
        )
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

            skills = ctx.load_skills_from_yaml(yaml_shared)
            agent = _apply_skills_everywhere(skills)

            return jsonify({
                "success": True,
                "message": f"{len(skills)} skills reloaded from shared folder",
                "source": yaml_shared,
                "skills": agent.get_skill_descriptions(),
            })
        except Exception as e:
            traceback.print_exc()
            return jsonify({"success": False, "error": str(e)}), 500

    def back_to_avatar():
        return leave_chatbot(ctx.session_agents)

    return {
        "back_to_avatar": back_to_avatar,
        "browse": browse,
        "chat_stop": chat_stop,
        "history_list": history_list,
        "history_stream": history_stream,
        "history_get": history_get,
        "history_delete": history_delete,
        "history_pin": history_pin,
        "history_rename": history_rename,
        "load_skills_yaml_route": load_skills_yaml_route,
        "reload_from_shared": reload_from_shared,
    }
