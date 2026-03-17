import os
import psutil
import threading
# import time

from configs.global_configs import app_config
from services.etl_parser.bt_parser import bt_analysis_manualSelect_mode, bt_analysis_autoFile_mode, bt_analysis_autoFolder_mode


class BTAnalysisService():

    def __init__(self):
        self.service_name = "bt"
        self._active_pid = None
        self._active_file_path = None
        self._active_mode = None  # 'Manual' or 'AutoFolder'
        self._monitored_pid = None
        self._monitor_lock = threading.Lock()

    def _release_previous(self):
        """Release tracking of the previous session so its button restores.
        The BT tool process is NOT killed — it will be reused by bt_parser."""
        with self._monitor_lock:
            old_file_path = self._active_file_path
            old_mode = self._active_mode
            self._active_pid = None
            self._active_file_path = None
            self._active_mode = None
        # Emit outside lock to avoid potential deadlock
        if old_file_path and old_mode:
            event_name = 'manual_complete' if old_mode == 'Manual' else 'autofolder_complete'
            app_config.socketio.emit(event_name, {'etl_path': old_file_path}, namespace='/progress')

    def analyze(self, file_path: str, mode: str) -> str:
        
        if mode == 'Manual':
            self._release_previous()
            with self._monitor_lock:
                self._active_file_path = file_path
                self._active_mode = 'Manual'
            t = threading.Thread(target=self._run_manual_analysis, args=(file_path,), daemon=True)
            t.start()

        elif mode == 'AutoFolder':
            self._release_previous()
            with self._monitor_lock:
                self._active_file_path = file_path
                self._active_mode = 'AutoFolder'
            t = threading.Thread(target=self._run_autofolder_analysis, args=(file_path,), daemon=True)
            t.start()

        else:
            print(f"Unknown BT tool mode: {mode}")
        return "BT analysis started successfully"

    def _run_manual_analysis(self, file_path: str):
        """背景執行 Manual 模式的 BT 分析"""
        pid = bt_analysis_manualSelect_mode(file_path)
        self.emit_log("BT tool - Manual launched.")
        self._finish_analysis(file_path, pid, 'Manual')

    def _run_autofolder_analysis(self, file_path: str):
        """背景執行 AutoFolder 模式的 BT 分析"""
        etl_folder = os.path.dirname(file_path)
        
        # should_stop callable: returns True if this operation was superseded
        def should_stop():
            with self._monitor_lock:
                return self._active_file_path != file_path
        
        pid = bt_analysis_autoFolder_mode(etl_folder, file_path, should_stop=should_stop)
        self.emit_log("BT tool - AutoFolder launched.")
        self._finish_analysis(file_path, pid, 'AutoFolder')

    def _finish_analysis(self, file_path: str, pid, mode: str):
        """完成分析後的通用處理：檢查 PID 並啟動 monitor"""
        event_name = 'manual_complete' if mode == 'Manual' else 'autofolder_complete'

        with self._monitor_lock:
            # 如果已經被另一個操作取代，就不用繼續
            if self._active_file_path != file_path:
                return

            if not pid or not psutil.pid_exists(pid):
                self._active_file_path = None
                self._active_mode = None
                app_config.socketio.emit(event_name, {'etl_path': file_path}, namespace='/progress')
                self.emit_log(f"BT tool PID not found or closed. ({mode})")
                return

            self._active_pid = pid

            # 只有新 PID 才需要新的 monitor thread
            need_monitor = (self._monitored_pid != pid)
            if need_monitor:
                self._monitored_pid = pid

        if need_monitor:
            t = threading.Thread(target=self.monitor_bt_tool, args=(pid,), daemon=True)
            t.start()
    
    def monitor_bt_tool(self, pid):
        """Monitor the BT tool process and emit an event when it closes."""
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
                if self._active_file_path:
                    file_path = self._active_file_path
                    mode = self._active_mode
                    self._active_pid = None
                    self._active_file_path = None
                    self._active_mode = None
                    event_name = 'manual_complete' if mode == 'Manual' else 'autofolder_complete'
                    app_config.socketio.emit(event_name, {'etl_path': file_path}, namespace='/progress')

    
    def emit_log(self, msg):
        app_config.socketio.emit('wpp_log', {'data': msg}, namespace='/progress')  # Ensure the correct namespace is used
