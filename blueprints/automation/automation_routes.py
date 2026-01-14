from flask import Blueprint, render_template, request, session, redirect, url_for, flash
import os
import subprocess
from threading import Thread, Event
import re

from models.models import CaseContext
from configs.global_configs import app_config

from utils.etl_utils import extract_address_digits, extract_etl_suffix_number
from utils.attachment_download import download_file
from utils.attachment_decompose import process_single_zip

from services.analysis_service_wifi import WiFiAnalysisService
from services.analysis_service_bt import BTAnalysisService

automation_bp = Blueprint("automation", __name__, url_prefix="/automation")


@automation_bp.route('/run_latest_etl', methods=['GET', 'POST'])
def run_latest_etl():
    return render_template("run_progress.html")

def register_socketio_handlers(socketio):
    @socketio.on('trigger_run_latest_etl', namespace='/progress')
    def socketio_run_latest_etl():
        return handle_run_latest_etl()

def handle_run_latest_etl():
    """
    Socket.IO event handler to process the latest ETL file automatically.
    Workflow:
      1. Validate session and case information.
      2. Ensure chromedriver and project_root are set.
      3. Retrieve/download PDF metadata for the case.
      4. Find and download the latest ZIP attachment.
      5. Extract WiFi / DDD / BT files depending on case subcategory.
      6. Apply filtering and select the most suitable ETL file.
      7. Launch analysis (WiFi or BT tool) in a background thread.
      8. Emit progress updates to the frontend via Socket.IO.
    """

    print("✅ [trigger_run_latest_etl] Start")


    case_context = session["case_context"]
    case_context = CaseContext.from_session(case_context)
    

    # 1) Case category display
    app_config.socketio.emit("stage", {"message": f"Case Category: {case_context.subcategory}"}, namespace="/progress")
    app_config.socketio.emit("stage", {"message": fr"Attachment list: {case_context.case_nbr}"}, namespace="/progress")

    try:
        download_path = case_context.case_download_dir
        app_config.socketio.emit("case_nbr", {"name": case_context.case_nbr}, namespace="/progress")


        # 2) Ensure attachment exists
        if not case_context.attachment_list:
            app_config.socketio.emit("stage", {"message": "❌ No attachment found for this case!"})
            print("❌ No attachment here!")
            return

        if not (case_context.case_nbr and download_path):
            app_config.socketio.emit("stage", {"message": "❌ Missing session info"})
            return

        print("downloading zip...")
        # 6) Download the first ZIP file from attachment list
        zip_info = next((item for item in case_context.attachment_list if item[0].lower().endswith('.zip')), None)
        
        print("zip_info:", zip_info)
        if not zip_info:
            app_config.socketio.emit("stage", {"message": "❌ No zip file found."})
            return

        zip_name, zip_url, *_ = zip_info
        app_config.socketio.emit("stage", {"message": f"📥 Downloading {zip_name}..."})

        zip_path, name, already_dload = download_file(zip_name, zip_url, download_path, app_config.driver_manager, app_config.socketio)
        if not zip_path or not os.path.exists(zip_path):
            app_config.socketio.emit("stage", {"message": f"❌ Failed to download {zip_name}"})
            return

        # 7) Extract contents of the ZIP file
        app_config.socketio.emit("stage", {"message": f"📂 Extracting {zip_name}..."})
        wifi_files, ddd_files, bt_files, fw_files = process_single_zip(zip_path, download_path, already_dload)

        # 8) Category filtering (WiFi keeps WiFi+DDD, BT keeps BT only)
        if 'wifi' in case_context.wifi_or_bt:
            bt_files = []
        else:
            wifi_files, ddd_files = [], []

        # 9) Filter out history files
        etl_paths = wifi_files + ddd_files + bt_files
        etl_paths_filtered = [p for p in etl_paths if 'history' not in p.lower()]
        if not etl_paths_filtered:
            app_config.socketio.emit("stage", {"message": "❌ All files skipped due to 'history'"})
            return

        # 10) Prefer DDD files if available
        ddd_candidates = ddd_files
        latest_etl = None
        if ddd_candidates:
            def extract_number(f):
                nums = re.findall(r'\d+', os.path.basename(f))
                return int(nums[-1]) if nums else -1
            latest_etl = max(ddd_candidates, key=extract_number)
            app_config.socketio.emit("stage", {"message": f"🚀 Running analysis on {os.path.basename(target_etl)} (DDD)"})
        else: 
            # 11) Sorting fallback: by address digits and ETL numeric suffix
            sorted_etls = sorted(
                etl_paths_filtered,
                key=lambda x: (extract_address_digits(x), extract_etl_suffix_number(x)),
                reverse=True
            )

        # Pick the latest ETL file after sorting
        latest_etl = sorted_etls[0] if sorted_etls else None


        # 12) Run analysis depending on category
        if 'wifi' in  case_context.wifi_or_bt or ddd_candidates:
            if latest_etl:
                subprocess.run(['explorer', '/select,', latest_etl])
        
                wifi_service = WiFiAnalysisService()
                app_config.socketio.emit("stage", {"message": f"🚀 Running analysis on {os.path.basename(latest_etl)}"})
                
                wifi_service.analyze(latest_etl)

                app_config.socketio.emit("all_done", {})
                app_config.socketio.emit("stage", {"message": f"📦 Case {case_context.id} processing complete ✅"})
            else:
                app_config.socketio.emit("stage", {"message": "❌ No suitable ETL found."})
        else:
            if latest_etl:
                subprocess.run(['explorer', '/select,', latest_etl])
                app_config.socketio.emit("stage", {"message": f"🚀 Running analysis on {os.path.basename(latest_etl)}"})
                
                bt_service = BTAnalysisService()
                bt_service.analyze(latest_etl, mode="AutoFile")
                
                app_config.socketio.emit("all_done", {})
                app_config.socketio.emit("stage", {"message": f"📦 Case {case_context.id} processing complete ✅"})
            else:
                app_config.socketio.emit("stage", {"message": "❌ No suitable ETL found."})

    except Exception as e:
        app_config.socketio.emit("stage", {"message": f"❌ Exception: {str(e)}"})