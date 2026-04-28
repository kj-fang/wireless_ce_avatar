from __future__ import absolute_import

# flake8: noqa

# import apis into api package
from snowflake.core.procedure._generated.api.procedure_api import ProcedureApi

__all__ = [
    "ProcedureApi",
]
