import os
import psutil
import threading
import time

from configs.global_configs import app_config
from services.etl_parser.bt_parser import bt_analysis_manualSelect_mode, bt_analysis_autoFile_mode, bt_analysis_autoFolder_mode


class BTAnalysisService():

    def __init__(self):
        self.service_name = "bt"
        self._active_manual_pid = None
        self._active_manual_file_path = None
        self._manual_session_id = 0  # incremented each time a new Manual is clicked
        self._monitor_lock = threading.Lock()

    def _release_previous_manual(self):
        """Release tracking of the previous Manual session so its button restores.
        The BT tool process is NOT killed — it will be reused by bt_parser."""
        with self._monitor_lock:
            old_file_path = self._active_manual_file_path
            self._active_manual_pid = None
            self._active_manual_file_path = None
        # Emit outside lock to avoid potential deadlock
        if old_file_path:
            app_config.socketio.emit('manual_complete', {'etl_path': old_file_path}, namespace='/progress')

    def analyze(self, file_path: str, mode: str) -> str:
        
        if mode == 'Manual':
            # Release previous manual session (restore old button); process is reused, not killed
            self._release_previous_manual()

            pid = bt_analysis_manualSelect_mode(file_path)
            self.emit_log("BT tool - Manual launched.")

            if pid:
                with self._monitor_lock:
                    self._manual_session_id += 1
                    session_id = self._manual_session_id
                    self._active_manual_pid = pid
                    self._active_manual_file_path = file_path

                t = threading.Thread(target=self.monitor_bt_tool, args=(pid, file_path, session_id), name=f"ibtdrvlogparser_Monitor_{pid}_{session_id}")
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
    
    def monitor_bt_tool(self, pid, file_path, session_id):
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
            with self._monitor_lock:
                # Only emit completion if this monitor's session is still the active one.
                # If a new Manual was clicked, _release_previous_manual already handled it
                # and _manual_session_id has been incremented, so old threads won't match.
                if self._manual_session_id == session_id:
                    self._active_manual_pid = None
                    self._active_manual_file_path = None
                    app_config.socketio.emit('manual_complete', {'etl_path': file_path}, namespace='/progress')

    
    def emit_log(self, msg):
        app_config.socketio.emit('wpp_log', {'data': msg}, namespace='/progress')  # Ensure the correct namespace is used

