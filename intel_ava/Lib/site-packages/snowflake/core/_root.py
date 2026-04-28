import json
import logging
import os
import warnings

from json import JSONDecodeError
from typing import TYPE_CHECKING, Optional, Union

from snowflake.connector import Error, SnowflakeConnection, cursor
from snowflake.core._options import require_snowpark

from ._common import TokenType
from ._constants import (
    PLATFORM,
    PYTHON_VERSION,
)
from ._internal.client_info import ClientInfo
from ._internal.root_configuration import RootConfiguration
from ._internal.snowapi_parameters import SnowApiParameter, SnowApiParameters
from ._internal.telemetry import ApiTelemetryClient
from ._utils import fix_hostname as _fix_hostname
from .account import AccountCollection
from .api_integration import ApiIntegrationCollection
from .catalog_integration import CatalogIntegrationCollection
from .compute_pool import ComputePoolCollection
from .cortex.chat_service import CortexChatService
from .cortex.embed_service import CortexEmbedService
from .cortex.inference_service import CortexInferenceService
from .cortex.lite_agent_service import CortexAgentService
from .database import DatabaseCollection
from .external_volume import ExternalVolumeCollection
from .grant._grants import Grants
from .managed_account import ManagedAccountCollection
from .network_policy import NetworkPolicyCollection
from .notification_integration import NotificationIntegrationCollection
from .role import RoleCollection
from .session import SnowAPISession
from .user import UserCollection
from .version import __version__
from .warehouse import WarehouseCollection


if TYPE_CHECKING:
    from snowflake.snowpark import Session

logger = logging.getLogger(__name__)


