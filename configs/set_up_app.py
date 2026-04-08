from pathlib import Path
from threading import Thread

from configs.path_configs import (
    KEY_PATH_prim, KEY_PATH_bkup, CLASSIFY_PATH,
    LOG_PARSER_DATA_DIR_prim, LOG_PARSER_DATA_DIR_bkup,
    LOCAL_LOG_PARSER_DATA_DIR,
    SKILLS_CONFIG_DIR_prim, SKILLS_CONFIG_DIR_bkup, SKILLS_YAML_FILENAME,
    LOCAL_SKILLS_YAML,
)
from utils import helpers
from services.llm_service import LLM_helper
from services.log_chatbot_service import WifiLogAgentSystem, sync_to_local, load_skills_from_yaml

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
        llm_helper.set_up( key.gnaigpt_token, key.gnaigpt_url, key.gnaigpt_model, CLASSIFY_PATH)
        #llm_helper.set_up( key.expertgpt_token, key.expertgpt_url, key.expertgpt_model, CLASSIFY_PATH)

    app_config.set_llm_helper(llm_helper)

    # Load diagnostic skills into LLM_helper (shared with chatbot agent)
    # Priority: Shared YAML → Local cache (prompt/filter dirs)
    
    # Step 1: Try to load from shared YAML location
    skills_yaml_shared = helpers.get_load_path(
        str(Path(SKILLS_CONFIG_DIR_prim) / SKILLS_YAML_FILENAME),
        str(Path(SKILLS_CONFIG_DIR_bkup) / SKILLS_YAML_FILENAME)
    )
    
    skills_loaded = False
    if skills_yaml_shared and Path(skills_yaml_shared).exists():
        try:
            print(f"📦 Loading skills from shared YAML: {skills_yaml_shared}")
            llm_helper.skills = load_skills_from_yaml(skills_yaml_shared)
            skills_loaded = True
            print(f"✅  {len(llm_helper.skills)} skills loaded from YAML")
        except Exception as e:
            print(f"⚠️  Failed to load skills from YAML: {e}")
    
    # Step 2: Fallback to directory-based loading (prompt/filter dirs)
    if not skills_loaded:
        local_data_dir  = Path(LOCAL_LOG_PARSER_DATA_DIR)
        local_has_data  = ((local_data_dir / "prompt").exists() and
                           (local_data_dir / "filter").exists())

        data_dir = None
        if local_has_data:
            print(f"🗂️  Using local skill cache: {LOCAL_LOG_PARSER_DATA_DIR}")
            data_dir = LOCAL_LOG_PARSER_DATA_DIR
        # else:
        #     print("🔄  Local cache missing — syncing from remote shared folder...")
        #     remote_dir = helpers.get_load_path(LOG_PARSER_DATA_DIR_prim, LOG_PARSER_DATA_DIR_bkup)
        #     if remote_dir:
        #         sync_to_local(remote_dir, LOCAL_LOG_PARSER_DATA_DIR)
        #         data_dir = LOCAL_LOG_PARSER_DATA_DIR   # use local after sync
        #     else:
        #         print("⚠️  Remote shared folder also unreachable — no skills will be loaded.")

        llm_helper.load_skills(data_dir)

    # Log Chatbot Agent — loaded at startup, reuses skills already in llm_helper
    if llm_helper.client is not None:
        model = getattr(llm_helper, 'model', 'gpt-4.1')
        log_chatbot_agent = WifiLogAgentSystem(
            client=llm_helper.client,
            model=model,
            skills=llm_helper.skills,   # reuse, no second disk read
        )
        print(f"🤖 Log Chatbot Agent loaded (model={model})")
    else:
        log_chatbot_agent = None
        print("⚠️  Log Chatbot Agent skipped — LLM client not configured (no API key).")
    app_config.set_log_chatbot_agent(log_chatbot_agent)

    # socketio
    app_config.set_socketio(socketio)
