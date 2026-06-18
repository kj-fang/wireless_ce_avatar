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

        if 'intelavatar' in name:
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


def _app_start_lock_path() -> str:
    return os.path.join(get_user_data_dir(), 'app_start.lock')


def acquire_app_start_lock() -> bool:
    """Atomically acquire the app startup lock.

    Returns True on success (this process is the one true starter).
    Returns False if another instance is already starting up.
    The lock must be released by calling release_app_start_lock() after
    register_instance() completes.
    """
    lock_path = _app_start_lock_path()
    try:
        fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, str(os.getpid()).encode())
        os.close(fd)
        return True
    except FileExistsError:
        # Stale lock detection: read the PID from the lock file and check if
        # that process is still alive.  A previous crash may have left the lock
        # behind without ever calling release_app_start_lock().
        try:
            with open(lock_path, 'r', encoding='utf-8') as f:
                holder_pid = int(f.read().strip())
            if not psutil.pid_exists(holder_pid):
                # Holder is dead — remove stale lock and claim it ourselves.
                os.remove(lock_path)
                fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.write(fd, str(os.getpid()).encode())
                os.close(fd)
                print(f"⚠️  Removed stale app_start.lock (dead PID={holder_pid})")
                return True
        except (ValueError, FileNotFoundError, FileExistsError, OSError):
            pass
        return False
    except Exception:
        # If we can't create the lock for unexpected reasons, allow startup
        # rather than blocking everyone.
        return True


def release_app_start_lock():
    """Release the app startup lock after register_instance() completes."""
    try:
        os.remove(_app_start_lock_path())
    except Exception:
        pass


def _tray_pid_file() -> str:
    return os.path.join(get_user_data_dir(), 'tray_manager.pid')


def _tray_spawn_lock() -> str:
    return os.path.join(get_user_data_dir(), 'tray_spawn.lock')


def _is_tray_process(proc: "psutil.Process") -> bool:
    """Return True only if proc looks like an IntelAvatar tray-mode process."""
    try:
        name = (proc.name() or '').lower()
        cmdline = [a.lower() for a in (proc.cmdline() or [])]
        print(f"[TRAY CHECK] PID={proc.pid} name={name!r} cmdline={cmdline}")
        # Frozen: IntelAvatar*.exe --tray-mode (supports versioned filenames e.g. IntelAvatar_v1.2.3.exe)
        if 'intelavatar' in name and '--tray-mode' in cmdline:
            print(f"[TRAY CHECK] PID={proc.pid} → matched frozen tray")
            return True
        # Dev: python tray_manager.py
        if name in ('python.exe', 'pythonw.exe'):
            if any('tray_manager.py' in a for a in cmdline):
                print(f"[TRAY CHECK] PID={proc.pid} → matched dev tray")
                return True
    except Exception as e:
        print(f"[TRAY CHECK] PID={proc.pid} → exception reading process info: {e} (result indeterminate)")
        return False
    print(f"[TRAY CHECK] PID={proc.pid} → NOT a tray process")
    return False


def _is_tray_running() -> bool:
    """Step 1: check tray_manager.pid written by the tray process itself."""
    pid_path = _tray_pid_file()
    print(f"[TRAY RUNNING] checking pid file: {pid_path}")
    try:
        with open(pid_path, 'r', encoding='utf-8') as f:
            pid = int(f.read().strip())
        pid_exists = psutil.pid_exists(pid)
        print(f"[TRAY RUNNING] pid file contains PID={pid}, exists={pid_exists}")
        if pid_exists:
            proc = psutil.Process(pid)
            is_running = proc.is_running()
            print(f"[TRAY RUNNING] PID={pid} is_running={is_running}")
            if is_running and _is_tray_process(proc):
                print(f"[TRAY RUNNING] → tray IS running (PID={pid})")
                return True
        # Stale pid file (dead or PID reused by unrelated process) — remove it
        print(f"[TRAY RUNNING] PID={pid} is stale — removing pid file")
        os.remove(pid_path)
    except (FileNotFoundError, ValueError):
        print(f"[TRAY RUNNING] pid file not found or invalid")
    except psutil.NoSuchProcess:
        # Race condition: process exited between pid_exists() and Process()/is_running().
        # Confirmed dead — treat as stale and remove the pid file.
        print(f"[TRAY RUNNING] PID={pid} vanished during check (NoSuchProcess) — treating as stale, removing pid file")
        try:
            os.remove(pid_path)
        except Exception:
            pass
    except psutil.AccessDenied:
        # Cannot inspect the process, but the PID file was written by the tray itself.
        # Assume the tray is still running to avoid spawning a duplicate.
        print(f"[TRAY RUNNING] ⚠️ PID={pid} access denied — cannot confirm this is the Avatar tray process; assuming running to avoid duplicate spawn")
        return True
    except Exception as e:
        print(f"[TRAY RUNNING] unexpected error: {e}")
    print(f"[TRAY RUNNING] → tray is NOT running")
    return False


