from pywinauto.application import Application
from pywinauto import Desktop
from pywinauto.keyboard import send_keys
import os
import time
import psutil
import subprocess, glob
import win32api
import math
import re

# Global variable to cache a running instance's PID so we can reconnect
# instead of launching a new GUI process every time.
active_bt_pid = None

# Temporary threshold large enough to disable split processing.
SPLIT_SIZE_THRESHOLD_BYTES = 1024 * 1024 * 1024 * 1024


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


def _get_splitter_list_count(app_window) -> int:
    """Return current ETL-Splitter list item count, or 0 if unavailable."""
    try:
        listbox = app_window.child_window(auto_id="lb_etlsplitter", control_type="List")
        if listbox.exists(timeout=0.5):
            return listbox.wrapper_object().item_count()
    except Exception:
        pass
    return 0


def _find_open_dialog(main_hwnd, timeout_sec: int = 10):
    """Find the file-open dialog by standard controls, excluding the main window."""
    deadline = time.monotonic() + timeout_sec
    while time.monotonic() < deadline:
        for win in Desktop(backend="uia").windows():
            try:
                if main_hwnd is not None and getattr(win, "handle", None) == main_hwnd:
                    continue
                file_name_ctrl = win.child_window(auto_id="1148")
                open_btn_ctrl = win.child_window(auto_id="1", control_type="Button")
                if file_name_ctrl.exists(timeout=0.1) and open_btn_ctrl.exists(timeout=0.1):
                    return win
            except Exception:
                continue
        time.sleep(0.2)
    return None


def _submit_path_in_open_dialog(open_dialog, file_path: str) -> None:
    """Set file path in the open dialog and confirm selection."""
    try:
        open_dialog.set_focus()
    except Exception:
        pass

    file_edit = None
    try:
        combo_1148 = open_dialog.child_window(auto_id="1148", control_type="ComboBox")
        if combo_1148.exists(timeout=0.5):
            file_edit = combo_1148.child_window(control_type="Edit")
    except Exception:
        file_edit = None

    if file_edit is None or not file_edit.exists(timeout=0.3):
        try:
            file_edit = open_dialog.child_window(auto_id="1148", control_type="Edit")
        except Exception:
            file_edit = None

    if file_edit is None or not file_edit.exists(timeout=0.3):
        try:
            file_edit = open_dialog.child_window(control_type="Edit", found_index=0)
        except Exception:
            file_edit = None

    if file_edit is not None and file_edit.exists(timeout=0.3):
        file_edit_wrapper = file_edit.wrapper_object()
        try:
            file_edit_wrapper.set_focus()
        except Exception:
            pass
        try:
            file_edit_wrapper.set_edit_text(file_path)
        except Exception:
            try:
                file_edit_wrapper.type_keys("^a{BACKSPACE}", with_spaces=True)
                file_edit_wrapper.type_keys(file_path, with_spaces=True)
            except Exception:
                open_dialog.set_focus()
                send_keys("^a{BACKSPACE}")
                send_keys(file_path, with_spaces=True)
    else:
        open_dialog.set_focus()
        send_keys(file_path, with_spaces=True)

    submitted = False
    try:
        open_btn = open_dialog.child_window(auto_id="1", control_type="Button")
        if open_btn.exists(timeout=0.3):
            try:
                open_btn.wrapper_object().click_input()
            except Exception:
                open_btn.wrapper_object().click()
            submitted = True
    except Exception:
        submitted = False

    if not submitted:
        try:
            for btn in open_dialog.descendants(control_type="Button"):
                t = (btn.window_text() or "").replace("&", "")
                if t in ("Open", "開啟", "打开"):
                    try:
                        btn.click_input()
                    except Exception:
                        btn.click()
                    submitted = True
                    break
        except Exception:
            submitted = False

    if not submitted:
        open_dialog.set_focus()
        send_keys("{ENTER}")


