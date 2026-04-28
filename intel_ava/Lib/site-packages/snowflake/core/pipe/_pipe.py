from collections.abc import Iterator
from datetime import datetime
from typing import TYPE_CHECKING, Optional

from pydantic import StrictStr

from snowflake.core import PollingOperation
from snowflake.core._common import (
    CreateMode,
    SchemaObjectCollectionParent,
    SchemaObjectReferenceMixin,
)
from snowflake.core._internal.telemetry import api_telemetry
from snowflake.core._operation import PollingOperations
from snowflake.core.pipe._generated.api import PipeApi
from snowflake.core.pipe._generated.api_client import StoredProcApiClient
from snowflake.core.pipe._generated.models.pipe import Pipe


if TYPE_CHECKING:
    from snowflake.core.schema import SchemaResource


class PipeCollection(SchemaObjectCollectionParent["PipeResource"]):
    """Represents the collection operations of the Snowflake Pipe resource.

    With this collection, you can create, iterate through, and search for pipes that you have access to
    in the current context.

    Examples
    ________
    Creaing a pipe instance:

    >>> pipes = root.databases["my_db"].schemas["my_schema"].pipes
    >>>     new_pipe = Pipe(
    ...         name="my_pipe",
    ...         comment="This is a pipe")
    >>> pipes.create(new_pipe)
    """

    def __init__(self, schema: "SchemaResource"):
        super().__init__(schema, PipeResource)
        self._api = PipeApi(
            root=self.root,
            resource_class=self._ref_class,
            sproc_client=StoredProcApiClient(root=self.root),
        )

    @api_telemetry
    def create(
        self,
        pipe: Pipe,
        *,
        mode: CreateMode = CreateMode.error_if_exists,
    ) -> "PipeResource":
        """Create a pipe in Snowflake.

        Parameters
        __________
        pipe: Pipe
        mode: CreateMode, optional
            One of the following strings.

            ``CreateMode.error_if_exists``: Throw an :class:`snowflake.core.exceptions.ConflictError`
            if the pipe already exists in Snowflake. Equivalent to SQL ``create pipe <name> ...``.

            ``CreateMode.or_replace``: Replace if the pipe already exists in Snowflake. Equivalent to SQL
            ``create or replace pipe <name> ...``.

            ``CreateMode.if_not_exists``: Do nothing if the pipe already exists in Snowflake. Equivalent to SQL
            ``create pipe <name> if not exists...``

            Default value is ``CreateMode.error_if_exists``.

        Examples
        ________
        Creating a pipe in Snowflake and getting reference to it:

        >>> pipe_parameters = Pipe(
        ...     name="my_pipe",
        ...     comment="This is a pipe"
        ... )
        >>> # Use the pipe collection created before to create a referece to the pipe resource
        >>> # in Snowflake.
        >>> pipe_reference = pipe_collection.create(pipe_parameters)
        """
        real_mode = CreateMode[mode].value
        self._api.create_pipe(
            self.database.name, self.schema.name, pipe, create_mode=StrictStr(real_mode), async_req=False
        )
        return PipeResource(pipe.name, self)

    @api_telemetry
    def create_async(
        self,
        pipe: Pipe,
        *,
        mode: CreateMode = CreateMode.error_if_exists,
    ) -> PollingOperation["PipeResource"]:
        """An asynchronous version of :func:`create`.

        Refer to :class:`~snowflake.core.PollingOperation` for more information on asynchronous execution and
        the return type.
        """ # noqa: D401
        real_mode = CreateMode[mode].value
        future = self._api.create_pipe(
            self.database.name, self.schema.name, pipe, create_mode=StrictStr(real_mode), async_req=True
        )
        return PollingOperation(future, lambda _: PipeResource(pipe.name, self))

    @api_telemetry
    def iter(
        self,
        *,
        like: Optional[str] = None,
    ) -> Iterator[Pipe]:
        """Iterate through ``Pipe`` objects in Snowflake, filtering on any optional ``like`` pattern.

        Parameters
        __________
        like: str, optional
            A case-insensitive string functioning as a filter, with support for SQL
            wildcard characters (% and _).

        Examples
        ________
        Showing all pipes that you have access to see:

        >>> pipes = pipe_collection.iter()

        Showing information of the exact pipe you want to see:

        >>> pipes = pipe_collection.iter(like="your-pipe-name")

        Showing pipes starting with 'your-pipe-name':

        >>> pipes = pipe_collection.iter(like="your-pipe-name%")

        Using a for loop to retrieve information from iterator:

        >>> for pipe in pipes:
        >>>     print(pipe.name, pipe.comment)
        """
        pipes = self._api.list_pipes(
            database=self.database.name, var_schema=self.schema.name, like=like, async_req=False
        )

        return iter(pipes)

    @api_telemetry
    def iter_async(
        self,
        *,
        like: Optional[str] = None,
    ) -> PollingOperation[Iterator[Pipe]]:
        """An asynchronous version of :func:`iter`.

        Refer to :class:`~snowflake.core.PollingOperation` for more information on asynchronous execution and
        the return type.
        """ # noqa: D401
        future = self._api.list_pipes(
            database=self.database.name, var_schema=self.schema.name, like=like, async_req=True
        )
        return PollingOperations.iterator(future)


