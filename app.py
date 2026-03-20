from flask import Flask
from flask_socketio import SocketIO
import argparse
import sys

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
)

from services.driver_manage_service import DriverManager
from configs.set_up_app import set_up
from configs.global_configs import app_config
from configs.version import __version__, BUILD_DATE, GIT_HASH, GIT_BRANCH

#from blueprints.main import main_bp
#from blueprints.attachment import attachment_bp
#from blueprints.log_analysis import log_bp
from blueprints import automation_bp, main_bp, llm_bp, download_bp, analysis_etl_bp, bsod_bp, log_parser_bp, log_chatbot_bp # , attachment_bp, log_bp, 
import blueprints.download.download_routes

def create_app():
    app = Flask(__name__)
    app.config['JSON_SORT_KEYS'] = False
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
    args = parser.parse_args()
    
    # Check whether to run in tray mode
    if args.tray_mode:
        from tray_manager import TrayManager
        manager = TrayManager()
        manager.run()
        sys.exit(0)
    
    # Create / refresh the Windows Startup shortcut so the app auto-starts on login
    ensure_startup_shortcut()

    # Ensure the tray manager is running (unless explicitly disabled)
    if not args.no_tray:
        ensure_tray_manager()

    # Single-instance enforcement
    existing = check_already_running()
    if existing:
        print(f"✅ Avatar is already running at {existing.get('url')} — opening in browser.")
        import webbrowser
        webbrowser.open(existing['url'])
        sys.exit(0)

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
            import webbrowser
            webbrowser.open(f'http://127.0.0.1:{preferred_port}')
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
    
    app_config.driver_manager.run_driver(socketio, app, port=port)
    
