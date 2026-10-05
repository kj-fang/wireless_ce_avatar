from pathlib import Path
from threading import Thread
import shutil
from typing import Optional

from configs.path_configs import (
    KEY_PATH_prim, KEY_PATH_bkup, CLASSIFY_PATH,
    LOG_PARSER_DATA_DIR_prim, LOG_PARSER_DATA_DIR_bkup,
    LOCAL_LOG_PARSER_DATA_DIR,
    SKILLS_CONFIG_DIR_prim, SKILLS_CONFIG_DIR_bkup, SKILLS_YAML_FILENAME,
    BT_SKILLS_YAML_FILENAME,
    LOCAL_SKILLS_YAML,
    USER_KEY_LOCAL_SUBDIR,
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
from services.feedback_service import _current_user

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


def _read_token_map(module) -> dict:
    """Normalize a ``gnaigpt_token_per_user`` dict from module/file-backed config."""
    token_map = getattr(module, "gnaigpt_token_per_user", None) or {}
    if not isinstance(token_map, dict):
        return {}
    return {str(k).strip(): v for k, v in token_map.items() if v}


def _write_local_user_cache(login: str, avatarfiles_dir: str, token: str) -> None:
    """Persist a personal token into the local user cache for easy repeat use."""
    filename = f"{login}.py"
    local_dir = Path(avatarfiles_dir) / USER_KEY_LOCAL_SUBDIR
    local_dir.mkdir(parents=True, exist_ok=True)
    local_file = local_dir / filename
    content = f'gnaigpt_token_per_user = {{\n    "{login}": "{token}",\n}}\n'
    tmp = local_file.with_suffix(local_file.suffix + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    shutil.move(str(tmp), str(local_file))


def _resolve_personal_token(login: str, avatarfiles_dir: str, key_module=None) -> Optional[str]:
    """Return the current user's personal gnaigpt token, or None to fall back to the shared pool.

    Lookup order:
      1. Local `<avatarfiles_dir>/user_keys/<login>.py`.
      2. Shared `keys.py` module `gnaigpt_token_per_user` map.
      3. Share `<USER_KEY_DIR>/<login>.py` — cached to local on hit.
      4. Neither reachable → None.

    When a match is found in the shared keys module, it is written to local
    cache so local development stays ergonomic without making the keys file the
    only truth source.
    """
    if not login:
        return None
    filename = f"{login}.py"
    local_dir = Path(avatarfiles_dir) / USER_KEY_LOCAL_SUBDIR
    local_file = local_dir / filename

    if local_file.exists():
        mod = helpers.load_module(str(local_file), f"user_key_{login}")
        token = _read_token_map(mod).get(login)
        if token:
            return token

    if key_module is not None:
        token = _read_token_map(key_module).get(login)
        if token:
            try:
                _write_local_user_cache(login, avatarfiles_dir, token)
                print(f"📥 [LLM] copied personal token from keys.py → local cache: {local_file}")
            except Exception as e:
                print(f"⚠️  [LLM] failed to cache keys.py token locally: {e}")
            return token

    print(f"ℹ️  [LLM] no personal token for '{login}' in local cache or keys.py")
    return None


def _current_login() -> str:
    """Windows login (lowercased) or empty when unavailable."""
    try:
        return (_current_user() or "").strip().lower()
    except Exception:
        return ""


def _make_personal_token_expired_hook(login: str):
    """Build a fire-and-forget hook the LLM retry loop calls on personal-token 401.

    Emits a Socket.IO event on the ``/api_key_error`` namespace; the frontend
    modal listens for it. ``socketio`` is looked up lazily via ``app_config``
    because it is set later in ``set_up`` than the LLM_helper.
    """
    def _hook():
        sio = getattr(app_config, "socketio", None)
        # Polling backstop: the real-time emit below can be missed if the
        # client's socket was starved mid-request (see llm_routes.py's
        # /personal_token/pending). Set this unconditionally so the next
        # poll catches it even when nobody was connected at emit time.
        app_config.personal_token_expired_pending[login] = True
        print(f"📌 [LLM] personal_token_expired_pending['{login}'] = True")
        if sio is None:
            print("⚠️  [LLM] personal-token expiry hook fired but socketio not ready")
            return
        try:
            sio.emit(
                "personal_token_expired",
                {"login": login},
                namespace="/api_key_error",
            )
            print(f"📣 [LLM] emitted personal_token_expired for '{login}'")
        except Exception as e:
            print(f"⚠️  [LLM] socketio emit failed in expiry hook: {e}")
    return _hook


def configure_llm_personal_token(llm_helper, key_module, avatarfiles_dir: str) -> Optional[str]:
    """Wire up the LLM_helper's gnaigpt client and token pool.

    Called at boot AND from the ``/api/personal_token/update`` writeback so
    both paths share one pool-building recipe. Returns the personal token
    string (or None) so callers can log which path was taken.

    Pool ordering is:
      1. current user personal token (if present)
      2. shared common pool from ``gnaigpt_token_per_user`` in keys.py
      3. single default token as the non-pool fallback
    """
    if key_module is None:
        return None
    login = _current_login()
    personal_token = _resolve_personal_token(login, avatarfiles_dir, key_module) if login else None

    common_pool = [(label, tok) for label, tok in _read_token_map(key_module).items()]
    deduped = []
    seen = set()
    for label, token in common_pool:
        if not token or token in seen:
            continue
        seen.add(token)
        deduped.append((label, token))

    if personal_token:
        # The personal token is always first in the failover chain; any shared
        # entries reusing the same JWT are removed so a user refresh does not
        # create duplicate dead entries in the pool.
        deduped = [(lbl, tok) for (lbl, tok) in deduped if tok != personal_token]
        combined_pool = [(login, personal_token)] + deduped
        print(f"🔑 [LLM] personal gnaigpt token for user '{login}' "
              f"(then falls back to {len(deduped)} shared/common tokens on 429)")
        llm_helper.set_up(
            personal_token, key_module.gnaigpt_url, key_module.gnaigpt_model, CLASSIFY_PATH,
            token_pool=combined_pool,
            personal_token=personal_token,
            on_personal_token_expired=_make_personal_token_expired_hook(login),
        )
        return personal_token

    fallback_pool = deduped or None
    llm_helper.set_up(
        fallback_pool[0][1], key_module.gnaigpt_url, key_module.gnaigpt_model, CLASSIFY_PATH,
        token_pool=fallback_pool,
    )
    return None

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

    # Initialize driver_manager before the prewarm thread so _get_vf_session()
    # can call driver_manager.create_download_driver() without hitting NoneType.
    # This is the single initialization point — do not re-create it elsewhere
    # (a second DriverManager would re-run ChromeDriver setup on the same dir
    # and could overwrite this reference while the prewarm thread uses it).
    from services.driver_manage_service import DriverManager
    if app_config.driver_manager is None:
        app_config.set_driver_manager(DriverManager(app_config.avatarfiles_dir))

    # Pre-warm Snowflake + Salesforce VF session in background so first user request is fast
    if key_path is not None:
        Thread(target=_prewarm_connections, args=(key.snowflake_passwd,), daemon=True).start()

    
    # LLM
    llm_helper = LLM_helper()

    if key_path != None:
        # Per-user personal token override: look for the current user's
        # `<login>.py` file (local cache first, then share) which carries a
        # `gnaigpt_token_per_user = {"<login>": "<jwt>"}` dict. On hit put
        # that token at the head of the failover pool so this user's own
        # quota is spent first; on daily-cost-limit 429s the pool rotates
        # into the shared `gnaigpt_tokens` entries. Users without a personal
        # file just use the shared pool.
        configure_llm_personal_token(llm_helper, key, avatarfiles_dir)

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
            from services.ace import AceRunner, HistoryWriter
            from services.ace import sync_utils as ace_sync
            from services import feedback_service
            try:
                ace_sync.sync_at_boot()
            except Exception as e:
                print(f"⚠️  ACE playbook cloud sync skipped: {e}")
            playbooks_root = ace_sync.local_working_dir()

            def _skill_provider(sid: str):
                # Look up the skill in the agent's already-loaded skills dict
                # so ACE prompts inherit the same description / expert_rules /
                # keywords the live agent reads at chat time.
                skills = getattr(llm_helper, "skills", None) or {}
                sk = skills.get(sid)
                if sk is None:
                    return None
                try:
                    return {
                        "description": getattr(sk, "description", "") or "",
                        "expert_rules": getattr(sk, "expert_rules", "") or "",
                        "keywords": list(getattr(sk, "keywords", []) or []),
                    }
                except Exception:
                    return None

            # Local-only turn history (retained/pruned, see services/ace/history.py).
            # Pushing to the cloud share is NOT done here — that's the
            # centrally-run adapt job's job (services/ace/web/server.py),
            # so an ordinary user's live chat session never touches the SMB
            # share directly.
            ace_history = HistoryWriter(root=playbooks_root / "history")

            ace_runner = AceRunner(
                llm=llm_helper,
                playbooks_dir=playbooks_root,
                feedback_root=feedback_service._feedback_root(),
                skills=list(llm_helper.skills.keys()) if llm_helper.skills else None,
                skill_context_provider=_skill_provider,
                history=ace_history,
                # WiFi's prefix happens to be "" (legacy: filenames stayed
                # bare when the BT stream was added later), but pass it
                # explicitly to stay symmetric with the BT block below and
                # keep intent obvious if AceRunner's default ever changes.
                feedback_prefix=feedback_service._domain_prefix("wifi"),
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

        # Attach a BT-specific ACE adapter — same mechanism as the WiFi agent
        # above, but pointed at the "bt" sync namespace so BT's reflected
        # workflow/domain playbooks never mix with WiFi's (separate local
        # dir ace_playbooks_bt/local/, separate share ace_playbook_bt/).
        try:
            from services.ace import AceRunner, HistoryWriter
            from services.ace import sync_utils as ace_sync
            from services import feedback_service
            try:
                ace_sync.sync_at_boot(namespace="bt")
            except Exception as e:
                print(f"⚠️  BT ACE playbook cloud sync skipped: {e}")
            bt_playbooks_root = ace_sync.local_working_dir(namespace="bt")

            def _bt_skill_provider(sid: str):
                skills = bt_skills or getattr(llm_helper, "skills", None) or {}
                sk = skills.get(sid)
                if sk is None:
                    return None
                try:
                    return {
                        "description": getattr(sk, "description", "") or "",
                        "expert_rules": getattr(sk, "expert_rules", "") or "",
                        "keywords": list(getattr(sk, "keywords", []) or []),
                    }
                except Exception:
                    return None

            # Local-only turn history, isolated under ace_playbooks_bt/local/history/
            # (never mixed with WiFi's). Cloud push is job-level, see the WiFi
            # block above for why this stays out of the online per-turn path.
            bt_ace_history = HistoryWriter(root=bt_playbooks_root / "history")

            bt_ace_runner = AceRunner(
                llm=llm_helper,
                playbooks_dir=bt_playbooks_root,
                feedback_root=feedback_service._feedback_root(),
                skills=list((bt_skills or llm_helper.skills or {}).keys()) or None,
                skill_context_provider=_bt_skill_provider,
                history=bt_ace_history,
                # BT feedback lives in its own bt_feedback*.jsonl /
                # conversations/bt_<id>.json stream (see
                # services/feedback_service.py's domain partitioning) —
                # without this the runner would silently read WiFi's stream.
                feedback_prefix=feedback_service._domain_prefix("bt"),
            )
            bt_chatbot_agent.attach_ace(bt_ace_runner)
        except Exception as e:
            print(f"⚠️  BT ACE attach skipped: {e}")
    else:
        bt_chatbot_agent = None
        print("⚠️  BT Chatbot Agent skipped — LLM client not configured (no API key).")
    app_config.set_bt_chatbot_agent(bt_chatbot_agent)

    # socketio
    app_config.set_socketio(socketio)
