from flask import Blueprint, render_template, request, session, redirect, url_for, flash, jsonify
import os
import subprocess
from datetime import datetime

from utils import helpers
from utils.etl_utils import get_auto_analysis_etl, get_issue_time_from_selected_files, filter_folders_by_time, extract_timestamp_from_folder
from utils.fw_utils import load_fw_system_info
from services.case_info_service import CaseService
from models.models import CaseContext
from configs.global_configs import app_config


main_bp = Blueprint("main", __name__, url_prefix="/")

#------------ALL ROUTE-------------#

@main_bp.route('/', methods=['GET', 'POST'])
def index():
    if request.method == 'POST':
        return handle_case_submission()
    return render_case_form()

@main_bp.route('/select_attachments', methods=['GET', 'POST'])
def select_attachments():
    if request.method == 'POST':
        return handle_select_attachments_submission()
    return render_select_attachments_form()

@main_bp.route('/download_attachments')
def download_attachments():
    return render_download_attachments_form()

@main_bp.route('/download_result')
def download_result():
    return render_download_result_form()

@main_bp.route('/download_result_bsod')
def download_result_bsod():
    return render_download_result_bsod_form()

@main_bp.route('/open_path', methods=['POST'])
def open_path():
    return handle_open_path()

@main_bp.route('/dump_event_txt', methods=['POST'])
def dump_event_txt():
    return handle_dump_event_txt()

@main_bp.route('/parse_event_log', methods=['POST'])
def parse_event_log():
    return handle_parse_event_log()

@main_bp.route('/api/bt_event_map', methods=['GET'])
def get_bt_event_map():
    return handle_get_bt_event_map()


#------------ INDEX render/submission -------------#

def render_case_form():
    clipboard_text = helpers.get_clipboard_case_number()
    
    return render_template('index.html', 
                         clipboard_text=clipboard_text)

def handle_case_submission():
    """Submit IPS number"""
    case_nbr = request.form.get('case_number', '').strip().replace(" ", "")

    if not case_nbr:
        flash("❌ No case number provided.", "danger")
        return redirect(url_for('main.index'))
    
    case_context = CaseContext(case_nbr=case_nbr)
    try:
        case_context = CaseService.process_case(case_context=case_context)
        if case_context.error_message:
            flash("Invalid case number or unable to retrieve data. Please try again.", "danger")
            case_context.error_message = None
            return redirect(url_for('main.index'))
        
        session.clear()

        session["case_context"] = case_context.to_session()
        session['prompt_file_path'] = CaseService.load_case_summary_prompt(case_context.wifi_or_bt)

        session['bsod'] = False
        session['latest_etl_llm'] = False
        session['debug_mode'] = False

        return redirect(url_for('main.select_attachments'))

    except Exception as e:
        print(f"❌ Error processing case: {e}")
        flash("An error occurred while processing the case.", "danger")
        return redirect(url_for('main.index'))



#------------ SELECT ATTACHMENT render/submission -------------#

def render_select_attachments_form():
    case_context = session.get("case_context")
    if not case_context:
        flash("Session expired. Please start again.", "warning")
        return redirect(url_for('main.index'))
    return render_template('select_attachments.html',
                           ai_analysis=None,     
                           case_context=case_context)

def handle_select_attachments_submission():
    selected_names = request.form.getlist('selected_files')
    case_context = session.get("case_context")
    if not case_context:
        flash("Session expired. Please start again.", "warning")
        return redirect(url_for('main.index'))
    case_context = CaseContext.from_session(case_context)
    
    selected_files = [item for item in case_context.attachment_list if item[0] in selected_names]
    session['selected_files'] = selected_files

    action = request.form.get('action')
    session['bsod'] = action == 'bsod'
    session['latest_etl_llm'] = action == 'latest_etl_llm'

    return redirect(url_for('main.download_attachments'))

#------------ DOWNLOAD ATTACHMENT render -------------#

