from __future__ import absolute_import

# flake8: noqa

# import apis into api package
from snowflake.core.cortex.chat_service._generated.api.cortex_chat_api import CortexChatApi

__all__ = [
    "CortexChatApi",
]
