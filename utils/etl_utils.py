import re
import os
from datetime import datetime
from flask import session

#-----------LATEST ETL LLM UTILS--------------
def get_auto_analysis_etl(wifi_dict, ddd_dict):
    if not session.get('latest_etl_llm'):
        return None
        
    session['latest_etl_llm'] = None
    
    if any(ddd_dict.values()):
        ddd_files = [f for files in ddd_dict.values() if files for f in files]
        if ddd_files:
            return max(ddd_files, key=extract_file_number)
    
    etl_paths = [f for files in wifi_dict.values() if files for f in files if 'history' not in f.lower()]
    if etl_paths:
        sorted_etls = sorted(etl_paths, 
                           key=lambda x: (extract_address_digits(x), extract_etl_suffix_number(x)), 
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
        - None if nothing is found
    """
    if not description:
        return None
    
    # Flag to track if we attempted to extract datetime, so we only try time if datetime extraction fails
    attempted_datetime = False 

    # Try to match full datetime patterns first
    # Pattern 1: YYYY-MM-DD HH:MM:SS or YYYY/MM/DD HH:MM:SS
    datetime_pattern1 = r'(\d{4})[-/](\d{1,2})[-/](\d{1,2})\s+(\d{1,2}):(\d{2}):(\d{2})'
    match = re.search(datetime_pattern1, description)
    if match:
        attempted_datetime = True

        year, month, day, hour, minute, second = match.groups()
        try:
            return datetime(int(year), int(month), int(day), 
                          int(hour), int(minute), int(second))
        except ValueError as e:
            print(f"Invalid datetime values: {e}")
    
    # Pattern 2 & 3 Combined: Intelligent date parsing (MM-DD-YYYY or DD-MM-YYYY)
    datetime_pattern_ambiguous = r'(\d{1,2})[-/](\d{1,2})[-/](\d{4})\s+(\d{1,2}):(\d{2}):(\d{2})'
    match = re.search(datetime_pattern_ambiguous, description)
    if match:
        attempted_datetime = True

        first, second, year, hour, minute, second_time = match.groups()
        first, second = int(first), int(second)
        
        # Rule 1: If first > 12, must be DD-MM-YYYY (European)
        if first > 12:
            day, month = first, second
        # Rule 2: If second > 12, must be MM-DD-YYYY (US)
        elif second > 12:
            month, day = first, second
        # Rule 3: Both <= 12, ambiguous - default to MM-DD-YYYY
        else:
            month, day = first, second  # Default to US format
            print(f"Ambiguous date {first}-{second}-{year}, assuming MM-DD-YYYY (US format)")
        
        try:
            return datetime(int(year), month, day, int(hour), int(minute), int(second_time))
        except ValueError as e:
            print(f"Invalid datetime values: {e}")
    
    # If no full datetime found, try to match time only
    if not attempted_datetime:
        print("No full datetime found, trying to extract time only...")
        time_pattern = r'(\d{1,2}):(\d{2}):(\d{2})'
        match = re.search(time_pattern, description)
        if match:
            hour, minute, second = match.groups()
            hour, minute, second = int(hour), int(minute), int(second)
            
            # Validate time ranges
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
        # Extract date and time pattern: MM-DD-YYYY_HH-MM-SS
        pattern = r'(\d{2})-(\d{2})-(\d{4})_(\d{2})-(\d{2})-(\d{2})'
        match = re.search(pattern, folder_path)
        
        if match:
            month, day, year, hour, minute, second = match.groups()
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
    """
    if not issue_time_or_datetime:
        return file_dict, {}
    
    # Check if we have a full datetime or just time
    is_full_datetime = isinstance(issue_time_or_datetime, datetime)
    
    if is_full_datetime:
        # We have full datetime - simpler logic
        issue_datetime = issue_time_or_datetime
        print(f"Using full datetime for filtering: {issue_datetime}")
        
        filtered_dict = file_dict.copy()  # Start with all entries to ensure nothing is lost
        warnings_dict = {}
        
        for zip_name, folder_list in file_dict.items():
            if not folder_list:
                continue  # Keep original empty list from copy
            
            # Extract timestamps from all folders
            folder_times = []
            for folder in folder_list:
                timestamp = extract_timestamp_from_folder(folder)
                if timestamp:
                    folder_times.append((folder, timestamp))
            
            if not folder_times:
                continue  # Keep original list from copy
            
            # Find folder closest to issue datetime (prefer after)
            folders_after = [(f, t) for f, t in folder_times if t >= issue_datetime]
            
            if folders_after:
                closest = min(folders_after, key=lambda x: (x[1] - issue_datetime).total_seconds())
                filtered_dict[zip_name] = [closest[0]]
            else:
                # No folder after issue time - keep all folders (already in filtered_dict) and add warning
                warnings_dict[zip_name] = f"All folders are before issue time {issue_datetime.strftime('%Y-%m-%d %H:%M:%S')}"
                print(f"WARNING {zip_name}: All folders are before issue time")
        
        return filtered_dict, warnings_dict
    
    else:
        # We only have time string - use date grouping logic
        issue_time_str = issue_time_or_datetime
        print(f"Using time-only for filtering: {issue_time_str}")
        
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
                timestamp = extract_timestamp_from_folder(folder)
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
            best_folders = []
            for date_key, folders_on_date in date_groups.items():
                try:
                    issue_datetime = datetime.combine(date_key, 
                                                     datetime.min.time().replace(
                                                         hour=issue_hour, 
                                                         minute=issue_min, 
                                                         second=issue_sec))
                    
                    # Find folders after issue time on this date
                    folders_after = [(f, t) for f, t in folders_on_date if t >= issue_datetime]
                    
                    if folders_after:
                        # Get the closest folder after issue time
                        closest = min(folders_after, key=lambda x: (x[1] - issue_datetime).total_seconds())
                        best_folders.append((closest, False))  # False = no warning
                    else:
                        # If no folder after issue time on this date, mark for potential warning
                        latest = max(folders_on_date, key=lambda x: x[1])
                        best_folders.append((latest, True))  # True = all before issue time
                except Exception as e:
                    print(f"Error processing date {date_key}: {e}")
                    continue
            
            if best_folders:
                # Check if all candidates are before issue time
                all_before = all(has_warning for _, has_warning in best_folders)
                
                if all_before:
                    # All folders are before issue time - keep all (already in filtered_dict) and add warning
                    warnings_dict[zip_name] = f"All folders are before issue time {issue_time_str}"
                    print(f"WARNING {zip_name}: All folders are before issue time {issue_time_str}")
                else:
                    # Filter to only keep folders that are actually after issue time
                    folders_after_only = [(folder_time, warn) for folder_time, warn in best_folders if not warn]
                    
                    if folders_after_only:
                        # Find the folder with time closest to issue time (same time across different dates)
                        def time_distance(item):
                            (folder, timestamp), has_warning = item
                            # Create issue datetime for this folder's date
                            issue_on_this_date = datetime.combine(timestamp.date(), 
                                                                 datetime.min.time().replace(
                                                                     hour=issue_hour, 
                                                                     minute=issue_min, 
                                                                     second=issue_sec))
                            # No need for abs() since we only have folders after issue time
                            return (timestamp - issue_on_this_date).total_seconds()
                        
                        closest_overall, _ = min(folders_after_only, key=time_distance)
                        filtered_dict[zip_name] = [closest_overall[0]]
            # else: Fallback - keep all folders (already in filtered_dict) if something went wrong
        
        return filtered_dict, warnings_dict


def get_issue_time_from_selected_files(selected_files):
    """
    Extract issue time/datetime from selected_files in session.
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
            description = file_info[2][1]  # Get description
            issue_time_or_datetime = extract_time_from_description(description)
            if issue_time_or_datetime:
                time_mapping[file_name] = issue_time_or_datetime
                if isinstance(issue_time_or_datetime, datetime):
                    print(f"Found datetime for {file_name}: {issue_time_or_datetime}")
                else:
                    print(f"Found time for {file_name}: {issue_time_or_datetime}")
    
    return time_mapping

