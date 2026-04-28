# mypy: ignore-errors
import warnings

from collections.abc import Iterator
from concurrent.futures import Future
from typing import TYPE_CHECKING, Any, Literal, Optional, Union, overload

from pydantic import StrictStr

from snowflake.core import PollingOperation
from snowflake.core._common import (
    CreateMode,
    SchemaObjectCollectionParent,
    SchemaObjectReferenceMixin,
)
from snowflake.core._operation import PollingOperations

from .._internal.telemetry import api_telemetry
from .._utils import get_function_name_with_args, map_result
from ._generated.api import ProcedureApi
from ._generated.api_client import StoredProcApiClient
from ._generated.models.call_argument_list import CallArgumentList
from ._generated.models.procedure import Procedure


if TYPE_CHECKING:
    from snowflake.core.schema import SchemaResource


class ProcedureCollection(SchemaObjectCollectionParent["ProcedureResource"]):
    """Represents the collection operations on the Snowflake Procedure resource.

    With this collection, you can create, iterate through, and fetch procedures that you have access to
    in the current context.

    Examples
    ________
    Creating a procedure instance:

    >>> procedure = Procedure(
    ...     name="sql_proc_table_func",
    ...     arguments=[Argument(name="id", datatype="VARCHAR")],
    ...     return_type=ReturnTable(
    ...         column_list=[
    ...             ColumnType(name="id", datatype="NUMBER"),
    ...             ColumnType(name="price", datatype="NUMBER"),
    ...         ]
    ...     ),
    ...     language_config=SQLFunction(),
    ...     body="
    ...         DECLARE
    ...             res RESULTSET DEFAULT (SELECT * FROM invoices WHERE id = :id);
    ...         BEGIN
    ...             RETURN TABLE(res);
    ...         END;
    ...     ",
    ... )
    >>> procedures = root.databases["my_db"].schemas["my_schema"].procedures
    >>> procedures.create(procedure)
    """

    def __init__(self, schema: "SchemaResource") -> None:
        super().__init__(schema, ProcedureResource)
        self._api = ProcedureApi(
            root=self.root,
            resource_class=self._ref_class,
            sproc_client=StoredProcApiClient(root=self.root)
        )

    @api_telemetry
    def create(
        self,
        procedure: Procedure,
        mode: CreateMode = CreateMode.error_if_exists,
        copy_grants: bool = False,
    ) -> "ProcedureResource":
        """Create a procedure in Snowflake.

        Parameters
        __________
        procedure: Procedure
            The ``Procedure`` object, together with the ``Procedure``'s properties:
            name, arguments, return_type, language_config;
            secure, runtime_version, packages, imports, handler, external_access_integrations, secrets,
            called_on_null_input, return_type nullable, comment, execute_as, body are optional.
        mode: CreateMode, optional
            One of the below enum values.

            ``CreateMode.error_if_exists``: Throw an :class:`snowflake.core.exceptions.ConflictError`
            if the procedure already exists in Snowflake.  Equivalent to SQL ``create procedure <name> ...``.

            ``CreateMode.or_replace``: Replace if the procedure already exists in Snowflake. Equivalent to SQL
            ``create or replace procedure <name> ...``.

            ``CreateMode.if_not_exists``: Do nothing if the procedure already exists in Snowflake.
            Equivalent to SQL ``create procedure <name> if not exists...``

            Default value is ``CreateMode.error_if_exists``.
        copy_grants: bool, optional
            Whether to enable copy grants when creating the object. Default is ``False``.

        Examples
        ________
        Creating a procedure, replacing an existing procedure with the same name:

        >>> procedure = Procedure(
        ...     name="my_procedure",
        ...     arguments=[],
        ...     return_type=ReturnDataType(datatype="FLOAT"),
        ...     language_config=JavaScriptFunction(),
        ...     body="return 3.14;"
        ... )
        >>> procedures = root.databases["my_db"].schemas["my_schema"].procedures
        >>> procedures.create(procedure, mode=CreateMode.or_replace)
        """
        real_mode = CreateMode[mode].value

        self._api.create_procedure(
            self.database.name,
            self.schema.name,
            procedure,
            create_mode=StrictStr(real_mode),
            copy_grants=copy_grants,
            async_req=False,
        )

        return ProcedureResource(get_function_name_with_args(procedure), self)

    @api_telemetry
    def create_async(
        self,
        procedure: Procedure,
        mode: CreateMode = CreateMode.error_if_exists,
        copy_grants: bool = False,
    ) -> PollingOperation["ProcedureResource"]:
        """An asynchronous version of :func:`create`.

        Refer to :class:`~snowflake.core.PollingOperation` for more information on asynchronous execution and
        the return type.
        """ # noqa: D401
        real_mode = CreateMode[mode].value

        future = self._api.create_procedure(
            self.database.name,
            self.schema.name,
            procedure,
            create_mode=StrictStr(real_mode),
            copy_grants=copy_grants,
            async_req=True,
        )
        return PollingOperation(future, lambda _: ProcedureResource(get_function_name_with_args(procedure), self))

    @api_telemetry
    def iter(
        self,
        *,
        like: Optional[str] = None,
    ) -> Iterator[Procedure]:
        """Iterate through ``Procedure`` objects from Snowflake, filtering on any optional 'like' pattern.

        Parameters
        __________
        like: str, optional
            A case-insensitive string functioning as a filter, with support for SQL
            wildcard characters (``%`` and ``_``).

        Examples
        ________
        Showing all procedures that you have access to see:

        >>> procedures = root.databases["my_db"].schemas["my_schema"].procedures.iter()

        Showing information of the exact procedure you want to see:

        >>> procedures = root.databases["my_db"].schemas["my_schema"].procedures.iter(like="your-procedure-name")

        Showing procedures starting with 'your-procedure-name-':

        >>> procedures = root.databases["my_db"].schemas["my_schema"].procedures.iter(like="your-procedure-name-%")

        Using a for loop to retrieve information from iterator:

        >>> for procedure in procedures:
        ...     print(procedure.name)
        """
        procedures = self._api.list_procedures(
            database=self.database.name,
            var_schema=self.schema.name,
            like=like,
            async_req=False,
        )
        return iter(procedures)

    @api_telemetry
    def iter_async(
        self,
        *,
        like: Optional[str] = None,
    ) -> PollingOperation[Iterator[Procedure]]:
        """An asynchronous version of :func:`iter`.

        Refer to :class:`~snowflake.core.PollingOperation` for more information on asynchronous execution and
        the return type.
        """ # noqa: D401
        future = self._api.list_procedures(
            database=self.database.name,
            var_schema=self.schema.name,
            like=like,
            async_req=True,
        )
        return PollingOperations.iterator(future)


