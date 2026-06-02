from pywinauto.application import Application
from pywinauto import Desktop
import os
import time
import psutil
import subprocess, glob

# Global variable to cache a running instance's PID so we can reconnect
# instead of launching a new GUI process every time.
active_bt_pid = None


def reset_active_bt_pid():
    """Reset the cached BT tool PID (e.g. after force-killing the process)."""
    global active_bt_pid
    active_bt_pid = None


def open_with_text_analysis_tool(file_path: str, filter_path: str = None) -> bool:
    """
    Open a generated .hci.txt file using TextAnalysisTool.NET.

    Purpose:
        After the BT tool produces a decoded HCI text log, this function
        launches an external viewer (TextAnalysisTool.NET) to inspect it.

    Args:
        file_path: Absolute or relative path to the .hci.txt output.

    Returns:
        True if the viewer is successfully launched; False otherwise.

    Notes:
        - The viewer path is assumed to live in the project under
          '_internal/TextAnalysisTool.NET.exe'. Adjust if your packaging differs.
        - Uses subprocess.Popen to avoid blocking the current script.
    """
    #exe_path = "_internal/TextAnalysisTool.NET.exe"
    #exe_path = "TextAnalysisTool.NET.exe"

    exe_path = os.path.abspath(os.path.join(os.path.dirname(__file__), 'TextAnalysisTool.NET.exe'))
    # Verify that both the viewer and the target file exist.
    if not os.path.exists(exe_path):
        print(f"❌ Executable not found: {exe_path}")
        return False

    if not os.path.exists(file_path):
        print(f"❌ File not found: {file_path}")
        return False

    # Launch the viewer with file path and optional precomputed filter path.
    cmd = [exe_path, file_path]
    if filter_path and os.path.exists(filter_path):
        cmd.append(f"/Filters:{filter_path}")

    try:
        subprocess.Popen(cmd)
        print(f"✅ Opened with TextAnalysisTool.NET: {file_path}")
        return True
    except Exception as e:
        print(f"❌ Failed to open with TextAnalysisTool.NET: {e}")
        return False


def is_file_ready(path: str) -> bool:
    """
    Check whether a file is stable and readable.

    Purpose:
        When waiting for an output file that is being actively written,
        we need to ensure it has stopped changing in size and can be opened
        for reading before proceeding.

    Strategy:
        - Snapshot size → wait 1s → snapshot again; if sizes differ, it's still being written.
        - Try to read a small portion to ensure read permission and file is not locked.

    Args:
        path: Path to the file under test.

    Returns:
        True if the file size is stable and it is readable; False otherwise.
    """
    try:
        prev_size = os.path.getsize(path)
        time.sleep(1)  # brief delay to detect ongoing writes
        new_size = os.path.getsize(path)
        if prev_size != new_size:
            return False

        # Try to read a few bytes to ensure file is accessible
        with open(path, 'r', encoding='utf-8', errors='ignore') as f:
            _ = f.read(10)
        return True
    except Exception:
        # Any exception here implies the file is not ready yet.
        return False


def close_error_dialog() -> None:
    """
    Dismiss any error dialog that might block further GUI automation.

    Purpose:
        The BT tool may show modal error dialogs (e.g., "HCI Decode" errors).
        A modal could block interaction with other controls. We proactively
        search and close such dialogs to keep automation unblocked.

    Behavior:
        - Enumerates all top-level windows via UIA (pywinauto).
        - Looks for window text containing 'HCI Decode'.
        - Attempts to click the "確定" (OK/Confirm) button to close it.
    """
    try:
        windows = Desktop(backend="uia").windows()
        for win in windows:
            # The HCI Decode error dialog has an EMPTY title ("").
            # We identify it by Win32 class "#32770" (standard MessageBox/Dialog),
            # then verify its [Text] child contains the expected error message.
            if win.element_info.class_name != "#32770":
                continue
            has_hci_error = any(
                "HCI Decode" in c.window_text()
                for c in win.descendants()
                if c.element_info.control_type == "Text"
            )
            if has_hci_error:
                print("⚠️ HCI Decode error dialog found. Closing it.")
                # child_window() is not available on raw UIAWrapper from Desktop.windows().
                # Use descendants() to locate the OK button directly.
                for btn in win.descendants():
                    if btn.element_info.control_type == "Button" and btn.window_text() == "OK":
                        btn.click_input()
                        print("✅ Closed dialog with button 'OK'")
                        break
                break
    except Exception as e:
        print("⚠️ Failed to close error dialog:", e)


