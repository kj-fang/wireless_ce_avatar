import re
import os
from datetime import datetime, timedelta
from flask import session

# Shared timezone helpers — the autologger writes folder names with the
# CUSTOMER's machine clock (e.g. CST), but engineers often type ``issue_time``
# transcribed straight from the ETL-decoded log (which is in the log frame,
# GMT+8). When those two frames disagree, the ±5-minute Segment-2 window in
# ``filter_folders_by_time`` never matches anything. We shift the folder ts
# into the log frame in that case so the comparison lands in the same frame.
from utils.timezone_utils import (
    get_effective_timezone as _tz_get_effective_timezone,
    get_issue_time_basis as _tz_get_issue_time_basis,
    local_to_taiwan as _tz_local_to_taiwan,
)

#-----------ZIP ATTACHMENT SELECTION--------------
def pick_latest_zip_attachment(attachment_list):
    """Return the ZIP attachment with the most recent datetime metadata.
    Falls back to the first ZIP found if no datetime can be parsed."""
    zip_attachments = [item for item in (attachment_list or []) if item and str(item[0]).lower().endswith('.zip')]
    if not zip_attachments:
        return None

    def parse_attachment_datetime(item):
        try:
            metadata = item[2] if len(item) > 2 else None
            raw_dt = metadata[0] if isinstance(metadata, (list, tuple)) and metadata else None
            if isinstance(raw_dt, datetime):
                return raw_dt
        except Exception:
            pass
        return datetime.min

    return max(zip_attachments, key=parse_attachment_datetime)

#-----------LATEST ETL LLM UTILS--------------
def get_auto_analysis_etl(wifi_dict, ddd_dict):
    if not session.get('latest_etl_llm'):
        return None
        
    session['latest_etl_llm'] = None
    
    if any(ddd_dict.values()):
        ddd_files = [f for files in ddd_dict.values() if files for f in files
                     if f.lower().endswith('.etl') or re.search(r'\.etl\.\d+$', f, re.IGNORECASE)]
        if ddd_files:
            return max(ddd_files, key=extract_file_number)
    
    _EXCLUDE_FOLDER_KEYWORDS = ('history', 'autologger', 'pldr', 'stopservice')
    etl_paths = [
        f for files in wifi_dict.values() if files
        for f in files
        if not any(kw in os.path.basename(os.path.dirname(f)).lower() for kw in _EXCLUDE_FOLDER_KEYWORDS)
    ]
    if etl_paths:
        sorted_etls = sorted(etl_paths,
                           key=lambda x: (extract_timestamp_from_folder(x) or datetime.min, extract_etl_suffix_number(x)),
                           reverse=True)
        return sorted_etls[0] if sorted_etls else None
    
    return None

def extract_file_number(filepath):
    nums = re.findall(r'\d+', os.path.basename(filepath))
    return int(nums[-1]) if nums else -1

def extract_address_digits(path):
    try:
        parts = path.split(os.sep)
        for i, part in enumerate(parts):
            if re.fullmatch(r'\d{8}', part):  # Match IPS folder like '00960179'
                if i + 1 < len(parts):
                    addr_folder = parts[i + 1]  # Take folder after IPS number
                    numbers = re.findall(r'\d+', addr_folder)
                    return [int(n) for n in numbers]
    except Exception as e:
        print(f"Failed to extract address digits from: {path}\n{e}")
    return []


def extract_etl_suffix_number(path):
    name = os.path.basename(path)
    match = re.search(r'\.etl\.(\d+)', name)
    return int(match.group(1)) if match else -1


