"""Manages Snowflake EventTables."""

from ..event_table._generated.models import EventTable, EventTableColumn
from ._event_table import EventTableCollection, EventTableResource


__all__ = [
    "EventTable",
    "EventTableResource",
    "EventTableCollection",
    "EventTableColumn",
]