def render_download_attachments_form():
    # If bsod: change download directory from local to shared folder
    case_context = session.get("case_context")
    if not case_context:
        flash("Session expired. Please start again.", "warning")
        return redirect(url_for('main.index'))
    case_context = CaseContext.from_session(case_context)
    download_path = case_context.case_download_dir

    if session.get('bsod') == True:
        
        from configs.path_configs import LOAD_PATH_prim, LOAD_PATH_bkup
        LOAD_PATH_bsod = helpers.get_load_path(LOAD_PATH_prim, LOAD_PATH_bkup)
        case_folder = case_context.backend_id if "-" in str(case_context.backend_id) else case_context.case_nbr
        download_path = rf"{LOAD_PATH_bsod}\{case_context.wifi_or_bt.upper()}\{case_folder}"
        
    files_to_download = {name: 0 for name, _, _ in session.get('selected_files', [])}
    session["download_path"] = download_path

    return render_template('attachment_download_progress.html', 
                           files_to_download=files_to_download, 
                           download_path=download_path)


#------------ DOWNLOAD RESULT render -------------#

def _get_latest_fw_system_info(fw_dict):
    fw_paths = [path for paths in (fw_dict or {}).values() for path in paths if path]
    if not fw_paths:
        return None, None

    def sort_key(path):
        timestamp = extract_timestamp_from_folder(path)
        return (timestamp or datetime.min, path)

    latest_fw_path = max(fw_paths, key=sort_key)
    return latest_fw_path, load_fw_system_info(latest_fw_path)


def _extract_first_folder_from_zip(file_path, zip_name, download_path):
    """Extract the first folder inside the zip extraction directory.
    
    Given a file path like: /downloads/test/20250101/subfolder/file.txt
    And zip_name: test.zip
    Returns: 20250101 (first folder under the extraction directory)
    """
    if not file_path or not zip_name or not download_path:
        return ''
    
    # Reconstruct the extraction folder path
    extract_folder_name = os.path.splitext(zip_name)[0].replace(" ", "_")
    extract_folder_path = os.path.join(download_path, extract_folder_name)
    
    # Normalize paths for comparison
    file_path_norm = os.path.normpath(str(file_path))
    extract_folder_norm = os.path.normpath(extract_folder_path)
    
    # Ensure proper path comparison (not just string prefix)
    try:
        rel_path = os.path.relpath(file_path_norm, extract_folder_norm)
        # If relative path starts with '..', file is not under extraction folder
        if rel_path.startswith('..'):
            return ''
    except ValueError:
        # Paths are on different drives (Windows)
        return ''
    
    # Extract the first folder component
    if rel_path in ('', '.'):
        return ''

    parts = rel_path.split(os.sep)
    first_folder = parts[0] if parts else ''
    if not first_folder:
        return ''

    # Fallback: if the first component is not a directory, use file's parent folder name.
    first_folder_path = os.path.join(extract_folder_norm, first_folder)
    if os.path.isdir(first_folder_path):
        return first_folder

    return os.path.basename(os.path.dirname(file_path_norm))


def _build_merged_table_rows(file_dict, path_key, path_filter=None, download_path=None):
    rows = []

    for zip_name, path_list in (file_dict or {}).items():
        items = []
        for item_path in (path_list or []):
            if path_filter and not path_filter(item_path):
                continue
            
            # Extract the first folder from zip
            folder_name = _extract_first_folder_from_zip(item_path, zip_name, download_path) if download_path else ''
            
            items.append({
                'zip_name': zip_name,
                'folder_name': folder_name,
                path_key: item_path,
            })

        if not items:
            continue

        zip_rowspan = len(items)

        folder_counts = {}
        for item in items:
            folder = item['folder_name']
            folder_counts[folder] = folder_counts.get(folder, 0) + 1

        folder_seen = {}
        for idx, item in enumerate(items):
            folder = item['folder_name']
            folder_seen[folder] = folder_seen.get(folder, 0) + 1

            row = {
                'zip_name': item['zip_name'],
                'folder_name': folder,
                'zip_rowspan': zip_rowspan,
                'folder_rowspan': folder_counts[folder],
                'show_zip_cell': idx == 0,
                'show_folder_cell': folder_seen[folder] == 1,
            }
            row[path_key] = item[path_key]
            rows.append(row)

    return rows


def _build_fw_table_rows(fw_dict, download_path=None):
    return _build_merged_table_rows(fw_dict, 'fw_path', download_path=download_path)


def _build_wifi_table_rows(wifi_dict, download_path=None):
    return _build_merged_table_rows(
        wifi_dict,
        'etl_path',
        path_filter=lambda p: not str(p).lower().endswith('.log'),
        download_path=download_path
    )


def _build_bt_table_rows(bt_dict, download_path=None):
    return _build_merged_table_rows(bt_dict, 'bt_path', download_path=download_path)


