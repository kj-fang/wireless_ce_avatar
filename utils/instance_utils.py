import psutil
import json
import os
import sys
import subprocess
import time
import base64

from utils.port_utils import get_user_data_dir


def is_intelavatar_process(pid):
    """Best-effort check whether PID belongs to IntelAvatar app process."""
    if not pid:
        return False
    try:
        proc = psutil.Process(pid)
        name = (proc.name() or '').lower()
        cmdline = [part.lower() for part in (proc.cmdline() or [])]

        if name == 'intelavatar.exe':
            return True

        if name in ('python.exe', 'pythonw.exe'):
            # Dev mode app process
            if any('app.py' in part for part in cmdline):
                return True

        # Fallback: command contains intelavatar executable/script path
        if any('intelavatar' in part and ('exe' in part or 'app.py' in part) for part in cmdline):
            return True
    except Exception:
        return False
    return False


def check_already_running():
    """
    Check whether an Avatar instance is already running.
    Returns the existing instance dict if found, otherwise None.
    """
    filepath = os.path.join(get_user_data_dir(), 'running_avatar.json')
    if not os.path.exists(filepath):
        return None

    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            instance = json.load(f)
        pid = instance.get('pid')
        port = instance.get('port')
        # Verify the process is actually still alive
        if pid and psutil.pid_exists(pid):
            proc = psutil.Process(pid)
            if proc.is_running() and is_intelavatar_process(pid):
                print(f"ℹ️  Avatar already running (PID={pid}, Port={port})")
                return instance
            else:
                # Stale/foreign PID in instance file – remove it
                os.remove(filepath)
        else:
            # Stale file – remove it
            os.remove(filepath)
    except Exception:
        pass
    return None


def register_instance(port):
    """Register this instance by writing running_avatar.json (always, regardless of tray usage)"""
    pid = os.getpid()
    instance_file = os.path.join(get_user_data_dir(), 'running_avatar.json')

    instance = {
        'pid': pid,
        'port': port,
        'url': f'http://127.0.0.1:{port}',
        'started_at': time.time()
    }

    try:
        with open(instance_file, 'w', encoding='utf-8') as f:
            json.dump(instance, f, indent=2, ensure_ascii=False)
        print(f"✅ Instance registered (Port: {port}, PID: {pid})")
        print(f"   Instance file: {instance_file}")
    except Exception as e:
        print(f"⚠️  Registration failed: {e}")


def ensure_tray_manager():
    """Ensure the tray manager is running, launching it if necessary."""
    tray_running = False
    for proc in psutil.process_iter(['name', 'cmdline']):
        try:
            name    = (proc.info['name'] or '').lower()
            cmdline = proc.info['cmdline'] or []
            # Frozen:  IntelAvatar.exe --tray-mode
            if name == 'intelavatar.exe' and '--tray-mode' in cmdline:
                tray_running = True
                break
            # Dev:     python tray_manager.py  (no --tray-mode flag)
            if name in ('python.exe', 'pythonw.exe'):
                if any('tray_manager.py' in arg.lower() for arg in cmdline):
                    tray_running = True
                    break
        except Exception:
            pass

    if not tray_running:
        detached_flags = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
        detached_flags |= getattr(subprocess, 'DETACHED_PROCESS', 0)
        detached_flags |= getattr(subprocess, 'CREATE_NEW_PROCESS_GROUP', 0)

        if getattr(sys, 'frozen', False):
            # Packaged: launch a copy of itself in tray mode
            subprocess.Popen(
                [sys.executable, '--tray-mode'],
                creationflags=detached_flags
            )
        else:
            # Development: run tray_manager.py directly
            # Go up one level from utils/ to reach the project root
            root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            tray_path = os.path.join(root, 'tray_manager.py')
            subprocess.Popen(
                [sys.executable, tray_path],
                creationflags=detached_flags,
                cwd=root
            )
        print("✅ Tray manager launched")
        time.sleep(1.5)  # Give it a moment to create its tray icon
    else:
        print("ℹ️ Tray manager is already running")


def ensure_startup_shortcut():
    """Create or refresh the IntelAvatar shortcut in the Windows Startup folder.
    Only runs when frozen (packaged exe); skipped in dev mode.
    """
    if not getattr(sys, 'frozen', False) or os.name != 'nt':
        return
    appdata = os.environ.get('APPDATA')
    if not appdata:
        print("⚠️  ensure_startup_shortcut: APPDATA environment variable is missing — skipping shortcut creation.")
        return
    try:
        startup_dir = os.path.join(
            appdata,
            r'Microsoft\Windows\Start Menu\Programs\Startup'
        )
        if not os.path.isdir(startup_dir):
            print(f"⚠️  ensure_startup_shortcut: Startup folder not found ({startup_dir}) — skipping shortcut creation.")
            return
        shortcut_path = os.path.join(startup_dir, 'IntelAvatar.lnk')
        exe_path = sys.executable
        exe_dir  = os.path.dirname(exe_path)
        icon_path = os.path.join(exe_dir, 'icon.ico')

        # Remove existing shortcut so the target is always up-to-date
        if os.path.exists(shortcut_path):
            os.remove(shortcut_path)

        # Build shortcut via PowerShell WScript.Shell (no extra dependencies).
        # Paths are escaped and the script is base64-encoded (-EncodedCommand)
        # to prevent injection if any path contains special/quote characters.
        def _ps_str(s: str) -> str:
            return s.replace('"', '`"')

        ps_script = (
            f'$s = (New-Object -COM WScript.Shell).CreateShortcut("{_ps_str(shortcut_path)}"); '
            f'$s.TargetPath = "{_ps_str(exe_path)}"; '
            f'$s.WorkingDirectory = "{_ps_str(exe_dir)}"; '
            f'$s.IconLocation = "{_ps_str(icon_path)}"; '
            f'$s.WindowStyle = 7; '   # 7 = start minimised
            f'$s.Save()'
        )
        encoded_cmd = base64.b64encode(ps_script.encode('utf-16-le')).decode('ascii')
        result = subprocess.run(
            ['powershell', '-NoProfile', '-NonInteractive', '-EncodedCommand', encoded_cmd],
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
            timeout=10
        )
        if result.returncode == 0:
            print(f"✅ Startup shortcut created: {shortcut_path}")
        else:
            print(f"⚠️  Startup shortcut creation failed (exit {result.returncode})")
    except Exception as e:
        print(f"⚠️  Failed to create startup shortcut: {e}")
