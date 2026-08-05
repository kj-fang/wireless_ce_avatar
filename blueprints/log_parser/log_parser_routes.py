from importlib.resources import path

from flask import Blueprint, render_template, request, session, redirect, url_for, flash, Response, jsonify, copy_current_request_context
import json
import os 
import datetime
import hmac
import logging
import re
import shutil
import stat
import threading
import time
import traceback
import uuid
import markdown
import bleach
from urllib.parse import unquote
from werkzeug.utils import secure_filename

from utils import helpers, attachment_decompose
from configs.global_configs import app_config
from configs.path_configs import LOG_PARSER_DIR, LOAD_PATH_prim, LOAD_PATH_bkup
from models.models import CaseContext
from utils.log_parser_preprocess import extract_all_keywords_from_filter_file

from services.log_parser_file_manage_service import FileManagerService
from services.log_parser_service import LogParserService
from services import gather_service
from services.etl_parser.wpp_ddd_parser import wpp_ddd_parser_run
from services.etl_parser.bt_parser import bt_decode_via_cli

log_parser_bp = Blueprint("log_parser", __name__, url_prefix="/log_parser")

log_parser_service = LogParserService()
file_manager_service = FileManagerService()

# ── One-time session pickup store for the SendTo background flow ─────────────
# The background thread cannot call Flask-Session's save_session reliably
# (flask-session 0.8.0 _ManagedSession is incompatible with direct save_session
# calls from Socket.IO event contexts).  Instead we snapshot dict(session) here,
# key it by a UUID token, and let before_app_request restore it on the next
# real HTTP request (the browser redirect).  The token travels as ?_st=<token>.
_sendto_session_store: dict = {}   # token -> (inserted_at, session_dict)
_sendto_session_lock  = threading.Lock()
_SENDTO_TOKEN_TTL     = 300  # seconds – tokens expire after 5 minutes

# ── Cooperative cancellation for /upload_local_analysis ──────────────────────
# Maps upload_id -> {'cancel_event': threading.Event}. The event is polled at
# stage boundaries inside _process_local_analysis. When cancel is signalled we
# also terminate known parser subprocesses (tracefmt.exe, 7z.exe, DDDPlayer.exe,
# ibtdrvlogparser.exe) spawned by this Python process, so blocking .wait() /
# subprocess.run() calls return promptly and the worker thread reaches its
# next checkpoint.
_active_local_uploads: dict = {}
_active_local_uploads_lock = threading.Lock()

# Executable names (lowercase) that /cancel_local_analysis is allowed to kill.
# Do NOT include chrome.exe or TextAnalysisTool.NET.exe — those are viewers
# owned by the app, not part of the parse.
_CANCELABLE_CHILD_NAMES = frozenset({
    '7z.exe', '7za.exe',
    'tracefmt.exe',
    'dddplayer.exe',
    'ibtdrvlogparser.exe',
})


class _LocalAnalysisCancelled(Exception):
    """Raised inside _process_local_analysis when the user cancels the upload."""


def _register_local_upload(upload_id: str) -> threading.Event:
    ev = threading.Event()
    with _active_local_uploads_lock:
        _active_local_uploads[upload_id] = {'cancel_event': ev}
    return ev


def _unregister_local_upload(upload_id: str) -> None:
    with _active_local_uploads_lock:
        _active_local_uploads.pop(upload_id, None)


def _signal_cancel_local_upload(upload_id: str) -> bool:
    with _active_local_uploads_lock:
        entry = _active_local_uploads.get(upload_id)
    if not entry:
        return False
    entry['cancel_event'].set()
    return True


def _wait_for_cancel_children_to_exit(timeout: float = 10.0) -> None:
    """Best-effort poll until no whitelisted parser subprocesses remain, so
    Windows releases file handles before we delete the extraction folder.
    Silently returns after `timeout` seconds regardless.
    """
    try:
        import psutil
    except ImportError:
        return
    try:
        self_proc = psutil.Process(os.getpid())
    except psutil.Error:
        return
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            still_alive = any(
                c.name().lower() in _CANCELABLE_CHILD_NAMES
                for c in self_proc.children(recursive=True)
            )
        except psutil.Error:
            return
        if not still_alive:
            return
        time.sleep(0.2)


def _force_rmtree(path: str, retries: int = 6, delay: float = 0.5) -> bool:
    """Aggressively delete a directory tree. Handles Windows extended-length
    paths, read-only bits, and files whose handles are only just being
    released. Returns True if the path is gone at the end. Never raises.

    Strategy per pass:
      1. shutil.rmtree with an onerror callback that clears the read-only bit.
      2. If anything is left, manually walk bottom-up and unlink each entry
         using the \\?\-prefixed path so deeply nested files past MAX_PATH
         still get removed.
    Retries the whole cycle a few times so late-releasing OS handles (AV
    scanners, Explorer previews, indexers) get another shot.
    """
    def _on_error(func, target, _exc_info):
        try:
            os.chmod(target, stat.S_IWRITE)
        except OSError:
            pass
        try:
            func(target)
        except OSError:
            pass

    def _manual_walk_delete(root: str) -> None:
        # Bottom-up walk using long-path form so deep trees are reachable.
        long_root = helpers.to_long_path(root)
        for dirpath, dirnames, filenames in os.walk(long_root, topdown=False):
            for name in filenames:
                fp = os.path.join(dirpath, name)
                try:
                    os.chmod(fp, stat.S_IWRITE)
                except OSError:
                    pass
                try:
                    os.remove(fp)
                except OSError:
                    pass
            for name in dirnames:
                dp = os.path.join(dirpath, name)
                try:
                    os.rmdir(dp)
                except OSError:
                    pass
        try:
            os.rmdir(long_root)
        except OSError:
            pass

    long_path = helpers.to_long_path(path)
    for _ in range(retries):
        if not os.path.exists(long_path):
            return True
        try:
            shutil.rmtree(long_path, onerror=_on_error)
        except OSError:
            pass
        if not os.path.exists(long_path):
            return True
        # rmtree left something behind — force a manual per-file pass.
        _manual_walk_delete(path)
        if not os.path.exists(long_path):
            return True
        time.sleep(delay)

    # Last-resort silent pass so we never leak an exception on stuck files.
    shutil.rmtree(long_path, ignore_errors=True)
    return not os.path.exists(long_path)


def _delete_cancel_cleanup_paths(cleanup_paths, upload_id: str) -> None:
    """Force-delete every path registered during a cancelled upload. Runs as
    the LAST step of the cancel flow, after subprocess kills have settled and
    the registry entry has been removed. Deletes the whole registered folder
    regardless of whether individual files inside were fully extracted.
    """
    for path in cleanup_paths:
        if not path:
            continue
        long_path = helpers.to_long_path(path)
        if not os.path.exists(long_path):
            continue
        try:
            if os.path.isdir(long_path):
                removed = _force_rmtree(path)
                if removed:
                    logging.info("[upload_local_analysis] force-removed %s on cancel (upload_id=%s)",
                                 path, upload_id)
                else:
                    logging.warning("[upload_local_analysis] could not fully remove %s after retries (upload_id=%s)",
                                    path, upload_id)
            else:
                try:
                    os.chmod(long_path, stat.S_IWRITE)
                except OSError:
                    pass
                os.remove(long_path)
                logging.info("[upload_local_analysis] removed file %s on cancel (upload_id=%s)",
                             path, upload_id)
        except OSError as e:
            logging.warning("[upload_local_analysis] failed to clean %s: %s", path, e)


