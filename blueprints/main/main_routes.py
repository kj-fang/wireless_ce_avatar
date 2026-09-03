from flask import Blueprint, render_template, request, session, redirect, url_for, flash, jsonify
import os
import glob
import subprocess
import time
from datetime import datetime

from utils import helpers
from utils.etl_utils import get_auto_analysis_etl, get_issue_time_from_selected_files, filter_folders_by_time, extract_timestamp_from_folder, pick_latest_zip_attachment
from utils.fw_utils import load_fw_system_info, infer_fw_parse_type
from services.case_info_service import CaseService
from models.models import CaseContext
from services.llm_service import LLM_helper
from services import gather_service
from configs.global_configs import app_config


main_bp = Blueprint("main", __name__, url_prefix="/")


def _start_gather_workflow(case_context: CaseContext, selected_files=None) -> str:
    """Create the case-scoped v5 ID before any pre-chat AI can run."""
    workflow_id = gather_service.new_workflow_id()
    session["gather_workflow_id"] = workflow_id
    issue = case_context.to_dict()
    domain = case_context.wifi_or_bt or "wifi"
    gather_service.record_workflow_start(
        workflow_id=workflow_id,
        issue=issue,
        domain=domain,
        attachment_list=case_context.attachment_list or [],
    )
    if selected_files:
        gather_service.record_attachment_selection(
            workflow_id=workflow_id,
            selected_files=selected_files,
            issue=issue,
            domain=domain,
        )
    return workflow_id

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
        _start_gather_workflow(case_context)

        return redirect(url_for('main.select_attachments'))

    except Exception as e:
        # Print the full traceback so root-cause "NoneType is not iterable"
        # style failures don't disappear behind a one-line summary.
        import traceback
        print(f"❌ Error processing case: {e}")
        traceback.print_exc()
        flash("An error occurred while processing the case.", "danger")
        return redirect(url_for('main.index'))

#------------ETL+LLM Route-------------#
@main_bp.route('/start_latest_etl_llm', methods=['POST'])
def start_latest_etl_llm():
    case_nbr = request.form.get('case_number', '').strip().replace(" ", "")
    if not case_nbr:
        return jsonify({'success': False, 'message': 'No case number provided.'}), 400

    case_context = CaseContext(case_nbr=case_nbr)
    try:
        case_context = CaseService.process_case(case_context=case_context)
        if case_context.error_message:
            case_context.error_message = None
            return jsonify({'success': False, 'message': 'Invalid case number or unable to retrieve data.'}), 400

        selected_latest = pick_latest_zip_attachment(case_context.attachment_list)
        if not selected_latest:
            return jsonify({'success': False, 'message': 'No ZIP attachment found for this case.'}), 400

        session.clear()
        session["case_context"] = case_context.to_session()
        session['prompt_file_path'] = CaseService.load_case_summary_prompt(case_context.wifi_or_bt)
        session['selected_files'] = [selected_latest]
        session['bsod'] = False
        session['latest_etl_llm'] = True
        _start_gather_workflow(case_context, [selected_latest])

        session['classification'] = {
            "issue_type": "Unclassified",
            "confidence": 0,
            "keywords_found": []
        }

        llm_helper: LLM_helper = app_config.llm_helper
        prepass_usage = llm_helper.empty_usage() if llm_helper is not None else None
        if llm_helper is not None:
            try:
                # Pass the rehydrated dict (heavy fields included) so
                # the LLM classifier sees the full context — comments
                # can be a strong signal for category routing.
                _full_ctx = CaseContext.from_session(session.get("case_context") or {}).to_dict()
                classification = llm_helper.classify_issue(
                    _full_ctx, usage_accumulator=prepass_usage
                )
                
                if isinstance(classification, dict):
                     session['classification'] = classification

            except Exception as llm_error:
                print(f"⚠️ Classification failed in start_latest_etl_llm: {llm_error}")

        # Mirror the select_attachments path: run the same token-frugal LLM
        # pre-pass that organises Issue Description → clean_description +
        # issue_times[] into session['_issue_ai_quick']. Without this, the
        # index-page Run Analysis (which jumps straight to download_attachments
        # via this route) never populates the AI cache, so download_result
        # has no llm_issue_time — the "Auto-pick log by AI time" checkbox
        # doesn't render and pick_etl_by_ai_time has nothing to compare.
        # Best-effort: failures here must not block the auto-launch.
        try:
            _prime_issue_ai_cache(case_context, initial_usage=prepass_usage)
        except Exception as _e:
            print(f"⚠️ issue-AI pre-pass skipped in start_latest_etl_llm: {_e}")

        # Independent marker for download_result's auto-launch decision.
        # Upstream's session['latest_etl_llm'] is cleared by the first call to
        # get_auto_analysis_etl, so any redundant request to /download_result
        # (browser prefetch, hot-reload, websocket reconnect, etc.) would
        # silently lose the auto-fire. This marker survives until popped
        # explicitly in render_download_result_form.
        session['_run_analysis_requested'] = True

        return jsonify({
            'success': True,
            'redirect_url': url_for('main.download_attachments')
        })

    except Exception as e:
        print(f"❌ Error starting latest ETL+LLM flow: {e}")
        return jsonify({'success': False, 'message': 'An error occurred while processing the case.'}), 500
       