#-----------TIME-BASED FILTERING UTILS--------------
def extract_time_from_description(description):
    """
    Extract time or datetime from description.
    Supports formats like:
    - 'Issue happened at 14:15:18.'
    - 'Issue happened at 2025-02-09 14:15:18.'
    - 'Issue happened at 02/09/2025 14:15:18.'
    - 'Issue happened at 02-09-2025 14:15:18.'
    
        Returns:
                - datetime object if full date+time is found
                - time string in 'HH:MM:SS' format if only time is found
                    (accepts both HH:MM and HH:MM:SS in source text)
                - None if nothing is found
    """
    if not description:
        return None
    
    # Normalize non-breaking spaces in Salesforce rich text outputs.
    text = str(description).replace('\xa0', ' ')

    # Pattern 1: YYYY-MM-DD HH:MM:SS(.sss) or YYYY/MM/DD HH:MM:SS(.sss)
    # Also supports separator between date/time as either space or '-'.
    match = re.search(
        r'(\d{4})[-/](\d{1,2})[-/](\d{1,2})[\sT-]+(\d{1,2}):(\d{2}):(\d{2})(?:\.(\d{1,6}))?',
        text,
    )
    if match:
        year, month, day, hour, minute, second, micro = match.groups()
        try:
            microsecond = int((micro or '0').ljust(6, '0')[:6])
            return datetime(
                int(year), int(month), int(day), int(hour), int(minute), int(second), microsecond
            )
        except ValueError as e:
            print(f"Invalid datetime values: {e}")

    # Pattern 2: MM/DD/YYYY-HH:MM:SS(.sss) or DD/MM/YYYY-HH:MM:SS(.sss)
    match = re.search(
        r'(\d{1,2})[-/](\d{1,2})[-/](\d{4})[\sT-]+(\d{1,2}):(\d{2}):(\d{2})(?:\.(\d{1,6}))?',
        text,
    )
    if match:
        first_part, second_part, year, hour, minute, second_time, micro = match.groups()
        first_part, second_part = int(first_part), int(second_part)

        # Disambiguation rule for dates like 04/02/2026:
        # when ambiguous, default to MM/DD/YYYY to match attachment description convention.
        if first_part > 12:
            day, month = first_part, second_part
        else:
            month, day = first_part, second_part

        try:
            microsecond = int((micro or '0').ljust(6, '0')[:6])
            return datetime(int(year), month, day, int(hour), int(minute), int(second_time), microsecond)
        except ValueError as e:
            print(f"Invalid datetime values: {e}")

    # Pattern 3: time only HH:MM[:SS] with optional AM/PM, for descriptions
    # without a date (e.g. "issue happened at 04:45 PM"). The AM/PM suffix is
    # significant — "04:45 PM" is 16:45, and dropping it shifts the issue time
    # 12 h, which then misses the ±5-min PreScan window entirely.
    match = re.search(
        r'(?<!\d)(\d{1,2}):(\d{2})(?::(\d{2}))?(?:\s*([AaPp][Mm]))?(?!\d)',
        text,
    )
    if match:
        hour = int(match.group(1))
        minute = int(match.group(2))
        second = int(match.group(3) or 0)
        ampm = (match.group(4) or '').lower()
        if ampm == 'pm' and hour < 12:
            hour += 12
        elif ampm == 'am' and hour == 12:
            hour = 0
        if not (0 <= hour <= 23 and 0 <= minute <= 59 and 0 <= second <= 59):
            print(f"Invalid time values: {hour}:{minute}:{second}")
            return None
        return f"{str(hour).zfill(2)}:{str(minute).zfill(2)}:{str(second).zfill(2)}"

    return None


def extract_timestamp_from_folder(folder_path):
    """
    Extract timestamp from folder name like:
    'LAPTOP-H2CAUN1I_02-09-2025_14-13-56_518_5_5055_0x20100508_0x1ef80002_0x2'
    Returns datetime object or None
    """
    try:
        # Extract date and time pattern: DD-MM-YYYY_HH-MM-SS
        pattern = r'(\d{2})-(\d{2})-(\d{4})_(\d{2})-(\d{2})-(\d{2})'
        match = re.search(pattern, folder_path)
        
        if match:
            day, month, year, hour, minute, second = match.groups()
            return datetime(int(year), int(month), int(day), 
                        int(hour), int(minute), int(second))

    except Exception as e:
        print(f"Failed to extract timestamp from folder: {folder_path}\n{e}")
    
    return None


