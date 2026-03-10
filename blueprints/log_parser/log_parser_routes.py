from flask import Blueprint, render_template, request, session, redirect, url_for, flash, Response, jsonify
import json
import os 
import traceback
import markdown
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


#------------Llog parser render -------------#

def render_log_parser_form():
    # 安全地获取 session 数据，如果不存在就使用默认值
    classification = session.get('classification', {})
    download_path = session.get('download_path', '')
    
    # 总是尝试设置 output_dir，即使 download_path 为空也使用默认值
    try:
        if download_path:
            output_dir = log_parser_service.set_up(download_path)
        else:
            # 如果没有 download_path，也尝试初始化，让服务层处理
            output_dir = log_parser_service.set_up('')
        session['logparser_output_dir'] = output_dir
    except Exception as e:
        print(f"Warning: Failed to set up output_dir: {e}")
        output_dir = None

    should_auto_analyze, auto_analysis_data = log_parser_service.check_auto_analysis_availability(classification)

    latest_etl_path = request.args.get('latest_etl_path', None)
    if latest_etl_path:
        etl_path_input = latest_etl_path
    else:
        etl_path_encoded = request.args.get('etl_path', '')
        etl_path_input = unquote(etl_path_encoded)
    
    # 总是尝试准备 log_path，即使 output_dir 为 None
    try:
        log_path = log_parser_service.prepare_log_file(etl_path_input, output_dir)
        if log_path:
            session['log_path'] = log_path
            print(f"✅ Log path set: {log_path}")
        else:
            print("⚠️ prepare_log_file returned None")
    except Exception as e:
        print(f"⚠️ Failed to prepare log file: {e}")
    
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
    
    try:
        filter_path = os.path.join(LOG_PARSER_DIR, "filter", selected_filter)
        print(f"📂 Filter path: {filter_path}")
        print(f"📖 Starting analysis...")
        
        success = log_parser_service.start_analysis(
            filter_path, log_path, output_dir, 
            app_config.llm_helper, custom_prompt_content
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
        reply_html = markdown.markdown(
            reply, extensions=["fenced_code", "tables", "nl2br", "sane_lists", "codehilite"]
        )

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
    """Handle a chat message with filter and prompt context, forward to LLM, return reply."""
    user_message = data.get('message', '').strip()
    filter_file = data.get('filter_file', '').strip()
    prompt_content = data.get('prompt_content', '').strip()
    
    emit_fn = socketio or app_config.socketio
    
    if not user_message:
        emit_fn.emit('chat_error', {'message': 'Empty message'}, namespace='/progress')
        return
    
    if not filter_file:
        emit_fn.emit('chat_error', {'message': 'No filter file selected'}, namespace='/progress')
        return
    
    if not prompt_content:
        emit_fn.emit('chat_error', {'message': 'No prompt content provided'}, namespace='/progress')
        return

    print(f"💬 User message with filter: {user_message}")
    print(f"📂 Filter file: {filter_file}")
    print(f"📝 Prompt length: {len(prompt_content)}")

    try:
        llm_helper = app_config.llm_helper
        if llm_helper is None:
            emit_fn.emit('chat_error', {'message': 'LLM helper is not available.'}, namespace='/progress')
            return

        # Build enhanced message with filter and prompt context
        enhanced_message = f"""Based on the following filter and prompt configuration:

[Filter File]: {filter_file}

[Prompt Configuration]:
{prompt_content}

[User Question]:
{user_message}"""

        # Use existing conversation history if available, otherwise start fresh
        if log_parser_service.conversation_history:
            reply = log_parser_service.handle_chat_message(enhanced_message, llm_helper)
        else:
            # Initialize conversation with filter/prompt context as system
            log_parser_service.chat_system_prompt = f"You are an expert log analyzer. Use the provided filter and prompt configuration to assist the user."
            log_parser_service.conversation_history = []
            reply = log_parser_service.handle_chat_message(enhanced_message, llm_helper)
        
        reply_html = markdown.markdown(
            reply, extensions=["fenced_code", "tables", "nl2br", "sane_lists", "codehilite"]
        )

        print(f"💬 LLM reply length: {len(reply)}")

        emit_fn.emit('chat_response', {
            'message': reply,
            'message_html': reply_html
        }, namespace='/progress')

    except Exception as e:
        print(f"❌ Chat with filter error: {str(e)}")
        traceback.print_exc()
        emit_fn.emit('chat_error', {'message': f'Chat failed: {str(e)}'}, namespace='/progress')
