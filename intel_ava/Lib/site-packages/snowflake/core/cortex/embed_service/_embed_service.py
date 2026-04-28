from typing import TYPE_CHECKING

from snowflake.core import PollingOperation
from snowflake.core._internal.telemetry import api_telemetry
from snowflake.core._operation import PollingOperations
from snowflake.core.cortex.embed_service._generated.api import CortexEmbedApi
from snowflake.core.cortex.embed_service._generated.api_client import (
    StoredProcApiClient,
)
from snowflake.core.cortex.embed_service._generated.models import (
    EmbedRequest,
    EmbedResponse,
)


if TYPE_CHECKING:
    from snowflake.core import Root


class CortexEmbedService:
    """Represents the collection operations of the Snowflake Cortex Embed Service resource."""

    def __init__(self, root: "Root") -> None:
        self._api = CortexEmbedApi(
            root=root,
            resource_class=None,
            sproc_client=StoredProcApiClient(root=root),
        )

    @api_telemetry
    def embed(self, model: str, text: list[str]) -> EmbedResponse:
        """Perform embed service, similar to snowflake.cortex.Embed.

        Parameters
        __________
        embed_request: EmbedRequest
            The embed request object to be sent to the embed service. Defined in ./_generated/models/embed_request.py.
        """
        return self._api.embed(
            EmbedRequest.from_dict({"model": model, "text": text}), async_req=False
        )

    @api_telemetry
    def embed_async(self, model: str, text: list[str]) -> PollingOperation[EmbedResponse]:
        """An asynchronous version of :func:`embed`.

        Refer to :class:`~snowflake.core.PollingOperation` for more information on asynchronous execution and
        the return type.
        """ # noqa: D401
        future = self._api.embed(
            EmbedRequest.from_dict({"model": model, "text": text}), async_req=True
        )
        return PollingOperations.identity(future)
