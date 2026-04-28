from collections.abc import Iterator
from concurrent.futures import Future
from typing import TYPE_CHECKING, Annotated, Any, Literal, Optional, overload

from pydantic import Field, StrictStr

from snowflake.core import PollingOperation
from snowflake.core._common import (
    CreateMode,
    SchemaObjectCollectionParent,
    SchemaObjectReferenceMixin,
)
from snowflake.core._internal.telemetry import api_telemetry
from snowflake.core._operation import PollingOperations
from snowflake.core.notebook._generated.api import NotebookApi
from snowflake.core.notebook._generated.api_client import StoredProcApiClient
from snowflake.core.notebook._generated.models.notebook import Notebook
from snowflake.core.notebook._generated.models.version_details import VersionDetails  #noqa


if TYPE_CHECKING:
    from snowflake.core.schema import SchemaResource


class NotebookCollection(SchemaObjectCollectionParent["NotebookResource"]):
    """Represents the collection operations of the Snowflake Notebook resource.

    With this collection, you can create, iterate through, and search for notebooks that you have access to
    in the current context.

    Examples
    ________
    Creating a notebook instance:

    >>> notebooks = root.databases["my_db"].schemas["my_schema"].notebooks
    >>> new_notebook = Notebook(
    ...     name="my_notebook",
    ...     comment="This is a notebook"
    ... )
    >>> notebooks.create(new_notebook)
    """

    def __init__(self, schema: "SchemaResource"):
        super().__init__(schema, NotebookResource)
        self._api = NotebookApi(
            root=self.root,
            resource_class=self._ref_class,
            sproc_client=StoredProcApiClient(root=self.root),
        )

    @api_telemetry
    def create(
        self,
        notebook: Notebook,
        *,
        mode: CreateMode = CreateMode.error_if_exists,
    ) -> "NotebookResource":
        """Create a notebook in Snowflake.

        Parameters
        __________
        notebook: Notebook
            The ``Notebook`` object that you want to create in Snowflake.
        mode: CreateMode, optional
            One of the following strings.

            ``CreateMode.error_if_exists``: Throw an :class:`snowflake.core.exceptions.ConflictError`
            if the notebook already exists in Snowflake. Equivalent to SQL ``create notebook <name> ...``.

            ``CreateMode.or_replace``: Replace if the notebook already exists in Snowflake. Equivalent to SQL
            ``create or replace notebook <name> ...``.

            ``CreateMode.if_not_exists``: Do nothing if the notebook already exists in Snowflake. Equivalent to SQL
            ``create notebook <name> if not exists...``

            Default value is ``CreateMode.error_if_exists``.

        Examples
        ________
        Creating a notebook in Snowflake and getting the reference to it:

        >>> notebook = Notebook(
        ...     name="my_notebook",
        ...     version="notebook_ver1",
        ...     comment="This is a notebook"
        ... )
        >>> # Use the notebook collection created before to create a reference to the notebook resource
        >>> # in Snowflake.
        >>> notebook_reference = notebook_collection.create(notebook)
        """
        real_mode = CreateMode[mode].value
        self._api.create_notebook(
            self.database.name, self.schema.name, notebook, create_mode=StrictStr(real_mode), async_req=False
        )
        return NotebookResource(notebook.name, self)

    @api_telemetry
    def create_async(
        self,
        notebook: Notebook,
        *,
        mode: CreateMode = CreateMode.error_if_exists,
    ) -> PollingOperation["NotebookResource"]:
        """An asynchronous version of :func:`create`.

        Refer to :class:`~snowflake.core.PollingOperation` for more information on asynchronous execution and
        the return type.
        """ # noqa: D401
        real_mode = CreateMode[mode].value
        future = self._api.create_notebook(
            self.database.name, self.schema.name, notebook, create_mode=StrictStr(real_mode), async_req=True
        )
        return PollingOperation(future, lambda _: NotebookResource(notebook.name, self))

    @api_telemetry
    def iter(
        self,
        *,
        like: Optional[str] = None,
        starts_with: Optional[StrictStr] = None,
        show_limit: Optional[Annotated[int, Field(le=10000, strict=True, ge=1)]] = None,
        from_name: Optional[StrictStr] = None,
    ) -> Iterator[Notebook]:
        """Iterate through ``Notebook`` objects in Snowflake, filtering on any optional `like` pattern.

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
        Showing all notebooks that you have access to see:

        >>> notebooks = notebook_collection.iter()

        Showing information of the exact notebook you want to see:

        >>> notebooks = notebook_collection.iter(like="your-notebook-name")

        Showing notebooks starting with 'your-notebook-name':

        >>> notebooks = notebook_collection.iter(like="your-notebook-name%")

        Using a for-loop to retrieve information from iterator:

        >>> for notebook in notebooks:
        ...     print(notebook.name, notebook.version, notebook.user_packages)
        """
        notebooks = self._api.list_notebooks(database=self.database.name, var_schema=self.schema.name, like=like,
                                             starts_with=starts_with, show_limit=show_limit, from_name=from_name,
                                             async_req=False)

        return iter(notebooks)

    @api_telemetry
    def iter_async(
        self,
        *,
        like: Optional[str] = None,
        starts_with: Optional[StrictStr] = None,
        show_limit: Optional[Annotated[int, Field(le=10000, strict=True, ge=1)]] = None,
        from_name: Optional[StrictStr] = None,
    ) -> PollingOperation[Iterator[Notebook]]:
        """An asynchronous version of :func:`iter`.

        Refer to :class:`~snowflake.core.PollingOperation` for more information on asynchronous execution and
        the return type.
        """ # noqa: D401
        future = self._api.list_notebooks(database=self.database.name, var_schema=self.schema.name, like=like,
                                             starts_with=starts_with, show_limit=show_limit, from_name=from_name,
                                             async_req=True)
        return PollingOperations.iterator(future)


