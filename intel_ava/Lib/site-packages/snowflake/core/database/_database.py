from collections.abc import Iterator
from concurrent.futures import Future
from functools import cached_property
from typing import TYPE_CHECKING, Literal, Optional, Union, overload

from pydantic import StrictStr

from snowflake.core import PollingOperation
from snowflake.core._common import AccountObjectCollectionParent, Clone, CreateMode, ObjectReferenceMixin, PointOfTime
from snowflake.core._internal.telemetry import api_telemetry
from snowflake.core._internal.utils import deprecated
from snowflake.core._operation import PollingOperations
from snowflake.core.database._generated import SuccessResponse
from snowflake.core.database._generated.api import DatabaseApi
from snowflake.core.database._generated.api_client import StoredProcApiClient
from snowflake.core.database._generated.models.account_identifiers import AccountIdentifiers
from snowflake.core.database._generated.models.database import DatabaseModel as Database
from snowflake.core.database._generated.models.database_clone import DatabaseClone
from snowflake.core.database._generated.models.database_from_share import DatabaseFromShare
from snowflake.core.database._generated.models.point_of_time import PointOfTime as DatabasePointOfTime
from snowflake.core.database_role import DatabaseRoleCollection
from snowflake.core.schema import SchemaCollection


if TYPE_CHECKING:
    from snowflake.core import Root