#------------ SELECT ATTACHMENT render/submission -------------#

def render_select_attachments_form():
    # Merged behaviour:
    #   * Session-expired guard from main — bails out gracefully if the
    #     cookie session got cleared between requests.
    #   * Sidecar rehydration from this branch — for heavyweight cases
    #     the cookie session only carries a pointer to the on-disk
    #     <case_download_dir>/.case_context_session.json; from_session()
    #     transparently loads comments / attachment_list back in.
    raw = session.get("case_context")
    if not raw:
        flash("Session expired. Please start again.", "warning")
        return redirect(url_for('main.index'))
    case_context = CaseContext.from_session(raw).to_dict()
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
    try:
        gather_service.record_attachment_selection(
            workflow_id=session.get("gather_workflow_id", ""),
            selected_files=selected_files,
            issue=case_context.to_dict(),
            domain=case_context.wifi_or_bt or "wifi",
        )
    except Exception:
        pass

    # Quick LLM pre-pass: organize the Issue Description into a clean problem
    # statement + issue time(s) now, so download_result can auto-match a log and
    # the chatbot can reuse it (one LLM call for the whole flow). Best-effort.
    try:
        _prime_issue_ai_cache(case_context)
    except Exception as _e:
        print(f"⚠️ issue-AI pre-pass skipped: {_e}")

    action = request.form.get('action')
    session['bsod'] = action == 'bsod'
    # Accept both the legacy "latest_etl_llm" name and the new "analysis"
    # name that the redesigned select_attachments template emits — the
    # template's Run Analysis button was renamed but the route was never
    # updated, which silently broke the auto-launch path (both this PR's
    # AI-time pick AND the upstream newest-by-number pick). Keeping both
    # values lets either template revision drive the auto flow.
    _run_analysis = action in ('latest_etl_llm', 'analysis')
    session['latest_etl_llm'] = _run_analysis
    # Independent marker for download_result's auto-launch — see the
    # corresponding comment in start_latest_etl_llm. Survives upstream's
    # flag-clearing so the AI-time pick + auto-fire still trigger reliably
    # even when /download_result is hit more than once.
    if _run_analysis:
        session['_run_analysis_requested'] = True

    return redirect(url_for('main.download_attachments'))


