import subprocess
from pywinauto import Application, Desktop
import psutil
import sys, os, ctypes
import time
import glob
import json
from threading import Event
from threading import Lock, Thread

DECODER_EXE = r"C:\UtilityPackage\uSnifferAutoParser\uSnifferAutoParser.exe"





active_fw_pid = None  # global PID cache for WRT_BT_Decoder.exe
active_tat_pid = None
_tat_lock = Lock()
_tat_suppressed_close_pids = set()


def _emit_viewer_log(on_log, message: str):
    if on_log:
        on_log(message)
    else:
        print(message)


def _watch_text_analysis_tool(pid: int, on_close=None):
    try:
        proc = psutil.Process(pid)
        proc.wait()
    except psutil.NoSuchProcess:
        pass
    except Exception as e:
        _emit_viewer_log(on_close, f"⚠️ Failed while watching TextAnalysisTool.NET.exe: {e}")
    finally:
        should_emit_close = False
        with _tat_lock:
            global active_tat_pid
            if pid in _tat_suppressed_close_pids:
                _tat_suppressed_close_pids.discard(pid)
            else:
                should_emit_close = True
            if active_tat_pid == pid:
                active_tat_pid = None
        if should_emit_close:
            _emit_viewer_log(on_close, "ℹ️ TextAnalysisTool.NET viewer closed.")


def close_active_text_analysis_tool(on_log=None) -> bool:
    global active_tat_pid
    with _tat_lock:
        pid = active_tat_pid
        if not pid or not psutil.pid_exists(pid):
            active_tat_pid = None
            return False
        _tat_suppressed_close_pids.add(pid)
        active_tat_pid = None

    try:
        _terminate_process_tree(pid)
        _emit_viewer_log(on_log, "ℹ️ Closed previous TextAnalysisTool.NET viewer.")
        return True
    except Exception as e:
        _emit_viewer_log(on_log, f"⚠️ Failed to close previous TextAnalysisTool.NET viewer: {e}")
        return False


def _terminate_process_tree(pid: int):
    """Terminate process and all children safely."""
    try:
        parent = psutil.Process(pid)
    except psutil.NoSuchProcess:
        return

    children = parent.children(recursive=True)
    for child in children:
        try:
            child.terminate()
        except Exception:
            pass

    try:
        parent.terminate()
    except Exception:
        pass

    _, alive = psutil.wait_procs(children + [parent], timeout=3)
    for p in alive:
        try:
            p.kill()
        except Exception:
            pass


# ---------------- Admin Elevation ----------------
def ensure_admin():
    """
    Ensure this script runs with Administrator privileges.
    If not, relaunch itself with UAC prompt and exit current process.
    """
    try:
        if ctypes.windll.shell32.IsUserAnAdmin():
            return True
    except Exception:
        pass

    # Relaunch with UAC
    params = " ".join([f'"{arg}"' for arg in sys.argv])
    try:
        rc = ctypes.windll.shell32.ShellExecuteW(
            None, "runas", sys.executable, params, None, 1
        )
        if rc <= 32:
            raise RuntimeError(f"ShellExecuteW failed with code {rc}")
    except Exception as e:
        print(f"❌ Failed to elevate privilege: {e}")
    sys.exit()


# ---------------- UI Controls Listing ----------------
def list_controls_clean(app):
    """
    Clean listing of all UI controls: Name, AutoId, ControlType
    """
    try:
        main_win = app.top_window()
        main_win.set_focus()
        print("=== Controls of WRT_BT_Decoder.exe ===")
        for ctrl in main_win.descendants():
            name = ctrl.window_text().strip()
            auto_id = ctrl.element_info.automation_id
            ctype = ctrl.friendly_class_name()
            print(f"Name='{name}' | AutoId='{auto_id}' | ControlType='{ctype}'")
    except Exception as e:
        print(f"⚠️ Error listing controls: {e}")

