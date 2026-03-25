
from threading import Thread, Event, Lock
import traceback
import os
import uuid

from configs.global_configs import app_config
from services.etl_parser.fw_parser import fw_bt_analysis, fw_wifi_analysis
from utils.fw_utils import load_fw_system_info

class FWAnalysisService():

    def __init__(self):
        self.fw_validate = True # for debug purpose, set to False to skip all the precheck and system info validation
        self.service_name = "bt"
        self._tasks = {}
        self._lock = Lock()
        self.exe_cli_path = r"C:\UtilityPackage\WRT_BT_Logs_Decoder\bt_decoder_cli.exe"

    def _validate_system_info(self, system_info):
        bt_fw_sha1 = (system_info or {}).get("BT FW SHA1", "")
        dbgc_bt = (system_info or {}).get("Dbgc Status Global as seen by BT", "")
        dbgc_mailbox = (system_info or {}).get("Dbgc Status as read from Mailbox", "")

        if not bt_fw_sha1:
            return False, "BT FW SHA1 is unavailable"

        try:
            int(bt_fw_sha1, 16)
        except ValueError:
            return False, "BT FW SHA1 is not a valid hex value"

        if dbgc_bt != 'Dram' or dbgc_mailbox != 'Dram':
            return False, "DBGC status is not Dram"

        return True, ""

    def _validate_precheck(self, file_path: str):

        # check etl file 
        if not file_path:
            return False, "FW path is empty"

        if not os.path.exists(file_path):
            return False, f"Invalid file path: {file_path}"

        if not os.path.isfile(file_path):
            return False, "FW path must be a file"

        if not file_path.lower().endswith('.etl'):
            return False, "FW parse only supports .etl files"

        try:
            if os.path.getsize(file_path) < 1024*1024*10: 
                return False, f"FW ETL file size is less than 10MB: {file_path}"
        except OSError as e:
            return False, f"Cannot access FW file: {e}"
        
        # check driver log file
        fw_dir = os.path.dirname(file_path)
        for file in os.listdir(os.path.join(fw_dir, "BT")):
            if file.lower().startswith("Host_Logs") and os.path.isdir(os.path.join(fw_dir, "BT", file)):
                for subfile in os.listdir(os.path.join(fw_dir, "BT", file)):
                    if subfile.lower().startswith("ibtpci") and subfile.lower().endswith('.etl'):
                        driver_log_path = os.path.join(fw_dir, "BT", file, subfile)
                        if os.path.exists(driver_log_path) and os.path.isfile(driver_log_path):
                            if os.path.getsize(driver_log_path) < 1024*96: 
                                return False, f"Driver log file size is less than 96KB: {driver_log_path}"

        # check CLI tool
        if not os.path.exists(self.exe_cli_path):
            return False, f"FW analysis CLI tool not found at: {self.exe_cli_path}"

        return True, ""

    def start_async(self, file_path: str, wifi_of_bt: str):
        if self.fw_validate:
            is_valid, error_msg = self._validate_precheck(file_path)
            if not is_valid:
                system_info = load_fw_system_info(file_path)
                app_config.socketio.emit(
                    'fw_analysis_rejected',
                    {
                        'task_id': None,
                        'fw_path': file_path,
                        'error': error_msg,
                        'system_info': system_info,
                    },
                    namespace='/progress'
                )
                return None, error_msg

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
        return task_id, ""

    def _run_task(self, task_id: str, file_path: str, wifi_of_bt: str, cancel_event: Event):
        try:
            results = {}
            results['system_info'] = load_fw_system_info(file_path)

            if self.fw_validate:
                system_info_ok, rejected_reason = self._validate_system_info(results['system_info'])

                if not system_info_ok:
                    with self._lock:
                        task = self._tasks.get(task_id)
                        if not task:
                            return
                        task["status"] = "rejected"
                        task["error"] = rejected_reason
                        task["result"]["system_info"] = results['system_info']

                    app_config.socketio.emit(
                        'fw_analysis_rejected',
                        {
                            'task_id': task_id,
                            'fw_path': file_path,
                            'error': rejected_reason,
                            'system_info': results.get('system_info'),
                        },
                        namespace='/progress'
                    )
                    return

            results['system_text'], results['log'] = self.analyze(file_path, wifi_of_bt, cancel_event=cancel_event)
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
                elif task["status"] == "rejected":
                    app_config.socketio.emit(
                        'fw_analysis_rejected',
                        {
                            'task_id': task_id,
                            'fw_path': file_path,
                            'error': task["error"] or "precheck failed",
                            'system_info': task.get("result", {}).get("system_info"),
                        },
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
            if task["status"] in ["completed", "failed", "canceled", "rejected"]:
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
    
    def analyze(self, file_path: str, wifi_of_bt: str, cancel_event: Event | None = None):
        
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
        return results['system_text'], results['log']
    
    
    def emit_log(self, msg):
        app_config.socketio.emit('wpp_log', {'data': msg}, namespace='/progress')  # Ensure the correct namespace is used

