from collections.abc import Iterator
from typing import TYPE_CHECKING, Optional

from pydantic import StrictStr

from snowflake.core import PollingOperation
from snowflake.core._common import (
    AccountObjectCollectionParent,
    CreateMode,
    ObjectReferenceMixin,
)
from snowflake.core._operation import PollingOperations

from .._internal.telemetry import api_telemetry
from ._generated.api import ApiIntegrationApi
from ._generated.api_client import StoredProcApiClient
from ._generated.models.api_integration import ApiIntegration


if TYPE_CHECKING:
    from snowflake.core import Root


class ApiIntegrationCollection(AccountObjectCollectionParent["ApiIntegrationResource"]):
    """Represents the collection operations on the Snowflake api integration resource.

    With this collection, you can create, iterate through, and search for api integration that you
    have access to in the current context.

    Examples
    ________
    Creating an ApiIntegration instance using AWS API Gateway:

    >>> api_integrations = root.api_integrations
    >>> new_api_integration = ApiIntegration(
    ...     name="name",
    ...     api_hook=AwsHook(
    ...         api_provider="AWS_API_GATEWAY",
    ...         api_aws_role_arn="your_arn",
    ...         api_key=os.environ.get("YOUR_API_KEY"),
    ...     ),
    ...     api_allowed_prefixes=["https://snowflake.com"],
    ...     enabled=True,
    ... )
    >>> api_integrations.create(new_api_integration)
    """

    def __init__(self, root: "Root"):
        super().__init__(root, ApiIntegrationResource)
        self._api = ApiIntegrationApi(
            root=self.root,
            resource_class=self._ref_class,
            sproc_client=StoredProcApiClient(root=self.root)
        )

    @api_telemetry
    def create(
        self,
        api_integration: ApiIntegration,
        *,
        mode: CreateMode = CreateMode.error_if_exists,
    ) -> "ApiIntegrationResource":
        """Create an API integration in Snowflake.

        Parameters
        __________
        api_integration: ApiIntegration
            The ``ApiIntegration`` object.
        mode: CreateMode, optional
            One of the following enum values:

            ``CreateMode.error_if_exists``: Throw an :class:`snowflake.core.exceptions.ConflictError`
            if the api integration already exists in Snowflake.  Equivalent to
            SQL ``create api integration <name> ...``.

            ``CreateMode.or_replace``: Replace if the api integration already exists in Snowflake. Equivalent to SQL
            ``create or replace api integration <name> ...``.

            ``CreateMode.if_not_exists``: Do nothing if the api integration already exists in Snowflake.
            Equivalent to SQL ``create api integration <name> if not exists...``

            Default is ``CreateMode.error_if_exists``.

        Examples
        ________
        Creating an API integration, replacing any existing api integration with the same name:

        >>> api_integrations = root.api_integrations
        >>> new_api_integration = ApiIntegration(
        ...     name="name",
        ...     api_hook=AwsHook(
        ...         api_provider="AWS_API_GATEWAY",
        ...         api_aws_role_arn="your_arn",
        ...         api_key=os.environ.get("YOUR_API_KEY"),
        ...     ),
        ...     api_allowed_prefixes=["https://snowflake.com"],
        ...     enabled=True,
        ... )
        >>> api_integrations.create(new_api_integration, mode=CreateMode.or_replace)
        """
        real_mode = CreateMode[mode].value

        if not isinstance(api_integration, ApiIntegration):
            raise TypeError("api_integration has to be ApiIntegration object")

        self._api.create_api_integration(
            api_integration,
            create_mode=StrictStr(real_mode),
            async_req=False,
        )

        return ApiIntegrationResource(api_integration.name, self)

    @api_telemetry
    def create_async(
        self,
        api_integration: ApiIntegration,
        *,
        mode: CreateMode = CreateMode.error_if_exists,
    ) -> PollingOperation["ApiIntegrationResource"]:
        """An asynchronous version of :func:`create`.

        Refer to :class:`~snowflake.core.PollingOperation` for more information on asynchronous execution and
        the return type.
        """ # noqa: D401
        real_mode = CreateMode[mode].value

        if not isinstance(api_integration, ApiIntegration):
            raise TypeError("api_integration has to be ApiIntegration object")

        future = self._api.create_api_integration(
            api_integration,
            create_mode=StrictStr(real_mode),
            async_req=True,
        )

        return PollingOperation(future, lambda _: self[api_integration.name])

    @api_telemetry
    def iter(
        self,
        *,
        like: Optional[StrictStr] = None,
    ) -> Iterator[ApiIntegration]:
        """Iterate through ``ApiIntegration`` objects from Snowflake, filtering on any optional 'like' pattern.

        Parameters
        __________
        like: str, optional
            A case-insensitive string functioning as a filter, with support for SQL
            wildcard characters (% and _).

        Examples
        ________
        Showing all api integrations that you have access to see:

        >>> api_integrations = root.api_integrations.iter()

        Showing information of the exact api integration you want to see:

        >>> api_integrations = root.api_integrations.iter(like="your-api-integration-name")

        Showing api integrations starting with 'your-api-integration-name-':

        >>> api_integrations = root.api_integrations.iter(like="your-api-integration-name-%")

        Using a for loop to retrieve information from iterator:

        >>> for api_integration in api_integrations:
        ...     print(api_integration.name)
        """
        api_integrations = self._api.list_api_integrations(
            like=like,
            async_req=False,
        )
        return iter(api_integrations)

    @api_telemetry
    def iter_async(
        self,
        like: Optional[StrictStr] = None,
    ) -> PollingOperation[Iterator[ApiIntegration]]:
        """An asynchronous version of :func:`iter`.

        Refer to :class:`~snowflake.core.PollingOperation` for more information on asynchronous execution and
        the return type.
        """ # noqa: D401
        future = self._api.list_api_integrations(
            like=like,
            async_req=True,
        )
        return PollingOperations.iterator(future)