def list_decoder_controls(verbose=True, max_depth=3):
    """
    Attach to WRT_BT_Decoder.exe GUI and list controls.
    - verbose=False: flat list (like before)
    - verbose=True : tree structure with indentation, up to max_depth
    """
    global active_fw_pid

    print("🔍 Debug: list_decoder_controls() called")
    print(f"🔍 Debug: cached active_fw_pid={active_fw_pid}")

    try:
        desktop = Desktop(backend="uia")
        
        target = None
        for w in desktop.windows():
            title = (w.window_text() or "").strip()
            if "WRT_BT_Decoder" in title:
                target = w
                break

        if not target:
            print("❌ Debug: Could not find WRT_BT_Decoder main window")
            return

        print(f"✅ Debug: Using window '{target.window_text()}' (handle={target.handle}, pid={target.process_id()})")

        # 直接 connect 到這個 handle
        app = Application(backend="uia").connect(handle=target.handle, timeout=10)
        main_win = app.window(handle=target.handle)
        main_win.set_focus()

        if not verbose:
            print("=== Controls of WRT_BT_Decoder.exe (flat list) ===")
            for ctrl in main_win.descendants():
                name = ctrl.window_text().strip()
                auto_id = ctrl.element_info.automation_id
                ctype = ctrl.friendly_class_name()
                print(f"Name='{name}' | AutoId='{auto_id}' | ControlType='{ctype}'")
        else:
            print("=== Controls of WRT_BT_Decoder.exe (tree view) ===")

            def dump_tree(ctrl, depth=0):
                if depth > max_depth:
                    return
                indent = "  " * depth
                name = ctrl.window_text().strip()
                auto_id = ctrl.element_info.automation_id
                ctype = ctrl.friendly_class_name()
                print(f"{indent}- Name='{name}' | AutoId='{auto_id}' | ControlType='{ctype}'")
                for child in ctrl.children():
                    dump_tree(child, depth + 1)

            dump_tree(main_win)

    except Exception as e:
        print(f"⚠️ Debug: Exception in list_decoder_controls: {e}")




def fw_wifi_analysis(fw_path: str, timeout: int = 30, cancel_event: Event | None = None):
    """
    Run the decoder exe with ETL file, wait for the generated output folder 
    (base name of fw_path without extension + '_xxxx'), and open that folder.

    Args:
        fw_path (str): Absolute path to the .etl file
        timeout (int): Max wait time for output folder in seconds
    """
    if not os.path.exists(DECODER_EXE):
        print(f"❌ Decoder executable not found: {DECODER_EXE}")
        return False
    
    if not os.path.exists(fw_path):
        print(f"❌ ETL file not found: {fw_path}")
        return False

    folder = os.path.dirname(fw_path)
    base_no_ext = os.path.splitext(os.path.basename(fw_path))[0]

    try:
        print(f"⚙️ Running decoder: {DECODER_EXE} {fw_path}")
        proc = subprocess.Popen([DECODER_EXE, fw_path])

        while proc.poll() is None:
            if cancel_event and cancel_event.is_set():
                print("⚠️ FW WiFi analysis canceled. Terminating decoder process...")
                _terminate_process_tree(proc.pid)
                return None
            time.sleep(0.5)

        if proc.returncode != 0:
            print(f"❌ Decoder failed with error code {proc.returncode}")
            return False

        # Look for output folder matching "base_no_ext_*"
        output_folder = None
        for _ in range(timeout):
            if cancel_event and cancel_event.is_set():
                print("⚠️ FW WiFi analysis canceled while waiting output folder.")
                return None
            candidates = glob.glob(os.path.join(folder, base_no_ext + "_*"))
            candidates = [c for c in candidates if os.path.isdir(c)]
            if candidates:
                # Pick the newest folder
                output_folder = max(candidates, key=os.path.getmtime)
                break
            time.sleep(1)

        if output_folder and os.path.exists(output_folder):
            print(f"✅ Output folder generated: {output_folder}")
            subprocess.run(['explorer', output_folder])
        else:
            print(f"⚠️ Output folder not found for base: {base_no_ext}_* (waited {timeout}s)")

        return True

    except subprocess.CalledProcessError as e:
        print(f"❌ Decoder failed with error code {e.returncode}")
        return False
    except Exception as e:
        print(f"❌ Failed to launch decoder: {e}")
        return False



