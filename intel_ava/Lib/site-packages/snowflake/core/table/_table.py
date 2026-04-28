import warnings

from collections.abc import Iterator
from concurrent.futures import Future
from typing import TYPE_CHECKING, Literal, Optional, Union, overload

from pydantic import StrictStr

from snowflake.core import FQN, PollingOperation
from snowflake.core._common import (
    Clone,
    CreateMode,
    PointOfTime,
    SchemaObjectCollectionParent,
    SchemaObjectReferenceMixin,
)
from snowflake.core._operation import PollingOperations

from .._internal.telemetry import api_telemetry
from .._internal.utils import deprecated
from ._generated import SuccessResponse
from ._generated.api import TableApi
from ._generated.api_client import StoredProcApiClient
from ._generated.models.point_of_time import PointOfTime as TablePointOfTime
from ._generated.models.table import Table
from ._generated.models.table_as_select import TableAsSelect
from ._generated.models.table_clone import TableClone
from ._generated.models.table_like import TableLike
from ._generated.models.table_using_template import TableUsingTemplate


if TYPE_CHECKING:
    from snowflake.core.schema import SchemaResource


_DEPRECATED_KIND_MAP: dict[Optional[str], Optional[str]] = {
    "": "PERMANENT",
    "transient": "TRANSIENT",
    "temporary": "TEMPORARY",
}


def _fix_table_kind(table: Table) -> Table:
    kind = _DEPRECATED_KIND_MAP.get(table.kind, table.kind)
    if kind != table.kind:
        warnings.warn(
            f"The value kind={table.kind!r} is deprecated; use kind={kind!r} instead.",
            category=DeprecationWarning,
            stacklevel=4,  # skip 1) this function, 2) create/create_or_update, 3) telemetry
        )
    return table.model_copy(update={"kind": kind})


def _validate_table_inputs(
    table: Union[Table, str],
    as_select: Optional[str] = None,
    template: Optional[str] = None,
    like_table: Optional[str] = None,
    clone_table: Optional[Union[str, Clone]] = None,
) -> None:
    not_none_count = sum(bool(x) for x in (as_select, template, like_table, clone_table))

    if not_none_count > 1:
        raise ValueError("at most one of the `as_select`, `template`, `clone_table`, " "or `like_table` can has value")

    if not_none_count == 0 and isinstance(table, str):
        raise ValueError(
            "When `table` is a str, any one of the `as_select`, `template`, `clone_table`, "
            "or `like_table` must not be empty."
        )


