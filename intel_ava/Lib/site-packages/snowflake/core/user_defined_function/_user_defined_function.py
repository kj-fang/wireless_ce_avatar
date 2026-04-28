# mypy: ignore-errors

from collections.abc import Iterator
from concurrent.futures import Future
from typing import TYPE_CHECKING, Any, Literal, Optional, overload

from snowflake.core import PollingOperation
from snowflake.core._common import (
    CreateMode,
    SchemaObjectCollectionParent,
    SchemaObjectReferenceMixin,
)
from snowflake.core._operation import PollingOperations

from .._internal.telemetry import api_telemetry
from .._utils import get_function_name_with_args, replace_function_name_in_name_with_args
from ._generated.api import UserDefinedFunctionApi
from ._generated.api_client import StoredProcApiClient
from ._generated.models.user_defined_function import UserDefinedFunction


if TYPE_CHECKING:
    from snowflake.core.schema import SchemaResource

class UserDefinedFunctionCollection(SchemaObjectCollectionParent["UserDefinedFunctionResource"]):
    """Represents the collection operations on the Snowflake User Defined Function resource.

    With this collection, you can create, iterate through, and fetch user defined functions
    that you have access to in the current context.

    Examples
    ________
    Creating a user defined function instance of python language:

    >>> user_defined_functions.create(
    ...     UserDefinedFunction(
    ...         name="my_python_function",
    ...         arguments=[],
    ...         return_type=ReturnDataType(datatype="VARIANT"),
    ...         language_config=PythonFunction(runtime_version="3.9", packages=[], handler="udf"),
    ...         body='''
    ... def udf():
    ...     return {"key": "value"}
    ...             ''',
    ...     )
    ... )
    """

    def __init__(self, schema: "SchemaResource") -> None:
        super().__init__(schema, UserDefinedFunctionResource)
        self._api = UserDefinedFunctionApi(
            root=self.root,
            resource_class=self._ref_class,
            sproc_client=StoredProcApiClient(root=self.root)
        )


    @api_telemetry
    def create(
        self,
        user_defined_function: UserDefinedFunction,
        *,
        mode: CreateMode = CreateMode.error_if_exists,
        copy_grants: Optional[bool] = False,
    ) -> "UserDefinedFunctionResource":
        """Create a user defined function in Snowflake.

        Parameters
        __________
        user_defined_function: UserDefinedFunction
            The details of ``UserDefinedFunction`` object, together with ``UserDefinedFunction``'s properties:
                name , arguments, return_type, language_config;
                body, comment, is_secure, is_memoizable, is_aggregate, is_temporary are optional.

        copy_grants: bool, optional
            Whether to enable copy grants when creating the object. Default is ``False``.

        mode: CreateMode, optional
            One of the below enum values.

            ``CreateMode.error_if_exists``: Throw an :class:`snowflake.core.exceptions.ConflictError` if the
            user defined function already exists in Snowflake.
            Equivalent to SQL ``create function <name> ...``.

            ``CreateMode.or_replace``: Replace if the user defined function already exists in Snowflake.
            Equivalent to SQL ``create or replace function <name> ...``.

            ``CreateMode.if_not_exists``: Do nothing if the user defined function already exists in Snowflake.
            Equivalent to SQL ``create function <name> if not exists...``

            Default value is ``CreateMode.error_if_exists``.

        Examples
        ________
        Creating a user defined function instance of python language:

        >>> user_defined_functions.create(
        ...     UserDefinedFunction(
        ...         name="my_python_function",
        ...         arguments=[],
        ...         return_type=ReturnDataType(datatype="VARIANT"),
        ...         language_config=PythonFunction(runtime_version="3.9", packages=[], handler="udf"),
        ...         body='''
        ... def udf():
        ...     return {"key": "value"}
        ...             ''',
        ...     )
        ... )

        Creating a user defined function instance of java language:

        >>> function_body = '''
        ... class TestFunc {
        ...     public static String echoVarchar(String x) {
        ...         return x;
        ...     }
        ... }
        ... '''
        >>> user_defined_functions.create(
        ...     UserDefinedFunction(
        ...         name="my_java_function",
        ...         arguments=[Argument(name="x", datatype="STRING")],
        ...         return_type=ReturnDataType(datatype="VARCHAR", nullable=True),
        ...         language_config=JavaFunction(
        ...             handler="TestFunc.echoVarchar",
        ...             runtime_version="11",
        ...             target_path="@~/my_java.jar",
        ...             packages=[],
        ...             called_on_null_input=True,
        ...             is_volatile=True,
        ...         ),
        ...         body=function_body,
        ...         comment="test_comment",
        ...     )
        ... )

        Creating a user defined function instance of javascript language:

        >>> function_body = '''
        ...     if (D <= 0) {
        ...         return 1;
        ...     } else {
        ...         var result = 1;
        ...         for (var i = 2; i <= D; i++) {
        ...             result = result * i;
        ...         }
        ...         return result;
        ...     }
        ... '''
        >>> user_defined_function_created = user_defined_functions.create(
        ...     UserDefinedFunction(
        ...         name="my_js_function",
        ...         arguments=[Argument(name="d", datatype="DOUBLE")],
        ...         return_type=ReturnDataType(datatype="DOUBLE"),
        ...         language_config=JavaScriptFunction(),
        ...         body=function_body,
        ...     )
        ... )

        Creating a user defined function instance of scala language:

        >>> function_body = '''
        ...     class Echo {
        ...         def echoVarchar(x : String): String = {
        ...             return x
        ...         }
        ...     }
        ... '''
        >>> user_defined_function_created = user_defined_functions.create(
        ...     UserDefinedFunction(
        ...         name="my_scala_function",
        ...         arguments=[Argument(name="x", datatype="VARCHAR")],
        ...         return_type=ReturnDataType(datatype="VARCHAR"),
        ...         language_config=ScalaFunction(
        ...             runtime_version="2.12", handler="Echo.echoVarchar", target_path="@~/my_scala.jar", packages=[]
        ...         ),
        ...         body=function_body,
        ...         comment="test_comment",
        ...     )
        ... )

        Creating a user defined function instance of sql language:

        >>> function_body = '''
        ...     SELECT 1, 2
        ...     UNION ALL
        ...     SELECT 3, 4
        ... '''
        >>> user_defined_function_created = user_defined_functions.create(
        ...     UserDefinedFunction(
        ...         name="my_sql_function",
        ...         arguments=[],
        ...         return_type=ReturnTable(
        ...             column_list=[ColumnType(name="x", datatype="INTEGER"), ColumnType(name="y", datatype="INTEGER")]
        ...         ),
        ...         language_config=SQLFunction(),
        ...         body=function_body,
        ...     )
        ... )
        """
        real_mode = CreateMode[mode].value
        self._api.create_user_defined_function(
            self.database.name, self.schema.name, user_defined_function, create_mode=real_mode,
            copy_grants=copy_grants, async_req=False
        )

        return UserDefinedFunctionResource(
            get_function_name_with_args(user_defined_function),
            self,
        )

    @api_telemetry
    def create_async(
        self,
        user_defined_function: UserDefinedFunction,
        *,
        mode: CreateMode = CreateMode.error_if_exists,
        copy_grants: Optional[bool] = False,
    ) -> PollingOperation["UserDefinedFunctionResource"]:
        """An asynchronous version of :func:`create`.

        Refer to :class:`~snowflake.core.PollingOperation` for more information on asynchronous execution and
        the return type.
        """ # noqa: D401
        real_mode = CreateMode[mode].value
        future = self._api.create_user_defined_function(
            self.database.name, self.schema.name, user_defined_function, create_mode=real_mode,
            copy_grants=copy_grants, async_req=True
        )
        return PollingOperation(
            future, lambda _: UserDefinedFunctionResource(get_function_name_with_args(user_defined_function), self))

    @api_telemetry
    def iter(
        self,
        *,
        like: Optional[str] = None
    ) -> Iterator[UserDefinedFunction]:
        """Iterate through ``UserDefinedFunction`` objects from Snowflake, filtering on any optional 'like' pattern.

        Parameters
        __________
        like: str, optional
            A case-insensitive string functioning as a filter, with support for SQL
            wildcard characters (% and _).

        Examples
        ________
        Showing all user defined functions that you have access to see:

        >>> user_defined_functions = user_defined_function_collection.iter()

        Showing information of the exact user defined function you want to see:

        >>> user_defined_functions = user_defined_function_collection.iter(like="your-user-defined-function-name")

        Showing user defined functions starting with 'your-user-defined-function-name-':

        >>> user_defined_functions = user_defined_function_collection.iter(like="your-user-defined-function-name-%")

        Using a for loop to retrieve information from iterator:

        >>> for user_defined_function in user_defined_functions:
        ...     print(user_defined_function.name)
        """
        user_defined_functions = self._api.list_user_defined_functions(database=self.database.name,
                                    var_schema=self.schema.name,like=like, async_req=False)
        return iter(user_defined_functions)


    @api_telemetry
    def iter_async(
        self,
        *,
        like: Optional[str] = None
    ) -> PollingOperation[Iterator[UserDefinedFunction]]:
        """An asynchronous version of :func:`iter`.

        Refer to :class:`~snowflake.core.PollingOperation` for more information on asynchronous execution and
        the return type.
        """ # noqa: D401
        future = self._api.list_user_defined_functions(database=self.database.name,
                                    var_schema=self.schema.name,like=like, async_req=True)
        return PollingOperations.iterator(future)


