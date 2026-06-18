from flask import Flask
from flask_socketio import SocketIO
import argparse
import json
import os
import sys
import webbrowser

# Force UTF-8 stdout/stderr so emoji print() calls don't crash on Windows
# cp1252 consoles (this is undone by cachelib/flask-session locale init).
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
if hasattr(sys.stderr, 'reconfigure'):
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')

# Install rotating-file log + stdout tee as early as possible so all
# subsequent print() calls are captured in the log file.
# Skip for --tray-mode: the tray process has no console (DETACHED_PROCESS),
# sys.stdout may be None, and the tray manager logs to its own tray.log.
if '--tray-mode' not in sys.argv:
    from configs.logger_setup import setup_file_logging as _setup_file_logging
    _log_path = _setup_file_logging()
    if _log_path:
        print(f"📝 Log file: {_log_path}")

from urllib.parse import quote
from urllib.request import ProxyHandler, Request, build_opener

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
    acquire_app_start_lock,
    release_app_start_lock,
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
from blueprints import automation_bp, main_bp, llm_bp, download_bp, analysis_etl_bp, bsod_bp, log_parser_bp, log_chatbot_bp, bt_chatbot_bp, nw_analysis_bp, feedback_bp # , attachment_bp, log_bp,
import blueprints.download.download_routes

def _bring_chrome_to_front(server_pid):
    """Bring Avatar's Chrome browser to the foreground.

    Called from the short-lived SendTo helper process which is spawned by
    File Explorer and therefore has foreground permission.

    Uses psutil to walk the Avatar server's descendant processes and find
    only the chrome.exe that belongs to Avatar's ChromeDriver — not any
    other Chrome window the user may have open.
    """
    if os.name != 'nt':
        return
    try:
        import ctypes
        import ctypes.wintypes as wt
        import psutil

        # Walk descendants of the Avatar server process to find its chrome.exe
        chrome_pids = set()
        try:
            for proc in psutil.Process(server_pid).children(recursive=True):
                try:
                    if proc.name().lower() == 'chrome.exe':
                        chrome_pids.add(proc.pid)
                except Exception:
                    pass
        except Exception:
            pass

        if not chrome_pids:
            print(f'⚠️ [SendTo] No chrome.exe children found under server PID={server_pid}')
            return

        print(f'ℹ️ [SendTo] Avatar Chrome PIDs: {chrome_pids}')

        user32 = ctypes.windll.user32
        found_hwnd = ctypes.c_size_t(0)  # pointer-sized to avoid truncation on 64-bit Windows
        WNDENUMPROC = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)

        def _cb(hwnd, _):
            cls = ctypes.create_unicode_buffer(256)
            user32.GetClassNameW(hwnd, cls, 256)
            if cls.value != 'Chrome_WidgetWin_1':
                return True
            pid = wt.DWORD(0)
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if pid.value in chrome_pids:
                found_hwnd.value = hwnd
                return False  # stop — found it
            return True

        user32.EnumWindows(WNDENUMPROC(_cb), 0)
        hwnd = found_hwnd.value
        if hwnd:
            user32.ShowWindow(hwnd, 9)          # SW_RESTORE
            user32.SetForegroundWindow(hwnd)
            print(f'✅ [SendTo] Chrome brought to front (HWND={hwnd:#010x})')
        else:
            print(f'⚠️ [SendTo] No Chrome_WidgetWin_1 found for pids={chrome_pids}')
    except Exception as e:
        print(f'⚠️ [SendTo] _bring_chrome_to_front failed: {e}')

def _build_startup_path(input_paths, sendto_token=None, is_agent_zip=False, auto_llm=False):
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
    resolved_token = sendto_token or app_config.sendto_token
    token = quote(resolved_token, safe='')
    # [DO NOT remove] - Only log the first few chars as a sanity check
    print(f"🔑 [_build_startup_path] using token: {'CLI arg=' + sendto_token[:8] + '...' if sendto_token else 'app_config=' + app_config.sendto_token[:8] + '...'}")

    url = f'/log_parser/open_local_analysis?token={token}&path={quoted_path}'
    if is_agent_zip:
        url += '&is_agent_zip=1'
    if auto_llm:
        url += '&auto_llm=1'
    print(f"🔗 Built startup path: {url}")
    print(f"🤖 [auto-llm] flag={'ON' if auto_llm else 'OFF'} → auto_send will be {'appended to redirect URL' if auto_llm else 'omitted'}")
    return url


