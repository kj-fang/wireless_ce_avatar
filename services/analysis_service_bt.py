import os
import psutil
import threading
# import time

from configs.global_configs import app_config
from services.etl_parser.bt_parser import bt_analysis_manualSelect_mode, bt_analysis_autoFile_mode, bt_analysis_autoFolder_mode


class BTAnalysisService():

    def __init__(self):
        self.service_name = "bt"
        self._active_manual_pid = None
        self._active_manual_file_path = None
        self._monitored_pid = None   # PID that already has a monitor thread
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

            # 2. 先標記這個 file_path 為 active（在慢操作之前）
            with self._monitor_lock:
                self._active_manual_file_path = file_path

            # 3. 把耗時操作丟到背景執行，讓 HTTP 快速返回
            t = threading.Thread(target=self._run_manual_analysis, args=(file_path,), daemon=True)
            t.start()

        # elif mode == 'AutoFile':
        #     bt_analysis_autoFile_mode(file_path)
        #     self.emit_log("BT tool - AutoFile launched.")
        elif mode == 'AutoFolder':
            etl_path_file = os.path.dirname(file_path)
            bt_analysis_autoFolder_mode(etl_path_file, file_path)
            self.emit_log("BT tool - AutoFolder launched.")
        else:
            print(f"Unknown BT tool mode: {mode}")
        return "BT analysis started successfully"

    def _run_manual_analysis(self, file_path: str):
        """背景執行 Manual 模式的 BT 分析"""
        pid = bt_analysis_manualSelect_mode(file_path)
        self.emit_log("BT tool - Manual launched.")

        with self._monitor_lock:
            # 如果這個 file_path 已經被另一個 Manual 取代，就不用繼續
            if self._active_manual_file_path != file_path:
                return

            if not pid or not psutil.pid_exists(pid):
                self._active_manual_file_path = None
                app_config.socketio.emit('manual_complete', {'etl_path': file_path}, namespace='/progress')
                self.emit_log("BT tool PID not found or closed. (Manual)")
                return

            self._active_manual_pid = pid

            # 只有新 PID 才需要新的 monitor thread
            need_monitor = (self._monitored_pid != pid)
            if need_monitor:
                self._monitored_pid = pid

        if need_monitor:
            t = threading.Thread(target=self.monitor_bt_tool, args=(pid,), daemon=True)
            t.start()
    
    def monitor_bt_tool(self, pid):
        """Monitor the BT tool process and emit an event when it closes.
        Only one thread per PID — it reads the latest session state on exit."""
        try:
            if psutil.pid_exists(pid):
                proc = psutil.Process(pid)
                proc.wait()
                self.emit_log(f"BT tool closed. (PID: {pid})")
            else:
                self.emit_log(f"BT tool already closed or invalid PID: {pid}")
        except Exception as e:
            print(f"Error monitoring BT process: {e}")
        finally:
            with self._monitor_lock:
                self._monitored_pid = None
                # Emit completion for whichever file_path is currently the active one
                if self._active_manual_file_path:
                    file_path = self._active_manual_file_path
                    self._active_manual_pid = None
                    self._active_manual_file_path = None
                    app_config.socketio.emit('manual_complete', {'etl_path': file_path}, namespace='/progress')

    
    def emit_log(self, msg):
        app_config.socketio.emit('wpp_log', {'data': msg}, namespace='/progress')  # Ensure the correct namespace is used

