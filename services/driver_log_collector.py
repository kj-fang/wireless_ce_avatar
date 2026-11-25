"""
Collect the Intel Wi-Fi driver log from the machine it ran on, for the Sleep
Study driver analysis (services/sleepstudy_cli.py --collect).

The Intel Wi-Fi driver keeps its WPP trace in an always-on ETW autologger,
WiFiDriverIHVSession, written under %SystemRoot%\\System32\\LogFiles\\WMI.
Intel's own Modern Standby power checklist collects the driver log by
flushing that session and copying its files (debug_checklist.json: "Copy
Wi-Fi auto-logger data ... tracelog.exe -flush WiFiDriverIHVSession"); WRT
is to be removed first, as it can disturb the capture. This module does the
same with logman, which ships with Windows (no WDK needed):

    logman update trace WiFiDriverIHVSession -fd -ets     flush its buffers
    copy WifiDriverIHVSession.etl[.N] -> <out>/capture/<host>_<DD-MM-YYYY_HH-MM-SS>_collected/

The capture folder carries the collection time in the shape autologger
captures use, so the analysis ranks it like any other capture. Nothing is
started, stopped or reconfigured: the driver keeps logging as before.

Flushing a session and reading System32\\LogFiles\\WMI need an administrator
prompt; the autologger is circular, so collect soon after the standby.
"""
from __future__ import annotations

import ctypes
import os
import re
import shutil
import socket
import subprocess
from datetime import datetime
from typing import Callable, Optional

DRIVER_SESSION = "WiFiDriverIHVSession"
AUTOLOGGER_DIR = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"),
                              "System32", "LogFiles", "WMI")
# The session's files: WiFiDriverIHVSession.etl and its rolled parts .etl.N,
# whatever the case on disk. Copies are named WifiDriverIHVSession..., the
# spelling Avatar's decoder and discovery match.
_SESSION_FILE = re.compile(r"(?i)^wifidriverihvsession(\.etl(?:\.\d+)?)$")
COPY_STEM = "WifiDriverIHVSession"


class CollectError(Exception):
    """A collection that cannot go on: (code, message)."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code, self.message = code, message


def is_admin() -> bool:
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:                 # noqa: BLE001 -- not Windows, or no shell32
        return False


def _run(args: list[str], timeout: int = 120) -> tuple[int, str]:
    """(return code, output) of a command, without a shell."""
    try:
        done = subprocess.run(args, capture_output=True, text=True, timeout=timeout,
                              creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return done.returncode, (done.stdout or "") + (done.stderr or "")
    except (OSError, subprocess.TimeoutExpired) as exc:
        return -1, f"{type(exc).__name__}: {exc}"


def session_running(run: Callable = _run, name: str = DRIVER_SESSION) -> bool:
    code, _ = run(["logman", "query", name, "-ets"])
    return code == 0


def flush_session(run: Callable = _run, name: str = DRIVER_SESSION) -> str:
    """Flush an ETW session's buffers to its file; "" or the failure."""
    code, out = run(["logman", "update", "trace", name, "-fd", "-ets"])
    return "" if code == 0 else (out.strip().splitlines() or [f"exit code {code}"])[-1]


def session_files(source_dir: str = AUTOLOGGER_DIR) -> list[str]:
    """The driver autologger's files in `source_dir`, oldest first."""
    try:
        names = os.listdir(source_dir)
    except OSError:
        return []
    files = [os.path.join(source_dir, n) for n in names if _SESSION_FILE.match(n)]
    return sorted(files, key=lambda f: os.path.getmtime(f))


