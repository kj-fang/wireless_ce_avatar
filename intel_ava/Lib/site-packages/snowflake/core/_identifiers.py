from __future__ import annotations

import re

from typing import TYPE_CHECKING, Any

from snowflake.core.exceptions import InvalidIdentifierError


if TYPE_CHECKING:
    from snowflake.connector import SnowflakeConnection


# See https://docs.snowflake.com/en/sql-reference/identifiers-syntax for identifier syntax
UNQUOTED_IDENTIFIER_REGEX = r"([a-zA-Z_])([a-zA-Z0-9_$]{0,254})"
QUOTED_IDENTIFIER_REGEX = r'"((""|[^"]){0,255})"'
VALID_IDENTIFIER_REGEX = f"(?:{UNQUOTED_IDENTIFIER_REGEX}|{QUOTED_IDENTIFIER_REGEX})"


class FQN:
    """Represents an object identifier, supporting fully qualified names.

    The instance supports builder pattern that allows updating the identifier with database and
    schema from different sources.

    Examples
    ________
    >>> fqn = FQN.from_string("my_schema.object").using_connection(conn)

    >>> fqn = FQN.from_string("my_name").set_database("db").set_schema("foo")
    """

    def __init__(
        self,
        database: str | None,
        schema: str | None,
        name: str,
        signature: str | None = None,
    ) -> None:
        self._database = database
        self._schema = schema
        self._name = name
        self.signature = signature

    @property
    def database(self) -> str | None:
        return self._database

    @property
    def schema(self) -> str | None:
        return self._schema

    @property
    def name(self) -> str:
        return self._name

    @property
    def prefix(self) -> str:
        if self.database:
            return f"{self.database}.{self.schema if self.schema else 'PUBLIC'}"
        if self.schema:
            return f"{self.schema}"
        return ""

    @property
    def identifier(self) -> str:
        if self.prefix:
            return f"{self.prefix}.{self.name}"
        return self.name

    def __str__(self) -> str:
        return self.identifier

    def __eq__(self, other: Any) -> bool:
        if not isinstance(other, FQN):
            return NotImplemented
        return self.identifier == other.identifier

    @classmethod
    def from_string(cls, identifier: str) -> FQN:
        """Take in an object name in the form [[database.]schema.]name and return a new :class:`FQN` instance.

        Raises:
            InvalidIdentifierError: If the object identifier does not meet identifier requirements.
        """
        qualifier_pattern = (
            rf"(?:(?P<first_qualifier>{VALID_IDENTIFIER_REGEX})\.)?"
            rf"(?:(?P<second_qualifier>{VALID_IDENTIFIER_REGEX})\.)?"
            rf"(?P<name>{VALID_IDENTIFIER_REGEX})(?P<signature>\(.*\))?"
        )
        result = re.fullmatch(qualifier_pattern, identifier)

        if result is None:
            raise InvalidIdentifierError(identifier)

        unqualified_name = result.group("name")
        if result.group("second_qualifier") is not None:
            database = result.group("first_qualifier")
            schema = result.group("second_qualifier")
        else:
            database = None
            schema = result.group("first_qualifier")

        signature = None
        if result.group("signature"):
            signature = result.group("signature")
        return cls(
            name=unqualified_name, schema=schema, database=database, signature=signature
        )

    def set_database(self, database: str | None) -> FQN:
        if database:
            self._database = database
        return self

    def set_schema(self, schema: str | None) -> FQN:
        if schema:
            self._schema = schema
        return self

    def set_name(self, name: str) -> FQN:
        self._name = name
        return self

    def using_connection(self, conn: SnowflakeConnection) -> FQN:
        """Update the instance with database and schema from connection."""
        # Update the identifier only it if wasn't already a qualified name
        if conn.database and not self.database:
            self.set_database(conn.database)
        if conn.schema and not self.schema:
            self.set_schema(conn.schema)
        return self

    def to_dict(self) -> dict[str, str | None]:
        """Return the dictionary representation of the instance."""
        return {"name": self.name, "schema": self.schema, "database": self.database}
