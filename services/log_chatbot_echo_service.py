"""Compatibility import for the shared chatbot Echo service."""

import sys

from services.chatbot import echo

sys.modules[__name__] = echo