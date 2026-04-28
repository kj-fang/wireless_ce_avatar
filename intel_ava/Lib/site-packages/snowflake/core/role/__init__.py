"""Manages Snowflake user roles.

Example:
    >>> role_name = "test_role"
    >>> test_role = Role(name=role_name, comment="test_comment")
    >>> created_role = roles.create(test_role)
    >>> roles[role_name].delete()
"""


from ._role import ContainingScope, Grant, Role, RoleCollection, RoleResource, Securable


__all__ = [
    "Role",
    "RoleCollection",
    "RoleResource",
    "Securable",
    "ContainingScope",
    "Grant",
]
