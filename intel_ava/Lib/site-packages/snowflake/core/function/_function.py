# mypy: ignore-errors

from collections.abc import Iterator
from concurrent.futures import Future
from datetime import time
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
from .._internal.utils import deprecated
from .._utils import get_function_name_with_args
from ..exceptions import InvalidArgumentsError, InvalidResultError
from ._generated.api import FunctionApi
from ._generated.api_client import StoredProcApiClient
from ._generated.models.function import Function
from ._generated.models.function_argument import FunctionArgument


if TYPE_CHECKING:
    from snowflake.core.schema import SchemaResource


def _cast_result(result: Any, returns: StrictStr) -> Any:
    if returns in ["NUMBER", "INT", "FIXED"]:
        return int(result)
    if returns == "REAL":
        return float(result)
    if returns == "TEXT":
        return str(result)
    if returns == "TIME":
        return time(result)
    if returns == "BOOLEAN":
        return bool(int(result))
    return result


class FunctionCollection(SchemaObjectCollectionParent["FunctionResource"]):
    """Represents the collection operations on the Snowflake Function resource.

    With this collection, you can create, iterate through, and search for function that you have access to in the
    current context.

    Examples
    ________
    Creating a function instance:

    >>> functions = root.databases["my_db"].schemas["my_schema"].functions
    >>> new_function = Function(
    ...     name="foo",
    ...     returns="NUMBER",
    ...     arguments=[FunctionArgument(datatype="NUMBER")],
    ...     service="python",
    ...     endpoint="https://example.com",
    ...     path="example.py"
    ... )
    >>> functions.create(new_function)
    """

    def __init__(self, schema: "SchemaResource"):
        super().__init__(schema, FunctionResource)
        self._api = FunctionApi(
            root=self.root,
            resource_class=self._ref_class,
            sproc_client=StoredProcApiClient(root=self.root)
        )

    @api_telemetry
    def create(
        self,
        function: Function,
        mode: CreateMode=CreateMode.error_if_exists,
    ) -> "FunctionResource":
        """Create a function in Snowflake.

        Parameters
        __________
        function: Function
            The ``Function`` object, together with ``Function``'s properties:
            name, returns, arguments, service, endpoint, path;
            max_batch_rows is optional
        mode: CreateMode, optional
            One of the following enum values.

            ``CreateMode.error_if_exists``: Throw an :class:`snowflake.core.exceptions.ConflictError`
            if the function already exists in Snowflake.  Equivalent to SQL ``create function <name> ...``.

            ``CreateMode.or_replace``: Replace if the function already exists in Snowflake. Equivalent to SQL
            ``create or replace function <name> ...``.

            ``CreateMode.if_not_exists``: Do nothing if the function already exists in Snowflake.
            Equivalent to SQL ``create function <name> if not exists...``

            Default is ``CreateMode.error_if_exists``.

        Examples
        ________
        Creating a function, replacing any existing function with the same name:

        >>> functions = root.databases["my_db"].schemas["my_schema"].functions
        >>> new_function = Function(
        ...     name="foo",
        ...     returns="NUMBER",
        ...     arguments=[FunctionArgument(datatype="NUMBER")],
        ...     service="python",
        ...     endpoint="https://example.com",
        ...     path="example.py"
        ... )
        >>> functions.create(new_function, mode=CreateMode.or_replace)
        """
        real_mode = CreateMode[mode].value

        self._api.create_function(
            self.database.name,
            self.schema.name,
            function,
            create_mode=StrictStr(real_mode),
            async_req=False,
        )

        return FunctionResource(get_function_name_with_args(function), self)

    @api_telemetry
    def create_async(
        self,
        function: Function,
        mode: CreateMode=CreateMode.error_if_exists,
    ) -> PollingOperation["FunctionResource"]:
        """An asynchronous version of :func:`create`.

        Refer to :class:`~snowflake.core.PollingOperation` for more information on asynchronous execution and
        the return type.
        """ # noqa: D401
        real_mode = CreateMode[mode].value

        future = self._api.create_function(
            self.database.name,
            self.schema.name,
            function,
            create_mode=StrictStr(real_mode),
            async_req=True,
        )
        return PollingOperation(future, lambda _: FunctionResource(get_function_name_with_args(function), self))

    @api_telemetry
    def iter(
        self,
        *,
        like: Optional[str] = None,
    ) -> Iterator[Function]:
        """Iterate through ``Function`` objects from Snowflake, filtering on any optional 'like' pattern.

        Parameters
        __________
        like: str, optional
            A case-insensitive string functioning as a filter, with support for SQL
            wildcard characters (% and _).

        Examples
        ________
        Showing all functions that you have access to see:

        >>> functions = function_collection.iter()

        Showing information of the exact function you want to see:

        >>> functions = function_collection.iter(like="your-function-name")

        Showing functions starting with 'your-function-name-':

        >>> functions = function_collection.iter(like="your-function-name-%")

        Using a for loop to retrieve information from iterator:

        >>> for function in functions:
        ...     print(function.name)
        """
        functions = self._api.list_functions(
            database=self.database.name,
            var_schema=self.schema.name,
            like=like,
            async_req=False,
        )
        return iter(functions)

    @api_telemetry
    def iter_async(
        self,
        *,
        like: Optional[str] = None,
    ) -> PollingOperation[Iterator[Function]]:
        """An asynchronous version of :func:`iter`.

        Refer to :class:`~snowflake.core.PollingOperation` for more information on asynchronous execution and
        the return type.
        """ # noqa: D401
        future = self._api.list_functions(
            database=self.database.name,
            var_schema=self.schema.name,
            like=like,
            async_req=True,
        )
        return PollingOperations.iterator(future)


