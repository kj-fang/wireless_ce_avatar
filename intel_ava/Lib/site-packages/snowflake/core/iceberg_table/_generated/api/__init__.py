from __future__ import absolute_import

# flake8: noqa

# import apis into api package
from snowflake.core.iceberg_table._generated.api.iceberg_table_api import IcebergTableApi

__all__ = [
    "IcebergTableApi",
]
