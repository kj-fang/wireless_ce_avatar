from collections.abc import Iterator
from concurrent.futures import Future
from typing import TYPE_CHECKING, Annotated, Literal, Optional, Union, overload

from pydantic import Field, StrictStr

from snowflake.core import FQN, PollingOperation
from snowflake.core._common import (
    Clone,
    CreateMode,
    SchemaObjectCollectionParent,
    SchemaObjectReferenceMixin,
)
from snowflake.core._operation import PollingOperations

from .._internal.telemetry import api_telemetry
from ._generated import SuccessResponse
from ._generated.api import StreamApi
from ._generated.api_client import StoredProcApiClient
from ._generated.models.stream import Stream
from ._generated.models.stream_clone import StreamClone


if TYPE_CHECKING:
    from snowflake.core.schema import SchemaResource

class StreamCollection(SchemaObjectCollectionParent["StreamResource"]):
    """Represents the collection operations on the Snowflake Stream resource.

    With this collection, you can create, iterate through, and fetch streams
    that you have access to in the current context.
    """

    def __init__(self, schema: "SchemaResource"):
        super().__init__(schema, StreamResource)
        self._api = StreamApi(
            root=self.root,
            resource_class=self._ref_class,
            sproc_client=StoredProcApiClient(root=self.root)
        )

    @overload
    @api_telemetry
    def create(
        self, stream: str,
        *,
        clone_stream: Union[str, Clone],
        mode: CreateMode=CreateMode.error_if_exists,
        copy_grants: Optional[bool] = False,
    ) -> "StreamResource":
        ...
    @overload
    @api_telemetry
    def create(
        self, stream: Stream,
        *,
        mode: CreateMode=CreateMode.error_if_exists,
        copy_grants: Optional[bool] = False,
    ) -> "StreamResource":
        ...
    @api_telemetry
    def create(
        self, stream: Union[str, Stream],
        *,
        clone_stream: Optional[Union[str, Clone]] = None,
        mode: CreateMode=CreateMode.error_if_exists,
        copy_grants: Optional[bool] = False,
    ) -> "StreamResource":
        """Create a stream in Snowflake.

        There are two ways to create a stream: by cloning or by building from scratch.

        **Cloning an existing stream**

        Parameters
        __________
        stream: str
            The new stream's name
        clone_stream: str or Clone object
            The name of stream to be cloned
        mode: CreateMode, optional
            One of the following enum values:

            ``CreateMode.error_if_exists``: Throw an :class:`snowflake.core.exceptions.ConflictError`
            if the stream already exists in Snowflake.  Equivalent to SQL ``create stream <name> ...``.

            ``CreateMode.or_replace``: Replace if the stream already exists in Snowflake. Equivalent to SQL
            ``create or replace stream <name> ...``.

            ``CreateMode.if_not_exists``: Do nothing if the stream already exists in Snowflake.
            Equivalent to SQL ``create stream <name> if not exists...``

            Default is ``CreateMode.error_if_exists``.

        copy_grants: bool, optional
            Whether to enable copy grants when creating the object. Default is ``False``.

        Examples
        ________
        Cloning a Stream instance:

        >>> streams = schema.streams
        >>> streams.create(
        ...     "new_stream_name",
        ...     clone_stream="stream_name_to_be_cloned",
        ...     mode=CreateMode.if_not_exists,
        ...     copy_grants=True
        ... )

        Cloning a Stream instance in a different database and schema

        >>> streams = schema.streams
        >>> streams.create(
        ...     "new_stream_name",
        ...     clone_stream="stream_database_name.stream_schema_name.stream_name_to_be_cloned",
        ...     mode=CreateMode.if_not_exists,
        ...     copy_grants=True
        ... )

        **Creating a stream from scratch**

        Parameters
        __________
        stream: Stream
            The details of ``Stream`` object, together with ``Stream``'s properties:
            name; comment is optional
            stream_source: ``StreamSource`` object, one of:
            ``StreamSourceStage``, ``StreamSourceTable``, ``StreamSourceView``.

        mode: CreateMode, optional
            One of the following enum values:

            ``CreateMode.error_if_exists``: Throw an :class:`snowflake.core.exceptions.ConflictError`
            if the stream already exists in Snowflake.  Equivalent to SQL ``create stream <name> ...``.

            ``CreateMode.or_replace``: Replace if the stream already exists in Snowflake. Equivalent to SQL
            ``create or replace stream <name> ...``.

            ``CreateMode.if_not_exists``: Do nothing if the stream already exists in Snowflake.
            Equivalent to SQL ``create stream <name> if not exists...``

            Default is ``CreateMode.error_if_exists``.

        copy_grants: bool, optional
            Whether to enable copy grants when creating the object. Default is ``False``.

        Examples
        ________
        Creating a stream instance by source table:

        >>> streams.create(
        ...     Stream(
        ...         name = "new_stream_name",
        ...         stream_source = StreamSourceTable(
        ...             point_of_time = PointOfTimeOffset(reference="before", offset="1"),
        ...             name = "my_source_table_name"
        ...             append_only = True,
        ...             show_initial_rows = False,
        ...             comment = "create stream by table"
        ...         )
        ...     ),
        ...     mode=CreateMode.if_not_exists,
        ...     copy_grants=True
        ... )

        Creating a stream instance by source view:

        >>> streams.create(
        ...     Stream(
        ...         name = "new_stream_name",
        ...         stream_source = StreamSourceView(
        ...             point_of_time = PointOfTimeOffset(reference="before", offset="1"),
        ...             name = "my_source_view_name"
        ...         )
        ...     ),
        ...     mode=CreateMode.if_not_exists,
        ...     copy_grants=True
        ... )

        Creating a stream instance by source directory table:

        >>> streams.create(
        ...     Stream(
        ...         name = "new_stream_name",
        ...         stream_source = StreamSourceStage(
        ...             point_of_time = PointOfTimeOffset(reference="before", offset="1"),
        ...             name = "my_source_directory_table_name"
        ...         )
        ...     ),
        ...     mode=CreateMode.if_not_exists,
        ...     copy_grants=True
        ... )
        """
        self._create(
            stream=stream,
            clone_stream=clone_stream,
            mode=mode,
            copy_grants=copy_grants,
            async_req=False,
        )
        return StreamResource(stream.name if isinstance(stream, Stream) else stream, self)

    @overload
    @api_telemetry
    def create_async(
        self, stream: str,
        *,
        clone_stream: Union[str, Clone],
        mode: CreateMode = CreateMode.error_if_exists,
        copy_grants: Optional[bool] = False,
    ) -> PollingOperation["StreamResource"]:
        ...

    @overload
    @api_telemetry
    def create_async(
        self, stream: Stream,
        *,
        mode: CreateMode = CreateMode.error_if_exists,
        copy_grants: Optional[bool] = False,
    ) -> PollingOperation["StreamResource"]:
        ...

    @api_telemetry
    def create_async(
        self, stream: Union[str, Stream],
        *,
        clone_stream: Optional[Union[str, Clone]] = None,
        mode: CreateMode = CreateMode.error_if_exists,
        copy_grants: Optional[bool] = False,
    ) -> PollingOperation["StreamResource"]:
        """An asynchronous version of :func:`create`.

        Refer to :class:`~snowflake.core.PollingOperation` for more information on asynchronous execution and
        the return type.
        """ # noqa: D401
        future = self._create(
            stream=stream,
            clone_stream=clone_stream,
            mode=mode,
            copy_grants=copy_grants,
            async_req=True,
        )
        return PollingOperation(
            future, lambda _: StreamResource(stream.name if isinstance(stream, Stream) else stream, self))

    @api_telemetry
    def iter(
        self,
        *,
        like: Optional[StrictStr] = None,
        starts_with: Optional[StrictStr] = None,
        show_limit: Optional[Annotated[int, Field(le=10000, strict=True, ge=1)]] = None,
        from_name: Optional[StrictStr] = None
    ) -> Iterator[Stream]:
        """Iterate through ``Stream`` objects from Snowflake, filtering on any optional 'like' pattern.

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
        Showing all streams that you have access to see:

        >>> streams = stream_collection.iter()

        Showing information of the exact stream you want to see:

        >>> streams = stream_collection.iter(like="your-stream-name")

        Showing streams starting with 'your-stream-name-':

        >>> streams = stream_collection.iter(like="your-stream-name-%")

        Using a for loop to retrieve information from iterator:

        >>> for stream in streams:
        ...     print(stream.name)
        """
        streams = self._api.list_streams(database=self.database.name, var_schema=self.schema.name, like=like,
                                     starts_with=starts_with, show_limit=show_limit, from_name=from_name,
                                     async_req=False)
        return iter(streams)

    @api_telemetry
    def iter_async(
        self,
        *,
        like: Optional[StrictStr] = None,
        starts_with: Optional[StrictStr] = None,
        show_limit: Optional[Annotated[int, Field(le=10000, strict=True, ge=1)]] = None,
        from_name: Optional[StrictStr] = None
    ) -> PollingOperation[Iterator[Stream]]:
        """An asynchronous version of :func:`iter`.

        Refer to :class:`~snowflake.core.PollingOperation` for more information on asynchronous execution and
        the return type.
        """ # noqa: D401
        future = self._api.list_streams(database=self.database.name, var_schema=self.schema.name, like=like,
                                     starts_with=starts_with, show_limit=show_limit, from_name=from_name,
                                     async_req=True)
        return PollingOperations.iterator(future)

    @overload
    def _create(
        self, stream: Union[str, Stream],
        clone_stream: Optional[Union[str, Clone]],
        mode: CreateMode,
        copy_grants: Optional[bool],
        async_req: Literal[True],
    ) -> Future[SuccessResponse]:
        ...

    @overload
    def _create(
        self, stream: Union[str, Stream],
        clone_stream: Optional[Union[str, Clone]],
        mode: CreateMode,
        copy_grants: Optional[bool],
        async_req: Literal[False],
    ) -> SuccessResponse:
        ...

    def _create(
        self, stream: Union[str, Stream],
        clone_stream: Optional[Union[str, Clone]],
        mode: CreateMode,
        copy_grants: Optional[bool],
        async_req: bool,
    ) -> Union[SuccessResponse, Future[SuccessResponse]]:
        real_mode = CreateMode[mode].value

        if clone_stream:
            # create stream by clone
            if not isinstance(stream, str):
                raise TypeError("Stream has to be str for clone")

            real_clone = Clone(source=clone_stream) if isinstance(clone_stream, str) else clone_stream

            req = StreamClone(
                name=stream,
            )

            source_stream_fqn = FQN.from_string(real_clone.source)
            return self._api.clone_stream(
                source_stream_fqn.database or self.database.name,
                source_stream_fqn.schema or self.schema.name,
                source_stream_fqn.name,
                create_mode=StrictStr(real_mode),
                target_database=self.database.name,
                target_schema=self.schema.name,
                stream_clone=req,
                copy_grants=copy_grants,
                async_req=async_req
            )

        if not isinstance(stream, Stream):
            raise TypeError("Stream has to be Stream object")
        return self._api.create_stream(
            self.database.name, self.schema.name, stream, create_mode=StrictStr(real_mode),
            copy_grants=copy_grants, async_req=async_req
        )

