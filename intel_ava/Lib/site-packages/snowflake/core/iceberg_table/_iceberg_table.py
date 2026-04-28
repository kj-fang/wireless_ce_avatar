from collections.abc import Iterator
from concurrent.futures import Future
from typing import TYPE_CHECKING, Annotated, Literal, Optional, Union, overload

from pydantic import Field, StrictStr

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
from ._generated import ConvertToManagedIcebergTableRequest, RefreshIcebergTableRequest, SuccessResponse
from ._generated.api import IcebergTableApi
from ._generated.api_client import StoredProcApiClient
from ._generated.models.iceberg_table import IcebergTable
from ._generated.models.iceberg_table_as_select import IcebergTableAsSelect
from ._generated.models.iceberg_table_clone import IcebergTableClone
from ._generated.models.iceberg_table_from_aws_glue_catalog import IcebergTableFromAWSGlueCatalog
from ._generated.models.iceberg_table_from_delta import IcebergTableFromDelta
from ._generated.models.iceberg_table_from_iceberg_files import IcebergTableFromIcebergFiles
from ._generated.models.iceberg_table_from_iceberg_rest import IcebergTableFromIcebergRest
from ._generated.models.iceberg_table_like import IcebergTableLike
from ._generated.models.point_of_time import PointOfTime as IcebergTablePointOfTime


if TYPE_CHECKING:
    from snowflake.core.schema import SchemaResource


