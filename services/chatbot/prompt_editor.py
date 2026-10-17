"""Prompt editor endpoints for profile-owned conversation overrides.

Production supplies ``get_agent``: Save & use changes that conversation's
prompt/report/suggest-time, optionally replacing/adding its skills in the
same action. Shared and saved Markdown remain selectable source material, but
do not automatically override a fresh conversation. The legacy persistent
handlers remain for callers without ``get_agent``.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

from flask import jsonify, request

from utils import speclets_utils as sp


@dataclass(frozen=True)
class PromptEditorContext:
    #: Whose page this is: "wifi", "bt", "nw", "linux".
    profile: str
    #: ``(profile, kind) -> str`` for the text compiled into the agent.
    default_text: Callable[[str, str], str]
    #: When supplied, edits are confined to the current conversation agent.
    get_agent: Optional[Callable[[], Any]] = None
    ensure_editable: Optional[Callable[[], None]] = None
    load_skills_from_yaml: Optional[Callable[[str], dict]] = None


def _parse_doc(context: PromptEditorContext, raw: Any) -> Optional[tuple[str, str]]:
    """``"linux_prompt"`` -> ``("linux", "prompt")``, if this page owns it.

    Returns None for anything else, including another profile's documents —
    the page offers a fixed list, so a request for something off it is a bug
    or a hand-rolled call, not a case to serve.
    """
    wanted = str(raw or "").strip()
    for doc_profile, kind in sp.documents_for(context.profile):
        if f"{doc_profile}_{kind}" == wanted:
            return doc_profile, kind
    return None


def _reload() -> int:
    """Re-resolve every document into the cache the engine reads per turn."""
    return sp.load_into_cache()


def build_prompt_editor_handlers(context: PromptEditorContext) -> dict[str, Callable[..., Any]]:
    """The seven endpoints, bound to one profile."""

    def _conversation_parts() -> dict[str, str]:
        agent = context.get_agent()
        overrides = getattr(agent, "prompt_overrides", {})
        return {part: overrides.get(part) or context.default_text(context.profile, part)
                for part in sp.BUNDLED_KINDS}

    def _apply_conversation_text(kind: str, text: str) -> None:
        agent = context.get_agent()
        if kind == sp.BUNDLE:
            # A bundle replaces all three together; stale per-part overrides
            # from a previous domain cannot leak into the new one.
            agent.prompt_overrides = sp.split_bundle(text)
        else:
            agent.prompt_overrides[kind] = text.strip()

    def _edit_guard():
        if context.ensure_editable is not None:
            try:
                context.ensure_editable()
            except RuntimeError as exc:
                return jsonify({"success": False, "error": str(exc)}), 409
        return None

    def prompts_status():
        payload = sp.speclet_status(context.profile, context.default_text)
        if context.get_agent is not None:
            overrides = getattr(context.get_agent(), "prompt_overrides", {})
            payload["documents"] = [d for d in payload["documents"]
                                    if d["profile"] == context.profile]
            for doc in payload["documents"]:
                doc["source"] = ("conversation" if overrides else "built-in") if doc["kind"] == sp.BUNDLE else (
                    "conversation" if doc["kind"] in overrides else "built-in")
                doc["via_bundle"] = False
            payload["conversation_only"] = True
        return jsonify(payload)

    def prompts_get():
        parsed = _parse_doc(context, request.args.get("doc"))
        if parsed is None:
            return jsonify({"success": False, "error": "Unknown prompt document."}), 400
        doc_profile, kind = parsed
        if context.get_agent is not None:
            if doc_profile != context.profile:
                return jsonify({"success": False, "error": "Shared prompts are not editable in a conversation."}), 400
            parts = _conversation_parts()
            overrides = getattr(context.get_agent(), "prompt_overrides", {})
            live = sp.join_bundle(parts) if kind == sp.BUNDLE else parts[kind]
            return jsonify({
                "success": True,
                "doc": f"{doc_profile}_{kind}",
                "filename": sp.speclet_filename(doc_profile, kind),
                "source": "conversation" if (bool(overrides) if kind == sp.BUNDLE
                                            else kind in overrides) else "built-in",
                "reads": "conversation",
                "live": live,
                "local": sp.read_user_speclet(doc_profile, kind) or "",
                "shared": sp._read_local(doc_profile, kind) or "",
                "builtin": context.default_text(doc_profile, kind) or "",
            })
        live, source = sp.resolve(doc_profile, kind)
        built_in = context.default_text(doc_profile, kind) or ""
        return jsonify({
            "success": True,
            "doc": f"{doc_profile}_{kind}",
            "filename": sp.speclet_filename(doc_profile, kind),
            "source": source,
            "reads": sp.get_source(doc_profile, kind),
            # The live text is what the next turn will use; the other three
            # are what the editor offers to start from or compare against.
            "live": live or built_in,
            "local": sp.read_user_speclet(doc_profile, kind) or "",
            "shared": sp._read_local(doc_profile, kind) or "",
            "builtin": built_in,
        })

    def prompts_save():
        blocked = _edit_guard()
        if blocked is not None:
            return blocked
        data = request.get_json(silent=True) or {}
        parsed = _parse_doc(context, data.get("doc"))
        if parsed is None:
            return jsonify({"success": False, "error": "Unknown prompt document."}), 400
        text = str(data.get("text") or "")
        if not text.strip():
            return jsonify({"success": False,
                            "error": "A prompt cannot be empty — the agent would "
                                     "be sent a blank system prompt."}), 400
        doc_profile, kind = parsed
        if kind == sp.BUNDLE:
            sections = sp.split_bundle(text)
            missing = [part for part in sp.BUNDLED_KINDS if not sections.get(part)]
            if missing:
                return jsonify({
                    "success": False,
                    "error": "A one-file agent must contain all three non-empty "
                             "sections: ## prompt, ## report, ## suggest_time. "
                             f"Missing: {', '.join(missing)}.",
                }), 400
        if context.get_agent is not None:
            if doc_profile != context.profile:
                return jsonify({"success": False, "error": "Shared prompts are not editable in a conversation."}), 400
            skill_mode = str(data.get("skill_mode") or "keep")
            skill_path = str(data.get("skill_yaml_path") or "").strip()
            if skill_mode not in ("keep", "replace", "append"):
                return jsonify({"success": False, "error": "Unknown skills choice."}), 400
            if skill_mode != "keep":
                if not skill_path or context.load_skills_from_yaml is None:
                    return jsonify({"success": False, "error": "Choose a skills YAML file first."}), 400
                try:
                    chosen_skills = context.load_skills_from_yaml(skill_path)
                except Exception as exc:
                    return jsonify({"success": False, "error": f"Could not load skills: {exc}"}), 400
                if not chosen_skills:
                    return jsonify({"success": False, "error": "The skills file is empty."}), 400
            agent = context.get_agent()
            if skill_mode != "keep":
                skills = ({**agent.skills, **chosen_skills} if skill_mode == "append"
                          else chosen_skills)
                agent.apply_updated_skills(skills)
                agent._session_skill_source = "user"
                agent._session_skill_path = Path(skill_path) if skill_mode == "replace" else None
                agent._session_skill_filename = Path(skill_path).name
            _apply_conversation_text(kind, text)
            return jsonify({
                "success": True,
                "message": "Applied to this conversation only. New conversations use the original prompt.",
                "doc": f"{doc_profile}_{kind}",
                "source": "conversation",
                "skills": agent.get_skill_descriptions() if skill_mode != "keep" else None,
            })
        try:
            path = sp.write_user_speclet(doc_profile, kind, text)
        except Exception as e:
            return jsonify({"success": False, "error": f"Could not save: {e}"}), 500
        # Saving is also choosing: a copy nothing reads would be a silent no-op.
        sp.set_source(doc_profile, kind, sp.SOURCE_LOCAL)
        _reload()
        return jsonify({
            "success": True,
            "message": f"Saved. The next question uses this prompt ({path.name}).",
            "doc": f"{doc_profile}_{kind}",
            "source": sp.SOURCE_LOCAL,
        })

    def prompts_use_source():
        blocked = _edit_guard()
        if blocked is not None:
            return blocked
        data = request.get_json(silent=True) or {}
        parsed = _parse_doc(context, data.get("doc"))
        if parsed is None:
            return jsonify({"success": False, "error": "Unknown prompt document."}), 400
        doc_profile, kind = parsed
        if context.get_agent is not None:
            if doc_profile != context.profile:
                return jsonify({"success": False, "error": "Shared prompts are not editable in a conversation."}), 400
            wanted = str(data.get("source") or "")
            if wanted == sp.SOURCE_LOCAL:
                text = sp.read_user_speclet(doc_profile, kind)
            elif wanted == sp.SOURCE_SHARED:
                text = sp._read_local(doc_profile, kind)
            elif wanted == sp.SOURCE_BUILTIN:
                text = context.default_text(doc_profile, kind)
            else:
                text = None
            if not text:
                return jsonify({"success": False, "error": "That prompt source is unavailable."}), 400
            if kind == sp.BUNDLE and set(sp.split_bundle(text)) != set(sp.BUNDLED_KINDS):
                return jsonify({"success": False, "error": "The bundle needs all three sections."}), 400
            _apply_conversation_text(kind, text)
            return jsonify({"success": True, "doc": f"{doc_profile}_{kind}",
                            "source": "conversation"})
        wanted = str(data.get("source") or "")
        if wanted == sp.SOURCE_LOCAL and not sp.read_user_speclet(doc_profile, kind):
            return jsonify({"success": False,
                            "error": "There is no local copy of this prompt yet. "
                                     "Save one first."}), 400
        stored = sp.set_source(doc_profile, kind, wanted)
        _reload()
        _text, live_source = sp.resolve(doc_profile, kind)
        return jsonify({"success": True, "doc": f"{doc_profile}_{kind}",
                        "reads": stored, "source": live_source})

    def prompts_revert():
        blocked = _edit_guard()
        if blocked is not None:
            return blocked
        data = request.get_json(silent=True) or {}
        parsed = _parse_doc(context, data.get("doc"))
        if parsed is None:
            return jsonify({"success": False, "error": "Unknown prompt document."}), 400
        doc_profile, kind = parsed
        if context.get_agent is not None:
            if doc_profile != context.profile:
                return jsonify({"success": False, "error": "Shared prompts are not editable in a conversation."}), 400
            agent = context.get_agent()
            if kind == sp.BUNDLE:
                agent.prompt_overrides.clear()
            else:
                agent.prompt_overrides.pop(kind, None)
            return jsonify({"success": True, "doc": f"{doc_profile}_{kind}",
                            "source": "built-in", "message": "Original prompt restored for this conversation."})
        removed = sp.delete_user_speclet(doc_profile, kind)
        sp.set_source(doc_profile, kind, sp.SOURCE_SHARED)
        _reload()
        _text, live_source = sp.resolve(doc_profile, kind)
        return jsonify({
            "success": True,
            "doc": f"{doc_profile}_{kind}",
            "source": live_source,
            "message": ("Your copy was deleted; this prompt now reads "
                        f"the {live_source} text.") if removed else
                       f"This prompt reads the {live_source} text.",
        })

    def prompts_refresh_shared():
        # The share probe is the slow part, so this is its own action rather
        # than something every status call pays for.
        try:
            copied = sp.refresh_local_mirror()
        except Exception as e:
            return jsonify({"success": False, "error": f"Could not reach the share: {e}"}), 502
        _reload()
        return jsonify({"success": True, "copied": copied,
                        "message": f"Refreshed {copied} prompt(s) from the share."})

    def prompts_upload():
        if context.get_agent is not None:
            return jsonify({"success": False,
                            "error": "Conversation-only prompts are not uploaded to the team share."}), 400
        data = request.get_json(silent=True) or {}
        parsed = _parse_doc(context, data.get("doc"))
        if parsed is None:
            return jsonify({"success": False, "error": "Unknown prompt document."}), 400
        doc_profile, kind = parsed
        ok, message = sp.publish_user_speclet(doc_profile, kind)
        return jsonify({"success": ok, "message" if ok else "error": message}), (200 if ok else 400)

    return {
        "prompts_status": prompts_status,
        "prompts_get": prompts_get,
        "prompts_save": prompts_save,
        "prompts_use_source": prompts_use_source,
        "prompts_revert": prompts_revert,
        "prompts_refresh_shared": prompts_refresh_shared,
        "prompts_upload": prompts_upload,
    }
