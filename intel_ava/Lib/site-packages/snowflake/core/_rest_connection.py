import logging

from typing import TYPE_CHECKING, Any, Optional, Union

from snowflake.connector import SnowflakeConnection

from ._common import TokenType
from ._internal.root_configuration import RootConfiguration
from ._root import Root
from ._utils import fix_hostname as _fix_hostname
from .exceptions import SnowflakePythonError


if TYPE_CHECKING:
    from snowflake.snowpark import Session

logger = logging.getLogger(__name__)


class RESTConnection:
    """Connection to the GS REST endpoint.

    Lightweight alternative to a full-blown SnowflakeConnection for use in multi-tenant services like Spark Connect.
    """

    def __init__(self, host: str,
                 port: int,
                 token_type: TokenType,
                 token: str,
                 external_session_id: Optional[str] = None,
                 protocol: str = 'https') -> None:
        self.host = host
        self.port = port
        self._token_type = token_type
        self._session_token = token
        # Only applicable when token_type is EXTERNAL_SESSION_WITH_PAT. Set to Spark session ID
        self.external_session_id = external_session_id
        self.protocol = protocol

    @property
    def url(self) -> str:
        return f"{self.protocol}://{self.host}:{self.port}"

    @property
    def session_token(self) -> str:
        return self._session_token

    @property
    def token_type(self) -> TokenType:
        return self._token_type


class RESTRoot(Root):
    """Root that can work either with Session/SnowflakeConnection like other resources or with RESTConnection.

    Can only be used for SparkConnectResource instantiation. All other resources must use the normal snowflake.core.Root
    """

    def __init__(
        self,
        connection: Union[RESTConnection, SnowflakeConnection, "Session"],
        root_config: Optional[RootConfiguration] = None,
    ) -> None:
        self._restConnection: Optional[RESTConnection] = None
        if isinstance(connection, RESTConnection):
            # DO NOT call super.__init__() as it expects either SnowflakeConnection or Session
            self._restConnection = connection
            logger.info("New root object was created for %r", connection)
            self._refresh_parameters()
            # In stored procedures connection.rest doesn't have _host. Let's set the base-case here.
            self._hostname = connection.host
            if self._hostname is not None and self._parameters.fix_hostname:
                # Fix hostname, if desired. This is done by replacing _ (underscores) with - (dashes)
                #  in the first part of the hostname.
                self._hostname = _fix_hostname(self._hostname)
            self._root_config = root_config or RootConfiguration()
        else:
            super().__init__(connection, root_config)

    def set_configuration_host_if_rest(self, api_client: Any) -> None:
        if self._restConnection is not None:
            api_client.configuration.host = self._restConnection.url

    @property
    def session(self) -> "Session":
        if self._restConnection is not None:
            raise SnowflakePythonError("REST connection does not have Session instance associated with it")
        return super().session

    @property
    def _session_token(self) -> Optional[str]:
        if self._restConnection is not None:
            return self._restConnection.session_token
        return super()._session_token

    @property
    def _master_token(self) -> Optional[str]:
        if self._restConnection is not None:
            return None
        return super()._master_token

    @property
    def token_type(self) -> TokenType:
        if self._restConnection is not None:
            return self._restConnection.token_type
        return super().token_type

    @property
    def external_session_id(self) -> Optional[str]:
        if self._restConnection is not None:
            return self._restConnection.external_session_id
        return super().external_session_id