def bt_decode_hci_via_folder(log_folder_path: str, log_path: str, timeout: int = 180) -> str:
    """
    Decode an ETL folder via the 'BT Driver Log Parser' tab (same as AutoFolder mode)
    but WITHOUT opening TextAnalysisTool.NET.

    Used by the LLM analysis flow: decodes the folder, waits for the specific
    <log_path>.hci.txt to appear, then returns its path so the caller can pass
    it directly to the log_parser / LLM pipeline.

    Args:
        log_folder_path: Directory that contains the ETL file(s) to decode.
        log_path:        Full path of the target ETL file (without .hci.txt suffix).
                         The function waits for '<log_path>.hci.txt'.
        timeout:         Max seconds to wait for the output file.

    Returns:
        str path to the generated .hci.txt, or None on failure / timeout.
    """
    # If hci.txt already exists and is ready, return it immediately
    print(log_path)
    _base = log_path[:-4] if log_path.lower().endswith('.etl') else log_path
    hci_txt = _base + ".hci.txt"
    if os.path.exists(hci_txt) and is_file_ready(hci_txt):
        print(f"✅ HCI log already exists and is ready: {hci_txt}")
        return hci_txt

    global active_bt_pid

    exe_path = os.path.abspath(os.path.join(os.path.dirname(__file__), 'ibtdrvlogparser.exe'))
    if not os.path.exists(exe_path):
        print(f"❌ Executable not found: {exe_path}")
        return None

    app = None

    # Reuse existing tool if possible
    if active_bt_pid and psutil.pid_exists(active_bt_pid):
        try:
            app = Application(backend='uia').connect(process=active_bt_pid)
            print(f"🔁 Reusing BT tool instance (PID: {active_bt_pid})")
        except Exception as e:
            print(f"⚠️ Reconnect failed: {e}")
            active_bt_pid = None

    if not app:
        app = Application(backend="uia").start(exe_path)
        active_bt_pid = app.process
        print(f"🚀 Launched BT tool: {exe_path} (PID: {active_bt_pid})")
        time.sleep(0.5)

    try:
        app_window = app.top_window()
    except Exception as e:
        print(f"❌ Failed to get app window: {e}")
        return None

    # Switch to 'BT Driver Log Parser' tab (same as AutoFolder mode)
    try:
        bt_tab = app_window.child_window(
            title="BT Driver Log Parser", control_type="TabItem"
        ).wrapper_object()
        bt_tab.select()
        print("✅ Selected 'BT Driver Log Parser' tab.")
        time.sleep(1)
    except Exception as e:
        print(f"❌ Failed to select 'BT Driver Log Parser' tab: {e}")

    # Set folder path
    try:
        folder_input = app_window.child_window(auto_id="txt_parse_folder", control_type="Edit")
        folder_input.set_edit_text(log_folder_path)
        print(f"✅ Folder path set: {log_folder_path}")
    except Exception as e:
        print(f"❌ Failed to set folder path: {e}")

    # Click 'Decode Folder'
    try:
        decode_btn = app_window.child_window(auto_id="btn_parse_decode", control_type="Button")
        decode_btn.invoke()
        print("✅ Decode Folder triggered.")
    except Exception as e:
        print(f"❌ Failed to trigger Decode Folder: {e}")

    # Poll for <log_path>.hci.txt with timeout (same logic as bt_analysis_autoFolder_mode)
    print(f"⏳ Waiting for HCI output (timeout={timeout}s): {hci_txt}")

    for _ in range(timeout):
        if not psutil.pid_exists(active_bt_pid):
            print("❌ BT tool closed unexpectedly during HCI wait.")
            active_bt_pid = None
            return None
        close_error_dialog()
        if os.path.exists(hci_txt) and is_file_ready(hci_txt):
            print(f"✅ HCI log ready: {hci_txt}")
            return hci_txt
        time.sleep(1)

    print(f"⚠️ Timed out ({timeout}s) waiting for HCI log: {hci_txt}")
    return None


