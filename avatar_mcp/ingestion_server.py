"""
Report Ingestion Endpoint
=========================
The one MCP tool set that uses Streamable HTTP instead of stdio (see
data/adr/0002-report-ingestion-streamable-http-exception.md). Lets a test AI
agent running on a different machine push a BT report archive, trigger
headless analysis, and poll for the structured result - no browser, no Flask
session, no Socket.IO.

Three tools + one plain HTTP upload route:
  1. create_report_upload(filename, size_bytes)  -> {upload_id, upload_path}
  2. PUT <upload_path>                            (raw bytes, streamed to disk)
  3. start_report_analysis(upload_id)            -> {job_id}
  4. get_report_status(job_id)                   -> {status, result, error_message}

Auth: static per-caller API key (Authorization: Bearer <key>), matched via
`hmac.compare_digest` - see data/adr/0002 for why this is not full OAuth, and
why there's no TLS yet (trusted internal network).
"""

import base64
import hmac
import os
import shutil
import tempfile
import threading
import time
import uuid
from typing import Any, Optional

from mcp.server import MCPServer
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.provider import AccessToken, TokenVerifier
from mcp.server.auth.settings import AuthSettings
from starlette.requests import Request
from starlette.responses import Response

from avatar_mcp import ingestion_config as cfg
from services import email_notify_service
from services.headless_analysis_service import analyze_bt_report_headless, is_allowed_bt_input_filename


# ---------------------------------------------------------------------------
# Auth: static API key, wired into the SDK's OAuth-shaped token_verifier so
# every MCP tool call is checked automatically (see chat discussion in
# data/adr/0002 - AuthSettings.issuer_url/resource_server_url are unused
# placeholders here, not a real OAuth authorization server).
# ---------------------------------------------------------------------------
class StaticApiKeyVerifier(TokenVerifier):
    async def verify_token(self, token: str) -> Optional[AccessToken]:
        for caller_id, key in cfg.API_KEYS.items():
            if key and hmac.compare_digest(token, key):
                return AccessToken(token=token, client_id=caller_id, scopes=[])
        return None


def _check_bearer_auth(request: Request) -> Optional[str]:
    """Manual auth check for the custom /uploads route, which the SDK's
    token_verifier does NOT cover (custom_route() bypasses it by design).
    Returns the matched caller_id, or None if the token is missing/invalid.
    """
    auth = request.headers.get('authorization', '')
    if not auth.startswith('Bearer '):
        return None
    token = auth[len('Bearer '):].strip()
    for caller_id, key in cfg.API_KEYS.items():
        if key and hmac.compare_digest(token, key):
            return caller_id
    return None


def _caller_id() -> str:
    """Identify who is making the current @server.tool() call, for logging.
    Populated by the SDK's auth middleware from StaticApiKeyVerifier's
    AccessToken.client_id - see get_access_token()'s contextvar-based lookup.
    """
    token = get_access_token()
    return token.client_id if token else 'unknown'


server = MCPServer(
    'IntelAvatar Report Ingestion',
    token_verifier=StaticApiKeyVerifier(),
    auth=AuthSettings(
        issuer_url=cfg.PUBLIC_URL,
        resource_server_url=cfg.PUBLIC_URL,
    ),
)


# ---------------------------------------------------------------------------
# In-memory upload / job state. This process is the only writer, so plain
# dicts + locks are enough - no need for a real database.
# ---------------------------------------------------------------------------
_uploads: dict[str, dict] = {}
_uploads_lock = threading.Lock()
_jobs: dict[str, dict] = {}
_jobs_lock = threading.Lock()
_analysis_semaphore = threading.Semaphore(cfg.MAX_CONCURRENT_ANALYSES)


def _build_report_file(result: Optional[dict], filename: str, job_id: str) -> dict[str, str]:
    """Base64-encode a Markdown report for embedding directly in get_report_status's
    JSON, so callers get the human-readable report without a second HTTP round trip.
    """
    data = (result or {}).get('data') or {}
    markdown = data.get('markdown_summary')
    if not markdown:
        # Fallback for reports that skipped markdown_summary - mirrors the
        # summary building in services/feedback_service.py/history_service.py.
        lines = [f'# Analysis Report - {filename}', f'Job ID: `{job_id}`', '']
        if data.get('root_cause_summary'):
            lines.append(f"**Root Cause:** {data['root_cause_summary']}")
        if data.get('confidence_score') is not None:
            lines.append(f"**Confidence:** {data['confidence_score']}%")
        actions = data.get('recommended_actions') or []
        if actions:
            lines.append('\n**Recommended Actions:**')
            lines.extend(f'- {a}' for a in actions)
        markdown = '\n'.join(lines)

    stem = os.path.splitext(os.path.basename(filename))[0]
    timestamp = time.strftime('%Y%m%d_%H%M%S')
    return {
        'filename': f'report_{stem}_{timestamp}.md',
        'mime_type': 'text/markdown',
        'content_base64': base64.b64encode(markdown.encode('utf-8')).decode('ascii'),
    }