class DatabaseCollection(AccountObjectCollectionParent["DatabaseResource"]):
    """Represents the collection operations on the Snowflake Database resource.

    With this collection, you can create, iterate through, and search for Databases that you have access to in the
    current context.

    Examples
    ________
    Creating a database instance:

    >>> databases = root.databases
    >>> new_database = Database(
    ...     name="my_new_database",
    ...     comment="this is my new database to prototype a new feature in",
    ...    )
    >>> databases.create(new_database)
    """

    def __init__(self, root: "Root") -> None:
        super().__init__(root, ref_class=DatabaseResource)
        self._api = DatabaseApi(
            root=root, resource_class=self._ref_class, sproc_client=StoredProcApiClient(root=self.root)
        )

    @api_telemetry
    def create(
        self,
        database: Database,
        *,
        clone: Optional[Union[str, Clone]] = None,
        from_share: Optional[str] = None,
        mode: CreateMode = CreateMode.error_if_exists,
    ) -> "DatabaseResource":
        """Create a database in Snowflake.

        Parameters
        __________
        database: Database
            The ``Database`` object, together with the ``Database``'s properties: name; kind, comment, retention_time,
            budget, data_retention_time_in_days, default_ddl_collation, log_level, max_data_extension_time_in_days,
            suspend_task_after_num_failures, trace_level, user_task_managed_initial_warehouse_size, user_task_timeout_ms
            serverless_task_min_statement_size and serverless_task_max_statement_size are optional.
        clone: str, or Clone, optional
            Whether to clone an existing database. An instance of :class:`Clone`, or ``None``
            if no cloning is necessary.
        from_share: str, optional
            ID of the share from which to create the database, in the form ``"<provider_account>.<share_name>``".
        mode: CreateMode, optional
            One of the following enum values.

            ``CreateMode.error_if_exists``: Throw an :class:`snowflake.core.exceptions.ConflictError`
            if the database already exists in Snowflake.  Equivalent to SQL ``create database <name> ...``.

            ``CreateMode.or_replace``: Replace if the database already exists in Snowflake. Equivalent to SQL
            ``create or replace database <name> ...``.

            ``CreateMode.if_not_exists``: Do nothing if the database already exists in Snowflake.
            Equivalent to SQL ``create database <name> if not exists...``

            Default is ``CreateMode.error_if_exists``.

            Example
            _______
            Creating a new database if it does not already exist:

            >>> new_db_ref = root.databases.create(Database(name="my_new_database"), mode=CreateMode.if_not_exists)
            >>> print(new_db_ref.fetch())
        """
        self._create(
            database=database,
            clone=clone,
            from_share=from_share,
            mode=mode,
            async_req=False,
        )
        return self[database.name]

    @api_telemetry
    def create_async(
        self,
        database: Database,
        *,
        clone: Optional[Union[str, Clone]] = None,
        from_share: Optional[str] = None,
        mode: CreateMode = CreateMode.error_if_exists,
    ) -> PollingOperation["DatabaseResource"]:
        """An asynchronous version of :func:`create`.

        Refer to :class:`~snowflake.core.PollingOperation` for more information on asynchronous execution and
        the return type.
        """ # noqa: D401
        future = self._create(
            database=database,
            clone=clone,
            from_share=from_share,
            mode=mode,
            async_req=True,
        )
        return PollingOperation(future, lambda _: self[database.name])

    @api_telemetry
    def iter(
        self,
        *,
        like: Optional[str] = None,
        starts_with: Optional[str] = None,
        limit: Optional[int] = None,
        from_name: Optional[str] = None,
    ) -> Iterator[Database]:
        """
        Iterate through ``Database`` objects from Snowflake,  filtering on any optional 'like' pattern.

        Parameters
        _________
        like: str, optional
            A case-insensitive string functioning as a filter, with support for SQL
            wildcard characters (``%`` and ``_``).
        starts_with: str, optional
            String used to filter the command output based on the string of characters that appear
            at the beginning of the object name. Uses case-sensitive pattern matching.
        limit: int, optional
            Limit of the maximum number of rows returned by iter(). The default is ``None``, which behaves equivalently
            to show_limit=10000. This value must be between ``1`` and ``10000``.
        from_name: str, optional
            Fetch rows only following the first row whose object name matches
            the specified string. This is case-sensitive and does not have to be the full name.

        Examples
        ________
        Showing all databases that you have access to see:

        >>> databases = root.databases.iter()

        Showing information of the exact database you want to see:

        >>> databases = root.databases.iter(like="your-database-name")

        Showing databases starting with 'your-database-name-':

        >>> databases = root.databases.iter(like="your-database-name-%")

        Using a for loop to retrieve information from iterator:

        >>> for database in databases:
        >>>     print(database.name, database.query)
        """
        databases = self._api.list_databases(
            StrictStr(like) if like is not None else None,
            StrictStr(starts_with) if starts_with else None,
            limit,
            from_name=from_name,
            async_req=False,
        )

        return map(Database._from_model, iter(databases))

    @api_telemetry
    def iter_async(
        self,
        *,
        like: Optional[str] = None,
        starts_with: Optional[str] = None,
        limit: Optional[int] = None,
        from_name: Optional[str] = None,
    ) -> PollingOperation[Iterator[Database]]:
        """An asynchronous version of :func:`iter`.

        Refer to :class:`~snowflake.core.PollingOperation` for more information on asynchronous execution and
        the return type.
        """ # noqa: D401
        future = self._api.list_databases(
            StrictStr(like) if like is not None else None,
            StrictStr(starts_with) if starts_with else None,
            limit,
            from_name=from_name,
            async_req=True,
        )
        return PollingOperation(future, lambda rest_models: map(Database._from_model, iter(rest_models)))

    @overload
    def _create(
        self,
        database: Database,
        clone: Optional[Union[str, Clone]],
        from_share: Optional[str],
        mode: CreateMode,
        async_req: Literal[True],
    ) -> Future[SuccessResponse]:
        ...

    @overload
    def _create(
        self,
        database: Database,
        clone: Optional[Union[str, Clone]],
        from_share: Optional[str],
        mode: CreateMode,
        async_req: Literal[False],
    ) -> SuccessResponse:
        ...

    def _create(
        self,
        database: Database,
        clone: Optional[Union[str, Clone]],
        from_share: Optional[str],
        mode: CreateMode,
        async_req: bool,
    ) -> Union[SuccessResponse, Future[SuccessResponse]]:
        if clone is not None and from_share is not None:
            raise ValueError("Cannot use both `clone` and `from_share`")

        real_mode = CreateMode[mode].value
        if clone is not None:
            pot: Optional[DatabasePointOfTime] = None
            if isinstance(clone, Clone) and isinstance(clone.point_of_time, PointOfTime):
                pot = DatabasePointOfTime.from_dict(clone.point_of_time.to_dict())
            real_clone = Clone(source=clone) if isinstance(clone, str) else clone
            clone_req = DatabaseClone(
                point_of_time=pot,
                **database._to_model().to_dict(),
            )
            return self._api.clone_database(
                name=real_clone.source,
                database_clone=clone_req,
                create_mode=StrictStr(real_mode),
                async_req=async_req,
            )

        if from_share is not None:
            share_req = DatabaseFromShare(
                **database._to_model().to_dict(),
            )
            return self._api.create_database_from_share(
                database_from_share=share_req,
                share=from_share,
                create_mode=StrictStr(real_mode),
                async_req=async_req,
            )

        return self._api.create_database(
            database=database._to_model(),
            create_mode=StrictStr(real_mode),
            async_req=async_req,
        )