class PipeResource(SchemaObjectReferenceMixin[PipeCollection]):
    """Represents a reference to a Snowflake pipe.

    With this pipe reference, you can fetch information about pipes, as well
    as perform certain actions on them.
    """

    def __init__(self, name: str, collection: PipeCollection) -> None:
        self.collection = collection
        self.name = name

    @property
    def _api(self) -> PipeApi:
        """Get the Pipe API object."""
        return self.collection._api

    @api_telemetry
    def fetch(self) -> Pipe:
        """Fetch the details of a pipe resource.

        Examples
        ________
        Fetching a pipe using its reference:

        >>> pipe = pipe_reference.fetch()
        # Accessing information of the pipe with pipe instance.
        >>> print(pipe.name, pipe.comment)
        """
        return self._api.fetch_pipe(
            self.database.name,
            self.schema.name,
            self.name,
            async_req=False,
        )

    @api_telemetry
    def fetch_async(self) -> PollingOperation[Pipe]:
        """An asynchronous version of :func:`fetch`.

        Refer to :class:`~snowflake.core.PollingOperation` for more information on asynchronous execution and
        the return type.
        """ # noqa: D401
        future = self._api.fetch_pipe(
            self.database.name,
            self.schema.name,
            self.name,
            async_req=True,
        )
        return PollingOperations.identity(future)

    @api_telemetry
    def drop(self, if_exists: Optional[bool] = None) -> None:
        """Drop this pipe.

        Parameters
        __________
        if_exists: bool, optional
            Check the existence of this pipe before dropping it.
            Default is ``None``, which is equivalent to ``False``.

        Examples
        ________
        Deleting a pipe using its reference:

        >>> pipe_reference.drop()

        Using a pipe reference to delete a pipe if it exists:

        >>> pipe_reference.drop(if_exists=True)
        """
        self._api.delete_pipe(self.database.name, self.schema.name, self.name, if_exists, async_req=False)

    @api_telemetry
    def drop_async(self, if_exists: Optional[bool] = None) -> PollingOperation[None]:
        """An asynchronous version of :func:`drop`.

        Refer to :class:`~snowflake.core.PollingOperation` for more information on asynchronous execution and
        the return type.
        """ # noqa: D401
        future = self._api.delete_pipe(self.database.name, self.schema.name, self.name, if_exists, async_req=True)
        return PollingOperations.empty(future)

    @api_telemetry
    def refresh(
        self, if_exists: Optional[bool] = None, prefix: Optional[str] = None, modified_after: Optional[datetime] = None
    ) -> None:
        """Refresh this pipe.

        Parameters
        __________
        if_exists: bool, optional
            Check the existence of this pipe before refreshing it.
            Default is ``None``, which is equivalent to ``False``.
        prefix: str, optional
            Path (or prefix) appended to the stage reference in the pipe definition.
        modified_after: datetime, optional
            Timestamp (in ISO-8601 format) of the oldest data files to copy into the Snowpipe ingest queue based on
            the LAST_MODIFIED date (i.e. date when a file was staged).

        Examples
        ________
        Using a pipe reference to refresh it:

        >>> pipe_reference.refresh(prefix="your_prefix")
        """
        self._api.refresh_pipe(
            self.database.name, self.schema.name, self.name, if_exists, prefix, modified_after, async_req=False
        )

    @api_telemetry
    def refresh_async(
        self, if_exists: Optional[bool] = None, prefix: Optional[str] = None, modified_after: Optional[datetime] = None
    ) -> PollingOperation[None]:
        """An asynchronous version of :func:`refresh`.

        Refer to :class:`~snowflake.core.PollingOperation` for more information on asynchronous execution and
        the return type.
        """ # noqa: D401
        future = self._api.refresh_pipe(
            self.database.name, self.schema.name, self.name, if_exists, prefix, modified_after, async_req=True
        )
        return PollingOperations.empty(future)
