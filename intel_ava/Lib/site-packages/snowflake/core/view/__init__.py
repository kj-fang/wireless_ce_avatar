"""Manages Snowflake Views."""

from ..view._generated.models import View, ViewColumn
from ._view import ViewCollection, ViewResource


__all__ = [
    "View",
    "ViewResource",
    "ViewCollection",
    "ViewColumn",
]