class ProcedureResource(SchemaObjectReferenceMixin[ProcedureCollection]):
    """Represents a reference to a Snowflake procedure.

    With this procedure reference, you can create and fetch information about procedures, as well as
    perform certain actions on them.
    """

    def __init__(
        self,
        name_with_args: StrictStr,
        collection: ProcedureCollection
    ) -> None:
        self.collection = collection
        self.name_with_args = name_with_args

    @api_telemetry
    def fetch(self) -> Procedure:
        """Fetch the details of a procedure.

        Examples
        ________
        Fetching a reference to a procedure to print its name:

        >>> my_procedure = procedure_reference.fetch()
        >>> print(my_procedure.name)
        """
        return self.collection._api.fetch_procedure(
            self.database.name,
            self.schema.name,
            self.name_with_args,
            async_req=False,
        )


    @api_telemetry
    def fetch_async(self) -> PollingOperation[Procedure]:
        """An asynchronous version of :func:`fetch`.

        Refer to :class:`~snowflake.core.PollingOperation` for more information on asynchronous execution and
        the return type.
        """ # noqa: D401
        future = self.collection._api.fetch_procedure(
            self.database.name,
            self.schema.name,
            self.name_with_args,
            async_req=True,
        )
        return PollingOperations.identity(future)


    @api_telemetry
    def drop(
        self,
        if_exists: bool = False,
    ) -> None:
        """Drop this procedure.

        Parameters
        __________
        if_exists: bool, optional
            Whether to error if the procedure doesn't exist. Default is ``False``.

        Examples
        ________
        Dropping a procedure using its reference, erroring if it doesn't exist:

        >>> procedure_reference.drop()

        Dropping a procedure using its reference, if it exists:

        >>> procedure_reference.drop(if_exists=True)
        """
        self.collection._api.delete_procedure(
            self.database.name,
            self.schema.name,
            self.name_with_args,
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
        future = self.collection._api.delete_procedure(
            self.database.name,
            self.schema.name,
            self.name_with_args,
            if_exists=if_exists,
            async_req=True,
        )
        return PollingOperations.empty(future)

    @api_telemetry
    def call(self, call_argument_list: Optional[CallArgumentList] = None, extract: Optional[bool] = False) -> Any:
        """Call this procedure.

        Examples
        ________
        Calling a procedure with no arguments using its reference:

        >>> procedure_reference.call(call_argument_list=CallArgumentList(call_arguments=[]))

        Calling a procedure with 2 arguments using its reference:

        >>> procedure_reference.call(call_argument_list=CallArgumentList(call_arguments=[
        ...     CallArgument(name="id", datatype="NUMBER", value=1),
        ...     CallArgument(name="tableName", datatype="VARCHAR", value="my_table_name"),
        ... ]))
        """
        return self._call(call_argument_list=call_argument_list, async_req=False, extract=extract)

    @api_telemetry
    def call_async(
        self,
        call_argument_list: Optional[CallArgumentList] = None,
        extract: Optional[bool] = False
    ) -> PollingOperation[Any]:
        """An asynchronous version of :func:`call`.

        Refer to :class:`~snowflake.core.PollingOperation` for more information on asynchronous execution and
        the return type.
        """ # noqa: D401
        return self._call(call_argument_list=call_argument_list, async_req=True, extract=extract)

    @overload
    def _call(
        self,
        call_argument_list: Optional[CallArgumentList],
        extract: Optional[bool],
        async_req: Literal[True],
    ) -> PollingOperation[Any]:
        ...

    @overload
    def _call(
        self,
        call_argument_list: Optional[CallArgumentList],
        extract: Optional[bool],
        async_req: Literal[False],
    ) -> Any:
        ...

    def _call(
        self,
        call_argument_list: Optional[CallArgumentList],
        async_req: bool,
        extract: bool = False
    ) -> Union[Any, PollingOperation[Any]]:
        if extract is False:
            warnings.warn(
                "Please use `extract=True` when calling procedure. This will extract "
                "result from [{sproc_name: result}] object. This will become default behavior.",
                DeprecationWarning,
                stacklevel=4
            )

        # None is not supported by self.collection._api.call_procedure
        if call_argument_list is None:
            call_argument_list = CallArgumentList(call_arguments=[])

        procedure = self.fetch()
        for argument in procedure.arguments:
            if argument.default_value is None:
                assert any(argument.name.upper() == call_argument.name.upper()
                           for call_argument in call_argument_list.call_arguments)

        result_or_future = self.collection._api.call_procedure(
            self.database.name,
            self.schema.name,
            procedure.name,
            call_argument_list=call_argument_list,
            async_req=async_req,
        )

        def mapper(r): return map_result(procedure, r, extract=extract)

        if isinstance(result_or_future, Future):
            return PollingOperation(result_or_future, mapper)
        return mapper(result_or_future)
