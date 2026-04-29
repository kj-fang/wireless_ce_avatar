from flask import Blueprint, render_template, request, session, redirect, url_for, flash, Response, jsonify
import json
import os 
import datetime
import hmac
import logging
import re
import shutil
from urllib.parse import unquote
from werkzeug.utils import secure_filename

from utils import helpers, attachment_decompose
from configs.global_configs import app_config
from configs.path_configs import LOG_PARSER_DIR, LOAD_PATH_prim, LOAD_PATH_bkup
from models.models import CaseContext

from services.log_parser_file_manage_service import FileManagerService
from services.log_parser_service import LogParserService
from services.etl_parser.wpp_ddd_parser import wpp_ddd_parser_run

log_parser_bp = Blueprint("log_parser", __name__, url_prefix="/log_parser")

log_parser_service = LogParserService()
file_manager_service = FileManagerService()

#------------Section for Local dmp file upload bar -------------#
def _copy_file_with_console_progress(src_path: str, dst_path: str, chunk_size: int = 4 * 1024 * 1024) -> None:
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

    with open(src_path, 'rb') as source, open(dst_path, 'wb') as destination:
        while True:
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
        or lower_name.endswith('.etl')
        or lower_name.endswith('.dmp')
        or bool(re.search(r'\.etl\.\d+$', clean_name, re.IGNORECASE))
    )


def _infer_local_upload_case_type(wifi_files, ddd_files, bt_files) -> str:
    if bt_files and not (wifi_files or ddd_files):
        return 'bt'
    return 'wifi'


