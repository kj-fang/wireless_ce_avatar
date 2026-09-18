"""
Headless Analysis Service
==========================
Runs BT log analysis (and, eventually, other modalities) without any Flask
request/session/Socket.IO dependency, so it can be called directly from a
script, test, or an MCP tool.

Reuses the existing analysis building blocks instead of duplicating them:
  - utils.attachment_decompose.process_single_zip   - archive extraction
  - services.etl_parser.bt_parser.bt_decode_via_cli - raw BT ETL -> .hci.txt
  - services.bt_chatbot_service.BtLogAgentSystem    - the actual LLM analysis agent

Each call creates its OWN BtLogAgentSystem instance (sharing only the
already-loaded LLM client / skills from app_config), so concurrent headless
calls never interfere with each other or with the interactive browser agent
at app_config.bt_chatbot_agent.

MAINTENANCE NOTE: the orchestration below (new agent -> prime_with_context ->
chat) mirrors the /chat route in blueprints/log_chatbot/log_chatbot_routes.py.
If that route's call sequence changes, check whether this needs to follow.
"""

import os
import tempfile
from typing import Optional

from configs.global_configs import app_config
from services.bt_chatbot_service import BtLogAgentSystem
from services.etl_parser.bt_parser import bt_decode_via_cli
from utils import attachment_decompose


def _is_bt_etl(file_path: str) -> bool:
    """True for a raw (undecoded) BT ETL capture, e.g. ibtpciNNN.etl / ibtusbNNN.etl.

    Duplicated from blueprints/log_parser/log_parser_routes.py::_is_bt_etl (not
    imported, to avoid a service->blueprint dependency) - keep both in sync.
    """
    name = os.path.basename(file_path).lower()
    return name.startswith(('ibtpci', 'ibtusb')) and name.endswith('.etl')


def is_allowed_bt_input_filename(filename: str) -> bool:
    """True if `filename` is a type analyze_bt_report_headless() can accept.

    Shared by the MCP Report Ingestion Endpoint to validate an upload before
    accepting it, so the allow-list only lives in one place.
    """
    lower = os.path.basename(filename).lower()
    return (
        lower.endswith(('.zip', '.7z', '.rar', '.hci.txt'))
        or _is_bt_etl(filename)
    )


def _ensure_bt_hci_log(source_path: str, work_dir: str) -> str:
    """Normalise any supported BT input (zip / raw ETL / already-decoded
    .hci.txt) into a decoded .hci.txt path that BtLogAgentSystem can read.
    """
    lower = source_path.lower()

    if lower.endswith('.hci.txt'):
        return source_path

    if _is_bt_etl(source_path):
        etl_folder = os.path.dirname(source_path)
        hci_path = bt_decode_via_cli(etl_folder, source_path)
        if not hci_path:
            raise ValueError(f'BT HCI decode failed or timed out for: {source_path}')
        return hci_path

    if lower.endswith(('.zip', '.7z', '.rar')):
        _, _, _, bt_files, _ = attachment_decompose.process_single_zip(
            source_path, work_dir, already_downloaded=False,
        )
        if not bt_files:
            raise ValueError('No BT ETL files found in archive.')
        # bt_decode_via_cli auto-detects decode parameters from sibling files
        # next to the ETL itself (see services/analysis_service_bt.py), so it
        # must be pointed at the ETL's own folder, not the extraction root.
        etl_folder = os.path.dirname(bt_files[0])
        hci_path = bt_decode_via_cli(etl_folder, bt_files[0])
        if not hci_path:
            raise ValueError(f'BT HCI decode failed or timed out for: {bt_files[0]}')
        return hci_path

    raise ValueError(f'Unsupported BT input type: {source_path}')


