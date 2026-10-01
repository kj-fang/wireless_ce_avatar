from flask import Blueprint, render_template, request, session, redirect, url_for, flash, Response, jsonify
import json
import os
import re
import time
import traceback
from pathlib import Path

from services.llm_service import LLM_helper
from configs.global_configs import app_config
from configs.path_configs import USER_KEY_LOCAL_SUBDIR
from services import gather_service
from services.feedback_service import _current_user

llm_bp = Blueprint("llm", __name__, url_prefix="/llm")


# ------------ REGISTER SOCKETIO -------------
def register_socketio_handlers(socketio):
    """Register the /api_key_error namespace so `emit(...)` reaches connected
    browsers.

    Flask-SocketIO only accepts client connections to namespaces that have at
    least one handler registered; without this stub the modal's
    `io('/api_key_error')` connect silently fails and the `personal_token_expired`
    event never reaches the frontend.
    """
    @socketio.on('connect', namespace='/api_key_error')
    def _api_key_error_connect():
        print("🔌 [/api_key_error] client connected")

    @socketio.on('disconnect', namespace='/api_key_error')
    def _api_key_error_disconnect():
        print("🔌 [/api_key_error] client disconnected")
# ------------ REGISTER SOCKETIO -------------


@llm_bp.route('/get_llm_analysis', methods=['GET'])
def get_llm_analysis():
    print(f"🤖 LLM Analysis started")
    session['classification'] = {
                "issue_type": "Unclassified",
                "confidence": 0,
                "keywords_found": []
            }
    started = time.perf_counter()
    operation_usage = LLM_helper.empty_usage()
    _ctx_full = {}
    try:
        llm_helper: LLM_helper = app_config.llm_helper
        if llm_helper != None:
            # Rehydrate from the on-disk sidecar so the LLM analysis
            # sees the comments/attachment_list payload that doesn't
            # fit in the cookie session for heavyweight cases.
            from models.models import CaseContext as _CaseContextLocal
            _ctx_full = _CaseContextLocal.from_session(
                session.get("case_context") or {}
            ).to_dict()
            ai_analysis, operation_usage = llm_helper.analyze_desc(
                prompt_path = session['prompt_file_path'],
                case_context = _ctx_full,
                return_usage = True,
            )
            if type(ai_analysis) == dict:
                session['classification'] = ai_analysis["Classification"]
        else:
            ai_analysis = "LLM helper currently not available"
        
        response_data = {
            'success': True,
            'ai_analysis': ai_analysis
        }
        print("session['classification'] ", session['classification'])
        session['ai_ips_analysis'] = ai_analysis
        if llm_helper is not None:
            try:
                feature_status = "success" if ai_analysis else "failed"
                gather_service.record_feature_usage(
                    workflow_id=session.get("gather_workflow_id", ""),
                    feature_code="select_attachments_ai_summary",
                    model=getattr(llm_helper, "model", "") or "",
                    usage=operation_usage,
                    issue=_ctx_full,
                    domain=str(_ctx_full.get("wifi_or_bt") or "wifi"),
                    trigger="click_ai",
                    status=feature_status,
                    latency_ms=int((time.perf_counter() - started) * 1000),
                    error_code="" if feature_status == "success" else "empty_result",
                )
                gather_service.record_attachment_declaration(
                    workflow_id=session.get("gather_workflow_id", ""),
                    # Hand over the raw summary so the classification, its
                    # confidence, and the sentence behind it are all recorded.
                    ai_analysis=ai_analysis,
                    source="select_attachments_ai_summary",
                    issue=_ctx_full,
                    domain=str(_ctx_full.get("wifi_or_bt") or "wifi"),
                )
            except Exception:
                pass
        return Response(
            json.dumps(response_data, ensure_ascii=False, indent=2),
            mimetype='application/json'
        )
    except Exception as e:
        try:
            llm_helper = app_config.llm_helper
            gather_service.record_feature_usage(
                workflow_id=session.get("gather_workflow_id", ""),
                feature_code="select_attachments_ai_summary",
                model=getattr(llm_helper, "model", "") if llm_helper else "",
                usage=operation_usage,
                issue=_ctx_full,
                domain=str(_ctx_full.get("wifi_or_bt") or "wifi"),
                trigger="click_ai",
                status="failed",
                latency_ms=int((time.perf_counter() - started) * 1000),
                error_code=type(e).__name__,
            )
        except Exception:
            pass
        error_traceback = traceback.format_exc()
        print(f"❌ Full traceback:\n{error_traceback}")
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500
    

