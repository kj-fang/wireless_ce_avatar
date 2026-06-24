from pywinauto.application import Application
from pywinauto import Desktop
import os
import time
import psutil
import subprocess, glob

# Global variable to cache a running instance's PID so we can reconnect
# instead of launching a new GUI process every time.
active_bt_pid = None


def _get_true_file_size(path: str) -> int:
    """Return the real current file size by seeking to the end via a file handle.

    Windows NTFS uses lazy metadata updates: the directory-level size cached in
    the MFT is only flushed periodically, so os.path.getsize() (which reads the
    directory cache) can return 0 while the file is actively being written.
    Opening the file and seeking to the end bypasses the directory cache and
    queries the in-memory inode directly, giving the true current size.
    """
    try:
        with open(path, 'rb') as f:
            f.seek(0, 2)
            return f.tell()
    except OSError:
        return -1


def reset_active_bt_pid():
    """Reset the cached BT tool PID (e.g. after force-killing the process)."""
    global active_bt_pid
    active_bt_pid = None


def _terminate_bt_tool(msg: str) -> None:
    """Terminate the BT tool process gracefully, with a kill fallback.

    - Sends terminate() and waits up to 5 s for a clean exit.
    - Falls back to kill() if the process does not exit in time.
    - Always clears active_bt_pid in a finally block so stale PIDs
      never cause incorrect reconnect attempts.
    """
    global active_bt_pid
    pid = active_bt_pid
    try:
        proc = psutil.Process(pid)
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except psutil.TimeoutExpired:
            proc.kill()
    except Exception as e:
        print(f"⚠️ Failed to terminate BT tool (PID {pid}): {e}")
    finally:
        active_bt_pid = None
        print(msg)


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
        prev_size = _get_true_file_size(path)
        time.sleep(1)  # brief delay to detect ongoing writes
        new_size = _get_true_file_size(path)
        if prev_size != new_size:
            return False

        # Try to read a few bytes to ensure file is accessible
        with open(path, 'r', encoding='utf-8', errors='ignore') as f:
            _ = f.read(10)
        return True
    except Exception:
        # Any exception here implies the file is not ready yet.
        return False

def close_warning_dialog() -> None:
    """
    Dismiss the 'Systeminfo.txt not present' warning dialog that may appear
    after clicking Decode.

    The dialog has title "Warning" and contains a message about
    Systeminfo.txt / system_info.txt not being present.  It is a benign
    warning that does not affect the decode result, so we just click OK.
    """
    try:
        windows = Desktop(backend="uia").windows()
        for win in windows:
            if win.window_text() != "Warning":
                continue
            if win.element_info.class_name != "#32770":
                continue
            has_sysinfo_warning = any(
                "Systeminfo" in c.window_text() or "system_info" in c.window_text()
                for c in win.descendants()
                if c.element_info.control_type == "Text"
            )
            if has_sysinfo_warning:
                print("⚠️ Systeminfo warning dialog found. Bringing to front and closing it.")
                try:
                    win.set_focus()
                except Exception:
                    pass
                for btn in win.descendants():
                    if btn.element_info.control_type == "Button" and btn.window_text() == "OK":
                        btn.click_input()
                        print("✅ Closed warning dialog with button 'OK'")
                        break
                break
    except Exception as e:
        print("⚠️ Failed to close warning dialog:", e)


def candidate_hci_paths(log_path: str) -> list:
    """
    All plausible decoded-output names for an ETL path, most-likely first.

    The BT tool keeps the original extension in the output name —
    ``ibtpci-X-boot.etl`` decodes to ``ibtpci-X-boot.etl.hci.txt`` — so the
    decoded file is ``<log_path>.hci.txt``. An older/other code path stripped
    the ``.etl`` first (``<name>.hci.txt``); we keep that as a fallback so a
    file produced either way is still recognised.
    """
    cands = [log_path + ".hci.txt"]                       # <name>.etl.hci.txt (tool's actual output)
    if log_path.lower().endswith(".etl"):
        cands.append(log_path[:-4] + ".hci.txt")          # <name>.hci.txt     (legacy fallback)
    return cands


def find_ready_hci(log_path: str) -> str:
    """Return the first already-decoded, stable .hci.txt for this ETL (either
    naming convention), or None. Lets callers skip an unnecessary re-decode —
    important when the firmware symbols are no longer in the artifactory but a
    previously-decoded .hci.txt still sits next to the ETL."""
    for p in candidate_hci_paths(log_path):
        try:
            if os.path.exists(p) and is_file_ready(p):
                return p
        except Exception:
            continue
    return None


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
        - Attempts to click the "OK" button to close it.
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
                print("⚠️ HCI Decode error dialog found. Bringing to front and closing it.")
                try:
                    win.set_focus()
                except Exception:
                    pass
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


