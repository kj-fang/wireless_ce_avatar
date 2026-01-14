import os
import importlib.util
from flask import jsonify
from concurrent.futures import ThreadPoolExecutor

def read_file_first_line(prompt_dir, prompt_file):
    try:
        file_path = os.path.join(prompt_dir, prompt_file)
        with open(file_path, 'r', encoding='utf-8') as f:
            first_line = f.readline(80)
        return os.path.basename(file_path), first_line.strip(), None
        
    except Exception as e:
        print(f"Error reading {file_path}: {e}")
        return False

def check_valid_prompt_dir(prompt_dir):
    available_prompts = []
    if os.path.exists(prompt_dir):
        prompts_path = [f for f in os.listdir(prompt_dir) if f.endswith('.py')]
        
        with ThreadPoolExecutor(max_workers=10) as executor:
            futures = [executor.submit(read_file_first_line,prompt_dir, fp) for fp in prompts_path]
            
            for future in futures:
                filename, first_line, error = future.result()
                if error:
                    continue
                    
                if (first_line and 
                    first_line.startswith('SYS_PROMPT') and 
                    '=' in first_line):
                    available_prompts.append(filename)

    print("available_prompts:", available_prompts)
    return available_prompts

def get_sys_prompt_content(file_path):
    """ SYS_PROMPT """
    try:
        spec = importlib.util.spec_from_file_location("temp_module", file_path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        
        if hasattr(module, 'SYS_PROMPT'):
            return getattr(module, 'SYS_PROMPT')
        return ""
    except Exception as e:
        print(f"Error loading prompt content from {file_path}: {e}")
        return ""

def get_available_filters(log_parser_dir):
    filter_dir = os.path.join(log_parser_dir, "filter")
    if os.path.exists(filter_dir):
        return [f for f in os.listdir(filter_dir) if f.endswith('.tat')]
    return []

def get_available_prompts(log_parser_dir):
    prompt_dir = os.path.join(log_parser_dir, "prompt")
    custom_prompt_dir = os.path.join(log_parser_dir, "custom_prompt")
    
    with ThreadPoolExecutor(max_workers=2) as executor:
        future_prompts = executor.submit(check_valid_prompt_dir, prompt_dir)
        future_custom_prompts = executor.submit(check_valid_prompt_dir, custom_prompt_dir)

        available_prompts = future_prompts.result()
        available_custom_prompts = future_custom_prompts.result()
    
    return available_prompts, available_custom_prompts


#------------------ file upload and update ----------------

