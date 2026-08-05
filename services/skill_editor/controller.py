"""Shared Flask controller for the chatbot skill-editor package."""

from __future__ import annotations

import os
import threading
import traceback
from dataclasses import dataclass
from functools import partial
from typing import Any, Callable

from flask import jsonify, request, session


@dataclass(frozen=True)
class SkillEditorContext:
    activate_yaml: Callable[..., Any]
    get_active_source: Callable[..., Any]
    get_or_create_agent: Callable[..., Any]
    latest_cloud_baseline: Callable[..., Any]
    latest_user_yaml: Callable[..., Any]
    persist_user_yaml_snapshot: Callable[..., Any]
    read_yaml_file: Callable[..., Any]
    refresh_cloud_baseline: Callable[..., Any]
    resolve_cloud_skills_dir: Callable[..., Any]
    sanitise_skill_payload: Callable[..., Any]
    set_active_source: Callable[..., Any]
    skills_yaml_status_payload: Callable[..., Any]


def skills_yaml_status(context: SkillEditorContext):
    """
    Report the cloud-baseline vs user-overrides state for the side panel.

    Response JSON:
      {
        "success":          True,
        "active_source":    "cloud" | "user",
        "effective_source": "cloud" | "user",
        "cloud_local":      {path, date, filename},     # local cloud/ mirror
        "user_local":       {path, date, filename},     # local user/ overrides
        "share_remote":     {path, date, filename, reachable},
      }
    """
    try:
        return jsonify({"success": True, **context.skills_yaml_status_payload()})
    except Exception as e:
        traceback.print_exc()
        return jsonify({"success": False, "error": str(e)}), 500

def skills_yaml_use_cloud(context: SkillEditorContext):
    """
    Switch the running agent to the local cloud/ baseline (the latest file
    pulled from the share folder). The user/ overrides on disk are kept
    intact so the user can toggle back later via /skills_yaml_use_user.
    """
    try:
        c_path, c_date = context.latest_cloud_baseline()
        if c_path is None:
            return jsonify({
                "success": False,
                "error":   "No cloud baseline found. Connect to VPN and retry "
                           "so the baseline can be refreshed from the share folder.",
            }), 404
        context.set_active_source("cloud")
        skills = context.activate_yaml(c_path)
        return jsonify({
            "success":         True,
            "active_source":   "cloud",
            "local_path":      str(c_path),
            "local_date":      c_date.isoformat() if c_date else None,
            "filename":        c_path.name,
            "message":         "Now using the cloud baseline configuration.",
            "skills":          skills,
        })
    except Exception as e:
        traceback.print_exc()
        return jsonify({"success": False, "error": str(e)}), 500

def skills_yaml_use_user(context: SkillEditorContext):
    """
    Switch the running agent to the user's local overrides. Returns 404 when
    the user has not yet edited the configuration this session — the toggle
    is only meaningful once a user override exists.
    """
    try:
        u_path, u_date = context.latest_user_yaml()
        if u_path is None:
            return jsonify({
                "success": False,
                "error":   "No customised configuration found yet. Edit a "
                           "skill via 'Edit Skills Configuration' first.",
            }), 404
        context.set_active_source("user")
        skills = context.activate_yaml(u_path)
        return jsonify({
            "success":         True,
            "active_source":   "user",
            "local_path":      str(u_path),
            "local_date":      u_date.isoformat() if u_date else None,
            "filename":        u_path.name,
            "message":         "Now using your customised configuration.",
            "skills":          skills,
        })
    except Exception as e:
        traceback.print_exc()
        return jsonify({"success": False, "error": str(e)}), 500

def refresh_cloud_baseline_route(context: SkillEditorContext):
    """
    Force-refresh the local cloud/ mirror from the share folder. Idempotent —
    safe to call from a "retry" button when the user reconnects to VPN.
    """
    try:
        path, dt = context.refresh_cloud_baseline()
        if path is None:
            return jsonify({
                "success": False,
                "error":   "Share folder is unreachable; please retry on VPN.",
            }), 503

        # If the agent is currently running on the cloud baseline, reload it
        # with the freshly pulled file so the user immediately sees the new
        # skills without having to click the toggle.
        if context.get_active_source() == "cloud":
            context.activate_yaml(path)

        agent = context.get_or_create_agent()
        return jsonify({
            "success":     True,
            "local_path":  str(path),
            "local_date":  dt.isoformat() if dt else None,
            "filename":    path.name,
            "message":     "Cloud baseline refreshed from the share folder.",
            "skills":      agent.get_skill_descriptions(),
        })
    except Exception as e:
        traceback.print_exc()
        return jsonify({"success": False, "error": str(e)}), 500

