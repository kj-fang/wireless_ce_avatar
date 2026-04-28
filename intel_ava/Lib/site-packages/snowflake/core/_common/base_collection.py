from abc import ABC, abstractmethod
from collections.abc import ItemsView, Iterator, KeysView, ValuesView
from typing import TYPE_CHECKING, Generic, Protocol, TypeVar

from snowflake.connector import SnowflakeConnection


T = TypeVar("T")

if TYPE_CHECKING:
    from snowflake.core import Root


class ObjectCollectionBC(ABC):
    @property
    @abstractmethod
    def _connection(self) -> SnowflakeConnection: ...

    @property
    @abstractmethod
    def root(self) -> "Root": ...


class ObjectCollection(ObjectCollectionBC, Generic[T]):
    def __init__(self, ref_class: type[T]) -> None:
        self._items: dict[str, T] = {}
        self._ref_class = ref_class

    def __getitem__(self, item: str) -> T:
        if item not in self._items:
            # Protocol doesn't support restricting __init__
            self._items[item] = self._ref_class(item, self)  # type: ignore[call-arg]
        return self._items[item]

    def __iter__(self) -> Iterator[str]:
        return iter(self.keys())

    def keys(self) -> KeysView[str]:
        return self._items.keys()

    def items(self) -> ItemsView[str, T]:
        return self._items.items()

    def values(self) -> ValuesView[T]:
        return self._items.values()


class ObjectReferenceProtocol(Protocol[T]):
    @property
    def collection(self) -> ObjectCollection[T]: ...

    @property
    def name(self) -> str: ...

    @property
    def root(self) -> "Root": ...


class ObjectReferenceMixin(Generic[T]):
    @property
    def _connection(self: ObjectReferenceProtocol[T]) -> SnowflakeConnection:
        return self.collection._connection

    @property
    def root(self: ObjectReferenceProtocol[T]) -> "Root":
        """The Root object this reference belongs to."""
        return self.collection.root

    def __repr__(self: ObjectReferenceProtocol[T]) -> str:
        type_name = type(self).__name__
        return f"<{type_name}: {self.name!r}>"