def _maybe_send_email(job_id: str, work_dir: str, filename: str,
                       status: str, result: Optional[dict], error_message: Optional[str]) -> None:
    try:
        email_notify_service.send_job_completion_email(
            extracted_dir=work_dir, job_id=job_id, source_filename=filename,
            status=status, result=result, error_message=error_message,
        )
    except Exception as exc:
        print(f'⚠️ [ingestion_server] email hook failed for job {job_id}: {exc}')


def _run_job(job_id: str) -> None:
    with _jobs_lock:
        job = _jobs[job_id]
        source_path = job['source_path']
        filename = job['filename']
        caller_id = job['caller_id']

    _analysis_semaphore.acquire()
    try:
        with _jobs_lock:
            _jobs[job_id]['status'] = 'running'

        work_dir = tempfile.mkdtemp(prefix='ingest_')
        # Short id/prefix on purpose - this dir + the extraction subfolder
        # inside it stack on top of the archive's own (often deep) internal
        # folder structure, and Windows' 260-char MAX_PATH bites fast.
        try:
            result = analyze_bt_report_headless(source_path, work_dir=work_dir)
            with _jobs_lock:
                _jobs[job_id]['status'] = 'done'
                _jobs[job_id]['result'] = result
                _jobs[job_id]['report_file'] = _build_report_file(result, filename, job_id)
                _jobs[job_id]['completed_at'] = time.time()
            print(f"✅ [{caller_id}] job {job_id} done ({filename})")
            _maybe_send_email(job_id, work_dir, filename, 'done', result, None)
        except Exception as exc:
            with _jobs_lock:
                _jobs[job_id]['status'] = 'error'
                _jobs[job_id]['error_message'] = str(exc)
                _jobs[job_id]['completed_at'] = time.time()
            print(f"❌ [{caller_id}] job {job_id} failed ({filename}): {exc}")
            _maybe_send_email(job_id, work_dir, filename, 'error', None, str(exc))
        finally:
            # Immediate cleanup, success or failure - see data/adr/0002.
            shutil.rmtree(work_dir, ignore_errors=True)
            try:
                os.remove(source_path)
            except OSError:
                pass
    finally:
        _analysis_semaphore.release()


def _ttl_sweep_loop() -> None:
    """Purge completed job records older than JOB_RESULT_TTL_SECONDS."""
    while True:
        time.sleep(300)
        cutoff = time.time() - cfg.JOB_RESULT_TTL_SECONDS
        with _jobs_lock:
            expired = [jid for jid, j in _jobs.items()
                       if j['completed_at'] is not None and j['completed_at'] < cutoff]
            for jid in expired:
                del _jobs[jid]


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------
@server.tool()
def create_report_upload(filename: str, size_bytes: int) -> dict[str, Any]:
    """Reserve an upload slot for a BT report archive.

    Call this first, PUT the raw file bytes to the returned upload_path,
    then call start_report_analysis with the returned upload_id.
    """
    if not is_allowed_bt_input_filename(filename):
        raise ValueError(f'Unsupported file type: {filename}')
    if size_bytes <= 0 or size_bytes > cfg.MAX_UPLOAD_BYTES:
        raise ValueError(f'size_bytes must be between 1 and {cfg.MAX_UPLOAD_BYTES}')

    caller_id = _caller_id()
    upload_id = uuid.uuid4().hex[:12]
    staging_dir = cfg.UPLOAD_STAGING_DIR or tempfile.gettempdir()
    os.makedirs(staging_dir, exist_ok=True)
    dest_path = os.path.join(staging_dir, f'{upload_id}_{os.path.basename(filename)}')

    with _uploads_lock:
        _uploads[upload_id] = {
            'dest_path': dest_path,
            'filename': filename,
            'expected_size': size_bytes,
            'complete': False,
            'caller_id': caller_id,
        }
    print(f"📥 [{caller_id}] create_report_upload({filename}, {size_bytes} bytes) -> upload_id={upload_id}")
    return {'upload_id': upload_id, 'upload_path': f'/uploads/{upload_id}'}


@server.custom_route('/uploads/{upload_id}', methods=['PUT'])
async def upload_report(request: Request) -> Response:
    caller_id = _check_bearer_auth(request)
    if caller_id is None:
        return Response(status_code=401)

    upload_id = request.path_params['upload_id']
    with _uploads_lock:
        record = _uploads.get(upload_id)
    if record is None:
        return Response(status_code=404)

    total = 0
    try:
        with open(record['dest_path'], 'wb') as fh:
            async for chunk in request.stream():
                total += len(chunk)
                if total > cfg.MAX_UPLOAD_BYTES:
                    raise ValueError('upload exceeds max_upload_bytes')
                fh.write(chunk)
    except Exception as exc:
        try:
            os.remove(record['dest_path'])
        except OSError:
            pass
        with _uploads_lock:
            _uploads.pop(upload_id, None)
        return Response(content=str(exc), status_code=400)

    with _uploads_lock:
        record['complete'] = True
        record['received_bytes'] = total
    print(f"📤 [{caller_id}] PUT /uploads/{upload_id} complete ({total} bytes)")
    return Response(status_code=200)