def filter_folders_by_time(file_dict, issue_time_or_datetime):
    """
    Filter folders based on issue time/datetime from description.
    Keep only the folder closest to (and after) the issue time.

    Args:
        file_dict: Dict like {'zip_name': ['folder_path1', 'folder_path2', ...]}
        issue_time_or_datetime: Either a datetime object or time string in 'HH:MM:SS' format

    Returns:
        Tuple: (filtered_dict, warnings_dict)
        - filtered_dict: Filtered file dict
        - warnings_dict: {zip_name: 'warning_message'} for files where all folders are before issue time

    Note on timezones:
        Autologger writes the folder name (``LUS-..._DD-MM-YYYY_HH-MM-SS_...``)
        in the customer machine's local clock (e.g. CST). The typed
        ``issue_time`` may live in either:
          - "log"      frame (engineer transcribed from ETL output, GMT+8) — we
                        then shift the folder ts FROM customer tz TO log frame
                        via ``local_to_taiwan`` so the ±5-min Segment-2 window
                        compares like-with-like.
          - "customer" frame (customer typed their own clock) — no shift, both
                        sides are already in customer tz.

        Resolution: auto-detected from the first folder's
        ``.timezone_override.json`` sidecar / parent ``system_info.txt``.
    """
    if not issue_time_or_datetime:
        return file_dict, {}

    # Pull customer tz + basis from the first available folder path. Both
    # default cleanly (tz='', basis='log') when nothing is known.
    customer_tz = ""
    basis = "log"
    for _zip, folders in file_dict.items():
        if folders:
            customer_tz = _tz_get_effective_timezone(folders[0])
            basis = _tz_get_issue_time_basis(folders[0])
            print(f"[filter_folders_by_time] tz={customer_tz!r}, basis={basis!r}")
            break
    should_shift_folder = bool(customer_tz) and basis == "log"

    def _align_folder_ts(ts):
        """Shift folder customer-tz ts into the log frame when basis='log'."""
        return _tz_local_to_taiwan(ts, customer_tz) if should_shift_folder else ts

    # Anchor a date-less issue clock (HH:MM[:SS]) to a candidate's decoded
    # ``.log`` last-timestamp date when one exists — consistent with the
    # chatbot's resolve_issue_time(ref=last_ts). Promote it to a full
    # datetime IN THE SAME FRAME the folder ts get compared in (log frame
    # when basis='log', else customer), so it hits the dated branch below
    # instead of the per-folder-date grouping. Falls through unchanged when
    # no candidate carries a ``.log`` sibling.
    if isinstance(issue_time_or_datetime, str):
        _anchor_date = None
        _clock = None
        _anchor_frame = "log" if should_shift_folder else "customer"
        try:
            _ih, _im, _is = (int(x) for x in issue_time_or_datetime.split(':'))
            _clock = (_ih, _im, _is)
            from utils.issue_time_ai import log_anchor_date_for_etl
            for _folders in file_dict.values():
                for _fp in (_folders or []):
                    _anchor_date = log_anchor_date_for_etl(
                        _fp, customer_tz, frame=_anchor_frame)
                    if _anchor_date:
                        break
                if _anchor_date:
                    break
        except Exception as _e:
            print(f"[filter_folders_by_time] log-date anchor skipped: {_e}")
        if _anchor_date and _clock:
            issue_time_or_datetime = datetime.combine(
                _anchor_date,
                datetime.min.time().replace(
                    hour=_clock[0], minute=_clock[1], second=_clock[2]))
            print(f"[filter_folders_by_time] time-only anchored to .log "
                  f"last-date: {issue_time_or_datetime} (frame={_anchor_frame})")

    # Check if we have a full datetime or just time
    is_full_datetime = isinstance(issue_time_or_datetime, datetime)
    
    if is_full_datetime:
        # We have full datetime - simpler logic
        issue_datetime = issue_time_or_datetime
        window_start = issue_datetime - timedelta(minutes=5)
        window_end = issue_datetime + timedelta(minutes=5)
        print(f"Using full datetime for filtering: {issue_datetime} (segment2 window: {window_start} ~ {window_end})")
        
        filtered_dict = file_dict.copy()  # Start with all entries to ensure nothing is lost
        warnings_dict = {}
        
        for zip_name, folder_list in file_dict.items():
            if not folder_list:
                continue  # Keep original empty list from copy

            # Extract timestamps from all folders
            folder_times = []
            for folder in folder_list:
                timestamp = _align_folder_ts(extract_timestamp_from_folder(folder))
                if timestamp:
                    folder_times.append((folder, timestamp))

            if not folder_times:
                continue  # Keep original list from copy

            # Segment2 baseline: keep EVERY folder within issue_time ±5 min,
            # sorted by closeness to the issue moment. Surfacing all of them
            # lets the engineer see the full candidate set on /download_result;
            # ``pick_etl_by_ai_time`` + ``ai_pre_selected_etl`` will tick the
            # most-likely ETL automatically while the others remain pickable
            # in case the AI's first guess is wrong (e.g. two zips extracted
            # the same autologger run into nested + flat paths).
            folders_in_window = [(f, t) for f, t in folder_times if window_start <= t <= window_end]
            if folders_in_window:
                folders_in_window.sort(key=lambda x: abs((x[1] - issue_datetime).total_seconds()))
                filtered_dict[zip_name] = [f for f, _ in folders_in_window]
                continue

            # Fallback: no folder inside the ±5 min window. We still keep
            # ALL ts-after-issue candidates (sorted by ascending distance) so
            # the user can pick one — historical behaviour returned only the
            # single closest one but that meant identical-timestamp duplicates
            # got hidden silently.
            folders_after = [(f, t) for f, t in folder_times if t >= issue_datetime]
            if folders_after:
                folders_after.sort(key=lambda x: (x[1] - issue_datetime).total_seconds())
                filtered_dict[zip_name] = [f for f, _ in folders_after]
                warnings_dict[zip_name] = (
                    f"No folder in segment2 window (+/-5 min) around "
                    f"{issue_datetime.strftime('%Y-%m-%d %H:%M:%S')}"
                )
            else:
                # No folder after issue time - keep all folders (already in filtered_dict) and add warning
                warnings_dict[zip_name] = f"All folders are before issue time {issue_datetime.strftime('%Y-%m-%d %H:%M:%S')}"
                print(f"WARNING {zip_name}: All folders are before issue time")
        
        return filtered_dict, warnings_dict
    
    else:
        # We only have time string - use date grouping logic
        issue_time_str = issue_time_or_datetime
        print(f"Using time-only for filtering: {issue_time_str} (segment2 baseline +/-5 min on each date)")
        
        try:
            # Parse issue time (hour, minute, second)
            issue_hour, issue_min, issue_sec = map(int, issue_time_str.split(':'))
        except Exception as e:
            print(f"Failed to parse issue time: {issue_time_str}\n{e}")
            return file_dict, {}
        
        filtered_dict = file_dict.copy()  # Start with all entries to ensure nothing is lost
        warnings_dict = {}
        
        for zip_name, folder_list in file_dict.items():
            if not folder_list:
                continue  # Keep original empty list from copy

            # Extract timestamps from all folders
            folder_times = []
            for folder in folder_list:
                timestamp = _align_folder_ts(extract_timestamp_from_folder(folder))
                if timestamp:
                    folder_times.append((folder, timestamp))

            if not folder_times:
                # No valid timestamps found, keep all folders (already in filtered_dict)
                continue
            
            # Group folders by date
            date_groups = {}
            for folder, timestamp in folder_times:
                date_key = timestamp.date()
                if date_key not in date_groups:
                    date_groups[date_key] = []
                date_groups[date_key].append((folder, timestamp))
            
            # For each date, create issue datetime and find closest folder
            window_candidates = []
            after_candidates = []
            all_before_by_date = True

            for date_key, folders_on_date in date_groups.items():
                try:
                    issue_datetime = datetime.combine(date_key, 
                                                     datetime.min.time().replace(
                                                         hour=issue_hour, 
                                                         minute=issue_min, 
                                                         second=issue_sec))
                    window_start = issue_datetime - timedelta(minutes=5)
                    window_end = issue_datetime + timedelta(minutes=5)

                    in_window = [(f, t, issue_datetime) for f, t in folders_on_date if window_start <= t <= window_end]
                    if in_window:
                        window_candidates.extend(in_window)

                    folders_after = [(f, t, issue_datetime) for f, t in folders_on_date if t >= issue_datetime]
                    if folders_after:
                        all_before_by_date = False
                        after_candidates.extend(folders_after)
                except Exception as e:
                    print(f"Error processing date {date_key}: {e}")
                    continue
            
            # Keep ALL window candidates (sorted by closeness) so the engineer
            # sees every plausible match; downstream auto-pick highlights the
            # best one. Same rationale as the dated branch above.
            if window_candidates:
                window_candidates.sort(key=lambda x: abs((x[1] - x[2]).total_seconds()))
                filtered_dict[zip_name] = [f for f, _, _ in window_candidates]
            elif after_candidates:
                after_candidates.sort(key=lambda x: (x[1] - x[2]).total_seconds())
                filtered_dict[zip_name] = [f for f, _, _ in after_candidates]
                warnings_dict[zip_name] = f"No folder in segment2 window (+/-5 min) around {issue_time_str}"
            elif all_before_by_date:
                warnings_dict[zip_name] = f"All folders are before issue time {issue_time_str}"
                print(f"WARNING {zip_name}: All folders are before issue time {issue_time_str}")
            # else: keep original list from filtered_dict copy
        
        return filtered_dict, warnings_dict


def get_issue_time_from_selected_files(selected_files):
    """
    Extract issue time/datetime from selected_files in session.
    Source of truth: attachment description text shown as the gray subtitle
    under Choose Attachment in select_attachments.
    Format: [["name", "link", [timestamp, description]], ...]
    
    Returns: 
        Dict mapping file name to time/datetime, e.g.
        {'file1.zip': '14:15:18', 'file2.zip': datetime(...), ...}
        Returns empty dict if no times found
    """
    if not selected_files:
        return {}
    
    time_mapping = {}
    
    for file_info in selected_files:
        if len(file_info) >= 3 and len(file_info[2]) >= 2:
            file_name = file_info[0]  # Get file name
            description = file_info[2][1]  # Gray subtitle text in Choose Attachment
            issue_time_or_datetime = extract_time_from_description(description)
            if issue_time_or_datetime:
                time_mapping[file_name] = issue_time_or_datetime
                if isinstance(issue_time_or_datetime, datetime):
                    print(f"Found datetime for {file_name}: {issue_time_or_datetime}")
                else:
                    print(f"Found time for {file_name}: {issue_time_or_datetime}")
    
    return time_mapping

