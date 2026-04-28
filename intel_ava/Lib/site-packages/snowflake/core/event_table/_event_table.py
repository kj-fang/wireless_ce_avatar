from collections.abc import Iterator
from typing import TYPE_CHECKING, Any, Optional

from snowflake.core import PollingOperation
from snowflake.core._common import (
    CreateMode,
    SchemaObjectCollectionParent,
    SchemaObjectReferenceMixin,
)
from snowflake.core._operation import PollingOperations

from .._internal.telemetry import api_telemetry
from ._generated.api import EventTableApi
from ._generated.api_client import StoredProcApiClient
from ._generated.models.event_table import EventTable


if TYPE_CHECKING:
    from snowflake.core.schema import SchemaResource

class EventTableCollection(SchemaObjectCollectionParent["EventTableResource"]):
    """Represents the collection operations on the Snowflake Event Table resource.

    With this collection, you can create, iterate through, and fetch event tables
    that you have access to in the current context.
    """

    def __init__(self, schema: "SchemaResource"):
        """Initialize collection for Event Table."""
        super().__init__(schema, EventTableResource)
        self._api = EventTableApi(
            root=self.root,
            resource_class=self._ref_class,
            sproc_client=StoredProcApiClient(root=self.root)
        )


    @api_telemetry
    def create(
        self,
        event_table: EventTable,
        *,
        mode: CreateMode = CreateMode.error_if_exists,
        copy_grants: Optional[bool] = False,
    ) -> "EventTableResource":
        """Create an event table in Snowflake.

        Parameters
        __________
        event_table: EventTable
            The details of ``EventTable`` object, together with ``EventTable``'s properties:
                name ; rows, columns are optional

        copy_grants: bool, optional
            Whether to enable copy grants when creating the object. Default is ``False``.

        mode: CreateMode, optional
            One of the below enum values.

            ``CreateMode.error_if_exists``: Throw an :class:`snowflake.core.exceptions.ConflictError` if the
            event table already exists in Snowflake. Equivalent to SQL ``create event table <name> ...``.

            ``CreateMode.or_replace``: Replace if the event table already exists in Snowflake. Equivalent to SQL
            ``create or replace event table <name> ...``.

            ``CreateMode.if_not_exists``: Do nothing if the event table already exists in Snowflake. Equivalent to SQL
            ``create event table <name> if not exists...``

            Default value is ``CreateMode.error_if_exists``.

        Examples
        ________
        Create an Event Table instance:

        >>> event_tables = schema.event_tables
        >>> event_tables.create(new_event_table)
        """
        real_mode = CreateMode[mode].value
        self._api.create_event_table(
            self.database.name, self.schema.name, event_table, create_mode=real_mode,
            copy_grants=copy_grants, async_req=False
        )

        return EventTableResource(event_table.name, self)

    @api_telemetry
    def create_async(
        self,
        event_table: EventTable,
        *,
        mode: CreateMode = CreateMode.error_if_exists,
        copy_grants: Optional[bool] = False,
    ) -> PollingOperation["EventTableResource"]:
        """An asynchronous version of :func:`create`.

        Refer to :class:`~snowflake.core.PollingOperation` for more information on asynchronous execution and
        the return type.
        """ # noqa: D401
        real_mode = CreateMode[mode].value
        future = self._api.create_event_table(
            self.database.name, self.schema.name, event_table, create_mode=real_mode,
            copy_grants=copy_grants, async_req=True
        )
        return PollingOperation(future, lambda _: EventTableResource(event_table.name, self))

    @api_telemetry
    def iter(
        self,
        *,
        like: Optional[str] = None,
        starts_with: Optional[str] = None,
        show_limit: Optional[int] = None,
        from_name: Optional[str] = None
    ) -> Iterator[EventTable]:
        """Iterate through ``Event Table`` objects from Snowflake, filtering on any optional 'like' pattern.

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
        Showing all event tables that you have access to see:

        >>> event_tables = event_table_collection.iter()

        Showing information of the exact event table you want to see:

        >>> event_tables = event_table_collection.iter(like="your-event-table-name")

        Showing event tables starting with 'your-event-table-name-':

        >>> event_tables = event_table_collection.iter(like="your-event-table-name-%")

        Using a for loop to retrieve information from iterator:

        >>> for event_table in event_tables:
        ...     print(event_table.name)
        """
        event_tables = self._api.list_event_tables(database=self.database.name, var_schema=self.schema.name, like=like,
                                     starts_with=starts_with, show_limit=show_limit, from_name=from_name,
                                     async_req=False)
        return iter(event_tables)


    @api_telemetry
    def iter_async(
        self,
        *,
        like: Optional[str] = None,
        starts_with: Optional[str] = None,
        show_limit: Optional[int] = None,
        from_name: Optional[str] = None
    ) -> PollingOperation[Iterator[EventTable]]:
        """An asynchronous version of :func:`iter`.

        Refer to :class:`~snowflake.core.PollingOperation` for more information on asynchronous execution and
        the return type.
        """ # noqa: D401
        future = self._api.list_event_tables(database=self.database.name, var_schema=self.schema.name, like=like,
                                     starts_with=starts_with, show_limit=show_limit, from_name=from_name,
                                     async_req=True)
        return PollingOperations.iterator(future)


