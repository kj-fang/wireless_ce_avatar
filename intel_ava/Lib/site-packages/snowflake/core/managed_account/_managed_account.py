from collections.abc import Iterator
from typing import TYPE_CHECKING, Optional

from pydantic import StrictStr

from snowflake.core import PollingOperation
from snowflake.core._common import AccountObjectCollectionParent, ObjectReferenceMixin
from snowflake.core._internal.telemetry import api_telemetry
from snowflake.core._operation import PollingOperations
from snowflake.core.managed_account._generated.api import ManagedAccountApi
from snowflake.core.managed_account._generated.api_client import StoredProcApiClient
from snowflake.core.managed_account._generated.models.managed_account import ManagedAccountModel as ManagedAccount


if TYPE_CHECKING:
    from snowflake.core import Root


class ManagedAccountCollection(AccountObjectCollectionParent["ManagedAccountResource"]):
    """Represents the collection operations of the Snowflake ManagedAccount resource.

    With this collection, you can create, iterate through, and search for managed accounts that you have access to
    in the current context.

    Examples
    ________
    Creating a managed account instance:

    >>> managed_account_collection = root.managed_accounts
    >>> managed_account = ManagedAccount(
    ...     name="managed_account_name",
    ...     admin_name = "admin"
    ...     admin_password = 'TestPassword1'
    ...     account_type = "READER"
    ...  )
    >>> managed_account_collection.create(managed_account)
    """

    def __init__(self, root: "Root") -> None:
        super().__init__(root, ref_class=ManagedAccountResource)
        self._api = ManagedAccountApi(
            root=root, resource_class=self._ref_class, sproc_client=StoredProcApiClient(root=self.root)
        )

    @api_telemetry
    def create(
        self,
        managed_account: ManagedAccount,
    ) -> "ManagedAccountResource":
        """Create a managed account in Snowflake.

        Parameters
        __________
        managed_account: ManagedAccount

        Examples
        ________
        Creating a managed account instance and getting reference to it:

        >>> managed_account_parameters = ManagedAccount(
        ...     name="managed_account_name",
        ...     admin_name = "admin"
        ...     admin_password = 'TestPassword1'
        ...     account_type = "READER"
        ...  )
        >>> # Use the managed account collection created before to create a reference to a managed account resource
        >>> # in Snowflake.
        >>> managed_account_reference = managed_account_collection.create(managed_account_parameters)
        """
        self._api.create_managed_account(managed_account=managed_account._to_model(), async_req=False)
        return self[managed_account.name]

    @api_telemetry
    def create_async(
        self,
        managed_account: ManagedAccount,
    ) -> PollingOperation["ManagedAccountResource"]:
        """An asynchronous version of :func:`create`.

        Refer to :class:`~snowflake.core.PollingOperation` for more information on asynchronous execution and
        the return type.
        """ # noqa: D401
        future = self._api.create_managed_account(managed_account=managed_account._to_model(), async_req=True)
        return PollingOperation(future, lambda _: self[managed_account.name])

    @api_telemetry
    def iter(
        self,
        *,
        like: Optional[str] = None,
    ) -> Iterator[ManagedAccount]:
        """Iterate through ``ManagedAccount`` objects in Snowflake, filtering on any optional `like` pattern.

        Parameters
        __________
        like : str, optional
            A case-insensitive string functioning as a filter, with support for SQL
            wildcard characters (% and _).

        Examples
        ________
        Showing all managed accounts that you have access to see:

        >>> managed_accounts = managed_account_collection.iter()

        Showing information of the exact managed account you want to see:

        >>> managed_accounts = managed_account_collection.iter(like="your-managed-account-name")

        Showing managed accounts starting with 'your-managed-account-name-':

        >>> managed_accounts = managed_account_collection.iter(like="your-managed-account-name-%")

        Using a for loop to retrieve information from iterator:

        >>> for managed_account in managed_accounts:
        >>>     print(managed_account.name, managed_account.comment)
        """
        managed_accounts = self._api.list_managed_accounts(
            StrictStr(like) if like is not None else None,
            async_req=False,
        )

        return map(ManagedAccount._from_model, iter(managed_accounts))

    @api_telemetry
    def iter_async(
        self,
        *,
        like: Optional[str] = None,
    ) -> PollingOperation[Iterator[ManagedAccount]]:
        """An asynchronous version of :func:`iter`.

        Refer to :class:`~snowflake.core.PollingOperation` for more information on asynchronous execution and
        the return type.
        """ # noqa: D401
        future = self._api.list_managed_accounts(
            StrictStr(like) if like is not None else None,
            async_req=True,
        )
        return PollingOperation(future, lambda rest_models: map(ManagedAccount._from_model, iter(rest_models)))


class ManagedAccountResource(ObjectReferenceMixin[ManagedAccountCollection]):
    """A reference to a ManagedAccount in Snowflake."""

    def __init__(self, name: str, collection: ManagedAccountCollection) -> None:
        self.name = name
        self.collection = collection

    @property
    def _api(self) -> ManagedAccountApi:
        """Get the ManagedAccount API object."""
        return self.collection._api

    @api_telemetry
    def drop(self) -> None:
        """Drop this managed account.

        Examples
        ________
        Deleting a warehouse using its reference:

        >>> managed_account_reference.drop()
        """
        self._api.delete_managed_account(self.name, async_req=False)

    @api_telemetry
    def drop_async(self) -> PollingOperation[None]:
        """An asynchronous version of :func:`drop`.

        Refer to :class:`~snowflake.core.PollingOperation` for more information on asynchronous execution and
        the return type.
        """ # noqa: D401
        future = self._api.delete_managed_account(self.name, async_req=True)
        return PollingOperations.empty(future)
