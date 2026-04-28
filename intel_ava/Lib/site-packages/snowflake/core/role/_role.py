from collections.abc import Iterator
from typing import TYPE_CHECKING, Optional

from pydantic import StrictStr

from snowflake.core import PollingOperation
from snowflake.core._common import AccountObjectCollectionParent, CreateMode, DeleteMode, ObjectReferenceMixin
from snowflake.core._internal.telemetry import api_telemetry
from snowflake.core._internal.utils import deprecated
from snowflake.core._operation import PollingOperations
from snowflake.core.role._generated.api import RoleApi
from snowflake.core.role._generated.api_client import StoredProcApiClient
from snowflake.core.role._generated.models.containing_scope import ContainingScope
from snowflake.core.role._generated.models.grant import Grant
from snowflake.core.role._generated.models.grant_of import GrantOf
from snowflake.core.role._generated.models.grant_on import GrantOn
from snowflake.core.role._generated.models.role import RoleModel as Role
from snowflake.core.role._generated.models.securable import Securable


if TYPE_CHECKING:
    from snowflake.core import Root


class RoleCollection(AccountObjectCollectionParent["RoleResource"]):
    """Represents the collection operations on the Snowflake Role resource.

    With this collection, you can create, iterate through, and search for roles that you have access to in the
    current context.

    Examples
    ________
    Creating a role instance:

    >>> role_collection = root.roles
    >>> role_collection.create(Role(
    ...     name="test-role",
    ...     comment="samplecomment"
    ... ))
    """

    def __init__(self, root: "Root") -> None:
        super().__init__(root, ref_class=RoleResource)
        self._api = RoleApi(
            root=self.root, resource_class=self._ref_class, sproc_client=StoredProcApiClient(root=self.root)
        )

    @api_telemetry
    def create(self, role: Role, *, mode: CreateMode = CreateMode.error_if_exists) -> "RoleResource":
        """Create a role in Snowflake.

        Parameters
        __________
        role: Role
            The ``Role`` object, together with the ``Role``'s properties:
            name ; comment is optional
        mode: CreateMode, optional
            One of the following enum values.

            ``CreateMode.error_if_exists``: Throw an :class:`snowflake.core.exceptions.ConflictError`
            if the role already exists in Snowflake.  Equivalent to SQL ``create role <name> ...``.

            ``CreateMode.or_replace``: Replace if the role already exists in Snowflake. Equivalent to SQL
            ``create or replace role <name> ...``.

            ``CreateMode.if_not_exists``: Do nothing if the role already exists in Snowflake.
            Equivalent to SQL ``create role <name> if not exists...``

            Default is ``CreateMode.error_if_exists``.


        Examples
        ________
        Creating a role, replacing any existing role with the same name:

        >>> role = Role(
        ...     name="test-role",
        ...     comment="samplecomment"
        ... )
        >>> role_ref = root.roles.create(role, mode=CreateMode.or_replace)
        """
        real_mode = CreateMode[mode].value
        self._api.create_role(role._to_model(), StrictStr(real_mode), async_req=False)
        return self[role.name]

    @api_telemetry
    def create_async(
        self,
        role: Role,
        *,
        mode: CreateMode = CreateMode.error_if_exists
    ) -> PollingOperation["RoleResource"]:
        """An asynchronous version of :func:`create`.

        Refer to :class:`~snowflake.core.PollingOperation` for more information on asynchronous execution and
        the return type.
        """ # noqa: D401
        real_mode = CreateMode[mode].value
        future = self._api.create_role(role._to_model(), StrictStr(real_mode), async_req=True)
        return PollingOperation(future, lambda _: self[role.name])

    def iter(
        self,
        *,
        like: Optional[str] = None,
        limit: Optional[int] = None,
        starts_with: Optional[str] = None,
        from_name: Optional[str] = None,
    ) -> Iterator[Role]:
        """Iterate through ``Role`` objects from Snowflake, filtering on any optional 'like' pattern.

        Parameters
        _________
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
        Showing all roles that you have access to see:

        >>> roles = role_collection.iter()

        Showing information of the exact role you want to see:

        >>> roles = role_collection.iter(like="your-role-name")

        Showing roles starting with 'your-role-name-':

        >>> roles = role_collection.iter(like="your-role-name-%")
        >>> roles = role_collection.iter(starts_with="your-role-name")

        Using a for loop to retrieve information from iterator:

        >>> for role in roles:
        ...    print(role.name, role.comment)
        """
        roles = self._api.list_roles(
            StrictStr(like) if like else None,
            StrictStr(starts_with) if starts_with else None,
            limit,
            from_name=from_name,
            async_req=False,
        )

        return map(Role._from_model, iter(roles))

    def iter_async(
        self,
        *,
        like: Optional[str] = None,
        limit: Optional[int] = None,
        starts_with: Optional[str] = None,
        from_name: Optional[str] = None,
    ) -> PollingOperation[Iterator[Role]]:
        """An asynchronous version of :func:`iter`.

        Refer to :class:`~snowflake.core.PollingOperation` for more information on asynchronous execution and
        the return type.
        """ # noqa: D401
        future = self._api.list_roles(
            StrictStr(like) if like else None,
            StrictStr(starts_with) if starts_with else None,
            limit,
            from_name=from_name,
            async_req=True,
        )
        return PollingOperation(future, lambda rest_models: map(Role._from_model, iter(rest_models)))