class EventTableResource(SchemaObjectReferenceMixin[EventTableCollection]):
    """Represents a reference to a Snowflake event table.

    With this event table reference, you can create, update, and fetch information about event tables, as well
    as perform certain actions on them.
    """

    def __init__(
        self,
        name: str,
        collection: EventTableCollection
    ) -> None:
        self.name = name
        self.collection = collection

    @api_telemetry
    def fetch(self) -> EventTable:
        """Fetch the details of an event table.

        Examples
        ________
        Fetching an event table reference to print its time of creation:

        >>> print(event_table_reference.fetch().created_on)
        """
        return self.collection._api.fetch_event_table(
            self.database.name, self.schema.name, self.name,
            async_req=False,
        )

    @api_telemetry
    def fetch_async(self) -> PollingOperation[EventTable]:
        """An asynchronous version of :func:`fetch`.

        Refer to :class:`~snowflake.core.PollingOperation` for more information on asynchronous execution and
        the return type.
        """ # noqa: D401
        future = self.collection._api.fetch_event_table(
            self.database.name, self.schema.name, self.name,
            async_req=True,
        )
        return PollingOperations.identity(future)

    @api_telemetry
    def drop(self, if_exists: Optional[bool] = None) -> None:
        """Drop this event table.

        Parameters
        __________
        if_exists: bool, optional
            Check the existance of event_table before drop.
            The default value is ``None``, which is equivalent to ``False``.

        Examples
        ________
        Deleting this event table using its reference:

        >>> event_table_reference.drop()

        Deleting this event table if it exists:

        >>> event_table_reference.drop(if_exists = True)
        """
        self.collection._api.delete_event_table(self.database.name, self.schema.name,
                                                self.name, if_exists=if_exists,
                                                async_req=False)

    @api_telemetry
    def drop_async(self, if_exists: Optional[bool] = None) -> PollingOperation[None]:
        """An asynchronous version of :func:`drop`.

        Refer to :class:`~snowflake.core.PollingOperation` for more information on asynchronous execution and
        the return type.
        """ # noqa: D401
        future = self.collection._api.delete_event_table(self.database.name, self.schema.name,
                                                self.name, if_exists=if_exists,
                                                async_req=True)
        return PollingOperations.empty(future)

    @api_telemetry
    def rename(
        self,
        target_name: str,
        if_exists: Optional[bool] = None
    ) -> None:
        """Rename this event table.

        Parameters
        __________
        target_name: str
            The new name of the event table
        if_exists: bool, optional
            Check the existence of event table before rename

        Examples
        ________
        Renaming this event table using its reference:

        >>> event_table_reference.rename("my_other_event_table")

        Renaming this event table if it exists:

        >>> event_table_reference.rename("my_other_event_table", if_exists = True)
        """
        self.collection._api.rename_event_table(
            self.database.name,
            self.schema.name,
            self.name,
            target_name=target_name,
            if_exists=if_exists
        )
        self.name = target_name

    @api_telemetry
    def rename_async(
        self,
        target_name: str,
        if_exists: Optional[bool] = None
    ) -> PollingOperation[None]:
        """An asynchronous version of :func:`rename`.

        Refer to :class:`~snowflake.core.PollingOperation` for more information on asynchronous execution and
        the return type.
        """ # noqa: D401
        future = self.collection._api.rename_event_table(
            self.database.name,
            self.schema.name,
            self.name,
            target_name=target_name,
            if_exists=if_exists,
            async_req=True,
        )
        def finalize(_: Any) -> None:
            self.name = target_name

        return PollingOperation(future, finalize)
