from typing import Optional, Dict, Any
from services.driver_manage_service import DriverManager
from services.llm_service import LLM_helper
from flask_socketio import SocketIO


class GlobalConfig:
    
    def __init__(self):
    
        # Core services
        self.socketio: Optional[SocketIO] = None
        self.driver_manager: Optional[DriverManager] = None
        self.llm_helper: Optional[LLM_helper] = None
        self.key_module: Optional[Any] = None
        
        # Directory paths
        self.avatarfiles_dir: Optional[str] = None
        self.driver_dir: Optional[str] = None
        self.prompt_dir: Optional[str] = None
        self.project_root: Optional[str] = None
        # Download results storage
        self.download_results: Dict[str, Dict[str, Any]] = {}
    
    # SocketIO management
    def set_socketio(self, socketio: SocketIO) -> None:
        self.socketio = socketio
    
    # Driver Manager
    def set_driver_manager(self, driver_manager: DriverManager) -> None:
        self.driver_manager = driver_manager
    
    # LLM Helper
    def set_llm_helper(self, llm_helper: LLM_helper) -> None:
        self.llm_helper = llm_helper
    
    # Key management
    def set_key(self, key: Any) -> None:
        self.key = key
    
    def get_key(self) -> Any:
        if self.key is None:
            raise RuntimeError("Key not initialized. Call set_key() first.")
        return self.key
    
    # Directory paths
    def set_avatarfiles_dir(self, path: str) -> None:
        self.avatarfiles_dir = path
    
    def set_driver_dir(self, path: str) -> None:
        self.driver_dir = path
    
    def set_prompt_dir(self, path: str) -> None:
        self.prompt_dir = path
    
    def set_project_root(self, path: str) -> None:
        self.project_root = path
    
    
    # Download results management
    def set_download_results(self, case_nbr: str, **results) -> None:
        defaults = {'wifi': {}, 'ddd': {}, 'bt': {}, 'fw': {}}
        self.download_results[case_nbr] = {**defaults, **results}
    
    def get_download_results(self, case_nbr: str) -> Dict[str, Any]:
        return self.download_results.get(case_nbr, {'wifi': {}, 'ddd': {}, 'bt': {}, 'fw': {}})
    
    def clear_download_results(self, case_nbr: str = None) -> None:
        if case_nbr:
            self.download_results.pop(case_nbr, None)
        else:
            self.download_results.clear()
    
    # Utility methods
    def is_initialized(self) -> Dict[str, bool]:
        return {
            'socketio': self.socketio is not None,
            'driver_manager': self.driver_manager is not None,
            'llm_helper': self.llm_helper is not None,
            'key': self.key is not None,
            'project_root': self.project_root is not None,
        }
    
    def reset(self) -> None:
        self.__init__()

app_config = GlobalConfig()