class StreamResource(SchemaObjectReferenceMixin[StreamCollection]):
    """Represents a reference to a Snowflake Stream resource.

    With this stream reference, you can create, update, and fetch information about streams, as well
    as perform certain actions on them.
    """

    def __init__(
        self,
        name: StrictStr,
        collection: StreamCollection
    ) -> None:
        self.name = name
        self.collection = collection

    @api_telemetry
    def fetch(self) -> Stream:
        """Fetch the details of a stream.

        Examples
        ________
        Fetching a stream reference to print its time of creation:

        >>> print(stream_reference.fetch().created_on)
        """
        return self.collection._api.fetch_stream(
            self.database.name, self.schema.name, self.name, async_req=False,
        )

    @api_telemetry
    def fetch_async(self) -> PollingOperation[Stream]:
        """An asynchronous version of :func:`fetch`.

        Refer to :class:`~snowflake.core.PollingOperation` for more information on asynchronous execution and
        the return type.
        """ # noqa: D401
        future = self.collection._api.fetch_stream(
            self.database.name, self.schema.name, self.name, async_req=True,
        )
        return PollingOperations.identity(future)

    @api_telemetry
    def drop(
        self,
        if_exists: Optional[bool] = None) -> None:
        """Drop this stream.

        Parameters
        __________
        if_exists: bool, optional
            Check the existence of this stream before dropping it.
            Default is ``None``, which is equivalent to ``False``.

        Examples
        ________
        Deleting a stream using its reference:

        >>> stream_reference.drop()

        Deleting a stream using its reference if it exists:

        >>> stream_reference.drop(if_exists = True)
        """
        self.collection._api.delete_stream(
            self.database.name, self.schema.name, self.name, if_exists=if_exists, async_req=False
        )

    @api_telemetry
    def drop_async(
        self,
        if_exists: Optional[bool] = None) -> PollingOperation[None]:
        """An asynchronous version of :func:`drop`.

        Refer to :class:`~snowflake.core.PollingOperation` for more information on asynchronous execution and
        the return type.
        """ # noqa: D401
        future = self.collection._api.delete_stream(
            self.database.name, self.schema.name, self.name, if_exists=if_exists, async_req=True
        )
        return PollingOperations.empty(future)