def bt_decode_hci_via_folder(log_folder_path: str, log_path: str, timeout: int = 180) -> str | None:
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
    print(f"📂 bt_decode_hci_via_folder: {log_path}")

    # Skip decode if a complete set of output files already exists (.hci.txt + .txt.cfa + .txt.pcap).
    # Checking all three ensures the previous decode actually finished (sidecar files are only
    # produced after .hci.txt is fully written).
    existing = find_ready_hci(log_path)
    if existing:
        txt_cfa = log_path + ".txt.cfa"
        txt_pcap = log_path + ".txt.pcap"
        if os.path.exists(txt_cfa) and os.path.exists(txt_pcap):
            print(f"✅ HCI log already exists and decode is complete, skipping: {existing}")
            return existing
        print(f"⚠️ HCI log exists but sidecar files missing; re-decoding: {existing}")

    global active_bt_pid

    # 1) Construct the path to the tool and verify it exists.
    exe_path = os.path.abspath(os.path.join(os.path.dirname(__file__), 'ibtdrvlogparser.exe'))
    if not os.path.exists(exe_path):
        print(f"❌ Executable not found: {exe_path}")
        return None

    app = None

    # 2) Reuse existing tool if possible
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

    # 5) Put the folder path into the input box
    try:
        folder_input = app_window.child_window(auto_id="txt_parse_folder", control_type="Edit")
        folder_input.set_edit_text(log_folder_path)
        print(f"✅ Folder path set: {log_folder_path}")
    except Exception as e:
        print(f"❌ Failed to set folder path: {e}")

    # 6) Trigger "Decode Folder"
    try:
        decode_btn = app_window.child_window(auto_id="btn_parse_decode", control_type="Button")
        decode_btn.invoke()
        print("✅ Decode Folder triggered.")
    except Exception as e:
        print(f"❌ Failed to trigger Decode Folder: {e}")

    # 7) Wait for the decoded output (either naming convention) and open it
    hci_txt = candidate_hci_paths(log_path)[0]
    print(f"⏳ Waiting for HCI log until found (timeout={timeout}s): {hci_txt}")

    etl_txt = log_path + ".txt"
    txt_cfa = log_path + ".txt.cfa"
    txt_pcap = log_path + ".txt.pcap"

    time.sleep(0.5)
    # Although this may only occur in ManualSelect via IbtSnoopgen.
    close_warning_dialog()  # Dismiss benign "Systeminfo.txt not present" warning if it appears

    # Poll until the output file stabilizes.
    # Two separate timers are used to distinguish the two wait phases:
    #   file_wait_start: tracks how long we have been waiting for the file to appear.
    #   idle_start:      tracks how long the file size has been unchanged (write has stopped).
    # Exits when: size is stable for >= timeout seconds, BT tool dies,
    #             or file never appears within timeout seconds.
    last_size = -1
    file_wait_start = None  # timer: waiting for the file to appear
    idle_start = None       # timer: waiting for the file size to stop changing
    last_etl_txt_size = -1  # tracker for intermediate .txt file size
    etl_txt_idle_start = None  # timer: waiting for .txt to stop changing

    while True:
        if not psutil.pid_exists(active_bt_pid):
            active_bt_pid = None
            # Tool exited — check if output file exists and wait for it to stabilize.
            # The tool may close immediately after writing (or even while flushing),
            # so retry is_file_ready a few times before giving up.
            if os.path.exists(hci_txt):
                print(f"\n⏳ BT tool exited; waiting for HCI file to stabilize: {hci_txt}")
                for _attempt in range(5):
                    if is_file_ready(hci_txt):
                        print(f"✅ BT tool exited cleanly; HCI file ready: {hci_txt}")
                        return hci_txt
                    time.sleep(2)
                print(f"⚠️ BT tool exited but file not stable after retries: {hci_txt}")
                return None
            print(f"\n❌ BT tool closed unexpectedly during HCI wait (file not found).")
            return None
        
        # Proactively close any modal error dialog that might appear
        close_error_dialog()

        if os.path.exists(hci_txt):
            file_wait_start = None  # file has appeared; reset the appearance timer
            current_size = _get_true_file_size(hci_txt)

            if current_size != last_size:
                # File is still being written; reset the idle timer
                last_size = current_size
                idle_start = time.monotonic()
                print(f"\r📝 Decoding... size={current_size} bytes", end='', flush=True)
            else:
                # File size is unchanged; start or continue the idle timer
                if idle_start is None:
                    idle_start = time.monotonic()

                if os.path.exists(txt_cfa) and os.path.exists(txt_pcap):
                    print(f"\n✅ Detected .txt.cfa and .txt.pcap alongside .hci.txt; assuming decode complete.")
                    time.sleep(3)  # brief pause to ensure files are fully flushed and closed by the tool
                    _terminate_bt_tool("✅ BT tool closed after successful decode.")
                    return hci_txt
                
                # Keep to avoid .txt.cfa and .txt.pcap being written after .hci.txt is stable.
                if time.monotonic() - idle_start >= timeout:
                    if is_file_ready(hci_txt):
                        print()
                        # Decode complete — close the BT tool
                        _terminate_bt_tool("✅ BT tool closed after successful decode.")
                        return hci_txt
                    print(f"\n⚠️ File idle for {timeout}s but not ready: {hci_txt}")
                    _terminate_bt_tool("⚠️ BT tool terminated due to timeout.")
                    return None
        else:
            # File not yet created; start the appearance timer
            idle_start = None  # reset idle timer since file does not exist
            if os.path.exists(etl_txt):
                # Intermediate .txt present — track its size; start timeout only when it stops changing
                file_wait_start = None
                etl_txt_size = _get_true_file_size(etl_txt)
                if etl_txt_size != last_etl_txt_size:
                    last_etl_txt_size = etl_txt_size
                    etl_txt_idle_start = time.monotonic()
                    print(f"\r⏳ File .etl.txt writing... size={etl_txt_size} bytes", end='', flush=True)
                else:
                    if etl_txt_idle_start is None:
                        etl_txt_idle_start = time.monotonic()
                    if time.monotonic() - etl_txt_idle_start >= timeout:
                        print(f"\n⚠️ File .etl.txt idle for {timeout}s, .hci.txt never appeared: {hci_txt}")
                        _terminate_bt_tool("⚠️ BT tool terminated due to timeout.")
                        return None
                    print(f"\r⏳ File .etl.txt idle, waiting for .hci.txt...", end='', flush=True)
            else:
                last_etl_txt_size = -1
                etl_txt_idle_start = None
                if file_wait_start is None:
                    file_wait_start = time.monotonic()
                if time.monotonic() - file_wait_start >= timeout:
                    print(f"\n⚠️ File never appeared after {timeout}s: {etl_txt}")
                    _terminate_bt_tool("⚠️ BT tool terminated due to timeout.")
                    return None

        time.sleep(1)

