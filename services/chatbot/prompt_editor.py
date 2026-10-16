"""The prompt editor: the skills lifecycle, applied to an agent's prompts.

A skill is a keyword filter a domain expert tunes from the page. A prompt is
the other half of what makes an agent an expert in anything — it carries the
identity, what to look for, and the shape of the report — and until now it
could only be changed by dropping a file on the share and restarting.

These handlers give the prompt the same three places to live and the same
moves (see :mod:`utils.speclets_utils` for the resolution rules):

    status          every document, where it reads from, and what is on disk
    get             one document's live / local / shared / built-in text
    save            write your own copy and point the document at it
    use_source      switch one document between the share and your copy
    revert          delete your copy, so the document reads the share again
    refresh_shared  re-mirror the share, for a prompt someone else changed
    upload          publish your copy to the share, for everyone

Every write reloads the in-memory cache, and the engine reads that cache
fresh on every turn, so an edit takes effect on the next question with no
restart and no reload of the page.

Only the profile's own two documents and the three the engine sends on its
own behalf are reachable from one page, so the Linux editor cannot rewrite
Wi-Fi's prompt. The shared three DO affect every agent, which the payload
marks and the page says out loud.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Optional

from flask import jsonify, request

from utils import speclets_utils as sp


@dataclass(frozen=True)
class PromptEditorContext:
    #: Whose page this is: "wifi", "bt", "nw", "linux".
    profile: str
    #: ``(profile, kind) -> str`` for the text compiled into the agent.
    default_text: Callable[[str, str], str]


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

    def prompts_status():
        return jsonify(sp.speclet_status(context.profile, context.default_text))

    def prompts_get():
        parsed = _parse_doc(context, request.args.get("doc"))
        if parsed is None:
            return jsonify({"success": False, "error": "Unknown prompt document."}), 400
        doc_profile, kind = parsed
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
        data = request.get_json(silent=True) or {}
        parsed = _parse_doc(context, data.get("doc"))
        if parsed is None:
            return jsonify({"success": False, "error": "Unknown prompt document."}), 400
        doc_profile, kind = parsed
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
        data = request.get_json(silent=True) or {}
        parsed = _parse_doc(context, data.get("doc"))
        if parsed is None:
            return jsonify({"success": False, "error": "Unknown prompt document."}), 400
        doc_profile, kind = parsed
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