def _raise_if_cancelled(cancel_event) -> None:
    if cancel_event is not None and cancel_event.is_set():
        raise _LocalAnalysisCancelled()


def _kill_local_analysis_children_until_done(upload_id: str) -> None:
    """Terminate whitelisted parser subprocesses spawned by this app until the
    worker thread for `upload_id` finishes (i.e. it removes itself from the
    registry). Runs in its own daemon thread so /cancel_local_analysis can
    return immediately.
    """
    try:
        import psutil
    except ImportError:
        logging.warning("psutil not available; cannot force-terminate parser children.")
        return

    try:
        self_proc = psutil.Process(os.getpid())
    except psutil.Error as e:
        logging.warning("Cannot open own process for child kill: %s", e)
        return

    deadline = time.time() + 60  # safety cap: stop polling after 60 s
    while time.time() < deadline:
        with _active_local_uploads_lock:
            still_active = upload_id in _active_local_uploads
        if not still_active:
            return
        try:
            children = self_proc.children(recursive=True)
        except psutil.Error:
            return
        for child in children:
            try:
                if child.name().lower() in _CANCELABLE_CHILD_NAMES:
                    logging.info("[cancel_local_analysis] terminating %s (PID=%s)",
                                 child.name(), child.pid)
                    child.terminate()
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        time.sleep(0.3)


@log_parser_bp.before_app_request
def _pickup_sendto_session():
    """Restore session data written by the SendTo background thread.

    Also purges expired tokens on every call so the store never grows
    unboundedly (e.g. user closes the tab before the redirect fires).
    """
    now = time.time()
    token = request.args.get('_st', '').strip()
    with _sendto_session_lock:
        # Purge expired entries regardless of whether a token was provided
        expired = [k for k, (ts, _) in _sendto_session_store.items()
                   if now - ts > _SENDTO_TOKEN_TTL]
        for k in expired:
            del _sendto_session_store[k]

        entry = _sendto_session_store.pop(token, None) if token else None

    if entry:
        _, data = entry
        session.update(data)

_SAFE_MARKDOWN_TAGS = [
    'p', 'br', 'strong', 'em', 'code', 'pre',
    'h1', 'h2', 'h3', 'h4', 'h5', 'h6',
    'ul', 'ol', 'li',
    'table', 'thead', 'tbody', 'tr', 'td', 'th',
    'blockquote', 'a'
]


def _render_safe_markdown_html(text: str) -> str:
    html = markdown.markdown(
        text,
        extensions=["fenced_code", "tables", "nl2br", "sane_lists", "codehilite"]
    )
    return bleach.clean(
        html,
        tags=_SAFE_MARKDOWN_TAGS,
        attributes={'a': ['href']},
        strip=True,
    )

#------------Section for Local dmp file upload bar -------------#
def _copy_file_with_console_progress(src_path: str, dst_path: str, chunk_size: int = 4 * 1024 * 1024, cancel_event=None) -> None:
    total_size = os.path.getsize(src_path)

    # Keep behavior predictable for empty files while still showing a completed upload line.
    if total_size == 0:
        open(dst_path, 'wb').close()
        shutil.copystat(src_path, dst_path)
        msg = f"Upload complete: {os.path.basename(src_path)} (0 B)"
        print(msg)
        app_config.socketio.emit('wpp_log', {'data': msg}, namespace='/progress')
        return

    copied = 0
    bar_width = 30
    percentage_per_block = 100 / bar_width
    upload_name = os.path.basename(src_path)
    last_emit_percent = -1

    cancelled = False
    try:
        with open(src_path, 'rb') as source, open(dst_path, 'wb') as destination:
            while True:
                if cancel_event is not None and cancel_event.is_set():
                    cancelled = True
                    break
                chunk = source.read(chunk_size)
                if not chunk:
                    break
                destination.write(chunk)
                copied += len(chunk)
                ratio = min(copied / total_size, 1.0)
                display_percent = round(ratio * 100, 2)
                filled = min(bar_width, int(display_percent / percentage_per_block))
                bar = '█' * filled + '░' * (bar_width - filled)

                current_percent = int(display_percent)
                console_msg = (
                    f"\rUploading {upload_name} [{bar}] {display_percent:6.2f}% "
                    f"({copied}/{total_size} bytes)"
                )
                print(console_msg, end='', flush=True)

                # Emit socket.io update every 1% to provide more frequent feedback
                if current_percent >= last_emit_percent + 1 or ratio >= 1.0:
                    formatted_size = _format_bytes(total_size)
                    formatted_copied = _format_bytes(copied)
                    socket_msg = f"Uploading {upload_name} [{bar}] {display_percent:6.2f}% ({formatted_copied}/{formatted_size})"
                    app_config.socketio.emit('wpp_log', {'data': socket_msg}, namespace='/progress')
                    last_emit_percent = current_percent
    finally:
        if cancelled:
            print()
            try:
                if os.path.exists(dst_path):
                    os.remove(dst_path)
            except OSError as e:
                logging.warning("Failed to remove partial file %s: %s", dst_path, e)

    if cancelled:
        raise _LocalAnalysisCancelled()

    print()
    completion_msg = f"Upload complete: {upload_name}"
    app_config.socketio.emit('wpp_log', {'data': completion_msg}, namespace='/progress')
    shutil.copystat(src_path, dst_path)


def _format_bytes(bytes_val: int) -> str:
    """Format bytes to human-readable format."""
    for unit in ['B', 'KB', 'MB', 'GB']:
        if bytes_val < 1024:
            return f"{bytes_val:.1f}{unit}"
        bytes_val /= 1024
    return f"{bytes_val:.1f}TB"


def _is_allowed_local_analysis_filename(filename: str) -> bool:
    clean_name = os.path.basename((filename or '').strip())
    lower_name = clean_name.lower()
    return (
        lower_name.endswith('.zip')
        or lower_name.endswith('.7z')
        or lower_name.endswith('.rar')
        or lower_name.endswith('.log')
        or lower_name.endswith('.hci.txt')
        or lower_name.endswith('.etl')
        or bool(re.fullmatch(r"dddLog_\d+\.bin", clean_name))
        or lower_name.endswith('.dmp')
        or bool(re.search(r'\.etl\.\d+$', clean_name, re.IGNORECASE))
    )

def _is_bt_etl(file_path: str) -> bool:
    """Return True if the file is a BT ETL that should be decoded via bt_decode_hci_via_folder."""
    name = os.path.basename(file_path).lower()
    return name.startswith(('ibtpci-', 'ibtusb-')) and name.endswith('.etl')

def _infer_local_upload_case_type(bt_files) -> str:
    if bt_files:
        return 'bt'
    return 'wifi'


