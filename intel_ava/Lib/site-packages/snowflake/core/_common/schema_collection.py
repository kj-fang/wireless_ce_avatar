from typing import TYPE_CHECKING, Generic, Protocol, TypeVar

from snowflake.connector import SnowflakeConnection
from snowflake.core._common.base_collection import ObjectCollection, ObjectReferenceMixin


if TYPE_CHECKING:
    from snowflake.core import Root
    from snowflake.core.database import DatabaseResource
    from snowflake.core.schema import SchemaResource

T = TypeVar("T")


class SchemaObjectCollectionParent(ObjectCollection[T], Generic[T]):
    def __init__(self, schema: "SchemaResource", ref_class: type[T]) -> None:
        super().__init__(ref_class)
        self._schema = schema

    @property
    def _connection(self) -> SnowflakeConnection:
        return self.schema._connection

    @property
    def schema(self) -> "SchemaResource":
        """The SchemaResource this collection belongs to."""
        return self._schema

    @property
    def database(self) -> "DatabaseResource":
        """The DatabaseResource this collection belongs to."""
        return self.schema.collection.database

    @property
    def root(self) -> "Root":
        """The Root object this collection belongs to."""
        return self.database.collection.root

    def __repr__(self) -> str:
        type_name = type(self).__name__
        return f"<{type_name}: {self.schema.qualified_name!r}>"


class SchemaObjectReferenceProtocol(Protocol[T]):
    @property
    def collection(self) -> SchemaObjectCollectionParent[T]: ...

    @property
    def name(self) -> str: ...

    @property
    def database(self) -> "DatabaseResource": ...

    @property
    def schema(self) -> "SchemaResource": ...



class SchemaObjectReferenceMixin(Generic[T], ObjectReferenceMixin[SchemaObjectCollectionParent[T]]):
    @property
    def schema(self: SchemaObjectReferenceProtocol[T]) -> "SchemaResource":
        """The SchemaResource this reference belongs to."""
        return self.collection.schema

    @property
    def database(self: SchemaObjectReferenceProtocol[T]) -> "DatabaseResource":
        """The DatabaseResource this reference belongs to."""
        return self.collection.schema.database

    @property
    def fully_qualified_name(self: SchemaObjectReferenceProtocol[T]) -> str:
        """Return the fully qualified name of the object this reference points to."""
        return build_resource_fqn_identifier(self)

    def __repr__(self: SchemaObjectReferenceProtocol[T]) -> str:
        type_name = type(self).__name__
        return f"<{type_name}: {build_resource_fqn_identifier(self)!r}>"


def build_resource_fqn_identifier(resource: SchemaObjectReferenceProtocol[T]) -> str:
    prefix = f"{resource.database.name}.{resource.schema.name}"
    if hasattr(resource, "name_with_args"):
        return f"{prefix}.{resource.name_with_args}"
    return f"{prefix}.{resource.name}"