# ---------------------------------------------------------------------------
# Personal gnaigpt token refresh — triggered by the frontend modal that
# opens on personal-token 401 (see set_up_app._make_personal_token_expired_hook).
# ---------------------------------------------------------------------------

_JWT_PART_RE = None  # populated lazily to avoid re import in cold paths


def _is_valid_jwt_shape(token: str) -> bool:
    """Level-1 format check: three non-empty base64url-ish segments."""
    if not isinstance(token, str):
        return False
    parts = token.strip().split(".")
    if len(parts) != 3:
        return False
    return all(p and all(ch.isalnum() or ch in "-_" for ch in p) for p in parts)


def _login() -> str:
    """Windows login lowercased — same convention as configs.set_up_app._current_login."""
    try:
        return (_current_user() or "").strip().lower()
    except Exception:
        return ""


def _personal_token_file_content(login: str, jwt: str) -> str:
    return f'gnaigpt_token_per_user = {{\n    "{login}": "{jwt}",\n}}\n'


def _normalize_token_map(token_map) -> dict:
    if not isinstance(token_map, dict):
        return {}
    return {str(k).strip(): v for k, v in token_map.items() if v}


def _render_token_map_literal(token_map: dict) -> str:
    lines = ["gnaigpt_token_per_user = {"]
    for key, value in token_map.items():
        escaped = str(value).replace('\\', '\\\\').replace('"', '\\"')
        lines.append(f'    "{str(key)}": "{escaped}",')
    lines.append("}")
    return "\n".join(lines)


def _update_keys_module_personal_token(key_module, login: str, jwt: str) -> dict:
    """Update the in-memory `gnaigpt_token_per_user` dict and patch its source file.

    This keeps the loaded module and the physical keys.py file in sync so the
    runtime pool and the developer copy stay aligned.
    """
    if key_module is None:
        raise RuntimeError("key module not initialised")

    key_path = getattr(key_module, "__file__", None)
    if not key_path:
        raise RuntimeError("key module file path unavailable")

    key_file = Path(key_path)
    old_map = _normalize_token_map(getattr(key_module, "gnaigpt_token_per_user", None) or {})
    new_map = dict(old_map)
    new_map[login] = jwt

    snapshot = key_file.read_text(encoding="utf-8") if key_file.exists() else None
    source = snapshot or ""
    pattern = re.compile(r"gnaigpt_token_per_user\s*=\s*\{.*?\}", re.S)
    replacement = _render_token_map_literal(new_map)
    if pattern.search(source):
        updated = pattern.sub(replacement, source, count=1)
    else:
        updated = source.rstrip() + "\n\n" + replacement + "\n"

    # Write via temp file then os.replace so the key file is never left half-written.
    tmp = key_file.with_suffix(key_file.suffix + ".tmp")
    tmp.write_text(updated, encoding="utf-8")
    os.replace(tmp, key_file)
    key_module.gnaigpt_token_per_user = new_map
    return old_map


def _restore_keys_module_personal_token(key_module, old_map: dict) -> None:
    try:
        if key_module is not None:
            key_module.gnaigpt_token_per_user = old_map
        key_file = Path(getattr(key_module, "__file__", ""))
        if key_file.exists():
            current = key_file.read_text(encoding="utf-8")
            new_text = _render_token_map_literal(old_map)
            if "gnaigpt_token_per_user" in current:
                current = re.sub(r"gnaigpt_token_per_user\s*=\s*\{.*?\}", new_text, current, count=1, flags=re.S)
                key_file.write_text(current, encoding="utf-8")
            else:
                key_file.write_text(current.rstrip() + "\n\n" + new_text + "\n", encoding="utf-8")
    except Exception as e:
        print(f"⚠️  [personal_token] rollback of keys.py map failed: {e}")


