from typing import TYPE_CHECKING

from snowflake.core import PollingOperation
from snowflake.core._internal.telemetry import api_telemetry
from snowflake.core.cortex.inference_service._generated.api import CortexInferenceApi
from snowflake.core.cortex.inference_service._generated.api_client import (
    StoredProcApiClient,
)
from snowflake.core.cortex.inference_service._generated.models import (
    CompleteRequest,
)
from snowflake.core.rest import SSEClient


if TYPE_CHECKING:
    from snowflake.core import Root


class CortexInferenceService:
    """Represents the Snowflake Cortex Inference Service resource."""

    def __init__(self, root: "Root") -> None:
        self._api = CortexInferenceApi(
            root=root,
            resource_class=None,
            sproc_client=StoredProcApiClient(root=root),
        )

    @api_telemetry
    def complete(self, complete_request: CompleteRequest) -> SSEClient:
        """Perform LLM text completion inference, similar to snowflake.cortex.Complete.

        Parameters
        __________
        complete_request: CompleteRequest
            LLM text completion request.
        """
        return SSEClient(self._api.cortex_llm_inference_complete(
            complete_request,
            async_req=False,
            _preload_content=False,
        ))

    @api_telemetry
    def complete_async(self, complete_request: CompleteRequest) -> PollingOperation[SSEClient]:
        """An asynchronous version of :func:`complete`.

        Refer to :class:`~snowflake.core.PollingOperation` for more information on asynchronous execution and
        the return type.
        """ # noqa: D401
        future = self._api.cortex_llm_inference_complete(
            complete_request,
            async_req=True,
            _preload_content=False,
        )
        return PollingOperation(future, lambda x: SSEClient(x))
