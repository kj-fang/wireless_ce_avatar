from typing import TYPE_CHECKING

from snowflake.core import PollingOperation
from snowflake.core._internal.telemetry import api_telemetry
from snowflake.core._internal.utils import deprecated
from snowflake.core.cortex.lite_agent_service._generated.api import CortexLiteAgentApi
from snowflake.core.cortex.lite_agent_service._generated.api_client import (
    StoredProcApiClient,
)
from snowflake.core.cortex.lite_agent_service._generated.models import (
    AgentRunRequest,
)
from snowflake.core.rest import SSEClient


if TYPE_CHECKING:
    from snowflake.core import Root


class CortexAgentService:
    """Represents the operations of the Snowflake Cortex Lite Agent Service resource."""

    def __init__(self, root: "Root") -> None:
        self._api = CortexLiteAgentApi(
            root=root,
            resource_class=None,
            sproc_client=StoredProcApiClient(root=root),
        )

    @api_telemetry
    def run(self, agent_run_request: AgentRunRequest) -> SSEClient:
        """Perform agent service.

        Parameters
        __________
        agent_request: AgentRequest
            The agent request object to be sent to the agent service.
            Defined in ./_generated/models/agent_run_request.py
        """
        return SSEClient(self._api.agent_run(
            agent_run_request,
            async_req=False,
            _preload_content=False,
        ))

    @api_telemetry
    def run_async(self, agent_run_request: AgentRunRequest) -> PollingOperation[SSEClient]:
        """An asynchronous version of :func:`Run`.

        Refer to :class:`~snowflake.core.PollingOperation` for more information on asynchronous execution and
        the return type.
        """ # noqa: D401
        future = self._api.agent_run(
            agent_run_request,
            async_req=True,
            _preload_content=False,
        )
        return PollingOperation(future, lambda x: SSEClient(x))

    @api_telemetry
    @deprecated("run")
    def Run(self, agent_run_request: AgentRunRequest) -> SSEClient:
        return self.run(agent_run_request=agent_run_request)
