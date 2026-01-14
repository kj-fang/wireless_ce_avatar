
from threading import Thread
import traceback

from configs.global_configs import app_config
from services.etl_parser.wpp_ddd_parser import wpp_ddd_parser_run


class WiFiAnalysisService():

    def __init__(self):
        self.service_name = "wifi"
    
    def analyze(self, file_path: str) -> str:
        
        Thread(target=self.run_wpp_and_check, args=(file_path,), daemon=True).start()
        print("🚀 WiFi analysis started.")
        return "WiFi analysis started successfully"
    
    def run_wpp_and_check(self, etl_file):
        try:
            self.emit_log("Start wpp_ddd_parser...")
            wpp_ddd_parser_run(etl_file)
        except Exception as e:
            self.emit_log(f"❌ Exception occurred:{e}")
            self.emit_log(traceback.format_exc())

    
    def emit_log(self, msg):
        app_config.socketio.emit('wpp_log', {'data': msg}, namespace='/progress')  # Ensure the correct namespace is used

