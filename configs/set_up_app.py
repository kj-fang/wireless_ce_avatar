from pathlib import Path
from threading import Thread

from configs.path_configs import KEY_PATH_prim, KEY_PATH_bkup, CLASSIFY_PATH
from utils import helpers
from services.llm_service import LLM_helper

from configs.global_configs import app_config


def _prewarm_connections(snowflake_passwd):
    """Run at startup in a background thread to pay auth costs before first user request."""
    # 1. Snowflake connection
    try:
        from services.snowflake_service import _get_connection
        _get_connection(snowflake_passwd)
        print("✅ [Prewarm] Snowflake connection ready")
    except Exception as e:
        print(f"⚠️ [Prewarm] Snowflake connection failed: {e}")

    # 2. Salesforce VF session (SSO via headless Chrome → cache cookies)
    try:
        from services.case_info_service import CaseService
        CaseService._get_vf_session()
        print("✅ [Prewarm] Salesforce VF session ready")
    except Exception as e:
        print(f"⚠️ [Prewarm] Salesforce VF session failed: {e}")

def set_up(socketio):
    # download dir
    avatarfiles_dir, driver_dir, prompt_dir = helpers.init_download_dir()
    app_config.set_avatarfiles_dir(avatarfiles_dir)
    app_config.set_driver_dir(driver_dir)
    app_config.set_prompt_dir(prompt_dir)

    # project root
    app_config.set_project_root(str(Path(__file__).parent.parent.absolute()))


    # key
    key_path = helpers.get_load_path(KEY_PATH_prim, KEY_PATH_bkup)
    if key_path != None:
        key = helpers.load_module(key_path, "key_moudle")
        app_config.set_key(key)

    # Pre-warm Snowflake + Salesforce VF session in background so first user request is fast
    if key_path is not None:
        Thread(target=_prewarm_connections, args=(key.snowflake_passwd,), daemon=True).start()

    
    # LLM
    llm_helper = LLM_helper()

    if key_path != None:
        llm_helper.set_up( key.expertgpt_token, key.expertgpt_url, key.expertgpt_model, CLASSIFY_PATH)

    app_config.set_llm_helper(llm_helper)

    # socketio
    app_config.set_socketio(socketio)

