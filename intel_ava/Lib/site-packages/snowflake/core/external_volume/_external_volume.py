from collections.abc import Iterator
from typing import TYPE_CHECKING, Optional

from pydantic import StrictStr

from snowflake.core import PollingOperation
from snowflake.core._common import AccountObjectCollectionParent, CreateMode, ObjectReferenceMixin
from snowflake.core._internal.telemetry import api_telemetry
from snowflake.core._operation import PollingOperations
from snowflake.core.external_volume._generated.api import ExternalVolumeApi
from snowflake.core.external_volume._generated.api_client import StoredProcApiClient
from snowflake.core.external_volume._generated.models.external_volume import ExternalVolume


if TYPE_CHECKING:
    from snowflake.core import Root


class ExternalVolumeCollection(AccountObjectCollectionParent["ExternalVolumeResource"]):
    """Represents the collection operations of the Snowflake External Volume resource.

    With this collection, you can create, iterate through, and search for external volume that you have access to
    in the current context.

    Examples
    ________
    Creating an external volume instance:

    >>> external_volume_collection = root.external_volumes
    >>> external_volume = ExternalVolume(
    ...     name="MY_EXTERNAL_VOLUME",
    ...     storage_location=StorageLocationS3(
    ...         name="abcd-my-s3-us-west-2",
    ...         storage_base_url="s3://MY_EXAMPLE_BUCKET/",
    ...         storage_aws_role_arn="arn:aws:iam::123456789022:role/myrole",
    ...         encryption=Encryption(type="AWS_SSE_KMS",
    ...                                kms_key_id="1234abcd-12ab-34cd-56ef-1234567890ab")
    ...     ),
    ...     comment="This is my external volume",
    ... )
    >>> external_volume_collection.create(external_volume)
    """

    def __init__(self, root: "Root") -> None:
        super().__init__(root, ref_class=ExternalVolumeResource)
        self._api = ExternalVolumeApi(
            root=root, resource_class=self._ref_class, sproc_client=StoredProcApiClient(root=self.root)
        )

    @api_telemetry
    def create(
        self,
        external_volume: ExternalVolume,
        *,
        mode: CreateMode = CreateMode.error_if_exists,
    ) -> "ExternalVolumeResource":
        """Create an external volume in Snowflake.

        Parameters
        __________
        external_volume: ExternalVolume
        mode: CreateMode, optional
            One of the following strings.

            ``CreateMode.error_if_exists``: Throw an :class:`snowflake.core.exceptions.ConflictError`
            if the external volume already exists in Snowflake. Equivalent to SQL ``create external volume <name> ...``.

            ``CreateMode.or_replace``: Replace if the external volume already exists in Snowflake. Equivalent to SQL
            ``create or replace external volume <name> ...``.

            ``CreateMode.if_not_exists``: Do nothing if the external volume already exists in Snowflake.
            Equivalent to SQL ``create external volume <name> if not exists...``

            Default value is ``CreateMode.error_if_exists``.

        Examples
        ________
        Creating an external volume in Snowflake and getting a reference to it:

        >>> external_volume_parameters = ExternalVolume(
        ...     name="MY_EXTERNAL_VOLUME",
        ...     storage_location=StorageLocationS3(
        ...         name="abcd-my-s3-us-west-2",
        ...         storage_base_url="s3://MY_EXAMPLE_BUCKET/",
        ...         storage_aws_role_arn="arn:aws:iam::123456789022:role/myrole",
        ...         encryption=Encryption(type="AWS_SSE_KMS",
        ...                                kms_key_id="1234abcd-12ab-34cd-56ef-1234567890ab")
        ...     ),
        ...     comment="This is my external volume",
        ... )
        >>> # Use the external volume collection created before to create a referece to the external volume resource
        >>> # in Snowflake.
        >>> external_volume_reference = external_volume_collection.create(external_volume_parameters)
        """
        real_mode = CreateMode[mode].value
        self._api.create_external_volume(
            external_volume=external_volume,
            create_mode=StrictStr(real_mode),
            async_req=False,
        )
        return self[external_volume.name]

    @api_telemetry
    def create_async(
        self,
        external_volume: ExternalVolume,
        *,
        mode: CreateMode = CreateMode.error_if_exists,
    ) -> PollingOperation["ExternalVolumeResource"]:
        """An asynchronous version of :func:`create`.

        Refer to :class:`~snowflake.core.PollingOperation` for more information on asynchronous execution and
        the return type.
        """ # noqa: D401
        real_mode = CreateMode[mode].value
        future = self._api.create_external_volume(
            external_volume=external_volume,
            create_mode=StrictStr(real_mode),
            async_req=True,
        )
        return PollingOperation(future, lambda _: self[external_volume.name])

    @api_telemetry
    def iter(
        self,
        *,
        like: Optional[str] = None,
    ) -> Iterator[ExternalVolume]:
        """Iterate through ``ExternalVolume`` objects in Snowflake, filtering on any optional ``like`` pattern.

        Parameters
        __________
        like: str, optional
            A case-insensitive string functioning as a filter, with support for SQL
            wildcard characters (% and _).

        Examples
        ________
        Showing all external volumes you have access to see:

        >>> external_volumes = external_volume_collection.iter()

        Showing information of the exact external volume you want to see:

        >>> external_volumes = external_volume_collection.iter(like="your-external-volume-name")

        Showing external volumes starting with 'your-external-volume-name':

        >>> external_volumes = external_volume_collection.iter(like="your-external-volume-name%")

        Using a for loop to retrieve information from iterator:

        >>> for external_volume in external_volumes:
        >>>     print(external_volume.name, external_volume.comment)
        """
        external_volumes = self._api.list_external_volumes(
            StrictStr(like) if like is not None else None,
            async_req=False,
        )

        return iter(external_volumes)


    @api_telemetry
    def iter_async(
        self,
        *,
        like: Optional[str] = None,
    ) -> PollingOperation[Iterator[ExternalVolume]]:
        """An asynchronous version of :func:`iter`.

        Refer to :class:`~snowflake.core.PollingOperation` for more information on asynchronous execution and
        the return type.
        """ # noqa: D401
        future = self._api.list_external_volumes(
            StrictStr(like) if like is not None else None,
            async_req=True,
        )
        return PollingOperations.iterator(future)


