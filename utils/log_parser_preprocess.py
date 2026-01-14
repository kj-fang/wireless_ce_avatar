import re
from collections import defaultdict
from typing import List


#---------------- log filter ---------------

def extract_enabled_keywords_from_filter_file(filter_file_path: str) -> List[str]:
    """
    Extracts all `text` attributes from filter lines where `enabled="y"`.
    """
    enabled_keywords = []
    pattern = re.compile(r'enabled="y".*?text="(.*?)"', re.IGNORECASE)

    with open(filter_file_path, "r", encoding="utf-8") as f:
        for line in f:
            match = pattern.search(line)
            if match:
                keyword = match.group(1).strip()
                enabled_keywords.append(keyword)
    print("enabled_keyword", enabled_keywords)
    return enabled_keywords

def filter_log_by_keywords(log_lines: List[str], keywords: List[str]) -> List[str]:
    """
    Filters log lines based on enabled keywords and removes the first character (e.g., line number or symbol).
    """
    filtered = []

    for line in log_lines:
        if any(k.lower() in line.lower() for k in keywords):
            cleaned_line = re.sub(r"^\d+\t", "", line)  # Remove first line number and leading whitespace
            filtered.append(cleaned_line)

    return filtered

#---------------- preprocess filtered log ---------------

def preprocess_log_for_llm(log_lines, preserve_timestamps=True):
    processed_lines = []
    
    for line in log_lines:
        if not line.strip():
            continue
            
        # 1. timestamp
        if preserve_timestamps:
            line = re.sub(r'(\d{2}/\d{2}/\d{4})-(\d{2}:\d{2}:\d{2})\.\d{3}', r'<TIME:\2>', line)
        else:
            line = re.sub(r'\d{2}/\d{2}/\d{4}-\d{2}:\d{2}:\d{2}\.\d{3}', '<TIMESTAMP>', line)
        
        # 2. unify 
        line = re.sub(r'\[(\d+)\]', '', line)
        
        # ETW
        line = re.sub(r'etwTimeStamp\s*=\s*\d+', '', line)
        
        # addr
        line = re.sub(r'etwEvtDataAddress\s*=\s*[0-9A-F]+', '', line)
        
        # hex
        line = re.sub(r'\b[0-9A-F]{8,}\b', '<HEX_VALUE>', line)
        
        # IP
        line = re.sub(r'\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b', '<IP_ADDR>', line)
        
        # arg
        line = re.sub(r'etwLength\s*=\s*\d+', '', line)
        
        # 3. space remove
        line = re.sub(r'\s+', ' ', line)
        
        processed_lines.append(line.strip())
    
    return processed_lines

def group_similar_logs(processed_lines):

    grouped_logs = defaultdict(list)
    
    for line in processed_lines:
        pattern = re.sub(r'\[TIME:[^\]]+\]', '[TIME:*]', line)
        grouped_logs[pattern].append(line)
    
    result = []
    for pattern, lines in grouped_logs.items():
        if len(lines) == 1:
            result.append(lines[0])
        elif len(lines) <= 2:
            result.extend(lines)
        else:
            first_time = re.search(r'\[TIME:([^\]]+)\]', lines[0])
            last_time = re.search(r'\[TIME:([^\]]+)\]', lines[-1])
            
            result.append(lines[0])
            if first_time and last_time:
                result.append(f"... (repeated {len(lines)-2} times between {first_time.group(1)} and {last_time.group(1)}) ...")
            else:
                result.append(f"... (repeated {len(lines)-2} times) ...")
            result.append(lines[-1])
    
    return result