def _process_local_analysis(source_path: str, source_dir: str, file_path: str,
                            original_name: str, timestamp: str,
                            is_bsod: bool = False,
                            progress_cb=None,
                            cancel_event=None,
                            cleanup_paths=None) -> str:
    """Shared core logic for local analysis (used by both upload and SendTo flows).

    Sets up session state, extracts archives / parses ETL / handles .log/.dmp files.
    Returns the redirect URL on success.
    Raises ValueError for validation failures (e.g. no supported files found).
    Raises _LocalAnalysisCancelled if cancel_event is set during processing.

    progress_cb: optional callable(pct: int, msg: str) – used by the SendTo flow
                 to stream progress to the browser.  Pass None for other callers.
    cancel_event: optional threading.Event polled at stage boundaries.
    cleanup_paths: optional list; any directories/files created during this
                   call that should be removed if the upload is cancelled are
                   appended here by the caller.
    """
    def _cb(pct: int, msg: str):
        if progress_cb:
            progress_cb(pct, msg)
            time.sleep(0.5)   # give the browser time to render each step

    def _track_cleanup(path: str) -> None:
        if cleanup_paths is not None and path:
            cleanup_paths.append(path)

    _raise_if_cancelled(cancel_event)

    session['download_path'] = source_dir
    session['uploaded_source_path'] = source_path
    session['local_in_place'] = True
    session['classification'] = {
        'issue_type': 'Unclassified',
        'confidence': 0,
        'keywords_found': []
    }

    if file_path.lower().endswith('.dmp') or is_bsod:
        local_case_nbr = f'local_bsod_{timestamp}'

        # For local BSOD uploads, copy the dump to shared storage first,
        # then submit analysis using that shared folder path.
        load_path_bsod = helpers.get_load_path(LOAD_PATH_prim, LOAD_PATH_bkup)
        if not load_path_bsod:
            raise ValueError('BSOD shared folder is unavailable. Please try again later.')

        shared_case_dir = os.path.join(load_path_bsod, local_case_nbr)
        os.makedirs(shared_case_dir, exist_ok=True)
        _track_cleanup(shared_case_dir)
        shared_dmp_path = os.path.join(shared_case_dir, original_name)
        _cb(20, 'Copying dump file to shared folder…')
        _copy_file_with_console_progress(file_path, shared_dmp_path, cancel_event=cancel_event)
        _cb(90, 'Copy complete. Redirecting to BSOD submission page…')

        # Ensure downstream BSOD page/API submission uses shared folder path.
        session['download_path'] = shared_case_dir
        
        # Build a minimal case context so BSOD submission page can render in local-upload mode.
        local_context = CaseContext(
            case_nbr=local_case_nbr,
            backend_id=local_case_nbr,
            wifi_or_bt='wifi',
            case_download_dir=shared_case_dir,
        )
        session['case_context'] = local_context.to_session()
        session['selected_files'] = [(original_name, original_name, None)]
        workflow_id = gather_service.new_workflow_id()
        session['gather_workflow_id'] = workflow_id
        gather_service.record_workflow_start(
            workflow_id=workflow_id, issue=local_context.to_dict(), domain='wifi',
            attachment_list=session['selected_files'],
        )
        gather_service.record_attachment_selection(
            workflow_id=workflow_id, selected_files=session['selected_files'],
            issue=local_context.to_dict(), domain='wifi',
        )
        try:
            local_bytes = os.path.getsize(shared_dmp_path)
        except OSError:
            local_bytes = None
        gather_service.record_attachment_download_result(
            workflow_id=workflow_id, name=original_name, status='already_exists',
            byte_count=local_bytes, latency_ms=0, attempt_count=0,
            issue=local_context.to_dict(), domain='wifi',
        )
        session['bsod'] = True
        session['latest_etl_llm'] = False
        session['latest_etl_path'] = None
        return url_for('main.download_result_bsod')

    elif file_path.lower().endswith('.zip') or file_path.lower().endswith('.7z') or file_path.lower().endswith('.rar'):
        print(f"📦 Extracting file: {file_path}")
        # process_single_zip creates <source_dir>/<stem_with_underscores>/;
        # only register it for cancel cleanup if it didn't already exist.
        _extract_folder_name = os.path.splitext(original_name)[0].replace(' ', '_')
        _extract_folder_path = os.path.join(source_dir, _extract_folder_name)
        if not os.path.exists(_extract_folder_path):
            _track_cleanup(_extract_folder_path)
        # CaseContext.to_session() writes a sidecar JSON in <source_dir>;
        # only delete it on cancel if it didn't exist before this run.
        _sidecar_path = os.path.join(source_dir, CaseContext._SESSION_SIDECAR_NAME)
        if not os.path.exists(_sidecar_path):
            _track_cleanup(_sidecar_path)
        _cb(20, 'Extracting archive contents…')

        # Stream per-file extraction progress (20 % → 60 %) when a progress_cb
        # is available (SendTo flow).  Throttle so we only emit when the integer
        # display value actually changes – avoids flooding the browser with
        # hundreds of events for large archives.
        _last_extract_pct = [20]
        def _extract_progress(raw_pct, filename):
            display_pct = 20 + int(raw_pct * 0.40)
            if display_pct != _last_extract_pct[0] and progress_cb:
                _last_extract_pct[0] = display_pct
                progress_cb(display_pct, f'Extracting: {filename}')

        wifi_files, ddd_files, evt_files, bt_files, fw_files = attachment_decompose.process_single_zip(
            file_path, source_dir, already_downloaded=False,
            progress_cb=_extract_progress if progress_cb else None,
            cancel_event=cancel_event,
        )

        extracted_files = wifi_files + ddd_files + evt_files + bt_files + fw_files
        if not extracted_files:
            raise ValueError('No supported analysis files found in the uploaded file.')

        total = len(extracted_files)
        _cb(60, f'Extraction complete. Found {total} file{"s" if total != 1 else ""}.')

        local_case_nbr = f'local_upload_{timestamp}'
        # Only consider bt_files for case type inference since wifi_files may be present in both wifi and bt cases
        local_case_type = _infer_local_upload_case_type(bt_files)

        local_context = CaseContext(
            case_nbr=local_case_nbr,
            wifi_or_bt=local_case_type,
            case_download_dir=source_dir
        )
        session['case_context'] = local_context.to_session()
        session['selected_files'] = []
        workflow_id = gather_service.new_workflow_id()
        session['gather_workflow_id'] = workflow_id
        local_attachment = [(original_name, original_name, None)]
        gather_service.record_workflow_start(
            workflow_id=workflow_id, issue=local_context.to_dict(),
            domain=local_case_type, attachment_list=local_attachment,
        )
        gather_service.record_attachment_selection(
            workflow_id=workflow_id, selected_files=local_attachment,
            issue=local_context.to_dict(), domain=local_case_type,
        )
        try:
            local_bytes = os.path.getsize(file_path)
        except OSError:
            local_bytes = None
        gather_service.record_attachment_download_result(
            workflow_id=workflow_id, name=original_name, status='already_exists',
            byte_count=local_bytes, latency_ms=0, attempt_count=0,
            issue=local_context.to_dict(), domain=local_case_type,
        )
        session['bsod'] = False
        session['latest_etl_llm'] = False

        app_config.set_download_results(
            local_case_nbr,
            wifi={original_name: wifi_files},
            ddd={original_name: ddd_files + evt_files},
            bt={original_name: bt_files},
            fw={original_name: fw_files}
        )
        _cb(90, 'File categories organised. Ready to analyse.')

        return url_for('main.download_result')

    elif file_path.lower().endswith('.log'):
        session['latest_etl_path'] = None
        app_config.last_analyzed_log_path = file_path
        _cb(90, 'Log file ready.')
        return url_for('log_chatbot.index', auto_run='analyze_all')

    elif file_path.lower().endswith('.hci.txt'):
        # Treat .hci.txt from BT HCI decode as a decoded BT log
        session['latest_etl_path'] = file_path
        app_config.last_analyzed_log_path = file_path
        _cb(90, 'BT HCI log file ready.')
        return url_for('bt_chatbot.index', auto_run='analyze_all')

    elif _is_bt_etl(file_path):
        _cb(20, 'Launching BT HCI decoder…')
        _cb(30, 'Decoding in progress (may take ~30 s)…')
        hci_path = bt_decode_via_cli(source_dir, file_path)
        _raise_if_cancelled(cancel_event)
        if not hci_path:
            raise ValueError(f'BT HCI decode failed or timed out for: {original_name}')
        _cb(90, 'BT HCI decode complete.')
        session['latest_etl_path'] = hci_path
        app_config.last_analyzed_log_path = hci_path
        return url_for('bt_chatbot.index', auto_run='analyze_all')

    else:
        _cb(20, 'Starting WPP/DDD parser…')
        wpp_ddd_parser_run(file_path)
        _raise_if_cancelled(cancel_event)
        _cb(90, 'Parser complete.')
        session['latest_etl_path'] = file_path
        app_config.last_analyzed_log_path = file_path + '.log'
        return url_for('log_chatbot.index', auto_run='analyze_all')


