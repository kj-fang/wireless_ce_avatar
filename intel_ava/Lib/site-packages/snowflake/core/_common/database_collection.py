from typing import TYPE_CHECKING, Generic, Protocol, TypeVar

from snowflake.connector import SnowflakeConnection
from snowflake.core._common.base_collection import ObjectCollection, ObjectReferenceMixin


if TYPE_CHECKING:
    from snowflake.core import Root
    from snowflake.core.database import DatabaseResource

T = TypeVar("T")


class DatabaseObjectCollectionParent(ObjectCollection[T], Generic[T]):
    def __init__(self, database: "DatabaseResource", ref_class: type[T]) -> None:
        super().__init__(ref_class)
        self._database = database

    @property
    def _connection(self) -> SnowflakeConnection:
        return self._database._connection

    @property
    def database(self) -> "DatabaseResource":
        """The DatabaseResource this collection belongs to."""
        return self._database

    @property
    def root(self) -> "Root":
        """The Root object this collection belongs to."""
        return self.database.collection.root

    def __repr__(self) -> str:
        type_name = type(self).__name__
        return f"<{type_name}: {self.database.name!r}>"


class DatabaseObjectReferenceProtocol(Protocol[T]):
    @property
    def collection(self) -> DatabaseObjectCollectionParent[T]: ...

    @property
    def name(self) -> str: ...

    @property
    def database(self) -> "DatabaseResource": ...


class DatabaseObjectReferenceMixin(Generic[T], ObjectReferenceMixin[DatabaseObjectCollectionParent[T]]):
    @property
    def database(self: DatabaseObjectReferenceProtocol[T]) -> "DatabaseResource":
        """The DatabaseResource this reference belongs to."""
        return self.collection.database

    @property
    def qualified_name(self: DatabaseObjectReferenceProtocol[T]) -> str:
        """Return the qualified name of the object this reference points to."""
        return f"{self.database.name}.{self.name}"

    def __repr__(self: DatabaseObjectReferenceProtocol[T]) -> str:
        type_name = type(self).__name__
        qualified_name = f"{self.database.name}.{self.name}"
        return f"<{type_name}: {qualified_name!r}>"