def load_local_skills_yaml(context: SkillEditorContext):
    """
    Return the raw contents of a local YAML for the side-panel editor.

    Query string:
      ?source=cloud|user   (default = current active source)

    The editor uses `source=user` to pre-fill from the user's previous
    edits, and `source=cloud` to start from the pristine baseline.
    """
    requested = (request.args.get("source") or "").strip().lower() or context.get_active_source()
    try:
        if requested == "user":
            local_path, local_date = context.latest_user_yaml()
        else:
            requested = "cloud"
            local_path, local_date = context.latest_cloud_baseline()

        if local_path is None:
            return jsonify({
                "success":   False,
                "error":     ("No customised configuration on disk yet."
                              if requested == "user"
                              else "Cloud baseline not present locally. "
                                   "Connect to VPN and use 'Refresh from share folder'."),
                "source":    requested,
            }), 404

        data = context.read_yaml_file(local_path)
        return jsonify({
            "success":    True,
            "source":     requested,
            "local_path": str(local_path),
            "local_date": local_date.isoformat() if local_date else None,
            "filename":   local_path.name,
            "skills":     data,
        })
    except Exception as e:
        traceback.print_exc()
        return jsonify({"success": False, "error": str(e)}), 500

def save_local_skills_yaml(context: SkillEditorContext):
    """
    Persist edits made in the side-panel structured form to the local
    `user/` overrides directory (a NEW dated file for today). The
    `cloud/` baseline is NEVER modified; uploads back to the share
    folder happen only when the user explicitly clicks "Upload".

    Saving always switches the active source to "user" so the agent
    starts using the edits immediately.

    Request JSON:
      { "skills": { "<skill_key>": { name, description, keywords, exclusive, expert_rules } } }
    """
    data = request.get_json(silent=True) or {}
    cleaned, err = context.sanitise_skill_payload(data.get("skills"))
    if err:
        return jsonify({"success": False, "error": err}), 400

    try:
        target = context.persist_user_yaml_snapshot(cleaned)

        context.set_active_source("user")
        skills = context.activate_yaml(target)
        session["yaml_modified"] = True
        session["yaml_modified_path"] = str(target)

        return jsonify({
            "success":       True,
            "active_source": "user",
            "local_path":    str(target),
            "local_date":    None,  # filename carries the date
            "filename":      target.name,
            "message":       f"Saved {len(cleaned)} skill(s) to {target.name}.",
            "skills":        skills,
        })
    except Exception as e:
        traceback.print_exc()
        return jsonify({"success": False, "error": str(e)}), 500

def delete_local_skill(context: SkillEditorContext):
    """
    Remove a single skill from whichever local source is currently active
    and save the result under today's dated filename in `user/`. Always
    flips the active source to "user".

    Request JSON: { "skill_key": "Roaming" }
    """
    data = request.get_json(silent=True) or {}
    skill_key = (data.get("skill_key") or "").strip()
    if not skill_key:
        return jsonify({
            "success": False,
            "error":   "skill_key is required.",
        }), 400

    try:
        # Start from the user copy if it exists, otherwise from the cloud
        # baseline — the resulting file always lands in user/ and becomes
        # the new active configuration.
        src_path, _ = context.latest_user_yaml()
        if src_path is None:
            src_path, _ = context.latest_cloud_baseline()
        if src_path is None:
            return jsonify({
                "success": False,
                "error":   "No local skill YAML to edit.",
            }), 404

        existing = context.read_yaml_file(src_path)
        if skill_key not in existing:
            return jsonify({
                "success": False,
                "error":   f"Skill '{skill_key}' is not present in the active configuration.",
            }), 404

        existing.pop(skill_key, None)
        target = context.persist_user_yaml_snapshot(existing)

        context.set_active_source("user")
        skills = context.activate_yaml(target)
        session["yaml_modified"] = True
        session["yaml_modified_path"] = str(target)

        return jsonify({
            "success":       True,
            "active_source": "user",
            "local_path":    str(target),
            "filename":      target.name,
            "message":       f"Removed skill '{skill_key}'.",
            "skills":        skills,
        })
    except Exception as e:
        traceback.print_exc()
        return jsonify({"success": False, "error": str(e)}), 500