class RoleResource(ObjectReferenceMixin[RoleCollection]):
    """Represents a reference to a Snowflake role.

    With this role reference, you can delete roles.
    """

    def __init__(self, name: str, collection: RoleCollection) -> None:
        self.name = name
        self.collection = collection

    @api_telemetry
    @deprecated("drop")
    def delete(self) -> None:
        self.drop()

    @api_telemetry
    def drop(self, if_exists: Optional[bool] = None) -> None:
        """Drop this role.

        Parameters
        __________
        if_exists: bool, optional
            Check the existence of this role before dropping it.
            Default is ``None``, which is equivalent to ``False``.

        Examples
        ________
        Deleting a role using its reference:

        >>> role_reference.drop()
        """
        self.collection._api.delete_role(self.name, if_exists, async_req=False)

    @api_telemetry
    def drop_async(self, if_exists: Optional[bool] = None) -> PollingOperation[None]:
        """An asynchronous version of :func:`drop`.

        Refer to :class:`~snowflake.core.PollingOperation` for more information on asynchronous execution and
        the return type.
        """ # noqa: D401
        future = self.collection._api.delete_role(self.name, if_exists, async_req=True)
        return PollingOperations.empty(future)

    @api_telemetry
    def grant_role(self, role_type: str, role: Securable) -> None:
        """
        Grant a role to this role.

        Parameters
        __________
        role_type: str
            The type of role which would be granted.
        role: Securable
            The role which would be granted.

        Examples
        ________
        Using a role reference to grant a role to it:

        >>> reference_role.grant("role", Securable(name="test_role"))
        """
        grant = Grant(
            securable_type=role_type,
            securable=role,
        )
        self.collection._api.grant_privileges(self.name, grant, async_req=False)

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
        future = self.collection._api.grant_privileges(self.name, grant, async_req=True)
        return PollingOperations.empty(future)

    @api_telemetry
    def grant_privileges(
        self,
        privileges: list[str],
        securable_type: str,
        securable: Optional[Securable] = None,
        grant_option: Optional[bool] = None,
    ) -> None:
        """
        Grant privileges on a securable to this role.

        Parameters
        __________
        privileges: list[str]
            The list of privileges to be granted.
        securable_type: str
            The type of securable on which the privileges would be granted.
        securable: Securable, optional
            The securable on which the privileges would be granted. Default is None.
        grant_option: bool, optional
            If True, the grantee can grant the privilege to others. Default is None which means False.

        Examples
        ________
        Using a role reference to grant privileges to it:

        >>> reference_role.grant_privileges(["CREATE", "USAGE"], "database", Securable(database="test_db"))
        """
        grant = Grant(
            privileges=privileges,
            securable_type=securable_type,
            securable=securable,
            grant_option=grant_option,
        )
        self.collection._api.grant_privileges(self.name, grant, async_req=False)

    @api_telemetry
    def grant_privileges_async(
        self,
        privileges: list[str],
        securable_type: str,
        securable: Optional[Securable] = None,
        grant_option: Optional[bool] = None,
    ) -> PollingOperation[None]:
        """An asynchronous version of :func:`grant_privileges`.

        Refer to :class:`~snowflake.core.PollingOperation` for more information on asynchronous execution and
        the return type.
        """ # noqa: D401
        grant = Grant(
            privileges=privileges,
            securable_type=securable_type,
            securable=securable,
            grant_option=grant_option,
        )
        future = self.collection._api.grant_privileges(self.name, grant, async_req=True)
        return PollingOperations.empty(future)

    @api_telemetry
    def grant_privileges_on_all(
        self,
        privileges: list[str],
        securable_type: str,
        containing_scope: ContainingScope,
        grant_option: Optional[bool] = None,
    ) -> None:
        """
        Grant privileges on all securables matching the criteria to this role.

        Parameters
        __________
        privileges: list[str]
            The list of privileges to be granted.
        securable_type: str
            The type of securable on which the privileges would be granted.
        containing_scope: ContainingScope
            The criteria to match the securables.
        grant_option: bool, optional
            If True, the grantee can grant the privilege to others. Default is None which means False.

        Examples
        ________
        Using a role reference to grant privileges on all schemas in a database to it:

        >>> reference_role.grant_privileges_on_all(["CREATE", "USAGE"], "schema", ContainingScope(database="test_db"))
        """
        grant = Grant(
            privileges=privileges,
            securable_type=securable_type,
            containing_scope=containing_scope,
            grant_option=grant_option,
        )
        self.collection._api.grant_privileges(self.name, grant, async_req=False)

    @api_telemetry
    def grant_privileges_on_all_async(
        self,
        privileges: list[str],
        securable_type: str,
        containing_scope: ContainingScope,
        grant_option: Optional[bool] = None,
    ) -> PollingOperation[None]:
        """An asynchronous version of :func:`grant_privileges_on_all`.

        Refer to :class:`~snowflake.core.PollingOperation` for more information on asynchronous execution and
        the return type.
        """ # noqa: D401
        grant = Grant(
            privileges=privileges,
            securable_type=securable_type,
            containing_scope=containing_scope,
            grant_option=grant_option,
        )
        future = self.collection._api.grant_privileges(self.name, grant, async_req=True)
        return PollingOperations.empty(future)

    @api_telemetry
    def grant_future_privileges(
        self,
        privileges: list[str],
        securable_type: str,
        containing_scope: ContainingScope,
        grant_option: Optional[bool] = None,
    ) -> None:
        """
        Grant privileges on all future securables matching the criteria to this role.

        Parameters
        __________
        privileges: list[str]
            The list of privileges to be granted.
        securable_type: str
            The type of securable on which the privileges would be granted.
        containing_scope: ContainingScope
            The criteria to match the securables.
        grant_option: bool, optional
            If True, the grantee can grant the privilege to others. Default is None which means False.

        Examples
        ________
        Using a role reference to grant privileges on all future schemas in a database to it:

        >>> reference_role.grant_future_privileges(["CREATE", "USAGE"], "schema", ContainingScope(database="test_db"))
        """
        grant = Grant(
            privileges=privileges,
            securable_type=securable_type,
            containing_scope=containing_scope,
            grant_option=grant_option,
        )
        self.collection._api.grant_future_privileges(self.name, grant, async_req=False)

    @api_telemetry
    def grant_future_privileges_async(
        self,
        privileges: list[str],
        securable_type: str,
        containing_scope: ContainingScope,
        grant_option: Optional[bool] = None,
    ) -> PollingOperation[None]:
        """An asynchronous version of :func:`grant_future_privileges`.

        Refer to :class:`~snowflake.core.PollingOperation` for more information on asynchronous execution and
        the return type.
        """ # noqa: D401
        grant = Grant(
            privileges=privileges,
            securable_type=securable_type,
            containing_scope=containing_scope,
            grant_option=grant_option,
        )
        future = self.collection._api.grant_future_privileges(self.name, grant, async_req=True)
        return PollingOperations.empty(future)

    @api_telemetry
    def revoke_role(
        self,
        role_type: str,
        role: Securable,
    ) -> None:
        """
        Revoke a role from this role.

        Parameters
        __________
        role_type: str
            The type of role which would be revoked.
        role: Securable
            The role which would be revoked.

        Examples
        ________
        Using a role reference to revoke a role from it:

        >>> reference_role.revoke("role", Securable(name="test_role"))
        """
        grant = Grant(
            securable_type=role_type,
            securable=role,
        )
        self.collection._api.revoke_grants(self.name, grant, async_req=False)

    @api_telemetry
    def revoke_role_async(
        self,
        role_type: str,
        role: Securable,
    ) -> PollingOperation[None]:
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
    def revoke_privileges(
        self,
        privileges: list[str],
        securable_type: str,
        securable: Optional[Securable] = None,
        mode: Optional[DeleteMode] = None,
    ) -> None:
        """
        Revoke privileges on a securable from this role.

        Parameters
        __________
        privileges: list[str]
            The list of privileges to be revoked.
        securable_type: str
            The type of securable on which the privileges would be revoked.
        securable: Securable, optional
            The securable on which the privileges would be revoked. Default is None.
        mode: DeleteMode, optional
            One of the following enum values.

            ``DeleteMode.restrict``: If the privilege being revoked has been re-granted to another role,
            the REVOKE command fails.

            ``DeleteMode.cascade``: If the privilege being revoked has been re-granted, the REVOKE command
            recursively revokes these dependent grants. If the same privilege on an object has been granted
            to the target role by a different grantor (parallel grant), that grant is not affected and the
            target role retains the privilege.

            Default is None which is equivalent to ``DeleteMode.restrict``.

        Examples
        ________
        Using a role reference to revoke privileges from it:

        >>> reference_role.revoke_privileges(["CREATE", "USAGE"], "database", Securable(database="test_db"))
        """
        grant = Grant(
            privileges=privileges,
            securable_type=securable_type,
            securable=securable,
        )
        self.collection._api.revoke_grants(self.name, grant, mode, async_req=False)

    @api_telemetry
    def revoke_privileges_async(
        self,
        privileges: list[str],
        securable_type: str,
        securable: Optional[Securable] = None,
        mode: Optional[DeleteMode] = None,
    ) -> PollingOperation[None]:
        """An asynchronous version of :func:`revoke_privileges`.

        Refer to :class:`~snowflake.core.PollingOperation` for more information on asynchronous execution and
        the return type.
        """ # noqa: D401
        grant = Grant(
            privileges=privileges,
            securable_type=securable_type,
            securable=securable,
        )
        future = self.collection._api.revoke_grants(self.name, grant, mode, async_req=True)
        return PollingOperations.empty(future)

    @api_telemetry
    def revoke_privileges_on_all(
        self,
        privileges: list[str],
        securable_type: str,
        containing_scope: ContainingScope,
        mode: Optional[DeleteMode] = None,
    ) -> None:
        """
        Revoke privileges on all securables matching the criteria from this role.

        Parameters
        __________
        privileges: list[str]
            The list of privileges to be revoked.
        securable_type: str
            The type of securable on which the privileges would be revoked.
        containing_scope: ContainingScope
            The criteria to match the securables.
        mode: DeleteMode, optional
            One of the following enum values.

            ``DeleteMode.restrict``: If the privilege being revoked has been re-granted to another role,
            the REVOKE command fails.

            ``DeleteMode.cascade``: If the privilege being revoked has been re-granted, the REVOKE command
            recursively revokes these dependent grants. If the same privilege on an object has been granted
            to the target role by a different grantor (parallel grant), that grant is not affected and the
            target role retains the privilege.

            Default is None which is equivalent to ``DeleteMode.restrict``.

        Examples
        ________
        Using a role reference to revoke privileges on all schemas in a database from it:

        >>> reference_role.revoke_privileges_on_all(["CREATE", "USAGE"], "schema", ContainingScope(database="test_db"))
        """
        grant = Grant(
            privileges=privileges,
            securable_type=securable_type,
            containing_scope=containing_scope,
        )
        self.collection._api.revoke_grants(self.name, grant, mode, async_req=False)

    @api_telemetry
    def revoke_privileges_on_all_async(
        self,
        privileges: list[str],
        securable_type: str,
        containing_scope: ContainingScope,
        mode: Optional[DeleteMode] = None,
    ) -> PollingOperation[None]:
        """An asynchronous version of :func:`revoke_privileges_on_all`.

        Refer to :class:`~snowflake.core.PollingOperation` for more information on asynchronous execution and
        the return type.
        """ # noqa: D401
        grant = Grant(
            privileges=privileges,
            securable_type=securable_type,
            containing_scope=containing_scope,
        )
        future = self.collection._api.revoke_grants(self.name, grant, mode, async_req=True)
        return PollingOperations.empty(future)

    @api_telemetry
    def revoke_future_privileges(
        self,
        privileges: list[str],
        securable_type: str,
        containing_scope: ContainingScope,
        mode: Optional[DeleteMode] = None,
    ) -> None:
        """
        Revoke privileges on all future securables matching the criteria from this role.

        Parameters
        __________
        privileges: list[str]
            The list of privileges to be revoked.
        securable_type: str
            The type of securable on which the privileges would be revoked.
        containing_scope: ContainingScope
            The criteria to match the securables.
        mode: DeleteMode, optional
            One of the following enum values.

            ``DeleteMode.restrict``: If the privilege being revoked has been re-granted to another role,
            the REVOKE command fails.

            ``DeleteMode.cascade``: If the privilege being revoked has been re-granted, the REVOKE command
            recursively revokes these dependent grants. If the same privilege on an object has been granted
            to the target role by a different grantor (parallel grant), that grant is not affected and the
            target role retains the privilege.

            Default is None which is equivalent to ``DeleteMode.restrict``.

        Examples
        ________
        Using a role reference to revoke privileges on all future schemas in a database from it:

        >>> reference_role.revoke_future_privileges(["CREATE", "USAGE"], "schema", ContainingScope(database="test_db"))
        """
        grant = Grant(
            privileges=privileges,
            securable_type=securable_type,
            containing_scope=containing_scope,
        )
        self.collection._api.revoke_future_grants(self.name, grant, mode, async_req=False)

    @api_telemetry
    def revoke_future_privileges_async(
        self,
        privileges: list[str],
        securable_type: str,
        containing_scope: ContainingScope,
        mode: Optional[DeleteMode] = None,
    ) -> PollingOperation[None]:
        """An asynchronous version of :func:`revoke_future_privileges`.

        Refer to :class:`~snowflake.core.PollingOperation` for more information on asynchronous execution and
        the return type.
        """ # noqa: D401
        grant = Grant(
            privileges=privileges,
            securable_type=securable_type,
            containing_scope=containing_scope,
        )
        future = self.collection._api.revoke_future_grants(self.name, grant, mode, async_req=True)
        return PollingOperations.empty(future)

    @api_telemetry
    def revoke_grant_option_for_privileges(
        self,
        privileges: list[str],
        securable_type: str,
        securable: Optional[Securable] = None,
        mode: Optional[DeleteMode] = None,
    ) -> None:
        """
        Revoke grant option for privileges on a securable from this role.

        Parameters
        __________
        privileges: list[str]
            The list of privileges to be revoked.
        securable_type: str
            The type of securable on which the privileges would be revoked.
        securable: Securable, optional
            The securable on which the privileges would be revoked. Default is None.
        mode: DeleteMode, optional
            One of the following enum values.

            ``DeleteMode.restrict``: If the privilege being revoked has been re-granted to another role,
            the REVOKE command fails.

            ``DeleteMode.cascade``: If the privilege being revoked has been re-granted, the REVOKE command
            recursively revokes these dependent grants. If the same privilege on an object has been granted
            to the target role by a different grantor (parallel grant), that grant is not affected and the
            target role retains the privilege.

            Default is None which is equivalent to ``DeleteMode.restrict``.

        Examples
        ________
        Using a role reference to revoke grant option for privileges from it:

        >>> reference_role.revoke_grant_option_for_privileges(["CREATE", "USAGE"], "database", Securable(database="test_db"))
        """  # noqa: E501
        grant = Grant(
            privileges=privileges,
            securable_type=securable_type,
            securable=securable,
            grant_option=True,
        )
        self.collection._api.revoke_grants(self.name, grant, mode, async_req=False)

    @api_telemetry
    def revoke_grant_option_for_privileges_async(
        self,
        privileges: list[str],
        securable_type: str,
        securable: Optional[Securable] = None,
        mode: Optional[DeleteMode] = None,
    ) -> PollingOperation[None]:
        """An asynchronous version of :func:`revoke_grant_option_for_privileges`.

        Refer to :class:`~snowflake.core.PollingOperation` for more information on asynchronous execution and
        the return type.
        """ # noqa: D401
        grant = Grant(
            privileges=privileges,
            securable_type=securable_type,
            securable=securable,
            grant_option=True,
        )
        future = self.collection._api.revoke_grants(self.name, grant, mode, async_req=True)
        return PollingOperations.empty(future)

    @api_telemetry
    def revoke_grant_option_for_privileges_on_all(
        self,
        privileges: list[str],
        securable_type: str,
        containing_scope: ContainingScope,
        mode: Optional[DeleteMode] = None,
    ) -> None:
        """
        Revoke grant option for privileges on all securables matching the criteria from this role.

        Parameters
        __________
        privileges: list[str]
            The list of privileges to be revoked.
        securable_type: str
            The type of securable on which the privileges would be revoked.
        containing_scope: ContainingScope
            The criteria to match the securables.
        mode: DeleteMode, optional
            One of the following enum values.

            ``DeleteMode.restrict``: If the privilege being revoked has been re-granted to another role,
            the REVOKE command fails.

            ``DeleteMode.cascade``: If the privilege being revoked has been re-granted, the REVOKE command
            recursively revokes these dependent grants. If the same privilege on an object has been granted
            to the target role by a different grantor (parallel grant), that grant is not affected and the
            target role retains the privilege.

            Default is None which is equivalent to ``DeleteMode.restrict``.

        Examples
        ________
        Using a role reference to revoke grant option for privileges on all schemas in a database from it:

        >>> reference_role.revoke_grant_option_for_privileges_on_all(["CREATE", "USAGE"], "schema", ContainingScope(database="test_db"))
        """  # noqa: E501
        grant = Grant(
            privileges=privileges,
            securable_type=securable_type,
            containing_scope=containing_scope,
            grant_option=True,
        )
        self.collection._api.revoke_grants(self.name, grant, mode, async_req=False)

    @api_telemetry
    def revoke_grant_option_for_privileges_on_all_async(
        self,
        privileges: list[str],
        securable_type: str,
        containing_scope: ContainingScope,
        mode: Optional[DeleteMode] = None,
    ) -> PollingOperation[None]:
        """An asynchronous version of :func:`revoke_grant_option_for_privileges_on_all`.

        Refer to :class:`~snowflake.core.PollingOperation` for more information on asynchronous execution and
        the return type.
        """ # noqa: D401
        grant = Grant(
            privileges=privileges,
            securable_type=securable_type,
            containing_scope=containing_scope,
            grant_option=True,
        )
        future = self.collection._api.revoke_grants(self.name, grant, mode, async_req=True)
        return PollingOperations.empty(future)

    @api_telemetry
    def revoke_grant_option_for_future_privileges(
        self,
        privileges: list[str],
        securable_type: str,
        containing_scope: ContainingScope,
        mode: Optional[DeleteMode] = None,
    ) -> None:
        """
        Revoke grant option for privileges on all future securables matching the criteria from this role.

        Parameters
        __________
        privileges: list[str]
            The list of privileges to be revoked.
        securable_type: str
            The type of securable on which the privileges would be revoked.
        containing_scope: ContainingScope
            The criteria to match the securables.
        mode: DeleteMode, optional
            One of the following enum values.

            ``DeleteMode.restrict``: If the privilege being revoked has been re-granted to another role,
            the REVOKE command fails.

            ``DeleteMode.cascade``: If the privilege being revoked has been re-granted, the REVOKE command
            recursively revokes these dependent grants. If the same privilege on an object has been granted
            to the target role by a different grantor (parallel grant), that grant is not affected and the
            target role retains the privilege.

            Default is None which is equivalent to ``DeleteMode.restrict``.

        Examples
        ________
        Using a role reference to revoke grant option for privileges on all future schemas in a database from it:

        >>> reference_role.revoke_grant_option_for_future_privileges(["CREATE", "USAGE"], "schema", ContainingScope(database="test_db"))
        """  # noqa: E501
        grant = Grant(
            privileges=privileges,
            securable_type=securable_type,
            containing_scope=containing_scope,
            grant_option=True,
        )
        self.collection._api.revoke_future_grants(self.name, grant, mode, async_req=False)

    @api_telemetry
    def revoke_grant_option_for_future_privileges_async(
        self,
        privileges: list[str],
        securable_type: str,
        containing_scope: ContainingScope,
        mode: Optional[DeleteMode] = None,
    ) -> PollingOperation[None]:
        """An asynchronous version of :func:`revoke_grant_option_for_future_privileges`.

        Refer to :class:`~snowflake.core.PollingOperation` for more information on asynchronous execution and
        the return type.
        """ # noqa: D401
        grant = Grant(
            privileges=privileges,
            securable_type=securable_type,
            containing_scope=containing_scope,
            grant_option=True,
        )
        future = self.collection._api.revoke_future_grants(self.name, grant, mode, async_req=True)
        return PollingOperations.empty(future)

    @api_telemetry
    def iter_grants_of(self, show_limit: Optional[int] = None) -> Iterator[GrantOf]:
        """List grants of this role.

        Lists all users and roles to which the role has been granted.

        Parameters
        __________
        show_limit: int, optional
            Limit of the maximum number of rows returned by iter(). The default is ``None``, which behaves equivalently
            to show_limit=10000. This value must be between ``1`` and ``10000``.

        Examples
        ________
        Using a role reference to list grants of it:

        >>> reference_role.iter_grants_of()
        """
        grants_list = self.collection._api.list_grants_of(self.name, show_limit, async_req=False)
        return iter(grants_list)

    @api_telemetry
    def iter_grants_of_async(self, show_limit: Optional[int] = None) -> PollingOperation[Iterator[GrantOf]]:
        """An asynchronous version of :func:`iter_grants_of`.

        Refer to :class:`~snowflake.core.PollingOperation` for more information on asynchronous execution and
        the return type.
        """ # noqa: D401
        future = self.collection._api.list_grants_of(self.name, show_limit, async_req=True)
        return PollingOperations.iterator(future)

    @api_telemetry
    def iter_grants_on(self, show_limit: Optional[int] = None) -> Iterator[GrantOn]:
        """List grants on this role.

        Lists all privileges that have been granted on the role.

        Parameters
        __________
        show_limit: int, optional
            Limit of the maximum number of rows returned by iter(). The default is ``None``, which behaves equivalently
            to show_limit=10000. This value must be between ``1`` and ``10000``.

        Examples
        ________
        Using a role reference to list grants on it:

        >>> reference_role.iter_grants_on()
        """
        grants_list = self.collection._api.list_grants_on(self.name, show_limit, async_req=False)
        return iter(grants_list)

    @api_telemetry
    def iter_grants_on_async(self, show_limit: Optional[int] = None) -> PollingOperation[Iterator[GrantOn]]:
        """An asynchronous version of :func:`iter_grants_on`.

        Refer to :class:`~snowflake.core.PollingOperation` for more information on asynchronous execution and
        the return type.
        """ # noqa: D401
        future = self.collection._api.list_grants_on(self.name, show_limit, async_req=True)
        return PollingOperations.iterator(future)

    @api_telemetry
    def iter_grants_to(self, show_limit: Optional[int] = None) -> Iterator[Grant]:
        """List grants to this role.

        Lists all privileges and roles granted to the role.

        Parameters
        __________
        show_limit: int, optional
            Limit of the maximum number of rows returned by iter(). The default is ``None``, which behaves equivalently
            to show_limit=10000. This value must be between ``1`` and ``10000``.

        Examples
        ________
        Using a role reference to list grants to it:

        >>> reference_role.iter_grants_to()
        """
        grants_list = self.collection._api.list_grants(self.name, show_limit, async_req=False)
        return iter(grants_list)

    @api_telemetry
    def iter_grants_to_async(self, show_limit: Optional[int] = None) -> PollingOperation[Iterator[Grant]]:
        """An asynchronous version of :func:`iter_grants_to`.

        Refer to :class:`~snowflake.core.PollingOperation` for more information on asynchronous execution and
        the return type.
        """ # noqa: D401
        future = self.collection._api.list_grants(self.name, show_limit, async_req=True)
        return PollingOperations.iterator(future)

    @api_telemetry
    def iter_future_grants_to(self, show_limit: Optional[int] = None) -> Iterator[Grant]:
        """List future grants to this role.

        Lists all privileges on new (i.e. future) objects of a specified type in a database
        or schema granted to the role.

        Parameters
        __________
        show_limit: int, optional
            Limit of the maximum number of rows returned by iter(). The default is ``None``, which behaves equivalently
            to show_limit=10000. This value must be between ``1`` and ``10000``.

        Examples
        ________
        Using a role reference to list future grants to it:

        >>> reference_role.iter_future_grants_to()
        """
        grants_list = self.collection._api.list_future_grants(self.name, show_limit, async_req=False)
        return iter(grants_list)

    @api_telemetry
    def iter_future_grants_to_async(self, show_limit: Optional[int] = None) -> PollingOperation[Iterator[Grant]]:
        """An asynchronous version of :func:`iter_future_grants_to`.

        Refer to :class:`~snowflake.core.PollingOperation` for more information on asynchronous execution and
        the return type.
        """ # noqa: D401
        future = self.collection._api.list_future_grants(self.name, show_limit, async_req=True)
        return PollingOperations.iterator(future)