@log_parser_bp.route('/log_parser', methods=['POST', 'GET'])
def log_parser():
    return render_log_parser_form()

@log_parser_bp.route("/load_prompt", methods=["POST"])
def load_prompt():
    data = request.get_json()
    prompt_type = data.get('type')
    prompt_file = data.get('file')
    
    content = log_parser_service.load_prompt_content(prompt_type, prompt_file)
    return jsonify({'content': content})

@log_parser_bp.route("/upload", methods=["POST"])
def upload():
    # Reset local_in_place when using regular file upload (not local analysis)
    session['local_in_place'] = False
    upload_type = request.form.get('type')
    result = file_manager_service.handle_file_upload(upload_type, request.files)
    return result


@log_parser_bp.route('/pick_local_analysis_file', methods=['POST'])
def pick_local_analysis_file():
    root = None
    try:
        try:
            import tkinter as tk
            from tkinter import filedialog
        except ImportError as e:
            logging.exception('Tkinter is unavailable in this environment: %s', e)
            return jsonify({'success': False, 'message': 'Native file picker is unavailable in this environment.'}), 503

        root = tk.Tk()
        root.withdraw()
        root.attributes('-topmost', True)
        selected_path = filedialog.askopenfilename(
            title='Select local analysis file',
            filetypes=[
                ('Supported files', '*.zip *.7z *.rar *.log *.hci.txt *.etl *.etl.* dddLog_*.bin *.dmp'),
                ('All files', '*.*'),
            ],
        )
        root.destroy()

        if not selected_path:
            return jsonify({'success': False, 'message': 'No file selected'}), 400

        selected_name = os.path.basename(selected_path)
        if not _is_allowed_local_analysis_filename(selected_name):
            return jsonify({
                'success': False,
                'message': f'Invalid file type: {selected_name}. Only .zip, .7z, .rar, .etl, ddd, .hci.txt, .log, or .dmp are allowed.'
            }), 400

        normalized_selected_path = os.path.normpath(os.path.abspath(selected_path))
        session.clear()
        session['picked_local_analysis_path'] = normalized_selected_path

        return jsonify({
            'success': True,
            'source_path': normalized_selected_path,
            'filename': selected_name,
        })
    except Exception as e:
        logging.exception('Failed to open native file dialog: %s', e)
        return jsonify({'success': False, 'message': f'Native file dialog unavailable: {str(e)}'}), 500
    finally:
        if root is not None:
            try:
                root.destroy()
            except Exception:
                logging.debug('Failed to destroy Tk root window cleanly', exc_info=True)


#------------ Section for Local Analysis file uploaded -------------#

