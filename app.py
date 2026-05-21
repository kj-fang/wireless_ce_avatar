from flask import Flask
from flask_socketio import SocketIO
import argparse
import json
import os
import sys
import webbrowser
from urllib.parse import quote
from urllib.request import Request, urlopen

from utils.port_utils import (
    is_port_in_use,
    get_listening_pid_on_port,
    find_next_available_port,
    load_persisted_default_port,
    persist_default_port,
    show_port_occupied_alert,
    show_port_reassigned_alert,
)
from utils.instance_utils import (
    is_intelavatar_process,
    check_already_running,
    register_instance,
    ensure_tray_manager,
    ensure_startup_shortcut,
    ensure_sendto_shortcut,
)

from services.driver_manage_service import DriverManager
from configs.set_up_app import set_up
from configs.global_configs import app_config
from configs.version import __version__, BUILD_DATE, GIT_HASH, GIT_BRANCH

#from blueprints.main import main_bp
#from blueprints.attachment import attachment_bp
#from blueprints.log_analysis import log_bp
from blueprints import automation_bp, main_bp, llm_bp, download_bp, analysis_etl_bp, bsod_bp, log_parser_bp, log_chatbot_bp, nw_analysis_bp, feedback_bp # , attachment_bp, log_bp,
import blueprints.download.download_routes


def _build_startup_path(input_paths, sendto_token=None):
    if not input_paths:
        return '/'

    supported_paths = []
    for input_path in input_paths:
        if not input_path:
            continue
        normalized_path = os.path.abspath(input_path)
        if not os.path.exists(normalized_path):
            print(f"⚠️ Ignoring missing SendTo path: {normalized_path}")
            continue

        lower_name = os.path.basename(normalized_path).lower()
        if lower_name.endswith('.zip') or lower_name.endswith('.7z') or lower_name.endswith('.rar') or lower_name.endswith('.log') or lower_name.endswith('.etl') or lower_name.endswith('.dmp') or '.etl.' in lower_name:
            supported_paths.append(normalized_path)
        else:
            print(f"⚠️ Ignoring unsupported SendTo path: {normalized_path}")

    if not supported_paths:
        return '/'

    if len(supported_paths) > 1:
        print(f"⚠️ Multiple SendTo files were provided; only the first one will be used: {supported_paths[0]}")

    quoted_path = quote(supported_paths[0], safe='')
    # Use CLI token from shortcut (for existing instance) or current app token (for fresh start)
    token = quote(sendto_token or app_config.sendto_token, safe='')
    return f'/log_parser/open_local_analysis?token={token}&path={quoted_path}'


def _navigate_existing_browser(instance_url, startup_path):
    endpoint = f"{instance_url}/log_parser/navigate_existing_browser"
    payload = json.dumps({'startup_path': startup_path}).encode('utf-8')
    request = Request(endpoint, data=payload, headers={'Content-Type': 'application/json'}, method='POST')

    try:
        with urlopen(request, timeout=5) as response:
            return 200 <= response.status < 300
    except Exception as error:
        print(f"⚠️ Failed to reuse existing browser: {error}")
        return False

def create_app():
    app = Flask(__name__)
    # Preserve dict insertion order in jsonify responses. The legacy
    # `JSON_SORT_KEYS` config flag was removed in Flask 2.2+ in favour of
    # the JSON provider attribute below; setting only the legacy flag
    # silently sorted skill YAMLs alphabetically on round-trip.
    app.config['JSON_SORT_KEYS'] = False
    try:
        app.json.sort_keys = False
    except AttributeError:
        # Older Flask (<2.2) — the config flag above is the only knob.
        pass
    app.secret_key = 'autoparselog2025'
    socketio = SocketIO(app, async_mode='threading', cors_allowed_origins="*")

    # Register blueprints

    app.register_blueprint(main_bp)
    app.register_blueprint(analysis_etl_bp)
    app.register_blueprint(download_bp)
    app.register_blueprint(llm_bp)
    app.register_blueprint(automation_bp)
    app.register_blueprint(bsod_bp)
    app.register_blueprint(log_parser_bp)
    app.register_blueprint(log_chatbot_bp)
    app.register_blueprint(nw_analysis_bp)
    app.register_blueprint(feedback_bp)

    # Register socketio
    blueprints.download.download_routes.register_socketio_handlers(socketio)
    blueprints.log_parser.log_parser_routes.register_socketio_handlers(socketio)
    blueprints.automation.automation_routes.register_socketio_handlers(socketio)

    print("📋 Registered routes:")
    for rule in app.url_map.iter_rules():
        print(f"  {rule.endpoint}: {rule.rule}")

    return app, socketio