@server.tool()
def start_report_analysis(upload_id: str) -> dict[str, Any]:
    """Trigger headless BT analysis on a completed upload.

    Returns a job_id immediately; poll get_report_status(job_id) for the result.
    """
    with _uploads_lock:
        record = _uploads.get(upload_id)
    if record is None:
        raise ValueError(f'Unknown upload_id: {upload_id}')
    if not record.get('complete'):
        raise ValueError(f'Upload {upload_id} has not finished uploading yet.')

    caller_id = _caller_id()
    job_id = uuid.uuid4().hex[:12]
    with _jobs_lock:
        _jobs[job_id] = {
            'status': 'queued',
            'result': None,
            'error_message': None,
            'report_file': None,
            'created_at': time.time(),
            'completed_at': None,
            'source_path': record['dest_path'],
            'filename': record['filename'],
            'caller_id': caller_id,
        }
    with _uploads_lock:
        _uploads.pop(upload_id, None)

    print(f"▶️  [{caller_id}] start_report_analysis({upload_id}) -> job_id={job_id}")
    threading.Thread(target=_run_job, args=(job_id,), daemon=True).start()
    return {'job_id': job_id}


@server.tool()
def get_report_status(job_id: str) -> dict[str, Any]:
    """Poll the status of a previously-started analysis job.

    report_file (present only once status == 'done') is a base64-encoded
    Markdown report: {filename, mime_type, content_base64}.
    """
    with _jobs_lock:
        job = _jobs.get(job_id)
    if job is None:
        raise ValueError(f'Unknown job_id: {job_id}')
    print(f"🔍 [{_caller_id()}] get_report_status({job_id}) -> {job['status']}")
    return {
        'status': job['status'],
        'result': job['result'],
        'error_message': job['error_message'],
        'report_file': job['report_file'],
    }


def _init_avatar_app_config() -> None:
    """Minimal app_config bring-up - LLM client + a real-skills BtLogAgentSystem
    - so analyze_bt_report_headless() works. Mirrors the smoke-test block in
    services/headless_analysis_service.py; does NOT touch ChromeDriver, ACE,
    Snowflake, or any of the other heavy machinery configs/set_up_app.py wires
    up for the interactive browser app.
    """
    from configs.global_configs import app_config
    from configs.path_configs import KEY_PATH_prim, KEY_PATH_bkup, CLASSIFY_PATH
    from services.bt_chatbot_service import BtLogAgentSystem
    from services.llm_service import LLM_helper
    from services.log_chatbot_service import load_skills_from_yaml
    from utils import helpers
    from utils.bt_skills_yaml_utils import (
        current_active_yaml as bt_current_active_yaml,
        refresh_local_cloud_baseline as bt_refresh_local_cloud_baseline,
        set_active_source as bt_set_active_source,
    )

    key_path = helpers.get_load_path(KEY_PATH_prim, KEY_PATH_bkup)
    if key_path is None:
        raise RuntimeError('Could not resolve the key module path (KEY_PATH_prim/bkup unreachable).')
    key = helpers.load_module(key_path, 'key_moudle')

    llm_helper = LLM_helper()
    llm_helper.set_up(
        key.gnaigpt_token_r, key.gnaigpt_url, key.gnaigpt_model, CLASSIFY_PATH,
        token_pool=getattr(key, 'gnaigpt_tokens', None),
    )
    app_config.set_llm_helper(llm_helper)

    bt_skills = None
    bt_set_active_source('cloud')
    try:
        bt_refresh_local_cloud_baseline()
    except Exception as e:
        print(f'⚠️  BT cloud baseline refresh skipped: {e}')
    bt_chosen_yaml, _bt_chosen_date, bt_chosen_source = bt_current_active_yaml()
    if bt_chosen_yaml is not None and bt_chosen_yaml.exists():
        try:
            bt_skills = load_skills_from_yaml(str(bt_chosen_yaml))
            print(f'✅  {len(bt_skills)} BT skills loaded from {bt_chosen_source} YAML: {bt_chosen_yaml}')
        except Exception as e:
            print(f'⚠️  Failed to load BT skills from YAML ({e}); falling back to built-in skills.')
    else:
        print('ℹ️  No BT skills YAML found — falling back to built-in skills.')

    app_config.set_bt_chatbot_agent(
        BtLogAgentSystem(client=llm_helper.client, model=llm_helper.model, skills=bt_skills)
    )


if __name__ == '__main__':
    _init_avatar_app_config()
    threading.Thread(target=_ttl_sweep_loop, daemon=True).start()
    print(f'🚀 Report Ingestion Endpoint listening on {cfg.HOST}:{cfg.PORT}')
    server.run(transport='streamable-http', host=cfg.HOST, port=cfg.PORT)