class UserDefinedFunctionResource(SchemaObjectReferenceMixin[UserDefinedFunctionCollection]):
    """Represents a reference to a Snowflake user defined function.

    With this user defined function reference, you can create, drop, rename
    and fetch information about user defined functions.
    """

    def __init__(
        self,
        name_with_args: str,
        collection: UserDefinedFunctionCollection
    ) -> None:
        self.name_with_args = name_with_args
        self.collection = collection

    @api_telemetry
    def fetch(self) -> UserDefinedFunction:
        """Fetch the details of a user defined function.

        Examples
        ________
        Fetching a user defined function reference to print its time of creation:

        >>> print(user_defined_function_reference.fetch().created_on)
        """
        return self.collection._api.fetch_user_defined_function(
            self.database.name, self.schema.name, self.name_with_args,
            async_req=False,
        )


    @api_telemetry
    def fetch_async(self) -> PollingOperation[UserDefinedFunction]:
        """An asynchronous version of :func:`fetch`.

        Refer to :class:`~snowflake.core.PollingOperation` for more information on asynchronous execution and
        the return type.
        """ # noqa: D401
        future = self.collection._api.fetch_user_defined_function(
            self.database.name, self.schema.name, self.name_with_args,
            async_req=True,
        )
        return PollingOperations.identity(future)


    @api_telemetry
    def drop(self, if_exists: Optional[bool] = None) -> None:
        """Drop this user defined function.

        Parameters
        __________
        if_exists: bool, optional
            Check the existence of this user defined function before dropping it.
            Default is ``None``, which is equivalent to ``False``.

        Examples
        ________
        Deleting this user defined function using its reference:

        >>> user_defined_function_reference.drop()

        Deleting this user defined function if it exists:

        >>> user_defined_function_reference.drop(if_exists = True)
        """
        self.collection._api.delete_user_defined_function(self.database.name, self.schema.name,
                                                self.name_with_args, if_exists=if_exists,
                                                async_req=False)


    @api_telemetry
    def drop_async(self, if_exists: Optional[bool] = None) -> PollingOperation[None]:
        """An asynchronous version of :func:`drop`.

        Refer to :class:`~snowflake.core.PollingOperation` for more information on asynchronous execution and
        the return type.
        """ # noqa: D401
        future = self.collection._api.delete_user_defined_function(self.database.name, self.schema.name,
                                                self.name_with_args, if_exists=if_exists,
                                                async_req=True)
        return PollingOperations.empty(future)


    @api_telemetry
    def rename(
        self,
        target_name: str,
        target_database: Optional[str] = None,
        target_schema: Optional[str] = None,
        if_exists: Optional[bool] = None
    ) -> None:
        """Rename this user defined function.

        Parameters
        __________
        target_name: str
            The new name of the user defined function
        target_database: str, optional
            The database where the user defined function will be located
        target_schema: str, optional
            The schema where the user defined function will be located
        if_exists: bool, optional
            Check the existence of user defined function before rename

        Examples
        ________
        Renaming this user defined function using its reference:

        >>> user_defined_function_reference.rename("my_other_user_defined_function")

        Renaming this user defined function if it exists:

        >>> user_defined_function_reference.rename("my_other_user_defined_function", if_exists = True)

        Renaming this user defined function and relocating it to another schema within same database:

        >>> user_defined_function_reference.rename(
        ...     "my_other_user_defined_function",
        ...     target_schema = "my_other_schema",
        ...     if_exists = True
        ... )

        Renaming this user defined function and relocating it to another database and schema:

        >>> user_defined_function_reference.rename(
        ...     "my_other_user_defined_function",
        ...     target_database = "my_other_database",
        ...     target_schema = "my_other_schema",
        ...     if_exists = True
        ... )
        """
        self._rename(
            target_name=target_name,
            target_database=target_database,
            target_schema=target_schema,
            if_exists=if_exists,
            async_req=False,
        )

    @api_telemetry
    def rename_async(
        self,
        target_name: str,
        target_database: Optional[str] = None,
        target_schema: Optional[str] = None,
        if_exists: Optional[bool] = None
    ) -> PollingOperation[None]:
        """An asynchronous version of :func:`rename`.

        Refer to :class:`~snowflake.core.PollingOperation` for more information on asynchronous execution and
        the return type.
        """ # noqa: D401
        return self._rename(
            target_name=target_name,
            target_database=target_database,
            target_schema=target_schema,
            if_exists=if_exists,
            async_req=True,
        )

    @overload
    def _rename(
        self,
        target_name: str,
        target_database: Optional[str],
        target_schema: Optional[str],
        if_exists: Optional[bool],
        async_req: Literal[True]
    ) -> PollingOperation[None]:
        ...

    @overload
    def _rename(
        self,
        target_name: str,
        target_database: Optional[str],
        target_schema: Optional[str],
        if_exists: Optional[bool],
        async_req: Literal[False],
    ) -> None:
        ...

    def _rename(
        self,
        target_name: str,
        target_database: Optional[str],
        target_schema: Optional[str],
        if_exists: Optional[bool],
        async_req: bool,
    ) -> Optional[PollingOperation[None]]:
        if target_database is None:
            target_database = self.database.name
        if target_schema is None:
            target_schema = self.schema.name

        result_or_future = self.collection._api.rename_user_defined_function(
            self.database.name,
            self.schema.name,
            self.name_with_args,
            target_name=target_name,
            target_database=target_database,
            target_schema=target_schema,
            if_exists=if_exists,
            async_req=async_req,
        )

        def finalize(_: Any) -> None:
            if target_database != self.database.name or target_schema != self.schema.name:
                self.collection = self.root.databases[target_database].schemas[target_schema].user_defined_functions

            self.name_with_args = replace_function_name_in_name_with_args(
                self.name_with_args,
                target_name
            )

        if not isinstance(result_or_future, Future):
            finalize(None)
        else:
            return PollingOperation(result_or_future, finalize)