class DatabaseResource(ObjectReferenceMixin[DatabaseCollection]):
    """Represents a reference to a Snowflake database.

    With this database reference, you can create, update, and fetch information about databases, as well
    as perform certain unique actions on them.
    """

    def __init__(self, name: str, collection: DatabaseCollection) -> None:
        self.name = name
        self.collection: DatabaseCollection = collection

    @property
    def _api(self) -> DatabaseApi:
        return self.collection._api

    @api_telemetry
    def fetch(self) -> Database:
        """Fetch the details of a database.

        Examples
        ________
        Fetching a reference to a database to print whether it's the currently used database:

        >>> my_database = root.databases["my_db"].fetch()
        >>> print(my_database.is_current)
        """
        return Database._from_model(
            self.collection._api.fetch_database(
                self.name,
                async_req=False,
            )
        )

    @api_telemetry
    def fetch_async(self) -> PollingOperation[Database]:
        """An asynchronous version of :func:`fetch`.

        Refer to :class:`~snowflake.core.PollingOperation` for more information on asynchronous execution and
        the return type.
        """ # noqa: D401
        future = self.collection._api.fetch_database(
            self.name,
            async_req=True,
        )
        return PollingOperation(future, lambda rest_model: Database._from_model(rest_model))

    @api_telemetry
    @deprecated("drop")
    def delete(self, if_exists: Optional[bool] = None) -> None:
        """Delete this database.

        Parameters
        __________
        if_exists: bool, optional
            Check the existence of this database before dropping it.
            The default is ``None``, which is equivalent to ``False``.

        Examples
        ________
        Deleting a database using its reference:

        >>> root.databases["to_be_deleted"].delete()
        """
        self.drop(if_exists=if_exists)

    @api_telemetry
    def drop(self, if_exists: Optional[bool] = None) -> None:
        """Drop this database.

        Parameters
        __________
        if_exists: bool, optional
            Check the existence of this database before dropping it.
            The default is ``None``, which is equivalent to ``False``.

        Examples
        ________
        Using a database reference to drop a database, erroring if it does not exist:

        >>> root.databases["to_be_dropped"].drop()

        Using a database reference to drop a database, if it exists:

        >>> root.databases["to_be_dropped"].drop(if_exists=True)
        """
        self.collection._api.delete_database(
            name=self.name,
            if_exists=if_exists,
            async_req=False,
        )

    @api_telemetry
    def drop_async(self, if_exists: Optional[bool] = None) -> PollingOperation[None]:
        """An asynchronous version of :func:`drop`.

        Refer to :class:`~snowflake.core.PollingOperation` for more information on asynchronous execution and
        the return type.
        """ # noqa: D401
        future = self.collection._api.delete_database(
            name=self.name,
            if_exists=if_exists,
            async_req=True,
        )
        return PollingOperations.empty(future)

    @api_telemetry
    def undrop(self) -> None:
        """Undrop this database.

        Examples
        ________
        Undropping a database using its reference:

        >>> root.databases["to_be_undropped"].undrop()
        """
        self.collection._api.undrop_database(
            name=self.name,
            async_req=False,
        )

    @api_telemetry
    def undrop_async(self) -> PollingOperation[None]:
        """An asynchronous version of :func:`undrop`.

        Refer to :class:`~snowflake.core.PollingOperation` for more information on asynchronous execution and
        the return type.
        """ # noqa: D401
        future = self.collection._api.undrop_database(
            name=self.name,
            async_req=True,
        )
        return PollingOperations.empty(future)

    @api_telemetry
    @deprecated("create_or_alter")
    def create_or_update(
        self,
        database: Database,
    ) -> None:
        """Create or update an existing database.

        Examples
        ________
        Create a database from a reference:

        >>> root.databases["my_new_db"].create_or_update(Database("my_new_db"))

        """
        self.create_or_alter(database=database)

    @api_telemetry
    def create_or_alter(
        self,
        database: Database,
    ) -> None:
        """Create or alter a database in Snowflake."""
        self._api.create_or_alter_database(database.name, database._to_model(), async_req=False)

    @api_telemetry
    def create_or_alter_async(
        self,
        database: Database,
    ) -> PollingOperation[None]:
        """An asynchronous version of :func:`create_or_alter`.

        Refer to :class:`~snowflake.core.PollingOperation` for more information on asynchronous execution and
        the return type.
        """ # noqa: D401
        future = self._api.create_or_alter_database(database.name, database._to_model(), async_req=True)
        return PollingOperations.empty(future)

    @api_telemetry
    def enable_replication(self, accounts: list[str], ignore_edition_check: bool = False) -> None:
        """Promotes a local database to serve as a primary database for replication.

        A primary database can be replicated in one or more accounts, allowing users
        in those accounts to query objects in each secondary (i.e. replica) database.

        Alternatively, modify an existing primary database to add to or remove from
        the list of accounts that can store a replica of the database.

        Provide a list of accounts in your organization that can store a replica of
        this database.

        Parameters
        __________
        accounts: list of str
            Array of unique account identifiers for which to enable replication.
        ignore_edition_check: bool, optional
            Whether to allow replicating data to accounts on lower editions.

            Default is ``True``.

        Examples
        ________
        Enabling replication of "my_db" database on 2 other accounts using its reference:

        >>> root.databases["my_db"].enable_replication(
        ...     accounts=["accountName1", "accountName2"],
        ...     ignore_edition_check=True,
        ... )
        """
        if len(accounts) == 0:
            raise ValueError(
                "Account list given to replication cannot be empty.",
            )
        self.collection._api.enable_database_replication(
            name=self.name,
            account_identifiers=AccountIdentifiers(accounts=accounts),
            ignore_edition_check=ignore_edition_check,
            async_req=False,
        )

    @api_telemetry
    def enable_replication_async(
        self,
        accounts: list[str],
        ignore_edition_check: bool = False,
    ) -> PollingOperation[None]:
        """An asynchronous version of :func:`enable_replication`.

        Refer to :class:`~snowflake.core.PollingOperation` for more information on asynchronous execution and
        the return type.
        """ # noqa: D401
        if len(accounts) == 0:
            raise ValueError(
                "Account list given to replication cannot be empty.",
            )
        future = self.collection._api.enable_database_replication(
            name=self.name,
            account_identifiers=AccountIdentifiers(accounts=accounts),
            ignore_edition_check=ignore_edition_check,
            async_req=True,
        )
        return PollingOperations.empty(future)


    @api_telemetry
    def disable_replication(self, accounts: Optional[list[str]] = None) -> None:
        """Disables replication for this primary database.

        Which means that no replica of this database (i.e. secondary database) in
        another account can be refreshed. Any secondary databases remain linked to
        the primary database, but requests to refresh a secondary database are denied.

        Note that disabling replication for a primary database does not prevent it
        from being replicated to the same account; therefore, the database continues
        to be listed in the SHOW REPLICATION DATABASES output.

        Optionally provide a comma-separated list of accounts in your organization
        to disable replication for this database only in the specified accounts.

        Parameters
        __________
        accounts: list of str, optional
            Array of unique account identifiers for which to disable replication.

        Examples
        ________
        Disabling all replication of "my_db" database using its reference:

        >>> root.databases["my_db"].disable_replication()
        """
        if accounts is None:
            accounts = []
        self.collection._api.disable_database_replication(
            name=self.name,
            account_identifiers=AccountIdentifiers(accounts=accounts),
            async_req=False,
        )

    @api_telemetry
    def disable_replication_async(self, accounts: Optional[list[str]] = None) -> PollingOperation[None]:
        """An asynchronous version of :func:`disable_replication`.

        Refer to :class:`~snowflake.core.PollingOperation` for more information on asynchronous execution and
        the return type.
        """ # noqa: D401
        if accounts is None:
            accounts = []
        future = self.collection._api.disable_database_replication(
            name=self.name,
            account_identifiers=AccountIdentifiers(accounts=accounts),
            async_req=True,
        )
        return PollingOperations.empty(future)

    @api_telemetry
    def refresh_replication(self) -> None:
        """Refresh a secondary database from its primary database.

        A snapshot includes changes to the objects and data.

        Examples
        ________
        Refreshing a database replication using its reference:

        >>> root.databases["db_replication"].refresh_replication()
        """
        self.collection._api.refresh_database_replication(
            name=self.name,
            async_req=False,
        )

    @api_telemetry
    def refresh_replication_async(self) -> PollingOperation[None]:
        """An asynchronous version of :func:`refresh_replication`.

        Refer to :class:`~snowflake.core.PollingOperation` for more information on asynchronous execution and
        the return type.
        """ # noqa: D401
        future = self.collection._api.refresh_database_replication(
            name=self.name,
            async_req=True,
        )
        return PollingOperations.empty(future)

    @api_telemetry
    def enable_failover(self, accounts: list[str]) -> None:
        """Enable a list of replicas of this database that can be promoted to primary.

        Parameters
        __________
        accounts: list of str
            Array of unique account identifiers for which to enable failover.

        Examples
        ________
        Enabling failover to an account using a database reference:

        >>> root.databases["my_db"].enable_failover(accounts=["my_failover_acc"])
        """
        if len(accounts) == 0:
            raise ValueError(
                "Account list given to replication cannot be empty.",
            )
        self.collection._api.enable_database_failover(
            name=self.name,
            account_identifiers=AccountIdentifiers(accounts=accounts),
            async_req=False,
        )

    @api_telemetry
    def enable_failover_async(self, accounts: list[str]) -> PollingOperation[None]:
        """An asynchronous version of :func:`enable_failover`.

        Refer to :class:`~snowflake.core.PollingOperation` for more information on asynchronous execution and
        the return type.
        """ # noqa: D401
        if len(accounts) == 0:
            raise ValueError(
                "Account list given to replication cannot be empty.",
            )
        future = self.collection._api.enable_database_failover(
            name=self.name,
            account_identifiers=AccountIdentifiers(accounts=accounts),
            async_req=True,
        )
        return PollingOperations.empty(future)

    @api_telemetry
    def disable_failover(self, accounts: Optional[list[str]] = None) -> None:
        """Disables failover for this primary databases.

        Which means that no replica of this database (i.e. secondary database) can be
        promoted to serve as the primary database.

        Optionally provide a comma-separated list of accounts in your organization to
        disable failover for this database only in the specified accounts.

        Parameters
        __________
        accounts: list of str, optional
            Array of unique account identifiers for which to enable failover.

        Examples
        ________
        Disabling failover to some account using a database reference:

        >>> root.databases["my_db"].enable_failover(accounts=["old_failover_acc"])
        """
        if accounts is None:
            accounts = []
        self.collection._api.disable_database_failover(
            name=self.name,
            account_identifiers=AccountIdentifiers(accounts=accounts),
            async_req=False,
        )

    @api_telemetry
    def disable_failover_async(self, accounts: Optional[list[str]] = None) -> PollingOperation[None]:
        """An asynchronous version of :func:`disable_failover`.

        Refer to :class:`~snowflake.core.PollingOperation` for more information on asynchronous execution and
        the return type.
        """ # noqa: D401
        if accounts is None:
            accounts = []
        future = self.collection._api.disable_database_failover(
            name=self.name,
            account_identifiers=AccountIdentifiers(accounts=accounts),
            async_req=True,
        )
        return PollingOperations.empty(future)

    @api_telemetry
    def promote_to_primary_failover(self) -> None:
        """Promotes the specified secondary (replica) database to serve as the primary.

        When promoted, the database becomes writeable. At the same time, the previous
        primary database becomes a read-only secondary database.

        Examples
        ________
        Promoting a ``my_db`` failover database to be the primary using its reference:

        >>> root.databases["my_db"].promote_to_primary_failover()
        """
        self.collection._api.primary_database_failover(
            name=self.name,
            async_req=False,
        )

    @api_telemetry
    def promote_to_primary_failover_async(self) -> PollingOperation[None]:
        """An asynchronous version of :func:`promote_to_primary_failover`.

        Refer to :class:`~snowflake.core.PollingOperation` for more information on asynchronous execution and
        the return type.
        """ # noqa: D401
        future = self.collection._api.primary_database_failover(
            name=self.name,
            async_req=True,
        )
        return PollingOperations.empty(future)

    @cached_property
    def schemas(self) -> SchemaCollection:
        """The SchemaCollection of all schemas contained in this database.

        Examples
        ________
        Getting all schemas in ``my_db``:

        >>> root.databases["my_db"].schemas
        """
        return SchemaCollection(self, self.root)

    @cached_property
    def database_roles(self) -> DatabaseRoleCollection:
        """The DatabaseRoleCollection of all database roles contained in this database.

        Examples
        ________
        Getting all database roles in ``my_db``:

        >>> root.databases["my_db"].database_roles
        """
        return DatabaseRoleCollection(self, self.root)
