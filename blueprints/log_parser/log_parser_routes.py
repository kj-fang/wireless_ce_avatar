from flask import Blueprint, render_template, request, session, redirect, url_for, flash, Response, jsonify
import json
import os 
from urllib.parse import unquote

from utils import helpers
from configs.global_configs import app_config
from configs.path_configs import LOG_PARSER_DIR
from models.models import CaseContext

from services.log_parser_file_manage_service import FileManagerService
from services.log_parser_service import LogParserService

log_parser_bp = Blueprint("log_parser", __name__, url_prefix="/log_parser")

log_parser_service = LogParserService()
file_manager_service = FileManagerService()


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
    print("session['classification'] ", session['classification'])
    classification = session['classification']
    print("classification.keys()", classification.keys())

    output_dir = log_parser_service.set_up(session['download_path'])
    session['logparser_output_dir'] = output_dir


    should_auto_analyze, auto_analysis_data = log_parser_service.check_auto_analysis_availability(classification)

    latest_etl_path = request.args.get('latest_etl_path', None)
    if latest_etl_path:
        etl_path_input = latest_etl_path
    else:
        etl_path_encoded = request.args.get('etl_path', '')
        etl_path_input = unquote(etl_path_encoded)
    
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
        
        success = log_parser_service.start_analysis(
            filter_path, log_path, session['logparser_output_dir'], 
            app_config.llm_helper, custom_prompt_content
        )
        
        if not success:
            app_config.socketio.emit('analysis_error', {'message': 'Failed to start analysis'})
        
    except Exception as e:
        app_config.socketio.emit('analysis_error', {'message': f'Failed to start analysis: {str(e)}'})