class ExternalVolumeResource(ObjectReferenceMixin[ExternalVolumeCollection]):
    """Represents a reference to a Snowflake external volume.

    With this external volume reference, you can fetch information about external volumes, as well
    as perform certain actions on them.
    """

    def __init__(self, name: str, collection: ExternalVolumeCollection) -> None:
        self.name = name
        self.collection = collection

    @property
    def _api(self) -> ExternalVolumeApi:
        """Get the External Volume API object."""
        return self.collection._api

    @api_telemetry
    def fetch(self) -> ExternalVolume:
        """Fetch the details of an external volume resource.

        Examples
        ________
        Fetching an external volume using its reference:
        >>> external_volume = external_volume_reference.fetch()
        # Accessing information of the external volume with external volume instance.
        >>> print(external_volume.name, external_volume.comment)
        """
        return self._api.fetch_external_volume(self.name, async_req=False)

    @api_telemetry
    def fetch_async(self) -> PollingOperation[ExternalVolume]:
        """An asynchronous version of :func:`fetch`.

        Refer to :class:`~snowflake.core.PollingOperation` for more information on asynchronous execution and
        the return type.
        """ # noqa: D401
        future = self._api.fetch_external_volume(self.name, async_req=True)
        return PollingOperations.identity(future)

    @api_telemetry
    def drop(self, if_exists: Optional[bool] = None) -> None:
        """Drop this external volume.

        Parameters
        __________
        if_exist: bool, optional
            If ``True``, does not throw an exception if the external volume does not exist. The default is ``None``,
            which behaves equivalently to it being ``False``.

        Examples
        ________
        Deleting an external volume using its reference:

        >>> external_volume_reference.drop()

        Using an external volume reference to delete an external volume if it exists:

        >>> external_volume_reference.drop(if_exist=True)
        """
        self._api.delete_external_volume(self.name, if_exists, async_req=False)

    @api_telemetry
    def drop_async(self, if_exists: Optional[bool] = None) -> PollingOperation[None]:
        """An asynchronous version of :func:`drop`.

        Refer to :class:`~snowflake.core.PollingOperation` for more information on asynchronous execution and
        the return type.
        """ # noqa: D401
        future = self._api.delete_external_volume(self.name, if_exists, async_req=True)
        return PollingOperations.empty(future)

    @api_telemetry
    def undrop(self) -> None:
        """Undrop this external volume.

        Examples
        ________
        Restoring an external volume using its reference:

        >>> external_volume_reference.undrop()
        """
        self._api.undrop_external_volume(self.name, async_req=False)

    @api_telemetry
    def undrop_async(self) -> PollingOperation[None]:
        """An asynchronous version of :func:`undrop`.

        Refer to :class:`~snowflake.core.PollingOperation` for more information on asynchronous execution and
        the return type.
        """ # noqa: D401
        future = self._api.undrop_external_volume(self.name, async_req=True)
        return PollingOperations.empty(future)