def upload_modified_yaml(context: SkillEditorContext):
    """
    Push the user's customised YAML to the share folder so the Wireless CE
    team can incorporate the tuning. Uploads ONLY ever come from the
    `user/` overrides directory — the cloud baseline is never re-uploaded
    back to itself.
    """
    import shutil as _shutil
    from pathlib import Path as _P

    try:
        local_path_str = session.get("yaml_modified_path") or ""
        local_path = _P(local_path_str) if local_path_str else None
        if local_path is None or not local_path.exists():
            latest, _ = context.latest_user_yaml()
            local_path = latest
        if local_path is None or not local_path.exists():
            return jsonify({
                "success": False,
                "error":   "No customised skill YAML was found to upload.",
            }), 404

        cloud_dir_str = context.resolve_cloud_skills_dir()
        if not cloud_dir_str:
            return jsonify({
                "success": False,
                "error":   "Shared skill folder is unreachable; please retry on VPN.",
            }), 503

        # Upload under a contributions sub-folder so cloud "latest" detection
        # still ranks team-approved revisions; reviewers promote files to the
        # top-level skills_config folder once vetted.
        contrib_dir = _P(cloud_dir_str) / "user_contributions"
        try:
            contrib_dir.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            return jsonify({
                "success": False,
                "error":   f"Cannot create contributions folder on share: {e}",
            }), 500

        import getpass
        import re as _re
        user = _re.sub(r"[^A-Za-z0-9_.-]+", "_",
                       (getpass.getuser() or os.environ.get("USERNAME") or "anon"))
        target = contrib_dir / f"{user}__{local_path.name}"
        _shutil.copy2(str(local_path), str(target))

        return jsonify({
            "success":     True,
            "uploaded_to": str(target),
            "message":     "Thank you. Your modified configuration has been "
                           "uploaded for review.",
        })
    except Exception as e:
        traceback.print_exc()
        return jsonify({"success": False, "error": str(e)}), 500

def build_profile_yaml_helpers(
    *,
    user_local_dir: Callable[[], Any],
    today_yaml_filename: Callable[[], str],
    user_yaml_prefix: str,
    latest_cloud_baseline: Callable[..., Any],
    latest_user_yaml: Callable[..., Any],
    write_yaml_file: Callable[..., Any],
    load_skills_from_yaml: Callable[[str], Any],
    get_agent: Callable[..., Any],
    agent_config_attr: str,
) -> dict[str, Callable[..., Any]]:
    """Build the four YAML helpers a chatbot profile needs.

    The BT and Wi-Fi blueprints carried identical copies of these; the only
    real differences are the dated filename prefix each profile writes into
    ``user/`` and which ``app_config`` attribute holds its app-level agent.
    """
    from configs.global_configs import app_config
    from services.skill_editor.yaml_service import gather_disabled_comments

    write_lock = threading.Lock()

    def gather_disabled(active_data: dict) -> dict:
        return gather_disabled_comments(
            active_data,
            latest_cloud_baseline=latest_cloud_baseline,
            latest_user_yaml=latest_user_yaml,
        )

    def persist_user_yaml_snapshot(data: dict) -> object:
        """Persist the current user-edited YAML under today's dated filename.

        The file name is date-based, so repeated saves on the same day target
        the same path. Writes are serialised in-process so overlapping
        save/delete requests do not race on the same target and temp file.
        """
        with write_lock:
            target_dir = user_local_dir()
            target = target_dir / today_yaml_filename()
            write_yaml_file(target, data, gather_disabled(data))

            # Keep only today's active revision in the user/ dir so lookup
            # stays unambiguous.
            for entry in target_dir.iterdir():
                if entry.is_file() and entry.name != target.name \
                        and entry.name.startswith(user_yaml_prefix) and entry.suffix == ".yaml":
                    try:
                        entry.unlink()
                    except OSError:
                        pass

            return target

    def refresh_loaded_skills(yaml_path: str) -> dict:
        """Re-load skills from `yaml_path` into the live agent and llm_helper."""
        skills = load_skills_from_yaml(yaml_path)
        agent = get_agent()
        # Keep history; clear rule/filter caches so the reloaded skills apply.
        agent.apply_updated_skills(skills)
        app_agent = getattr(app_config, agent_config_attr, None)
        if app_agent:
            app_agent.skills = skills
        if app_config.llm_helper:
            app_config.llm_helper.skills = skills
        return skills

    def activate_yaml(path) -> dict:
        """Re-load skills from `path` and return the chatbot's descriptions."""
        refresh_loaded_skills(str(path))
        return get_agent().get_skill_descriptions()

    return {
        "gather_disabled_comments": gather_disabled,
        "persist_user_yaml_snapshot": persist_user_yaml_snapshot,
        "refresh_loaded_skills": refresh_loaded_skills,
        "activate_yaml": activate_yaml,
    }


def build_skill_editor_handlers(
    context: SkillEditorContext,
) -> dict[str, Callable[..., Any]]:
    return {
        "skills_yaml_status": partial(skills_yaml_status, context),
        "skills_yaml_use_cloud": partial(skills_yaml_use_cloud, context),
        "skills_yaml_use_user": partial(skills_yaml_use_user, context),
        "refresh_cloud_baseline_route": partial(refresh_cloud_baseline_route, context),
        "load_local_skills_yaml": partial(load_local_skills_yaml, context),
        "save_local_skills_yaml": partial(save_local_skills_yaml, context),
        "delete_local_skill": partial(delete_local_skill, context),
        "upload_modified_yaml": partial(upload_modified_yaml, context),
    }