def bt_analysis_autoFile_mode(
    log_path: str,
    debug: bool = False,
    filter_path: str = None
    
) -> None:
    """
    Run a single-file (ETL) decode via the 'IbtSnoopgen' tab and open the .hci.txt result.

    Workflow:
        1) Start or attach to the BT tool (ibtdrvlogparser.exe).
        2) Switch to "IbtSnoopgen" tab.
        3) Fill the ETL file path.
        4) Toggle symbol options: 'Local symbol file (.pdb)' then 'Fetch symbol from Server'.
        5) Enable "Generate BTSnoop log", "Generate .txt file", and "Decode HCI Data".
        6) Click "Decode Log".
        7) Wait for the *.hci.txt file to appear/stabilize and open it in TextAnalysisTool.NET.

    Args:
        log_path: Absolute path to the ETL file to decode.
        debug: If True, prints control tree to help with UI mapping.
        wait_hci_timeout: (Reserved) If you later add a timeout for waiting.

    Notes:
        - Uses robust setters: set_edit_text → set_value → type_keys fallback.
        - Uses UIA control toggling/invoking with fallback clicks to handle finicky controls.
        - Reuses an existing process via active_bt_pid when available.
    """
    global active_bt_pid

    exe_path = os.path.abspath(os.path.join(os.path.dirname(__file__), 'ibtdrvlogparser.exe'))
    if not os.path.exists(exe_path):
        print(f"❌ Executable not found: {exe_path}")
        return

    app = None


    # 1) Reuse existing GUI process if we know its PID and it still exists
    if active_bt_pid and psutil.pid_exists(active_bt_pid):
        try:
            app = Application(backend='uia').connect(process=active_bt_pid)
            print(f"🔁 Reusing BT tool instance (PID: {active_bt_pid})")
        except Exception as e:
            print(f"⚠️ Reconnect failed: {e}")
            active_bt_pid = None

    # 2) If attach failed or we have no PID, start a new instance
    if not app:
        app = Application(backend="uia").start(exe_path)
        active_bt_pid = app.process
        print(f"🚀 Launched BT tool at: {exe_path} (PID: {active_bt_pid})")
        time.sleep(2)           # Give the app time to fully load and show startup dialogs
        close_error_dialog()    # Dismiss "Could not create/load HCI Decode library" and similar
        time.sleep(0.5)         # Brief pause after dismissal before interacting with the UI

    # 3) Obtain the main window handle
    try:
        app_window = app.top_window()
        # app_window.set_focus()  # (optional) bring to foreground if needed
    except Exception as e:
        print(f"❌ Failed to get app window: {e}")
        return

    if debug:
        print("🔎 Dumping all controls in main window:")
        app_window.dump_tree()

    # 4) Switch to the "IbtSnoopgen" tab with retries (helps if UI not ready yet)
    tab_selected = False
    for i in range(2):
        try:
            ibt_tab = app_window.child_window(title_re=".*IbtSnoopgen.*", control_type="TabItem")
            if ibt_tab.exists(timeout=2):
                ibt_tab = ibt_tab.wrapper_object()
                ibt_tab.select()
                print("✅ Selected IbtSnoopgen tab.")
                time.sleep(1)
                tab_selected = True
                break
        except Exception as e:
            print(f"⚠️ Retry {i+1}/5 failed to select IbtSnoopgen tab: {e}")
            time.sleep(1)
    if not tab_selected:
        print("❌ Failed to select IbtSnoopgen tab after retries.")
        return

    # 5) Enter the ETL file path using progressively more forceful methods
    folder_set = False

    # Try up to 2 times in case the control is not ready yet
    for i in range(2):
        try:
            # Locate the ETL input box by its automation ID
            etl_spec = app_window.child_window(auto_id="textBoxETLLog", control_type="Edit")

            # If the control exists, get its wrapper object for interaction
            if etl_spec.exists(timeout=0.5):
                etl_edit = etl_spec.wrapper_object()

                # 1. First attempt: use set_edit_text (preferred)
                try:
                    etl_edit.set_edit_text(log_path)

                # 2. If that fails, try set_value (some controls expose value instead)
                except Exception:
                    try:
                        etl_edit.set_value(log_path)

                    # 3. Last resort: simulate typing into the field
                    except Exception:
                        etl_edit.set_focus()
                        etl_edit.type_keys("^a{BACKSPACE}", with_spaces=True)  # clear existing text
                        etl_edit.type_keys(log_path, with_spaces=True)         # type full path

                print("✅ ETL file path set successfully.")
                folder_set = True
                break

        except Exception as e:
            print(f"⚠️ Retry {i+1}/5 failed to set ETL path: {e}")
            time.sleep(1)

    # If still not set after retries, abort
    if not folder_set:
        print("❌ Failed to set ETL path after multiple retries.")
        return


    # 6) Toggle symbol options in order: Local → Fetch from Server
    try:
        time.sleep(0.2)  # Small delay to let the tab content render

        # 'snoopgen_pane' is the container for controls in this tab page.
        snoopgen_pane = app_window.child_window(title="IbtSnoopgen", auto_id="tabPage_snoopgen", control_type="Pane")

        # 6.1) Select "Local symbol file (.pdb)" first
        local_sym_radio = snoopgen_pane.child_window(title="Local symbol file( .pdb)", auto_id="rb_localsym")
        if local_sym_radio.exists(timeout=0.5):
            w = local_sym_radio.wrapper_object()
            try:
                w.toggle()
            except:
                try:
                    w.invoke()
                except:
                    w.click()
        else:
            print("❌ Local symbol file (.pdb) not found")

        # 6.2) Then select "Fetch symbol from Server"
        #toggle() → invoke() → click()
        print("🔍 seek Fetch symbol from Server ...")
        fetch_radio = snoopgen_pane.child_window(title="Fetch symbol from Server", auto_id="rb_serversym")
        if fetch_radio.exists(timeout=0.5):
            w = fetch_radio.wrapper_object()
            try:
                w.toggle()
            except:
                try:
                    w.invoke()
                except:
                    w.click()
        else:
            print("❌ Fetch symbol from Server not found")

    except Exception as e:
        print(f"⚠️ Error in Step 6 Local→Server: {e}")

    # 7) Enable "Generate BTSnoop log"
    try:
        btsnoop_chk = app_window.child_window(auto_id="checkBoxBTSnoop", control_type="CheckBox")
        if btsnoop_chk.exists(timeout=0.5):
            if hasattr(btsnoop_chk, "get_toggle_state"):
                if not btsnoop_chk.get_toggle_state():
                    btsnoop_chk.toggle()
                    print("✅ Checked 'Generate BTSnoop log'.")
            else:
                # Some controls lack get_toggle_state but can be invoked.
                btsnoop_chk.invoke()
                print("✅ Toggled 'Generate BTSnoop log' via invoke().")
        else:
            print("ℹ️ 'Generate BTSnoop log' checkbox not found.")
    except Exception as e:
        print(f"⚠️ Could not check 'Generate BTSnoop log': {e}")

    # 8) Enable "Generate .txt file"
    try:
        txt_chk = app_window.child_window(auto_id="checkBoxTxt", control_type="CheckBox")
        if txt_chk.exists(timeout=0.5):
            if hasattr(txt_chk, "get_toggle_state"):
                if not txt_chk.get_toggle_state():
                    txt_chk.toggle()
                    print("✅ Checked 'Generate .txt file'.")
            else:
                txt_chk.invoke()
                print("✅ Toggled 'Generate .txt file' via invoke().")
        else:
            print("ℹ️ 'Generate .txt file' checkbox not found.")
    except Exception as e:
        print(f"⚠️ Could not check 'Generate .txt file': {e}")

    # 9) Enable "Decode HCI Data"
    try:
        decode_hci_chk = app_window.child_window(auto_id="DecodeHcidata", control_type="CheckBox")
        if decode_hci_chk.exists(timeout=0.5):
            if hasattr(decode_hci_chk, "get_toggle_state"):
                if not decode_hci_chk.get_toggle_state():
                    decode_hci_chk.toggle()
                    print("✅ Checked 'Decode HCI Data'.")
            else:
                decode_hci_chk.invoke()
                print("✅ Toggled 'Decode HCI Data' via invoke().")
        else:
            print("ℹ️ 'Decode HCI Data' checkbox not found.")
    except Exception as e:
        print(f"⚠️ Could not check 'Decode HCI Data': {e}")

    # 10) Start decoding by pressing "Decode Log"
    try:
        decode_log_btn = app_window.child_window(auto_id="buttonExtract", control_type="Button")
        if decode_log_btn.exists(timeout=0.5):
            if decode_log_btn.is_enabled():
                decode_log_btn.invoke()
                print("✅ Clicked 'Decode Log'.")
            else:
                print("ℹ️ 'Decode Log' button disabled (maybe already running).")
        else:
            print("ℹ️ 'Decode Log' button not found.")
    except Exception as e:
        print(f"⚠️ Could not click 'Decode Log': {e}")

    # 11) Poll the output directory for *.hci.txt and open it once stable
    hci_dir = os.path.dirname(log_path)
    print(f"bt_analysis_autoFile_mode >>>>>>>>>>>>>>>>>> Step 11 📂 Current working directory: {os.getcwd()}")

    retry_count = 0
    found_file = None

    # Continuous loop (no timeout currently). Add a timeout if desired.
    while True:
        close_error_dialog()  # Avoid being blocked by modals

        # Look for any .hci.txt in the ETL's directory.
        files = glob.glob(os.path.join(hci_dir, "*.hci.txt"))
        if files:
            found_file = files[0]
            if is_file_ready(found_file):
                print(f"📂 HCI log is ready: {found_file}")
                if open_with_text_analysis_tool(found_file, filter_path=filter_path):
                    break  # Stop once opened
            else:
                print(f"⚠️ File exists but still being written: {found_file}")

        time.sleep(1)
        retry_count += 1
        # If needed later: implement a max retries / timeout using wait_hci_timeout