class IcebergTableCollection(SchemaObjectCollectionParent["IcebergTableResource"]):
    """Represents the collection operations on the Snowflake Iceberg table resource.

    With this collection, you can create, iterate through, and search for Iceberg tables that you
    have access to in the current context.

    Examples
    ________
    Creating an IcebergTable instance:

    >>> iceberg_tables = root.databases["my_db"].schemas["my_schema"].iceberg_tables
    >>> new_iceberg_table = IcebergTable(
    ...     name="name",
    ...     columns=[IcebergTableColumn(name="col1", datatype="string")],
    ... )
    >>> iceberg_tables.create(new_iceberg_table)
    """

    def __init__(self, schema: "SchemaResource"):
        super().__init__(schema, IcebergTableResource)
        self._api = IcebergTableApi(
            root=self.root,
            resource_class=self._ref_class,
            sproc_client=StoredProcApiClient(root=self.root)
        )

    @overload
    @api_telemetry
    def create(
            self,
            iceberg_table: IcebergTable,
            *,
            copy_grants: Optional[bool] = False,
            mode: CreateMode = CreateMode.error_if_exists,
    ) -> "IcebergTableResource":
        ...

    @overload
    @api_telemetry
    def create(
        self,
        iceberg_table: IcebergTable,
        *,
        as_select: Optional[str] = None,
        copy_grants: Optional[bool] = False,
        mode: CreateMode = CreateMode.error_if_exists,
    ) -> "IcebergTableResource":
        ...

    @overload
    @api_telemetry
    def create(
        self,
        iceberg_table: IcebergTable,
        *,
        like: str,
        copy_grants: Optional[bool] = False,
        mode: CreateMode = CreateMode.error_if_exists,
    ) -> "IcebergTableResource":
        ...

    @overload
    @api_telemetry
    def create(
        self,
        iceberg_table: IcebergTable,
        *,
        from_aws_glue_catalog: Literal[True] = True,
        mode: CreateMode = CreateMode.error_if_exists,
    ) -> "IcebergTableResource":
        ...

    @overload
    @api_telemetry
    def create(
        self,
        iceberg_table: IcebergTable,
        *,
        from_delta: Literal[True] = True,
        mode: CreateMode = CreateMode.error_if_exists,
    ) -> "IcebergTableResource":
        ...

    @overload
    @api_telemetry
    def create(
        self,
        iceberg_table: IcebergTable,
        *,
        from_iceberg_files: Literal[True] = True,
        mode: CreateMode = CreateMode.error_if_exists,
    ) -> "IcebergTableResource":
        ...

    @overload
    @api_telemetry
    def create(
        self,
        iceberg_table: IcebergTable,
        *,
        from_iceberg_rest: Literal[True] = True,
        mode: CreateMode = CreateMode.error_if_exists,
    ) -> "IcebergTableResource":
        ...

    @overload
    @api_telemetry
    def create(
        self,
        iceberg_table: IcebergTable,
        *,
        clone_iceberg_table: Union[str, Clone],
        copy_grants: Optional[bool] = False,
        mode: CreateMode = CreateMode.error_if_exists,
    ) -> "IcebergTableResource":
        ...

    @api_telemetry
    def create(
        self,
        iceberg_table: IcebergTable,
        *,
        as_select: Optional[str] = None,
        like: Optional[str] = None,
        from_aws_glue_catalog: Optional[bool] = False,
        from_delta: Optional[bool] = False,
        from_iceberg_files: Optional[bool] = False,
        from_iceberg_rest: Optional[bool] = False,
        clone_iceberg_table: Optional[Union[str, Clone]] = None,
        copy_grants: Optional[bool] = False,
        mode: CreateMode = CreateMode.error_if_exists,
    ) -> "IcebergTableResource":
        """Create an Iceberg table in Snowflake.

        There are multiple ways to create an Iceberg table:
            * by building from scratch
            * by creating from select query
            * by creating it from AWS Glue Catalog or Delta catalog
            * by creating it from files
            * by creating it from REST API
            * by cloning existing table

        Parameters
        __________
        iceberg_table: Union[IcebergTable, str]
            The new Iceberg table's name.
        as_select: str
            Creates an Iceberg table using a select query.
        like: str
            Create a new Iceberg table like the specified one, but empty.
        from_aws_glue_catalog: IcebergTableFromAwsGlueCatalog
            Creates an Iceberg table from AWS Glue Catalog.
        from_delta: IcebergTableFromDelta
            Creates an Iceberg table from Delta.
        from_iceberg_files: IcebergTableFromIcebergFiles
            Creates an Iceberg table using Iceberg files in object storage (external cloud storage).
        from_iceberg_rest: IcebergTableFromIcebergRest
            Creates an Iceberg in Iceberg REST catalog.
        clone_iceberg_table: str or Clone object
            The name of Iceberg table to be cloned, or a ``Clone`` object which would contain the
            name of the Iceberg table with support to clone at a specific time.
        copy_grants: bool, optional
            Copy grants when resource is created.
        mode: CreateMode, optional
            One of the following enum values:

            ``CreateMode.error_if_exists``: Throw an :class:`snowflake.core.exceptions.ConflictError`
            if the Iceberg table already exists in Snowflake.  Equivalent to SQL ``create Iceberg table <name> ...``.

            ``CreateMode.or_replace``: Replace if the Iceberg table already exists in Snowflake. Equivalent to SQL
            ``create or replace Iceberg table <name> ...``.

            ``CreateMode.if_not_exists``: Do nothing if the Iceberg table already exists in Snowflake.
            Equivalent to SQL ``create Iceberg table <name> if not exists...``

            Default is ``CreateMode.error_if_exists``.

        Examples
        ________
        Creating an Iceberg table, replacing any existing Iceberg table with the same name:

        >>> iceberg_tables = root.databases["my_db"].schemas["my_schema"].iceberg_tables
        >>> new_iceberg_table = IcebergTable(
        ...     name="name",
        ...     columns=[IcebergTableColumn(name="col1", datatype="string")],
        ... )
        >>> iceberg_tables.create(new_iceberg_table, mode=CreateMode.or_replace)

        Cloning an Iceberg table instance:

        >>> iceberg_tables = root.databases["my_db"].schemas["my_schema"].iceberg_tables
        >>> iceberg_tables.create(
        ...     IcebergTable(name="new_table"),
        ...     clone_iceberg_table="iceberg_table_name_to_be_cloned",
        ...     mode=CreateMode.if_not_exists,
        ... )
        """
        self._create(
            iceberg_table=iceberg_table,
            as_select=as_select,
            like=like,
            from_aws_glue_catalog=from_aws_glue_catalog,
            from_delta=from_delta,
            from_iceberg_files=from_iceberg_files,
            from_iceberg_rest=from_iceberg_rest,
            clone_iceberg_table=clone_iceberg_table,
            copy_grants=copy_grants,
            mode=mode,
            async_req=False,
        )
        return IcebergTableResource(iceberg_table.name, self)

    @api_telemetry
    def create_async(
        self,
        iceberg_table: IcebergTable,
        *,
        as_select: Optional[str] = None,
        like: Optional[str] = None,
        from_aws_glue_catalog: Optional[bool] = False,
        from_delta: Optional[bool] = False,
        from_iceberg_files: Optional[bool] = False,
        from_iceberg_rest: Optional[bool] = False,
        clone_iceberg_table: Optional[Union[str, Clone]] = None,
        copy_grants: Optional[bool] = False,
        mode: CreateMode = CreateMode.error_if_exists,
    ) -> PollingOperation["IcebergTableResource"]:
        """An asynchronous version of :func:`create`.

        Refer to :class:`~snowflake.core.PollingOperation` for more information on asynchronous execution and
        the return type.
        """ # noqa: D401
        future = self._create(
            iceberg_table=iceberg_table,
            as_select=as_select,
            like=like,
            from_aws_glue_catalog=from_aws_glue_catalog,
            from_delta=from_delta,
            from_iceberg_files=from_iceberg_files,
            from_iceberg_rest=from_iceberg_rest,
            clone_iceberg_table=clone_iceberg_table,
            copy_grants=copy_grants,
            mode=mode,
            async_req=True,
        )
        return PollingOperation(future, lambda _: IcebergTableResource(iceberg_table.name, self))

    @api_telemetry
    def iter(
        self,
        *,
        like: Optional[StrictStr] = None,
        starts_with: Optional[StrictStr] = None,
        show_limit: Optional[Annotated[int, Field(le=10000, strict=True, ge=1)]] = None,
        from_name: Optional[StrictStr] = None,
    ) -> Iterator[IcebergTable]:
        """Iterate through ``IcebergTable`` objects from Snowflake, filtering on any optional 'like' pattern.

        Parameters
        __________
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

        Examples
        ________
        Showing all Iceberg tables that you have access to see:

        >>> iceberg_tables = iceberg_table_collection.iter()

        Showing information of the exact Iceberg table you want to see:

        >>> iceberg_tables = iceberg_table_collection.iter(like="your-iceberg-table-name")

        Showing Iceberg tables starting with 'your-iceberg-table-name-':

        >>> iceberg_tables = iceberg_table_collection.iter(like="your-iceberg-table-name-%")

        Using a for loop to retrieve information from iterator:

        >>> for iceberg_table in iceberg_tables:
        ...     print(iceberg_table.name)
        """
        iceberg_tables = self._api.list_iceberg_tables(
            database=self.database.name,
            var_schema=self.schema.name,
            like=like,
            starts_with=starts_with,
            show_limit=show_limit,
            from_name=from_name,
            async_req=False,
        )
        return iter(iceberg_tables)

    @api_telemetry
    def iter_async(
        self,
        *,
        like: Optional[StrictStr] = None,
        starts_with: Optional[StrictStr] = None,
        show_limit: Optional[Annotated[int, Field(le=10000, strict=True, ge=1)]] = None,
        from_name: Optional[StrictStr] = None,
    ) -> PollingOperation[Iterator[IcebergTable]]:
        """An asynchronous version of :func:`iter`.

        Refer to :class:`~snowflake.core.PollingOperation` for more information on asynchronous execution and
        the return type.
        """ # noqa: D401
        future = self._api.list_iceberg_tables(
            database=self.database.name,
            var_schema=self.schema.name,
            like=like,
            starts_with=starts_with,
            show_limit=show_limit,
            from_name=from_name,
            async_req=True,
        )
        return PollingOperations.iterator(future)

    @staticmethod
    def _validate_iceberg_table(
            iceberg_table: Union[IcebergTable, str],
            as_select: Optional[str] = None,
            like: Optional[str] = None,
            from_aws_glue_catalog: Optional[bool] = None,
            from_delta: Optional[bool] = None,
            from_iceberg_files: Optional[bool] = None,
            from_iceberg_rest: Optional[bool] = None,
            clone_iceberg_table: Optional[Union[str, Clone]] = None,
    ) -> None:
        if not isinstance(iceberg_table, IcebergTable):
            raise TypeError("iceberg_table has to be IcebergTable object")

        options_map = {
            "as_select": as_select,
            "like": like,
            "from_aws_glue_catalog": from_aws_glue_catalog,
            "from_delta": from_delta,
            "from_iceberg_files": from_iceberg_files,
            "from_iceberg_rest": from_iceberg_rest,
            "clone_iceberg_table": clone_iceberg_table,
        }
        if len([o for o in options_map.values() if o]) > 1:
            exclusive_options = ", ".join(k for k, v in options_map.items() if v)
            msg = f"Options {exclusive_options} are mutually exclusive"
            raise ValueError(msg)

    @overload
    def _create(
        self,
        iceberg_table: IcebergTable,
        as_select: Optional[str],
        like: Optional[str],
        from_aws_glue_catalog: Optional[bool],
        from_delta: Optional[bool],
        from_iceberg_files: Optional[bool],
        from_iceberg_rest: Optional[bool],
        clone_iceberg_table: Optional[Union[str, Clone]],
        copy_grants: Optional[bool],
        mode: CreateMode,
        async_req: Literal[True],
    ) -> Future[SuccessResponse]:
        ...

    @overload
    def _create(
        self,
        iceberg_table: IcebergTable,
        as_select: Optional[str],
        like: Optional[str],
        from_aws_glue_catalog: Optional[bool],
        from_delta: Optional[bool],
        from_iceberg_files: Optional[bool],
        from_iceberg_rest: Optional[bool],
        clone_iceberg_table: Optional[Union[str, Clone]],
        copy_grants: Optional[bool],
        mode: CreateMode,
        async_req: Literal[False],
    ) -> SuccessResponse:
        ...

    def _create(
        self,
        iceberg_table: IcebergTable,
        as_select: Optional[str],
        like: Optional[str],
        from_aws_glue_catalog: Optional[bool],
        from_delta: Optional[bool],
        from_iceberg_files: Optional[bool],
        from_iceberg_rest: Optional[bool],
        clone_iceberg_table: Optional[Union[str, Clone]],
        copy_grants: Optional[bool],
        mode: CreateMode,
        async_req: bool,
    ) -> Union[SuccessResponse, Future[SuccessResponse]]:
        real_mode = CreateMode[mode].value

        self._validate_iceberg_table(
            iceberg_table=iceberg_table,
            as_select=as_select,
            like=like,
            from_aws_glue_catalog=from_aws_glue_catalog,
            from_delta=from_delta,
            from_iceberg_files=from_iceberg_files,
            from_iceberg_rest=from_iceberg_rest,
            clone_iceberg_table=clone_iceberg_table,
            )

        if as_select:
            return self._api.create_snowflake_managed_iceberg_table_as_select(
                database=self.database.name,
                var_schema=self.schema.name,
                query=as_select,
                iceberg_table_as_select=IcebergTableAsSelect(**iceberg_table.to_dict()),
                create_mode=StrictStr(real_mode),
                copy_grants=copy_grants,
                async_req=async_req,
            )
        if like:
            return self._api.create_snowflake_managed_iceberg_table_like(
                database=self.database.name,
                var_schema=self.schema.name,
                name=like,
                iceberg_table_like=IcebergTableLike(**iceberg_table.to_dict()),
                create_mode=StrictStr(real_mode),
                copy_grants=copy_grants,
                target_database=self.database.name,
                target_schema=self.schema.name,
                async_req=async_req,
            )
        if from_aws_glue_catalog:
            return self._api.create_unmanaged_iceberg_table_from_aws_glue_catalog(
                database=self.database.name,
                var_schema=self.schema.name,
                iceberg_table_from_aws_glue_catalog=IcebergTableFromAWSGlueCatalog(**iceberg_table.to_dict()),
                create_mode=StrictStr(real_mode),
                async_req=async_req,
            )
        if from_delta:
            return self._api.create_unmanaged_iceberg_table_from_delta(
                database=self.database.name,
                var_schema=self.schema.name,
                iceberg_table_from_delta=IcebergTableFromDelta(**iceberg_table.to_dict()),
                create_mode=StrictStr(real_mode),
                async_req=async_req,
            )
        if from_iceberg_files:
            return self._api.create_unmanaged_iceberg_table_from_iceberg_files(
                database=self.database.name,
                var_schema=self.schema.name,
                iceberg_table_from_iceberg_files=IcebergTableFromIcebergFiles(**iceberg_table.to_dict()),
                create_mode=StrictStr(real_mode),
                async_req=async_req,
            )
        if from_iceberg_rest:
            return self._api.create_unmanaged_iceberg_table_from_iceberg_rest(
                database=self.database.name,
                var_schema=self.schema.name,
                iceberg_table_from_iceberg_rest=IcebergTableFromIcebergRest(**iceberg_table.to_dict()),
                create_mode=StrictStr(real_mode),
                async_req=async_req,
            )
        if clone_iceberg_table:
            pot: Optional[IcebergTablePointOfTime] = None
            if isinstance(clone_iceberg_table, Clone) and isinstance(clone_iceberg_table.point_of_time, PointOfTime):
                pot = IcebergTablePointOfTime.from_dict(clone_iceberg_table.point_of_time.to_dict())

            real_clone = (Clone(source=clone_iceberg_table) if
                          isinstance(clone_iceberg_table, str) else clone_iceberg_table)
            req = IcebergTableClone(
                point_of_time=pot,
                name=iceberg_table.name,
            )

            source_iceberg_table_fqn = FQN.from_string(real_clone.source)
            return self._api.clone_snowflake_managed_iceberg_table(
                source_iceberg_table_fqn.database or self.database.name,
                source_iceberg_table_fqn.schema or self.schema.name,
                source_iceberg_table_fqn.name,
                iceberg_table_clone=req,
                create_mode=StrictStr(real_mode),
                async_req=async_req,
                target_database=self.database.name,
                target_schema=self.schema.name,
                copy_grants=copy_grants,
            )

        return self._api.create_snowflake_managed_iceberg_table(
            self.database.name,
            self.schema.name,
            iceberg_table,
            create_mode=StrictStr(real_mode),
            async_req=async_req,
            copy_grants=copy_grants,
        )