class NotebookResource(SchemaObjectReferenceMixin[NotebookCollection]):
    """Represents a reference to a Snowflake notebook.

    With this notebook reference, you can fetch information about notebooks, as well
    as perform certain actions on them: renaming, executing, committing, and managing
    versions.
    """

    def __init__(self, name: str, collection: NotebookCollection) -> None:
        self.collection = collection
        self.name = name

    @property
    def _api(self) -> NotebookApi:
        """Get the Notebook API object."""
        return self.collection._api

    @api_telemetry
    def fetch(self) -> Notebook:
        """Fetch the details of a notebook resource.

        Examples
        ________
        Fetching a notebook using its reference:

        >>> notebook = notebook_reference.fetch()
        >>> print(notebook.name, notebook.comment)
        """
        return self._api.fetch_notebook(
            self.database.name,
            self.schema.name,
            self.name,
            async_req=False,
        )

    @api_telemetry
    def fetch_async(self) -> PollingOperation[Notebook]:
        """An asynchronous version of :func:`fetch`.

        Refer to :class:`~snowflake.core.PollingOperation` for more information on asynchronous execution and
        the return type.
        """ # noqa: D401
        future = self._api.fetch_notebook(
            self.database.name,
            self.schema.name,
            self.name,
            async_req=True,
        )
        return PollingOperations.identity(future)

    @api_telemetry
    def drop(self, if_exists: bool = False) -> None:
        """Drop this notebook.

        Parameters
        __________
        if_exists: bool, optional
            If ``True``, does not throw an exception if the notebook does not exist. The default is ``False``.

        Examples
        ________
        Deleting a notebook using its reference:

        >>> notebook_reference.drop()

        Using a notebook reference to delete a notebook if it exists:

        >>> notebook_reference.drop(if_exists=True)
        """
        self._api.delete_notebook(self.database.name, self.schema.name, self.name, if_exists, async_req=False)

    @api_telemetry
    def drop_async(self, if_exists: bool = False) -> PollingOperation[None]:
        """An asynchronous version of :func:`drop`.

        Refer to :class:`~snowflake.core.PollingOperation` for more information on asynchronous execution and
        the return type.
        """ # noqa: D401
        future = self._api.delete_notebook(self.database.name, self.schema.name, self.name, if_exists, async_req=True)
        return PollingOperations.empty(future)

    @api_telemetry
    def rename(
        self,
        target_name: str,
        target_database: Optional[str] = None,
        target_schema: Optional[str] = None,
        if_exists: Optional[bool] = None
    ) -> None:
        """Rename this notebook.

        Parameters
        __________
        target_name: str
            The new name of the notebook
        target_database: str, optional
            The new database name of the notebook. If not provided,
            the current database name is used. The default is ``None``.
        target_schema: str, optional
            The new schema name of the notebook. If not provided,
            the current schema name is used. The default is ``None``.
        if_exists: bool, optional
            Whether to check for the existence of notebook before
            renaming. The default is ``None``, which is equivalent to ``False``.

        Examples
        ________
        Renaming this notebook using its reference:

        >>> notebook_reference.rename("my_other_notebook")

        Renaming this notebook if it exists:

        >>> notebook_reference.rename("my_other_notebook", if_exists = True)
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

    @api_telemetry
    def execute(
        self,
    ) -> None:
        """Execute this notebook.

        Examples
        ________
        Executing a notebook using its reference:

        >>> notebook_reference.execute()
        """
        self._api.execute_notebook(database=self.database.name, var_schema=self.schema.name, name=self.name,
                                   async_req=False)

    @api_telemetry
    def execute_async(
        self,
    ) -> PollingOperation[None]:
        """An asynchronous version of :func:`execute`.

        Refer to :class:`~snowflake.core.PollingOperation` for more information on asynchronous execution and
        the return type.
        """ # noqa: D401
        future = self._api.execute_notebook(database=self.database.name, var_schema=self.schema.name, name=self.name,
                                   async_req=True)
        return PollingOperations.empty(future)

    @api_telemetry
    def commit(
        self,
        version: Optional[str] = None,
        comment: Optional[str] = None
    ) -> None:
        """Commit the LIVE version of the notebook to the Git.

        If a Git connection is set up for the notebook, commits the
        LIVE version of the notebook to the Git repository.

        If no Git repository is set up for the notebook, running this
        command sets the LIVE version to null and increments the
        auto-generated version alias.

        Parameters
        __________
        version: str, optional
            The alias of the version of the notebook that you want to
            commit. The default is ``None``, which is equivalent to
            ``"LIVE"``.
        comment: str, optional
            An optional comment to add to the commit.

        Examples
        ________
        Committing a notebook using its reference:

        >>> notebook_reference.commit(version="prod-1.1.0",
        ...                           comment="prod release 1.1.0")
        """
        self._api.commit_notebook(database=self.database.name, var_schema=self.schema.name, name=self.name,
                                  version=version, comment=comment,
                                  async_req=False)

    @api_telemetry
    def commit_async(
        self,
        version: Optional[str] = None,
        comment: Optional[str] = None
    ) -> PollingOperation[None]:
        """An asynchronous version of :func:`commit`.

        Refer to :class:`~snowflake.core.PollingOperation` for more information on asynchronous execution and
        the return type.
        """ # noqa: D401
        future = self._api.commit_notebook(database=self.database.name, var_schema=self.schema.name, name=self.name,
                                  version=version, comment=comment,
                                  async_req=True)
        return PollingOperations.empty(future)

    @api_telemetry
    def add_live_version(
        self,
        from_last: Optional[bool] = None,
        comment: Optional[str] = None
    ) -> None:
        """Add a LIVE version to the notebook.

        The LIVE version is the version that runs when the notebook is
        executed.

        Parameters
        __________
        from_last: bool, optional
            If ``True``, the LIVE version is set to the LAST version of
            the notebook. The default is ``None``, which is equivalent to ``False``.
        comment: str, optional
            An optional comment to for the version of the notebook.

        Examples
        ________
        Adding a LIVE version to this notebook using its reference:

        >>> notebook_reference.add_live_version(from_last=True,
        ...                                     comment="new live version")
        """
        self._api.add_live_version_notebook(database=self.database.name, var_schema=self.schema.name, name=self.name,
                                            from_last=from_last, comment=comment,
                                            async_req=False)
    @api_telemetry
    def add_live_version_async(
        self,
        from_last: Optional[bool] = None,
        comment: Optional[str] = None
    ) -> PollingOperation[None]:
        """An asynchronous version of :func:`add_live_version`.

        Refer to :class:`~snowflake.core.PollingOperation` for more information on asynchronous execution and
        the return type.
        """ # noqa: D401
        future = self._api.add_live_version_notebook(
            database=self.database.name,
            var_schema=self.schema.name,
            name=self.name,
            from_last=from_last,
            comment=comment,
            async_req=True,
        )
        return PollingOperations.empty(future)

    @overload
    def _rename(
        self,
        target_name: str,
        target_database: Optional[str],
        target_schema: Optional[str],
        if_exists: Optional[bool],
        async_req: Literal[True],
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

        result_or_future = self.collection._api.rename_notebook(
            database=self.database.name,
            var_schema=self.schema.name,
            name=self.name,
            target_database=target_database,
            target_schema=target_schema,
            target_name=target_name,
            if_exists=if_exists,
            async_req=async_req,
        )

        def finalize(_: Any) -> None:
            if target_database != self.database.name or target_schema != self.schema.name:
                self.collection = self.root.databases[target_database].schemas[target_schema].notebooks

            self.name = target_name

        if isinstance(result_or_future, Future):
            return PollingOperation(result_or_future, finalize)

        finalize(None)
        return None