def bt_analysis_manualSelect_mode(
    log_path: str,
    debug: bool = False,
    wait_hci_timeout: int = 180
) -> int:
    """
    Prepare the 'IbtSnoopgen' tab and populate the ETL path for manual follow-up.

    Purpose:
        Similar to autoFile mode but stops after setting up the 'IbtSnoopgen'
        tab and populating the ETL path—useful if you want to manually
        review or change advanced options before decoding.

    Args:
        log_path: Absolute path to the ETL file to decode.
        debug: If True, dumps the control tree for troubleshooting.
        wait_hci_timeout: Reserved for future use.

    Returns:
        int: The process ID (PID) of the BT tool.
    """
    global active_bt_pid

    exe_path = os.path.abspath(os.path.join(os.path.dirname(__file__), 'ibtdrvlogparser.exe'))
    if not os.path.exists(exe_path):
        print(f"❌ Executable not found: {exe_path}")
        return None

    app = None

    # Reuse existing tool if possible
    if active_bt_pid and psutil.pid_exists(active_bt_pid):
        try:
            app = Application(backend='uia').connect(process=active_bt_pid)
            print(f"🔁 Reusing BT tool instance (PID: {active_bt_pid})")
        except Exception as e:
            print(f"⚠️ Reconnect failed: {e}")
            active_bt_pid = None

    # Launch a new one if needed
    if not app:
        app = Application(backend="uia").start(exe_path)
        active_bt_pid = app.process
        print(f"🚀 Launched BT tool at: {exe_path} (PID: {active_bt_pid})")
        time.sleep(2)           # Give the app time to fully load and show startup dialogs
        close_error_dialog()    # Dismiss "Could not create/load HCI Decode library" and similar
        time.sleep(0.5)

    # Connect to window
    try:
        app_window = app.top_window()
        # app_window.set_focus()
    except Exception as e:
        print(f"❌ Failed to get app window: {e}")
        return active_bt_pid

    if debug:
        print("🔎 Dumping all controls in main window:")
        app_window.dump_tree()

    # Go to IbtSnoopgen tab
    tab_selected = False
    for i in range(2):
        try:
            ibt_tab = app_window.child_window(title_re=".*IbtSnoopgen.*", control_type="TabItem")
            if ibt_tab.exists(timeout=2):
                ibt_tab = ibt_tab.wrapper_object()
                ibt_tab.select()
                print("✅ Selected IbtSnoopgen tab.")
                time.sleep(1)
                tab_selected = True
                break
        except Exception as e:
            print(f"⚠️ Retry {i+1}/5 failed to select IbtSnoopgen tab: {e}")
            time.sleep(0.5)
    if not tab_selected:
        print("❌ Failed to select IbtSnoopgen tab after retries.")
        return

    print(f"📂 Processing ETL file: {log_path}")

    # Set ETL path (no decode trigger here; user proceeds manually)
    folder_set = False
    for i in range(2):
        try:
            etl_spec = app_window.child_window(auto_id="textBoxETLLog", control_type="Edit")
            if etl_spec.exists(timeout=0.5):
                etl_edit = etl_spec.wrapper_object()
                try:
                    etl_edit.set_edit_text(log_path)
                except Exception:
                    try:
                        etl_edit.set_value(log_path)
                    except Exception:
                        etl_edit.set_focus()
                        etl_edit.type_keys("^a{BACKSPACE}", with_spaces=True)
                        etl_edit.type_keys(log_path, with_spaces=True)

                print("✅ ETL file path set successfully.")
                folder_set = True
                break
        except Exception as e:
            print(f"⚠️ Retry {i+1}/5 failed to set ETL path: {e}")
            time.sleep(1)
    if not folder_set:
        print("❌ Failed to set ETL path after multiple retries.")
        return active_bt_pid
    
    return active_bt_pid


