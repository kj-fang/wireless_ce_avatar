from enum import Enum
from typing import Any, Optional


BRIDGE_OVERRIDE_PARAMETER_PREFIX = 'ENABLE_SNOW_API_FOR_'


class SnowApiParameter(Enum):
    USE_CLIENT_RETRY = "USE_CLIENT_RETRY"
    PRINT_VERBOSE_STACK_TRACE = "PRINT_VERBOSE_STACK_TRACE"
    FIX_HOSTNAME = "FIX_HOSTNAME"
    MAX_THREADS = "MAX_THREADS"


class SnowApiParameters:
    """Wrapper that abstracts away the behavior from the parsing/reading of parameters.

    Args:
        params_map: A ``dict[str,str]`` of parameter names to their values
    """

    def __init__(self, params_map: dict[SnowApiParameter, Any]) -> None:
        self.params_map = params_map

    def is_parameter_true(self, param_name: SnowApiParameter, default: str) -> bool:
        return ((self.params_map.get(param_name, default) or default)
                .lower()
                .strip()
                in (
                    "true",
                    "t",
                    "yes",
                    "y",
                    "on",
                )
            )

    @property
    def should_retry_request(self) -> bool:
        return self.is_parameter_true(SnowApiParameter.USE_CLIENT_RETRY, 'true')

    @property
    def should_print_verbose_stack_trace(self) -> bool:
        return self.is_parameter_true(SnowApiParameter.PRINT_VERBOSE_STACK_TRACE, 'true')

    @property
    def fix_hostname(self) -> bool:
        return self.is_parameter_true(SnowApiParameter.FIX_HOSTNAME, 'true')

    @property
    def max_threads(self) -> Optional[int]:
        param = self.params_map.get(SnowApiParameter.MAX_THREADS)
        if param is None:
            return None
        return int(param)
