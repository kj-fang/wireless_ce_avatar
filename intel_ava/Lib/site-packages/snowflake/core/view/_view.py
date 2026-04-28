from collections.abc import Iterator
from typing import TYPE_CHECKING, Annotated, Optional

from pydantic import Field, StrictBool, StrictStr

from snowflake.core import PollingOperation
from snowflake.core._common import (
    CreateMode,
    SchemaObjectCollectionParent,
    SchemaObjectReferenceMixin,
)
from snowflake.core._operation import PollingOperations

from .._internal.telemetry import api_telemetry
from ._generated.api import ViewApi
from ._generated.api_client import StoredProcApiClient
from ._generated.models.view import View


if TYPE_CHECKING:
    from snowflake.core.schema import SchemaResource

class ViewCollection(SchemaObjectCollectionParent["ViewResource"]):
    """Represents the collection operations on the Snowflake View resource.

    With this collection, you can create, iterate through, and search for views that you have access to in the
    current context.

    Examples
    ________
    Creating a view instance:

    >>> views = root.databases["my_db"].schemas["my_schema"].views
    >>> new_view = View(
    ...     name="my_view",
    ...     columns=[
    ...        ViewColumn(name="col1"), ViewColumn(name="col2"), ViewColumn(name="col3"),
    ...     ],
    ...     query="SELECT * FROM my_table",
    ...    )
    >>> views.create(new_view)
    """

    def __init__(self, schema: "SchemaResource"):
        super().__init__(schema, ViewResource)
        self._api = ViewApi(
            root=self.root,
            resource_class=self._ref_class,
            sproc_client=StoredProcApiClient(root=self.root)
        )


    @api_telemetry
    def create(
        self, view: View,
        mode: CreateMode=CreateMode.error_if_exists,
        copy_grants: Optional[bool] = False,
    ) -> "ViewResource":
        """Create a view in Snowflake.

        Parameters
        __________
        view: View
            The ``View`` object, together with the ``View``'s properties:
            name, columns, query; secure, kind, recursive, comment are optional
        copy_grants: bool, optional
            Whether to enable copy grants when creating the object. Default is ``False``.
        mode: CreateMode, optional
            One of the following enum values.

            ``CreateMode.error_if_exists``: Throw an :class:`snowflake.core.exceptions.ConflictError`
            if the view already exists in Snowflake.  Equivalent to SQL ``create view <name> ...``.

            ``CreateMode.or_replace``: Replace if the view already exists in Snowflake. Equivalent to SQL
            ``create or replace view <name> ...``.

            ``CreateMode.if_not_exists``: Do nothing if the view already exists in Snowflake.
            Equivalent to SQL ``create view <name> if not exists...``

            Default is ``CreateMode.error_if_exists``.

        Examples
        ________
        Creating a view, replacing any existing view with the same name:

        >>> views = root.databases["my_db"].schemas["my_schema"].views
        >>> new_view = View(
        ...     name="my_view",
        ...     columns=[
        ...        ViewColumn(name="col1"), ViewColumn(name="col2"), ViewColumn(name="col3"),
        ...     ],
        ...     query="SELECT * FROM my_table",
        ...    )
        >>> views.create(new_view, mode=CreateMode.or_replace)
        """
        real_mode = CreateMode[mode].value

        self._api.create_view(
            self.database.name, self.schema.name, view, create_mode=StrictStr(real_mode),
            copy_grants=copy_grants, async_req=False
        )

        return ViewResource(view.name, self)

    @api_telemetry
    def create_async(
        self, view: View,
        mode: CreateMode=CreateMode.error_if_exists,
        copy_grants: Optional[bool] = False,
    ) -> PollingOperation["ViewResource"]:
        """An asynchronous version of :func:`create`.

        Refer to :class:`~snowflake.core.PollingOperation` for more information on asynchronous execution and
        the return type.
        """ # noqa: D401
        real_mode = CreateMode[mode].value

        future = self._api.create_view(
            self.database.name, self.schema.name, view, create_mode=StrictStr(real_mode),
            copy_grants=copy_grants, async_req=True
        )
        return PollingOperation(future, lambda _: ViewResource(view.name, self))

    @api_telemetry
    def iter(
        self,
        *,
        like: Optional[StrictStr] = None,
        starts_with: Optional[StrictStr] = None,
        show_limit: Optional[Annotated[int, Field(le=10000, strict=True, ge=1)]] = None,
        from_name: Optional[StrictStr] = None,
        deep: Optional[StrictBool] = None
    ) -> Iterator[View]:
        """Iterate through ``View`` objects from Snowflake, filtering on any optional 'like' pattern.

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
        deep: bool, optional
            Optionally includes dependency information of the view. Default is ``None``,
            which is equivalent to ``False``.

        Examples
        ________
        Showing all views that you have access to see:

        >>> views = view_collection.iter()

        Showing information of the exact view you want to see:

        >>> views = view_collection.iter(like="your-view-name")

        Showing views starting with 'your-view-name-':

        >>> views = view_collection.iter(like="your-view-name-%")

        Using a for loop to retrieve information from iterator:

        >>> for view in views:
        ...     print(view.name, view.query)
        """
        views = self._api.list_views(database=self.database.name, var_schema=self.schema.name, like=like,
                                     starts_with=starts_with, show_limit=show_limit, from_name=from_name, deep=deep,
                                     async_req=False)
        return iter(views)

    @api_telemetry
    def iter_async(
        self,
        *,
        like: Optional[StrictStr] = None,
        starts_with: Optional[StrictStr] = None,
        show_limit: Optional[Annotated[int, Field(le=10000, strict=True, ge=1)]] = None,
        from_name: Optional[StrictStr] = None,
        deep: Optional[StrictBool] = None
    ) -> PollingOperation[Iterator[View]]:
        """An asynchronous version of :func:`iter`.

        Refer to :class:`~snowflake.core.PollingOperation` for more information on asynchronous execution and
        the return type.
        """ # noqa: D401
        future = self._api.list_views(database=self.database.name, var_schema=self.schema.name, like=like,
                                     starts_with=starts_with, show_limit=show_limit, from_name=from_name, deep=deep,
                                     async_req=True)
        return PollingOperations.iterator(future)