def bt_analysis_autoFolder_mode(
    log_folder_path: str,
    log_path: str,
    debug: bool = False,
    wait_hci_timeout: int = 15,
    should_stop: callable = None,
    filter_path: str = None
) -> int:
    """
    Decode an entire folder via the 'BT Driver Log Parser' tab and open the target .hci.txt.

    Workflow:
        1) Start/attach to BT tool.
        2) Switch to 'BT Driver Log Parser' tab.
        3) Put the target folder path into the folder input box.
        4) Click 'Decode Folder' to process logs in the folder.
        5) Wait for '<log_path>.hci.txt' to appear and open it.

    Args:
        log_folder_path: Directory to parse (contains logs to decode).
        log_path: Full path (without .hci.txt suffix) of the specific output of interest.
                  The function waits for '<log_path>.hci.txt'.
        debug: If True, prints control identifiers for debugging.
        wait_hci_timeout: (Currently unused) intended for adding a timeout later.
        should_stop: Optional callable that returns True if this operation should be aborted.

    Returns:
        int: The process ID (PID) of the BT tool, or None if failed/aborted.

    Notes:
        - Uses the same attach-or-launch pattern as other functions.
        - Uses 'invoke()' on the decode button to avoid focus issues.
    """
    global active_bt_pid

    # 1) Construct the path to the tool and verify it exists.
    exe_path = os.path.abspath(os.path.join(os.path.dirname(__file__), 'ibtdrvlogparser.exe'))
    if not os.path.exists(exe_path):
        print(f"❌ Executable not found: {exe_path}")
        return None

    app = None

    # 2) Try to reuse an existing instance (faster, avoids multiple GUIs)
    if active_bt_pid and psutil.pid_exists(active_bt_pid):
        try:
            app = Application(backend='uia').connect(process=active_bt_pid)
            print(f"🔁 Reusing existing BT tool instance (PID: {active_bt_pid})")
        except Exception as e:
            print(f"⚠️ Failed to reconnect to PID {active_bt_pid}: {e}")
            active_bt_pid = None

    # 3) Launch if not attached
    if not app:
        app = Application(backend="uia").start(exe_path)
        active_bt_pid = app.process
        print(f"🚀 BT tool launched at: {exe_path} (PID: {active_bt_pid})")
        time.sleep(2)           # Give the app time to fully load and show startup dialogs
        close_error_dialog()    # Dismiss "Could not create/load HCI Decode library" and similar
        time.sleep(0.5)

    # 4) Get the app window; dump controls if requested
    try:
        app_window = app.top_window()
    except Exception as e:
        print(f"❌ Failed to get app window: {e}")
        active_bt_pid = None
        return None

    if debug:
        print("🔎 Dumping all controls:")
        app_window.print_control_identifiers()

    # Switch to the 'BT Driver Log Parser' tab
    try:
        bt_tab = app_window.child_window(title="BT Driver Log Parser", control_type="TabItem").wrapper_object()
        bt_tab.select()
        print("✅ Selected 'BT Driver Log Parser' tab.")
        time.sleep(1)  # Give UI time to switch content
    except Exception as e:
        print("❌ Failed to select 'BT Driver Log Parser' tab:", e)

    # 5) Put the folder path into the input box
    try:
        folder_input = app_window.child_window(auto_id="txt_parse_folder", control_type="Edit")
        folder_input.set_edit_text(log_folder_path)
        print("✅ Folder path input set.")
    except Exception as e:
        print("❌ Failed to set folder path:", e)

    # 6) Trigger "Decode Folder"
    try:
        decode_btn = app_window.child_window(auto_id="btn_parse_decode", control_type="Button")
        decode_btn.invoke()
        print("✅ Decode Folder triggered.")
    except Exception as e:
        print("❌ Failed to trigger Decode Folder:", e)

    # 7) Wait for specific output '<log_path>.hci.txt' and open with viewer
    _base = log_path[:-4] if log_path.lower().endswith('.etl') else log_path
    hci_txt = _base + ".hci.txt"
    print(f"⏳ Waiting for HCI log until found: {hci_txt}")

    retry_count = 0

    while True:
        # Check if this operation was superseded by another
        if should_stop and should_stop():
            print("⚠️ AutoFolder operation superseded, stopping wait.")
            return None

        # Check if process is still alive
        if not psutil.pid_exists(active_bt_pid):
            print("❌ BT tool closed during HCI wait.")
            active_bt_pid = None
            return None

        # Proactively close any modal error dialog that might appear
        close_error_dialog()

        # If the output exists and is stable, open it and stop polling
        if os.path.exists(hci_txt):
            if is_file_ready(hci_txt):
                print(f"📂 HCI log is ready: {hci_txt}")
                if open_with_text_analysis_tool(hci_txt, filter_path=filter_path):
                    return active_bt_pid

        time.sleep(1)
        retry_count += 1
        # Optionally: enforce a timeout using wait_hci_timeout

        if retry_count >= wait_hci_timeout:
            print(f"⚠️ Waited {wait_hci_timeout} seconds for HCI log, giving up.")
            return active_bt_pid