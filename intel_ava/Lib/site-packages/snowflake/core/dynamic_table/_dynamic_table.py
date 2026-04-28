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
from ._generated.api import DynamicTableApi
from ._generated.api_client import StoredProcApiClient
from ._generated.models.dynamic_table import DynamicTable
from ._generated.models.dynamic_table_clone import DynamicTableClone
from ._generated.models.point_of_time import PointOfTime as TablePointOfTime


if TYPE_CHECKING:
    from snowflake.core.schema import SchemaResource


class DynamicTableCollection(SchemaObjectCollectionParent["DynamicTableResource"]):
    """Represents the collection operations on the Snowflake Dynamic Table resource.

    With this collection, you can create, iterate through, and search for dynamic tables that you have access to in the
    current context.

    Examples
    ________
    Creating a dynamic table instance:

    >>> dynamic_tables = root.databases["my_db"].schemas["my_schema"].dynamic_tables
    >>> dynamic_tables.create(
    ...     DynamicTable(
    ...        name="my_dynamic_table",
    ...        columns=[
    ...            DynamicTableColumn(name="c1"),
    ...            DynamicTableColumn(name='"cc2"', datatype="varchar"),
    ...        ],
    ...        warehouse=db_parameters["my_warehouse"],
    ...        target_lag=UserDefinedLag(seconds=60),
    ...        query="SELECT * FROM my_table",
    ...    ),
    ...    mode=CreateMode.error_if_exists,
    ... )
    """

    def __init__(self, schema: "SchemaResource"):
        super().__init__(schema, DynamicTableResource)
        self._api = DynamicTableApi(
            root=self.root,
            resource_class=self._ref_class,
            sproc_client=StoredProcApiClient(root=self.root)
        )

    @api_telemetry
    def create(
        self, table: Union[DynamicTable, DynamicTableClone, str],
        *,
        clone_table: Optional[Union[str, Clone]] = None,
        copy_grants: Optional[bool] = False,
        mode: CreateMode=CreateMode.error_if_exists,
    ) -> "DynamicTableResource":
        """Create a dynamic table.

        Parameters
        __________
        table: DynamicTable | DynamicTableClone | str
            1. The ``DynamicTable`` object, together with the dynamic table's properties:
                name, target_lag, warehouse, query;
                columns, refresh_mode, initialize, cluster_by, comment are optional.
            2. The ``DynamicTableClone`` object, when it's used with `clone_table`.
            3. The table name.
        clone_table: Clone, optional
            The source table to clone from.
        copy_grants: bool, optional
            Whether to enable copy grants when creating the object. Default is ``False``.
        mode: CreateMode, optional
            One of the following enum values.

            ``CreateMode.error_if_exists``: Throw an :class:`snowflake.core.exceptions.ConflictError`
            if the dynamic table already exists in Snowflake.  Equivalent to SQL ``create dynamic table <name> ...``.

            ``CreateMode.or_replace``: Replace if the dynamic table already exists in Snowflake. Equivalent to SQL
            ``create or replace dynamic table <name> ...``.

            ``CreateMode.if_not_exists``: Do nothing if the dynamic table already exists in Snowflake.
            Equivalent to SQL ``create dynamic table <name> if not exists...``

            Default is ``CreateMode.error_if_exists``.

        Examples
        ________
        Creating a dynamic table, replacing any existing dynamic table with the same name:

        >>> dynamic_tables = root.databases["my_db"].schemas["my_schema"].dynamic_tables
        >>> dynamic_tables.create(
        ...     DynamicTable(
        ...        name="my_dynamic_table",
        ...        columns=[
        ...            DynamicTableColumn(name="c1"),
        ...            DynamicTableColumn(name='"cc2"', datatype="varchar"),
        ...        ],
        ...        warehouse=db_parameters["my_warehouse"],
        ...        target_lag=UserDefinedLag(seconds=60),
        ...        query="SELECT * FROM my_table",
        ...    ),
        ...    mode=CreateMode.error_if_exists,
        ... )

        Creating a dynamic table by cloning an existing table:

        >>> dynamic_tables = root.databases["my_db"].schemas["my_schema"].dynamic_tables
        >>> dynamic_tables.create(
        ...     DynamicTableClone(
        ...         name="my_dynamic_table",
        ...         target_lag=UserDefinedLag(seconds=120),
        ...     ),
        ...     clone_table=Clone(
        ...         source="my_source_dynamic_table",
        ...         point_of_time=PointOfTimeOffset(reference="before", when="-1")
        ...     ),
        ...     copy_grants=True,
        ... )

        Creating a dynamic table by cloning an existing table in a different database and schema:

        >>> dynamic_tables = root.databases["my_db"].schemas["my_schema"].dynamic_tables
        >>> dynamic_tables.create(
        ...     DynamicTableClone(
        ...         name="my_dynamic_table",
        ...         target_lag=UserDefinedLag(seconds=120),
        ...     ),
        ...     clone_table=Clone(
        ...         source="database_of_source_table.schema_of_source_table.my_source_dynamic_table",
        ...         point_of_time=PointOfTimeOffset(reference="before", when="-1")
        ...     ),
        ...     copy_grants=True,
        ... )
        """
        self._create(
            table=table,
            clone_table=clone_table,
            copy_grants=copy_grants,
            mode=mode,
            async_req=False,
        )
        return DynamicTableResource(table if isinstance(table, str) else table.name, self)

    @api_telemetry
    def create_async(
        self, table: Union[DynamicTable, DynamicTableClone, str],
        *,
        clone_table: Optional[Union[str, Clone]] = None,
        copy_grants: Optional[bool] = False,
        mode: CreateMode=CreateMode.error_if_exists,
    ) -> PollingOperation["DynamicTableResource"]:
        """An asynchronous version of :func:`create`.

        Refer to :class:`~snowflake.core.PollingOperation` for more information on asynchronous execution and
        the return type.
        """ # noqa: D401
        future = self._create(
            table=table,
            clone_table=clone_table,
            copy_grants=copy_grants,
            mode=mode,
            async_req=True,
        )
        return PollingOperation(future, lambda _:
            DynamicTableResource(table if isinstance(table, str) else table.name, self))

    @api_telemetry
    def iter(
        self,
        *,
        like: Optional[str] = None,
        starts_with: Optional[str] = None,
        limit: Optional[int] = None,
        from_name: Optional[str] = None,
        deep: bool = False,
    ) -> Iterator[DynamicTable]:
        """Iterate through ``DynamicTable`` objects from Snowflake, filtering on any optional 'like' pattern.

        Parameters
        _________
        like: str, optional
            A case-insensitive string functioning as a filter, with support for SQL
            wildcard characters (% and _).
        starts_with: str, optional
            String used to filter the command output based on the string of characters that appear
            at the beginning of the object name. Uses case-sensitive pattern matching.
        show_limit: int, optional
            Limit of the maximum number of rows returned by iter(). The default is ``None``, which behaves equivalently
            to show_limit=10000. This value must be between ``1`` and ``10000``.
        from_name: str, optional
            Fetch rows only following the first row whose object name matches
            the specified string. This is case-sensitive and does not have to be the full name.
        deep: bool, optional
            Optionally includes dependency information of the dynamic table. Default is ``None``,
            which is equivalent to ``False``.

        Examples
        ________
        Showing all dynamic tables that you have access to see:

        >>> dynamic_tables = dynamic_table_collection.iter()

        Showing information of the exact dynamic table you want to see:

        >>> dynamic_tables = dynamic_table_collection.iter(like="your-dynamic-table-name")

        Showing dynamic tables starting with 'your-dynamic-table-name-':

        >>> dynamic_tables = dynamic_table_collection.iter(like="your-dynamic-table-name-%")

        Using a for loop to retrieve information from iterator:

        >>> for dynamic_table in dynamic_tables:
        ...     print(dynamic_table.name, dynamic_table.query)
        """
        tables = self._api.list_dynamic_tables(
            database=self.database.name, var_schema=self.schema.name, like=like,
            starts_with=starts_with, show_limit=limit, from_name=from_name, deep=deep,
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
        deep: bool = False,
    ) -> PollingOperation[Iterator[DynamicTable]]:
        """An asynchronous version of :func:`iter`.

        Refer to :class:`~snowflake.core.PollingOperation` for more information on asynchronous execution and
        the return type.
        """ # noqa: D401
        future = self._api.list_dynamic_tables(
            database=self.database.name, var_schema=self.schema.name, like=like,
            starts_with=starts_with, show_limit=limit, from_name=from_name, deep=deep,
            async_req=True,
        )
        return PollingOperations.iterator(future)

    @overload
    def _create(
        self, table: Union[DynamicTable, DynamicTableClone, str],
        clone_table: Optional[Union[str, Clone]],
        copy_grants: Optional[bool],
        mode: CreateMode,
        async_req: Literal[True],
    ) -> Future[SuccessResponse]:
        ...

    @overload
    def _create(
        self, table: Union[DynamicTable, DynamicTableClone, str],
        clone_table: Optional[Union[str, Clone]],
        copy_grants: Optional[bool],
        mode: CreateMode,
        async_req: Literal[False],
    ) -> SuccessResponse:
        ...

    def _create(
        self, table: Union[DynamicTable, DynamicTableClone, str],
        clone_table: Optional[Union[str, Clone]],
        copy_grants: Optional[bool],
        mode: CreateMode,
        async_req: bool,
    ) -> Union[SuccessResponse, Future[SuccessResponse]]:
        real_mode = CreateMode[mode].value

        if clone_table:
            # create table by clone

            if isinstance(table, str):
                table = DynamicTableClone(name=table)

            pot: Optional[TablePointOfTime] = None
            if isinstance(clone_table, Clone) and isinstance(clone_table.point_of_time, PointOfTime):
                pot = TablePointOfTime.from_dict(clone_table.point_of_time.to_dict())
            real_clone = Clone(source=clone_table) if isinstance(clone_table, str) else clone_table
            req = DynamicTableClone(
                name=table.name,
                target_lag=table.target_lag,
                warehouse=table.warehouse,
                point_of_time=pot,
            )

            source_table_fqn = FQN.from_string(real_clone.source)
            return self._api.clone_dynamic_table(
                source_table_fqn.database or self.database.name,
                source_table_fqn.schema or self.schema.name,
                source_table_fqn.name,
                req, create_mode=StrictStr(real_mode),
                copy_grants=copy_grants,
                target_database=self.database.name,
                target_schema=self.schema.name,
                async_req=async_req,
            )

        # create empty table

        if not isinstance(table, DynamicTable):
            raise ValueError("`table` must be a `DynamicTable` unless `clone_table` is used")

        return self._api.create_dynamic_table(
            self.database.name, self.schema.name, table, create_mode=StrictStr(real_mode),
            async_req=async_req,
        )


