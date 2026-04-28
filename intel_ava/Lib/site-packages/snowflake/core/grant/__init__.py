"""Manages Snowflake Privileges.

Example:
    >>> root.grants.grant(Grant(
    >>>        grantee=Grantees.role(name=role_name),
    >>>        securable=Securables.current_account,
    >>>        privileges=[Privileges.create_database]))
"""


from ._grant import Grant
from ._grantee import Grantee, Grantees
from ._grants import Grants
from ._privileges import Privileges
from ._securables import Securable, Securables


__all__ = [
    "Grant",
    "Grants",
    "Grantee",
    "Grantees",
    "Privileges",
    "Securable",
    "Securables",
]
