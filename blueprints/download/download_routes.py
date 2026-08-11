from flask import Blueprint, render_template, request, session, redirect, url_for, flash, Response, jsonify
from threading import Thread

from utils import attachment_decompose, attachment_download
from configs.global_configs import app_config
from models.models import CaseContext
from services import gather_service



download_bp = Blueprint("download", __name__, url_prefix="/download")

#------------REGISTER SOCKETIO-------------#
def register_socketio_handlers(socketio):
    @socketio.on('start_download', namespace='/progress')
    def socketio_start_download():
        return handle_start_download(socketio)

    @socketio.on('cancel_download', namespace='/progress')
    def socketio_cancel_download():
        return handle_cancel_download(socketio, request.sid)
#------------REGISTER SOCKETIO-------------#

@download_bp.route('/cancel_download', methods=['POST'])
def cancel_download_route():
    """HTTP endpoint used when the user leaves the download page (e.g. presses
    the browser Back button). Called via navigator.sendBeacon so the request
    reliably fires during page unload. Stops downloads and removes partial files."""
    app_config.driver_manager.cancel_downloads()
    return ('', 204)

def handle_cancel_download(socketio, client_sid):
    driver_manager = app_config.driver_manager
    driver_manager.cancel_downloads()
    socketio.emit('download_cancelled', {}, namespace='/progress', to=client_sid)

def handle_start_download(socketio):
    driver_manager = app_config.driver_manager
    driver_manager.download_cancel_event.clear()

    selected_files = session.get('selected_files', [])
    download_path = session.get('download_path', '')
    case_context = CaseContext.from_session(session.get('case_context') or {})
    case_nbr = case_context.case_nbr
    is_bsod = session.get('bsod')
    workflow_id = session.get('gather_workflow_id', '')
    issue_snapshot = case_context.to_dict()
    domain = case_context.wifi_or_bt or 'wifi'
    
    def background_download(selected_files, download_path):
        wifi_dict = {}
        ddd_dict = {}
        bt_dict = {}       
        fw_dict = {}
        file_path = None  

        def record_download_result(result):
            try:
                gather_service.record_attachment_download_result(
                    workflow_id=workflow_id,
                    name=str(result.get('name') or ''),
                    status=str(result.get('status') or 'failed'),
                    byte_count=result.get('bytes'),
                    latency_ms=result.get('latency_ms'),
                    attempt_count=result.get('attempt_count') or 0,
                    error_code=str(result.get('error_code') or ''),
                    issue=issue_snapshot,
                    domain=domain,
                )
            except Exception:
                pass
        
        for file_path, name, already_dload in attachment_download.run_dload_threads(
            selected_files, download_path, socketio,
            result_callback=record_download_result,
        ):
            print("Downloaded:", file_path, name)
            if not is_bsod:
                wifi_files, ddd_files, evt_files, bt_files, fw_files = attachment_decompose.process_single_zip(file_path, download_path, already_dload) #####0806

                wifi_dict[name] = wifi_files
                ddd_dict[name] = ddd_files + evt_files
                bt_dict[name] = bt_files
                fw_dict[name] = fw_files

        if driver_manager.download_cancel_event.is_set():
            print("🛑 Download cancelled by user. Skipping result processing.")
            attachment_download.cleanup_incomplete_downloads(download_path)
            return

        if not is_bsod:
            
            app_config.set_download_results(case_nbr,wifi=wifi_dict, ddd=ddd_dict, bt = bt_dict, fw= fw_dict )
            
            socketio.emit('all_attachments_download_done', {'wifi_dict': wifi_dict, 'ddd_dict': ddd_dict , 'bt_dict': bt_dict, 'fw_dict': fw_dict}, namespace='/progress')
        else:
            socketio.emit('all_attachments_download_done_bsod', {'dump_path': file_path}, namespace='/progress')
        
    Thread(target=background_download,  args=(selected_files, download_path,), daemon=False).start()

