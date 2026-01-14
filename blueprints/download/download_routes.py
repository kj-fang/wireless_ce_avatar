from flask import Blueprint, render_template, request, session, redirect, url_for, flash, Response, jsonify
from threading import Thread

from utils import attachment_decompose, attachment_download
from configs.global_configs import app_config



download_bp = Blueprint("download", __name__, url_prefix="/download")

#------------REGISTER SOCKETIO-------------#
def register_socketio_handlers(socketio):
    @socketio.on('start_download', namespace='/progress')
    def socketio_start_download():
        return handle_start_download(socketio)
#------------REGISTER SOCKETIO-------------#

def handle_start_download(socketio):
    selected_files = session.get('selected_files', [])
    download_path = session.get('download_path', '')
    case_nbr = session.get('case_context')['case_nbr']
    is_bsod = session.get('bsod')
    
    def background_download(selected_files, download_path):
        wifi_dict = {}
        ddd_dict = {}
        bt_dict = {}       
        fw_dict = {}
        file_path = None  
        
        for file_path, name, already_dload in attachment_download.run_dload_threads(selected_files, download_path, socketio):
            print("Downloaded:", file_path, name)
            if not is_bsod:
                wifi_files, ddd_files, bt_files, fw_files= attachment_decompose.process_single_zip(file_path, download_path, already_dload) #####0806

                wifi_dict[name] = wifi_files
                ddd_dict[name] = ddd_files
                bt_dict[name] = bt_files
                fw_dict[name] = fw_files

        if not is_bsod:
            
            app_config.set_download_results(case_nbr,wifi=wifi_dict, ddd=ddd_dict, bt = bt_dict, fw= fw_dict )
            
            socketio.emit('all_attachments_download_done', {'wifi_dict': wifi_dict, 'ddd_dict': ddd_dict , 'bt_dict': bt_dict, 'fw_dict': fw_dict}, namespace='/progress')
        else:
            socketio.emit('all_attachments_download_done_bsod', {'dump_path': file_path}, namespace='/progress')
        
    Thread(target=background_download,  args=(selected_files, download_path,), daemon=False).start()

