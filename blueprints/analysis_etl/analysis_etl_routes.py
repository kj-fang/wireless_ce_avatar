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
        classification = session.get("classification", {})
        issue_type = (classification or {}).get("issue_type")
        bt_service.analyze(
            etl_path,
            mode=mode,
            issue_type=issue_type,
            wifi_or_bt=case_context.wifi_or_bt,
        )
    else:
        return "❌ Unknown case subcategory", 400
    
    return f"🚀 Analysis triggered for: {etl_path}"
    

@analysis_etl_bp.route('/process_etl_path_fw')
def process_etl_path_fw():
    case_context = session["case_context"]
    case_context = CaseContext.from_session(case_context)

    fw_path = unquote(request.args.get("fw_path", ""))

    if case_context.wifi_or_bt in ['wifi', 'bt']:
        task_id, error_msg = fw_service.start_async(fw_path, case_context.wifi_or_bt)
        if not task_id:
            return jsonify({"ok": False, "error": error_msg}), 400
    else:
        return jsonify({"ok": False, "error": "Unknown case subcategory"}), 400

    subprocess.run(['explorer', '/select,', fw_path])

    return jsonify({"ok": True, "task_id": task_id, "fw_path": fw_path})


@analysis_etl_bp.route('/cancel_fw_analysis', methods=['POST'])
def cancel_fw_analysis():
    task_id = request.args.get('task_id', '')
    if not task_id:
        body = request.get_json(silent=True) or {}
        task_id = body.get('task_id', '')

    if not task_id:
        return jsonify({"ok": False, "error": "task_id is required"}), 400

    canceled = fw_service.cancel_task(task_id)
    if not canceled:
        return jsonify({"ok": False, "error": "task not found or already finished"}), 404

    return jsonify({"ok": True, "task_id": task_id})


@analysis_etl_bp.route('/fw_analysis_result')
def fw_analysis_result():
    task_id = request.args.get('task_id', '')
    if not task_id:
        return "❌ task_id is required", 400

    task = fw_service.get_task(task_id)
    if not task:
        return f"❌ task not found: {task_id}", 404

    if task['status'] != 'completed':
        return f"❌ task is not completed yet (status={task['status']})", 400

    results = task['result']
    return render_template(
        "fw_analysis.html",
        fw_path=task['fw_path'],
        system_info=results['system_info'],
        system_text=results['system_text'],
        log=results['log']
    )
