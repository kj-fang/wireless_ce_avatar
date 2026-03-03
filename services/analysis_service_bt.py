import os
import psutil
import threading
import time

from configs.global_configs import app_config
from services.etl_parser.bt_parser import bt_analysis_manualSelect_mode, bt_analysis_autoFile_mode, bt_analysis_autoFolder_mode


class BTAnalysisService():

    def __init__(self):
        self.service_name = "bt"
    
    def analyze(self, file_path: str, mode: str) -> str:
        
        if mode == 'Manual':
            pid = bt_analysis_manualSelect_mode(file_path)
            self.emit_log("BT tool - Manual launched.")

            if pid:
                t = threading.Thread(target=self.monitor_bt_tool, args=(pid, file_path), name=f"ibtdrvlogparser_Monitor_{pid}")
                t.daemon = True
                t.start()
            else:
                self.emit_log("BT tool PID not found. (Manual)")
                app_config.socketio.emit('manual_complete', {'etl_path': file_path}, namespace='/progress')

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
    
    def monitor_bt_tool(self, pid, file_path):
        """Monitor the BT tool process and emit an event when it closes."""
        try:
            if psutil.pid_exists(pid):
                proc = psutil.Process(pid)
                # Wait for the process to terminate
                proc.wait()
                self.emit_log(f"BT tool closed. (PID: {pid})")
            else:
                self.emit_log(f"BT tool already closed or invalid PID: {pid}")
        except Exception as e:
            print(f"Error monitoring BT process: {e}")
        finally:
            # Emit the completion event to the frontend
            app_config.socketio.emit('manual_complete', {'etl_path': file_path}, namespace='/progress')

    
    def emit_log(self, msg):
        app_config.socketio.emit('wpp_log', {'data': msg}, namespace='/progress')  # Ensure the correct namespace is used

