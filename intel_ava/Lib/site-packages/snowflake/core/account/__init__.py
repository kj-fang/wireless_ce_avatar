"""Manages Snowflake Accounts.

Example:
    >>> account_collection = root.accounts
    >>> account = Account(
    ...     name="MY_ACCOUNT",
    ...     admin_name = "admin"
    ...     admin_password = 'TestPassword1'
    ...     first_name = "Jane"
    ...     last_name = "Smith"
    ...     email = 'myemail@myorg.org'
    ...     edition = "enterprise"
    ...     region = "aws_us_west_2"
    ...  )
    >>> account_collection.create(account)

Refer to :class:`snowflake.core.Root` to create the ``root``.
"""


from ._account import Account, AccountCollection, AccountResource


__all__ = [
    "Account",
    "AccountCollection",
    "AccountResource",
]
