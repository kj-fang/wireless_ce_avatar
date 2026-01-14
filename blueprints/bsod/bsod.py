from flask import Blueprint, render_template, request, session, redirect, url_for, flash, Response, jsonify
import json
import requests

from utils import helpers
from configs.global_configs import app_config
from models.models import CaseContext

bsod_bp = Blueprint("bsod", __name__, url_prefix="/bsod")


@bsod_bp.route('/bsod_submit', methods=['POST', 'GET'])
def bsod_submit():
    #bsod_number = request.form.get('bsod_number', '').strip()
    print("bsod_submit")
    case_context = session["case_context"]
    case_context = CaseContext.from_session(case_context)

    email = helpers.detect_user_email()
    if not email:
        return "❌ Unable to retrieve email. Please enter manually."
    

    bsod_nbr = case_context.backend_id
    if "-" not in str(bsod_nbr):
        bsod_nbr = case_context.case_nbr


    # ✅ Combine DUMPS_PATH
    #dumps_path = rf"{SHARED_FOLDER_BSOD}\{bsod_nbr}"
    dumps_path = session['download_path']

    print("dumps path:", dumps_path)

    key = app_config.key

    headers = {
        "Content-Type": "application/json",
        "X-Api-Token": key.potatofarm_api
    }
    payload = {
        "DUMPS_PATH": dumps_path,
        "EMAIL_LIST": email
    }

      # ✅ Debug output
    print("📧 	User email: ", email)
    print("📦 Assembled payload: ", json.dumps(payload, indent=2))
    url = key.potatofarm_url

    try:
        response = requests.post(url, data=json.dumps(payload), proxies={"http": None, "https": None}, headers=headers, verify=False)
        if response.status_code == 200:
            flash("✅ BSOD Analysis Form Submitted Successfully!", "success")
            print("submit bsod success!")
            return redirect(url_for('main.download_result_bsod'))
        else:
            print("submit bsod fail!")
            return f"❌ Failed to call BSOD API：{response.status_code}<br>{response.text}"
        
    except requests.exceptions.RequestException as e:
        print("submit bsod error!", e)
        return f"⚠️ An error occurred during API call：{e}"