def _add_etl_to_splitter(app_window, file_path: str, main_hwnd) -> bool:
    """Add ETL to splitter list via browse flow, then fallback to LB_ADDSTRING."""
    added_via_browse = False
    before_count = _get_splitter_list_count(app_window)

    try:
        browse_btn = app_window.child_window(auto_id="btn_spliter_browse", control_type="Button")
        if browse_btn.exists(timeout=1):
            browse_wrapper = browse_btn.wrapper_object()
            print("🖱️ Clicking 'Browse' for ETL-Splitter...")
            try:
                browse_wrapper.click_input()
            except Exception as e_click_browse:
                print(f"⚠️ browse click_input() failed: {e_click_browse}; trying click()")
                browse_wrapper.click()
            print("✅ Clicked 'Browse' for ETL-Splitter")

            open_dialog = _find_open_dialog(main_hwnd, timeout_sec=10)
            if open_dialog is not None:
                _submit_path_in_open_dialog(open_dialog, file_path)
            else:
                print("⚠️ File-open dialog not detected after clicking Browse; trying blind input fallback")
                try:
                    send_keys("^a{BACKSPACE}")
                    send_keys(file_path, with_spaces=True)
                    send_keys("{ENTER}")
                except Exception as e_blind:
                    print(f"⚠️ Blind input fallback failed: {e_blind}")

            verify_deadline = time.monotonic() + 5
            while time.monotonic() < verify_deadline:
                after_count = _get_splitter_list_count(app_window)
                if after_count > before_count:
                    added_via_browse = True
                    print(f"✅ ETL-Splitter: added via Browse flow: {file_path}")
                    break
                time.sleep(0.2)

            if not added_via_browse:
                print("⚠️ Browse flow submitted but list item count did not increase")
        else:
            print("⚠️ 'Browse' button for ETL-Splitter not found")
    except Exception as e_browse:
        print(f"⚠️ Failed to add ETL via Browse flow: {e_browse}")

    if not added_via_browse:
        LB_ADDSTRING = 0x0180
        listbox = app_window.child_window(auto_id="lb_etlsplitter", control_type="List")
        hwnd = listbox.wrapper_object().handle
        win32api.SendMessage(hwnd, LB_ADDSTRING, 0, file_path)
        print(f"⚠️ Fallback: added via LB_ADDSTRING (may not enable Start): {file_path}")

    return added_via_browse


def _set_split_count_in_splitter(app_window, split_count: int) -> None:
    """Set split count in ETL-Splitter spinner."""
    try:
        splitter_pane = app_window.child_window(
            title="ETL-Splitter", auto_id="tab_splitter", control_type="Pane"
        )
        if splitter_pane.exists(timeout=1):
            spinner_edit = splitter_pane.child_window(title="Spinner", control_type="Edit")
            if spinner_edit.exists(timeout=1):
                spinner_edit.wrapper_object().set_edit_text(str(split_count))
                print(f"✅ Split count set to {split_count}")
            else:
                print("⚠️ Spinner Edit not found within Pane")
        else:
            print("⚠️ Pane (tab_splitter) not found")
    except Exception as e:
        print(f"⚠️ Failed to set split count: {e}")


def _trigger_split_start(app_window) -> None:
    """Click the Start Etl Split button with diagnostics."""
    try:
        split_btn = app_window.child_window(auto_id="btn_splitter_start", control_type="Button")
        if split_btn.exists(timeout=1):
            split_wrapper = split_btn.wrapper_object()
            print("🔍 Button state:")
            print(f"   Enabled: {split_wrapper.is_enabled()}")
            print(f"   Window text: {split_wrapper.window_text()}")
            print("🔍 ListBox state:")
            print(f"   Item count: {_get_splitter_list_count(app_window)}")
            try:
                split_wrapper.invoke()
                print("✅ Split triggered via invoke()")
            except Exception as e_invoke:
                print(f"⚠️ invoke() failed: {e_invoke}. Trying click()...")
                try:
                    split_wrapper.set_focus()
                    time.sleep(0.2)
                    split_wrapper.click()
                    print("✅ Split triggered via click()")
                except Exception as e_click:
                    print(f"⚠️ click() also failed: {e_click}")
        else:
            print("⚠️ 'Start Etl Split' button not found")
    except Exception as e:
        print(f"⚠️ Failed to trigger split button: {e}")


def _wait_for_split_outputs(file_path: str, split_count: int, wait_timeout: int = 300, stable_seconds: int = 60) -> bool:
    """Return True when expected split outputs are stable; False otherwise."""
    split_dir = os.path.dirname(file_path)
    base_name = os.path.splitext(os.path.basename(file_path))[0]
    wait_start = time.monotonic()
    prev_parts_signature = None
    stable_start = None

    while time.monotonic() - wait_start < wait_timeout:
        parts = sorted(glob.glob(os.path.join(split_dir, f"{base_name}_split*.etl")))
        parts_signature = []
        for p in parts:
            try:
                parts_signature.append((p, os.path.getsize(p)))
            except OSError:
                parts_signature.append((p, -1))
        parts_signature = tuple(parts_signature)

        if parts_signature != prev_parts_signature:
            prev_parts_signature = parts_signature
            stable_start = time.monotonic()
            if parts:
                print(
                    f"⏳ Splitting in progress: {len(parts)}/{split_count} part(s), "
                    f"sizes={[s for _, s in parts_signature]}"
                )
        else:
            if stable_start is None:
                stable_start = time.monotonic()

            if time.monotonic() - stable_start >= stable_seconds:
                if len(parts) >= split_count:
                    print(
                        f"✅ Split complete: {len(parts)} part(s) stable for "
                        f"{stable_seconds}s for {file_path}"
                    )
                    return True

                print(
                    f"⚠️ Split stopped but incomplete: {len(parts)}/{split_count} part(s) "
                    f"stable for {stable_seconds}s"
                )
                return False
        time.sleep(2)

    return False


