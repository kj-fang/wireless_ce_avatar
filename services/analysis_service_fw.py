
from threading import Thread, Event, Lock
import traceback
import os
import uuid

from configs.global_configs import app_config
from services.etl_parser.fw_parser import fw_bt_analysis, fw_wifi_analysis

class FWAnalysisService():

    def __init__(self):
        self.service_name = "bt"
        self._tasks = {}
        self._lock = Lock()

    def start_async(self, file_path: str, wifi_of_bt: str) -> str:
        task_id = str(uuid.uuid4())
        cancel_event = Event()

        with self._lock:
            self._tasks[task_id] = {
                "status": "running",
                "fw_path": file_path,
                "wifi_of_bt": wifi_of_bt,
                "cancel_event": cancel_event,
                "result": {
                    "log": None,
                    "system_text": None,
                    "system_info": None,
                },
                "error": None,
            }

        Thread(
            target=self._run_task,
            args=(task_id, file_path, wifi_of_bt, cancel_event),
            daemon=True,
        ).start()
        return task_id

    def _run_task(self, task_id: str, file_path: str, wifi_of_bt: str, cancel_event: Event):
        try:
            results = self.analyze(file_path, wifi_of_bt, cancel_event=cancel_event)
            with self._lock:
                task = self._tasks.get(task_id)
                if not task:
                    return
                task["result"] = results
                if task["status"] == "canceled":
                    app_config.socketio.emit(
                        'fw_analysis_cancelled',
                        {'task_id': task_id, 'fw_path': file_path},
                        namespace='/progress'
                    )
                else:
                    task["status"] = "completed"
                    app_config.socketio.emit(
                        'fw_analysis_complete',
                        {'task_id': task_id, 'fw_path': file_path},
                        namespace='/progress'
                    )
        except Exception as e:
            with self._lock:
                task = self._tasks.get(task_id)
                if not task:
                    return
                task["status"] = "failed"
                task["error"] = str(e)
            self.emit_log(f"❌ FW analysis failed: {e}")
            self.emit_log(traceback.format_exc())
            app_config.socketio.emit(
                'fw_analysis_failed',
                {'task_id': task_id, 'fw_path': file_path, 'error': str(e)},
                namespace='/progress'
            )

    def cancel_task(self, task_id: str) -> bool:
        with self._lock:
            task = self._tasks.get(task_id)
            if not task:
                return False
            if task["status"] in ["completed", "failed", "canceled"]:
                return False
            task["status"] = "canceled"
            task["cancel_event"].set()
            fw_path = task["fw_path"]

        app_config.socketio.emit(
            'fw_analysis_cancelled',
            {'task_id': task_id, 'fw_path': fw_path},
            namespace='/progress'
        )
        return True

    def get_task(self, task_id: str):
        with self._lock:
            task = self._tasks.get(task_id)
            if not task:
                return None
            return {
                "status": task["status"],
                "fw_path": task["fw_path"],
                "result": task["result"],
                "error": task["error"],
            }
    
    def analyze(self, file_path: str, wifi_of_bt: str, cancel_event: Event | None = None) -> dict:
        
        results = {
            "log": None,
            "system_text": None,
            'system_info': None

        }
        if 'wifi' in wifi_of_bt:
            self.emit_log("Start FW WiFi analysis.")
            results['system_text'] = fw_wifi_analysis(file_path, cancel_event=cancel_event)
        else:  # BT case → run BT FW analysis
            self.emit_log("Start FW BT analysis.")
            results['system_info'], results['system_text'], results['log'] = fw_bt_analysis(file_path, cancel_event=cancel_event)
        return results
    
    
    def emit_log(self, msg):
        app_config.socketio.emit('wpp_log', {'data': msg}, namespace='/progress')  # Ensure the correct namespace is used

