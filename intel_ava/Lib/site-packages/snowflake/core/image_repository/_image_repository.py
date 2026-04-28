from collections.abc import Iterator
from typing import TYPE_CHECKING, Optional

from pydantic import StrictStr

from snowflake.core import PollingOperation
from snowflake.core._common import (
    CreateMode,
    SchemaObjectCollectionParent,
    SchemaObjectReferenceMixin,
)
from snowflake.core._internal.telemetry import api_telemetry
from snowflake.core._internal.utils import deprecated
from snowflake.core._operation import PollingOperations


if TYPE_CHECKING:
    from snowflake.core.schema import SchemaResource


from snowflake.core.image_repository._generated import ImageRepositoryApi
from snowflake.core.image_repository._generated.api_client import StoredProcApiClient
from snowflake.core.image_repository._generated.models.image import Image
from snowflake.core.image_repository._generated.models.image_repository import (
    ImageRepositoryModel as ImageRepository,
)


class ImageRepositoryCollection(SchemaObjectCollectionParent["ImageRepositoryResource"]):
    """Represents the collection operations on the Snowflake Image Repository resource.

    With this collection, you can create, iterate through, and search for image repositories that you have access to
    in the current context.

    Examples
    ________
    Creating an image repository instance:

    >>> image_repository = ImageRepository(name="my_image_repository")
    >>> image_repositories = root.databases["my_db"].schemas["my_schema"].image_repositories
    >>> image_repositories.create(image_repository)
    """

    def __init__(self, schema: "SchemaResource"):
        super().__init__(schema, ImageRepositoryResource)
        self._api = ImageRepositoryApi(
            root=self.root, resource_class=self._ref_class, sproc_client=StoredProcApiClient(root=self.root)
        )

    @api_telemetry
    def create(
        self,
        image_repository: ImageRepository,
        mode: CreateMode = CreateMode.error_if_exists,
    ) -> "ImageRepositoryResource":
        """Create an image repository in Snowflake.

        Parameters
        __________
        image_repository: ImageRepository
            The ``ImageRepository`` object, together with the ``ImageRepository``'s properties:
            name;
        mode: CreateMode, optional
            One of the following enum values.

            ``CreateMode.error_if_exists``: Throw an :class:`snowflake.core.exceptions.ConflictError` if the image
            repository already exists in Snowflake. Equivalent to SQL ``create image repository <name> ...``.

            ``CreateMode.or_replace``: Replace if the image repository already exists in Snowflake. Equivalent to SQL
            ``create or replace image repository <name> ...``.

            ``CreateMode.if_not_exists``: Do nothing if the image repository already exists in Snowflake.
            Equivalent to SQL ``create image repository <name> if not exists...``

            Default value is ``CreateMode.error_if_exists``.

        Examples
        ________
        Creating an image repository, replacing an existing image repository with the same name:

        >>> image_repository = ImageRepository(name="my_image_repository")
        >>> image_repositories = root.databases["my_db"].schemas["my_schema"].image_repositories
        >>> image_repositories.create(image_repository, mode=CreateMode.or_replace)
        """
        real_mode = CreateMode[mode].value
        self._api.create_image_repository(
            self.database.name,
            self.schema.name,
            image_repository._to_model(),
            StrictStr(real_mode),
            async_req=False,
        )
        return self[image_repository.name]

    @api_telemetry
    def create_async(
        self,
        image_repository: ImageRepository,
        mode: CreateMode = CreateMode.error_if_exists,
    ) -> PollingOperation["ImageRepositoryResource"]:
        """An asynchronous version of :func:`create`.

        Refer to :class:`~snowflake.core.PollingOperation` for more information on asynchronous execution and
        the return type.
        """ # noqa: D401
        real_mode = CreateMode[mode].value
        future = self._api.create_image_repository(
            self.database.name,
            self.schema.name,
            image_repository._to_model(),
            StrictStr(real_mode),
            async_req=True,
        )
        return PollingOperation(future, lambda _: self[image_repository.name])

    @api_telemetry
    def iter(
        self,
        *,
        like: Optional[str] = None,
    ) -> Iterator[ImageRepository]:
        """Iterate through ``ImageRepository`` objects from Snowflake, filtering on any optional 'like' pattern.

        Parameters
        __________
        like: str, optional
            A case-insensitive string functioning as a filter, with support for SQL
            wildcard characters (% and _).

        Examples
        ________
        Showing all image repositories that you have access to see:

        >>> image_repositories = image_repository_collection.iter()

        Showing information of the exact image repository you want to see:

        >>> image_repositories = image_repository_collection.iter(like="your-image-repository-name")

        Showing image repositories starting with 'your-image-repository-name-':

        >>> image_repositories = image_repository_collection.iter(like="your-image-repository-name-%")

        Using a for loop to retrieve information from iterator:

        >>> for image_repository in image_repositories:
        >>>     print(image_repository.name)
        """
        image_repositories = self._api.list_image_repositories(
            self.database.name,
            self.schema.name,
            StrictStr(like) if like is not None else None,
            async_req=False,
        )

        return map(ImageRepository._from_model, iter(image_repositories))


    @api_telemetry
    def iter_async(
        self,
        *,
        like: Optional[str] = None,
    ) -> PollingOperation[Iterator[ImageRepository]]:
        """An asynchronous version of :func:`iter`.

        Refer to :class:`~snowflake.core.PollingOperation` for more information on asynchronous execution and
        the return type.
        """ # noqa: D401
        future = self._api.list_image_repositories(
            self.database.name,
            self.schema.name,
            StrictStr(like) if like is not None else None,
            async_req=True,
        )
        return PollingOperation(future, lambda rest_models: map(ImageRepository._from_model, iter(rest_models)))