def analyze_bt_report_headless(source_path: str, work_dir: Optional[str] = None) -> dict:
    """Run a full, no-UI BT log analysis and return the agent's final result.

    Args:
        source_path: Path to a .zip/.7z/.rar archive, a raw BT .etl capture,
                     or an already-decoded .hci.txt file.
        work_dir:    Directory to extract/decode into. Defaults to a fresh
                     temp directory when not given.

    Returns:
        dict: {"type": "report" | "text" | "error", "data": ...} - same shape
        as BtLogAgentSystem.chat().

    Raises:
        RuntimeError: if the LLM client isn't configured.
        ValueError:   if source_path is an unsupported/unreadable input.
    """
    if app_config.llm_helper is None or app_config.llm_helper.client is None:
        raise RuntimeError('LLM client is not configured; cannot run headless BT analysis.')
    if app_config.bt_chatbot_agent is None:
        raise RuntimeError('BT chatbot agent is not initialised (no LLM client at startup).')

    source_path = os.path.abspath(source_path)
    if not os.path.exists(source_path):
        raise ValueError(f'Input file not found: {source_path}')

    if work_dir is None:
        work_dir = tempfile.mkdtemp(prefix='bt_headless_')
    os.makedirs(work_dir, exist_ok=True)

    hci_path = _ensure_bt_hci_log(source_path, work_dir)

    # Own instance per call - never touch the shared app_config.bt_chatbot_agent,
    # so a headless run can't collide with an interactive browser session.
    agent = BtLogAgentSystem(
        client=app_config.llm_helper.client,
        model=getattr(app_config.llm_helper, 'model', 'gpt-4.1'),
        skills=app_config.bt_chatbot_agent.skills,   # reuse already-loaded skills
    )
    agent.current_log_path = hci_path
    agent.prime_with_context()

    return agent.chat(
        '🔍 Run full multi-skill analysis',
        use_tools=True,
        max_steps=6,
        temperature=0.2,
        max_tokens=4000,
    )


if __name__ == '__main__':
    # Smoke test: initialise just enough of app_config (LLM client + a
    # real-skills BtLogAgentSystem) to call analyze_bt_report_headless()
    # without going through the full Flask app_config.set_up_app.set_up().
    #
    # Usage: python -m services.headless_analysis_service <path-to-bt.zip>
    import sys

    from configs.path_configs import KEY_PATH_prim, KEY_PATH_bkup, CLASSIFY_PATH
    from utils import helpers
    from utils.bt_skills_yaml_utils import (
        current_active_yaml as bt_current_active_yaml,
        refresh_local_cloud_baseline as bt_refresh_local_cloud_baseline,
        set_active_source as bt_set_active_source,
    )
    from services.log_chatbot_service import load_skills_from_yaml

    if len(sys.argv) < 2:
        print('Usage: python -m services.headless_analysis_service <path-to-bt.zip>')
        sys.exit(1)

    key_path = helpers.get_load_path(KEY_PATH_prim, KEY_PATH_bkup)
    if key_path is None:
        print('Error: could not resolve the key module path (KEY_PATH_prim/bkup unreachable).')
        sys.exit(1)
    key = helpers.load_module(key_path, 'key_moudle')

    from services.llm_service import LLM_helper
    llm_helper = LLM_helper()
    llm_helper.set_up(
        key.gnaigpt_token_r, key.gnaigpt_url, key.gnaigpt_model, CLASSIFY_PATH,
        token_pool=getattr(key, 'gnaigpt_tokens', None),
    )
    app_config.set_llm_helper(llm_helper)

    # Load the real BT skills YAML the same way configs/set_up_app.py does,
    # so the smoke test exercises the same skill set the interactive BT
    # chatbot uses (not the built-in fallback skills).
    bt_skills = None
    bt_set_active_source('cloud')
    try:
        bt_refresh_local_cloud_baseline()
    except Exception as e:
        print(f'⚠️  BT cloud baseline refresh skipped: {e}')
    bt_chosen_yaml, bt_chosen_date, bt_chosen_source = bt_current_active_yaml()
    if bt_chosen_yaml is not None and bt_chosen_yaml.exists():
        try:
            bt_skills = load_skills_from_yaml(str(bt_chosen_yaml))
            print(f'✅  {len(bt_skills)} BT skills loaded from {bt_chosen_source} YAML: {bt_chosen_yaml}')
        except Exception as e:
            print(f'⚠️  Failed to load BT skills from YAML ({e}); falling back to built-in skills.')
    else:
        print('ℹ️  No BT skills YAML found — falling back to built-in skills.')

    app_config.set_bt_chatbot_agent(
        BtLogAgentSystem(client=llm_helper.client, model=llm_helper.model, skills=bt_skills)
    )

    print(f'🚀 Running headless BT analysis on: {sys.argv[1]}')
    outcome = analyze_bt_report_headless(sys.argv[1])
    print('\n=== RESULT ===')
    print(outcome)
