from __future__ import absolute_import

# flake8: noqa

# import apis into api package
from snowflake.core.managed_account._generated.api.managed_account_api import ManagedAccountApi

__all__ = [
    "ManagedAccountApi",
]