@log_parser_bp.route('/upload_local_analysis', methods=['POST'])
def upload_local_analysis():
    source_path = (request.form.get('source_path') or '').strip()
    picked_source_path = (session.get('picked_local_analysis_path') or '').strip()

    if not source_path:
        return jsonify({'success': False, 'message': 'Please use native picker to select a local file path.'}), 400

    if not picked_source_path:
        return jsonify({'success': False, 'message': 'No path is bound to this session. Please re-pick the file using native picker.'}), 400

    normalized_source_path = os.path.normpath(os.path.abspath(source_path))
    normalized_picked_source_path = os.path.normpath(os.path.abspath(picked_source_path))
    if os.path.normcase(normalized_source_path) != os.path.normcase(normalized_picked_source_path):
        return jsonify({'success': False, 'message': 'Invalid source path for this session. Please re-pick the file using native picker.'}), 400

    source_path = normalized_picked_source_path

    if not os.path.exists(source_path):
        return jsonify({'success': False, 'message': f'Source file not found: {source_path}'}), 400

    original_name = os.path.basename(source_path)
    print(f"[upload_local_analysis] source_path: {source_path}")
    logging.info("[upload_local_analysis] source_path: %s", source_path)

    if not _is_allowed_local_analysis_filename(original_name):
        return jsonify({
            'success': False,
            'message': f'Invalid file type: {original_name}. Only .zip, .7z, .rar, .etl, ddd, .hci.txt, .log, or .dmp are allowed.'
        }), 400

    timestamp = datetime.datetime.now().strftime('%Y%m%d_%H%M%S')
    source_dir = os.path.dirname(source_path) or os.getcwd()
    file_path = source_path

    # Keys mutated by _process_local_analysis that must be cleared on cancellation
    _cancelable_session_keys = (
        'download_path', 'uploaded_source_path', 'local_in_place',
        'classification', 'case_context', 'selected_files', 'bsod',
        'latest_etl_llm', 'latest_etl_path', 'gather_workflow_id',
    )

    upload_id = (request.form.get('upload_id') or '').strip()
    cancel_event = _register_local_upload(upload_id) if upload_id else None
    cleanup_paths: list = []

    def _cancelled_response():
        for k in _cancelable_session_keys:
            session.pop(k, None)
        logging.info("[upload_local_analysis] cancelled by user (upload_id=%s)", upload_id)
        return jsonify({
            'success': False,
            'cancelled': True,
            'message': 'Upload cancelled by user.'
        }), 200

    try:
        try:
            redirect_url = _process_local_analysis(
                source_path, source_dir, file_path, original_name, timestamp,
                is_bsod=request.form.get('is_bsod') == 'true',
                cancel_event=cancel_event,
                cleanup_paths=cleanup_paths,
            )
        except (_LocalAnalysisCancelled, attachment_decompose.ExtractionCancelled):
            return _cancelled_response()
        except SystemExit:
            # Parser helpers call sys.exit(0) on failure. If the failure was
            # caused by our forced subprocess kill during cancel, treat it as
            # cancelled; otherwise let it propagate.
            if cancel_event is not None and cancel_event.is_set():
                return _cancelled_response()
            raise
        except ValueError as e:
            if cancel_event is not None and cancel_event.is_set():
                return _cancelled_response()
            return jsonify({'success': False, 'message': str(e)}), 400
        except Exception as e:
            if cancel_event is not None and cancel_event.is_set():
                return _cancelled_response()
            logging.exception("Failed local analysis flow for %s: %s", file_path, e)
            return jsonify({'success': False, 'message': f'Failed local analysis flow: {str(e)}'}), 500
    finally:
        # Keep the upload_id registered until after cancel cleanup completes so
        # the cancel thread can continue terminating child processes while we wait.
        if cleanup_paths and cancel_event is not None and cancel_event.is_set():
            _wait_for_cancel_children_to_exit()
            _delete_cancel_cleanup_paths(cleanup_paths, upload_id)
        if upload_id:
            _unregister_local_upload(upload_id)

    resp = {
        'success': True,
        'uploaded_source_path': source_path,
        'redirect': redirect_url,
    }
    # Signal the client to use the chatbot flow instead of the log-parser redirect.
    # Use the extended-length path so prepare/set_log don't fail on long paths.
    source_lower = source_path.lower()
    if source_lower.endswith('.log'):
        resp['use_chatbot'] = True
        resp['log_path'] = helpers.to_long_path(source_path)
    elif source_lower.endswith('.hci.txt'):                        # .hci.txt / BT decoded log
        resp['use_chatbot'] = True
        resp['is_bt'] = True
        resp['log_path'] = helpers.to_long_path(source_path)
    elif _is_bt_etl(source_path):                              # BT .etl go log_path
        resp['use_chatbot'] = True
        resp['is_bt'] = True
        resp['log_path'] = helpers.to_long_path(source_path + '.hci.txt')
    elif source_lower.endswith('.etl') or bool(re.search(r'\.etl\.\d+$', source_lower)) or bool(re.fullmatch(r"dddLog_\d+\.bin", os.path.basename(source_path))):  # Wi-Fi .etl(.N) or DDD dddLog_<n>.bin
        resp['use_chatbot'] = True
        resp['etl_path'] = helpers.to_long_path(source_path)
    return jsonify(resp)


@log_parser_bp.route('/cancel_local_analysis', methods=['POST'])
def cancel_local_analysis():
    """Signal a running /upload_local_analysis worker to abort. Sets the cancel
    event AND terminates known parser child processes (tracefmt.exe, 7z.exe,
    DDDPlayer.exe, ibtdrvlogparser.exe) so blocking subprocess calls return
    promptly. Returns was_active=False if upload_id is unknown.
    """
    # Match other local-only endpoints in this blueprint.
    if request.remote_addr not in ('127.0.0.1', '::1'):
        return jsonify({'success': False, 'message': 'Access denied: localhost only'}), 403
    data = request.get_json(silent=True) or {}
    upload_id = (data.get('upload_id') or request.form.get('upload_id') or '').strip()
    if not upload_id:
        return jsonify({'success': False, 'message': 'upload_id is required'}), 400
    
    was_active = _signal_cancel_local_upload(upload_id)
    start_killer = False
    with _active_local_uploads_lock:
        entry = _active_local_uploads.get(upload_id)
        if entry and not entry.get('killer_started'):
            entry['killer_started'] = True
            start_killer = True
    logging.info("[cancel_local_analysis] upload_id=%s was_active=%s", upload_id, was_active)
    if start_killer:
        threading.Thread(
            target=_kill_local_analysis_children_until_done,
            args=(upload_id,),
            name=f'cancel-local-upload-{upload_id[:8]}',
            daemon=True,
        ).start()
    return jsonify({'success': True, 'was_active': was_active})


#------------ Section for SendTo file -------------#

@log_parser_bp.route('/open_local_analysis', methods=['GET'])
def open_local_analysis():
    # Restrict to localhost only
    if request.remote_addr not in ('127.0.0.1', '::1'):
        flash('Access denied: this endpoint is only available from localhost.', 'danger')
        return redirect(url_for('main.index'))

    # Validate SendTo token
    provided_token = (request.args.get('token') or '').strip()
    expected_token = app_config.sendto_token
    if not provided_token or not hmac.compare_digest(provided_token, expected_token):
        flash('Invalid or expired SendTo token. Please restart the application.', 'danger')
        return redirect(url_for('main.index'))

    source_path = (request.args.get('path') or '').strip()
    if not source_path:
        flash('No local analysis file path was provided.', 'danger')
        return redirect(url_for('main.index'))

    source_path = os.path.abspath(source_path)
    original_name = os.path.basename(source_path)

    if not os.path.exists(source_path):
        flash(f'Local analysis file not found: {source_path}', 'danger')
        return redirect(url_for('main.index'))

    if not _is_allowed_local_analysis_filename(original_name):
        flash(f'Invalid file type: {original_name}. Only .zip, .7z, .rar, .etl, ddd, .hci.txt, .log, or .dmp are allowed.', 'danger')
        return redirect(url_for('main.index'))

    # Store validated path in session; actual processing starts after the
    # browser connects to the /sendto-progress Socket.IO namespace.
    session['sendto_pending_path'] = source_path

    return render_template('sendto_transmission.html', filename=original_name)


@log_parser_bp.route('/navigate_existing_browser', methods=['POST'])
def navigate_existing_browser():
    if request.remote_addr not in ('127.0.0.1', '::1'):
        return jsonify({'success': False, 'message': 'Access denied: localhost only'}), 403

    data = request.get_json(silent=True) or {}
    startup_path = data.get('startup_path', '/')

    # Validate startup_path is a safe in-app relative path
    if not isinstance(startup_path, str) or not startup_path.startswith('/') \
            or startup_path.startswith('//') or '://' in startup_path:
        return jsonify({'success': False, 'message': 'Invalid startup path'}), 400

    driver_manager = app_config.driver_manager
    if driver_manager is None:
        return jsonify({'success': False, 'message': 'Driver manager is not available'}), 409

    # Use a fixed localhost origin instead of request.host_url to prevent Host header manipulation
    port = request.environ.get('SERVER_PORT', '5000')
    base_url = f'http://127.0.0.1:{port}/'

    try:
        driver_manager.navigate_main_browser(base_url, startup_path)
    except Exception as error:
        logging.exception('Failed to navigate existing browser: %s', error)
        return jsonify({'success': False, 'message': str(error)}), 409

    return jsonify({'success': True})