class TableCollection(SchemaObjectCollectionParent["TableResource"]):
    """Represents the collection operations on the Snowflake Table resource.

    With this collection, you can create, iterate through, and search for tables that you have access to in the
    current context.

    Examples
    ________
    Creating a table instance:

    >>> tables = root.databases["my_db"].schemas["my_schema"].tables
    >>> new_table = Table(
    ...     name="accounts",
    ...     columns=[
    ...         TableColumn(
    ...             name="id",
    ...             datatype="int",
    ...             nullable=False,
    ...             autoincrement=True,
    ...             autoincrement_start=0,
    ...             autoincrement_increment=1,
    ...         ),
    ...         TableColumn(name="created_on", datatype="timestamp_tz", nullable=False),
    ...         TableColumn(name="email", datatype="string", nullable=False),
    ...         TableColumn(name="password_hash", datatype="string", nullable=False),
    ...     ],
    ... )
    >>> tables.create(new_tables)
    """

    def __init__(self, schema: "SchemaResource"):
        super().__init__(schema, TableResource)
        self._api = TableApi(
            root=self.root, resource_class=self._ref_class, sproc_client=StoredProcApiClient(root=self.root)
        )

    @api_telemetry
    def create(
        self,
        table: Union[Table, str],
        *,
        as_select: Optional[str] = None,
        template: Optional[str] = None,
        like_table: Optional[str] = None,
        clone_table: Optional[Union[str, Clone]] = None,
        copy_grants: Optional[bool] = False,
        mode: CreateMode = CreateMode.error_if_exists,
    ) -> "TableResource":
        """Create a table in Snowflake.

        Parameters
        __________
        table: Table
            The ``Table`` object, together with the ``Table``'s properties, object parameters, columns, and constraints.
            It can either be a table name or a ``Table`` object when it's used together with `as_select`,
            `template`, `like_table`, `clone_table`. It must be a ``Table`` when it's not used with these clauses.
            ``Table`` has the following properties: name; kind, cluster_by, enable_schema_evolution, change_tracking,
            data_retention_time_in_days, max_data_extension_time_in_days, default_ddl_collation, columns, constraints,
            comment, database_name, schema_name are optional.
        as_select: str, optional
            Creates a table from a select statement.
        template: str, optional
            Create a table using the templates specified in staged files.
        like_table: str, optional
            Create a new table like the specified one, but empty.
        clone_table: str or Clone, optional
            Create a new table by cloning the specified table.
        copy_grants: bool, optional
            Copy grants when `clone_table` is provided.
        mode: CreateMode, optional
            One of the following strings.

            ``CreateMode.error_if_exists``: Throw an :class:`snowflake.core.exceptions.ConflictError`
            if the table already exists in Snowflake.  Equivalent to SQL ``create table <name> ...``.

            ``CreateMode.or_replace``: Replace if the task already exists in Snowflake. Equivalent to SQL
            ``create or replace table <name> ...``.

            ``CreateMode.if_not_exists``: Do nothing if the task already exists in Snowflake.
            Equivalent to SQL ``create table <name> if not exists...``

            Default value is ``CreateMode.error_if_exists``.

        Examples
        ________
        Creating a table instance:

        >>> tables = root.databases["my_db"].schemas["my_schema"].tables
        >>> new_table = Table(
        ...     name="events",
        ...     columns=[
        ...         TableColumn(
        ...             name="id",
        ...             datatype="int",
        ...             nullable=False,
        ...             autoincrement=True,
        ...             autoincrement_start=0,
        ...             autoincrement_increment=1,
        ...         ),
        ...         TableColumn(name="category", datatype="string"),
        ...         TableColumn(name="event", datatype="string"),
        ...     ],
        ...     comment="store events/logs in here",
        ... )
        >>> tables.create(new_tables)

        Cloning a Table instance:

        >>> tables = root.databases["my_db"].schemas["my_schema"].tables
        >>> tables.create("new_table", clone_table="original_table_name")

        Cloning a Table instance in a different database and schema:

        >>> tables = root.databases["my_db"].schemas["my_schema"].tables
        >>> tables.create("new_table", clone_table="database_name.schema_name.original_table_name")

        Notes
        _____
        Not currently implemented:
            - Row access policy
            - Column masking policy
            - Search optimization
            - Tags
            - Stage file format and copy options
        """
        _validate_table_inputs(table, as_select, template, like_table, clone_table)

        if isinstance(table, str):
            table = Table(name=table)
        else:
            table = _fix_table_kind(table)

        self._create(
            table=table,
            as_select=as_select,
            template=template,
            like_table=like_table,
            clone_table=clone_table,
            copy_grants=copy_grants,
            mode=mode,
            async_req=False,
        )
        return TableResource(table.name, self)

    @api_telemetry
    def create_async(
        self,
        table: Union[Table, str],
        *,
        as_select: Optional[str] = None,
        template: Optional[str] = None,
        like_table: Optional[str] = None,
        clone_table: Optional[Union[str, Clone]] = None,
        copy_grants: Optional[bool] = False,
        mode: CreateMode = CreateMode.error_if_exists,
    ) -> PollingOperation["TableResource"]:
        """An asynchronous version of :func:`iter`.

        Refer to :class:`~snowflake.core.PollingOperation` for more information on asynchronous execution and
        the return type.
        """ # noqa: D401
        _validate_table_inputs(table, as_select, template, like_table, clone_table)

        if isinstance(table, str):
            table = Table(name=table)
        else:
            table = _fix_table_kind(table)

        future = self._create(
            table=table,
            as_select=as_select,
            template=template,
            like_table=like_table,
            clone_table=clone_table,
            copy_grants=copy_grants,
            mode=mode,
            async_req=True,
        )
        return PollingOperation(future, lambda _: TableResource(table.name, self))

    @api_telemetry
    def iter(
        self,
        *,
        like: Optional[str] = None,
        starts_with: Optional[str] = None,
        limit: Optional[int] = None,
        from_name: Optional[str] = None,
        history: bool = False,
        deep: bool = False,
    ) -> Iterator[Table]:
        """Iterate through ``Table`` objects from Snowflake, filtering on any optional 'like' pattern.

        Parameters
        __________
        like: str, optional
            A case-insensitive string functioning as a filter, with support for SQL wildcard characters
            (``%`` and ``_``).
        starts_with: str, optional
            String used to filter the command output based on the string of characters that appear at the beginning of
            the object name. Uses case-sensitive pattern matching.
        limit: int, optional
            Limit of the maximum number of rows returned by iter(). The default is ``None``, which behaves equivalently
            to show_limit=10000. This value must be between ``1`` and ``10000``.
        from_name: str, optional
            Fetch rows only following the first row whose object name matches the specified string. This is
            case-sensitive and does not have to be the full name.
        history: bool, optional
            Include dropped tables that have not yet been purged yet.
        deep: bool, optional
            Fetch the sub-resources columns and constraints of every table if it's ``True``. Default ``False``.

        Examples
        ________
        Showing all tables that you have access to see in a schema:

        >>> tables = my_schema.tables.iter()

        Showing information of the exact table you want to see:

        >>> tables = my_schema.tables.iter(like="my-table-name")

        Showing tables starting with 'my-table-name-':

        >>> tables = my_schema.tables.iter(like="my-table-name-%")

        Using a for loop to retrieve information from iterator:

        >>> for table in table:
        >>>     print(table.name, table.kind)
        """
        tables = self._api.list_tables(
            database=self.database.name,
            var_schema=self.schema.name,
            like=like,
            starts_with=starts_with,
            show_limit=limit,
            from_name=from_name,
            history=history,
            deep=deep,
            async_req=False,
        )

        return iter(tables)

    @api_telemetry
    def iter_async(
        self,
        *,
        like: Optional[str] = None,
        starts_with: Optional[str] = None,
        limit: Optional[int] = None,
        from_name: Optional[str] = None,
        history: bool = False,
        deep: bool = False,
    ) -> PollingOperation[Iterator[Table]]:
        """An asynchronous version of :func:`iter`.

        Refer to :class:`~snowflake.core.PollingOperation` for more information on asynchronous execution and
        the return type.
        """ # noqa: D401
        future = self._api.list_tables(
            database=self.database.name,
            var_schema=self.schema.name,
            like=like,
            starts_with=starts_with,
            show_limit=limit,
            from_name=from_name,
            history=history,
            deep=deep,
            async_req=True,
        )
        return PollingOperations.iterator(future)

    @overload
    def _create(
        self,
        table: Table,
        as_select: Optional[str],
        template: Optional[str],
        like_table: Optional[str],
        clone_table: Optional[Union[str, Clone]],
        copy_grants: Optional[bool],
        mode: CreateMode,
        async_req: Literal[True],
    ) -> Future[SuccessResponse]:
        ...

    @overload
    def _create(
        self,
        table: Table,
        as_select: Optional[str],
        template: Optional[str],
        like_table: Optional[str],
        clone_table: Optional[Union[str, Clone]],
        copy_grants: Optional[bool],
        mode: CreateMode,
        async_req: Literal[False],
    ) -> SuccessResponse:
        ...

    def _create(
        self,
        table: Table,
        as_select: Optional[str],
        template: Optional[str],
        like_table: Optional[str],
        clone_table: Optional[Union[str, Clone]],
        copy_grants: Optional[bool],
        mode: CreateMode,
        async_req: bool,
    ) -> Union[SuccessResponse, Future[SuccessResponse]]:
        real_mode = CreateMode[mode].value

        if as_select:
            # create table by select
            return self._api.create_table_as_select(
                self.database.name,
                self.schema.name,
                as_select,
                table_as_select=TableAsSelect(**table.to_dict()),
                create_mode=StrictStr(real_mode),
                copy_grants=copy_grants,
                async_req=async_req,
            )
        if template:
            # create table by template
            return self._api.create_table_using_template(
                self.database.name,
                self.schema.name,
                template,
                table_using_template=TableUsingTemplate(**table.to_dict()),
                create_mode=StrictStr(real_mode),
                copy_grants=copy_grants,
                async_req=async_req,
            )
        if clone_table:
            # create table by clone
            pot: Optional[TablePointOfTime] = None
            if isinstance(clone_table, Clone) and isinstance(clone_table.point_of_time, PointOfTime):
                pot = TablePointOfTime.from_dict(clone_table.point_of_time.to_dict())
            real_clone = Clone(source=clone_table) if isinstance(clone_table, str) else clone_table
            req = TableClone(
                point_of_time=pot,
                **table.to_dict(),
            )

            source_table_fqn = FQN.from_string(real_clone.source)
            return self._api.clone_table(
                source_table_fqn.database or self.database.name,
                source_table_fqn.schema or self.schema.name,
                source_table_fqn.name,
                req,
                create_mode=StrictStr(real_mode),
                copy_grants=copy_grants,
                async_req=async_req,
                target_database=self.database.name,
                target_schema=self.schema.name,
            )
        if like_table:
            # create table by like
            return self._api.create_table_like(
                self.database.name,
                self.schema.name,
                like_table,
                table_like=TableLike(**table.to_dict()),
                create_mode=StrictStr(real_mode),
                copy_grants=copy_grants,
                async_req=async_req,
            )
        # create empty table
        return self._api.create_table(
            self.database.name,
            self.schema.name,
            table,
            create_mode=StrictStr(real_mode),
            copy_grants=copy_grants,
            async_req=async_req,
        )