def _build_event_table_rows(ddd_dict, download_path=None):
    return _build_merged_table_rows(
        ddd_dict,
        'ddd_path',
        path_filter=lambda p: 'raweventviewersystemlogs.evt' in str(p).lower(),
        download_path=download_path
    )


def render_download_result_form():
    
    case_context = session.get("case_context")
    download_path = session.get("download_path", "")
    if not case_context or not download_path:
        flash("Session expired. Please start again.", "warning")
        return redirect(url_for('main.index'))
    case_context = CaseContext.from_session(case_context)

    result_data = app_config.get_download_results(case_context.case_nbr)

    if session.get('debug_mode'):
        file_dicts = {
            'wifi_dict': result_data.get('wifi', {}),
            'ddd_dict': result_data.get('ddd', {}),
            'bt_dict': result_data.get('bt', {}),
            'fw_dict': result_data.get('fw', {})
        }
    elif case_context.wifi_or_bt == 'wifi':
        file_dicts = {
            'wifi_dict': result_data.get('wifi', {}),
            'ddd_dict': result_data.get('ddd', {}),
            'bt_dict': {},
            'fw_dict': result_data.get('fw', {})
        }
    else:
        file_dicts = {
            'wifi_dict': {},
            'ddd_dict': result_data.get('ddd', {}),
            'bt_dict': result_data.get('bt', {}),
            'fw_dict': result_data.get('fw', {})
        }
    
    # Extract issue time from selected files
    selected_files = session.get("selected_files", [])
    time_mapping = get_issue_time_from_selected_files(selected_files)
    
    # Apply time-based filtering for each file if issue time is found
    time_filter_info = []  # Store info for display: [(file_name, time_display), ...]
    time_filter_warnings = []  # Store warnings: [(file_name, warning_message), ...]
    
    if time_mapping:
        from datetime import datetime as dt
        print(f"Applying time-based filtering for {len(time_mapping)} file(s)")
        try:
            # Filter each dict type with corresponding time for each file
            for dict_name in ['wifi_dict', 'ddd_dict', 'bt_dict', 'fw_dict']:
                file_dict = file_dicts[dict_name]
                
                filtered_dict = {}
                for zip_name, paths in file_dict.items():
                    if zip_name in time_mapping:
                        # Apply time filter for this specific file
                        issue_time = time_mapping[zip_name]
                        temp_dict = {zip_name: paths}
                        filtered_temp, warnings = filter_folders_by_time(temp_dict, issue_time)
                        filtered_dict.update(filtered_temp)
                        
                        # Collect warnings
                        for warn_file, warn_msg in warnings.items():
                            if not any(item[0] == warn_file for item in time_filter_warnings):
                                time_filter_warnings.append((warn_file, warn_msg))
                        
                        # Prepare display info - only add to success list if no warnings
                        if zip_name not in warnings:
                            time_display = issue_time.strftime('%Y-%m-%d %H:%M:%S') if isinstance(issue_time, dt) else issue_time
                            if not any(item[0] == zip_name for item in time_filter_info):
                                time_filter_info.append((zip_name, time_display))
                    else:
                        # No time filter for this file, keep as is
                        filtered_dict[zip_name] = paths
                
                # Update the file_dicts dynamically
                file_dicts[dict_name] = filtered_dict
            
            print(f"Time filtering completed successfully")
        except Exception as e:
            print(f"Error during time filtering: {e}")
            import traceback
            traceback.print_exc()
            time_filter_info = []
            time_filter_warnings = []
    else:
        print(f"No issue time found in selected files, skipping time filter")
    
    auto_analysis_etl = get_auto_analysis_etl(file_dicts['wifi_dict'], file_dicts['ddd_dict'])
    latest_fw_system_info_path, latest_fw_system_info = _get_latest_fw_system_info(file_dicts['fw_dict'])
    wifi_table_rows = _build_wifi_table_rows(file_dicts['wifi_dict'], download_path=download_path)
    bt_table_rows = _build_bt_table_rows(file_dicts['bt_dict'], download_path=download_path)
    event_table_rows = _build_event_table_rows(file_dicts['ddd_dict'], download_path=download_path)
    fw_table_rows = _build_fw_table_rows(file_dicts['fw_dict'], download_path=download_path)
    
    return render_template('download_result.html',
                         case_path=download_path,
                         wifi_or_bt=case_context.wifi_or_bt,
                         auto_analysis_etl = auto_analysis_etl,
                         exclude_keywords=app_config.etl_exclude_keywords,
                         latest_fw_system_info=latest_fw_system_info,
                         latest_fw_system_info_path=latest_fw_system_info_path,
                         wifi_table_rows=wifi_table_rows,
                         bt_table_rows=bt_table_rows,
                         event_table_rows=event_table_rows,
                         fw_table_rows=fw_table_rows,
                         time_filter_info=time_filter_info,
                         time_filter_warnings=time_filter_warnings,
                         **file_dicts)


