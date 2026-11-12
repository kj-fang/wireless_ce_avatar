"""
One-click start for Beacon (handsfree replyer) testing (dev mode, live source).

Starts the Avatar app from this checkout when it is not already running,
waits for it to boot, then points the app's own Chrome window at the
/handsfree review page and brings it to the front. When the app is already
running it only does the last step.

Run with the app's Python environment (the intel_ava venv), from anywhere:

    python tools/handsfree_start.py                     # start / focus (client)
    python tools/handsfree_start.py --server            # start / focus as the Handsfree SERVER
    python tools/handsfree_start.py --install-shortcut  # both Desktop shortcuts

Client (default): the review queue, Run now and Approve; the schedule is
read-only. Server (--server, i.e. app.py --handsfree-role server): the same
page plus "Start automatic analysis" — the scheduled rounds run inside this
app and show in the live panel. One role per app process: to switch, close
the app's console window and start again. While the in-app schedule is ON,
disable the Task Scheduler job that runs tools/handsfree_server_run.py (same
Chrome sign-in profile, duplicate rounds).

The app runs in its own (minimized) console window — close that window
to stop it. Nothing is analyzed or posted by starting: use "Check now"
in the page as usual.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from urllib.request import ProxyHandler, Request, build_opener

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

# The app's helpers print emoji; a cp1252 console would crash on them.
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

START_PATH = "/handsfree"
SHORTCUT_NAME = "Beacon (dev).lnk"
SERVER_SHORTCUT_NAME = "Beacon Server (dev).lnk"
OLD_SHORTCUT_NAMES = ("Handsfree Replyer (dev).lnk",)   # removed on --install-shortcut
REGISTER_TIMEOUT_S = 60     # instance file is written before the heavy boot
BOOT_TIMEOUT_S = 360        # set_up(): key share, ChromeDriver, skills, agents


def _post_navigate(base_url: str) -> bool:
    """Ask the running app to load START_PATH in its own Chrome window.
    False while the server is not listening / its browser is not open yet."""
    req = Request(f"{base_url}/log_parser/navigate_existing_browser",
                  data=json.dumps({"startup_path": START_PATH}).encode("utf-8"),
                  headers={"Content-Type": "application/json"}, method="POST")
    # Empty ProxyHandler: the ambient HTTP_PROXY must not see loopback calls.
    try:
        with build_opener(ProxyHandler({})).open(req, timeout=5) as resp:
            return 200 <= resp.status < 300
    except Exception:
        return False


def _bring_chrome_to_front(server_pid: int) -> None:
    """Best effort: raise the Chrome window owned by the app's ChromeDriver."""
    try:
        import ctypes
        import ctypes.wintypes as wt
        import psutil

        pids = {p.pid for p in psutil.Process(server_pid).children(recursive=True)
                if p.name().lower() == "chrome.exe"}
        if not pids:
            return
        user32 = ctypes.windll.user32
        found = ctypes.c_size_t(0)
        enum_proc = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)

        def _cb(hwnd, _):
            cls = ctypes.create_unicode_buffer(256)
            user32.GetClassNameW(hwnd, cls, 256)
            pid = wt.DWORD(0)
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if (cls.value == "Chrome_WidgetWin_1" and pid.value in pids
                    and user32.IsWindowVisible(hwnd)):
                found.value = hwnd
                return False
            return True

        user32.EnumWindows(enum_proc(_cb), 0)
        if found.value:
            user32.ShowWindow(wt.HWND(found.value), 9)      # SW_RESTORE
            user32.SetForegroundWindow(wt.HWND(found.value))
    except Exception:
        pass


def _start_server(role: str = "client") -> subprocess.Popen:
    env = dict(os.environ, PYTHONUTF8="1")
    si = subprocess.STARTUPINFO()
    si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    si.wShowWindow = 7                                      # SW_SHOWMINNOACTIVE
    return subprocess.Popen(
        [sys.executable, "app.py", "--no-tray", "--handsfree-role", role],
        cwd=str(REPO_ROOT), env=env,
        creationflags=subprocess.CREATE_NEW_CONSOLE, startupinfo=si)


def _running_role(url: str) -> str:
    """The Handsfree role of the app already running at url ("" when it
    cannot be asked: older build, still booting)."""
    try:
        req = Request(url + "/handsfree/auto")
        with build_opener(ProxyHandler({})).open(req, timeout=5) as r:
            return str(json.loads(r.read().decode("utf-8")).get("role") or "")
    except Exception:
        return ""


def _checkout_banner() -> None:
    """Which code this is: branch and commit (best effort), and whether app.py
    knows the role flag at all — a checkout without server mode fails later
    with an argparse error in a console window that closes at once."""
    try:
        branch = subprocess.run(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=REPO_ROOT,
                                capture_output=True, text=True, timeout=10).stdout.strip()
        commit = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=REPO_ROOT,
                                capture_output=True, text=True, timeout=10).stdout.strip()
        print(f"Checkout: {REPO_ROOT} ({branch} @ {commit})")
    except Exception:
        print(f"Checkout: {REPO_ROOT}")


def _app_knows_role() -> bool:
    try:
        return "--handsfree-role" in (REPO_ROOT / "app.py").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False


