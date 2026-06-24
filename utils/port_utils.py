import psutil
import json
import os
import socket

DEFAULT_PORT = 48596


def _get_intelavatar_files_dir() -> str:
    """Return Downloads\\IntelAvatar_files, creating it if needed."""
    import winreg
    try:
        reg_key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Explorer\Shell Folders",
        )
        downloads_dir = winreg.QueryValueEx(reg_key, "{374DE290-123F-4565-9164-39C4925E467B}")[0]
        winreg.CloseKey(reg_key)
    except Exception:
        downloads_dir = os.path.join(os.path.expanduser('~'), 'Downloads')
    base = os.path.join(downloads_dir, 'IntelAvatar_files')
    os.makedirs(base, exist_ok=True)
    return base


def get_user_data_dir() -> str:
    """Return Downloads\\IntelAvatar_files\\app_state for runtime state/config files."""
    state_dir = os.path.join(_get_intelavatar_files_dir(), 'app_state')
    os.makedirs(state_dir, exist_ok=True)
    return state_dir


def get_logs_dir() -> str:
    """Return Downloads\\IntelAvatar_files\\logs for log files."""
    logs_dir = os.path.join(_get_intelavatar_files_dir(), 'logs')
    os.makedirs(logs_dir, exist_ok=True)
    return logs_dir


def get_port_config_file():
    return os.path.join(get_user_data_dir(), 'port_config.json')


def is_port_in_use(port):
    """Check if a port is in use by attempting to bind to it on all interfaces."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 0)
            s.bind(('0.0.0.0', port))
        return False
    except OSError:
        return True


def get_listening_pid_on_port(port):
    """Return PID listening on the given TCP port, or None if unavailable."""
    try:
        for conn in psutil.net_connections(kind='inet'):
            if conn.status != psutil.CONN_LISTEN:
                continue
            if not conn.laddr:
                continue
            if conn.laddr.port == port:
                return conn.pid
    except Exception:
        pass
    return None


def find_next_available_port(start_port):
    """Find next free port from start_port upward."""
    for port in range(max(1, start_port), 65536):
        if not is_port_in_use(port):
            return port
    return None


def load_persisted_default_port():
    """Load machine-local default port from port_config.json; fallback to DEFAULT_PORT."""
    config_file = get_port_config_file()
    if os.path.exists(config_file):
        try:
            with open(config_file, 'r', encoding='utf-8') as f:
                data = json.load(f)
            port = int(data.get('default_port', DEFAULT_PORT))
            if 1 <= port <= 65535:
                return port
        except Exception:
            pass
    return DEFAULT_PORT


def persist_default_port(port):
    """Persist machine-local default port to port_config.json."""
    config_file = get_port_config_file()
    try:
        with open(config_file, 'w', encoding='utf-8') as f:
            json.dump({'default_port': int(port)}, f, indent=2)
    except Exception as e:
        print(f"⚠️  Failed to persist default port: {e}")


def show_port_occupied_alert(port):
    """Show a user-visible alert when the fixed port is occupied."""
    message = f"Port {port} is already in use. IntelAvatar will not start."
    print(f"❌ {message}")


def show_port_reassigned_alert(old_port, new_port, persisted: bool = True):
    """Notify user that the port was reassigned due to conflict."""
    if persisted:
        message = (
            f"Default port {old_port} is occupied by another app.\n"
            f"IntelAvatar switched this machine default port to {new_port}."
        )
    else:
        message = (
            f"Requested port {old_port} is occupied by another app.\n"
            f"IntelAvatar will use port {new_port} for this run only."
        )
    print(f"⚠️ {message}")