class ApiIntegrationResource(ObjectReferenceMixin[ApiIntegrationCollection]):
    """Represents a reference to a Snowflake api integration.

    With this api integration reference, you can create, update, delete and fetch information about
    api integrations, as well as perform certain actions on them.
    """

    def __init__(
        self,
        name: StrictStr,
        collection: ApiIntegrationCollection
    ) -> None:
        self.name = name
        self.collection = collection

    @api_telemetry
    def fetch(self) -> ApiIntegration:
        """Fetch the details of an API integration.

        Examples
        ________
        Fetching a reference to an API integration to print its name:

        >>> api_integration_reference = root.api_integrations["foo"]
        >>> my_api_integration = api_integration_reference.fetch()
        >>> print(my_api_integration.name)
        """
        return self.collection._api.fetch_api_integration(
            self.name,
            async_req=False,
        )

    @api_telemetry
    def fetch_async(self) -> PollingOperation[ApiIntegration]:
        """An asynchronous version of :func:`fetch`.

        Refer to :class:`~snowflake.core.PollingOperation` for more information on asynchronous execution and
        the return type.
        """ # noqa: D401
        future = self.collection._api.fetch_api_integration(
            self.name,
            async_req=True,
        )
        return PollingOperations.identity(future)

    @api_telemetry
    def create_or_alter(
        self,
        api_integration: ApiIntegration,
    ) -> None:
        """Create or alter an API integration.

        The operation is limited by the fact that api_key will not be updated
        and api_blocked_prefixes cannot be unset.

        Parameters
        __________
        api_integration: ApiIntegration
            The ``ApiIntegration`` object.

        Examples
        ________
        Creating a new API integration:

        >>> root.api_integrations["my_api"].create_or_alter(my_api_def)

        See ``ApiIntegrationCollection.create`` for more examples.
        """
        self.collection._api.create_or_alter_api_integration(
            name=api_integration.name,
            api_integration=api_integration,
            async_req=False

        )

    @api_telemetry
    def create_or_alter_async(
        self,
        api_integration: ApiIntegration,
    ) -> PollingOperation[None]:
        """An asynchronous version of :func:`create_or_alter`.

        Refer to :class:`~snowflake.core.PollingOperation` for more information on asynchronous execution and
        the return type.
        """ # noqa: D401
        future = self.collection._api.create_or_alter_api_integration(
            name=api_integration.name,
            api_integration=api_integration,
            async_req=True
        )
        return PollingOperations.empty(future)

    @api_telemetry
    def drop(
        self,
        if_exists: bool = False,
    ) -> None:
        """Drop this api integration.

        Parameters
        __________
        if_exists: bool, optional
            Whether to error if the api integration doesn't exist. Default is ``False``.

        Examples
        ________
        Deleting an API integration using its reference, error if it doesn't exist:

        >>> api_integration_reference.drop()

        Deleting an API integration using its reference, if it exists:

        >>> api_integration_reference.drop(if_exists=True)
        """
        self.collection._api.delete_api_integration(
            self.name,
            if_exists=if_exists,
            async_req=False,
        )

    @api_telemetry
    def drop_async(
        self,
        if_exists: bool = False,
    ) -> PollingOperation[None]:
        """An asynchronous version of :func:`drop`.

        Refer to :class:`~snowflake.core.PollingOperation` for more information on asynchronous execution and
        the return type.
        """ # noqa: D401
        future = self.collection._api.delete_api_integration(
            self.name,
            if_exists=if_exists,
            async_req=True,
        )
        return PollingOperations.empty(future)
