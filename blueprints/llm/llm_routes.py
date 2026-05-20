from flask import Blueprint, render_template, request, session, redirect, url_for, flash, Response, jsonify
import json
import traceback

from services.llm_service import LLM_helper
from configs.global_configs import app_config

llm_bp = Blueprint("llm", __name__, url_prefix="/llm")


@llm_bp.route('/get_llm_analysis', methods=['GET'])
def get_llm_analysis():
    print(f"🤖 LLM Analysis started")
    session['classification'] = {
                "issue_type": "Unclassified",
                "confidence": 0,
                "keywords_found": []
            }
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
            ai_analysis = llm_helper.analyze_desc(
                prompt_path = session['prompt_file_path'],
                case_context = _ctx_full
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
        return Response(
            json.dumps(response_data, ensure_ascii=False, indent=2),
            mimetype='application/json'
        )
    except Exception as e:
        error_traceback = traceback.format_exc()
        print(f"❌ Full traceback:\n{error_traceback}")
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500
    

