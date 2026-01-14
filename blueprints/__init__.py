from .main import main_bp
from .bsod import bsod_bp
from .analysis_etl import analysis_etl_bp
from .llm import llm_bp
from .automation import automation_bp
from .download import download_bp
from .log_parser import log_parser_bp

__all__ = [
    "main_bp",
    "bsod_bp",
    "analysis_etl_bp",
    "llm_bp",
    "automation_bp",
    "download_bp",
    "log_parser_bp"
]