def _rename_split_source(file_path: str) -> None:
    """Rename original ETL to .split after split success."""
    try:
        if os.path.exists(file_path):
            renamed_path = file_path + ".split"
            if os.path.exists(renamed_path):
                renamed_path = file_path + f".split.{int(time.time()*1000)}"
            os.rename(file_path, renamed_path)
            print(f"📦 Renamed split source to prevent reprocessing: {file_path} → {renamed_path}")
    except Exception as e_rename:
        print(f"⚠️ Failed to rename split source {file_path}: {e_rename}")


def _extract_split_index(file_path: str) -> int:
    """Extract numeric suffix from '<name>_split<N>.etl'; return -1 if missing."""
    m = re.search(r"_split(\d+)\.etl$", os.path.basename(file_path), re.IGNORECASE)
    return int(m.group(1)) if m else -1


def _pick_last_split_part(original_file_path: str) -> str | None:
    """Pick split part with highest numeric index for an original ETL file."""
    split_dir = os.path.dirname(original_file_path)
    base_name = os.path.splitext(os.path.basename(original_file_path))[0]
    parts = glob.glob(os.path.join(split_dir, f"{base_name}_split*.etl"))
    if not parts:
        return None
    return max(parts, key=_extract_split_index)


def _collect_large_etl_files(log_folder_path: str) -> list:
    """Collect ETL files requiring split."""
    split_file_path = []
    for file in os.listdir(log_folder_path):
        if file.lower().endswith(".etl"):
            file_path = os.path.join(log_folder_path, file)
            file_size_bytes = os.path.getsize(file_path)
            if file_size_bytes >= SPLIT_SIZE_THRESHOLD_BYTES:
                split_file_path.append({"path": file_path, "size": file_size_bytes})
                print(f"⚠️ {file}: File size is {file_size_bytes / (1024 * 1024):.2f} MB (>1GB). Splitting...")
    return split_file_path


def _split_large_etl_files(app_window, log_folder_path: str, main_hwnd) -> dict:
    """Run ETL-Splitter flow for large ETLs in the folder.

    Returns:
        dict: original ETL path -> chosen split target path (highest split index).
    """
    split_target_map = {}
    try:
        split_file_path = _collect_large_etl_files(log_folder_path)
        if not split_file_path:
            return split_target_map

        split_tab = app_window.child_window(title="ETL-Splitter", control_type="TabItem").wrapper_object()
        split_tab.select()
        print("✅ Selected 'ETL-Splitter' tab.")
        time.sleep(0.5)

        for file_info in split_file_path:
            try:
                file_path = file_info["path"]
                file_size_bytes = file_info["size"]
                split_count = math.ceil(file_size_bytes / (512 * 1024 * 1024))
                print(
                    f"📐 Split count for {os.path.basename(file_path)}: {split_count} "
                    f"(size={file_size_bytes / (1024 * 1024):.1f} MB)"
                )

                _set_split_count_in_splitter(app_window, split_count)
                _add_etl_to_splitter(app_window, file_path, main_hwnd)
                _trigger_split_start(app_window)

                if _wait_for_split_outputs(file_path, split_count, wait_timeout=300, stable_seconds=15):
                    _rename_split_source(file_path)
                    selected_split = _pick_last_split_part(file_path)
                    if selected_split:
                        split_target_map[file_path] = selected_split
                        print(f"✅ Selected chatbot input target: {selected_split}")
                    else:
                        print(f"⚠️ Split succeeded but no split part found for: {file_path}")
                else:
                    print(f"⚠️ Split not completed successfully for {file_path}")

            except Exception as e:
                print(f"⚠️ Failed to split {file_info.get('path')}: {e}")
    except Exception as e:
        print(f"⚠️ Failed to check file size for splitting: {e}")
    return split_target_map