def _prime_issue_ai_cache(case_context, initial_usage=None):
    """Run the (token-frugal) LLM organize on the case Issue Description and
    stash {clean_description, issue_times, interpretation} in the session under
    ``_issue_ai_quick``. No log exists yet, so times come back clock-only; they
    get dated against the actual log later (download_result / chatbot).

    Source-of-truth chain (first non-empty wins):
      1. ``case_context.description`` — the IPS Issue Description field. Usually
         populated when the case came from the standard Salesforce pull.
      2. Attachment subtitle text in ``case_context.attachment_list`` — the
         small gray "uploaded by Partner at ..." line shown beneath each
         attachment in the Choose Attachment picker. Many cases (especially
         partner-uploaded ones) leave the IPS Issue Description blank and
         instead put the symptom + time inline next to the attachment, e.g.
         ``"could not connect to AP at 04/14/2026 01:26:00"``. Without this
         fallback the AI cache stays empty for those cases and the
         ``🪄 Auto-pick log by AI time`` checkbox on /download_result never
         renders.
    """
    from utils.issue_time_ai import organize_issue_context
    started = time.perf_counter()
    helper = app_config.llm_helper
    client = getattr(helper, "client", None) if helper else None
    model = getattr(helper, "model", "gpt-4.1") if helper else None

    def _record_prepass_usage(usage):
        if int((usage or {}).get("llm_calls") or 0) <= 0:
            return
        try:
            gather_service.record_feature_usage(
                workflow_id=session.get("gather_workflow_id", ""),
                feature_code="issue_time_prepass",
                model=model or "",
                usage=usage,
                issue=case_context.to_dict(),
                domain=case_context.wifi_or_bt or "wifi",
                trigger="run_analysis_prepass",
                status="success",
                latency_ms=int((time.perf_counter() - started) * 1000),
            )
        except Exception:
            pass

    desc = (getattr(case_context, "description", "") or "").strip()
    print(f"[_prime_issue_ai_cache] entry — case_nbr={getattr(case_context, 'case_nbr', '?')!r}  "
          f"description empty? {not desc}")
    if not desc:
        # Fall back to attachment subtitle text. attachment_list entries look
        # like ``[name, link, [timestamp_meta, subtitle_text]]``; we collect
        # every non-empty subtitle and let the LLM/regex pass mine it for a
        # time + symptom statement.
        try:
            chunks = []
            for item in (getattr(case_context, "attachment_list", []) or []):
                if not item or len(item) < 3:
                    continue
                meta = item[2] if isinstance(item[2], (list, tuple)) else None
                if meta and len(meta) >= 2 and isinstance(meta[1], str):
                    s = meta[1].strip()
                    if s:
                        chunks.append(s)
            desc = "\n".join(chunks).strip()
            if desc:
                print(f"[_prime_issue_ai_cache] description empty — using "
                      f"{len(chunks)} attachment subtitle(s) as fallback")
        except Exception as _e:
            print(f"[_prime_issue_ai_cache] attachment fallback failed: {_e}")
    if not desc:
        print(f"[_prime_issue_ai_cache] STILL empty after attachment fallback — bailing")
        _record_prepass_usage(initial_usage or {})
        return
    print(f"[_prime_issue_ai_cache] description ({len(desc)} chars): {desc[:200]!r}")
    data, own_usage = organize_issue_context(
        desc, first_ts=None, last_ts=None,
        llm_client=client, llm_model=model, return_usage=True,
    )
    operation_usage = dict(initial_usage or LLM_helper.empty_usage())
    for key in LLM_helper.empty_usage():
        operation_usage[key] = int(operation_usage.get(key) or 0) + int(own_usage.get(key) or 0)
    print(f"[_prime_issue_ai_cache] organize_issue_context returned: "
          f"issue_times={data.get('issue_times')!r}")
    session["_issue_ai_quick"] = {"data": data}
    # Deterministic explicit-time extraction costs nothing and is intentionally
    # omitted.  Record only when at least one provider call actually happened.
    _record_prepass_usage(operation_usage)

def _resolve_download_path(case_context: CaseContext, is_bsod: bool) -> str:
    if not case_context:
        return ''

    if not is_bsod:
        return case_context.case_download_dir or ''

    from configs.path_configs import LOAD_PATH_prim, LOAD_PATH_bkup

    load_path_bsod = helpers.get_load_path(LOAD_PATH_prim, LOAD_PATH_bkup)
    if not load_path_bsod:
        return ''

    case_folder = case_context.backend_id if "-" in str(case_context.backend_id) else case_context.case_nbr
    return rf"{load_path_bsod}\{case_folder}"

