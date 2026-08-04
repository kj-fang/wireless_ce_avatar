import os
import psutil
import threading
# import time

from configs.global_configs import app_config
from configs.path_configs import LOG_PARSER_DIR
from services.etl_parser.bt_parser import bt_analysis_manualSelect_mode, bt_analysis_autoFile_mode, bt_analysis_autoFolder_mode, bt_decode_hci_via_folder, bt_decode_via_cli, open_with_text_analysis_tool


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

    def _resolve_bt_filter_path(self, issue_type: str = None, wifi_or_bt: str = None):
        issue = (issue_type or '').lower()
        if not issue:
            return None

        filter_name = issue
        if 'yellow bang' in issue:
            filter_name = 'BT_YB_LOST' if str(wifi_or_bt).lower() == 'bt' else 'yellow_bang'

        candidate = os.path.join(LOG_PARSER_DIR, 'filter', f"{filter_name}.tat")
        return candidate if os.path.exists(candidate) else None

    def analyze(self, file_path: str, mode: str, issue_type: str = None, wifi_or_bt: str = None) -> str:
        filter_path = self._resolve_bt_filter_path(issue_type, wifi_or_bt)
        
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
            t = threading.Thread(
                target=self._run_autofolder_analysis,
                args=(file_path, filter_path),
                daemon=True
            )
            t.start()

        elif mode == 'LLM':
            t = threading.Thread(
                target=self._run_llm_decode,
                args=(file_path,),
                daemon=True
            )
            t.start()

        else:
            print(f"Unknown BT tool mode: {mode}")
        return "BT analysis started successfully"

    def _run_llm_decode(self, file_path: str):
        """背景執行 LLM 模式：用 AutoFolder tab decode HCI 後 emit bt_hci_ready 讓前端跳轉 log_parser"""
        self.emit_log(f"🔍 HCI decoding for LLM analysis (AutoFolder): {os.path.basename(file_path)}")
        etl_folder = os.path.dirname(file_path)
        hci_path = bt_decode_via_cli(etl_folder, file_path)
        if hci_path:
            self.emit_log(f"✅ HCI decode complete: {hci_path}")
            app_config.socketio.emit(
                'bt_hci_ready',
                {'hci_path': hci_path, 'etl_path': file_path},
                namespace='/progress'
            )
        else:
            self.emit_log("❌ HCI decode failed or timed out.")
            app_config.socketio.emit(
                'bt_hci_failed',
                {'etl_path': file_path},
                namespace='/progress'
            )

    def _run_manual_analysis(self, file_path: str):
        """背景執行 Manual 模式的 BT 分析"""
        pid = bt_analysis_manualSelect_mode(file_path)
        self.emit_log("BT tool - Manual launched.")
        self._finish_analysis(file_path, pid, 'Manual')

    def _run_autofolder_analysis(self, file_path: str, filter_path: str = None):
        """背景執行 AutoFolder 模式的 BT 分析（CLI 版本）"""
        etl_folder = os.path.dirname(file_path)
        print(f"Starting AutoFolder analysis for: {etl_folder}")

        # should_stop callable: returns True if this operation was superseded
        def should_stop():
            with self._monitor_lock:
                return self._active_file_path != file_path

        self.emit_log(f"🔍 Decoding ETL (CLI): {os.path.basename(file_path)}")
        hci_path = bt_decode_via_cli(etl_folder, file_path, skip_non_target=False)

        # CLI decode 無法中途打斷，但完成後需確認是否已被取代
        if should_stop():
            self.emit_log("⚠️ AutoFolder operation superseded, discarding result.")
            return

        with self._monitor_lock:
            self._active_file_path = None
            self._active_mode = None

        if hci_path:
            self.emit_log(f"✅ Decode complete: {os.path.basename(hci_path)}")
            open_with_text_analysis_tool(hci_path, filter_path=filter_path)
        else:
            self.emit_log("❌ AutoFolder decode failed or timed out.")

        app_config.socketio.emit('autofolder_complete', {'etl_path': file_path}, namespace='/progress')

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
