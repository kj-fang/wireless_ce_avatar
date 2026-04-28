import logging
import typing

from typing import TYPE_CHECKING, Optional, Union

from pydantic import StrictBytes, StrictStr

from snowflake.connector import SnowflakeConnection
from snowflake.core import PollingOperation, RESTRoot
from snowflake.core.exceptions import UnexpectedResponseError

from .._internal.root_configuration import RootConfiguration
from .._rest_connection import RESTConnection
from ._generated.api import SparkConnectApi
from ._generated.api_client import StoredProcApiClient


if TYPE_CHECKING:
    from snowflake.snowpark import Session

logger = logging.getLogger(__name__)




class SparkConnectResource:
    """Represents a Spark Connect resource on GS.

    Note that there is no corresponding SparkConnectCollection class unlike other Snowflake objects e.g. Schema,
    Table etc. Spark Connect Resource does not represent a Snowflake object with metadata, so it doesn't need a
    "Collection" class to fetch or desc it. It is just a REST endpoint used by the Spark Connect gRPC service.

    Unlike other Snowflake Collection and Resource classes, SparkConnectResource is NOT instantiated by passing in a
    "Root" instance, instead it is instantiated by passing in an instance of a Session, SnowflakeConnection or
    RESTConnection. This makes it easy for the Spark Connect gRPC service to use it as a client as Spark Connect gRPC
    service is a multi-tenant service that receives session token with each gRPC call.

    Each method in the SparkConnectResource class maps one to one with a corresponding gRPC method in the Spark Connect
    gRPC service.
    """

    def __init__(self,
                 connection: Union[RESTConnection, SnowflakeConnection, "Session"],
                 root_config: Optional[RootConfiguration] = None) -> None:
        self._root = RESTRoot(connection, root_config)
        self._api = SparkConnectApi(
            root=self._root, resource_class=SparkConnectResource, sproc_client=StoredProcApiClient(root=self._root)
        )
        self._root.set_configuration_host_if_rest(self._api.api_client)

    @property
    def root(self) -> "RESTRoot":
        return self._root

    def _assert_protobuf_response(self, response: typing.Any) -> bytearray:
        if not isinstance(response, bytearray):
            raise UnexpectedResponseError(self.root, response)
        return response

    def execute_plan(self, request: Union[StrictBytes, StrictStr]) -> bytearray:
        response = self._api.execute_plan(body=request, async_req=False)
        return self._assert_protobuf_response(response)

    def execute_plan_async(self, request: Union[StrictBytes, StrictStr]) -> PollingOperation[bytearray]:
        """An asynchronous version of :func:`execute_plan`.

        Refer to :class:`~snowflake.core.PollingOperation` for more information on asynchronous execution and
        the return type.
        """ # noqa: D401
        future = self._api.execute_plan(body=request, async_req=True)
        return PollingOperation(future, lambda response: self._assert_protobuf_response(response))

    def analyze_plan(self, request: Union[StrictBytes, StrictStr]) -> bytearray:
        response = self._api.analyze_plan(body=request, async_req=False)
        return self._assert_protobuf_response(response)

    def analyze_plan_async(self, request: Union[StrictBytes, StrictStr]) -> PollingOperation[bytearray]:
        """An asynchronous version of :func:`analyze_plan`.

        Refer to :class:`~snowflake.core.PollingOperation` for more information on asynchronous execution and
        the return type.
        """ # noqa: D401
        future = self._api.analyze_plan(body=request, async_req=True)
        return PollingOperation(future, lambda response: self._assert_protobuf_response(response))

    def config(self, request: Union[StrictBytes, StrictStr]) -> bytearray:
        response = self._api.config(body=request, async_req=False)
        return self._assert_protobuf_response(response)

    def config_async(self, request: Union[StrictBytes, StrictStr]) -> PollingOperation[bytearray]:
        """An asynchronous version of :func:`config`.

        Refer to :class:`~snowflake.core.PollingOperation` for more information on asynchronous execution and
        the return type.
        """ # noqa: D401
        future = self._api.config(body=request, async_req=True)
        return PollingOperation(future, lambda response: self._assert_protobuf_response(response))

    def add_artifacts(self, request: Union[StrictBytes, StrictStr]) -> bytearray:
        response = self._api.add_artifacts(body=request, async_req=False)
        return self._assert_protobuf_response(response)

    def add_artifacts_async(self, request: Union[StrictBytes, StrictStr]) -> PollingOperation[bytearray]:
        """An asynchronous version of :func:`add_artifacts`.

        Refer to :class:`~snowflake.core.PollingOperation` for more information on asynchronous execution and
        the return type.
        """ # noqa: D401
        future = self._api.add_artifacts(body=request, async_req=True)
        return PollingOperation(future, lambda response: self._assert_protobuf_response(response))

    def artifact_status(self, request: Union[StrictBytes, StrictStr]) -> bytearray:
        response = self._api.artifact_status(body=request, async_req=False)
        return self._assert_protobuf_response(response)

    def artifact_status_async(self, request: Union[StrictBytes, StrictStr]) -> PollingOperation[bytearray]:
        """An asynchronous version of :func:`artifact_status`.

        Refer to :class:`~snowflake.core.PollingOperation` for more information on asynchronous execution and
        the return type.
        """ # noqa: D401
        future = self._api.artifact_status(body=request, async_req=True)
        return PollingOperation(future, lambda response: self._assert_protobuf_response(response))

    def interrupt(self, request: Union[StrictBytes, StrictStr]) -> bytearray:
        response = self._api.interrupt(body=request, async_req=False)
        return self._assert_protobuf_response(response)

    def interrupt_async(self, request: Union[StrictBytes, StrictStr]) -> PollingOperation[bytearray]:
        """An asynchronous version of :func:`interrupt`.

        Refer to :class:`~snowflake.core.PollingOperation` for more information on asynchronous execution and
        the return type.
        """ # noqa: D401
        future = self._api.interrupt(body=request, async_req=True)
        return PollingOperation(future, lambda response: self._assert_protobuf_response(response))

    def reattach_execute(self, request: Union[StrictBytes, StrictStr]) -> bytearray:
        response = self._api.reattach_execute(body=request, async_req=False)
        return self._assert_protobuf_response(response)

    def reattach_execute_async(self, request: Union[StrictBytes, StrictStr]) -> PollingOperation[bytearray]:
        """An asynchronous version of :func:`reattach_execute`.

        Refer to :class:`~snowflake.core.PollingOperation` for more information on asynchronous execution and
        the return type.
        """ # noqa: D401
        future = self._api.reattach_execute(body=request, async_req=True)
        return PollingOperation(future, lambda response: self._assert_protobuf_response(response))

    def release_execute(self, request: Union[StrictBytes, StrictStr]) -> bytearray:
        response = self._api.release_execute(body=request, async_req=False)
        return self._assert_protobuf_response(response)

    def release_execute_async(self, request: Union[StrictBytes, StrictStr]) -> PollingOperation[bytearray]:
        """An asynchronous version of :func:`release_execute`.

        Refer to :class:`~snowflake.core.PollingOperation` for more information on asynchronous execution and
        the return type.
        """ # noqa: D401
        future = self._api.release_execute(body=request, async_req=True)
        return PollingOperation(future, lambda response: self._assert_protobuf_response(response))
