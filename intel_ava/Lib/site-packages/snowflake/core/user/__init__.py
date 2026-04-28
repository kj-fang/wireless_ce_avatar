"""
Manages Snowflake Users.

Example:
    >>> user = User("test_user")
    >>> created_user = root.users.create(user)
    >>> root.users["test_user"].fetch()
    >>> root.users["test_user"].delete()
"""


from ._generated.models import ContainingScope as ContainingScope
from ._user import Grant, Securable, User, UserCollection, UserResource


__all__ = [
    "User",
    "UserCollection",
    "UserResource",
    "Securable",
    "Grant",
    "ContainingScope",
]
