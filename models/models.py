from typing import Dict, Any, List, Optional
from dataclasses import dataclass, field, fields
import json

@dataclass
class CaseContext:
    ##----- case info -----##
    case_nbr: Optional[str] = None
    id: Optional[str] = None
    backend_id: Optional[str] = ""
    
    subject: Optional[str] = None
    description: Optional[str] = None
    env_detail: Optional[Dict] = field(default_factory=dict)
    subcategory: Optional[str] = None

    comments: Optional[str | List[str]] = None
    attachment_info: Optional[Dict] = field(default_factory=dict)
    attachment_list: Optional[List[Any]] = field(default_factory=list)

    ##----- case utils -----##
    case_download_dir: Optional[str] = None
    ips_pdf_path: Optional[str] = None
    error_message: Optional[str] = None

    ##----- case attribute -----##
    wifi_or_bt: Optional[str] = None

    ##----- helper functions -----##
    def print_all(self):
        print(f"\n=== {self.__class__.__name__} ===")
        for field in fields(self):
            value = getattr(self, field.name)
            print(f"{field.name:20}: {value}")
        print("=" * 30)

    def to_dict(self):
        return {field.name: getattr(self, field.name) for field in fields(self)}
    
    def to_json(self, indent=2):
        return json.dumps(self.to_dict(), indent=indent, ensure_ascii=False)
    
    def to_session(self):
        return self.to_dict()
    
    @classmethod
    def from_session(cls, data):
        if not data:
            return cls()
        return cls(**data)
    
    