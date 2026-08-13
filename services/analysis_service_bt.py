import os
import threading

from configs.global_configs import app_config
from configs.path_configs import LOG_PARSER_DIR
from services.etl_parser.bt_parser import bt_decode_via_cli, open_with_text_analysis_tool


class BTAnalysisService():

    def __init__(self):
        self.service_name = "bt"
        self._active_file_path = None
        self._active_mode = None
        self._monitor_lock = threading.Lock()

    def _release_previous(self):
        """Release tracking of the previous session so its button restores."""
        with self._monitor_lock:
            old_file_path = self._active_file_path
            self._active_file_path = None
            self._active_mode = None
        # Emit outside lock to avoid potential deadlock
        if old_file_path:
            app_config.socketio.emit('autofolder_complete', {'etl_path': old_file_path}, namespace='/progress')

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

        if mode == 'AutoFolder':
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
            return f"Unknown BT tool mode: {mode}"
        return "BT analysis started successfully"

    def _run_llm_decode(self, file_path: str):
        """Background thread: decode HCI via CLI and emit bt_hci_ready for the LLM log_parser flow."""
        self.emit_log(f"🔍 HCI decoding for LLM analysis (AutoFolder): {os.path.basename(file_path)}")
        etl_folder = os.path.dirname(file_path)
        hci_path = bt_decode_via_cli(etl_folder, file_path, emit_callback=self.emit_log)
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

    def _run_autofolder_analysis(self, file_path: str, filter_path: str = None):
        """Background thread: decode all ETLs in the folder via CLI and open the result."""
        etl_folder = os.path.dirname(file_path)
        print(f"Starting AutoFolder analysis for: {etl_folder}")

        # should_stop callable: returns True if this operation was superseded
        def should_stop():
            with self._monitor_lock:
                return self._active_file_path != file_path

        self.emit_log(f"🔍 Decoding ETL (CLI): {os.path.basename(file_path)}")
        hci_path = bt_decode_via_cli(etl_folder, file_path, skip_non_target=False, emit_callback=self.emit_log)

        # CLI decode cannot be cancelled mid-run; check for supersession after it returns.
        # Normally, the frontend will disable the button while running the decode. 
        # So this check is just a safety net in case the user clicks it again after the decode completes.
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

    def emit_log(self, msg):
        app_config.socketio.emit('bt_log', {'data': msg}, namespace='/progress')
