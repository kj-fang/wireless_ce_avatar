# services/log_parser_service.py
import os
import shutil
import datetime
import threading
from concurrent.futures import ThreadPoolExecutor
from flask import session
from typing import Dict, Any, Optional, Tuple
import markdown

from configs.path_configs import LOG_PARSER_DIR
from configs.global_configs import app_config

from utils import helpers
from utils.log_parser_file_utils import (
    get_available_filters, get_available_prompts,
    get_sys_prompt_content
)
from utils.log_parser_preprocess import (
    filter_log_by_keywords, extract_enabled_keywords_from_filter_file,
    preprocess_log_for_llm, group_similar_logs)



class LogParserService:
    def __init__(self):
        self.log_parser_dir = LOG_PARSER_DIR
        self.progress_status = {
            'percentage': 0,
            'message': 'Ready',
            'status': 'idle'
        }

        self.analysis_result = {
            'llm_result_html': None,
            'log_output_path': None
        }
    
    # -------------------- setup and check avalibility ------------- 
    def set_up(self, download_path: str) -> str:
        timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        output_dir = os.path.join(download_path, f"log_data/run_{timestamp}")
        os.makedirs(output_dir, exist_ok=True)
        return output_dir
    
    def check_auto_analysis_availability(self, classification: Dict) -> Tuple[bool, Optional[Dict]]:
        if 'issue_type' not in classification or classification['issue_type'] == "Unclassified":
            return False, None
            
        issue_type = classification['issue_type'].lower()
        filter_file = f"{issue_type}.tat"
        prompt_file = f"{issue_type}.py"
        
        filter_path = os.path.join(self.log_parser_dir, "filter", filter_file)
        prompt_path = os.path.join(self.log_parser_dir, "prompt", prompt_file)
        
        if os.path.exists(filter_path) and os.path.exists(prompt_path):
            return True, {
                'filter_file': filter_file,
                'prompt_file': prompt_file,
                'issue_type': issue_type
            }
        return False, None
    
    def prepare_log_file(self, etl_path_input: str, output_dir: str) -> Optional[str]:
        if not etl_path_input:
            return None
            
        log_path_input = rf"{etl_path_input}.log"
        if not os.path.exists(log_path_input):
            return None
            
        filename = os.path.basename(log_path_input) or "etl_file"
        log_path = os.path.join(output_dir, filename)
        shutil.copy2(log_path_input, log_path)
        return log_path
    
    def get_available_resources(self) -> Tuple[list, Tuple[list, list]]:

        with ThreadPoolExecutor(max_workers=2) as executor:
            future_filters = executor.submit(get_available_filters, self.log_parser_dir)
            future_prompts = executor.submit(get_available_prompts, self.log_parser_dir)
            
            available_filters = future_filters.result()
            available_prompts, available_custom_prompts = future_prompts.result()
            
        return available_filters, (available_prompts, available_custom_prompts)
    
    def validate_analysis_inputs(self, log_path: str, selected_filter: str, 
                               custom_prompt_content: str) -> Tuple[bool, str]:
        if not log_path:
            return False, "Log path is required"
        if not selected_filter:
            return False, "Filter file is required"
        if not custom_prompt_content:
            return False, "Prompt content is required"
        return True, ""
    
    def load_prompt_content(self, prompt_type: str, prompt_file: str) -> str:

        if prompt_type == 'template':
            prompt_dir = os.path.join(self.log_parser_dir, "prompt")
        else:
            prompt_dir = os.path.join(self.log_parser_dir, "custom_prompt")
        
        file_path = os.path.join(prompt_dir, prompt_file)
        return get_sys_prompt_content(file_path)

    #-------------- analyze progress ------------------

    def start_analysis(self, filter_path: str, log_path: str, output_dir: str, 
                      llm_helper, custom_prompt_content: str) -> bool:
        try:
            thread = threading.Thread(
                target=self.process_analysis,
                args=(filter_path, log_path, output_dir, llm_helper, custom_prompt_content)
            )
            thread.daemon = True
            thread.start()
            return True
        except Exception as e:
            print(f"Failed to start analysis: {str(e)}")
            return False
    
    def update_progress(self, percentage, message):
        self.progress_status['percentage'] = percentage
        self.progress_status['message'] = message
        self.progress_status['status'] = 'processing' if percentage < 100 else 'completed'
        print(f"Progress: {percentage}% - {message}")
        app_config.socketio.emit('Progress', {
                        'percentage': percentage,
                        'message': message,
                        'status': self.progress_status['status']
                    }, namespace='/progress')
        

    def process_analysis(self, filter_path, log_path, output_dir, llm_helper, prompt):

        try:
            self.reset_log_parser()
            self.analysis_result['status'] = 'processing'

            # 1: Reading log file
            self.update_progress(35, "Reading log file...")
            log_file = log_path
            log_lines = helpers.read_log_file(log_file)
            
            # 2: Filter keywords(tat)
            self.update_progress(40, "Extracting filter keywords...")
            filter_keywords = extract_enabled_keywords_from_filter_file(filter_path)
            
            # 3: Filter keywords
            self.update_progress(55, "Filtering log entries...")
            filtered_log = filter_log_by_keywords(log_lines, filter_keywords)
            helpers.save_file(os.path.join(output_dir, "filtered.log"), filtered_log, ensure_newline=True)
            
            # 4: Preprocess log
            self.update_progress(70, "Preprocessing log for LLM...")
            processed_lines = preprocess_log_for_llm(filtered_log)
            grouped = group_similar_logs(processed_lines)
            
            save_filtered_log_path = os.path.join(output_dir, "filtered_preprocessed.log")
            helpers.save_file(save_filtered_log_path, grouped, ensure_newline=True)
            
            # 5: LLM analysis
            self.update_progress(85, "Running LLM analysis...")
            llm_result = llm_helper.analyze_log(
                system_content=prompt,
                log=str(grouped)
            )
            
            # 6: done
            self.update_progress(100, "Analysis completed!")
            
            # save result
            self.analysis_result['llm_result_html'] = markdown.markdown(llm_result, 
                                                                extensions=["fenced_code", "tables", "nl2br", "sane_lists", "codehilite"])
            self.analysis_result['log_output_path'] = save_filtered_log_path
            app_config.socketio.emit('analysis_completed', {
                'success': True,
                'result_html': self.analysis_result['llm_result_html'],
                'log_output_path': self.analysis_result['log_output_path']
            }, namespace='/progress')
            return True
            
        except Exception as e:
            self.update_progress(0, f"Error: {str(e)}")
            return False
        
    def reset_log_parser(self):
        self.progress_status = {
            'percentage': 0,
            'message': 'Ready',
            'status': 'idle'
        }
        self.analysis_result = {
            'llm_result_html': None,
            'log_output_path': None
        }