def bt_decode_hci_via_folder(log_folder_path: str, log_path: str, etl_txt_timeout: int = 180, hci_txt_timeout: int = 15) -> str | None:
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
        etl_txt_timeout: Max seconds to wait for the .etl.txt file.
        hci_txt_timeout: Max seconds to wait for the .hci.txt file.

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

    main_hwnd = None
    try:
        main_hwnd = app_window.wrapper_object().handle
    except Exception:
        main_hwnd = None

    # 4.5) Split oversized ETLs via ETL-Splitter before Decode Folder.
    # Decode still runs on the whole folder; map is only for final chatbot input selection.
    split_target_map = _split_large_etl_files(app_window, log_folder_path, main_hwnd)
    chatbot_input_etl = split_target_map.get(log_path, log_path)
    if chatbot_input_etl != log_path:
        print(f"🎯 Chatbot input will use last split part: {chatbot_input_etl}")

    # Rename all the other ETL files to avoid decoding them (we only want the chatbot_input_etl to be decoded).
    normalized_target = os.path.normcase(os.path.abspath(chatbot_input_etl))
    for etl_file in os.listdir(log_folder_path):
        etl_file_path = os.path.join(log_folder_path, etl_file)
        if etl_file_path.lower().endswith(".etl") and os.path.normcase(os.path.abspath(etl_file_path)) != normalized_target:
            try:
                renamed_path = etl_file_path + ".skip"
                os.rename(etl_file_path, renamed_path)
                print(f"📦 Renamed non-target ETL to avoid decode: {etl_file_path} → {renamed_path}")
            except Exception as e_rename:
                print(f"⚠️ Failed to rename {etl_file_path}: {e_rename}")
    
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

    # 7) Wait for the decoded output used by chatbot input path.
    hci_txt = candidate_hci_paths(chatbot_input_etl)[0]
    print(f"⏳ Waiting for HCI log until found: {hci_txt}")

    etl_txt = chatbot_input_etl + ".txt"
    txt_cfa = chatbot_input_etl + ".txt.cfa"
    txt_pcap = chatbot_input_etl + ".txt.pcap"

    time.sleep(0.5)
    # Although this may only occur in ManualSelect via IbtSnoopgen.
    close_warning_dialog()  # Dismiss benign "Systeminfo.txt not present" warning if it appears

    # Poll until the output file stabilizes.
    # Two separate timers are used to distinguish the two wait phases:
    #   file_wait_start: tracks how long we have been waiting for the file to appear.
    #   idle_start:      tracks how long the file size has been unchanged (write has stopped).
    # Exits when: size is stable for >= timeout seconds, BT tool dies,
    #             or file never appears within timeout seconds.
    #
    # IMPORTANT: Timeouts below are based on the selected ETL's outputs. Other ETLs are
    # renamed to ".skip" above so Decode Folder focuses on `chatbot_input_etl` only.
    last_size = -1
    file_wait_start = None  # timer: waiting for the file to appear
    idle_start = None       # timer: waiting for the file size to stop changing
    last_etl_txt_size = -1  # tracker for intermediate .txt file size
    etl_txt_idle_start = None  # timer: waiting for .txt to stop changing
    last_folder_activity_snapshot = None  # tracks any decode activity in folder

    # def _folder_has_decode_activity() -> bool:
    #     """Check if any .txt or .hci.txt in the folder is still being written."""
    #     nonlocal last_folder_activity_snapshot
    #     try:
    #         snapshot = {}
    #         for f in os.listdir(log_folder_path):
    #             if f.lower().endswith((".txt", ".hci.txt")):
    #                 fp = os.path.join(log_folder_path, f)
    #                 snapshot[fp] = _get_true_file_size(fp)
    #         if snapshot != last_folder_activity_snapshot:
    #             last_folder_activity_snapshot = snapshot
    #             return True  # something changed → still active
    #         return False  # nothing changed → idle
    #     except Exception:
    #         return False

    def _recover_rename_etl_files() -> None:
        """Rename any .etl.skip back to .etl to restore original state."""
        for file in os.listdir(log_folder_path):
            if file.lower().endswith(".etl.skip"):
                skip_path = os.path.join(log_folder_path, file)
                original_path = skip_path[:-5]  # remove ".skip"
                try:
                    os.rename(skip_path, original_path)
                    print(f"🔄 Restored skipped ETL: {skip_path} → {original_path}")
                except Exception as e_restore:
                    print(f"⚠️ Failed to restore skipped ETL {skip_path}: {e_restore}")

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
                        _recover_rename_etl_files()  # restore any skipped ETLs
                        return hci_txt
                    time.sleep(2)
                print(f"⚠️ BT tool exited but file not stable after retries: {hci_txt}")
                _recover_rename_etl_files()
                return None
            print(f"\n❌ BT tool closed unexpectedly during HCI wait (file not found).")
            _recover_rename_etl_files()
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
                print(f"\r📝 Decoding... size={current_size} bytes\033[K", end='', flush=True)
            else:
                # File size is unchanged; start or continue the idle timer
                if idle_start is None:
                    idle_start = time.monotonic()

                if is_file_ready(hci_txt) and os.path.exists(txt_cfa) and os.path.exists(txt_pcap):
                    print(f"\n✅ Detected .txt.cfa and .txt.pcap alongside .hci.txt; assuming decode complete.")
                    time.sleep(3)  # brief pause to ensure files are fully flushed and closed by the tool
                    _terminate_bt_tool("✅ BT tool closed after successful decode.")
                    _recover_rename_etl_files()  # restore any skipped ETLs
                    return hci_txt
                
                # Keep to avoid .txt.cfa and .txt.pcap being written after .hci.txt is stable.
                if time.monotonic() - idle_start >= hci_txt_timeout:
                    print(f"\n⚠️ File .hci.txt idle for {hci_txt_timeout}s but not ready: {hci_txt}")
                    _terminate_bt_tool("⚠️ BT tool terminated due to .hci.txt timeout.")
                    _recover_rename_etl_files()  # restore any skipped ETLs
                    return None
        else:
            # File not yet created; check if BT tool is still actively decoding anything.
            idle_start = None  # reset idle timer since file does not exist

            if os.path.exists(etl_txt):
                # Intermediate .txt present — track its size; start timeout only when it stops changing
                file_wait_start = None
                etl_txt_size = _get_true_file_size(etl_txt)
                if etl_txt_size != last_etl_txt_size:
                    last_etl_txt_size = etl_txt_size
                    etl_txt_idle_start = time.monotonic()
                    print(f"\r⏳ File .etl.txt writing... size={etl_txt_size} bytes\033[K", end='', flush=True)
                else:
                    if etl_txt_idle_start is None:
                        etl_txt_idle_start = time.monotonic()
                    if time.monotonic() - etl_txt_idle_start >= etl_txt_timeout:
                        print(f"\n⚠️ File .etl.txt idle for {etl_txt_timeout}s, .hci.txt never appeared: {hci_txt}")
                        _terminate_bt_tool("⚠️ BT tool terminated due to .etl.txt timeout.")
                        _recover_rename_etl_files()
                        return None
                    print(f"\r⏳ File .etl.txt idle, waiting for .hci.txt...\033[K", end='', flush=True)
            else:
                last_etl_txt_size = -1
                etl_txt_idle_start = None
                if file_wait_start is None:
                    file_wait_start = time.monotonic()
                if time.monotonic() - file_wait_start >= etl_txt_timeout:
                    print(f"\n⚠️ File .etl.txt never appeared after {etl_txt_timeout}s: {etl_txt}")
                    _terminate_bt_tool("⚠️ BT tool terminated due to .etl.txt timeout.")
                    _recover_rename_etl_files()
                    return None

        time.sleep(1)


