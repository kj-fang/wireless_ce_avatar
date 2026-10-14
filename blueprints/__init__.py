from .main import main_bp
from .bsod import bsod_bp
from .analysis_etl import analysis_etl_bp
from .llm import llm_bp
from .automation import automation_bp
from .download import download_bp
from .log_parser import log_parser_bp
from .nw_analysis import nw_analysis_bp
# Both full log-analysis agents come from one route module now — see
# blueprints/chatbot/chatbot_routes.py. Linux still has its own route
# module; folding it in is the next commit on this branch.
from .chatbot import bt_chatbot_bp, log_chatbot_bp
from .linux_chatbot import linux_chatbot_bp
from .feedback import feedback_bp
from .handsfree import handsfree_bp
from .check_ips import check_ips_bp

__all__ = [
    "main_bp",
    "bsod_bp",
    "analysis_etl_bp",
    "llm_bp",
    "automation_bp",
    "download_bp",
    "log_parser_bp",
    "nw_analysis_bp",
    "log_chatbot_bp",
    "bt_chatbot_bp",
    "linux_chatbot_bp",
    "feedback_bp",
    "handsfree_bp",
    "check_ips_bp",
]
