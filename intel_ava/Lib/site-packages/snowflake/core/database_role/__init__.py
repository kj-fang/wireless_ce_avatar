"""Manages Snowflake database roles.

Example:
    >>> database_role_name = "test_database_role"
    >>> database_role = DatabaseRole(name=database_role_name, comment="test_comment")
    >>> created_database_role = database_roles.create(database_role)
    >>> database_roles[database_role_name].drop()
"""


from ._database_role import (
    ContainingScope,
    DatabaseRole,
    DatabaseRoleCollection,
    DatabaseRoleResource,
    Grant,
    Securable,
)


__all__ = [
    "DatabaseRole",
    "DatabaseRoleCollection",
    "DatabaseRoleResource",
    "ContainingScope",
    "Securable",
    "Grant",
]