#------------DOWNLOAD ATTACHMENT render -------------#

def render_download_attachments_form():
    # If bsod: change download directory from local to shared folder
    case_context = session.get("case_context")
    if not case_context:
        flash("Session expired. Please start again.", "warning")
        return redirect(url_for('main.index'))
    case_context = CaseContext.from_session(case_context)
    download_path = _resolve_download_path(case_context, session.get('bsod') == True)
        
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
        path_filter=lambda p: 'raweventviewersystemlogs.evt' in str(p).lower() or 'system.evtx' in str(p).lower(),
        download_path=download_path
    )


def render_download_result_form():
    case_context = session.get("case_context")
    if not case_context:
        flash("Session expired. Please start again.", "warning")
        return redirect(url_for('main.index'))
    case_context = CaseContext.from_session(case_context)
    download_path = _resolve_download_path(case_context, session.get('bsod') == True)
    if not download_path:
        flash("Session expired. Please start again.", "warning")
        return redirect(url_for('main.index'))

    result_data = app_config.get_download_results(case_context.case_nbr)

    # Tab display is driven purely by which dicts have items — the Salesforce
    # hint (case_context.wifi_or_bt) no longer gates visibility. This surfaces
    # BT+WiFi coex zips naturally (both tabs show up) and also unbreaks cases
    # where the Salesforce subcategory was misclassified relative to the
    # actual attached logs.
    file_dicts = {
        'wifi_dict': result_data.get('wifi', {}),
        'ddd_dict': result_data.get('ddd', {}),
        'bt_dict': result_data.get('bt', {}),
        'fw_dict': result_data.get('fw', {})
    }

    # Coex detection: both BT and WiFi ETL logs present in the same case.
    # Downstream reads case_context.files_coexist to enable the FW BT/WiFi selector,
    # keep both parse tabs interactive, and gate the auto-analysis fallback.
    has_wifi_items = any(bool(v) for v in file_dicts['wifi_dict'].values())
    has_bt_items = any(bool(v) for v in file_dicts['bt_dict'].values())
    case_context.files_coexist = has_wifi_items and has_bt_items
    session["case_context"] = case_context.to_session()
    
    # Extract issue time from selected files
    selected_files = session.get("selected_files", [])
    time_mapping = get_issue_time_from_selected_files(selected_files)
    
    # Apply time-based filtering for each file if issue time is found
    time_filter_info = []  # Store info for display: [(file_name, time_display), ...]
    time_filter_warnings = []  # Store warnings: [(file_name, warning_message), ...]
    
    if time_mapping:
        from datetime import datetime as dt
        from utils.issue_time_ai import align_issue_datetime_to_customer_frame
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
                        # The attachment subtitle is often in log frame (e.g.
                        # "04/14/2026 01:26:00" for a CST customer), but
                        # folder names are in the customer's local clock.
                        # filter_folders_by_time expects both sides in the
                        # same frame — align here so the +/- 5 min window
                        # actually catches the right folder instead of
                        # warning "All folders are before issue time".
                        if isinstance(issue_time, dt):
                            aligned = align_issue_datetime_to_customer_frame(issue_time, paths)
                            if aligned != issue_time:
                                print(f"🪄 issue_time aligned for {zip_name!r}: "
                                      f"{issue_time} → {aligned}")
                            issue_time = aligned
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
    
    # AI-organized issue time(s) from the select-attachments pre-pass, used by
    # the "auto-pick log by AI time" checkbox next to Chatbot Analysis.
    _quick_ai = (session.get("_issue_ai_quick") or {}).get("data") or {}
    llm_issue_times = _quick_ai.get("issue_times") or []
    llm_issue_time_raw = llm_issue_times[0] if llm_issue_times else ""

    # Fallback: the LLM pre-pass cache (``_issue_ai_quick``) can be empty —
    # e.g. the chatbot primed it first with a blank description, or the
    # select-attachments step was skipped. But ``time_mapping`` (parsed from
    # the attachment subtitle just above, the SAME source filter_folders_by_time
    # uses) often DOES carry a usable issue time. Reuse it so the auto-pick
    # checkbox renders and the AI pre-select runs even when the LLM cache is
    # cold. Prefer a full datetime; format it into the canonical string the
    # rest of this view expects.
    if not llm_issue_time_raw and time_mapping:
        from datetime import datetime as _dt
        for _zip, _it in time_mapping.items():
            if isinstance(_it, _dt):
                llm_issue_time_raw = _it.strftime("%m/%d/%Y-%H:%M:%S.%f")[:-3]
                break
            if isinstance(_it, str) and _it.strip():
                llm_issue_time_raw = _it.strip()
                break
        if llm_issue_time_raw:
            print(f"[download_result] _issue_ai_quick empty — using attachment "
                  f"time from time_mapping: {llm_issue_time_raw!r}")
            llm_issue_times = [llm_issue_time_raw]

    # Trace why the chip might be missing: prime-pass ran? extracted times?
    print(f"[download_result] _issue_ai_quick present: "
          f"{bool(session.get('_issue_ai_quick'))}  "
          f"issue_times: {llm_issue_times!r}  "
          f"raw: {llm_issue_time_raw!r}")

    # Rewrite the AI issue time into the customer wall-clock so the chip on
    # /download_result reads in the SAME frame as the folder names. The
    # description usually transcribes a log-frame value (e.g. ``04/14/2026
    # 01:26:00`` for a CST customer = ``04/13/2026 12:26:00`` locally), which
    # is confusing next to ``LUS-..._13-04-2026_12-26-53_...`` folders. The
    # alignment is deterministic-first (compare both interpretations to the
    # folder ts list, pick the smaller gap) and only consults the LLM when
    # the two gaps are within a minute of each other. Falls back to the raw
    # string when no tz can be detected.
    llm_issue_time = llm_issue_time_raw
    if llm_issue_time_raw:
        try:
            from utils.issue_time_ai import align_issue_time_for_display
            helper = app_config.llm_helper
            client = getattr(helper, "client", None) if helper else None
            model = getattr(helper, "model", "gpt-4.1") if helper else None
            llm_issue_time = align_issue_time_for_display(
                llm_issue_time_raw, file_dicts,
                llm_client=client, llm_model=model,
            )
            if llm_issue_time != llm_issue_time_raw:
                print(f"🪄 issue_time aligned: {llm_issue_time_raw} → {llm_issue_time}")
        except Exception as e:
            print(f"⚠️ align_issue_time_for_display failed: {e}")

    # Independent Run Analysis marker (see handle_select_attachments_submission /
    # start_latest_etl_llm). Pop once so subsequent reloads of /download_result
    # don't re-trigger an auto-launch — but it survives the upstream
    # latest_etl_llm flag-clearing inside get_auto_analysis_etl, which made the
    # auto-fire path fragile when /download_result was hit more than once.
    run_analysis_pending = bool(session.pop('_run_analysis_requested', False))

    # Upstream pick: newest-by-number (also clears session['latest_etl_llm']).
    # In coex mode with a BT hint, skip the wifi-flavored pick entirely — the
    # BT side has its own auto_analysis_bt pipeline below and picking a wifi
    # ETL here would launch the wrong parser.
    if case_context.files_coexist and case_context.wifi_or_bt == 'bt':
        auto_analysis_etl = None
        auto_analysis_etl_reason = None
    else:
        auto_analysis_etl = get_auto_analysis_etl(file_dicts['wifi_dict'], file_dicts['ddd_dict'])
        auto_analysis_etl_reason = 'latest_by_number' if auto_analysis_etl else None

    # Recovery: if Run Analysis was pending but upstream picked nothing
    # (most often because its latest_etl_llm flag was already cleared by an
    # earlier request to this view), find any .etl ourselves so the auto-
    # launch the user just asked for still happens.
    if not auto_analysis_etl and run_analysis_pending and not (
        case_context.files_coexist and case_context.wifi_or_bt == 'bt'
    ):
        try:
            import re as _re
            from utils.etl_utils import extract_file_number
            for _dn in ('ddd_dict', 'wifi_dict'):
                _cands = []
                for _paths in (file_dicts.get(_dn) or {}).values():
                    if not _paths:
                        continue
                    for _p in _paths:
                        _pl = str(_p)
                        if _pl.lower().endswith('.etl') or _re.search(r'\.etl\.\d+$', _pl, _re.IGNORECASE):
                            _cands.append(_pl)
                if _cands:
                    auto_analysis_etl = max(_cands, key=extract_file_number)
                    auto_analysis_etl_reason = 'latest_by_number'
                    print(f"🛟 Run Analysis recovery: upstream returned None, "
                          f"falling back to {auto_analysis_etl}")
                    break
        except Exception as _e:
            print(f"⚠️ Run Analysis recovery pick failed: {_e}")

    # Run Analysis path: when an AI-extracted issue time is available, prefer
    # the time-based pick (the .etl whose folder timestamp best matches the
    # issue time). More accurate than file-number sorting when the latest
    # collection isn't actually the one that captured the issue. Gated on
    # `auto_analysis_etl OR run_analysis_pending` so the override fires even
    # when upstream returned None (now backed by the recovery above). Purely
    # additive — falls back to the existing pick on any failure.
    if llm_issue_time and (auto_analysis_etl or run_analysis_pending):
        try:
            from utils.issue_time_ai import pick_etl_by_ai_time
            # Picker expects ``issue_time`` in the customer frame (folder
            # names are written in customer time). ``llm_issue_time`` has
            # been routed through ``align_issue_time_for_display`` above so
            # any log-frame transcription in the description has already
            # been shifted to customer.
            ai_pick = pick_etl_by_ai_time(file_dicts, llm_issue_time)
            if ai_pick:
                if not auto_analysis_etl or ai_pick != auto_analysis_etl:
                    print(f"🎯 Run Analysis: AI-time pick "
                          f"(issue_time={llm_issue_time})")
                    print(f"   was: {auto_analysis_etl}")
                    print(f"   now: {ai_pick}")
                    auto_analysis_etl = ai_pick
                    auto_analysis_etl_reason = 'ai_time'
                else:
                    # AI pick == newest-by-number → same file, tag the reason
                    # so the UI alert shows "AI-verified" rather than blind newest.
                    auto_analysis_etl_reason = 'ai_time'
        except Exception as e:
            print(f"⚠️ AI-time ETL pick failed, keeping newest-by-number: {e}")

    # --- BT auto-analysis: pick the best BT path when Run Analysis was
    # requested for a BT case. Reuses the same folder-timestamp logic as
    # bt_chatbot.find_best_log (newest capture-folder timestamp wins).
    auto_analysis_bt = None
    if run_analysis_pending and 'bt' in case_context.wifi_or_bt:
        try:
            from blueprints.bt_chatbot.bt_chatbot_routes import _parse_path_timestamp
            bt_paths = []
            for _paths in (file_dicts.get('bt_dict') or {}).values():
                bt_paths.extend(_paths or [])
            if bt_paths:
                # Pick the one with the latest folder timestamp
                best_bt = None
                best_bt_ts = None
                for bp in bt_paths:
                    ts = _parse_path_timestamp(bp)
                    if ts and (best_bt_ts is None or ts > best_bt_ts):
                        best_bt_ts = ts
                        best_bt = bp
                auto_analysis_bt = best_bt or bt_paths[0]
                print(f"🚀 BT Run Analysis: auto-selected {auto_analysis_bt}")
        except Exception as _e:
            print(f"⚠️ BT auto-analysis pick failed: {_e}")

    # Download path: even when the user clicked "Download" (no auto-run), we
    # still want the row PRE-SELECTED so the user just has to click "Wi-Fi
    # Analysis Agent" — no second decision. The frontend's
    # ``autoSelectLogByIssueTime`` walks the page itself but treats folder ts
    # and ``__llmIssueTime`` as the same frame (off by ~13 h for non-Asia
    # customers), so a manual checkbox click ends up picking the wrong row.
    # We compute the path server-side once (timezone-aware via
    # ``pick_etl_by_ai_time``) and hand it to the template; the JS only has
    # to look up the matching ``.wifi-file-item`` and click it.
    ai_pre_selected_etl = ""
    if llm_issue_time:
        try:
            from utils.issue_time_ai import pick_etl_by_ai_time
            ai_pre_selected_etl = pick_etl_by_ai_time(file_dicts, llm_issue_time) or ""
            if ai_pre_selected_etl:
                print(f"🪄 AI pre-select: {ai_pre_selected_etl} "
                      f"(issue_time={llm_issue_time})")
        except Exception as e:
            print(f"⚠️ AI pre-select pick failed: {e}")

    # Multi-ETL display: when the AI issue time is time-only (no date), anchor
    # the CHIP to the AI-picked ETL's folder capture date so it reads as a real
    # moment instead of a bare clock. Done AFTER the picks above so it only
    # affects display, not which ETL is selected (the pickers handle time-only
    # internally). Folder names + llm_issue_time are both customer frame here,
    # so this is a pure date fill. Picked-folder priority: the download
    # pre-select, else the Run-Analysis pick.
    _picked_folder = ai_pre_selected_etl or auto_analysis_etl
    if llm_issue_time and _picked_folder:
        try:
            from utils.issue_time_ai import anchor_time_only_to_folder_date
            _dated = anchor_time_only_to_folder_date(llm_issue_time, _picked_folder)
            if _dated != llm_issue_time:
                print(f"[download_result] chip date anchored to folder: "
                      f"{llm_issue_time} -> {_dated}")
                llm_issue_time = _dated
        except Exception as e:
            print(f"[download_result] chip date anchor failed: {e}")

    latest_fw_system_info_path, latest_fw_system_info = _get_latest_fw_system_info(file_dicts['fw_dict'])
    fw_type_hints = {
        fw_path: infer_fw_parse_type(fw_path, case_context.wifi_or_bt)
        for fw_list in file_dicts['fw_dict'].values()
        for fw_path in fw_list
        if fw_path
    }
    auto_analysis_fw = session.pop('auto_analysis_fw', None)
    wifi_table_rows = _build_wifi_table_rows(file_dicts['wifi_dict'], download_path=download_path)
    bt_table_rows = _build_bt_table_rows(file_dicts['bt_dict'], download_path=download_path)
    event_table_rows = _build_event_table_rows(file_dicts['ddd_dict'], download_path=download_path)
    fw_table_rows = _build_fw_table_rows(file_dicts['fw_dict'], download_path=download_path)

    # Expand any .etl that was split: original renamed to .etl.split → show _split*.etl parts instead.
    # This handles the case where the user returns to download_result after Bluetooth Analysis Agent
    # already ran a split; the original file is gone but the split parts still exist.
    for zip_name, bt_paths in list((file_dicts.get('bt_dict') or {}).items()):
        expanded = []
        for bp in (bt_paths or []):
            if not os.path.isfile(bp) and os.path.isfile(bp + '.split'):
                split_dir = os.path.dirname(bp)
                base_name = os.path.splitext(os.path.basename(bp))[0]
                parts = sorted(glob.glob(os.path.join(split_dir, f"{base_name}_split*.etl")))
                if parts:
                    expanded.extend(parts)
                    print(f"[download_result] {os.path.basename(bp)} was split; replacing with {[os.path.basename(p) for p in parts]}")
                    continue
            expanded.append(bp)
        file_dicts['bt_dict'][zip_name] = expanded

    # Compute BT file sizes for display and auto-select filtering
    bt_file_sizes = {}
    for bt_paths in (file_dicts.get('bt_dict') or {}).values():
        for bp in (bt_paths or []):
            try:
                bt_file_sizes[bp] = os.path.getsize(bp) if os.path.isfile(bp) else 0
            except OSError:
                bt_file_sizes[bp] = 0

    # Find the evt path with the latest timestamp for auto-load
    latest_evt_path = None
    latest_evt_time = None

    for row in event_table_rows:
        ts = extract_timestamp_from_folder(row['ddd_path'])
        if ts and (latest_evt_time is None or ts > latest_evt_time):
            latest_evt_time = ts
            latest_evt_path = row['ddd_path']
    
    if not latest_evt_path and event_table_rows:
        latest_evt_path = event_table_rows[-1]['ddd_path'] 

    return render_template('download_result.html',
                         case_path=download_path,
                         llm_issue_time=llm_issue_time,
                         llm_issue_times=llm_issue_times,
                         ai_pre_selected_etl=ai_pre_selected_etl,
                         wifi_or_bt=case_context.wifi_or_bt,
                         files_coexist=bool(case_context.files_coexist),
                         auto_analysis_etl = auto_analysis_etl,
                         auto_analysis_etl_reason = auto_analysis_etl_reason,
                         auto_analysis_bt=auto_analysis_bt,
                         auto_analysis_fw=auto_analysis_fw,
						 sendto_auto_llm=bool(session.get('sendto_auto_llm')),
                         exclude_keywords=app_config.etl_exclude_keywords,
                         latest_fw_system_info=latest_fw_system_info,
                         latest_fw_system_info_path=latest_fw_system_info_path,
                         wifi_table_rows=wifi_table_rows,
                         bt_table_rows=bt_table_rows,
                         event_table_rows=event_table_rows,
                         fw_table_rows=fw_table_rows,
                         fw_type_hints=fw_type_hints,
                         bt_file_sizes=bt_file_sizes,
                         latest_evt_path=latest_evt_path,
                         time_filter_info=time_filter_info,
                         time_filter_warnings=time_filter_warnings,
                         **file_dicts)