class IcebergTableResource(SchemaObjectReferenceMixin[IcebergTableCollection]):
    """Represents a reference to a Snowflake Iceberg table.

    With this Iceberg table reference, you can create, update, delete and fetch information
    about Iceberg tables, as well as perform certain actions on them.
    """

    def __init__(
        self,
        name: StrictStr,
        collection: IcebergTableCollection
    ) -> None:
        self.name = name
        self.collection = collection

    @api_telemetry
    def fetch(self) -> IcebergTable:
        """Fetch the details of an Iceberg table.

        Examples
        ________
        Fetching a reference to an Iceberg table to print its name:

        >>> iceberg_table_reference = root.databases["my_db"].schemas["my_schema"].iceberg_tables["foo"]
        >>> my_iceberg_table = iceberg_table_reference.fetch()
        >>> print(my_iceberg_table.name)
        """
        return self.collection._api.fetch_iceberg_table(
            self.database.name,
            self.schema.name,
            self.name,
            async_req=False,
        )

    @api_telemetry
    def fetch_async(self) -> PollingOperation[IcebergTable]:
        """An asynchronous version of :func:`fetch`.

        Refer to :class:`~snowflake.core.PollingOperation` for more information on asynchronous execution and
        the return type.
        """ # noqa: D401
        future = self.collection._api.fetch_iceberg_table(
            self.database.name,
            self.schema.name,
            self.name,
            async_req=True,
        )
        return PollingOperations.identity(future)

    @api_telemetry
    def drop(
        self,
        if_exists: bool = False,
    ) -> None:
        """Drop this Iceberg table.

        Parameters
        __________
        if_exists: bool, optional
            Whether to error if the Iceberg table doesn't exist. Default is ``False``.

        Examples
        ________
        Deleting an Iceberg table using its reference, error if it doesn't exist:

        >>> iceberg_table_reference.drop()

        Deleting an Iceberg table using its reference, if it exists:

        >>> iceberg_table_reference.drop(if_exists=True)
        """
        self.collection._api.drop_iceberg_table(
            self.database.name,
            self.schema.name,
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
        future = self.collection._api.drop_iceberg_table(
            self.database.name,
            self.schema.name,
            self.name,
            if_exists=if_exists,
            async_req=True,
        )
        return PollingOperations.empty(future)

    @api_telemetry
    def convert_to_managed(
        self,
        base_location: Optional[StrictStr] = None,
        storage_serialization_policy: Optional[StrictStr] = None,
        if_exists: bool = False,
    ) -> None:
        """Convert Iceberg table using external Iceberg catalog into a table that uses Snowflake as the catalog.

        Parameters
        __________
        base_location: str, optional
            The path to a directory where Snowflake can write data and metadata files for the table.
        storage_serialization_policy: str, optional
            Specifies the storage serialization policy for the table.
        if_exists: bool, optional
            Whether to error if the Iceberg table doesn't exist. Default is ``False``.

        Examples
        ________
        Converts an Iceberg table to managed table using its reference, errors if it doesn't exist:

        >>> iceberg_table.convert_to_managed()

        Converts an Iceberg table to managed table using its reference, if it exists:

        >>> iceberg_table.convert_to_managed(if_exists=True)

        """
        self.collection._api.convert_to_managed_iceberg_table(
            self.database.name,
            self.schema.name,
            self.name,
            convert_to_managed_iceberg_table_request=ConvertToManagedIcebergTableRequest(
                base_location=base_location,
                storage_serialization_policy=storage_serialization_policy,
            ),
            if_exists=if_exists,
            async_req=False,
        )

    @api_telemetry
    def convert_to_managed_async(
        self,
        base_location: Optional[StrictStr] = None,
        storage_serialization_policy: Optional[StrictStr] = None,
        if_exists: bool = False,
    ) -> PollingOperation[None]:
        """An asynchronous version of :func:`convert_to_managed`.

        Refer to :class:`~snowflake.core.PollingOperation` for more information on asynchronous execution and
        the return type.
        """ # noqa: D401
        future = self.collection._api.convert_to_managed_iceberg_table(
            self.database.name,
            self.schema.name,
            self.name,
            convert_to_managed_iceberg_table_request=ConvertToManagedIcebergTableRequest(
                base_location=base_location,
                storage_serialization_policy=storage_serialization_policy,
            ),
            if_exists=if_exists,
            async_req=True,
        )
        return PollingOperations.empty(future)

    @api_telemetry
    def refresh(
        self,
        metadata_file_relative_path: Optional[StrictStr] = None,
        if_exists: bool = False,
    ) -> None:
        """Refresh the metadata for an Iceberg table that uses an external Iceberg catalog.

        Parameters
        __________
        metadata_file_relative_path: str, optional
            Specifies a metadata file path for a table created from Iceberg files in object storage.
        if_exists: bool, optional
            Whether to error if the Iceberg table doesn't exist. Default is ``False``.

        Examples
        ________
        To refresh an Iceberg table using its reference, error if it doesn't exist:

        >>> iceberg_table.refresh()

        To refresh an Iceberg table using its reference, if it exists:

        >>> iceberg_table.refresh(if_exists=True)

        """
        self.collection._api.refresh_iceberg_table(
            self.database.name,
            self.schema.name,
            self.name,
            refresh_iceberg_table_request=RefreshIcebergTableRequest(
                metadata_file_relative_path=metadata_file_relative_path
            ),
            if_exists=if_exists,
            async_req=False,
        )

    @api_telemetry
    def refresh_async(
        self,
        metadata_file_relative_path: Optional[StrictStr] = None,
        if_exists: bool = False,
    ) -> PollingOperation[None]:
        """An asynchronous version of :func:`refresh`.

        Refer to :class:`~snowflake.core.PollingOperation` for more information on asynchronous execution and
        the return type.
        """ # noqa: D401
        future = self.collection._api.refresh_iceberg_table(
            self.database.name,
            self.schema.name,
            self.name,
            refresh_iceberg_table_request=RefreshIcebergTableRequest(
                metadata_file_relative_path=metadata_file_relative_path
            ),
            if_exists=if_exists,
            async_req=True,
        )
        return PollingOperations.empty(future)

    @api_telemetry
    def resume_recluster(
        self,
        if_exists: bool = False,
    ) -> None:
        """Resume reclustering for an Iceberg table.

        Parameters
        __________
        if_exists: bool, optional
            Whether to error if the Iceberg table doesn't exist. Default is ``False``.

        Examples
        ________
        To resume reclustering for an Iceberg table using its reference, error if it doesn't exist:

        >>> iceberg_table.resume_recluster()

        To resume reclustering for an Iceberg table using its reference, if it exists:

        >>> iceberg_table.resume_recluster(if_exists=True)

        """
        self.collection._api.resume_recluster_iceberg_table(
            self.database.name,
            self.schema.name,
            self.name,
            if_exists=if_exists,
            async_req=False,
        )

    @api_telemetry
    def resume_recluster_async(
        self,
        if_exists: bool = False,
    ) -> PollingOperation[None]:
        """An asynchronous version of :func:`resume_recluster`.

        Refer to :class:`~snowflake.core.PollingOperation` for more information on asynchronous execution and
        the return type.
        """ # noqa: D401
        future = self.collection._api.resume_recluster_iceberg_table(
            self.database.name,
            self.schema.name,
            self.name,
            if_exists=if_exists,
            async_req=True,
        )
        return PollingOperations.empty(future)

    @api_telemetry
    def suspend_recluster(
        self,
        if_exists: bool = False,
    ) -> None:
        """Suspend reclustering for an Iceberg table.

        Parameters
        __________
        if_exists: bool, optional
            Whether to error if the Iceberg table doesn't exist. Default is ``False``.

        Examples
        ________
        To suspend reclustering for an Iceberg table using its reference, error if it doesn't exist:

        >>> iceberg_table.suspend_recluster()

        To suspend reclustering for an Iceberg table using its reference, if it exists:

        >>> iceberg_table.suspend_recluster(if_exists=True)

        """
        self.collection._api.suspend_recluster_iceberg_table(
            self.database.name,
            self.schema.name,
            self.name,
            if_exists=if_exists,
            async_req=False,
        )

    @api_telemetry
    def suspend_recluster_async(
        self,
        if_exists: bool = False,
    ) -> PollingOperation[None]:
        """An asynchronous version of :func:`suspend_recluster`.

        Refer to :class:`~snowflake.core.PollingOperation` for more information on asynchronous execution and
        the return type.
        """ # noqa: D401
        future = self.collection._api.suspend_recluster_iceberg_table(
            self.database.name,
            self.schema.name,
            self.name,
            if_exists=if_exists,
            async_req=True,
        )
        return PollingOperations.empty(future)

    @api_telemetry
    def undrop(
        self,
    ) -> None:
        """Undrop an Iceberg table.

        Parameters
        __________
        if_exists: bool, optional
            Whether to error if the Iceberg table doesn't exist. Default is ``False``.

        Examples
        ________
        To undrop an Iceberg table using its reference, error if it doesn't exist:

        >>> iceberg_table.undrop()

        To undrop an Iceberg table using its reference, if it exists:

        >>> iceberg_table.undrop(if_exists=True)

        """
        self.collection._api.undrop_iceberg_table(
            self.database.name,
            self.schema.name,
            self.name,
            async_req=False,
        )

    @api_telemetry
    def undrop_async(
        self,
    ) -> PollingOperation[None]:
        """An asynchronous version of :func:`undrop`.

        Refer to :class:`~snowflake.core.PollingOperation` for more information on asynchronous execution and
        the return type.
        """ # noqa: D401
        future = self.collection._api.undrop_iceberg_table(
            self.database.name,
            self.schema.name,
            self.name,
            async_req=True,
        )
        return PollingOperations.empty(future)
