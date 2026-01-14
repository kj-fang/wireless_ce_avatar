
from threading import Thread
import traceback
import os

from configs.global_configs import app_config
from services.etl_parser.fw_parser import fw_bt_analysis, fw_wifi_analysis

class FWAnalysisService():

    def __init__(self):
        self.service_name = "bt"
    
    def analyze(self, file_path: str, wifi_of_bt: str) -> str:
        
        if 'wifi' in wifi_of_bt:
            self.emit_log("Start FW WiFi analysis.")
            result = fw_wifi_analysis(file_path)
        else:  # BT case → run BT FW analysis
            self.emit_log("Start FW BT analysis.")
            result = fw_bt_analysis(file_path)
        return result
    
    
    def emit_log(self, msg):
        app_config.socketio.emit('wpp_log', {'data': msg}, namespace='/progress')  # Ensure the correct namespace is used