def fw_bt_analysis(fw_path, use_cli=True, cancel_event: Event | None = None):
    """
    Launch WRT_BT_Decoder.exe with elevation and attach UI (via window detection)
    """
    global active_fw_pid
    exe_path = r"C:\UtilityPackage\WRT_BT_Logs_Decoder\WRT_BT_Decoder.exe"
    exe_cli_path = r"C:\UtilityPackage\WRT_BT_Logs_Decoder\bt_decoder_cli.exe"

    if use_cli:
        if not os.path.exists(exe_cli_path):
            return f"❌ CLI executable not found: {exe_cli_path}"
    
        try:
            print(f"🔍 Debug: Running CLI decoder with fw_path={fw_path}")
            arguments = ["-e", fw_path, "-autoFetchDevTrace_Headers"]
            result_proc = subprocess.Popen(
                [exe_cli_path] + arguments,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                encoding='utf-8'
            )

            while result_proc.poll() is None:
                if cancel_event and cancel_event.is_set():
                    print("⚠️ FW BT analysis canceled. Terminating bt_decoder_cli process...")
                    _terminate_process_tree(result_proc.pid)
                    return None, None, "FW analysis canceled"
                time.sleep(0.5)

            stdout, stderr = result_proc.communicate()

            outputs_ready = _has_fw_bt_decode_outputs(fw_path)

            if result_proc.returncode != 0:
                if outputs_ready:
                    print(f"⚠️ bt_decoder_cli exited with code {result_proc.returncode}, but decode outputs exist. Continue parsing.")
                    if stderr:
                        print(stderr)
                else:
                    print(f"❌ bt_decoder_cli exited with code {result_proc.returncode}")
                    if stderr:
                        print(stderr)
                    return None, None, stdout
            
            print("✅ Debug: FW bt decoder CLI is completed successfully.")
            sysmon_text = _get_sysmon_to_text(fw_path)
            system_info = _get_system_info(fw_path)
            return system_info, sysmon_text, stdout

        except subprocess.CalledProcessError as e:
            print(f"❌ Failed to launch bt_decoder_cli.exe, (Error Code {e.returncode}):")
            print(e.stderr)
        except Exception as e:
            print(f"❌ Unexpected error: {e}")

    else:
            
        if not os.path.exists(exe_path):
            return f"❌ Executable not found: {exe_path}"

        try:
            
            params = f'"{fw_path}"'
            print(f"🔍 Debug: Launching exe with params={params}")
            rc = ctypes.windll.shell32.ShellExecuteW(
                None, "runas", exe_path, params, os.path.dirname(exe_path), 1
            )
            if rc <= 32:
                return f"❌ Failed to launch WRT_BT_Decoder.exe, code={rc}"

            
            win, handle, pid = None, None, None
            for i in range(20):
                try:
                    desktop = Desktop(backend="uia")
                    for w in desktop.windows():
                        title = (w.window_text() or "").strip()
                        if "WRT" in title and "Decoder" in title:
                            win, handle, pid = w, w.handle, w.process_id()
                            print(f"✅ Debug: Found window '{title}' (handle={handle}, pid={pid}) after {i+1}s")
                            break
                except Exception as e:
                    print(f"⚠️ Debug: Window search error: {e}")
                if win:
                    break
                time.sleep(1)

            if not win:
                return "❌ Could not detect WRT_BT_Decoder.exe window after waiting."

            active_fw_pid = pid
            print(f"🔍 Debug: Active PID set to {active_fw_pid}")

        
            app = Application(backend="uia").connect(handle=handle, timeout=10)
            print(f"✅ Connected to WRT_BT_Decoder.exe via window handle (PID={pid})")

            list_controls_clean(app)
            return f"✅ FW Analysis launched for {fw_path}"
        except Exception as e:
            return f"❌ Unexpected error: {str(e)}"

def _extract_last_timestamp_from_folder_name(folder_name: str):
    """
    Extract the LAST MM-DD-YYYY_HH-MM-SS timestamp embedded in a folder name.
    Example: 'wrt-fw-07-12-2025_04-21-38_483_1_07-12-2025_04-21-48_000_07-12-2025_04-21-40-431_6050'
    Returns a datetime or None.
    """
    import re
    from datetime import datetime
    pattern = r'(\d{2})-(\d{2})-(\d{4})_(\d{2})-(\d{2})-(\d{2})'
    matches = re.findall(pattern, folder_name)
    if not matches:
        return None
    try:
        month, day, year, hour, minute, second = matches[-1]
        return datetime(int(year), int(month), int(day),
                        int(hour), int(minute), int(second))
    except Exception:
        return None


