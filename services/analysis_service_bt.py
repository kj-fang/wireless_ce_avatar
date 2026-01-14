import os

from configs.global_configs import app_config
from services.etl_parser.bt_parser import bt_analysis_manualSelect_mode, bt_analysis_autoFile_mode, bt_analysis_autoFolder_mode


class BTAnalysisService():

    def __init__(self):
        self.service_name = "bt"
    
    def analyze(self, file_path: str, mode: str) -> str:
        
        if mode == 'Manual':
            bt_analysis_manualSelect_mode(file_path)
            self.emit_log("BT tool - Manual launched.")
        elif mode == 'AutoFile':
            bt_analysis_autoFile_mode(file_path)
            self.emit_log("BT tool - AutoFile launched.")
        elif mode == 'AutoFolder':
            etl_path_file = os.path.dirname(file_path)
            bt_analysis_autoFolder_mode(etl_path_file, file_path)
            self.emit_log("BT tool - AutoFolder launched.")
        else:
            print(f"Unknown BT tool mode: {mode}")
        return "BT analysis started successfully"
    
    
    def emit_log(self, msg):
        app_config.socketio.emit('wpp_log', {'data': msg}, namespace='/progress')  # Ensure the correct namespace is used

