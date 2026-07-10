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


def _is_bt_etl_file(file_path: str) -> bool:
    """Return True when an .etl file was produced by the BT driver.

    Matches the same naming rule used by ``utils/attachment_decompose.py`` to
    classify BT ETLs during archive extraction (basename starts with
    ``ibtusb-`` / ``ibtpci-`` and ends in ``.etl``). Kept local to the routes
    module so it can be used to dispatch to the right analysis service for
    Wi-Fi / BT coexistence cases where ``case_context.wifi_or_bt`` alone is
    ambiguous.
    """
    name = os.path.basename(file_path or '').lower()
    return name.startswith(('ibtusb-', 'ibtpci-')) and name.endswith('.etl')


def _run_bt_analysis(etl_path: str, mode: str, case_context: CaseContext) -> None:
    classification = session.get("classification", {})
    issue_type = (classification or {}).get("issue_type")
    bt_service.analyze(
        etl_path,
        mode=mode,
        issue_type=issue_type,
        wifi_or_bt=case_context.wifi_or_bt,
    )


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

    wifi_or_bt = (case_context.wifi_or_bt or '').lower()
    has_wifi = 'wifi' in wifi_or_bt
    has_bt = 'bt' in wifi_or_bt

    # For coexistence cases (both 'wifi' and 'bt' substrings, e.g. the
    # 'wifi_bt' value emitted by the local-upload flow) the case-level tag
    # can't tell us which parser to run — the substring check ``'wifi' in ...``
    # would always win and misroute BT ETLs into the Wi-Fi service. Dispatch
    # by the actual file name instead so each row on the download result
    # page hits the correct analyser.
    if has_wifi and has_bt:
        if _is_bt_etl_file(etl_path):
            _run_bt_analysis(etl_path, mode, case_context)
        else:
            wifi_service.analyze(etl_path)
    elif has_wifi:
        wifi_service.analyze(etl_path)
    elif has_bt:
        _run_bt_analysis(etl_path, mode, case_context)
    else:
        return "❌ Unknown case subcategory", 400
    
    return f"🚀 Analysis triggered for: {etl_path}"
    

@analysis_etl_bp.route('/process_etl_path_fw')
def process_etl_path_fw():
    case_context = session["case_context"]
    case_context = CaseContext.from_session(case_context)

    fw_path = unquote(request.args.get("fw_path", ""))

    # Accept coexistence values (e.g. 'wifi_bt') in addition to the strict
    # 'wifi' / 'bt'. FW ETLs from both technologies share the same ``wrt-fw``
    # filename prefix, so we can't dispatch by file name; instead we forward
    # the raw ``wifi_or_bt`` tag and let ``fw_service`` (which already uses
    # substring checks internally) pick a branch. For 'wifi_bt' that defaults
    # to the Wi-Fi FW path, matching the existing substring behaviour.
    wifi_or_bt = (case_context.wifi_or_bt or '').lower()
    is_coex = 'wifi' in wifi_or_bt and 'bt' in wifi_or_bt
    if wifi_or_bt in ('wifi', 'bt') or is_coex:
        task_id, error_msg = fw_service.start_async(fw_path, wifi_or_bt)
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