class DynamicTableResource(SchemaObjectReferenceMixin[DynamicTableCollection]):
    """Represents a reference to a Snowflake dynamic table.

    With this dynamic table reference, you can create, drop, undrop, suspend, resume, swap_with other table,
    suspend recluster, resume recluster and fetch information about dynamic tables, as well
    as perform certain actions on them.
    """

    _supports_rest_api = True

    def __init__(self, name: str, collection: DynamicTableCollection) -> None:
        self.collection = collection
        self.name = name

    @api_telemetry
    def fetch(self) -> DynamicTable:
        """Fetch the details of a dynamic table.

        Examples
        ________
        Fetching a reference to a dynamic table to print its name and query properties:

        >>> my_dynamic_table = dynamic_table_reference.fetch()
        >>> print(my_dynamic_table.name, my_dynamic_table.query)
        """
        return self.collection._api.fetch_dynamic_table(
            self.database.name, self.schema.name, self.name, async_req=False,
        )

    @api_telemetry
    def fetch_async(self) -> PollingOperation[DynamicTable]:
        """An asynchronous version of :func:`fetch`.

        Refer to :class:`~snowflake.core.PollingOperation` for more information on asynchronous execution and
        the return type.
        """ # noqa: D401
        future = self.collection._api.fetch_dynamic_table(
            self.database.name, self.schema.name, self.name, async_req=True,
        )
        return PollingOperations.identity(future)

    @api_telemetry
    @deprecated("drop")
    def delete(self) -> None:
        self.drop()

    @api_telemetry
    def drop(
        self,
        if_exists: Optional[bool] = None,
    ) -> None:
        """Drop the dynamic table.

        Parameters
        __________
        if_exists: bool, optional
            Check the existence of this dynamic table before dropping it.
            Default is ``None``, which is equivalent to ``False``.

        Examples
        ________
        Deleting a dynamic table using its reference:

        >>> dynamic_table_reference.drop()
        """
        self.collection._api.delete_dynamic_table(
            self.database.name,
            self.schema.name,
            self.name,
            if_exists=if_exists,
            async_req=False,
        )

    @api_telemetry
    def drop_async(self, if_exists: Optional[bool] = None) -> PollingOperation[None]:
        """An asynchronous version of :func:`drop`.

        Refer to :class:`~snowflake.core.PollingOperation` for more information on asynchronous execution and
        the return type.
        """ # noqa: D401
        future = self.collection._api.delete_dynamic_table(
            self.database.name,
            self.schema.name,
            self.name,
            if_exists=if_exists,
            async_req=True,
        )
        return PollingOperations.empty(future)

    @api_telemetry
    @deprecated("undrop")
    def undelete(self) -> None:
        self.undrop()

    @api_telemetry
    def undrop(
        self,
    ) -> None:
        """Undrop the previously dropped dynamic table.

        Examples
        ________
        Reverting delete a dynamic table using its reference:

        >>> dynamic_table_reference.undrop()
        """
        self.collection._api.undrop_dynamic_table(
            self.database.name,
            self.schema.name,
            self.name,
            async_req=False,
        )

    @api_telemetry
    def undrop_async(self) -> PollingOperation[None]:
        """An asynchronous version of :func:`undrop`.

        Refer to :class:`~snowflake.core.PollingOperation` for more information on asynchronous execution and
        the return type.
        """ # noqa: D401
        future = self.collection._api.undrop_dynamic_table(
            self.database.name,
            self.schema.name,
            self.name,
            async_req=True,
        )
        return PollingOperations.empty(future)

    @api_telemetry
    def suspend(
        self,
        if_exists: Optional[bool] = None,
    ) -> None:
        """Suspend the dynamic table.

        Parameters
        __________
        if_exists: bool, optional
            Check the existence of this dynamic table before suspending it.
            Default is ``None``, which is equivalent to ``False``.

        Examples
        ________
        Suspending a dynamic table using its reference:

        >>> dynamic_table_reference.suspend()
        """
        self.collection._api.suspend_dynamic_table(
            self.database.name,
            self.schema.name,
            self.name,
            if_exists=if_exists,
            async_req=False,
        )

    @api_telemetry
    def suspend_async(self, if_exists: Optional[bool] = None) -> PollingOperation[None]:
        """An asynchronous version of :func:`suspend`.

        Refer to :class:`~snowflake.core.PollingOperation` for more information on asynchronous execution and
        the return type.
        """ # noqa: D401
        future = self.collection._api.suspend_dynamic_table(
            self.database.name,
            self.schema.name,
            self.name,
            if_exists=if_exists,
            async_req=True,
        )
        return PollingOperations.empty(future)

    @api_telemetry
    def resume(
        self,
        if_exists: Optional[bool] = None,
    ) -> None:
        """Resume the dynamic table.

        Parameters
        __________
        if_exists: bool, optional
            Check the existence of this dynamic table before resuming it.
            Default is ``None``, which is equivalent to ``False``.

        Examples
        ________
        Resuming a dynamic table using its reference:

        >>> dynamic_table_reference.resume()
        """
        self.collection._api.resume_dynamic_table(
            self.database.name,
            self.schema.name,
            self.name,
            if_exists=if_exists,
            async_req=False,
        )

    @api_telemetry
    def resume_async(self, if_exists: Optional[bool] = None) -> PollingOperation[None]:
        """An asynchronous version of :func:`resume`.

        Refer to :class:`~snowflake.core.PollingOperation` for more information on asynchronous execution and
        the return type.
        """ # noqa: D401
        future = self.collection._api.resume_dynamic_table(
            self.database.name,
            self.schema.name,
            self.name,
            if_exists=if_exists,
            async_req=True,
        )
        return PollingOperations.empty(future)

    @api_telemetry
    def refresh(
        self,
        if_exists: Optional[bool] = None,
    ) -> None:
        """Refresh the dynamic table.

        Parameters
        __________
        if_exists: bool, optional
            Check the existence of this dynamic table before refreshing it.
            Default is ``None``, which is equivalent to ``False``.

        Examples
        ________
        Refreshing a dynamic table using its reference:

        >>> dynamic_table_reference.refresh()
        """
        self.collection._api.refresh_dynamic_table(
            self.database.name,
            self.schema.name,
            self.name,
            if_exists=if_exists,
            async_req=False,
        )

    @api_telemetry
    def refresh_async(self, if_exists: Optional[bool] = None) -> PollingOperation[None]:
        """An asynchronous version of :func:`refresh`.

        Refer to :class:`~snowflake.core.PollingOperation` for more information on asynchronous execution and
        the return type.
        """ # noqa: D401
        future = self.collection._api.refresh_dynamic_table(
            self.database.name,
            self.schema.name,
            self.name,
            if_exists=if_exists,
            async_req=True,
        )
        return PollingOperations.empty(future)

    @api_telemetry
    def swap_with(
        self,
        to_swap_table_name: str,
        if_exists: Optional[bool] = None,
    ) -> None:
        """Swap the name with another dynamic table.

        Parameters
        __________
        to_swap_table_name: str
            The name of the table to swap with.

        if_exists: bool, optional
            Check the existence of this dynamic table before swapping its name.
            Default is ``None``, which is equivalent to ``False``.

        Examples
        ________
        Swaping name with another dynamic table using its reference:

        >>> dynamic_table_reference.swap_with("my_other_dynamic_table")
        """
        self.collection._api.swap_with_dynamic_table(
            self.database.name,
            self.schema.name,
            self.name,
            to_swap_table_name,
            if_exists=if_exists,
            async_req=False,
        )

    @api_telemetry
    def swap_with_async(
        self,
        to_swap_table_name: str,
        if_exists: Optional[bool] = None,
    ) -> PollingOperation[None]:
        """An asynchronous version of :func:`swap_with`.

        Refer to :class:`~snowflake.core.PollingOperation` for more information on asynchronous execution and
        the return type.
        """ # noqa: D401
        future = self.collection._api.swap_with_dynamic_table(
            self.database.name,
            self.schema.name,
            self.name,
            to_swap_table_name,
            if_exists=if_exists,
            async_req=True,
        )
        return PollingOperations.empty(future)

    @api_telemetry
    def suspend_recluster(
        self,
        if_exists: Optional[bool] = None,
    ) -> None:
        """Disable reclustering the dynamic table.

        Parameters
        __________
        if_exists: bool, optional
            Check the existence of this dynamic table before suspending its recluster.
            Default is ``None``, which is equivalent to ``False``.

        Examples
        ________
        Disable reclustering a dynamic table using its reference:

        >>> dynamic_table_reference.suspend_recluster()
        """
        self.collection._api.suspend_recluster_dynamic_table(
            self.database.name,
            self.schema.name,
            self.name,
            if_exists=if_exists,
            async_req=False,
        )

    @api_telemetry
    def suspend_recluster_async(
        self,
        if_exists: Optional[bool] = None,
    ) -> PollingOperation[None]:
        """An asynchronous version of :func:`suspend_recluster`.

        Refer to :class:`~snowflake.core.PollingOperation` for more information on asynchronous execution and
        the return type.
        """ # noqa: D401
        future = self.collection._api.suspend_recluster_dynamic_table(
            self.database.name,
            self.schema.name,
            self.name,
            if_exists=if_exists,
            async_req=True,
        )
        return PollingOperations.empty(future)

    @api_telemetry
    def resume_recluster(
        self,
        if_exists: Optional[bool] = None,
    ) -> None:
        """Enable reclustering the dynamic table.

        Parameters
        __________
        if_exists: bool, optional
            Check the existence of this dynamic table before resuming its recluster.
            Default is ``None``, which is equivalent to ``False``.

        Examples
        ________
        Enable reclustering a dynamic table using its reference:

        >>> dynamic_table_reference.resume_recluster()
        """
        self.collection._api.resume_recluster_dynamic_table(
            self.database.name,
            self.schema.name,
            self.name,
            if_exists=if_exists,
            async_req=False,
        )

    @api_telemetry
    def resume_recluster_async(
        self,
        if_exists: Optional[bool] = None,
    ) -> PollingOperation[None]:
        """An asynchronous version of :func:`resume_recluster`.

        Refer to :class:`~snowflake.core.PollingOperation` for more information on asynchronous execution and
        the return type.
        """ # noqa: D401
        future = self.collection._api.resume_recluster_dynamic_table(
            self.database.name,
            self.schema.name,
            self.name,
            if_exists=if_exists,
            async_req=True,
        )
        return PollingOperations.empty(future)
