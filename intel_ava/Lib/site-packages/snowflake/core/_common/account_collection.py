from typing import TYPE_CHECKING, Generic, TypeVar

from snowflake.connector import SnowflakeConnection
from snowflake.core._common.base_collection import ObjectCollection


if TYPE_CHECKING:
    from snowflake.core import Root

T = TypeVar("T")


class AccountObjectCollectionParent(ObjectCollection[T], Generic[T]):
    def __init__(self, root: "Root", ref_class: type[T]) -> None:
        super().__init__(ref_class)
        self._root = root

    @property
    def _connection(self) -> SnowflakeConnection:
        return self._root._connection

    @property
    def root(self) -> "Root":
        """The Root object this collection belongs to."""
        return self._root

    def __repr__(self) -> str:
        type_name = type(self).__name__
        return f"<{type_name}>"