def _get_short_path(long_path: str) -> str:
    """Convert a long Windows path to its 8.3 short form to bypass MAX_PATH limits."""
    import ctypes
    buf_size = ctypes.windll.kernel32.GetShortPathNameW(long_path, None, 0)
    if buf_size == 0:
        return long_path  # fallback: return as-is
    buf = ctypes.create_unicode_buffer(buf_size)
    ctypes.windll.kernel32.GetShortPathNameW(long_path, buf, buf_size)
    return buf.value or long_path


def open_sysmon_with_tool(fw_path: str, on_log=None, on_close=None):
    """
    Find the .sysmon file from the latest decode output folder (the folder
    whose name ends with the event ID) and open it with TextAnalysisTool.NET.
    'Latest' is determined by the timestamp embedded in the folder name.
    """
    from datetime import datetime
    fw_dir = os.path.dirname(fw_path)
    eventid = _get_eventid_from_summary(fw_path)
    if not eventid:
        _emit_viewer_log(on_log, "❌ Cannot get Event ID, aborting sysmon open.")
        return False

    # Find all dirs ending with the event ID, sort by embedded folder-name timestamp.
    candidates = [
        os.path.join(fw_dir, d)
        for d in os.listdir(fw_dir)
        if os.path.isdir(os.path.join(fw_dir, d)) and d.endswith(str(eventid))
    ]
    if not candidates:
        _emit_viewer_log(on_log, f"❌ No directory ending with event ID '{eventid}' found in {fw_dir}")
        return False

    def sort_key(p):
        ts = _extract_last_timestamp_from_folder_name(os.path.basename(p))
        return ts if ts is not None else datetime.min

    sysmon_dir = max(candidates, key=sort_key)

    sysmon_path = None
    for f in os.listdir(sysmon_dir):
        if f.endswith(".sysmon"):
            sysmon_path = os.path.join(sysmon_dir, f)
            break

    if not sysmon_path:
        _emit_viewer_log(on_log, f"❌ No .sysmon file found in {sysmon_dir}")
        return False

    exe_path = os.path.abspath(os.path.join(os.path.dirname(__file__), 'TextAnalysisTool.NET.exe'))
    if not os.path.exists(exe_path):
        _emit_viewer_log(on_log, f"❌ TextAnalysisTool.NET.exe not found: {exe_path}")
        return False

    try:
        # sysmon_path may exceed 260 chars. Copy to a short temp path so
        # TextAnalysisTool.NET (which uses .NET Framework IO) can open it.
        import shutil
        tmp_dir = r"C:\Temp\tat_sysmon"
        os.makedirs(tmp_dir, exist_ok=True)
        tmp_sysmon = os.path.join(tmp_dir, os.path.basename(sysmon_path))
        # Use \\?\ prefix on source so Python can read the long path
        shutil.copy2("\\\\?\\" + sysmon_path, tmp_sysmon)
        _emit_viewer_log(on_log, f"✅ Copied sysmon to: {tmp_sysmon}")
        close_active_text_analysis_tool(on_log=on_log)
        proc = subprocess.Popen([exe_path, tmp_sysmon])
        with _tat_lock:
            global active_tat_pid
            active_tat_pid = proc.pid
        Thread(target=_watch_text_analysis_tool, args=(proc.pid, on_close), daemon=True).start()
        _emit_viewer_log(on_log, f"✅ Opened sysmon with TextAnalysisTool.NET: {tmp_sysmon}")
        return True
    except Exception as e:
        _emit_viewer_log(on_log, f"❌ Failed to open sysmon: {e}")
        return False


def _get_system_info(fw_path):
    
    fw_dir = os.path.dirname(fw_path)
    system_info_path = os.path.join(fw_dir, "system_info.txt")
    with open(system_info_path, 'r', encoding='utf-8') as file:
        system_info = json.load(file)
        return {
            "BT Driver Version": system_info['Versions']['BT Driver Version'],
            "Wi-Fi Driver Version": system_info['Versions']['Wi-Fi Driver Version'],
            "Device Name": system_info['Device Name'],
            "BT FW SHA1": system_info['BT FW SHA1'],
            "Wi-Fi Adapter": system_info['Wi-Fi Adapter'],
            "OS Information": system_info['OS Information'],
            "Intel® Smart Sound Technology BUS": system_info['Intel® Smart Sound Technology BUS'],
            "Intel® Smart Sound Technology OED": system_info['Intel® Smart Sound Technology OED'],
            "Intel® Smart Sound Technology for Bluetooth® Audio": system_info['Intel® Smart Sound Technology for Bluetooth® Audio'],
            "WRT::2G Version": system_info['Versions']['WRT::2G Version'],
            "preset": system_info['preset'],
            "BT FW Config": system_info['BT FW Config'],
            "Dbgc Status Global as seen by BT": system_info['Dbgc Status Global as seen by BT'],
            "Dbgc Status as read from Mailbox": system_info['Dbgc Status as read from Mailbox'],
        }

