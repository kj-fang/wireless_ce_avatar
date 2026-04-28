from __future__ import absolute_import

# flake8: noqa

# import apis into api package
from snowflake.core.cortex.inference_service._generated.api.cortex_inference_api import CortexInferenceApi

__all__ = [
    "CortexInferenceApi",
]
