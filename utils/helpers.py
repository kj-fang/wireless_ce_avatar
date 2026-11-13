import socket
import random
import winreg
import os, re
import subprocess
# load_module() below uses importlib.util, which is a submodule: importing the
# package alone does not bind it. It has worked only because something else
# imported importlib.util first and left it bound as a side effect; on an
# interpreter where nothing does, load_module() raises AttributeError.
import importlib.util
import pyperclip
import threading
import time
from pathlib import Path
from typing import List

from utils import check_ips_utils


def to_long_path(path: str) -> str:
    """Return a Windows extended-length path to bypass the 260-char MAX_PATH limit.

    - Already-prefixed paths (\\\\?\\) are returned unchanged.
    - UNC paths (\\\\server\\share\\...) become \\\\?\\UNC\\server\\share\\...
    - All other paths are made absolute then prefixed with \\\\?\\
    - Non-Windows paths are returned as-is.
    """
    if os.name != 'nt':
        return path
    if path.startswith('\\\\?\\'):
        return path
    path = os.path.abspath(path)
    if path.startswith('\\\\'):
        # UNC path: \\\\server\\share\\... \u2192 \\\\?\\UNC\\server\\share\\...
        return '\\\\?\\UNC\\' + path[2:]
    return '\\\\?\\' + path

def get_long_path(path: str) -> str:
    """Expand Windows 8.3 short names (e.g. INTELA~1) to the full long path.

    Uses GetLongPathNameW so that downstream tools receive the real name
    instead of creating short-name artefacts. Falls back to the original
    path on non-Windows or if the API call fails.
    """
    if os.name != 'nt' or not path:
        return path
    try:
        import ctypes
        buf_size = ctypes.windll.kernel32.GetLongPathNameW(path, None, 0)
        if buf_size == 0:
            return path
        buf = ctypes.create_unicode_buffer(buf_size)
        ctypes.windll.kernel32.GetLongPathNameW(path, buf, buf_size)
        return buf.value or path
    except Exception:
        return path

def get_short_path(path: str) -> str:
    """Convert a long path to its 8.3 short form. Falls back to the original on failure."""
    if os.name != 'nt' or not path:
        return path
    try:
        import ctypes
        buf_size = ctypes.windll.kernel32.GetShortPathNameW(path, None, 0)
        if buf_size == 0:
            return path
        buf = ctypes.create_unicode_buffer(buf_size)
        ctypes.windll.kernel32.GetShortPathNameW(path, buf, buf_size)
        return buf.value or path
    except Exception:
        return path

def get_available_port(start=54000, end=60000, max_tries=20):
    for _ in range(max_tries):
        port = random.randint(start, end)
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(('', port))  
                return port
            except OSError:
                continue
    raise RuntimeError("no available port")


def init_download_dir():
    reg_key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Explorer\Shell Folders")
    downloads_dir = winreg.QueryValueEx(reg_key, "{374DE290-123F-4565-9164-39C4925E467B}")[0]
    winreg.CloseKey(reg_key)

    downloads_dir = os.path.join(downloads_dir, "IntelAvatar_files")
    os.makedirs(downloads_dir, exist_ok=True)

    os.makedirs(os.path.join(downloads_dir, "logs"), exist_ok=True)
    os.makedirs(os.path.join(downloads_dir, "app_state"), exist_ok=True)

    driver_dir = os.path.join(downloads_dir, "chrome_driver")
    os.makedirs(driver_dir, exist_ok=True)

    prompt_dir = os.path.join(downloads_dir, "avatar_prompt")
    os.makedirs(prompt_dir, exist_ok=True)
    return downloads_dir, driver_dir, prompt_dir


def get_clipboard_case_number():
    text = pyperclip.paste()
    # normalise_ips also pads the seven-digit form people actually type, and
    # rejects a YYYYMMDD date such as 20260923, which the previous ^\d{8}$
    # accepted. A zero-led MMDDYYYY date still passes: it cannot be told from
    # a real case number (see utils/check_ips_utils.py).
    return check_ips_utils.normalise_ips(text)

def detect_user_email():
    try:
        result = subprocess.run(['whoami', '/upn'], capture_output=True, text=True, check=True)
        return result.stdout.strip()
    except subprocess.CalledProcessError as e:
        print("fetch email fail", e)
        return None
    
def load_module(path, module_name):
    spec = importlib.util.spec_from_file_location(module_name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module

def get_load_path(primary, backup, timeout_sec=8, label=None):
    tag = f"[{label}] " if label else ""
    print(f"{tag}checking shared folder path...")
    for path in [primary, backup]:
        result = {"exists": False, "error": None,
                   "parent_exists": None, "parent_error": None}
        def check():
            try:
                if Path(path).exists():
                    result["exists"] = True
                    return
            except Exception as e:
                result["error"] = e
                return
            # Full path not found — re-check the parent folder so a
            # missing/inaccessible share or intermediate folder can be told
            # apart from "folder is fine, this file just isn't in it".
            try:
                result["parent_exists"] = Path(path).parent.exists()
            except Exception as pe:
                result["parent_error"] = pe
        start = time.monotonic()
        thread = threading.Thread(target=check, daemon=True)
        thread.start()
        thread.join(timeout_sec)
        elapsed = time.monotonic() - start
        if thread.is_alive():
            print(f"{tag}  TIMEOUT after {elapsed:.1f}s (> {timeout_sec}s): {path}")
            continue
        if result["error"] is not None:
            err = result["error"]
            print(f"{tag}  ERROR after {elapsed:.1f}s checking {path}: {type(err).__name__}: {err}")
            continue
        if result["exists"]:
            print(f"{tag}Using: {path} (found in {elapsed:.1f}s)")
            return path
        if result["parent_error"] is not None:
            pe = result["parent_error"]
            print(f"{tag}  NOT FOUND after {elapsed:.1f}s — parent folder also inaccessible "
                  f"({type(pe).__name__}: {pe}): {path}")
        elif result["parent_exists"] is False:
            print(f"{tag}  NOT FOUND after {elapsed:.1f}s — parent folder is also missing/unreachable: {path}")
        else:
            print(f"{tag}  NOT FOUND after {elapsed:.1f}s — parent folder is reachable, "
                  f"this specific file/path is missing: {path}")
    print(f"{tag}!! ALL SHARED FOLDER UNAVAILABLE")


def read_log_file(path: str) -> List[str]:
    """Read an ETL-decoded ``.log`` into a list of lines as-is.

    Timestamps stay in the log's own frame (decoder host clock, GMT+8 in
    practice). The chatbot now keeps ``issue_time`` aligned to that frame
    for in-log matching and surfaces a customer-tz annotation separately,
    so we no longer shift the file content here.

    Skips the ``# WCE_LOG_TZ_SHIFTED=...`` header marker on files that
    were previously rewritten by the now-retired decoder shift step, so
    those legacy files still read cleanly.
    """
    if not path:
        return []
    try:
        with open(path, 'r', encoding='utf-8', errors="replace") as f:
            lines = f.readlines()
        # Drop the legacy customer-tz marker if present so the marker line
        # never reaches the chatbot's segment scanner as content.
        if lines and lines[0].startswith("# WCE_LOG_TZ_SHIFTED="):
            lines = lines[1:]
        return lines
    except Exception as e:
        print(f"Error reading file {path}: {e}")
        return []

def save_file(output_path: str, lines, ensure_newline: bool = False):
    with open(output_path, 'w', encoding='utf-8') as f:
        for line in lines:
            line_str = str(line)
            if ensure_newline and not line_str.endswith('\n'):
                line_str += '\n'
            
            f.write(line_str)
    print(f"save to: {output_path}")