#------------ [BSOD] DOWNLOAD RESULT render -------------#

def render_download_result_bsod_form():

    case_context = session.get("case_context")
    if not case_context:
        flash("Session expired. Please start again.", "warning")
        return redirect(url_for('main.index'))
    case_context = CaseContext.from_session(case_context)
    download_path = _resolve_download_path(case_context, True)
    if not download_path:
        flash("Session expired. Please start again.", "warning")
        return redirect(url_for('main.index'))

    case_path = download_path
    email = helpers.detect_user_email()

    return render_template('bsod.html', 
                           case_nbr=case_context.case_nbr, 
                           email=email, 
                           case_path=case_path)


#------------ Utility Handlers -------------#

def handle_open_path():
    """Open a local folder path in Windows Explorer."""
    path = request.get_json(silent=True) or {}
    path = path.get('path', '')
    print("Now opening path:", path)
    if path and os.path.exists(path):
        subprocess.run(['explorer', path])
        return '', 204
    return 'Invalid path', 400

def handle_dump_event_txt():
    try:
        from services import event_log_service
    except ImportError as e:
        return jsonify({'error': f'Event log feature unavailable: {e}'}), 503

    path = request.get_json(silent=True) or {}
    path = path.get('path', '')
    if not path or not os.path.exists(path):
        return jsonify({'error': 'Invalid path'}), 400
    try:
        txt_path, count = event_log_service.dump_event_to_txt(path)
        print(f"[Background Task] ✅ Processed {count} events → {txt_path}")
        return jsonify({'success': True, 'txt_path': txt_path})
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500

def handle_parse_event_log():
    try:
        from services import event_log_service
    except ImportError as e:
        return jsonify({'error': f'Event log feature unavailable: {e}'}), 503

    path = request.get_json(silent=True) or {}
    path = path.get('path', '')
    print(f"\n[UI View] 🔍 Scanning Event Log: {path}")
    if not path or not os.path.exists(path):
        return jsonify({'error': 'Invalid path'}), 400
    try:
        offset = max(0, int(request.json.get('offset', 0) or 0))
        limit = int(request.json.get('limit', 0) or 0)
        source_filter = request.json.get('source_filter', 'all')
        level_filter = request.json.get('level_filter', 'all')
        result = event_log_service.get_paged_events(path, offset, limit, source_filter, level_filter)
        return jsonify(result)
    except Exception as e:
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
