from typing import TYPE_CHECKING

from snowflake.core import PollingOperation
from snowflake.core._internal.telemetry import api_telemetry
from snowflake.core.cortex.chat_service._generated.api import CortexChatApi
from snowflake.core.cortex.chat_service._generated.api_client import (
    StoredProcApiClient,
)
from snowflake.core.cortex.chat_service._generated.models import (
    ChatRequest,
)
from snowflake.core.rest import SSEClient


if TYPE_CHECKING:
    from snowflake.core import Root


class CortexChatService:
    """Represents the collection operations of the Snowflake Cortex Chat Service resource."""

    def __init__(self, root: "Root") -> None:
        self._api = CortexChatApi(
            root=root,
            resource_class=None,
            sproc_client=StoredProcApiClient(root=root),
        )

    @api_telemetry
    def chat(self, chat_request: ChatRequest) -> SSEClient:
        """Perform chat service, similar to snowflake.cortex.Chat.

        Parameters
        __________
        chat_request: ChatRequest
            The chat request object to be sent to the chat service. Defined in ./_generated/models/chat_request.py
        """
        return SSEClient(self._api.chat_request(
            chat_request,
            async_req=False,
            _preload_content=False,
        ))

    @api_telemetry
    def chat_async(self, chat_request: ChatRequest) -> PollingOperation[SSEClient]:
        """An asynchronous version of :func:`chat`.

        Refer to :class:`~snowflake.core.PollingOperation` for more information on asynchronous execution and
        the return type.
        """ # noqa: D401
        future = self._api.chat_request(
            chat_request,
            async_req=True,
            _preload_content=False,
        )
        return PollingOperation(future, lambda x: SSEClient(x))
