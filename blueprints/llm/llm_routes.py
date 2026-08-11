from flask import Blueprint, render_template, request, session, redirect, url_for, flash, Response, jsonify
import json
import time
import traceback

from services.llm_service import LLM_helper
from configs.global_configs import app_config
from services import gather_service

llm_bp = Blueprint("llm", __name__, url_prefix="/llm")


@llm_bp.route('/get_llm_analysis', methods=['GET'])
def get_llm_analysis():
    print(f"🤖 LLM Analysis started")
    session['classification'] = {
                "issue_type": "Unclassified",
                "confidence": 0,
                "keywords_found": []
            }
    started = time.perf_counter()
    operation_usage = LLM_helper.empty_usage()
    _ctx_full = {}
    try:
        llm_helper: LLM_helper = app_config.llm_helper
        if llm_helper != None:
            # Rehydrate from the on-disk sidecar so the LLM analysis
            # sees the comments/attachment_list payload that doesn't
            # fit in the cookie session for heavyweight cases.
            from models.models import CaseContext as _CaseContextLocal
            _ctx_full = _CaseContextLocal.from_session(
                session.get("case_context") or {}
            ).to_dict()
            ai_analysis, operation_usage = llm_helper.analyze_desc(
                prompt_path = session['prompt_file_path'],
                case_context = _ctx_full,
                return_usage = True,
            )
            if type(ai_analysis) == dict:
                session['classification'] = ai_analysis["Classification"]
        else:
            ai_analysis = "LLM helper currently not available"
        
        response_data = {
            'success': True,
            'ai_analysis': ai_analysis
        }
        print("session['classification'] ", session['classification'])
        session['ai_ips_analysis'] = ai_analysis
        if llm_helper is not None:
            try:
                feature_status = "success" if ai_analysis else "failed"
                gather_service.record_feature_usage(
                    workflow_id=session.get("gather_workflow_id", ""),
                    feature_code="select_attachments_ai_summary",
                    model=getattr(llm_helper, "model", "") or "",
                    usage=operation_usage,
                    issue=_ctx_full,
                    domain=str(_ctx_full.get("wifi_or_bt") or "wifi"),
                    trigger="click_ai",
                    status=feature_status,
                    latency_ms=int((time.perf_counter() - started) * 1000),
                    error_code="" if feature_status == "success" else "empty_result",
                )
                gather_service.record_attachment_declaration(
                    workflow_id=session.get("gather_workflow_id", ""),
                    # Hand over the raw summary so the classification, its
                    # confidence, and the sentence behind it are all recorded.
                    ai_analysis=ai_analysis,
                    source="select_attachments_ai_summary",
                    issue=_ctx_full,
                    domain=str(_ctx_full.get("wifi_or_bt") or "wifi"),
                )
            except Exception:
                pass
        return Response(
            json.dumps(response_data, ensure_ascii=False, indent=2),
            mimetype='application/json'
        )
    except Exception as e:
        try:
            llm_helper = app_config.llm_helper
            gather_service.record_feature_usage(
                workflow_id=session.get("gather_workflow_id", ""),
                feature_code="select_attachments_ai_summary",
                model=getattr(llm_helper, "model", "") if llm_helper else "",
                usage=operation_usage,
                issue=_ctx_full,
                domain=str(_ctx_full.get("wifi_or_bt") or "wifi"),
                trigger="click_ai",
                status="failed",
                latency_ms=int((time.perf_counter() - started) * 1000),
                error_code=type(e).__name__,
            )
        except Exception:
            pass
        error_traceback = traceback.format_exc()
        print(f"❌ Full traceback:\n{error_traceback}")
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500
    