class FunctionResource(SchemaObjectReferenceMixin[FunctionCollection]):
    """Represents a reference to a Snowflake function.

    With this function reference, you can create and fetch information about functions, as well
    as perform certain actions on them.
    """

    def __init__(
        self,
        name_with_args: StrictStr,
        collection: FunctionCollection
    ) -> None:
        self.collection = collection
        self.name_with_args = name_with_args

    @api_telemetry
    def fetch(self) -> Function:
        """Fetch the details of a function.

        Examples
        ________
        Fetching a reference to a function to print its name:

        >>> function_reference = root.databases["my_db"].schemas["my_schema"].functions["foo(REAL)"]
        >>> my_function = function_reference.fetch()
        >>> print(my_function.name)
        """
        return self.collection._api.fetch_function(
            self.database.name,
            self.schema.name,
            self.name_with_args,
            async_req=False,
        )

    @api_telemetry
    def fetch_async(self) -> PollingOperation[Function]:
        """An asynchronous version of :func:`fetch`.

        Refer to :class:`~snowflake.core.PollingOperation` for more information on asynchronous execution and
        the return type.
        """ # noqa: D401
        future = self.collection._api.fetch_function(
            self.database.name,
            self.schema.name,
            self.name_with_args,
            async_req=True,
        )
        return PollingOperations.identity(future)

    @api_telemetry
    @deprecated("drop")
    def delete(
        self,
        if_exists: bool = False,
    ) -> None:
        """Delete this function.

        Parameters
        __________
        if_exists: bool, optional
            Whether to error if the function doesn't exist. Default is ``False``.

        Examples
        ________
        Deleting a function using its reference, erroring if it doesn't exist:

        >>> function_reference.delete()

        Deleting a function using its reference, if it exists:

        >>> function_reference.delete(if_exists=True)
        """
        self.drop(if_exists=if_exists)

    @api_telemetry
    def drop(
        self,
        if_exists: bool = False,
    ) -> None:
        """Drop this function.

        Parameters
        __________
        if_exists: bool, optional
            Whether to error if the function doesn't exist. Default is ``False``.

        Examples
        ________
        Dropping a function using its reference, erroring if it doesn't exist:

        >>> function_reference.drop()

        Dropping a function using its reference, if it exists:

        >>> function_reference.drop(if_exists=True)
        """
        self.collection._api.delete_function(
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
        future = self.collection._api.delete_function(
            self.database.name,
            self.schema.name,
            self.name_with_args,
            if_exists=if_exists,
            async_req=True,
        )
        return PollingOperations.empty(future)

    @api_telemetry
    def execute(self, input_args: Optional[list[Any]] = None) -> Any:
        """Execute this function.

        Parameters
        __________
        input_args: list[Any], optional
            A list of arguments to pass to the function. The number of arguments must match the number of arguments
            the function expects.

        Examples
        ________
        Executing a function using its reference:

        >>> function_reference.execute(input_args=[1, 2, "word"])
        """
        return self._execute(input_args=input_args, async_req=False)

    @api_telemetry
    def execute_async(self, input_args: Optional[list[Any]] = None) -> PollingOperation[Any]:
        """An asynchronous version of :func:`execute`.

        Refer to :class:`~snowflake.core.PollingOperation` for more information on asynchronous execution and
        the return type.
        """ # noqa: D401
        return self._execute(input_args=input_args, async_req=True)

    @overload
    def _execute(self, input_args: Optional[list[Any]], async_req: Literal[True]) -> PollingOperation[Any]:
        ...

    @overload
    def _execute(self, input_args: Optional[list[Any]], async_req: Literal[False]) -> Any:
        ...

    def _execute(self, input_args: Optional[list[Any]], async_req: bool) -> Union[Any, PollingOperation[Any]]:
        function = self.fetch()
        args_count = len(function.arguments) if function.arguments is not None else 0

        if input_args is None:
            input_args = []

        if len(input_args) != args_count:
            raise InvalidArgumentsError(f"Function expects {args_count} arguments but received {len(input_args)}")

        function_args = []
        for i in range(args_count):
            argument = FunctionArgument()
            argument.value = input_args[i]
            argument.datatype = function.arguments[i].datatype
            function_args.append(argument)

        result_or_future = self.collection._api.execute_function(
            self.database.name,
            self.schema.name,
            function.name,
            function_args,
            async_req=async_req,
        )

        def map_result(result: object) -> Any:
            if not isinstance(result, dict) or len(result.values()) != 1:
                raise InvalidResultError(f"Function result {str(result)} is invalid or empty")

            result = list(result.values())[0]
            return _cast_result(result, str(function.returns))

        if isinstance(result_or_future, Future):
            return PollingOperation(result_or_future, map_result)
        return map_result(result_or_future)
