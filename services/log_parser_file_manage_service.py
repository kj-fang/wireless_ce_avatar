import os
from flask import request, jsonify
from typing import Dict, Any

from configs.path_configs import get_log_parser_dir
from utils.log_parser_file_utils import get_available_filters, get_available_prompts

class FileManagerService:
    def __init__(self):
        self.log_parser_dir = get_log_parser_dir()

    def _resolve_log_parser_dir(self) -> str:
        """Re-probe if the dir was never resolved, so a VPN reconnect heals it.

        Not forced: the shared cache retries a failed probe on its own after
        a bounded interval, so repeated lookups right after each other (e.g.
        within one page load) reuse the recent failure instead of each
        paying the full prim/bkup timeout.
        """
        if not self.log_parser_dir:
            self.log_parser_dir = get_log_parser_dir()
        return self.log_parser_dir

    def handle_file_upload(self, upload_type: str, files) -> Dict[str, Any]:
        log_parser_dir = self._resolve_log_parser_dir()
        if upload_type == 'filter':
            return self._handle_filter_upload(files, log_parser_dir)
        elif upload_type == 'prompt':
            return self._handle_prompt_upload(files, log_parser_dir)
        else:
            return {'success': False, 'message': 'Invalid upload type'}
    
    def handle_prompt_operation(self, action: str, filename: str, content: str) -> Dict[str, Any]:
        if not filename or not content:
            return {'success': False, 'message': 'Filename and content are required'}

        log_parser_dir = self._resolve_log_parser_dir()
        if not log_parser_dir:
            return {'success': False,
                    'message': 'Log parser shared folder is unavailable. Please try again later.'}, 503

        if action == 'save':
            return self._handle_save_prompt(filename, content, log_parser_dir)
        elif action == 'update':
            return self._handle_update_prompt(filename, content, log_parser_dir)
        else:
            return {'success': False, 'message': 'Invalid action'}
        
    def _handle_filter_upload(self, files):
        if 'file' not in files:
            return jsonify({'success': False, 'message': 'No file selected'})
        
        file = files['file']
        if file.filename == '':
            return jsonify({'success': False, 'message': 'No file selected'})
        
        if not file.filename.lower().endswith('.tat'):
            return jsonify({'success': False, 'message': 'Please select a .tat file'})
        
        try:
            file_path = os.path.join(self._resolve_log_parser_dir(), "filter", file.filename)
            
            if os.path.exists(file_path):
                return jsonify({'success': False, 'message': f'File "{file.filename}" already exists'})
            
            file.save(file_path)
            
            updated_filters = get_available_filters(self.log_parser_dir)
            
            return jsonify({
                'success': True, 
                'message': f'Filter "{file.filename}" uploaded successfully',
                'uploaded_file': file.filename,
                'filters': updated_filters
            })
        
        except Exception as e:
            return jsonify({'success': False, 'message': f'Error uploading file: {str(e)}'})


    def _handle_prompt_upload(self, files):
        if 'file' not in files:
            return jsonify({'success': False, 'message': 'No file selected'})
        
        file = files['file']
        if file.filename == '':
            return jsonify({'success': False, 'message': 'No file selected'})
        
        if not file.filename.lower().endswith('.py'):
            return jsonify({'success': False, 'message': 'Please select a .py file'})
        
        try:
            file_path = os.path.join(self._resolve_log_parser_dir(), "custom_prompt", file.filename)
            
            if os.path.exists(file_path):
                return jsonify({'success': False, 'message': f'File "{file.filename}" already exists'})
            
            file.save(file_path)

            available_prompts, available_custom_prompts = get_available_prompts(self.log_parser_dir)
            
            return jsonify({
                'success': True, 
                'message': f'Prompt "{file.filename}" uploaded successfully',
                'uploaded_file': file.filename,
                'templates': available_prompts,
                'customs': available_custom_prompts
            })
        
        except Exception as e:
            return jsonify({'success': False, 'message': f'Error uploading file: {str(e)}'})
            
    def _handle_update_prompt(self, filename, content, project_root):
        file_path = os.path.join(project_root, "custom_prompt", filename)
        
        if not os.path.exists(file_path):
            return jsonify({'success': False, 'message': 'File does not exist'})
        
        try:
            file_content = f'''SYS_PROMPT = """{content}"""
            '''
            with open(file_path, 'w', encoding='utf-8') as f:
                f.write(file_content)

            available_prompts, available_custom_prompts = get_available_prompts(project_root)
            
            return jsonify({'success': True, 
                            'message': f'Prompt updated: {filename}',
                            'uploaded_file': filename,
                            'templates': available_prompts,
                            'customs': available_custom_prompts})
        
        except Exception as e:
            return jsonify({'success': False, 'message': f'Error updating file: {str(e)}'})
        

    def _handle_save_prompt(self, filename, content, project_root):
        if not filename.endswith('.py'):
            filename += '.py'
        
        custom_dir = os.path.join(project_root, "custom_prompt")
        os.makedirs(custom_dir, exist_ok=True)
        
        file_path = os.path.join(custom_dir, filename)
        
        if os.path.exists(file_path):
            return jsonify({'success': False, 'message': f'File "{filename}" already exists'})
        
        try:
            file_content = f'''SYS_PROMPT = """{content}"""
            '''
            with open(file_path, 'w', encoding='utf-8') as f:
                f.write(file_content)

            available_prompts, available_custom_prompts = get_available_prompts(project_root)
            
            return jsonify({'success': True, 
                            'message': f'Prompt saved as: {filename}',
                            'uploaded_file': filename,
                            'templates': available_prompts,
                            'customs': available_custom_prompts})
        
        except Exception as e:
            return jsonify({'success': False, 'message': f'Error saving file: {str(e)}'})
