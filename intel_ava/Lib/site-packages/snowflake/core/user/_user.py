from collections.abc import Iterator
from typing import TYPE_CHECKING, Optional

from pydantic import StrictStr

from snowflake.core import PollingOperation
from snowflake.core._common import AccountObjectCollectionParent, CreateMode, ObjectReferenceMixin
from snowflake.core._internal.telemetry import api_telemetry
from snowflake.core._internal.utils import deprecated
from snowflake.core._operation import PollingOperations
from snowflake.core.user._generated.api import UserApi
from snowflake.core.user._generated.api_client import StoredProcApiClient
from snowflake.core.user._generated.models.grant import Grant
from snowflake.core.user._generated.models.securable import Securable
from snowflake.core.user._generated.models.user import UserModel as User


if TYPE_CHECKING:
    from snowflake.core import Root


class UserCollection(AccountObjectCollectionParent["UserResource"]):
    """Represents the collection operations on the Snowflake User resource.

    With this collection, you can create, iterate through, and search for users that you have access to in the
    current context.

    Examples
    ________
    Creating a user instance:

    >>> sample_user = User(name="test_user")
    >>> root.users.create(sample_user)
    """

    def __init__(self, root: "Root") -> None:
        super().__init__(root, ref_class=UserResource)
        self._api = UserApi(
            root=self.root, resource_class=self._ref_class, sproc_client=StoredProcApiClient(root=self.root)
        )

    @api_telemetry
    def create(self, user: User, *, mode: CreateMode = CreateMode.error_if_exists) -> "UserResource":
        """
        Create a user in Snowflake account.

        Parameters
        __________
        user: User
            The ``User`` object, together with the ``User``'s properties:
            name; password, login_name, display_name, first_name, middle_name, last_name,
            email, must_change_password, disabled, days_to_expiry, mins_to_unlock, default_warehouse,
            default_namespace, default_role, default_secondary_roles, mins_to_bypass_mfa, rsa_public_key,
            rsa_public_key_fp, rsa_public_key_2, rsa_public_key_2_fp, comment, type,
            enable_unredacted_query_syntax_error, network_policy are optional
        mode: CreateMode, optional
            One of the following enum values.

            ``CreateMode.error_if_exists``: Throw an :class:`snowflake.core.exceptions.ConflictError`
            if the user already exists in Snowflake.  Equivalent to SQL ``create user <name> ...``.

            ``CreateMode.or_replace``: Replace if the user already exists in Snowflake. Equivalent to SQL
            ``create or replace user <name> ...``.

            ``CreateMode.if_not_exists``: Do nothing if the user already exists in Snowflake.
            Equivalent to SQL ``create user <name> if not exists...``

            Default is ``CreateMode.error_if_exists``.

        Examples
        ________
        Creating a user, replacing any existing user with the same name:

        >>> sample_user = User(name = "test_user")
        >>> root.users.create(sample_user, mode = CreateMode.or_replace)
        """
        create_mode = CreateMode[mode].value
        self._api.create_user(user._to_model(), StrictStr(create_mode), async_req=False)
        return self[user.name]

    @api_telemetry
    def create_async(
        self,
        user: User,
        *,
        mode: CreateMode = CreateMode.error_if_exists
    ) -> PollingOperation["UserResource"]:
        """An asynchronous version of :func:`create`.

        Refer to :class:`~snowflake.core.PollingOperation` for more information on asynchronous execution and
        the return type.
        """ # noqa: D401
        create_mode = CreateMode[mode].value
        future = self._api.create_user(user._to_model(), StrictStr(create_mode), async_req=True)
        return PollingOperation(future, lambda _: self[user.name])

    @api_telemetry
    def iter(
        self,
        like: Optional[str] = None,
        limit: Optional[int] = None,
        starts_with: Optional[str] = None,
        from_name: Optional[str] = None,
    ) -> Iterator[User]:
        users = self._api.list_users(
            StrictStr(like) if like else None,
            StrictStr(starts_with) if starts_with else None,
            limit,
            from_name=from_name,
            async_req=False,
        )
        """Iterate through ``User`` objects from Snowflake, filtering on any optional 'like' pattern.

        Parameters
        __________
        like: str, optional
            A case-insensitive string functioning as a filter, with support for SQL
            wildcard characters (% and _).
        starts_with: str, optional
            String used to filter the command output based on the string of characters that appear
            at the beginning of the object name. Uses case-sensitive pattern matching.
        show_limit: int, optional
            Limit of the maximum number of rows returned by iter(). The default is ``None``, which behaves equivalently
            to show_limit=10000. This value must be between ``1`` and ``10000``.
        from_name: str, optional
            Fetch rows only following the first row whose object name matches
            the specified string. This is case-sensitive and does not have to be the full name.

        Examples
        ________
        Showing all users that you have access to see:

        >>> users = user_collection.iter()

        Showing information of the exact user you want to see:

        >>> users = user_collection.iter(like="your-user-name")

        Showing users starting with 'your-user-name-':

        >>> users = user_collection.iter(like="your-user-name-%")
        >>> users = user_collection.iter(starts_with="your-user-name")

        Using a for loop to retrieve information from iterator:

        >>> for user in users:
        ...     print(user.name, user.display_name)
        """
        return map(User._from_model, iter(users))

    @api_telemetry
    def iter_async(
        self,
        like: Optional[str] = None,
        limit: Optional[int] = None,
        starts_with: Optional[str] = None,
        from_name: Optional[str] = None,
    ) -> PollingOperation[Iterator[User]]:
        """An asynchronous version of :func:`iter`.

        Refer to :class:`~snowflake.core.PollingOperation` for more information on asynchronous execution and
        the return type.
        """ # noqa: D401
        future = self._api.list_users(
            StrictStr(like) if like else None,
            StrictStr(starts_with) if starts_with else None,
            limit,
            from_name=from_name,
            async_req=True,
        )
        return PollingOperation(future, lambda rest_models: map(User._from_model, iter(rest_models)))