class ViewResource(SchemaObjectReferenceMixin[ViewCollection]):
    """Represents a reference to a Snowflake view.

    With this view reference, you can drop and fetch information about views.
    """

    def __init__(
            self,
            name: StrictStr,
            collection: ViewCollection
        ) -> None:
        self.name = name
        self.collection = collection

    @api_telemetry
    def fetch(self) -> View:
        """Fetch the details of a view.

        Examples
        ________
        Fetching a reference to a view to print its name and query properties:

        >>> my_view = view_reference.fetch()
        >>> print(my_view.name, my_view.query)
        """
        return self.collection._api.fetch_view(
            self.database.name, self.schema.name, self.name, async_req=False,
        )

    @api_telemetry
    def fetch_async(self) -> PollingOperation[View]:
        """An asynchronous version of :func:`fetch`.

        Refer to :class:`~snowflake.core.PollingOperation` for more information on asynchronous execution and
        the return type.
        """ # noqa: D401
        future = self.collection._api.fetch_view(
            self.database.name, self.schema.name, self.name, async_req=True,
        )
        return PollingOperations.identity(future)

    @api_telemetry
    def drop(self, if_exists: Optional[bool] = None) -> None:
        """Drop this view.

        Parameters
        __________
        if_exists: bool, optional
            Check the existence of this view before dropping it.
            Default is ``None``, which is equivalent to ``False``.

        Examples
        ________
        Deleting a view using its reference:

        >>> view_reference.drop()

        Deleting a view using its reference if it exists:

        >>> view_reference.drop(if_exists = True)
        """
        self.collection._api.delete_view(
            self.database.name, self.schema.name, self.name, if_exists=if_exists, async_req=False
        )

    @api_telemetry
    def drop_async(self, if_exists: Optional[bool] = None) -> PollingOperation[None]:
        """An asynchronous version of :func:`drop`.

        Refer to :class:`~snowflake.core.PollingOperation` for more information on asynchronous execution and
        the return type.
        """ # noqa: D401
        future = self.collection._api.delete_view(
            self.database.name, self.schema.name, self.name, if_exists=if_exists, async_req=True
        )
        return PollingOperations.empty(future)