def start(role: str = "client") -> int:
    from utils.instance_utils import check_already_running
    from utils.port_utils import get_logs_dir

    os.chdir(REPO_ROOT)
    _checkout_banner()
    if role == "server" and not _app_knows_role():
        print("This checkout has no Handsfree server mode (app.py does not take --handsfree-role): "
              "check out the branch that has it (handsfree/server-ui or later) and pull.")
        return 1
    inst = check_already_running()
    proc = None
    if inst:
        print(f"Avatar is already running at {inst['url']}.")
        running = _running_role(inst["url"])
        # An instance that does not report a role is an older build: for a
        # client that is fine (same page), for the server it is not.
        if (running and running != role) or (role == "server" and running != "server"):
            what = (f"as the Handsfree {running.upper()}" if running
                    else "an older build without Handsfree server mode")
            print(f"It runs {what}, not as the {role} you asked for. "
                  "The role is fixed per process: stop that Avatar first (close its console "
                  "window, or its tray icon > Exit), then run this again"
                  f"{' with --server' if role == 'server' else ' without --server'}.")
            return 1
    else:
        print(f"Starting Avatar from {REPO_ROOT} as the Handsfree {role} ...")
        proc = _start_server(role)
        deadline = time.time() + REGISTER_TIMEOUT_S
        while inst is None and time.time() < deadline:
            if proc.poll() is not None:
                break
            time.sleep(1)
            inst = check_already_running()
        if inst is None:
            print("The app did not start"
                  + (f" (exit code {proc.returncode})" if proc.poll() is not None else "")
                  + f". See the newest log in {get_logs_dir()}")
            return 1
        print(f"Server process up (port {inst['port']}); waiting for boot "
              "(key share, ChromeDriver, skills) ...")

    deadline = time.time() + BOOT_TIMEOUT_S
    waited = 0
    while not _post_navigate(inst["url"]):
        if proc is not None and proc.poll() is not None:
            print(f"The app exited during boot (exit code {proc.returncode}). "
                  f"See the newest log in {get_logs_dir()}")
            return 1
        if time.time() > deadline:
            print(f"Timed out waiting for {inst['url']} — the server console "
                  "window (minimized) shows what it is doing.")
            return 1
        time.sleep(2)
        waited += 2
        if waited % 20 == 0:
            print(f"  still booting ... {waited}s")
    if proc is not None:
        # The app loads its index page right after opening Chrome; if our
        # first navigate landed before that, repeat it once it has settled.
        time.sleep(3)
        _post_navigate(inst["url"])
    _bring_chrome_to_front(int(inst["pid"]))
    print(f"Handsfree page open: {inst['url']}{START_PATH}")
    return 0


def shortcut_script() -> str:
    """PowerShell that writes both Desktop shortcuts (client and server) that
    run this script with the current interpreter."""
    def ps(s) -> str:
        return str(s).replace("'", "''")

    icon = REPO_ROOT / "icon.ico"
    me = ps(Path(__file__).resolve())

    def one(name: str, extra_args: str, description: str) -> str:
        return (
            f"$p = Join-Path $d '{ps(name)}'; "
            "$s = (New-Object -COM WScript.Shell).CreateShortcut($p); "
            f"$s.TargetPath = '{ps(sys.executable)}'; "
            f"$s.Arguments = '\"{me}\"{extra_args}'; "
            f"$s.WorkingDirectory = '{ps(REPO_ROOT)}'; "
            + (f"$s.IconLocation = '{ps(icon)},0'; " if icon.exists() else "")
            + f"$s.Description = '{ps(description)}'; "
            "$s.Save(); Write-Output $p; ")

    return (
        "$d = [Environment]::GetFolderPath('Desktop'); "
        + "".join(f"Remove-Item -LiteralPath (Join-Path $d '{ps(n)}') -ErrorAction SilentlyContinue; "
                  for n in OLD_SHORTCUT_NAMES)
        + one(SHORTCUT_NAME, "", "Start Avatar from source and open the Beacon page")
        + one(SERVER_SHORTCUT_NAME, " --server",
              "Start Avatar from source as the Handsfree server (scheduled rounds run here)"))


def install_shortcut() -> int:
    """Both Desktop shortcuts: Beacon (dev) and Beacon Server (dev)."""
    script = shortcut_script()
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    r = subprocess.run(["powershell", "-NoProfile", "-EncodedCommand", encoded],
                       capture_output=True, text=True)
    if r.returncode != 0:
        print(f"Shortcut creation failed: {r.stderr.strip()}")
        return 1
    for line in r.stdout.strip().splitlines():
        print(f"Shortcut created: {line.strip()}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--install-shortcut", action="store_true",
                    help="create both Desktop shortcuts instead of starting the app")
    ap.add_argument("--server", action="store_true",
                    help="start (or expect) the app as the Handsfree SERVER: the Beacon "
                         "page can start the scheduled rounds, which run in this app")
    args = ap.parse_args()
    if args.install_shortcut:
        return install_shortcut()
    rc = start("server" if args.server else "client")
    if rc != 0:
        # Launched from a shortcut the console closes with the process —
        # keep the failure readable.
        try:
            input("Press Enter to close this window...")
        except EOFError:
            pass
    return rc


if __name__ == "__main__":
    sys.exit(main())
