import subprocess
from pywinauto import Application, Desktop
import psutil
import sys, os, ctypes
import time
import glob

DECODER_EXE = r"C:\UtilityPackage\uSnifferAutoParser\uSnifferAutoParser.exe"





active_fw_pid = None  # global PID cache for WRT_BT_Decoder.exe


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




def fw_wifi_analysis(fw_path: str, timeout: int = 30):
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
        subprocess.run([DECODER_EXE, fw_path], check=True)

        # Look for output folder matching "base_no_ext_*"
        output_folder = None
        for _ in range(timeout):
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



def fw_bt_analysis(fw_path):
    """
    Launch WRT_BT_Decoder.exe with elevation and attach UI (via window detection)
    """
    global active_fw_pid
    exe_path = r"C:\UtilityPackage\WRT_BT_Logs_Decoder\WRT_BT_Decoder.exe"

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