def _process_local_analysis(source_path: str, source_dir: str, file_path: str,
                            original_name: str, timestamp: str,
                            is_bsod: bool = False) -> str:
    """Shared core logic for local analysis (used by both upload and SendTo flows).

    Sets up session state, extracts archives / parses ETL / handles .log/.dmp files.
    Returns the redirect URL on success.
    Raises ValueError for validation failures (e.g. no supported files found).
    """
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
        shared_dmp_path = os.path.join(shared_case_dir, original_name)
        _copy_file_with_console_progress(file_path, shared_dmp_path)

        # Ensure downstream BSOD page/API submission uses shared folder path.
        session['download_path'] = shared_case_dir
        
        # Build a minimal case context so BSOD submission page can render in local-upload mode.
        session['case_context'] = CaseContext(
            case_nbr=local_case_nbr,
            backend_id=local_case_nbr,
            wifi_or_bt='wifi',
            case_download_dir=shared_case_dir,
        ).to_session()
        session['selected_files'] = [(original_name, original_name, None)]
        session['bsod'] = True
        session['latest_etl_llm'] = False
        session['latest_etl_path'] = None
        return url_for('main.download_result_bsod')

    elif file_path.lower().endswith('.zip') or file_path.lower().endswith('.7z') or file_path.lower().endswith('.rar'):
        print(f"📦 Extracting file: {file_path}")
        wifi_files, ddd_files, bt_files, fw_files = attachment_decompose.process_single_zip(
            file_path, source_dir, already_downloaded=False
        )

        extracted_files = wifi_files + ddd_files + bt_files + fw_files
        if not extracted_files:
            raise ValueError('No supported analysis files found in the uploaded file.')

        local_case_nbr = f'local_upload_{timestamp}'
        local_case_type = _infer_local_upload_case_type(wifi_files, ddd_files, bt_files)

        session['case_context'] = CaseContext(
            case_nbr=local_case_nbr,
            wifi_or_bt=local_case_type,
            case_download_dir=source_dir
        ).to_session()
        session['selected_files'] = []
        session['bsod'] = False
        session['latest_etl_llm'] = False

        app_config.set_download_results(
            local_case_nbr,
            wifi={original_name: wifi_files},
            ddd={original_name: ddd_files},
            bt={original_name: bt_files},
            fw={original_name: fw_files}
        )

        return url_for('main.download_result')

    elif file_path.lower().endswith('.log'):
        session['latest_etl_path'] = None
        return url_for('log_parser.log_parser', etl_path=file_path)

    else:
        wpp_ddd_parser_run(file_path)
        session['latest_etl_path'] = file_path
        return url_for('log_parser.log_parser', etl_path=file_path)


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
                ('Supported files', '*.zip *.7z *.rar *.log *.etl *.etl.* *.dmp'),
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
                'message': f'Invalid file type: {selected_name}. Only .zip, .7z, .rar, .etl, .log, or .dmp are allowed.'
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
            'message': f'Invalid file type: {original_name}. Only .zip, .7z, .rar, .etl, .log, or .dmp are allowed.'
        }), 400

    timestamp = datetime.datetime.now().strftime('%Y%m%d_%H%M%S')
    source_dir = os.path.dirname(source_path) or os.getcwd()
    file_path = source_path

    try:
        redirect_url = _process_local_analysis(
            source_path, source_dir, file_path, original_name, timestamp,
            is_bsod=request.form.get('is_bsod') == 'true'
        )
    except ValueError as e:
        return jsonify({'success': False, 'message': str(e)}), 400
    except Exception as e:
        logging.exception("Failed local analysis flow for %s: %s", file_path, e)
        return jsonify({'success': False, 'message': f'Failed local analysis flow: {str(e)}'}), 500

    return jsonify({
        'success': True,
        'uploaded_source_path': source_path,
        'redirect': redirect_url
    })

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
        flash(f'Invalid file type: {original_name}. Only .zip, .7z, .rar, .etl, .log, or .dmp are allowed.', 'danger')
        return redirect(url_for('main.index'))

    timestamp = datetime.datetime.now().strftime('%Y%m%d_%H%M%S')
    source_dir = os.path.dirname(source_path) or os.getcwd()
    file_path = source_path

    try:
        redirect_url = _process_local_analysis(source_path, source_dir, file_path, original_name, timestamp)
    except ValueError as e:
        flash(str(e), 'danger')
        return redirect(url_for('main.index'))
    except Exception as error:
        logging.exception("Failed SendTo local analysis flow for %s: %s", source_path, error)
        flash(f'Failed local analysis flow: {error}', 'danger')
        return redirect(url_for('main.index'))

    return redirect(redirect_url)


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
        return handle_submit_analysis(data)


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

    latest_etl_path = request.args.get('latest_etl_path', None) or session.get('latest_etl_path', None)
    if latest_etl_path:
        etl_path_input = latest_etl_path
    else:
        etl_path_encoded = request.args.get('etl_path', '')
        etl_path_input = unquote(etl_path_encoded)

    if session.get('local_in_place') and etl_path_input:
        # In-place mode: output is already in source dir, no copy needed.
        candidate = etl_path_input if etl_path_input.lower().endswith('.log') else etl_path_input + '.log'
        log_path = candidate if os.path.exists(candidate) else None
    elif etl_path_input and etl_path_input.lower().endswith('.log') and os.path.exists(etl_path_input):
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
                          available_custom_prompts=available_custom_prompts)


def handle_submit_analysis(data):
    print("Received analysis submission:", data)
    
    log_path = session.get('log_path', '')
    selected_filter = data.get('filter_file')
    custom_prompt_content = data.get('prompt_content')
    
    is_valid, error_message = log_parser_service.validate_analysis_inputs(
        log_path, selected_filter, custom_prompt_content
    )
    
    if not is_valid:
        app_config.socketio.emit('validation_error', {'message': error_message})
        return
    
    try:
        filter_path = os.path.join(LOG_PARSER_DIR, "filter", selected_filter)
        
        # Get case description from session
        case_context_dict = session.get('case_context', {})
        case_description = case_context_dict.get('description', '')
        
        success = log_parser_service.start_analysis(
            filter_path, log_path, session['logparser_output_dir'], 
            app_config.llm_helper, custom_prompt_content, case_description
        )
        
        if not success:
            app_config.socketio.emit('analysis_error', {'message': 'Failed to start analysis'})
        
    except Exception as e:
        app_config.socketio.emit('analysis_error', {'message': f'Failed to start analysis: {str(e)}'})