def bt_decode_via_cli(
    log_folder_path: str,
    log_path: str,
    etl_txt_timeout: int = 180,
    hci_txt_timeout: int = 15,
    skip_non_target: bool = True,
) -> str | None:
    """
    Decode an ETL folder via CLI (ibtdrvlogparser_cli.exe) without any GUI automation.

    A lighter alternative to bt_decode_hci_via_folder(): no pywinauto dependency,
    no visible GUI window. The CLI tool runs synchronously and exits when the decode
    is complete, so no polling loop is needed.

    Commands used:
        Split:  ibtdrvlogparser_cli.exe split <etl_path> -n <count>
        Decode: ibtdrvlogparser_cli.exe decode <log_folder_path>

    Args:
        log_folder_path: Directory that contains the ETL file(s) to decode.
        log_path:        Full path of the target ETL file (without .hci.txt suffix).
                         The function looks for '<log_path>.hci.txt' after decode.
        etl_txt_timeout: Max seconds to wait for the .etl.txt file (also used when
                         neither file has appeared yet).
        hci_txt_timeout: Max seconds the .hci.txt output may be idle (no size change)
                         before the decode is considered stuck and the process is killed.

    Returns:
        str path to the generated .hci.txt, or None on failure / timeout.
    """
    print(f"📂 bt_decode_via_cli: {log_path}")

    # Skip decode if a complete set of output files already exists.
    existing = find_ready_hci(log_path)
    if existing:
        txt_cfa = log_path + ".txt.cfa"
        txt_pcap = log_path + ".txt.pcap"
        if os.path.exists(txt_cfa) and os.path.exists(txt_pcap):
            print(f"✅ HCI log already exists and decode is complete, skipping: {existing}")
            return existing
        print(f"⚠️ HCI log exists but sidecar files missing; re-decoding: {existing}")

    # 1) Locate the CLI executable next to this script.
    cli_exe = os.path.abspath(os.path.join(os.path.dirname(__file__), 'ibtdrvlogparser_cli.exe'))
    if not os.path.exists(cli_exe):
        print(f"❌ CLI executable not found: {cli_exe}")
        return None

    def _recover_rename_etl_files() -> None:
        """Rename any .etl.skip back to .etl to restore original state."""
        for file in os.listdir(log_folder_path):
            if file.lower().endswith(".etl.skip"):
                skip_path = os.path.join(log_folder_path, file)
                original_path = skip_path[:-5]  # remove ".skip"
                try:
                    os.rename(skip_path, original_path)
                    print(f"🔄 Restored skipped ETL: {skip_path} → {original_path}")
                except Exception as e_restore:
                    print(f"⚠️ Failed to restore skipped ETL {skip_path}: {e_restore}")

    # 2) Split any oversized ETLs (>=1 GB) via CLI before decode.
    split_target_map = {}
    if skip_non_target:
        large_etls = _collect_large_etl_files(log_folder_path)
        for file_info in large_etls:
            etl_path = file_info["path"]
            etl_size = file_info["size"]
            split_count = math.ceil(etl_size / (512 * 1024 * 1024))
            print(
                f"📐 Splitting {os.path.basename(etl_path)} into {split_count} parts "
                f"(size={etl_size / (1024 * 1024):.1f} MB)"
            )
            split_cmd = [cli_exe, "split", etl_path, "-n", str(split_count)]
            split_proc = None

            try:
                split_proc = subprocess.Popen(
                    split_cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
                split_dir = os.path.dirname(etl_path)
                base_name = os.path.splitext(os.path.basename(etl_path))[0]
                split_wait_start = time.monotonic()
                prev_parts_signature = None
                stable_start = None
                split_stable_seconds = 15
                split_wait_timeout = 300
                split_success = False

                while time.monotonic() - split_wait_start < split_wait_timeout:
                    ret = split_proc.poll()

                    parts = sorted(glob.glob(os.path.join(split_dir, f"{base_name}_split*.etl")))
                    parts_signature = tuple((p, _get_true_file_size(p)) for p in parts)

                    if parts_signature != prev_parts_signature:
                        prev_parts_signature = parts_signature
                        stable_start = time.monotonic()
                        if parts:
                            print(
                                f"\r⏳ Splitting in progress: {len(parts)}/{split_count} part(s), "
                                f"sizes={[s for _, s in parts_signature]}\033[K",
                                end='', flush=True,
                            )
                    else:
                        if stable_start is None:
                            stable_start = time.monotonic()
                        if time.monotonic() - stable_start >= split_stable_seconds:
                            if len(parts) >= split_count:
                                print(
                                    f"\n✅ Split complete: {len(parts)} part(s) stable for "
                                    f"{split_stable_seconds}s for {etl_path}"
                                )
                                split_success = True
                                if split_proc.poll() is None:
                                    split_proc.kill()
                                    split_proc.wait()
                                break
                            elif split_proc.poll() is not None:
                                # Process has already exited and parts are still incomplete → genuine failure
                                print(
                                    f"\n⚠️ Split stopped but incomplete: {len(parts)}/{split_count} "
                                    f"part(s) stable for {split_stable_seconds}s (process exited)"
                                )
                                break
                            else:
                                # Process is still running — it may be preparing the next part;
                                # reset stable_start and keep waiting.
                                print(
                                    f"\r⏳ Split parts stable for {split_stable_seconds}s but process "
                                    f"still running ({len(parts)}/{split_count}); waiting...\033[K",
                                    end='', flush=True,
                                )
                                stable_start = time.monotonic()

                    if ret is not None:
                        # Process exited naturally; read buffered output and do a final scan.
                        stdout_data, stderr_data = split_proc.communicate()
                        for line in stdout_data.splitlines():
                            print(f"  [CLI] {line}")
                        if ret != 0:
                            print(f"\n⚠️ Split CLI exited with rc={ret}: {stderr_data.strip()}")
                        else:
                            parts = sorted(glob.glob(os.path.join(split_dir, f"{base_name}_split*.etl")))
                            if len(parts) >= split_count:
                                print(f"\n✅ Split completed (process exited cleanly) for: {etl_path}")
                                split_success = True
                            else:
                                print(
                                    f"\n⚠️ Split process exited but only "
                                    f"{len(parts)}/{split_count} parts found"
                                )
                        break

                    time.sleep(2)
                else:
                    print(f"\n⚠️ Split timed out after {split_wait_timeout}s for: {etl_path}")
                    if split_proc.poll() is None:
                        split_proc.kill()
                        split_proc.wait()

                if split_success:
                    _rename_split_source(etl_path)
                    selected_split = _pick_last_split_part(etl_path)
                    if selected_split:
                        split_target_map[etl_path] = selected_split
                        print(f"✅ Selected chatbot input target: {selected_split}")
                    else:
                        print(f"⚠️ Split succeeded but no split part found for: {etl_path}")

            except Exception as e:
                print(f"⚠️ Failed to split {etl_path}: {e}")
                if split_proc and split_proc.poll() is None:
                    try:
                        split_proc.kill()
                    except Exception:
                        pass

    # 3) Determine which ETL is the chatbot / analysis target.
    chatbot_input_etl = split_target_map.get(log_path, log_path)
    if chatbot_input_etl != log_path:
        print(f"🎯 Chatbot input will use last split part: {chatbot_input_etl}")

    # 4) Rename all non-target ETLs to .skip so decode focuses on chatbot_input_etl only.
    if skip_non_target:
        normalized_target = os.path.normcase(os.path.abspath(chatbot_input_etl))
        for etl_file in os.listdir(log_folder_path):
            etl_file_path = os.path.join(log_folder_path, etl_file)
            if (etl_file_path.lower().endswith(".etl")
                    and os.path.normcase(os.path.abspath(etl_file_path)) != normalized_target):
                try:
                    renamed_path = etl_file_path + ".skip"
                    os.rename(etl_file_path, renamed_path)
                    print(f"📦 Renamed non-target ETL to avoid decode: {etl_file_path} → {renamed_path}")
                except Exception as e_rename:
                    print(f"⚠️ Failed to rename {etl_file_path}: {e_rename}")

    # 5) Run CLI decode on the folder (Popen — file-size idle-based timeout).
    # Uses Popen instead of subprocess.run so we can monitor .hci.txt size while
    # the process runs.  The decode is considered stuck when the output file has
    # not grown for `decode_timeout` seconds; the process is then killed.
    decode_cmd = [cli_exe, "decode", log_folder_path]
    print(f"🚀 Running CLI decode: {' '.join(decode_cmd)}")
    hci_txt_expected = candidate_hci_paths(chatbot_input_etl)[0]
    etl_txt_expected = chatbot_input_etl + ".txt"
    txt_cfa_expected = chatbot_input_etl + ".txt.cfa"
    txt_pcap_expected = chatbot_input_etl + ".txt.pcap"
    proc = None
    try:
        proc = subprocess.Popen(
            decode_cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        last_size = None           # None until .hci.txt first appears
        idle_start = None          # reset whenever .hci.txt size changes
        last_etl_txt_size = -1     # tracker for intermediate .etl.txt size
        etl_txt_idle_start = None  # timer: .etl.txt stopped changing
        no_file_start = time.monotonic()  # fallback: neither file appears

        while True:
            ret = proc.poll()
            if ret is not None:
                # Process finished — collect buffered output.
                stdout, stderr = proc.communicate()
                for line in stdout.splitlines():
                    print(f"  [CLI] {line}")
                if ret != 0:
                    print(f"❌ Decode CLI failed (rc={ret}): {stderr.strip()}")
                    _recover_rename_etl_files()
                    return None
                print("✅ CLI decode process completed successfully.")
                break

            if os.path.exists(hci_txt_expected):
                # .hci.txt appeared — track its size idle
                current_size = _get_true_file_size(hci_txt_expected)
                no_file_start = None
                etl_txt_idle_start = None
                if current_size != last_size:
                    last_size = current_size
                    idle_start = time.monotonic()
                    print(f"\r📝 Decoding... size={current_size} bytes\033[K", end='', flush=True)
                else:
                    if idle_start is None:
                        idle_start = time.monotonic()
                    if is_file_ready(hci_txt_expected) and os.path.exists(txt_cfa_expected) and os.path.exists(txt_pcap_expected):
                        print(f"\n✅ Detected .txt.cfa and .txt.pcap alongside .hci.txt; decode complete.")
                        if proc.poll() is None:
                            proc.kill()
                            proc.wait()
                        break
                    if time.monotonic() - idle_start >= hci_txt_timeout:
                        print(f"\n⚠️ Decode idle for {hci_txt_timeout}s (file size unchanged). Killing process.")
                        proc.kill()
                        proc.wait()
                        _recover_rename_etl_files()
                        return None
            elif os.path.exists(etl_txt_expected):
                # Intermediate .etl.txt present — track its size; timeout only when it stops changing
                no_file_start = None
                etl_txt_size = _get_true_file_size(etl_txt_expected)
                if etl_txt_size != last_etl_txt_size:
                    last_etl_txt_size = etl_txt_size
                    etl_txt_idle_start = time.monotonic()
                    print(f"\r⏳ File .etl.txt writing... size={etl_txt_size} bytes\033[K", end='', flush=True)
                else:
                    if etl_txt_idle_start is None:
                        etl_txt_idle_start = time.monotonic()
                    if time.monotonic() - etl_txt_idle_start >= etl_txt_timeout:
                        print(f"\n⚠️ File .etl.txt idle for {etl_txt_timeout}s, .hci.txt never appeared. Killing process.")
                        proc.kill()
                        proc.wait()
                        _recover_rename_etl_files()
                        return None
                    print(f"\r⏳ File .etl.txt idle, waiting for .hci.txt...\033[K", end='', flush=True)
            else:
                # Neither file exists yet
                last_etl_txt_size = -1
                etl_txt_idle_start = None
                if no_file_start is None:
                    no_file_start = time.monotonic()
                if time.monotonic() - no_file_start >= etl_txt_timeout:
                    print(f"\n⚠️ Neither .etl.txt nor .hci.txt appeared after {etl_txt_timeout}s. Killing process.")
                    proc.kill()
                    proc.wait()
                    _recover_rename_etl_files()
                    return None

            time.sleep(2)

    except Exception as e:
        print(f"❌ Failed to run decode CLI: {e}")
        if proc and proc.poll() is None:
            try:
                proc.kill()
            except Exception:
                pass
        _recover_rename_etl_files()
        return None

    # 6) Locate the .hci.txt output and verify it is stable.
    for p in candidate_hci_paths(chatbot_input_etl):
        if os.path.exists(p) and is_file_ready(p):
            print(f"✅ HCI log ready: {p}")
            _recover_rename_etl_files()
            return p

    hci_txt = candidate_hci_paths(chatbot_input_etl)[0]
    print(f"❌ HCI log not found after decode: {hci_txt}")
    _recover_rename_etl_files()
    return None


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
    etl_txt_timeout: int = 180,
    hci_txt_timeout: int = 15,
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
        etl_txt_timeout: Max seconds to wait for the .etl.txt file.
        hci_txt_timeout: Max seconds to wait for the .hci.txt file.
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
    print(f"⏳ Waiting for HCI log until found: {hci_txt}")

    etl_txt = log_path + ".txt"
    txt_cfa = log_path + ".txt.cfa"
    txt_pcap = log_path + ".txt.pcap"

    time.sleep(0.5)
    # Although this may only occur in ManualSelect via IbtSnoopgen.
    close_warning_dialog()  # Dismiss benign "Systeminfo.txt not present" warning if it appears

    # Poll until the ENTIRE folder finishes decoding.
    # Strategy: folder-wide activity is the ONLY exit condition.
    # As long as ANY file in the folder is being written, the tool is still
    # working — keep waiting. Exit ONLY when the folder has been completely
    # idle for `etl_txt_timeout` seconds (180s default). At that point, check
    # whether the target .hci.txt exists and open it.
    last_folder_snapshot = None  # {path: size} of all decode outputs in the folder
    folder_idle_start = None    # timer: folder-wide idle detection

    def _take_folder_snapshot() -> dict:
        """Snapshot sizes of all decode-related files in the folder."""
        snap = {}
        try:
            for f in os.listdir(log_folder_path):
                fl = f.lower()
                if fl.endswith((".txt", ".hci.txt", ".txt.cfa", ".txt.pcap")):
                    fp = os.path.join(log_folder_path, f)
                    snap[fp] = _get_true_file_size(fp)
        except Exception:
            pass
        return snap

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

        # --- Folder-wide activity monitoring (the ONLY timeout/exit) ---
        current_snapshot = _take_folder_snapshot()

        if current_snapshot != last_folder_snapshot:
            # Something changed in the folder — tool is still working; reset idle timer.
            last_folder_snapshot = current_snapshot
            folder_idle_start = time.monotonic()
            total_files = len(current_snapshot)
            total_bytes = sum(v for v in current_snapshot.values() if v > 0)
            print(f"\r⏳ Folder active: {total_files} output file(s), "
                  f"total {total_bytes / (1024*1024):.1f} MB\033[K",
                  end='', flush=True)
        else:
            # Folder unchanged — nothing being written.
            if folder_idle_start is None:
                folder_idle_start = time.monotonic()

            folder_idle_seconds = time.monotonic() - folder_idle_start
            if folder_idle_seconds >= etl_txt_timeout:
                # Folder completely idle for 180s — all decoding finished (or stuck).
                print(f"\n✅ Folder idle for {etl_txt_timeout}s — decode phase complete.")
                if os.path.exists(hci_txt) and is_file_ready(hci_txt):
                    time.sleep(3)  # brief pause to ensure files are fully flushed
                    if open_with_text_analysis_tool(hci_txt, filter_path=filter_path):
                        print("✅ Opened HCI log with TextAnalysisTool.NET.")
                    else:
                        print("⚠️ Failed to open HCI log with TextAnalysisTool.NET.")
                    _terminate_bt_tool("✅ BT tool closed after folder decode complete.")
                else:
                    print(f"⚠️ Target .hci.txt not found or not ready: {hci_txt}")
                    _terminate_bt_tool("⚠️ BT tool terminated — folder idle, target not produced.")
                return None
            print(f"\r⏳ Folder idle {folder_idle_seconds:.0f}s / {etl_txt_timeout}s\033[K",
                  end='', flush=True)

        time.sleep(1)