class Root:
    """The entry point of the Snowflake Core Python APIs that manage the Snowflake objects.

    Parameters
    __________
    connection: Union[SnowflakeConnection, Session]
        A ``SnowflakeConnection`` or Snowpark ``Session`` instance.

    Examples
    ________
    Creating a ``Root`` instance:

    >>> from snowflake.connector import connect
    >>> from snowflake.core import Root
    >>> from snowflake.snowpark import Session
    >>> CONNECTION_PARAMETERS = {
    ...    "account": os.environ["snowflake_account_demo"],
    ...    "user": os.environ["snowflake_user_demo"],
    ...    "password": os.environ["snowflake_password_demo"],
    ...    "database": test_database,
    ...    "warehouse": test_warehouse,
    ...    "schema": test_schema,
    ... }
    >>> # create from a Snowflake Connection
    >>> connection = connect(**CONNECTION_PARAMETERS)
    >>> root = Root(connection)
    >>> # or create from a Snowpark Session
    >>> session = Session.builder.config(CONNECTION_PARAMETERS).create()
    >>> root = Root(session)

    Using the ``Root`` instance to access resource management APIs:

    >>> tasks = root.databases["mydb"].schemas["myschema"].tasks
    >>> mytask = tasks["mytask"]
    >>> mytask.resume()
    >>> compute_pools = root.compute_pools
    >>> my_computepool = compute_pools["mycomputepool"]
    >>> my_computepool.delete()
    """

    def __init__(
        self,
        connection: Union[SnowflakeConnection, "Session"],
        root_config: Optional[RootConfiguration] = None,
    ) -> None:
        self._session: Optional[Session] = None
        if isinstance(connection, SnowflakeConnection):
            self._connection = connection
        else:
            self._session = connection
            self._connection = connection._conn._conn

        logger.info("New root object was created for %r", connection)

        self._refresh_parameters()
        # In stored procedures connection.rest doesn't have _host. Let's set the base-case here.
        self._hostname = None
        if self._connection.rest is not None and hasattr(self._connection.rest, "_host"):
            self._hostname = self._connection.rest._host
            if self._parameters.fix_hostname:
                # Fix hostname, if desired. This is done by replacing _ (underscores) with - (dashes)
                #  in the first part of the hostname.
                self._hostname = _fix_hostname(self._hostname)
        self._snowapi_session = SnowAPISession(self)
        self._initialize_client_info()

        self._databases = DatabaseCollection(self)
        self._accounts = AccountCollection(self)
        self._managed_accounts = ManagedAccountCollection(self)
        self._compute_pools = ComputePoolCollection(self)
        self._external_volumes = ExternalVolumeCollection(self)
        self._telemetry_client = ApiTelemetryClient(self._connection)
        self._warehouses = WarehouseCollection(self)
        self._network_policies = NetworkPolicyCollection(self)
        self._roles = RoleCollection(self)
        self._grants = Grants(self)
        self._users = UserCollection(self)
        self._catalog_integrations = CatalogIntegrationCollection(self)
        self._notification_integrations = NotificationIntegrationCollection(self)
        self._api_integrations = ApiIntegrationCollection(self)

        self._cortex_chat_service = CortexChatService(self)
        self._cortex_inference_service = CortexInferenceService(self)
        self._cortex_embed_service = CortexEmbedService(self)
        self._cortex_agent_service = CortexAgentService(self)

        logger.info("Snowflake Core version: %s, on Python %s, on platform: %s", __version__, PYTHON_VERSION, PLATFORM)
        parameter_map = self._parameters.params_map
        for parameter in parameter_map:
            logger.info("Parameter %s: %s", parameter, parameter_map.get(parameter))

        self._root_config = root_config or RootConfiguration()

    def parameters(self, refresh: bool = False) -> SnowApiParameters:
        if refresh:
            self._refresh_parameters()

        return self._parameters

    @property
    def root_config(self) -> RootConfiguration:
        """Return the root configuration object."""
        return self._root_config

    @property
    def connection(self) -> SnowflakeConnection:
        """Return the connection in use.

        This is the connection used to create this ``Root`` instance, or the
        Snowpark session's connection if this root is created from a
        session.
        """
        return self._connection

    @property
    def session(self) -> "Session":
        """Returns the session that is used to create this ``Root`` instance."""
        require_snowpark()
        if self._session is None:
            from snowflake.snowpark.session import Session, _active_sessions
            self._session = Session.builder.configs(
                {"connection": self._connection}
            ).create()
            _active_sessions.remove(
                self._session
            )  # This is supposed to avoid a user double using sessions
        return self._session

    @property
    def databases(self) -> DatabaseCollection:
        """Returns the ``DatabaseCollection`` that represents the visible databases.

        Examples
        ________

        Getting a specific database resource:

        >>> root = Root(session)
        >>> my_db = root.databases["my_db"]
        """
        return self._databases

    @property
    def accounts(self) -> AccountCollection:
        """Returns the ``AccountCollection`` that represents the visible accounts.

        Examples
        ________

        Getting a specific account resource:

        >>> root = Root(session)
        >>> my_account = root.accounts["my_account"]
        """
        return self._accounts

    @property
    def api_integrations(self) -> ApiIntegrationCollection:
        """Returns the ``ApiIntegrationCollection`` that represents the visible API integrations.

        Examples
        ________

        Getting a specific API integration resource:

        >>> root = Root(session)
        >>> my_api_int = root.api_integrations["my_api_int"]
        """
        return self._api_integrations

    @property
    def cortex_inference_service(self) -> CortexInferenceService:
        """Returns the ``CortexInferenceService`` that represents the cortex inference service.

        Examples
        ________

        Getting the cortex inference service resource:

        >>> root = Root(session)
        >>> my_cortex_inference_service = root.cortex_inference_service
        """
        return self._cortex_inference_service

    @property
    def cortex_chat_service(self) -> CortexChatService:
        """
        Returns the CortexChatService that represents the cortex chat service.

        Examples
        ________
        To get the cortex chat service resource, you can do the following:

        >>> root = Root(session)
        >>> my_cortex_chat_service = root.cortex_chat_service
        """
        return self._cortex_chat_service

    @property
    def cortex_embed_service(self) -> CortexEmbedService:
        """
        Returns the CortexEmbedService that represents the cortex embed service.

        Examples
        ________
        To get the cortex embed service resource, you can do the following:

        >>> root = Root(session)
        >>> my_cortex_embed_service = root.cortex_embed_service
        """
        return self._cortex_embed_service

    @property
    def cortex_agent_service(self) -> CortexAgentService:
        """
        Returns the CortexAgentService that represents the cortex lite Agent service.

        Examples
        ________
        To get the cortex lite Agent service resource, you can do the following:

        >>> root = Root(session)
        >>> my_cortex_agent_service = root.cortex_agent_service
        """
        return self._cortex_agent_service

    @property
    def managed_accounts(self) -> ManagedAccountCollection:
        """Returns the ``ManagedAccountCollection`` that represents the visible accounts.

        Examples
        ________

        Getting a specific managed account resource:

        >>> root = Root(session)
        >>> my_managed_account = root.managed_accounts["my_managed_account"]
        """
        return self._managed_accounts

    @property
    def compute_pools(self) -> ComputePoolCollection:
        """Returns the ``ComputePoolCollection`` that represents the visible compute pools.

        Examples
        ________

        Getting a specific compute pool resource:

        >>> root = Root(session)
        >>> my_cp = root.compute_pools["my_cp"]
        """
        return self._compute_pools

    @property
    def external_volumes(self) -> ExternalVolumeCollection:
        """Returns the ``ExternalVolumeCollection`` that represents the visible external volumes.

        Examples
        ________

        Getting a specific external volume resource:

        >>> root = Root(session)
        >>> my_external_volume = root.external_volumes["my_external_volume"]
        """
        return self._external_volumes

    @property
    def network_policies(self) -> NetworkPolicyCollection:
        """Returns the ``NetworkPolicyCollection`` that represents the visible network policies.

        Examples
        ________

        Getting a specific network policy resource:

        >>> root = Root(session)
        >>> my_network_policy = root.network_policies["my_network_policy"]
        """
        return self._network_policies

    @property
    def notification_integrations(self) -> NotificationIntegrationCollection:
        """Returns the ``NotificationIntegrationCollection`` that represents the visible notification integrations.

        Examples
        ________

        Listing all available Notification Integrations:

        >>> root = Root(session)
        >>> my_nis = list(root.notification_integrations.iter())
        """
        return self._notification_integrations

    @property
    def warehouses(self) -> WarehouseCollection:
        """Returns the ``WarehouseCollection`` that represents the visible warehouses.

        Examples
        ________

        Getting a specific warehouse resource:

        >>> root = Root(session)
        >>> my_wh = root.warehouses["my_wh"]
        """
        return self._warehouses

    @property
    def roles(self) -> RoleCollection:
        """Returns the ``RoleCollection`` that represents the visible roles.

        Examples
        ________

        Getting a specific role resource:

        >>> root = Root(session)
        >>> my_role = root.roles["my_role"]
        """
        return self._roles

    @property
    def grants(self) -> Grants:
        """Returns the visible Grants in Snowflake.

        Examples
        ________

        Using the ``Grants`` object to grant a privilege to a role:

        >>> grants.grant(
        ...    Grant(
        ...        grantee=Grantees.role(name="public"),
        ...        securable=Securables.database("invaliddb123"),
        ...        privileges=[Privileges.create_database],
        ...        grant_option=False,
        ...    )
        ... )
        """
        return self._grants

    @property
    def users(self) -> UserCollection:
        """Returns the ``UserCollection`` that represents the visible users.

        Examples
        ________

        Getting a specific user resource:

        >>> root = Root(session)
        >>> my_user = root.users["my_user"]
        """
        return self._users

    @property
    def catalog_integrations(self) -> CatalogIntegrationCollection:
        """Returns the ``CatalogIntegrationCollection`` that represents the visible catalog integrations.

        Examples
        ________

        Getting a specific catalog integration resource:

        >>> root = Root(session)
        >>> my_catalog_integration = root.catalog_integrations["my_catalog_integration"]
        """
        return self._catalog_integrations

    @property
    def _session_token(self) -> Optional[str]:
        # TODO: this needs to be fixed in the connector
        return self._connection.rest.token  # type: ignore[union-attr]

    @property
    def _master_token(self) -> Optional[str]:
        # TODO: this needs to be fixed in the connector
        return self._connection.rest.master_token  # type: ignore[union-attr]

    @property
    def external_session_id(self) -> Optional[str]:
        return getattr(self._connection.rest, "_external_session_id", None)

    @property
    def token_type(self) -> TokenType:
        if getattr(self._connection, "_authenticator", None) == "PAT_WITH_EXTERNAL_SESSION":
            return TokenType.EXTERNAL_SESSION_WITH_PAT
        return TokenType.SESSION_TOKEN

    def _refresh_parameters(self) -> None:
        parameters = {
            SnowApiParameter.USE_CLIENT_RETRY: os.getenv("_SNOWFLAKE_ENABLE_RETRY_REQUEST_QUERY"),
            SnowApiParameter.PRINT_VERBOSE_STACK_TRACE: os.getenv("_SNOWFLAKE_PRINT_VERBOSE_STACK_TRACE"),
            SnowApiParameter.FIX_HOSTNAME: os.getenv("_SNOWFLAKE_FIX_HOSTNAME", "true"),
            SnowApiParameter.MAX_THREADS: os.getenv("_SNOWFLAKE_MAX_THREADS"),
        }
        self._parameters = SnowApiParameters(parameters)

    def _query_for_client_info(self) -> Optional[list[dict[str, str]]]:
        logger.info("Querying the server for client info")
        ret = None
        query_res = self.connection.cursor().execute("SELECT SYSTEM$CLIENT_VERSION_INFO()")
        if query_res is not None and type(query_res) is cursor.SnowflakeCursor:
            result = query_res.fetchone()
            if isinstance(result, tuple):
                ret = json.loads(result[0])

        return ret

    def _initialize_client_info(self) -> None:
        client_support_info = None

        try:
            client_info_from_query = self._query_for_client_info()

            if client_info_from_query is not None:
                # Find the entry that corresponds to PyCore. If not found, then we assume all versions
                # are supported; this will be None and ClientInfo will be instantiated with None
                # as the value for the support info.
                client_support_info = next((client for client in client_info_from_query if
                                            client.get("clientId") == "PyCore"), None)
        except (Error, JSONDecodeError) as error:
            warning = "Could not fetch client version info from Snowflake. Please make sure your client is up to date."
            logger.warning(warning, exc_info=error)
            warnings.warn(warning, stacklevel=3)

        client_info = ClientInfo(client_support_info)

        if not client_info.version_is_supported():
            # Later on, we may make this more aggressive and terminate with an error.
            warning = (f"Your current version of the client, {client_info.client_version}, "
                       f"is not supported. The minimum supported version is {client_info.minimum_supported_version}.")

            if client_info.recommended_version:
                warning = warning + f" The recommended version is {client_info.recommended_version}."

            logger.warning(warning)
            warnings.warn(warning, stacklevel=3)

        elif client_info.version_is_nearing_end_of_support():
            warning = (
                f"Your current version of the client, {client_info.client_version}, is nearing the end of support.")

            if client_info.recommended_version:
                warning = warning + f" The recommended version is {client_info.recommended_version}."

            logger.warning(warning)
            warnings.warn(warning, stacklevel=3)

        self._client_info = client_info
