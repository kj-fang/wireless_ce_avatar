from pathlib import Path

from configs.path_configs import KEY_PATH_prim, KEY_PATH_bkup, CLASSIFY_PATH
from utils import helpers
from services.llm_service import LLM_helper

from configs.global_configs import app_config

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

    
    # LLM
    llm_helper = LLM_helper()

    if key_path != None:
        llm_helper.set_up( key.expertgpt_token, key.expertgpt_url, key.expertgpt_model, CLASSIFY_PATH)

    app_config.set_llm_helper(llm_helper)

    # socketio
    app_config.set_socketio(socketio)

