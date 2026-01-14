import socket
import random
import winreg
import os, re
import subprocess
import importlib
import pyperclip
import threading
from pathlib import Path
from typing import List

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

    driver_dir = os.path.join(downloads_dir, "chrome_driver")
    os.makedirs(driver_dir, exist_ok=True)

    prompt_dir = os.path.join( downloads_dir, "avatar_prompt")
    os.makedirs(prompt_dir, exist_ok=True)
    return downloads_dir, driver_dir, prompt_dir


def get_clipboard_case_number():
    text = pyperclip.paste()
    text = text.strip().replace(" ", "")  # 

    pattern = r"^\d{8}$"  # 
    if not text or not re.match(pattern, text):
        text = ""
    return text.strip()

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

def get_load_path(primary, backup, timeout_sec=8):
    print("checking shared folder path...")
    for path in [primary, backup]:
        result = [False]
        def check(): 
            try: result.__setitem__(0, Path(path).exists())
            except: pass
        thread = threading.Thread(target=check, daemon=True)
        thread.start()
        thread.join(timeout_sec)
        if not thread.is_alive() and result[0]:
            print(f"Using: {path}")
            return path
    print("!! ALL SHARED FOLDER UNAVAILABLE")


def read_log_file(path: str) -> List[str]:
    try:
        with open(path, 'r', encoding='utf-8', errors="replace") as f:
            return f.readlines()
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