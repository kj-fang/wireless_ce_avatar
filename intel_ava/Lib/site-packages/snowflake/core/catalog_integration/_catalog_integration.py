from collections.abc import Iterator
from typing import TYPE_CHECKING, Optional

from pydantic import StrictStr

from snowflake.core import PollingOperation
from snowflake.core._common import (
    AccountObjectCollectionParent,
    CreateMode,
    ObjectReferenceMixin,
)

from .._internal.telemetry import api_telemetry
from .._operation import PollingOperations
from ._generated.api import CatalogIntegrationApi
from ._generated.api_client import StoredProcApiClient
from ._generated.models.catalog_integration import CatalogIntegration


if TYPE_CHECKING:
    from snowflake.core._root import Root

class CatalogIntegrationCollection(AccountObjectCollectionParent["CatalogIntegrationResource"]):
    """Represents the collection operations on the Snowflake Catalog Integration resource.

    With this collection, you can create, iterate through, and fetch catalog integrations
    that you have access to in the current context.

    Examples
    ________
    Creating a catalog integration instance by object store:

    >>> root.catalog_integrations.create(CatalogIntegration(
    ...     name = 'my_catalog_integration',
    ...     catalog = ObjectStore(),
    ...     table_format = "ICEBERG",
    ...     enabled = True,
    ... ))
    """

    def __init__(self, root: "Root"):
        super().__init__(root, ref_class=CatalogIntegrationResource)
        self._api = CatalogIntegrationApi(
            root = self.root,
            resource_class = self._ref_class,
            sproc_client = StoredProcApiClient(root = self.root)
        )


    @api_telemetry
    def create(
        self,
        catalog_integration: CatalogIntegration,
        mode: CreateMode=CreateMode.error_if_exists,
    ) -> "CatalogIntegrationResource":
        """Create a catalog integration in Snowflake.

        Parameters
        __________
        catalog_integration: CatalogIntegration
            The ``CatalogIntegration`` object, together with ``CatalogIntegration``'s properties:
            name, table_format, enabled, catalog;
            comment, type, category are optional

        mode: CreateMode, optional
            One of the following enum values:

            ``CreateMode.error_if_exists``: Throw an :class:`snowflake.core.exceptions.ConflictError`
            if the catalog integration already exists in Snowflake.
            Equivalent to SQL ``create catalog integration <name> ...``.

            ``CreateMode.or_replace``: Replace if the catalog integration already exists in Snowflake. Equivalent to SQL
            ``create or replace catalog integration <name> ...``.

            ``CreateMode.if_not_exists``: Do nothing if the catalog integration already exists in Snowflake.
            Equivalent to SQL ``create catalog integration <name> if not exists...``

            Default is ``CreateMode.error_if_exists``.

        Examples
        ________
        Creating a catalog integration instance by glue:

        >>> root.catalog_integrations.create(CatalogIntegration(
        ...     name = 'my_catalog_integration',
        ...     catalog = Glue(
        ...         catalog_namespace="abcd-ns",
        ...         glue_aws_role_arn="arn:aws:iam::123456789012:role/sqsAccess",
        ...         glue_catalog_id="1234567",
        ...     ),
        ...     table_format = "ICEBERG",
        ...     enabled = True,
        ... ))

        Creating a catalog integration instance by object store:

        >>> root.catalog_integrations.create(CatalogIntegration(
        ...     name = 'my_catalog_integration',
        ...     catalog = ObjectStore(),
        ...     table_format = "ICEBERG",
        ...     enabled = True,
        ... ))

        Creating a catalog integration instance by polaris:

        >>> root.catalog_integrations.create(CatalogIntegration(
        ...     name = 'my_catalog_integration',
        ...     catalog = Polaris(
        ...         catalog_namespace="abcd-ns",
        ...         rest_config=RestConfig(
        ...             catalog_uri="https://my_account.snowflakecomputing.com/polaris/api/catalog",
        ...             warehouse_name="my_warehouse",
        ...         ),
        ...         rest_authenticator=OAuth(
        ...             type="OAUTH",
        ...             oauth_client_id="my_oauth_client_id",
        ...             oauth_client_secret="my_oauth_client_secret",
        ...             oauth_allowed_scopes=["PRINCIPAL_ROLE:ALL"],
        ...         ),
        ...     ),
        ...     table_format = "ICEBERG",
        ...     enabled = True,
        ... ))
        """
        real_mode = CreateMode[mode].value

        self._api.create_catalog_integration(
            catalog_integration, create_mode=StrictStr(real_mode),
            async_req=False
        )

        return CatalogIntegrationResource(catalog_integration.name, self)

    @api_telemetry
    def create_async(
        self,
        catalog_integration: CatalogIntegration,
        mode: CreateMode = CreateMode.error_if_exists,
    ) -> PollingOperation["CatalogIntegrationResource"]:
        """An asynchronous version of :func:`create`.

        Refer to :class:`~snowflake.core.PollingOperation` for more information on asynchronous execution and
        the return type.
        """ # noqa: D401
        real_mode = CreateMode[mode].value

        future = self._api.create_catalog_integration(
            catalog_integration, create_mode=StrictStr(real_mode),
            async_req=True
        )
        return PollingOperation(future, lambda _: self[catalog_integration.name])

    @api_telemetry
    def iter(
        self,
        like: Optional[str] = None
    ) -> Iterator[CatalogIntegration]:
        """Iterate through ``CatalogIntegration`` objects from Snowflake.

        Parameters
        __________
        like: str, optional
            A case-insensitive string functioning as a filter, with support for SQL
            wildcard characters (% and _).

        Examples
        ________
        Showing all catalog integrations that you have access to see:

        >>> catalog_integrations = catalog_integration_collection.iter()

        Showing information of the exact catalog integration you want to see:

        >>> catalog_integrations = catalog_integration_collection.iter(like="your-catalog-integration-name")

        Showing catalog integrations starting with 'your-catalog-integration-name-':

        >>> catalog_integrations = catalog_integration_collection.iter(like="your-catalog-integration-name-%")

        Using a for loop to retrieve information from iterator:

        >>> for catalog_integration in catalog_integrations:
        ...     print(catalog_integration.name)
        """
        catalog_integrations = self._api.list_catalog_integrations(
            like=like,
            async_req=False
        )
        return iter(catalog_integrations)

    @api_telemetry
    def iter_async(self, like: Optional[str] = None) -> PollingOperation[Iterator[CatalogIntegration]]:
        """An asynchronous version of :func:`iter`.

        Refer to :class:`~snowflake.core.PollingOperation` for more information on asynchronous execution and
        the return type.
        """ # noqa: D401
        future = self._api.list_catalog_integrations(
            like=like,
            async_req=True
        )
        return PollingOperations.iterator(future)