def collect_wifi_driver_log(out_dir: str, *, source_dir: Optional[str] = None,
                            now: Optional[datetime] = None, run: Callable = _run,
                            require_admin: bool = True) -> dict:
    """
    Flush and copy the Intel Wi-Fi driver autologger into `out_dir`/capture.
    Returns what was collected; raises CollectError:
      E_NOT_ADMIN                 not an administrator prompt
      E_DRIVER_TRACE_NOT_RUNNING  no WiFiDriverIHVSession files on this machine
      E_DRIVER_LOG_COPY_FAILED    files found, none could be copied
    """
    source_dir = source_dir or AUTOLOGGER_DIR
    if require_admin and not is_admin():
        raise CollectError("E_NOT_ADMIN",
                           "collecting the Wi-Fi driver log needs an administrator prompt: it flushes "
                           f"the {DRIVER_SESSION} trace session and reads {source_dir}")
    warnings = []
    running = session_running(run)
    flushed = False
    if running:
        error = flush_session(run)
        flushed = not error
        if error:
            warnings.append(f"flushing {DRIVER_SESSION} failed ({error}); its newest events may be missing")
    else:
        warnings.append(f"the {DRIVER_SESSION} trace session is not running; copying what it last wrote")
    files = session_files(source_dir)
    if not files:
        raise CollectError("E_DRIVER_TRACE_NOT_RUNNING",
                           f"no Intel Wi-Fi driver autologger files ({DRIVER_SESSION}.etl*) in {source_dir}: "
                           "the driver's logging is not enabled on this machine")

    now = now or datetime.now()
    capture = os.path.join(out_dir, "capture",
                           f"{socket.gethostname()}_{now:%d-%m-%Y_%H-%M-%S}_collected")
    os.makedirs(capture, exist_ok=True)
    copied = []
    for path in files:
        suffix = _SESSION_FILE.match(os.path.basename(path)).group(1)
        dest = os.path.join(capture, COPY_STEM + suffix)
        try:
            shutil.copy2(path, dest)
            copied.append({"file": os.path.basename(dest), "source": path,
                           "size": os.path.getsize(dest),
                           "modified": datetime.fromtimestamp(os.path.getmtime(path)).isoformat(timespec="seconds")})
        except OSError as exc:
            warnings.append(f"could not copy {os.path.basename(path)}: {exc}")
    if not copied:
        raise CollectError("E_DRIVER_LOG_COPY_FAILED",
                           f"found {len(files)} {DRIVER_SESSION} file(s) but could not copy any: "
                           + "; ".join(warnings[-len(files):]))
    return {"capture_dir": capture, "files": copied, "session": DRIVER_SESSION,
            "session_running": running, "flushed": flushed, "source_dir": source_dir,
            "collected_at": now.astimezone().isoformat(timespec="seconds"), "warnings": warnings}


def run_elevated(exe: str, params: list[str], cwd: Optional[str] = None,
                 timeout_s: int = 600, verb: str = "runas") -> int:
    """
    Run a command with administrator rights through Windows' UAC prompt and
    wait for it; returns its exit code. The person has to approve the prompt:
    nothing here can, or should, get round it. Raises CollectError:
      E_NOT_ADMIN            the prompt was declined
      E_ELEVATION_FAILED     the command could not be started
      E_ELEVATION_TIMEOUT    it did not finish within `timeout_s`
    """
    from ctypes import wintypes

    class SHELLEXECUTEINFOW(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.DWORD), ("fMask", ctypes.c_ulong),
                    ("hwnd", wintypes.HWND), ("lpVerb", wintypes.LPCWSTR),
                    ("lpFile", wintypes.LPCWSTR), ("lpParameters", wintypes.LPCWSTR),
                    ("lpDirectory", wintypes.LPCWSTR), ("nShow", ctypes.c_int),
                    ("hInstApp", wintypes.HINSTANCE), ("lpIDList", ctypes.c_void_p),
                    ("lpClass", wintypes.LPCWSTR), ("hkeyClass", wintypes.HKEY),
                    ("dwHotKey", wintypes.DWORD), ("hIconOrMonitor", wintypes.HANDLE),
                    ("hProcess", wintypes.HANDLE)]

    SEE_MASK_NOCLOSEPROCESS, SW_SHOWMINNOACTIVE = 0x00000040, 7
    ERROR_CANCELLED, WAIT_TIMEOUT = 1223, 0x00000102
    info = SHELLEXECUTEINFOW()
    info.cbSize = ctypes.sizeof(info)
    info.fMask = SEE_MASK_NOCLOSEPROCESS
    info.lpVerb = verb                     # "runas" asks for administrator rights
    info.lpFile = exe
    info.lpParameters = subprocess.list2cmdline(params)
    info.lpDirectory = cwd
    info.nShow = SW_SHOWMINNOACTIVE        # its console stays minimised
    shell32, kernel32 = ctypes.windll.shell32, ctypes.windll.kernel32
    kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    if not shell32.ShellExecuteExW(ctypes.byref(info)):
        error = ctypes.GetLastError()
        if error == ERROR_CANCELLED:
            raise CollectError("E_NOT_ADMIN", "administrator rights were declined at the Windows (UAC) prompt")
        raise CollectError("E_ELEVATION_FAILED", f"could not start the elevated collection (Windows error {error})")
    try:
        if kernel32.WaitForSingleObject(info.hProcess, timeout_s * 1000) == WAIT_TIMEOUT:
            raise CollectError("E_ELEVATION_TIMEOUT",
                               f"the elevated collection did not finish within {timeout_s} s")
        code = wintypes.DWORD()
        kernel32.GetExitCodeProcess(info.hProcess, ctypes.byref(code))
        return int(code.value)
    finally:
        kernel32.CloseHandle(info.hProcess)


def generate_sleepstudy(out_dir: str, run: Callable = _run) -> str:
    """powercfg /sleepstudy into `out_dir`; the report's path, or CollectError."""
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, "sleepstudy-report.html")
    code, out = run(["powercfg", "/sleepstudy", "/output", path], timeout=300)
    if code != 0 or not os.path.isfile(path):
        raise CollectError("E_SLEEPSTUDY_FAILED",
                           f"powercfg /sleepstudy failed: {(out.strip().splitlines() or [str(code)])[-1]}")
    return path