if __name__ == "__main__":
    # Parse command-line arguments
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', type=int, default=None, help='Override the port for this run only (not persisted). Defaults to the machine-local persisted port (initially 48596).')
    parser.add_argument('--no-tray', action='store_true', help='Disable tray manager')
    parser.add_argument('--tray-mode', action='store_true', help='Run as tray manager')
    parser.add_argument('--sendto-token', type=str, default=None, help='SendTo security token (auto-set by shortcut, not for manual use).')
    parser.add_argument('input_paths', nargs='*', help='Optional local analysis file paths passed from Windows SendTo.')
    args = parser.parse_args()
    startup_path = _build_startup_path(args.input_paths, sendto_token=args.sendto_token)
    
    # Check whether to run in tray mode
    if args.tray_mode:
        from tray_manager import TrayManager
        manager = TrayManager()
        manager.run()
        sys.exit(0)

    # Single-instance enforcement (must happen BEFORE shortcut/tray setup so that
    # short-lived SendTo instances don't overwrite the .lnk with a new token)
    existing = check_already_running()
    if existing:
        print(f"✅ Avatar is already running at {existing.get('url')} — opening in browser.")
        if not _navigate_existing_browser(existing['url'], startup_path):
            webbrowser.open(f"{existing['url']}{startup_path}")
        sys.exit(0)
    
    # Create / refresh the Windows Startup shortcut so the app auto-starts on login
    ensure_startup_shortcut()

    # Create / refresh the Windows SendTo shortcut for right-click Send To support
    ensure_sendto_shortcut(sendto_token=app_config.sendto_token)
    print(f"🔑 SendTo token: {app_config.sendto_token[:8]}...")

    # Ensure the tray manager is running (unless explicitly disabled)
    if not args.no_tray:
        ensure_tray_manager()

    # Port decision policy:
    # 1) no one uses preferred port -> use it
    # 2) IntelAvatar uses preferred port -> open existing and exit
    # 3) other app uses preferred port -> pick a new free port, persist as machine default, then start
    if args.port is not None:
        preferred_port = args.port
        print(f"ℹ️ Using explicit --port {preferred_port} for this run.")
    else:
        preferred_port = load_persisted_default_port()

    if is_port_in_use(preferred_port):
        # Port is occupied — try to identify whether it's IntelAvatar or another process.
        listener_pid = get_listening_pid_on_port(preferred_port)
        if listener_pid and is_intelavatar_process(listener_pid):
            print(f"✅ IntelAvatar is already running on port {preferred_port} (PID={listener_pid}).")
            existing_url = f'http://127.0.0.1:{preferred_port}'
            if not _navigate_existing_browser(existing_url, startup_path):
                webbrowser.open(f'{existing_url}{startup_path}')
            sys.exit(0)
        else:
            # Either another process owns the port, or we couldn't determine the owner.
            new_port = find_next_available_port(preferred_port + 1)
            if new_port is None:
                show_port_occupied_alert(preferred_port)
                sys.exit(1)
            will_persist = args.port is None
            show_port_reassigned_alert(preferred_port, new_port, persisted=will_persist)
            if will_persist:
                persist_default_port(new_port)
            port = new_port
    else:
        port = preferred_port
        if args.port is None:
            persist_default_port(port)
    
    # Register this instance so single-instance enforcement works regardless of tray usage
    register_instance(port)

    print(f"🚀 IntelAvatar v{__version__} starting...")
    print(f"📅 Build: {BUILD_DATE}")
    print(f"🔖 Git: {GIT_HASH} ({GIT_BRANCH})")
    print(f"🌐 Running on port: {port}")
    print()
    
    app, socketio = create_app()
    set_up(socketio)
    app_config.set_driver_manager(DriverManager(app_config.avatarfiles_dir))
    
    app_config.driver_manager.run_driver(socketio, app, port=port, startup_path=startup_path)
    
