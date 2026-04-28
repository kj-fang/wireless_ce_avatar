from collections.abc import Iterator
from typing import TYPE_CHECKING, Optional

from pydantic import StrictStr

from snowflake.core import PollingOperation
from snowflake.core._common import AccountObjectCollectionParent, ObjectReferenceMixin
from snowflake.core._internal.telemetry import api_telemetry
from snowflake.core._operation import PollingOperations
from snowflake.core.account._generated.api import AccountApi
from snowflake.core.account._generated.api_client import StoredProcApiClient
from snowflake.core.account._generated.models.account import AccountModel as Account


if TYPE_CHECKING:
    from snowflake.core import Root


class AccountCollection(AccountObjectCollectionParent["AccountResource"]):
    """Represents the collection operations of the Snowflake Account resource.

    With this collection, you can create, iterate through, and search for an account that you have access to
    in the current context.

    Examples
    ________
    Creating an account instance:

    >>> account_collection = root.accounts
    >>> account = Account(
    ...     name="MY_ACCOUNT",
    ...     admin_name = "admin"
    ...     admin_password = 'TestPassword1'
    ...     first_name = "Jane"
    ...     last_name = "Smith"
    ...     email = 'myemail@myorg.org'
    ...     edition = "enterprise"
    ...     region = "aws_us_west_2"
    ...  )
    >>> account_collection.create(account)
    """

    def __init__(self, root: "Root") -> None:
        super().__init__(root, ref_class=AccountResource)
        self._api = AccountApi(
            root=root, resource_class=self._ref_class, sproc_client=StoredProcApiClient(root=self.root)
        )

    @api_telemetry
    def create(
        self,
        account: Account,
    ) -> "AccountResource":
        """Create an account in Snowflake.

        Parameters
        __________
        account: Account
            The ``Account`` object, together with the ``Account``'s properties:
            name, admin_name, email, edition; admin_password, first_name, last_name, must_change_password, region_group,
            region, comment, polaris are optional.

        Examples
        ________
        Creating an account instance and getting reference to it:

        >>> account = Account(
        ...     name="MY_ACCOUNT",
        ...     admin_name = "admin"
        ...     admin_password = 'TestPassword1'
        ...     first_name = "Jane"
        ...     last_name = "Smith"
        ...     email = 'myemail@myorg.org'
        ...     edition = "enterprise"
        ...     region = "aws_us_west_2"
        ...  )
        >>> # Use the account collection created before to create a reference to the account resource
        >>> # in Snowflake.
        >>> account_reference = account_collection.create(account)
        """
        self._api.create_account(account=account._to_model(), async_req=False)
        return self[account.name]

    @api_telemetry
    def create_async(
        self,
        account: Account,
    ) -> PollingOperation["AccountResource"]:
        """An asynchronous version of :func:`create`.

        Refer to :class:`~snowflake.core.PollingOperation` for more information on asynchronous execution and
        the return type.
        """ # noqa: D401
        future = self._api.create_account(account=account._to_model(), async_req=True)
        return PollingOperation(future, lambda _: self[account.name])

    @api_telemetry
    def iter(
        self,
        *,
        like: Optional[str] = None,
        limit: Optional[int] = None,
        history: Optional[bool] = None,
    ) -> Iterator[Account]:
        """Iterate through ``Account`` objects in Snowflake, filtering on any optional ``like`` pattern.

        Parameters
        __________
        like: str, optional
            A case-insensitive string functioning as a filter, with support for SQL
            wildcard characters (% and _).
        limit: int, optional
            Limit of the maximum number of rows returned by iter(). The default is ``None``, which behaves equivalently
            to show_limit=10000. This value must be between ``1`` and ``10000``.
        history: bool, optional
            If ``True``, includes dropped accounts that have not yet been deleted. The default is ``None``, which
            behaves equivalently to ``False``.

        Examples
        ________

        Showing all accounts you have access to see:

        >>> accounts = account_collection.iter()

        Showing information of the exact account you want to see:

        >>> accounts = account_collection.iter(like="your-account-name")

        Showing accounts starting with 'your-account-name':

        >>> accounts = account_collection.iter(like="your-account-name%")

        Using a for loop to retrieve information from iterator:

        >>> for account in accounts:
        >>>     print(account.name, account.comment)
        """
        accounts = self._api.list_accounts(
            StrictStr(like) if like is not None else None,
            limit,
            history,
            async_req=False,
        )

        return map(Account._from_model, iter(accounts))

    @api_telemetry
    def iter_async(
            self,
            *,
            like: Optional[str] = None,
            limit: Optional[int] = None,
            history: Optional[bool] = None,
    ) -> PollingOperation[Iterator[Account]]:
        """An asynchronous version of :func:`iter`.

        Refer to :class:`~snowflake.core.PollingOperation` for more information on asynchronous execution and
        the return type.
        """ # noqa: D401
        future = self._api.list_accounts(
            StrictStr(like) if like is not None else None,
            limit,
            history,
            async_req=True,
        )
        return PollingOperation(future, lambda accounts: map(Account._from_model, iter(accounts)))


class AccountResource(ObjectReferenceMixin[AccountCollection]):
    """Represents a reference to a Snowflake account.

    With this account reference, you can fetch information about accounts, as well
    as perform certain actions on them.
    """

    def __init__(self, name: str, collection: AccountCollection) -> None:
        self.name = name
        self.collection = collection

    @property
    def _api(self) -> AccountApi:
        """Get the Account API object."""
        return self.collection._api

    @api_telemetry
    def drop(self, grace_period_in_days: int, if_exists: Optional[bool] = None) -> None:
        """Drop this account.

        Parameters
        __________
        grace_period_in_days : int
            Specifies the number of days during which the account can be restored.
        if_exists: bool, optional
            Check the existence of this account before dropping it.
            Default is ``None``, which is equivalent to ``False``.

        Examples
        ________
        Deleting an account using its reference:

        >>> account_reference.drop()

        Deleting an account using its reference if it exists:

        >>> account_reference.drop(if_exists=True)
        """
        self._api.delete_account(
            self.name, grace_period_in_days=grace_period_in_days, if_exists=if_exists, async_req=False
        )

    @api_telemetry
    def drop_async(self, grace_period_in_days: int, if_exists: Optional[bool] = None) -> PollingOperation[None]:
        """An asynchronous version of :func:`drop`.

        Refer to :class:`~snowflake.core.PollingOperation` for more information on asynchronous execution and
        the return type.
        """ # noqa: D401
        future = self._api.delete_account(
            self.name, grace_period_in_days=grace_period_in_days, if_exists=if_exists, async_req=True
        )
        return PollingOperations.empty(future)

    @api_telemetry
    def undrop(self) -> None:
        """Undrop this account.

        Examples
        ________
        Undropping an account using its reference:

        >>> account_reference.undrop()
        """
        self._api.undrop_account(self.name, async_req=False)

    @api_telemetry
    def undrop_async(self) -> PollingOperation[None]:
        """An asynchronous version of :func:`undrop`.

        Refer to :class:`~snowflake.core.PollingOperation` for more information on asynchronous execution and
        the return type.
        """ # noqa: D401
        future = self._api.undrop_account(self.name, async_req=True)
        return PollingOperations.empty(future)