#------------ [BSOD] DOWNLOAD RESULT render -------------#

def render_download_result_bsod_form():

    case_context = session.get("case_context")
    download_path = session.get("download_path", "")
    if not case_context or not download_path:
        flash("Session expired. Please start again.", "warning")
        return redirect(url_for('main.index'))
    case_context = CaseContext.from_session(case_context)

    case_path = download_path
    email = helpers.detect_user_email()

    return render_template('bsod.html', 
                           case_nbr=case_context.case_nbr, 
                           email=email, 
                           case_path=case_path)


#------------ Utility Handlers -------------#

def handle_open_path():
    """Open a local folder path in Windows Explorer."""
    path = request.json.get('path')
    print("Now opening path:", path)
    if path and os.path.exists(path):
        subprocess.run(['explorer', path])
        return '', 204
    return 'Invalid path', 400

def handle_dump_event_txt():
    """Decode .evt files to TXT using pywin32 for ultra-fast native access."""
    import xml.etree.ElementTree as ET
    import win32evtlog
    
    path = request.json.get('path')
    
    print(f"\n[Background Task] 🚀 Starting Event Log decoding using pywin32...")
    print(f"Source file: {path}")
    
    if not path or not os.path.exists(path):
        return jsonify({'error': 'Invalid path'}), 400
    
    txt_path = f"{path}.txt"
    try:
        print(f"Writing content to: {txt_path} (Lightning fast...)")
        
        # Use native API to open Event Log
        query_handle = win32evtlog.EvtQuery(path, win32evtlog.EvtQueryFilePath | win32evtlog.EvtQueryForwardDirection, None)
        
        with open(txt_path, 'w', encoding='utf-8') as f:
            count = 0
            while True:
                # Read 100 events at a time to avoid excessive memory usage
                events = win32evtlog.EvtNext(query_handle, 100)
                if not events:
                    break
                    
                for event in events:
                    count += 1
                    try:
                        # Convert to XML and parse key fields (faster than looking up DLL strings)
                        xml_content = win32evtlog.EvtRender(event, win32evtlog.EvtRenderEventXml)
                        root = ET.fromstring(xml_content)
                        ns = {'e': 'http://schemas.microsoft.com/win/2004/08/events/event'}
                        
                        system = root.find('e:System', ns)
                        if system is not None:
                            time_created = system.find('e:TimeCreated', ns)
                            time_str = time_created.get('SystemTime', '')[:19].replace('T', ' ') if time_created is not None else 'Unknown'
                            
                            provider = system.find('e:Provider', ns)
                            source_str = provider.get('Name', '') if provider is not None else 'Unknown'
                            
                            level = system.find('e:Level', ns)
                            level_map = {'1': 'Critical', '2': 'Error', '3': 'Warning', '4': 'Information', '5': 'Verbose'}
                            level_str = level_map.get(level.text if level is not None else '', 'Unknown')
                            
                            event_id = system.find('e:EventID', ns)
                            id_str = event_id.text if event_id is not None else 'Unknown'
                            
                            event_data = root.find('e:EventData', ns)
                            message = ''
                            if event_data is not None:
                                data_items = event_data.findall('e:Data', ns)
                                message = ' | '.join([d.text or '' for d in data_items if d.text])
                            
                            # Write each event as a single line: header + TAB + Message Data
                            f.write(f"[{time_str}] [{level_str}] [{source_str}] Event ID: {id_str}\tMessage Data: {message}\n")
                            
                    except Exception as parse_e:
                        f.write(f"[Error parsing event]: {parse_e}\n")
                        
        print(f"[Background Task] ✅ Decoding complete! Processed {count} events. TXT file successfully generated.\n")
        return jsonify({'success': True, 'txt_path': txt_path})
        
    except Exception as e:
        import traceback
        traceback.print_exc()
        print(f"[Background Task] ❌ Error occurred: {e}")
        return jsonify({'error': str(e)}), 500

