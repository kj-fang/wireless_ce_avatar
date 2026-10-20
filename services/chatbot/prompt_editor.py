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
    #: ``(yaml_path, replace) -> payload``. The same import the sidebar's
    #: "Append Skills from YAML..." performs: it writes a dated user file and
    #: flips the active source, so the choice outlives the conversation. The
    #: panel calls it for the same reason the sidebar does -- a skill set that
    #: vanished on reload left a kept prompt naming skills the agent no longer
    #: had, which is dead text in fetch_filtered_logs's enum.
    persist_skills: Optional[Callable[[str, bool], dict]] = None


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
        # Same order as conversation_speclet(): this conversation's edit, then
        # a prompt the user kept for next time, then the built-in text. The
        # panel has to show what the agent will actually read, or Save & use
        # would be comparing against the wrong "before".
        parts = {part: (overrides.get(part)
                        or sp.kept_text(context.profile, part)
                        or context.default_text(context.profile, part))
                 for part in sp.BUNDLED_KINDS}
        # The editor's note travels with the three sections, so reopening the
        # panel shows the file as it was written rather than a rebuild of it
        # with its own warnings stripped off.
        note = (overrides.get(sp.PREAMBLE)
                or sp.kept_text(context.profile, sp.PREAMBLE) or "")
        if note:
            parts[sp.PREAMBLE] = note
        return parts

    def _part_source(kind: str, overrides: dict) -> str:
        """Where the live text of one document comes from, for the badge."""
        if kind == sp.BUNDLE:
            if overrides:
                return "conversation"
            return "kept" if sp.is_kept(context.profile, sp.BUNDLE) else "built-in"
        if kind in overrides:
            return "conversation"
        return "kept" if sp.kept_text(context.profile, kind) else "built-in"

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
                doc["source"] = _part_source(doc["kind"], overrides)
                doc["kept"] = sp.is_kept(context.profile, doc["kind"]) or (
                    doc["kind"] != sp.BUNDLE
                    and sp.is_kept(context.profile, sp.BUNDLE))
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
                "source": _part_source(kind, overrides),
                "kept": sp.is_kept(doc_profile, kind) or (
                    kind != sp.BUNDLE and sp.is_kept(doc_profile, sp.BUNDLE)),
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
            wants_keep = bool(data.get("keep"))
            if skill_mode not in ("keep", "replace", "append"):
                return jsonify({"success": False, "error": "Unknown skills choice."}), 400
            if skill_mode != "keep" and not skill_path:
                return jsonify({"success": False, "error": "Choose a skills YAML file first."}), 400

            agent = context.get_agent()
            skill_note = ""
            persisted_skill_file = None
            if skill_mode != "keep":
                # The same import the sidebar performs, so the panel's choice
                # is persisted rather than living only on this agent. Skills
                # have always had their own visible source badge and their own
                # undo ("Cloud baseline"), so persisting here does not weaken
                # anything -- it removes a difference between two buttons that
                # look like they do the same thing.
                if context.persist_skills is not None:
                    try:
                        imported = context.persist_skills(skill_path, skill_mode == "replace")
                    except ValueError as exc:
                        return jsonify({"success": False, "error": str(exc)}), 400
                    except Exception as exc:
                        return jsonify({"success": False, "error": f"Could not load skills: {exc}"}), 400
                    # The dated snapshot, not the file the person picked: that
                    # one lives on their Desktop and can move or change, and a
                    # kept prompt pointing at a file that is gone is worse than
                    # one pointing at nothing.
                    persisted_skill_file = imported["target_path"]
                    skill_note = (f" Skills {'replaced with' if skill_mode == 'replace' else 'extended from'} "
                                  f"{imported['source_name']}, saved as {imported['filename']}.")
                elif context.load_skills_from_yaml is not None:
                    try:
                        chosen_skills = context.load_skills_from_yaml(skill_path)
                    except Exception as exc:
                        return jsonify({"success": False, "error": f"Could not load skills: {exc}"}), 400
                    if not chosen_skills:
                        return jsonify({"success": False, "error": "The skills file is empty."}), 400
                    agent.apply_updated_skills(
                        {**agent.skills, **chosen_skills} if skill_mode == "append" else chosen_skills)
                    agent._session_skill_source = "user"
                    agent._session_skill_path = Path(skill_path) if skill_mode == "replace" else None
                    agent._session_skill_filename = Path(skill_path).name
                else:
                    return jsonify({"success": False, "error": "Choose a skills YAML file first."}), 400

            _apply_conversation_text(kind, text)

            # Keeping is the same action, answered on the same screen rather
            # than on a second one after the fact: the person deciding has the
            # prompt and the skills in front of them, which is when they know.
            prompt_note = ""
            if wants_keep:
                try:
                    path = sp.write_user_speclet(doc_profile, kind, text)
                except Exception as exc:
                    return jsonify({"success": False, "error": f"Could not save: {exc}"}), 500
                sp.set_source(doc_profile, kind, sp.SOURCE_LOCAL)
                sp.set_kept(doc_profile, kind, True)
                # The skills travel with the prompt, because the prompt names
                # them. Keeping one without the other leaves the agent reading
                # instructions to call skills that are not in its enum.
                if persisted_skill_file:
                    sp.set_kept_skills(doc_profile, persisted_skill_file)
                _reload()
                kept_names = sp.kept_skills(doc_profile)
                prompt_note = (f"Kept as {path.name}: new conversations on this page "
                               "start from it"
                               + (" and from the skills saved with it" if kept_names else "")
                               + " until you press Restore original.")
            kept = sp.is_kept(doc_profile, kind) or (
                kind != sp.BUNDLE and sp.is_kept(doc_profile, sp.BUNDLE))
            return jsonify({
                "success": True,
                "message": (prompt_note or
                            ("Applied to this conversation only. New conversations "
                             + ("start from the prompt you kept." if kept
                                else "use the original prompt."))) + skill_note,
                "doc": f"{doc_profile}_{kind}",
                "source": "conversation",
                "kept": kept,
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

    # Asked on the way out of the panel: the one place where a person knows
    # whether the prompt they have been editing was worth more than this
    # conversation. Save & use deliberately writes nothing to disk, so without
    # this there was no answer to "keep it for next time" except downloading a
    # file and loading it again by hand.
    def prompts_keep():
        blocked = _edit_guard()
        if blocked is not None:
            return blocked
        if context.get_agent is None:
            return jsonify({"success": False,
                            "error": "Keeping a prompt needs a conversation."}), 400
        data = request.get_json(silent=True) or {}
        parsed = _parse_doc(context, data.get("doc"))
        if parsed is None:
            return jsonify({"success": False, "error": "Unknown prompt document."}), 400
        doc_profile, kind = parsed
        if doc_profile != context.profile:
            return jsonify({"success": False,
                            "error": "Shared prompts are not editable in a conversation."}), 400

        # keep: false withdraws it, and needs no text — the caller is saying
        # "stop starting new conversations from my copy", not sending one.
        if data.get("keep") is False:
            sp.set_kept(doc_profile, kind, False)
            return jsonify({
                "success": True,
                "doc": f"{doc_profile}_{kind}",
                "kept": False,
                "message": "New conversations start from the original prompt again.",
            })

        text = str(data.get("text") or "")
        if not text.strip():
            return jsonify({"success": False,
                            "error": "A prompt cannot be empty — the agent would "
                                     "be sent a blank system prompt."}), 400
        if kind == sp.BUNDLE:
            missing = [part for part in sp.BUNDLED_KINDS
                       if not sp.split_bundle(text).get(part)]
            if missing:
                return jsonify({
                    "success": False,
                    "error": "A one-file agent must contain all three non-empty "
                             "sections: ## prompt, ## report, ## suggest_time. "
                             f"Missing: {', '.join(missing)}.",
                }), 400
        try:
            path = sp.write_user_speclet(doc_profile, kind, text)
        except Exception as e:
            return jsonify({"success": False, "error": f"Could not save: {e}"}), 500
        # Three separate facts, and all three are needed: the text exists on
        # disk, the library shows that copy, and a conversation that has made
        # no choice of its own begins from it.
        sp.set_source(doc_profile, kind, sp.SOURCE_LOCAL)
        sp.set_kept(doc_profile, kind, True)
        _apply_conversation_text(kind, text)
        _reload()
        return jsonify({
            "success": True,
            "doc": f"{doc_profile}_{kind}",
            "source": "conversation",
            "kept": True,
            "filename": path.name,
            "message": f"Kept as {path.name}. New conversations on this page "
                       "start from it until you press Restore original.",
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
            if kind == sp.BUNDLE and (set(sp.split_bundle(text)) - {sp.PREAMBLE}
                                      != set(sp.BUNDLED_KINDS)):
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
            # "Original" has to mean the built-in text, not a kept copy -- a
            # Restore that left the keep standing would put the same custom
            # prompt back on the next question and look broken. Withdrawing it
            # does not delete the file: Choose .md file... and the library
            # still have it.
            was_kept = sp.is_kept(doc_profile, kind) or bool(sp.kept_skills(doc_profile))
            if sp.is_kept(doc_profile, kind):
                sp.set_kept(doc_profile, kind, False)
            # Both halves of the keep, together: the prompt named the skills,
            # so leaving the skills kept would be half a restore.
            sp.set_kept_skills(doc_profile, None)
            if kind != sp.BUNDLE and sp.is_kept(doc_profile, sp.BUNDLE):
                # One part of a kept bundle cannot be singled out, so this is
                # the whole document going back to built-in.
                sp.set_kept(doc_profile, sp.BUNDLE, False)
                was_kept = True
            return jsonify({
                "success": True,
                "doc": f"{doc_profile}_{kind}",
                "source": "built-in",
                "kept": False,
                "message": "Original prompt restored for this conversation."
                           + (" New conversations no longer start from your "
                              "kept prompt or its skills; both files are still "
                              "saved." if was_kept else ""),
            })
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
        "prompts_keep": prompts_keep,
        "prompts_use_source": prompts_use_source,
        "prompts_revert": prompts_revert,
        "prompts_refresh_shared": prompts_refresh_shared,
        "prompts_upload": prompts_upload,
    }