@log_parser_bp.route('/verify_sendto_token', methods=['GET'])
def verify_sendto_token():
    """Test endpoint to verify whether a given SendTo token matches the current app token."""
    if request.remote_addr not in ('127.0.0.1', '::1'):
        return jsonify({'success': False, 'message': 'Access denied: localhost only'}), 403

    provided_token = (request.args.get('token') or '').strip()
    expected_token = app_config.sendto_token
    is_match = bool(provided_token) and hmac.compare_digest(provided_token, expected_token)

    return jsonify({
        'success': True,
        'token_match': is_match,
        'provided_token_preview': provided_token[:8] + '...' if len(provided_token) > 8 else provided_token,
        'expected_token_preview': expected_token[:8] + '...',
    })


@log_parser_bp.route("/get_filter_details", methods=["POST"])
def get_filter_details():
    """Return all keywords from a single .tat file with their enabled status."""
    data = request.get_json()
    filter_file = data.get('filter_file', '')
    if not filter_file:
        return jsonify({'success': False, 'message': 'No filter file specified'})
    
    filter_path = os.path.join(LOG_PARSER_DIR, "filter", filter_file)
    if not os.path.exists(filter_path):
        return jsonify({'success': False, 'message': f'Filter file not found: {filter_file}'})
    
    try:
        keywords = extract_all_keywords_from_filter_file(filter_path)
        return jsonify({'success': True, 'keywords': keywords})
    except Exception as e:
        return jsonify({'success': False, 'message': f'Failed to read filter: {str(e)}'})


@log_parser_bp.route("/get_all_filter_details", methods=["POST"])
def get_all_filter_details():
    """Return ALL .tat files with ALL their keywords and enabled status."""
    filter_dir = os.path.join(LOG_PARSER_DIR, "filter")
    if not os.path.exists(filter_dir):
        return jsonify({'success': False, 'message': 'Filter directory not found'})
    
    try:
        tat_files = sorted([f for f in os.listdir(filter_dir) if f.endswith('.tat')])
        all_filters = []
        for tat_file in tat_files:
            tat_path = os.path.join(filter_dir, tat_file)
            keywords = extract_all_keywords_from_filter_file(tat_path)
            all_filters.append({
                'file': tat_file,
                'keywords': keywords
            })
        return jsonify({'success': True, 'filters': all_filters})
    except Exception as e:
        return jsonify({'success': False, 'message': f'Failed to load filters: {str(e)}'})


@log_parser_bp.route("/edit_prompt", methods=["POST"])
def edit_prompt():
    data = request.get_json()
    action = data.get('action')
    filename = data.get('filename')
    content = data.get('content')
    
    if not action:
        return jsonify({'success': False, 'message': 'Action is required'})
    
    result = file_manager_service.handle_prompt_operation(action, filename, content)
    print("edit_prompt", result, type(result))
    return result


@log_parser_bp.route("/estimate_tokens", methods=["POST"])
def estimate_tokens():
    data = request.get_json()
    selected_filter = (data.get('filter_file') or '').strip()
    if not selected_filter:
        return jsonify({'success': False, 'message': 'No filter file specified'}), 400

    log_path = session.get('log_path', '')
    if not log_path or not os.path.exists(log_path):
        return jsonify({'success': False, 'message': 'Log file not found in session'}), 400

    filter_path = os.path.join(LOG_PARSER_DIR, "filter", os.path.basename(selected_filter))
    if not os.path.exists(filter_path):
        return jsonify({'success': False, 'message': f'Filter file not found: {selected_filter}'}), 400

    try:
        from utils.log_parser_preprocess import (
            extract_enabled_keywords_from_filter_file,
            filter_log_by_keywords, preprocess_log_for_llm, group_similar_logs
        )
        from utils import helpers as _helpers

        log_lines = _helpers.read_log_file(log_path)
        keywords = extract_enabled_keywords_from_filter_file(filter_path)
        filtered = filter_log_by_keywords(log_lines, keywords)
        processed = preprocess_log_for_llm(filtered)
        grouped = group_similar_logs(processed)

        TOKEN_LIMIT = 20_000
        filtered_tokens = log_parser_service._estimate_tokens("\n".join(filtered))
        grouped_tokens = log_parser_service._estimate_tokens("\n".join(grouped))

        return jsonify({
            'success': True,
            'filtered_tokens': filtered_tokens,
            'grouped_tokens': grouped_tokens,
            'token_limit': TOKEN_LIMIT,
            'exceeds_limit': grouped_tokens > TOKEN_LIMIT,
            'keyword_count': len(keywords),
            'filtered_lines': len(filtered),
        })
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)}), 500


def register_socketio_handlers(socketio):
    @socketio.on('submit_analysis', namespace='/progress')
    def socketio_submit_analysis(data):
        print("✅ Received socket event 'submit_analysis':", data)
        return handle_submit_analysis(data, socketio)

    @socketio.on('chat_message', namespace='/progress')
    def socketio_chat_message(data):
        print("💬 Received chat_message:", data)
        return handle_chat_message(data, socketio)

    @socketio.on('chat_message_with_filter', namespace='/progress')
    def socketio_chat_message_with_filter(data):
        print("💬 Received chat_message_with_filter:", data)
        return handle_chat_message_with_filter(data, socketio)

    @socketio.on('reset_log_parser_session', namespace='/progress')
    def socketio_reset_log_parser_session():
        print("♻️ Received reset_log_parser_session")
        return handle_reset_log_parser_session(request.sid, socketio)

    # ── /sendto-progress namespace ─────────────────────────────────────────
    @socketio.on('start_sendto', namespace='/sendto-progress')
    def socketio_start_sendto():
        client_sid = request.sid
        source_path = (session.get('sendto_pending_path') or '').strip()
        print(f"[sendto] start_sendto received, sid={client_sid}, path={source_path}")

        if not source_path:
            socketio.emit('sendto_error',
                          {'message': 'No pending file in session. Please use Send To again.'},
                          namespace='/sendto-progress', to=client_sid)
            return

        if not os.path.exists(source_path):
            socketio.emit('sendto_error',
                          {'message': f'File no longer exists: {source_path}'},
                          namespace='/sendto-progress', to=client_sid)
            return

        # Clear the pending path so a page-refresh doesn't re-trigger processing
        session.pop('sendto_pending_path', None)

        # copy_current_request_context copies the Flask request/session context
        # into the background thread so that session reads/writes work correctly.
        @copy_current_request_context
        def _run_with_context():
            _run_sendto_in_background(socketio, client_sid, source_path)

        t = threading.Thread(target=_run_with_context, daemon=True)
        t.start()


# ── SendTo background worker ────────────────────────────────────────────────

def _emit_sendto(socketio, sid, event, data):
    """Convenience wrapper: emit to a specific client in /sendto-progress."""
    socketio.emit(event, data, namespace='/sendto-progress', to=sid)


