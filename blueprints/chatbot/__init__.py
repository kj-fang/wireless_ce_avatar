"""Both full log-analysis agent blueprints, built from one route module."""

from .chatbot_routes import (
    BT_PROFILE,
    WIFI_PROFILE,
    AgentRouteProfile,
    bt_chatbot_bp,
    log_chatbot_bp,
)

__all__ = [
    "AgentRouteProfile",
    "BT_PROFILE",
    "WIFI_PROFILE",
    "bt_chatbot_bp",
    "log_chatbot_bp",
]
