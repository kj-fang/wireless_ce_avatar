import re
import os
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
        print(f"⚠️ Failed to extract address digits from: {path}\n{e}")
    return []


def extract_etl_suffix_number(path):
    name = os.path.basename(path)
    match = re.search(r'\.etl\.(\d+)', name)
    return int(match.group(1)) if match else -1