def ensure_tray_manager():
    """Ensure the tray manager is running, launching it if necessary."""
    # Step 1: fast check via pid file written by the tray process itself.
    print("[TRAY ENSURE] checking if tray is already running before acquiring spawn lock...")
    if _is_tray_running():
        print("ℹ️ Tray manager is already running")
        return

    # Step 2: atomic spawn lock — only one process may spawn the tray.
    # os.open with O_CREAT|O_EXCL is atomic at the OS level; exactly one
    # caller succeeds even when multiple processes race here simultaneously.
    lock_path = _tray_spawn_lock()
    try:
        fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, str(os.getpid()).encode())
        os.close(fd)
    except FileExistsError:
        # Another process is already in the middle of spawning the tray.
        # Stale lock detection: if the holder PID is dead, remove and proceed.
        try:
            with open(lock_path, 'r', encoding='utf-8') as f:
                holder_pid = int(f.read().strip())
            if not psutil.pid_exists(holder_pid):
                os.remove(lock_path)
                fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.write(fd, str(os.getpid()).encode())
                os.close(fd)
                print(f"⚠️  Removed stale tray_spawn.lock (dead PID={holder_pid})")
                # Fall through to spawn the tray below.
            else:
                print("ℹ️ Tray manager is being launched by another process")
                return
        except (ValueError, FileNotFoundError, FileExistsError, OSError):
            print("ℹ️ Tray manager is being launched by another process")
            return

    try:
        # Re-check inside the lock in case tray started between our check and lock.
        print("[TRAY ENSURE] checking if tray is already running after acquiring spawn lock...")
        if _is_tray_running():
            print("ℹ️ Tray manager is already running")
            return

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
            root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            tray_path = os.path.join(root, 'tray_manager.py')
            subprocess.Popen(
                [sys.executable, tray_path],
                creationflags=detached_flags,
                cwd=root
            )
        print("✅ Tray manager launched")
        time.sleep(1.5)  # Give the tray process time to write its pid file
    finally:
        # Always release the spawn lock.
        try:
            os.remove(lock_path)
        except Exception:
            pass


def _create_windows_shortcut(appdata_subdir, label, extra_ps_props=''):
    """Shared helper to create/refresh an IntelAvatar .lnk shortcut.

    Args:
        appdata_subdir: Relative path under %APPDATA% (e.g. r'Microsoft\\Windows\\SendTo').
        label: Human-readable label for log messages (e.g. 'Startup', 'SendTo').
        extra_ps_props: Additional PowerShell property assignments inserted before $s.Save().
    """
    if os.name != 'nt':
        return
    appdata = os.environ.get('APPDATA')
    if not appdata:
        print(f"⚠️  ensure_{label.lower()}_shortcut: APPDATA environment variable is missing — skipping shortcut creation.")
        return
    try:
        target_dir = os.path.join(appdata, appdata_subdir)
        if not os.path.isdir(target_dir):
            print(f"⚠️  ensure_{label.lower()}_shortcut: {label} folder not found ({target_dir}) — skipping shortcut creation.")
            return
        shortcut_path = os.path.join(target_dir, 'IntelAvatar.lnk')
        exe_path = sys.executable
        exe_dir = os.path.dirname(exe_path)
        icon_location = f'{exe_path},0'

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
            f'$s.IconLocation = "{_ps_str(icon_location)}"; '
            f'{extra_ps_props}'
            f'$s.Save()'
        )
        encoded_cmd = base64.b64encode(ps_script.encode('utf-16-le')).decode('ascii')
        result = subprocess.run(
            ['powershell', '-NoProfile', '-NonInteractive', '-EncodedCommand', encoded_cmd],
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
            timeout=10
        )
        if result.returncode == 0:
            print(f"✅ {label} shortcut created: {shortcut_path}")
        else:
            print(f"⚠️  {label} shortcut creation failed (exit {result.returncode})")
    except Exception as e:
        print(f"⚠️  Failed to create {label} shortcut: {e}")


def ensure_startup_shortcut():
    """Create or refresh the IntelAvatar shortcut in the Windows Startup folder.
    Only runs when frozen (packaged exe); skipped in dev mode.
    """
    if not getattr(sys, 'frozen', False):
        return
    _create_windows_shortcut(
        r'Microsoft\Windows\Start Menu\Programs\Startup',
        'Startup',
        extra_ps_props='$s.WindowStyle = 7; ',  # 7 = start minimised
    )


def ensure_sendto_shortcut(sendto_token=None):
    """Create or refresh the IntelAvatar shortcut in the Windows SendTo folder.

    Args:
        sendto_token: If provided, appended as --sendto-token argument in the shortcut.
    """
    # In dev mode sys.executable is python.exe, not IntelAvatar.exe.
    # We must prepend the app.py path to Arguments so the shortcut runs
    # `python "app.py" --sendto-token=…` instead of `python --sendto-token=…`
    # (the latter would not execute the app at all).
    if not getattr(sys, 'frozen', False):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        app_py = os.path.join(root, 'app.py').replace('"', '`"')
        script_arg = f'`"{app_py}`" '
    else:
        script_arg = ''

    extra = ''
    if sendto_token:
        safe_token = sendto_token.replace('"', '`"')
        extra = f'$s.Arguments = "{script_arg}--sendto-token={safe_token}"; '
    elif script_arg:
        extra = f'$s.Arguments = "{script_arg}"; '
    _create_windows_shortcut(r'Microsoft\Windows\SendTo', 'SendTo', extra_ps_props=extra)