def _navigate_existing_browser(instance_url, startup_path):
    endpoint = f"{instance_url}/log_parser/navigate_existing_browser"
    payload = json.dumps({'startup_path': startup_path}).encode('utf-8')
    request = Request(endpoint, data=payload, headers={'Content-Type': 'application/json'}, method='POST')

    # Empty ProxyHandler: without it urlopen honours HTTP_PROXY and sends this
    # loopback call to the corporate proxy, which rejects it with 403.
    opener = build_opener(ProxyHandler({}))

    try:
        with opener.open(request, timeout=5) as response:
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

    # Store sessions server-side (filesystem) so large IPS context never
    # overflows the browser's 4 KB cookie limit and causes a crash.
    from flask_session import Session
    import time as _time
    import glob as _glob
    _session_dir = os.path.join(os.path.expanduser('~'), '.intelavatar_sessions')
    os.makedirs(_session_dir, exist_ok=True)

    # Purge session files older than 24 h at startup to avoid unbounded growth.
    _SESSION_TTL_SECS = 24 * 3600
    _now = _time.time()
    for _f in _glob.glob(os.path.join(_session_dir, '*')):
        try:
            if _now - os.path.getmtime(_f) > _SESSION_TTL_SECS:
                os.remove(_f)
        except Exception:
            pass

    app.config['SESSION_TYPE'] = 'filesystem'
    app.config['SESSION_FILE_DIR'] = _session_dir
    app.config['SESSION_PERMANENT'] = False
    app.config['SESSION_USE_SIGNER'] = True   # sign the session-ID cookie for integrity
    Session(app)

    @app.context_processor
    def inject_version():
        return {
            'app_version': __version__,
            'app_build_date': BUILD_DATE,
            'app_git_hash': GIT_HASH,
            'app_git_branch': GIT_BRANCH,
        }

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
    app.register_blueprint(bt_chatbot_bp)
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
    parser.add_argument('--json', type=str, default=None, help='Optional json file path (.json / .jsonl) produced by validation AI agent.')
    parser.add_argument('--agent-zip', type=str, default=None, help='Path to a report zip produced by the validation AI agent. Reads the SendTo token from the running instance automatically.')
    parser.add_argument('--auto-llm', action='store_true', help='Automatically submit LLM analysis using the log\'s last timestamp (no user click required).')
    parser.add_argument('input_paths', nargs='*', help='Optional local analysis file paths passed from Windows SendTo.')
    args = parser.parse_args()

    # --agent-zip: let an external agent (or script) pass a report zip without
    # knowing the current SendTo token.  The token is read directly from the
    # running instance registry (running_avatar.json) so no --sendto-token arg
    # is needed.
    if args.agent_zip:
        _existing = check_already_running()
        if not _existing:
            print('⚠️ IntelAvatar is not running. Please start it first before using --agent-zip.')
            sys.exit(1)
        _instance_token = _existing.get('sendto_token', '')
        if not _instance_token:
            print('⚠️ Running instance has no sendto_token registered. Please restart IntelAvatar.')
            sys.exit(1)
        _agent_startup_path = _build_startup_path([args.agent_zip], sendto_token=_instance_token, is_agent_zip=True, auto_llm=args.auto_llm)
        _bring_chrome_to_front(_existing['pid'])
        if not _navigate_existing_browser(_existing['url'], _agent_startup_path):
            webbrowser.open(f"{_existing['url']}{_agent_startup_path}")
        sys.exit(0)

    startup_path = _build_startup_path(args.input_paths, sendto_token=args.sendto_token, auto_llm=args.auto_llm)
    
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
        if startup_path and startup_path != '/':
            # This process was launched by File Explorer (SendTo) so it has
            # foreground permission — bring Chrome to front before exiting.
            _bring_chrome_to_front(existing['pid'])
        if not _navigate_existing_browser(existing['url'], startup_path):
            webbrowser.open(f"{existing['url']}{startup_path}")
        sys.exit(0)
    # Acquire a startup lock so only one process proceeds past this point.
    # Any other instance that starts while we are in the window between
    # check_already_running() and register_instance() will hit the lock and exit.
    if not acquire_app_start_lock():
        print("\u2139\ufe0f Another IntelAvatar instance is starting up \u2014 exiting this instance.")
        existing = check_already_running()
        if existing:
            if startup_path and startup_path != '/':
                _bring_chrome_to_front(existing['pid'])
            if not _navigate_existing_browser(existing['url'], startup_path):
                webbrowser.open(f"{existing['url']}{startup_path}")
        sys.exit(0)    
    # Create / refresh the Windows Startup shortcut so the app auto-starts on login
    ensure_startup_shortcut()

    # Create / refresh the Windows SendTo shortcut for right-click Send To support
    ensure_sendto_shortcut(sendto_token=app_config.sendto_token)
    # [DO NOT remove] - Only log the first few chars as a sanity check
    print(f"🔑 SendTo token: {app_config.sendto_token[:8]}...")

    # Ensure the tray manager is running (unless explicitly disabled).
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
            if startup_path and startup_path != '/':
                _bring_chrome_to_front(listener_pid)
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
    register_instance(port, sendto_token=app_config.sendto_token)
    release_app_start_lock()  # Lock no longer needed — json is written

    print(f"🚀 IntelAvatar v{__version__} starting...")
    print(f"📅 Build: {BUILD_DATE}")
    print(f"🔖 Git: {GIT_HASH} ({GIT_BRANCH})")
    print(f"🌐 Running on port: {port}")
    print()
    
    app, socketio = create_app()
    set_up(socketio)
    app_config.set_driver_manager(DriverManager(app_config.avatarfiles_dir))

    # =========================================================================
    # Rebuild startup_path using THIS instance's token (app_config.sendto_token).
    # The CLI --sendto-token arg may be from a stale shortcut (previous run),
    # which would cause token validation to fail in open_local_analysis.
    startup_path = _build_startup_path(args.input_paths)
    # =========================================================================
    
    app_config.driver_manager.run_driver(socketio, app, port=port, startup_path=startup_path)
    