def _run_sendto_in_background(socketio, client_sid, source_path: str):
    """Run _process_local_analysis in a background thread and stream progress
    to the browser via Socket.IO namespace /sendto-progress.

    Progress events emitted:
        sendto_progress  { pct: int, msg: str, detail: str|None }
        sendto_wpp_log   { data: str }   (forwarded from wpp_ddd_parser_run)
        sendto_complete  { redirect_url: str }
        sendto_error     { message: str }
    """
    def emit_progress(pct: int, msg: str, detail: str = None):
        payload = {'pct': pct, 'msg': msg}
        if detail:
            payload['detail'] = detail
        _emit_sendto(socketio, client_sid, 'sendto_progress', payload)
        print(f"[sendto] {pct}% – {msg}")

    try:
        original_name = os.path.basename(source_path)
        source_dir    = os.path.dirname(source_path) or os.getcwd()
        timestamp     = datetime.datetime.now().strftime('%Y%m%d_%H%M%S')
        file_lower    = source_path.lower()

        emit_progress(5, f'File validated: {original_name}')
        time.sleep(0.5)

        # ── Determine file type and emit appropriate pre-step message ────────
        if file_lower.endswith('.dmp'):
            emit_progress(10, 'BSOD dump detected. Copying to shared folder…')
        elif file_lower.endswith(('.zip', '.7z', '.rar')):
            emit_progress(10, 'Archive detected. Extracting…')
        elif file_lower.endswith('.log'):
            emit_progress(10, 'Log file detected. Preparing chatbot…')
        elif file_lower.endswith('.hci.txt'):
            emit_progress(10, 'Bluetooth log detected. Preparing Bluetooth chatbot…')
        elif _is_bt_etl(source_path):
            emit_progress(10, 'Bluetooth ETL detected. Starting HCI decode…')
        else:
            emit_progress(10, 'Wi-Fi ETL file detected. Starting WPP/DDD parser…')
        time.sleep(0.5)
        # ── Intercept wpp_log events and forward to /sendto-progress ─────────
        # Temporarily monkey-patch the socketio emit for wpp_log so the
        # detail log on the waiting page also shows ETL sub-step output.
        _orig_emit = socketio.emit

        def _forwarding_emit(event, data=None, **kwargs):
            _orig_emit(event, data, **kwargs)
            if event == 'wpp_log' and isinstance(data, dict):
                _emit_sendto(socketio, client_sid, 'sendto_wpp_log', data)

        socketio.emit = _forwarding_emit

        try:
            emit_progress(15, 'Processing file…')
            time.sleep(0.5)
            redirect_url = _process_local_analysis(
                source_path, source_dir, source_path, original_name, timestamp,
                progress_cb=emit_progress,
            )
        finally:
            socketio.emit = _orig_emit   # always restore original emit

        # Flask-Session (filesystem) save_session is unreliable from a background
        # thread (flask-session 0.8.0 _ManagedSession has no 'sid' in Socket.IO
        # event contexts).  Instead, snapshot the session into a module-level
        # store and let the next real HTTP request (the browser redirect) pick it
        # up via the before_app_request hook _pickup_sendto_session.
        token = uuid.uuid4().hex
        with _sendto_session_lock:
            _sendto_session_store[token] = (time.time(), dict(session))
        sep = '&' if '?' in redirect_url else '?'
        redirect_url = redirect_url + sep + '_st=' + token

        emit_progress(100, 'Processing complete!')
        time.sleep(0.5)
        _emit_sendto(socketio, client_sid, 'sendto_complete', {'redirect_url': redirect_url})

    except ValueError as e:
        logging.warning('[sendto] Validation error: %s', e)
        _emit_sendto(socketio, client_sid, 'sendto_error', {'message': str(e)})
    except Exception as e:
        logging.exception('[sendto] Unexpected error for %s: %s', source_path, e)
        _emit_sendto(socketio, client_sid, 'sendto_error', {'message': f'Processing failed: {e}'})


#------------Llog parser render -------------#

def render_log_parser_form():
    # Validate local_in_place context: only honor this flag if we have an active uploaded_source_path
    # from a local analysis flow. This prevents stale session flags from affecting new/other flows.
    if session.get('local_in_place') and not session.get('uploaded_source_path'):
        session['local_in_place'] = False
    
    classification = session.get('classification', {
        'issue_type': 'Unclassified',
        'confidence': 0,
        'keywords_found': []
    })
    print("session['classification'] ", classification)
    print("classification.keys()", classification.keys())

    download_path = session.get('download_path', app_config.avatarfiles_dir or os.getcwd())
    if session.get('local_in_place'):
        output_dir = download_path
    else:
        output_dir = log_parser_service.set_up(download_path)
    session['logparser_output_dir'] = output_dir


    should_auto_analyze, auto_analysis_data = log_parser_service.check_auto_analysis_availability(classification)

    # BT LLM flow: caller may supply explicit filter/prompt to override auto-detection
    preselect_filter = request.args.get('preselect_filter', '').strip()
    preselect_prompt = request.args.get('preselect_prompt', '').strip()
    if preselect_filter and preselect_prompt:
        should_auto_analyze = True
        auto_analysis_data = {
            'filter_file': preselect_filter,
            'prompt_file': preselect_prompt,
            'issue_type': 'bt_hci',
        }

    latest_etl_path = request.args.get('latest_etl_path', None) or session.get('latest_etl_path', None)
    if latest_etl_path:
        etl_path_input = latest_etl_path
    else:
        etl_path_encoded = request.args.get('etl_path', '')
        etl_path_input = unquote(etl_path_encoded)

    # Accept .log and .txt (incl. .hci.txt from BT HCI decode) as direct log files
    _is_direct_log = (
        etl_path_input
        and os.path.exists(etl_path_input)
        and (etl_path_input.lower().endswith('.log') or etl_path_input.lower().endswith('.txt'))
    )
    
    if session.get('local_in_place') and etl_path_input:
        # In-place mode: output is already in source dir, no copy needed.
        candidate = etl_path_input if etl_path_input.lower().endswith('.log') else etl_path_input + '.log'
        log_path = candidate if os.path.exists(candidate) else None
    elif _is_direct_log:
        log_path = os.path.join(output_dir, os.path.basename(etl_path_input))
        shutil.copy2(etl_path_input, log_path)
    else:
        log_path = log_parser_service.prepare_log_file(etl_path_input, output_dir)
    if log_path:
        session['log_path'] = log_path

    available_filters, (available_prompts, available_custom_prompts) = log_parser_service.get_available_resources()

    result = log_parser_service.analysis_result
    return render_template('log_parser.html', 
                          classification=json.dumps(classification),
                          should_auto_analyze=should_auto_analyze,
                          auto_analysis_data=auto_analysis_data if auto_analysis_data else '{}',
                          llm_result_html=result.get('llm_result_html'), 
                          log_output_path=result.get('log_output_path'), 
                          available_filters=available_filters,
                          available_prompts=available_prompts,
                          available_custom_prompts=available_custom_prompts,
                          log_path=log_path)