# This function is not accessed.
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
    timeout: int = 180,
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
        timeout: Seconds to wait both for the file to appear and for the file size
                 to remain unchanged (write complete) before opening the viewer.
        should_stop: Optional callable that returns True if this operation should be aborted.
        filter_path: Optional path to a filter file for the text analysis tool.

    Returns:
        int: The process ID (PID) of the BT tool, or None if failed/aborted.

    Notes:
        - Uses the same attach-or-launch pattern as other functions.
        - Uses 'invoke()' on the decode button to avoid focus issues.
    """
    global active_bt_pid

    # 0) If this ETL is already decoded (either naming convention), skip the
    # decode entirely — just open the existing .hci.txt in the viewer. Avoids
    # a pointless re-decode (and the artifactory symbol dependency it carries).
    existing = find_ready_hci(log_path)
    if existing:
        txt_cfa = log_path + ".txt.cfa"
        txt_pcap = log_path + ".txt.pcap"
        if os.path.exists(txt_cfa) and os.path.exists(txt_pcap):
            print(f"✅ HCI log already exists and decode is complete, skipping: {existing}")
            open_with_text_analysis_tool(existing, filter_path=filter_path)
            return active_bt_pid
        print(f"⚠️ HCI log exists but sidecar files missing; re-decoding: {existing}")

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

    # 7) Wait for the decoded output (either naming convention) and open it
    hci_txt = candidate_hci_paths(log_path)[0]
    print(f"⏳ Waiting for HCI log until found (timeout={timeout}s): {hci_txt}")

    etl_txt = log_path + ".txt"
    txt_cfa = log_path + ".txt.cfa"
    txt_pcap = log_path + ".txt.pcap"

    time.sleep(0.5)
    # Although this may only occur in ManualSelect via IbtSnoopgen.
    close_warning_dialog()  # Dismiss benign "Systeminfo.txt not present" warning if it appears

    # Poll until the output file stabilizes and has been opened.
    # Two separate timers distinguish the two wait phases:
    #   file_wait_start: tracks how long we have been waiting for the file to appear.
    #   idle_start:      tracks how long the file size has been unchanged (write has stopped).
    last_size = -1
    file_wait_start = None  # timer: waiting for the file to appear
    idle_start = None       # timer: waiting for the file size to stop changing
    last_etl_txt_size = -1  # tracker for intermediate .txt file size
    etl_txt_idle_start = None  # timer: waiting for .txt to stop changing

    while True:
        # Check if this operation was superseded by another
        if should_stop and should_stop():
            print("⚠️ AutoFolder operation superseded, stopping wait.")
            return None

        # Check if process is still alive
        if not psutil.pid_exists(active_bt_pid):
            active_bt_pid = None
            # Tool exited — check if output file exists and wait for it to stabilize.
            if os.path.exists(hci_txt):
                print(f"⏳ BT tool exited; waiting for HCI file to stabilize: {hci_txt}")
                for _attempt in range(5):
                    if is_file_ready(hci_txt):
                        print(f"✅ BT tool exited cleanly; HCI file ready: {hci_txt}")
                        open_with_text_analysis_tool(hci_txt, filter_path=filter_path)
                        return None  # Return None so _finish_analysis emits autofolder_complete
                    time.sleep(2)
                print(f"⚠️ BT tool exited but file not stable after retries: {hci_txt}")
                return None
            print("❌ BT tool closed during HCI wait (file not found).")
            return None

        # Proactively close any modal error dialog that might appear
        close_error_dialog()

        if os.path.exists(hci_txt):
            file_wait_start = None  # file has appeared; reset the appearance timer
            current_size = _get_true_file_size(hci_txt)

            if current_size != last_size:
                # File is still being written; reset the idle timer
                last_size = current_size
                idle_start = time.monotonic()
                print(f"\r📝 Decoding... size={current_size} bytes", end='', flush=True)
            else:
                # File size is unchanged; start or continue the idle timer
                if idle_start is None:
                    idle_start = time.monotonic()

                if os.path.exists(txt_cfa) and os.path.exists(txt_pcap):
                    print(f"\n✅ Detected .txt.cfa and .txt.pcap alongside .hci.txt; assuming decode complete.")
                    time.sleep(3)  # brief pause to ensure files are fully flushed and closed by the tool
                    if open_with_text_analysis_tool(hci_txt, filter_path=filter_path):
                        print("✅ Opened HCI log with TextAnalysisTool.NET.")
                    else:
                        print("⚠️ Failed to open HCI log with TextAnalysisTool.NET.")
                    _terminate_bt_tool("✅ BT tool closed after successful decode.")
                    return None  # Return None so _finish_analysis emits autofolder_complete
                
                # Keep to avoid .txt.cfa and .txt.pcap being written after .hci.txt is stable.
                if time.monotonic() - idle_start >= timeout:
                    if is_file_ready(hci_txt):
                        print(f"📂 HCI log is ready: {hci_txt}")
                        if open_with_text_analysis_tool(hci_txt, filter_path=filter_path):
                            print("✅ Opened HCI log with TextAnalysisTool.NET.")
                        else:
                            print("⚠️ Failed to open HCI log with TextAnalysisTool.NET.")
                        # Decode complete — close the BT tool
                        _terminate_bt_tool("✅ BT tool closed after successful decode.")
                        return None  # Return None so _finish_analysis emits autofolder_complete immediately
                    else:
                        print(f"⚠️ File idle for {timeout}s but not ready: {hci_txt}")
                        _terminate_bt_tool("⚠️ BT tool terminated due to timeout.")
                        return None
        else:
            # File not yet created; start the appearance timer
            idle_start = None  # reset idle timer since file does not exist
            if os.path.exists(etl_txt):
                # Intermediate .txt present — track its size; start timeout only when it stops changing
                file_wait_start = None
                etl_txt_size = _get_true_file_size(etl_txt)
                if etl_txt_size != last_etl_txt_size:
                    last_etl_txt_size = etl_txt_size
                    etl_txt_idle_start = time.monotonic()
                    print(f"\r⏳ File .etl.txt writing... size={etl_txt_size} bytes", end='', flush=True)
                else:
                    if etl_txt_idle_start is None:
                        etl_txt_idle_start = time.monotonic()
                    if time.monotonic() - etl_txt_idle_start >= timeout:
                        print(f"\n⚠️ File .etl.txt idle for {timeout}s, .hci.txt never appeared: {hci_txt}")
                        _terminate_bt_tool("⚠️ BT tool terminated due to timeout.")
                        return None
                    print(f"\r⏳ File .etl.txt idle, waiting for .hci.txt...", end='', flush=True)
            else:
                last_etl_txt_size = -1
                etl_txt_idle_start = None
                if file_wait_start is None:
                    file_wait_start = time.monotonic()
                if time.monotonic() - file_wait_start >= timeout:
                    print(f"⚠️ File never appeared after {timeout}s: {hci_txt}")
                    _terminate_bt_tool("⚠️ BT tool terminated due to timeout.")
                    return None

        time.sleep(1)