def handle_parse_event_log():
    """Parse .evt file: keep ALL errors/warnings, ALL special sources (ibtusb/ibtpci/bthmini/bthusb/netwaw/netwtw), and up to 1000 normal events."""
    import xml.etree.ElementTree as ET
    import win32evtlog
    
    path = request.json.get('path')
    print(f"\n[UI View] 🔍 Scanning Event Log: {path}")
    
    if not path or not os.path.exists(path):
        return jsonify({'error': 'Invalid path'}), 400
    
    try:
        # Read from newest to oldest (Reverse Direction) for newest-first display
        query_handle = win32evtlog.EvtQuery(
            path, 
            win32evtlog.EvtQueryFilePath | win32evtlog.EvtQueryReverseDirection, 
            None
        )
        
        events_list = []
        # All special sources to always keep regardless of level
        special_sources = ['ibtusb', 'ibtpci', 'bthmini', 'bthusb', 'netwaw', 'netwtw']
        
        normal_kept = 0
        total_scanned = 0
        
        while True:
            # Read 100 events at a time
            events = win32evtlog.EvtNext(query_handle, 100)
            if not events:
                break  # End of file reached
                
            for event in events:
                total_scanned += 1
                try:
                    xml_content = win32evtlog.EvtRender(event, win32evtlog.EvtRenderEventXml)
                    root = ET.fromstring(xml_content)
                    ns = {'e': 'http://schemas.microsoft.com/win/2004/08/events/event'}
                    
                    system = root.find('e:System', ns)
                    if system is None:
                        continue
                        
                    level_elem = system.find('e:Level', ns)
                    level_val = level_elem.text if level_elem is not None else ''
                    
                    provider = system.find('e:Provider', ns)
                    source = provider.get('Name', '') if provider is not None else ''
                    
                    # 💡 FILTERING LOGIC 💡
                    # Condition 1: Important levels (1=Critical, 2=Error, 3=Warning)
                    # Condition 2: Special sources (ibtusb, ibtpci, bthmini, bthusb, netwaw, netwtw)
                    is_important_level = level_val in ['1', '2', '3']
                    is_special_source = any(kw in source.lower() for kw in special_sources)
                    
                    # If it is neither an important level nor a special source (i.e., normal Information)
                    if not (is_important_level or is_special_source):
                        # Keep only the latest 1000 normal logs
                        if normal_kept >= 1000:
                            continue  # Skip if we already have 1000 normal logs
                        normal_kept += 1
                        
                    # Parse the data and add it to the list
                    time_created = system.find('e:TimeCreated', ns)
                    time_str = time_created.get('SystemTime', '')[:19].replace('T', ' ') if time_created is not None else 'Unknown'
                    
                    event_id = system.find('e:EventID', ns)
                    id_str = event_id.text if event_id is not None else 'Unknown'
                    
                    level_map = {'1': 'Critical', '2': 'Error', '3': 'Warning', '4': 'Information', '5': 'Verbose'}
                    level_text = level_map.get(level_val, 'Unknown')
                    
                    event_data = root.find('e:EventData', ns)
                    message = ''
                    if event_data is not None:
                        data_items = event_data.findall('e:Data', ns)
                        message = ' | '.join([d.text or '' for d in data_items if d.text])
                    
                    events_list.append({
                        'time': time_str,
                        'level': level_text,
                        'source': source,
                        'event_id': id_str,
                        'message': message[:500]
                    })
                        
                except Exception:
                    continue
                    
        print(f"[UI View] ✅ Scan complete! Scanned {total_scanned} events. Kept {len(events_list)} events (including {normal_kept} normal logs).")
        
        return jsonify({'events': events_list})
        
    except Exception as e:
        print(f"Error parsing event log: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500

def handle_get_bt_event_map():
    """Read the local JSON file and provide the BT Event ID map to the frontend."""
    import json
    
    # Locate the JSON file in the configs directory
    base_dir = os.path.abspath(os.path.dirname(__file__))
    json_path = os.path.join(base_dir, '..', '..', 'configs', 'bt_event_id_map.json')
    
    try:
        if os.path.exists(json_path):
            with open(json_path, 'r', encoding='utf-8') as f:
                event_map = json.load(f)
            return jsonify(event_map)
        else:
            print(f"[Warning] BT Event map JSON not found at: {json_path}")
            return jsonify({})
    except Exception as e:
        print(f"[Error] Failed to read BT Event map JSON: {e}")
        return jsonify({})