def _snapshot_and_write(path: Path, new_content: str) -> str | None:
    """Atomic-ish write with old-content snapshot for rollback.

    Returns the old text (or None if the file did not exist) so the caller
    can restore it on a later step's failure. Uses ``os.replace`` after a
    temp write so a mid-write crash never leaves a truncated target.
    """
    old = path.read_text(encoding="utf-8") if path.exists() else None
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(new_content, encoding="utf-8")
    os.replace(tmp, path)
    return old


def _restore(path: Path, snapshot: str | None) -> None:
    """Undo a prior ``_snapshot_and_write`` — best-effort, swallows errors."""
    try:
        if snapshot is None:
            if path.exists():
                path.unlink()
        else:
            path.write_text(snapshot, encoding="utf-8")
    except Exception as e:
        print(f"⚠️  [personal_token] rollback of {path} failed: {e}")


@llm_bp.route('/personal_token/update', methods=['POST'])
def update_personal_token():
    """Strict all-or-nothing refresh of the current user's personal gnaigpt token.

    Flow (each step must succeed; any failure rolls back completed steps):
      1. Format check (Level 1: ``xxx.yyy.zzz`` base64url).
      2. Write ``<avatarfiles_dir>/user_keys/<login>.py``.
            3. Update the loaded ``keys.py`` ``gnaigpt_token_per_user`` map.
            4. Hot-swap the live LLM_helper by rebuilding its pool.

        The local cache and keys.py map are treated as one logical state: if either
        write fails, the other is restored to its prior content.
    """
    data = request.get_json(silent=True) or {}
    new_token = (data.get("token") or "").strip()

    if not _is_valid_jwt_shape(new_token):
        return jsonify({
            "ok": False,
            "reason": "invalid_format",
            "message": "Token format looks wrong — expected three base64url segments separated by dots.",
        }), 400

    login = _login()
    if not login:
        return jsonify({
            "ok": False,
            "reason": "no_login",
            "message": "Could not resolve the current Windows login.",
        }), 500

    avatarfiles_dir = getattr(app_config, "avatarfiles_dir", None)
    if not avatarfiles_dir:
        return jsonify({
            "ok": False,
            "reason": "no_avatarfiles_dir",
            "message": "Local IntelAvatar_files directory is not initialised yet.",
        }), 500

    filename = f"{login}.py"
    local_file = Path(avatarfiles_dir) / USER_KEY_LOCAL_SUBDIR / filename
    new_content = _personal_token_file_content(login, new_token)

    local_snapshot = None
    local_written = False
    keys_old_map = {}
    keys_updated = False

    try:
        try:
            local_snapshot = _snapshot_and_write(local_file, new_content)
            local_written = True
        except Exception as e:
            return jsonify({
                "ok": False,
                "reason": "local_write_failed",
                "message": f"Could not write local user_keys file: {e}",
            }), 500

        try:
            # Late import — avoids a circular import at blueprint load time,
            # and configs.set_up_app is already loaded by the time any HTTP
            # request reaches this route.
            from configs.set_up_app import configure_llm_personal_token
            key_module = getattr(app_config, "key", None)
            llm_helper = getattr(app_config, "llm_helper", None)
            if key_module is None or llm_helper is None:
                raise RuntimeError("LLM helper or key module not initialised")
            keys_old_map = _normalize_token_map(getattr(key_module, "gnaigpt_token_per_user", None) or {})
            keys_updated = True
            _update_keys_module_personal_token(key_module, login, new_token)
            configure_llm_personal_token(llm_helper, key_module, avatarfiles_dir)
            print(f"✅ [Hot-Swap] Token successfully refreshed and hot-swapped for '{login}'")

        except Exception as e:
            if keys_updated:
                _restore_keys_module_personal_token(key_module, keys_old_map)
            _restore(local_file, local_snapshot)
            return jsonify({
                "ok": False,
                "reason": "hot_swap_failed",
                "message": f"Files written but live token swap failed — rolled back. {e}",
            }), 500

        return jsonify({"ok": True})

    except Exception as e:
        # Defensive: any un-anticipated failure — best-effort rollback.
        if keys_updated:
            try:
                _restore_keys_module_personal_token(key_module, keys_old_map)
            except Exception:
                pass
        if local_written:
            _restore(local_file, local_snapshot)
        return jsonify({
            "ok": False,
            "reason": "internal_error",
            "message": str(e),
        }), 500