def _get_sysmon_to_text(fw_path):

    fw_dir = os.path.dirname(fw_path)
    eventid = _get_eventid_from_summary(fw_path)
    if not eventid:
        print("❌ Cannot get Event ID, aborting sysmon log extraction.")
        return None
    try:
        for file in os.listdir(fw_dir):
            if os.path.isdir(os.path.join(fw_dir, file)) and file.endswith(eventid):
                sysmon_dir = os.path.join(fw_dir, file)
                for candidate in os.listdir(sysmon_dir):
                    if candidate.endswith(".sysmon"):
                        sysmon_path = os.path.join(sysmon_dir, candidate)
                        with open("\\\\?\\"+sysmon_path, 'r') as f:
                            return f.read()
    except Exception as e:
        print(f"❌ Error while searching for sysmon log: {e}")
        return None


def _has_fw_bt_decode_outputs(fw_path):
    """Use generated artifacts to determine decode success when CLI exit code is non-zero."""
    fw_dir = os.path.dirname(fw_path)
    summary_path = fw_path[:-4] + "decodeSummary.json"
    system_info_path = os.path.join(fw_dir, "system_info.txt")
    return os.path.exists(summary_path) and os.path.exists(system_info_path)

def _get_eventid_from_summary(fw_path):
    if not os.path.exists(fw_path) or not fw_path.endswith(".etl"):
        print(f"❌ ETL file not found: {fw_path}")
        return None
    
    summary_path = fw_path[:-4] + "decodeSummary.json"

    if not os.path.exists(summary_path):
        print(f"❌ Summary file not found: {summary_path}")
        return None
    try:
        with open(summary_path, 'r') as f:
            summary_data = json.load(f)
            event_id = summary_data['dumpInfo']['dumps'][0]['dumps'][0]['eventID']
            print(f"✅ Extracted Event ID: {event_id} from summary")
            return str(event_id)
    except Exception as e:
        print(f"❌ Failed to read summary file: {e}")
        return None

def launch_decoder(fw_path):
    exe_path = r"C:\UtilityPackage\WRT_BT_Logs_Decoder\WRT_BT_Decoder.exe"
    params = f'"{fw_path}"'
    print(f"🚀 Launching with ShellExecuteW, params={params}")

    rc = ctypes.windll.shell32.ShellExecuteW(
        None, "runas", exe_path, params, os.path.dirname(exe_path), 1
    )
    if rc <= 32:
        print(f"❌ Failed to launch exe, rc={rc}")
        return None

    print("⏳ Waiting up to 30s for any new window containing 'Decoder'...")
    for i in range(30):
        desktop = Desktop(backend="uia")
        wins = desktop.windows()
        for w in wins:
            title = (w.window_text() or "").strip()
            if "Decoder" in title:   
                print(f"✅ Found candidate window after {i+1}s: '{title}' (pid={w.process_id()}, handle={w.handle})")
                return w
        time.sleep(1)
    print("❌ Timeout, no window found.")
    return None

def attach_and_list(win, verbose=False):
    try:
        app = Application(backend="uia").connect(handle=win.handle, timeout=10)
        main_win = app.window(handle=win.handle)
        main_win.set_focus()
        print(f"🔗 Attached to window: '{win.window_text()}' (pid={win.process_id()})")

        if not verbose:
            for ctrl in main_win.descendants():
                print(ctrl.window_text(), ctrl.element_info.automation_id, ctrl.friendly_class_name())
        else:
            def dump(ctrl, depth=0):
                indent = "  " * depth
                print(f"{indent}- {ctrl.window_text()} | {ctrl.element_info.automation_id} | {ctrl.friendly_class_name()}")
                for child in ctrl.children():
                    dump(child, depth+1)
            dump(main_win)

    except Exception as e:
        print(f"⚠️ Failed to attach/list: {e}")