class ImageRepositoryResource(SchemaObjectReferenceMixin[ImageRepositoryCollection]):
    """Represents a reference to a Snowflake image repository.

    With this image repository reference, you can create and fetch information about image repositories, as well as
    perform certain actions on them.
    """

    def __init__(self, name: str, collection: ImageRepositoryCollection):
        self.name = name
        self.collection = collection

    @api_telemetry
    def fetch(self) -> ImageRepository:
        """Fetch the details of an image repository.

        Examples
        ________
        Fetching a reference to an image repository to print its name and properties:

        >>> my_image_repository = image_repository_reference.fetch()
        >>> print(my_image_repository.name)
        """
        return ImageRepository._from_model(
            self.collection._api.fetch_image_repository(
                self.database.name, self.schema.name, self.name, async_req=False
            )
        )

    @api_telemetry
    def fetch_async(self) -> PollingOperation[ImageRepository]:
        """An asynchronous version of :func:`fetch`.

        Refer to :class:`~snowflake.core.PollingOperation` for more information on asynchronous execution and
        the return type.
        """ # noqa: D401
        future = self.collection._api.fetch_image_repository(
            self.database.name, self.schema.name, self.name, async_req=True
        )
        return PollingOperation(future, lambda rest_model: ImageRepository._from_model(rest_model))


    @api_telemetry
    @deprecated("drop")
    def delete(self) -> None:
        """Delete this image repository.

        Examples
        ________
        Deleting an image repository using its reference:

        >>> image_repository_reference.delete()
        """
        self.drop()

    @api_telemetry
    def drop(self, if_exists: Optional[bool] = None) -> None:
        """Drop this image repository.

        Parameters
        __________
        if_exists: bool, optional
            Check the existence of this image repository before dropping it.
            Default is ``None``, which is equivalent to ``False``.

        Examples
        ________
        Deleting an image repository using its reference:

        >>> image_repository_reference.drop()
        """
        self.collection._api.delete_image_repository(
            self.database.name, self.schema.name, self.name, if_exists, async_req=False
        )

    @api_telemetry
    def drop_async(self, if_exists: Optional[bool] = None) -> PollingOperation[None]:
        """An asynchronous version of :func:`drop`.

        Refer to :class:`~snowflake.core.PollingOperation` for more information on asynchronous execution and
        the return type.
        """ # noqa: D401
        future = self.collection._api.delete_image_repository(
            self.database.name, self.schema.name, self.name, if_exists, async_req=True
        )
        return PollingOperations.empty(future)

    @api_telemetry
    def list_images_in_repository(self) -> Iterator[Image]:
        """List the images in an image repository.

        Examples
        ________
        Listing images in an image repository using an image repository reference:

        >>> for image in image_repository_reference.list_images_in_repository():
        ...     print(image.name)
        """
        images = self.collection._api.list_images_in_repository(
            self.database.name, self.schema.name, self.name, async_req=False
        )
        return iter(images)

    @api_telemetry
    def list_images_in_repository_async(self) -> PollingOperation[Iterator[Image]]:
        """An asynchronous version of :func:`list_images_in_repository`.

        Refer to :class:`~snowflake.core.PollingOperation` for more information on asynchronous execution and
        the return type.
        """ # noqa: D401
        future = self.collection._api.list_images_in_repository(
            self.database.name, self.schema.name, self.name, async_req=True
        )
        return PollingOperations.iterator(future)