class CatalogIntegrationResource(ObjectReferenceMixin[CatalogIntegrationCollection]):
    """Represents a reference to a Snowflake Catalog Integration resource.

    With this catalog integration reference, you can create, update, and fetch information about catalog integrations,
    as well as perform certain actions on them.
    """

    def __init__(
        self,
        name: StrictStr,
        collection: CatalogIntegrationCollection
    ) -> None:
        self.name = name
        self.collection = collection

    @api_telemetry
    def fetch(self) -> CatalogIntegration:
        """Fetch the details of a catalog integration.

        Examples
        ________
        Fetching a catalog integration reference to print its time of creation:

        >>> print(catalog_integration_reference.fetch().created_on)
        """
        return self.collection._api.fetch_catalog_integration(
            self.name, async_req=False
        )

    @api_telemetry
    def fetch_async(self) -> PollingOperation[CatalogIntegration]:
        """An asynchronous version of :func:`fetch`.

        Refer to :class:`~snowflake.core.PollingOperation` for more information on asynchronous execution and
        the return type.
        """ # noqa: D401
        future = self.collection._api.fetch_catalog_integration(
            self.name, async_req=True
        )
        return PollingOperations.identity(future)


    @api_telemetry
    def drop(self, if_exists: Optional[bool] = None) -> None:
        """Drop this catalog integration.

        Parameters
        __________
        if_exists: bool, optional
            Check the existence of this catalog integration before dropping it.
            Default is ``None``, which is equivalent to ``False``.

        Examples
        ________
        Deleting a catalog integration using its reference:

        >>> catalog_integration_reference.drop()

        Deleting a catalog integration using its reference if it exists:

        >>> catalog_integration_reference.drop(if_exists = True)
        """
        self.collection._api.delete_catalog_integration(
            self.name, if_exists=if_exists, async_req=False
        )

    @api_telemetry
    def drop_async(self, if_exists: Optional[bool] = None) -> PollingOperation[None]:
        """An asynchronous version of :func:`drop`.

        Refer to :class:`~snowflake.core.PollingOperation` for more information on asynchronous execution and
        the return type.
        """ # noqa: D401
        future = self.collection._api.delete_catalog_integration(
            self.name, if_exists=if_exists, async_req=True
        )
        return PollingOperations.empty(future)
