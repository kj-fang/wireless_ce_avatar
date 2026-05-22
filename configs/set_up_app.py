from pathlib import Path
from threading import Thread

from configs.path_configs import (
    KEY_PATH_prim, KEY_PATH_bkup, CLASSIFY_PATH,
    LOG_PARSER_DATA_DIR_prim, LOG_PARSER_DATA_DIR_bkup,
    LOCAL_LOG_PARSER_DATA_DIR,
    SKILLS_CONFIG_DIR_prim, SKILLS_CONFIG_DIR_bkup, SKILLS_YAML_FILENAME,
    BT_SKILLS_YAML_FILENAME,
    LOCAL_SKILLS_YAML,
)
from utils import helpers
from utils.skills_yaml_utils import (
    current_active_yaml,
    refresh_local_cloud_baseline,
    set_active_source,
)
from utils.bt_skills_yaml_utils import (
    current_active_yaml as bt_current_active_yaml,
    refresh_local_cloud_baseline as bt_refresh_local_cloud_baseline,
    set_active_source as bt_set_active_source,
)
from services.llm_service import LLM_helper
from services.log_chatbot_service import WifiLogAgentSystem, sync_to_local, load_skills_from_yaml
from services.nw_analysis_service import WifiLogAgentSystem as NwAnalysisAgentSystem
from services.bt_chatbot_service import BtLogAgentSystem

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

    # Pre-warm the feedback sidecar's share probe now that
    # avatarfiles_dir is set — running this earlier (e.g. at module
    # import) would race the config and pin the local fallback to a
    # cwd-relative path instead of <avatarfiles_dir>/feedback.
    from services import feedback_service
    feedback_service.prewarm()


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

    # Load diagnostic skills into LLM_helper (shared with chatbot agent).
    #
    # Every boot:
    #   1. Refresh the local `cloud/` mirror from the share folder so the
    #      baseline tracks team-published revisions.
    #   2. Reset the active source to "cloud" — user overrides persist on
    #      disk but the agent always starts on the published baseline,
    #      matching the user-visible "Cloud baseline" badge.
    #   3. Load whichever file `current_active_yaml()` resolves to.

    skills_loaded = False
    set_active_source("cloud")
    try:
        refreshed_path, refreshed_date = refresh_local_cloud_baseline()
        if refreshed_path is not None:
            print(f"📥 Refreshed local cloud baseline → {refreshed_path} "
                  f"(date={refreshed_date})")
        else:
            print("ℹ️  Cloud baseline refresh skipped — share folder unreachable.")
    except Exception as e:
        print(f"⚠️  Cloud baseline refresh failed: {e}")

    chosen_yaml, chosen_date, chosen_source = current_active_yaml()
    if chosen_yaml is not None and chosen_yaml.exists():
        try:
            llm_helper.skills = load_skills_from_yaml(str(chosen_yaml))
            skills_loaded = True
            print(f"✅  {len(llm_helper.skills)} skills loaded from "
                  f"{chosen_source} YAML: {chosen_yaml} (date={chosen_date})")
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

        # Attach the ACE adapter so the agent reads its evolving workflow +
        # domain playbooks at generation time and can run Reflector/Curator
        # updates from feedback. Safe to skip on failure — the agent keeps
        # working without playbooks.
        try:
            from services.ace import AceRunner
            from services import feedback_service
            playbooks_root = Path(getattr(app_config, "avatarfiles_dir", ".")) / "ace_playbooks"
            ace_runner = AceRunner(
                llm=llm_helper,
                playbooks_dir=playbooks_root,
                feedback_root=feedback_service._feedback_root(),
                skills=list(llm_helper.skills.keys()) if llm_helper.skills else None,
            )
            log_chatbot_agent.attach_ace(ace_runner)
        except Exception as e:
            print(f"⚠️  ACE attach skipped: {e}")
    else:
        log_chatbot_agent = None
        print("⚠️  Log Chatbot Agent skipped — LLM client not configured (no API key).")
    app_config.set_log_chatbot_agent(log_chatbot_agent)

    # NW Analysis Agent — separate backend instance (own copy of WifiLogAgentSystem)
    if llm_helper.client is not None:
        model = getattr(llm_helper, 'model', 'gpt-4.1')
        nw_analysis_agent = NwAnalysisAgentSystem(
            client=llm_helper.client,
            model=model,
            skills=llm_helper.skills,   # reuse, no second disk read
        )
        print(f"🌐 NW Analysis Agent loaded (model={model})")
    else:
        nw_analysis_agent = None
        print("⚠️  NW Analysis Agent skipped — LLM client not configured (no API key).")
    app_config.set_nw_analysis_agent(nw_analysis_agent)

    # ------------------------------------------------------------------
    # BT Chatbot Agent — Bluetooth-flavoured log analysis chatbot
    # ------------------------------------------------------------------
    # BT skills follow the SAME user/cloud lifecycle as WiFi (mirrored on
    # share → local cloud/ at startup, user/ for hand edits) but file names
    # carry a `bt_skills_` prefix so the two domains share the same
    # skills_config sub-folders without collision.
    #
    # Loading sequence mirrors the WiFi block above:
    #   1. Reset BT active source to "cloud" on every restart.
    #   2. Refresh local `cloud/` mirror from share's bt_skills_*.yaml
    #      (best-effort; off-VPN runs simply skip this).
    #   3. Resolve and load whichever YAML `bt_current_active_yaml()`
    #      picks (user override > cloud baseline > legacy un-dated).
    #   4. If none reachable, fall back to the WiFi skills so the BT page
    #      remains usable until a BT skills YAML is published.
    bt_skills = None
    bt_set_active_source("cloud")
    try:
        bt_refreshed_path, bt_refreshed_date = bt_refresh_local_cloud_baseline()
        if bt_refreshed_path is not None:
            print(f"📥 Refreshed local BT cloud baseline → {bt_refreshed_path} "
                  f"(date={bt_refreshed_date})")
        else:
            print("ℹ️  BT cloud baseline refresh skipped — share folder unreachable.")
    except Exception as e:
        print(f"⚠️  BT cloud baseline refresh failed: {e}")

    bt_chosen_yaml, bt_chosen_date, bt_chosen_source = bt_current_active_yaml()
    if bt_chosen_yaml is not None and bt_chosen_yaml.exists():
        try:
            bt_skills = load_skills_from_yaml(str(bt_chosen_yaml))
            print(f"✅  {len(bt_skills)} BT skills loaded from "
                  f"{bt_chosen_source} YAML: {bt_chosen_yaml} (date={bt_chosen_date})")
        except Exception as e:
            print(f"⚠️  Failed to load BT skills from YAML ({e}); BT chatbot will reuse WiFi skills.")
    else:
        print("ℹ️  No BT skills YAML found (cloud/user/share all empty) — "
              "BT chatbot will reuse WiFi skills.")

    if llm_helper.client is not None:
        model = getattr(llm_helper, 'model', 'gpt-4.1')
        bt_chatbot_agent = BtLogAgentSystem(
            client=llm_helper.client,
            model=model,
            skills=bt_skills if bt_skills else llm_helper.skills,
        )
        print(f"🔵 BT Chatbot Agent loaded (model={model}, markers=ibtpci)")
    else:
        bt_chatbot_agent = None
        print("⚠️  BT Chatbot Agent skipped — LLM client not configured (no API key).")
    app_config.set_bt_chatbot_agent(bt_chatbot_agent)

    # socketio
    app_config.set_socketio(socketio)
