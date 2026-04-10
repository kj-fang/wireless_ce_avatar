from flask import Blueprint, render_template, request, session, redirect, url_for, flash, Response, jsonify
import json
import os 
import datetime
import logging
import re
import shutil
from urllib.parse import unquote
from werkzeug.utils import secure_filename

from utils import helpers, attachment_decompose
from configs.global_configs import app_config
from configs.path_configs import LOG_PARSER_DIR
from models.models import CaseContext

from services.log_parser_file_manage_service import FileManagerService
from services.log_parser_service import LogParserService
from services.etl_parser.wpp_ddd_parser import wpp_ddd_parser_run

log_parser_bp = Blueprint("log_parser", __name__, url_prefix="/log_parser")

log_parser_service = LogParserService()
file_manager_service = FileManagerService()


def _is_allowed_local_analysis_filename(filename: str) -> bool:
    clean_name = os.path.basename((filename or '').strip())
    lower_name = clean_name.lower()
    return (
        lower_name.endswith('.zip')
        or lower_name.endswith('.7z')
        or lower_name.endswith('.rar')
        or lower_name.endswith('.log')
        or bool(re.search(r'\.etl\.\d+$', clean_name, re.IGNORECASE))
    )


def _infer_local_upload_case_type(wifi_files, ddd_files, bt_files) -> str:
    if bt_files and not (wifi_files or ddd_files):
        return 'bt'
    return 'wifi'


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
    upload_type = request.form.get('type')
    result = file_manager_service.handle_file_upload(upload_type, request.files)
    return result


#------------Section for Local Analysis file uploaded -------------#

@log_parser_bp.route('/upload_local_analysis', methods=['POST'])
def upload_local_analysis():
    files = request.files.getlist('files')
    if not files:
        return jsonify({'success': False, 'message': 'No file uploaded'}), 400

     # Enforce a single file upload to avoid silently dropping additional files
    if len(files) != 1:
        return jsonify({'success': False, 'message': 'Exactly one file must be uploaded for local analysis'}), 400

    etl_file = files[0]

    original_name = etl_file.filename or ''
    if not _is_allowed_local_analysis_filename(original_name):
        return jsonify({
            'success': False,
            'message': f'Invalid file type: {original_name}. Only .zip, .7z, .rar, .etl, or .log are allowed.'
        }), 400

    # folder name is based on the user input
    folder_name = (request.form.get('folder_name') or '').strip()
    if not folder_name:
        return jsonify({
            'success': False,
            'message': 'Folder name is required.'
        }), 400

    safe_folder_name = secure_filename(folder_name)
    if not safe_folder_name:
        return jsonify({
            'success': False,
            'message': 'Invalid folder name. Please use letters, numbers, spaces, hyphen, or underscore.'
        }), 400

    base_upload_dir = app_config.avatarfiles_dir or os.getcwd()
    timestamp = datetime.datetime.now().strftime('%Y%m%d_%H%M%S')
    upload_dir = os.path.join(base_upload_dir, 'local_uploads', safe_folder_name)
    os.makedirs(upload_dir, exist_ok=True)

    safe_name = secure_filename(etl_file.filename)
    if not safe_name:
        safe_name = f'uploaded_{timestamp}.etl'

    file_path = os.path.join(upload_dir, safe_name)

    try:
        etl_file.save(file_path)
        session['download_path'] = upload_dir
        session['classification'] = {
            'issue_type': 'Unclassified',
            'confidence': 0,
            'keywords_found': []
        }

        # Handle .zip, .7z, and .rar files: extract and auto-pick an .etl file
        if file_path.lower().endswith('.zip') or file_path.lower().endswith('.7z') or file_path.lower().endswith('.rar'):
            print(f"📦 Extracting file: {file_path}")
            wifi_files, ddd_files, bt_files, fw_files = attachment_decompose.process_single_zip(
                file_path, upload_dir, already_downloaded=False
            )

            extracted_files = wifi_files + ddd_files + bt_files + fw_files

            if not extracted_files:
                return jsonify({
                    'success': False,
                    'message': 'No supported analysis files found in the uploaded file.'
                }), 400

            local_case_nbr = f'local_upload_{timestamp}'
            local_case_type = _infer_local_upload_case_type(wifi_files, ddd_files, bt_files)

            session['case_context'] = CaseContext(
                case_nbr=local_case_nbr,
                wifi_or_bt=local_case_type,
                case_download_dir=upload_dir
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
            
            return jsonify({
                'success': True,
                'redirect': url_for('main.download_result')
            })
        
        elif file_path.lower().endswith('.log'):
            session['latest_etl_path'] = None
            return jsonify({
                'success': True,
                'redirect': url_for('log_parser.log_parser', etl_path=file_path)
            })
        else:
            # Direct .etl file (no need to extract)
            etl_path = file_path

        # Run parser synchronously so redirect only happens after .etl.log is ready
        wpp_ddd_parser_run(etl_path)
        session['latest_etl_path'] = etl_path

    except Exception as e:
        logging.exception("Failed local analysis flow for %s: %s", file_path, e)
        return jsonify({'success': False, 'message': f'Failed local analysis flow: {str(e)}'}), 500

    return jsonify({
        'success': True,
        'redirect': url_for('log_parser.log_parser', etl_path=etl_path)
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


def register_socketio_handlers(socketio):
    @socketio.on('submit_analysis', namespace='/progress')
    def socketio_submit_analysis(data):
        return handle_submit_analysis(data)


#------------Llog parser render -------------#

def render_log_parser_form():
    classification = session.get('classification', {
        'issue_type': 'Unclassified',
        'confidence': 0,
        'keywords_found': []
    })
    print("session['classification'] ", classification)
    print("classification.keys()", classification.keys())

    download_path = session.get('download_path', app_config.avatarfiles_dir or os.getcwd())
    output_dir = log_parser_service.set_up(download_path)
    session['logparser_output_dir'] = output_dir


    should_auto_analyze, auto_analysis_data = log_parser_service.check_auto_analysis_availability(classification)

    latest_etl_path = request.args.get('latest_etl_path', None) or session.get('latest_etl_path', None)
    if latest_etl_path:
        etl_path_input = latest_etl_path
    else:
        etl_path_encoded = request.args.get('etl_path', '')
        etl_path_input = unquote(etl_path_encoded)

    if etl_path_input and etl_path_input.lower().endswith('.log') and os.path.exists(etl_path_input):
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