class TableResource(SchemaObjectReferenceMixin[TableCollection]):
    """Represents a reference to a Snowflake table.

    With this table reference, you can create, update, delete and fetch information about tables, as well
    as perform certain actions on them.
    """

    def __init__(self, name: str, collection: TableCollection) -> None:
        self.collection = collection
        self.name = name

    @api_telemetry
    @deprecated("create_or_alter")
    def create_or_update(self, table: Table) -> None:
        """Create or update a table.

        Parameters
        __________
        table: Table
            The ``Table`` object, including the ``Table``'s properties: name; kind, cluster_by,
            enable_schema_evolution, change_tracking, data_retention_time_in_days,
            max_data_extension_time_in_days, default_ddl_collation, columns, constraints, comment are optional.

        Examples
        ________
        Creating a new table:

        >>> my_schema.table["my_table"].create_or_update(my_table_def)

        See ``TableCollection.create`` for more examples.

        Notes
        _____
            - Not currently implemented:
                - Row access policy
                - Column masking policy
                - Search optimization
                - Tags
                - Stage file format and copy options
                - Foreign keys.
                - Rename the table.
                - If the name and table's name don't match, an error will be thrown.
                - Rename or drop a column.
            - New columns can only be added to the back of the column list.
        """
        self.create_or_alter(table=table)

    @api_telemetry
    def create_or_alter(
        self,
        table: Table,
    ) -> None:
        """Create or alter a table.

        Parameters
        __________
        table: Table
            The ``Table`` object, including the ``Table``'s properties: name; kind, cluster_by,
            enable_schema_evolution, change_tracking, data_retention_time_in_days,
            max_data_extension_time_in_days, default_ddl_collation, columns, constraints, comment are optional.

        Examples
        ________
        Creating a new table:

        >>> my_schema.table["my_table"].create_or_alter(my_table_def)

        See ``TableCollection.create`` for more examples.

        Notes
        _____
            - Not currently implemented:
                - Row access policy
                - Column masking policy
                - Search optimization
                - Tags
                - Stage file format and copy options
                - Foreign keys.
                - Rename the table.
                - If the name and table's name don't match, an error will be thrown.
                - Rename or drop a column.
            - New columns can only be added to the back of the column list.
        """
        table = _fix_table_kind(table)
        self.collection._api.create_or_alter_table(
            self.database.name, self.schema.name, self.name, table, async_req=False)

    @api_telemetry
    def create_or_alter_async(
        self,
        table: Table,
    ) -> PollingOperation[None]:
        """An asynchronous version of :func:`create_or_alter`.

        Refer to :class:`~snowflake.core.PollingOperation` for more information on asynchronous execution and
        the return type.
        """ # noqa: D401
        table = _fix_table_kind(table)
        future = self.collection._api.create_or_alter_table(
            self.database.name, self.schema.name, self.name, table, async_req=True)
        return PollingOperations.empty(future)

    @api_telemetry
    def fetch(self) -> Table:
        """Fetch the details of a table.

        Examples
        ________
        Fetching a reference to a table to print its comment:

        >>> table_ref = my_schema.tables["my_table"].fetch()
        >>> print(table_ref.comment)

        Notes
        _____
            Inline constraints will become Outofline constraints because Snowflake database doesn't tell whether a
            constraint is inline or out of line from Snowflake database.
        """
        return self.collection._api.fetch_table(
            self.database.name,
            self.schema.name,
            self.name,
            async_req=False,
        )

    @api_telemetry
    def fetch_async(self) -> PollingOperation[Table]:
        """An asynchronous version of :func:`fetch`.

        Refer to :class:`~snowflake.core.PollingOperation` for more information on asynchronous execution and
        the return type.
        """ # noqa: D401
        future = self.collection._api.fetch_table(
            self.database.name,
            self.schema.name,
            self.name,
            async_req=True,
        )
        return PollingOperations.identity(future)

    @api_telemetry
    @deprecated("drop")
    def delete(self) -> None:
        """Delete the table.

        Examples
        ________
        Deleting a table using its reference:

        >>> table_ref.delete()
        """
        self.drop()

    @api_telemetry
    def drop(self, if_exists: Optional[bool] = None) -> None:
        """Drop the table.

        Parameters
        __________
        if_exists: bool, optional
            Check the existence of this table before suspending it.
            Default is ``None``, which is equivalent to ``False``.

        Examples
        ________
        Dropping a table using its reference:

        >>> table_ref.drop()
        """
        self.collection._api.delete_table(self.database.name, self.schema.name, self.name, if_exists, async_req=False)

    @api_telemetry
    def drop_async(self, if_exists: Optional[bool] = None) -> PollingOperation[None]:
        """An asynchronous version of :func:`drop`.

        Refer to :class:`~snowflake.core.PollingOperation` for more information on asynchronous execution and
        the return type.
        """ # noqa: D401
        future = self.collection._api.delete_table(
            self.database.name, self.schema.name, self.name, if_exists, async_req=True)
        return PollingOperations.empty(future)

    @api_telemetry
    @deprecated("undrop")
    def undelete(self) -> None:
        """Undelete the previously deleted table.

        Examples
        ________
        Undeleting a table using its reference:

        >>> table_ref.delete()
        >>> table_ref.undelete()
        """
        self.undrop()

    @api_telemetry
    def undrop(self) -> None:
        """Undrop the previously dropped table.

        Examples
        ________
        Undropping a table using its reference:

        >>> table_ref.drop()
        >>> table_ref.undrop()
        """
        self.collection._api.undrop_table(self.database.name, self.schema.name, self.name, async_req=False)

    @api_telemetry
    def undrop_async(self) -> PollingOperation[None]:
        """An asynchronous version of :func:`undrop`.

        Refer to :class:`~snowflake.core.PollingOperation` for more information on asynchronous execution and
        the return type.
        """ # noqa: D401
        future = self.collection._api.undrop_table(self.database.name, self.schema.name, self.name, async_req=True)
        return PollingOperations.empty(future)

    @api_telemetry
    def swap_with(
        self,
        to_swap_table_name: str,
        if_exists: Optional[bool] = None,
        target_database: Optional[str] = None,
        target_schema: Optional[str] = None,
    ) -> None:
        """Swap the name with another table.

        Parameters
        __________
        to_swap_table_name: str
            The name of the table we should swap the current table with.
        if_exists: bool, optional
            Check the existence of this table before swapping its name.
            Default is ``None``, which is equivalent to ``False``.
        target_database: str, optional
            The name of the database where the table to be swapped with exists. The default is ``None``,
            which means the current database.
        target_schema: str, optional
            The name of the schema where the table to be swapped with exists. The default is ``None``,
            which means the current schema.

        Examples
        ________
        Swapping ``my_table`` with ``other_table`` in the same schema:

        >>> my_table = my_schema.tables["my_table"].swap("other_table")
        """
        self.collection._api.swap_with_table(
            self.database.name,
            self.schema.name,
            self.name,
            to_swap_table_name,
            if_exists=if_exists,
            target_database=target_database,
            target_schema=target_schema,
            async_req=False,
        )

    @api_telemetry
    def swap_with_async(
        self,
        to_swap_table_name: str,
        if_exists: Optional[bool] = None,
        target_database: Optional[str] = None,
        target_schema: Optional[str] = None,
    ) -> PollingOperation[None]:
        """An asynchronous version of :func:`swap_with`.

        Refer to :class:`~snowflake.core.PollingOperation` for more information on asynchronous execution and
        the return type.
        """ # noqa: D401
        future = self.collection._api.swap_with_table(
            self.database.name,
            self.schema.name,
            self.name,
            to_swap_table_name,
            if_exists=if_exists,
            target_database=target_database,
            target_schema=target_schema,
            async_req=True,
        )
        return PollingOperations.empty(future)

    @api_telemetry
    def suspend_recluster(self, if_exists: Optional[bool] = None) -> None:
        """Suspend reclustering for this table.

        Parameters
        __________
        if_exists: bool, optional
            Check the existence of this table before suspending its recluster.
            Default is ``None``, which is equivalent to ``False``.

        Examples
        ________
        Suspending reclustering for a table using its reference:

        >>> my_schema.tables["my_table"].suspend_recluster()
        """
        self.collection._api.suspend_recluster_table(
            self.database.name, self.schema.name, self.name, if_exists, async_req=False
        )

    @api_telemetry
    def suspend_recluster_async(self, if_exists: Optional[bool] = None) -> PollingOperation[None]:
        """An asynchronous version of :func:`suspend_recluster`.

        Refer to :class:`~snowflake.core.PollingOperation` for more information on asynchronous execution and
        the return type.
        """ # noqa: D401
        future = self.collection._api.suspend_recluster_table(
            self.database.name, self.schema.name, self.name, if_exists, async_req=True
        )
        return PollingOperations.empty(future)

    @api_telemetry
    def resume_recluster(self, if_exists: Optional[bool] = None) -> None:
        """Resume reclustering for this table.

        Parameters
        __________
        if_exists: bool, optional
            Check the existence of this table before resuming its recluster.
            Default is ``None``, which is equivalent to ``False``.

        Examples
        ________
        Resume reclustering for a table using its reference`:

        >>> my_schema.tables["my_table"].resume_recluster()
        """
        self.collection._api.resume_recluster_table(
            self.database.name, self.schema.name, self.name, if_exists, async_req=False
        )

    @api_telemetry
    def resume_recluster_async(self, if_exists: Optional[bool] = None) -> PollingOperation[None]:
        """An asynchronous version of :func:`resume_recluster`.

        Refer to :class:`~snowflake.core.PollingOperation` for more information on asynchronous execution and
        the return type.
        """ # noqa: D401
        future = self.collection._api.resume_recluster_table(
            self.database.name, self.schema.name, self.name, if_exists, async_req=True
        )
        return PollingOperations.empty(future)