def handle_submit_analysis(data, socketio=None):
    print("Received analysis submission:", data)
    
    log_path = session.get('log_path', '')
    output_dir = session.get('logparser_output_dir', '')
    selected_filter = data.get('filter_file')
    custom_prompt_content = data.get('prompt_content')
    
    print(f"📋 Validation data:")
    print(f"   - log_path: {log_path}")
    print(f"   - output_dir: {output_dir}")
    print(f"   - filter: {selected_filter}")
    print(f"   - prompt length: {len(custom_prompt_content) if custom_prompt_content else 0}")
    
    is_valid, error_message = log_parser_service.validate_analysis_inputs(
        log_path, selected_filter, custom_prompt_content
    )
    
    if not is_valid:
        print(f"❌ Validation failed: {error_message}")
        if socketio:
            socketio.emit('validation_error', {'message': error_message}, namespace='/progress')
        else:
            app_config.socketio.emit('validation_error', {'message': error_message})
        return
    
    # Check if user sent custom keyword selections
    custom_keywords = data.get('custom_keywords', None)
    
    try:
        filter_path = os.path.join(LOG_PARSER_DIR, "filter", selected_filter)
        print(f"📂 Filter path: {filter_path}")
        if custom_keywords is not None:
            print(f"🔧 Using {len(custom_keywords)} user-selected keywords")
        print(f"📖 Starting analysis...")
        
        # Get case description from session
        case_context_dict = session.get('case_context', {})
        case_description = case_context_dict.get('description', '')
        
        success = log_parser_service.start_analysis(
            filter_path, log_path, session['logparser_output_dir'], 
            app_config.llm_helper, custom_prompt_content, case_description
        )
        
        if not success:
            print("❌ Analysis failed to start")
            if socketio:
                socketio.emit('analysis_error', {'message': 'Failed to start analysis'}, namespace='/progress')
            else:
                app_config.socketio.emit('analysis_error', {'message': 'Failed to start analysis'})
        
    except Exception as e:
        print(f"❌ Exception during analysis: {str(e)}")
        traceback.print_exc()
        if socketio:
            socketio.emit('analysis_error', {'message': f'Failed to start analysis: {str(e)}'}, namespace='/progress')
        else:
            app_config.socketio.emit('analysis_error', {'message': f'Failed to start analysis: {str(e)}'})


def handle_chat_message(data, socketio=None):
    """Handle a chat message from the user, forward to LLM, return reply."""
    user_message = data.get('message', '').strip()
    if not user_message:
        emit_fn = socketio or app_config.socketio
        emit_fn.emit('chat_error', {'message': 'Empty message'}, namespace='/progress')
        return

    print(f"💬 User message: {user_message}")

    # Check if analysis has been run (conversation history exists)
    if not log_parser_service.conversation_history:
        emit_fn = socketio or app_config.socketio
        emit_fn.emit('chat_error', {'message': 'Please run analysis first before chatting.'}, namespace='/progress')
        return

    try:
        llm_helper = app_config.llm_helper
        if llm_helper is None:
            emit_fn = socketio or app_config.socketio
            emit_fn.emit('chat_error', {'message': 'LLM helper is not available.'}, namespace='/progress')
            return

        reply = log_parser_service.handle_chat_message(user_message, llm_helper)
        reply_html = _render_safe_markdown_html(reply)

        print(f"💬 LLM reply length: {len(reply)}")

        emit_fn = socketio or app_config.socketio
        emit_fn.emit('chat_response', {
            'message': reply,
            'message_html': reply_html
        }, namespace='/progress')

    except Exception as e:
        print(f"❌ Chat error: {str(e)}")
        traceback.print_exc()
        emit_fn = socketio or app_config.socketio
        emit_fn.emit('chat_error', {'message': f'Chat failed: {str(e)}'}, namespace='/progress')


def handle_chat_message_with_filter(data, socketio=None):
    """Handle a chat message with user-selected filter keywords.
    
    Re-filters the raw log with the selected keywords, preprocesses it,
    and sends it along with the user's instruction to the LLM as a new chat message.
    """
    user_message = data.get('message', '').strip()
    selected_keywords = data.get('keywords', [])
    
    emit_fn = socketio or app_config.socketio
    
    if not user_message:
        emit_fn.emit('chat_error', {'message': 'Empty message'}, namespace='/progress')
        return
    
    if not selected_keywords:
        emit_fn.emit('chat_error', {'message': 'No filter keywords selected'}, namespace='/progress')
        return

    # Check that raw log lines exist (analysis must have been run)
    if not log_parser_service.raw_log_lines:
        emit_fn.emit('chat_error', {'message': 'Please run analysis first. No raw log available.'}, namespace='/progress')
        return

    print(f"💬 User message with filter: {user_message}")
    print(f"🔧 Selected {len(selected_keywords)} keywords for re-filtering")

    try:
        llm_helper = app_config.llm_helper
        if llm_helper is None:
            emit_fn.emit('chat_error', {'message': 'LLM helper is not available.'}, namespace='/progress')
            return

        # Re-filter and preprocess the raw log with user-selected keywords
        from utils.log_parser_preprocess import filter_log_by_keywords, preprocess_log_for_llm, group_similar_logs
        filtered_log = filter_log_by_keywords(log_parser_service.raw_log_lines, selected_keywords)
        processed_lines = preprocess_log_for_llm(filtered_log)
        grouped = group_similar_logs(processed_lines)
        filtered_content = str(grouped)
        
        print(f"📋 Re-filtered: {len(log_parser_service.raw_log_lines)} raw lines → {len(filtered_log)} filtered → {len(grouped)} grouped")

        # Build the enhanced message with re-filtered log content
        enhanced_message = f"""I have re-filtered the raw log with the following keywords: {', '.join(selected_keywords)}

Here are the re-filtered and preprocessed log entries:
{filtered_content}

Based on these re-filtered logs, please respond to my instruction:
{user_message}"""

        # Must have conversation history (analysis must have been run)
        if not log_parser_service.conversation_history:
            log_parser_service.chat_system_prompt = "You are an expert log analyzer."
            log_parser_service.conversation_history = []

        reply = log_parser_service.handle_chat_message(enhanced_message, llm_helper)
        
        reply_html = _render_safe_markdown_html(reply)

        print(f"💬 LLM reply length: {len(reply)}")

        emit_fn.emit('chat_response', {
            'message': reply,
            'message_html': reply_html
        }, namespace='/progress')

    except Exception as e:
        print(f"❌ Chat with filter error: {str(e)}")
        traceback.print_exc()
        emit_fn.emit('chat_error', {'message': f'Chat failed: {str(e)}'}, namespace='/progress')


def handle_reset_log_parser_session(client_sid: str, socketio=None):
    """Reset in-memory log parser/chat state for a fresh analysis session."""
    emit_fn = socketio or app_config.socketio
    try:
        log_parser_service.reset_log_parser()
        emit_fn.emit('session_reset', {'success': True}, namespace='/progress', to=client_sid)
    except Exception as e:
        emit_fn.emit('session_reset', {
            'success': False,
            'message': f'Failed to reset session: {str(e)}'
        }, namespace='/progress', to=client_sid)
