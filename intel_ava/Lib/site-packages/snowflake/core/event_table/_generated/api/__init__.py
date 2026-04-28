from __future__ import absolute_import

# flake8: noqa

# import apis into api package
from snowflake.core.event_table._generated.api.event_table_api import EventTableApi

__all__ = [
    "EventTableApi",
]
