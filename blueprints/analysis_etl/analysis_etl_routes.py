from flask import Blueprint, render_template, request, session, redirect, url_for, flash, Response, jsonify
import os
import json
import traceback
from urllib.parse import unquote
import subprocess

from models.models import CaseContext
from configs.global_configs import app_config

from services.analysis_service_wifi import WiFiAnalysisService
from services.analysis_service_bt import BTAnalysisService
from services.analysis_service_fw import FWAnalysisService


analysis_etl_bp = Blueprint("analysis_etl", __name__, url_prefix="/analysis_etl")

wifi_service = WiFiAnalysisService()
bt_service = BTAnalysisService()
fw_service = FWAnalysisService()


@analysis_etl_bp.route('/process_etl_path')
def process_etl_path():
    case_context = session["case_context"]
    case_context = CaseContext.from_session(case_context)

    etl_path = unquote(request.args.get('etl_path', ''))
    mode = request.args.get('mode', '')

    print("etl_path: ", etl_path)

    if not etl_path or not os.path.exists(etl_path):
        return f"❌ Invalid file path: {etl_path}"
    
    subprocess.run(['explorer', '/select,', etl_path])

    if 'wifi' in case_context.wifi_or_bt:
        wifi_service.analyze(etl_path)
    elif 'bt' in case_context.wifi_or_bt:
        bt_service.analyze(etl_path, mode=mode)
    else:
        return "❌ Unknown case subcategory", 400
    
    return f"🚀 Analysis triggered for: {etl_path}"
    

@analysis_etl_bp.route('/process_etl_path_fw')
def process_etl_path_fw():
    case_context = session["case_context"]
    case_context = CaseContext.from_session(case_context)

    fw_path = request.args.get("fw_path")
    result = None

    subprocess.run(['explorer', '/select,', fw_path])

    if case_context.wifi_or_bt in ['wifi', 'bt']:
        result = fw_service.analyze(fw_path, case_context.wifi_or_bt)
    else:
        return "❌ Unknown case subcategory", 400
    
    return render_template("fw_analysis.html", fw_path=fw_path, result=result)
