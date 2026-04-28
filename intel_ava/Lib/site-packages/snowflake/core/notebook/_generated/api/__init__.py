from __future__ import absolute_import

# flake8: noqa

# import apis into api package
from snowflake.core.notebook._generated.api.notebook_api import NotebookApi

__all__ = [
    "NotebookApi",
]