class UserResource(ObjectReferenceMixin[UserCollection]):
    """Represents a reference to a Snowflake user.

    With this user reference, you can create or alter, delete and fetch information about users, as well
    as perform certain actions on them.
    """

    def __init__(self, name: str, collection: UserCollection) -> None:
        self.name = name
        self.collection = collection

    @api_telemetry
    def create_or_alter(self, user: User) -> None:
        """Create a user in Snowflake or alter one if it already exists.

        Parameters
        __________
        user: User
            The ``User`` object, together with the ``User``'s properties:
            name; password, login_name, display_name, first_name, middle_name, last_name,
            email, must_change_password, disabled, days_to_expiry, mins_to_unlock, default_warehouse,
            default_namespace, default_role, default_secondary_roles, mins_to_bypass_mfa, rsa_public_key,
            rsa_public_key_fp, rsa_public_key_2, rsa_public_key_2_fp, comment, type,
            enable_unredacted_query_syntax_error, network_policy are optional

        Examples
        ________
        Creating a user or altering one if it already exists:

        >>> user_parameters = User(
        ...     name="User1",
        ...     first_name="Snowy",
        ...     last_name="User",
        ...     must_change_password=False
        ...)
        >>> user_reference.create_or_alter(user_parameters)
        """
        self.collection._api.create_or_alter_user(user.name, user._to_model(), async_req=False)

    @api_telemetry
    def create_or_alter_async(self, user: User) -> PollingOperation[None]:
        """An asynchronous version of :func:`create_or_alter`.

        Refer to :class:`~snowflake.core.PollingOperation` for more information on asynchronous execution and
        the return type.
        """ # noqa: D401
        future = self.collection._api.create_or_alter_user(user.name, user._to_model(), async_req=True)
        return PollingOperations.empty(future)

    @api_telemetry
    @deprecated("drop")
    def delete(self) -> None:
        self.drop()

    @api_telemetry
    def drop(self, if_exists: Optional[bool] = None) -> None:
        """Drop this user.

        Parameters
        __________
        if_exists: bool, optional
            Check the existence of this user before dropping it.
            Default is ``None``, which is equivalent to ``False``.

        Examples
        ________
        Deleting a user using its reference:

        >>> user_ref.drop()

        Deleting a user using its reference if it exists:

        >>> user_ref.drop(if_exists=True)
        """
        self.collection._api.delete_user(self.name, if_exists=if_exists, async_req=False)

    @api_telemetry
    def drop_async(self, if_exists: Optional[bool] = None) -> PollingOperation[None]:
        """An asynchronous version of :func:`drop`.

        Refer to :class:`~snowflake.core.PollingOperation` for more information on asynchronous execution and
        the return type.
        """ # noqa: D401
        future = self.collection._api.delete_user(self.name, if_exists=if_exists, async_req=True)
        return PollingOperations.empty(future)

    @api_telemetry
    def fetch(self) -> User:
        """
        Fetch the details of a user.

        Examples
        ________
        Fetching a reference to a user to print its informations:

        >>> user_ref = root.users["test_user"].fetch()
        >>> print(user_ref.name, user_ref.first_name)
        """
        return User._from_model(self.collection._api.fetch_user(name=self.name, async_req=False))

    @api_telemetry
    def fetch_async(self) -> PollingOperation[User]:
        """An asynchronous version of :func:`fetch`.

        Refer to :class:`~snowflake.core.PollingOperation` for more information on asynchronous execution and
        the return type.
        """ # noqa: D401
        future = self.collection._api.fetch_user(name=self.name, async_req=True)
        return PollingOperation(future, lambda rest_model: User._from_model(rest_model))

    @api_telemetry
    def grant_role(self, role_type: str, role: Securable) -> None:
        """
        Grant a role to this user.

        Parameters
        __________
        role_type: str
            The type of role which would be granted.
        role: Securable
            The role which would be granted.

        Examples
        ________
        Using a user reference to grant a role to it:

        >>> user_reference.grant_role("role", Securable(name="test_role"))
        """
        grant = Grant(
            securable_type=role_type,
            securable=role,
        )
        self.collection._api.grant(self.name, grant, async_req=False)

    @api_telemetry
    def grant_role_async(self, role_type: str, role: Securable) -> PollingOperation[None]:
        """An asynchronous version of :func:`grant_role`.

        Refer to :class:`~snowflake.core.PollingOperation` for more information on asynchronous execution and
        the return type.
        """ # noqa: D401
        grant = Grant(
            securable_type=role_type,
            securable=role,
        )
        future = self.collection._api.grant(self.name, grant, async_req=True)
        return PollingOperations.empty(future)

    @api_telemetry
    def revoke_role(self, role_type: str, role: Securable) -> None:
        """
        Revoke a role from this user.

        Parameters
        __________
        role_type: str
            The type of role which would be revoked.
        role: Securable
            The role which would be revoked.

        Examples
        ________
        Using a user reference to revoke a role from it:

        >>> user_reference.revoke_role("role", Securable(name="test_role"))
        """
        grant = Grant(
            securable_type=role_type,
            securable=role,
        )
        self.collection._api.revoke_grants(self.name, grant, async_req=False)

    @api_telemetry
    def revoke_role_async(self, role_type: str, role: Securable) -> PollingOperation[None]:
        """An asynchronous version of :func:`revoke_role`.

        Refer to :class:`~snowflake.core.PollingOperation` for more information on asynchronous execution and
        the return type.
        """ # noqa: D401
        grant = Grant(
            securable_type=role_type,
            securable=role,
        )
        future = self.collection._api.revoke_grants(self.name, grant, async_req=True)
        return PollingOperations.empty(future)

    @api_telemetry
    def iter_grants_to(self) -> Iterator[Grant]:
        """
        List grants to this user.

        Lists all roles granted to the user.

        Examples
        ________
        Using a user reference to list grants to it:

        >>> user_reference.iter_grants_to()
        """
        grants = self.collection._api.list_grants(self.name, async_req=False)
        return iter(grants)

    @api_telemetry
    def iter_grants_to_async(self) -> PollingOperation[Iterator[Grant]]:
        """An asynchronous version of :func:`iter_grants_to`.

        Refer to :class:`~snowflake.core.PollingOperation` for more information on asynchronous execution and
        the return type.
        """ # noqa: D401
        future = self.collection._api.list_grants(self.name, async_req=True)
        return PollingOperations.iterator(future)
