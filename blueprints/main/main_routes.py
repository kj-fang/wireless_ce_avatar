from flask import Blueprint, render_template, request, session, redirect, url_for, flash, jsonify
import os
import subprocess

from utils import helpers
from utils.etl_utils import get_auto_analysis_etl, get_issue_time_from_selected_files, filter_folders_by_time
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


#------------INDEX render/submission -------------#

def render_case_form():
    clipboard_text = helpers.get_clipboard_case_number()
    
    return render_template('index.html', 
                         clipboard_text=clipboard_text)

def handle_case_submission():
    """submit IPS number"""
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
        return redirect(url_for('main.select_attachments'))
            
    except Exception as e:
        print(f"❌ Error processing case: {e}")
        flash("An error occurred while processing the case.", "danger")
        return redirect(url_for('main.index'))
    

#------------SELLECT ATTACHMENT render/submission -------------#

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

#------------DOWNLOAD ATTACHMENT render -------------#

def render_download_attachments_form():
    # if bsod: change download directory from local to shared folder 
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


#------------DOWNLOAD RESULT render -------------#

def render_download_result_form():
    
    case_context = session.get("case_context")
    download_path = session.get("download_path", "")
    if not case_context or not download_path:
        flash("Session expired. Please start again.", "warning")
        return redirect(url_for('main.index'))
    case_context = CaseContext.from_session(case_context)

    result_data = app_config.get_download_results(case_context.case_nbr)

    if case_context.wifi_or_bt == 'wifi':
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
    
    return render_template('download_result.html',
                         case_path=download_path,
                         auto_analysis_etl = auto_analysis_etl,
                         exclude_keywords=app_config.etl_exclude_keywords,
                         time_filter_info=time_filter_info,
                         time_filter_warnings=time_filter_warnings,
                         **file_dicts)


#------------[BSOD] DOWNLOAD RESULT render -------------#

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






#------------ Other Utils -------------#

@main_bp.route('/open_path', methods=['POST']) 
def open_path():
    path = request.json.get('path')
    print("now open path:", path)
    if path and os.path.exists(path):
        subprocess.run(['explorer', path])
        return '', 204
    return 'Invalid path', 400

@main_bp.route('/open_event_viewer', methods=['POST'])
def open_event_viewer():
    """Open .evt file with Windows Event Viewer"""
    path = request.json.get('path')
    print("Opening Event Viewer for:", path)
    if path and os.path.exists(path):
        # Use os.startfile to open .evt file with default application (Event Viewer)
        os.startfile(path)
        return '', 204
    return 'Invalid path', 400

@main_bp.route('/parse_event_log', methods=['POST'])
def parse_event_log():
    """Parse .evt file and return events as JSON"""
    import xml.etree.ElementTree as ET
    import tempfile
    
    path = request.json.get('path')
    print("Parsing Event Log:", path)
    
    if not path or not os.path.exists(path):
        return jsonify({'error': 'Invalid path'}), 400
    
    try:
        # Use wevtutil to export .evt to XML
        with tempfile.NamedTemporaryFile(mode='w', suffix='.xml', delete=False) as tmp:
            tmp_path = tmp.name
        
        # Export event log to XML format
        result = subprocess.run(
            ['wevtutil', 'qe', path, '/lf:true', '/f:xml'],
            capture_output=True,
            text=True,
            encoding='utf-8',
            errors='replace'
        )
        
        if result.returncode != 0:
            return jsonify({'error': f'wevtutil failed: {result.stderr}'}), 500
        
        xml_content = result.stdout
        
        # Parse XML and extract events
        events = []
        
        # Wrap in root element for valid XML
        xml_content = f'<Events>{xml_content}</Events>'
        
        try:
            root = ET.fromstring(xml_content)
            
            ns = {'e': 'http://schemas.microsoft.com/win/2004/08/events/event'}
            
            for event in root.findall('.//e:Event', ns):
                system = event.find('e:System', ns)
                event_data = event.find('e:EventData', ns)
                
                if system is not None:
                    time_created = system.find('e:TimeCreated', ns)
                    provider = system.find('e:Provider', ns)
                    event_id = system.find('e:EventID', ns)
                    level = system.find('e:Level', ns)
                    
                    # Map level number to text
                    level_map = {'1': 'Critical', '2': 'Error', '3': 'Warning', '4': 'Information', '5': 'Verbose'}
                    level_text = level_map.get(level.text if level is not None else '', 'Unknown')
                    
                    # Get message from EventData
                    message = ''
                    if event_data is not None:
                        data_items = event_data.findall('e:Data', ns)
                        message = ' | '.join([d.text or '' for d in data_items if d.text])
                    
                    events.append({
                        'time': time_created.get('SystemTime', '')[:19].replace('T', ' ') if time_created is not None else '',
                        'level': level_text,
                        'source': provider.get('Name', '') if provider is not None else '',
                        'event_id': event_id.text if event_id is not None else '',
                        'message': message[:500]  # Limit message length
                    })
        except ET.ParseError as e:
            print(f"XML parse error: {e}")
            return jsonify({'error': f'Failed to parse event log XML: {str(e)}'}), 500
        
        # Return latest events first
        events = events[:500]  # Limit to 500 events
        
        return jsonify({'events': events})
        
    except Exception as e:
        print(f"Error parsing event log: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500