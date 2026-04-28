from collections.abc import Iterator
from typing import TYPE_CHECKING, Any, Optional

from pydantic import StrictStr

from snowflake.core import PollingOperation
from snowflake.core._common import CreateMode, DatabaseObjectCollectionParent, DatabaseObjectReferenceMixin, DeleteMode
from snowflake.core._internal.telemetry import api_telemetry
from snowflake.core._operation import PollingOperations
from snowflake.core.database_role._generated.api import DatabaseRoleApi
from snowflake.core.database_role._generated.api_client import StoredProcApiClient
from snowflake.core.database_role._generated.models import DatabaseRole, DatabaseRoleClone
from snowflake.core.database_role._generated.models.containing_scope import ContainingScope
from snowflake.core.database_role._generated.models.grant import Grant
from snowflake.core.database_role._generated.models.securable import Securable


if TYPE_CHECKING:
    from snowflake.core import Root

    from ..database import DatabaseResource


class DatabaseRoleCollection(DatabaseObjectCollectionParent["DatabaseRoleResource"]):
    """Represents the collection operations on the Snowflake DatabaseRole resource.

    With this collection, you can create, iterate through, and search for database roles that you have access to in the
    current context.

    Examples
    ________
    Creating a database role instance:

    >>> database_role_collection = root.databases["my_db"].database_roles
    >>> database_role_collection.create(DatabaseRole(
    ...     name="test-role",
    ...     comment="samplecomment"
    ... ))
    """

    def __init__(self, database: "DatabaseResource", root: "Root") -> None:
        super().__init__(database, DatabaseRoleResource)
        self._api = DatabaseRoleApi(
            root=root, resource_class=self._ref_class, sproc_client=StoredProcApiClient(root=root)
        )

    @api_telemetry
    def create(
        self, database_role: DatabaseRole, *, mode: CreateMode = CreateMode.error_if_exists
    ) -> "DatabaseRoleResource":
        """Create a database role in Snowflake.

        Parameters
        __________
        database_role: DatabaseRole
            The ``DatabaseRole`` object, together with the ``DatabaseRole``'s properties:
            name; comment is optional
        mode: CreateMode, optional
            One of the following enum values.

            ``CreateMode.error_if_exists``: Throw an :class:`snowflake.core.exceptions.ConflictError`
            if the database role already exists in Snowflake.  Equivalent to SQL ``create database role <name> ...``.

            ``CreateMode.or_replace``: Replace if the database role already exists in Snowflake. Equivalent to SQL
            ``create or replace database role <name> ...``.

            ``CreateMode.if_not_exists``: Do nothing if the database role already exists in Snowflake.
            Equivalent to SQL ``create database role <name> if not exists...``

            Default is ``CreateMode.error_if_exists``.

        Examples
        ________
        Creating a database role, replacing any existing database role with the same name:

        >>> database_role = DatabaseRole(
        ...     name="test-role",
        ...     comment="samplecomment"
        ... )
        >>> database_role_ref = root.databases["my_db"].database_roles.create(database_role, mode=CreateMode.or_replace)
        """
        real_mode = CreateMode[mode].value
        self._api.create_database_role(self.database.name, database_role, StrictStr(real_mode), async_req=False)
        return self[database_role.name]

    @api_telemetry
    def create_async(
        self, database_role: DatabaseRole, *, mode: CreateMode = CreateMode.error_if_exists
    ) -> PollingOperation["DatabaseRoleResource"]:
        """An asynchronous version of :func:`create`.

        Refer to :class:`~snowflake.core.PollingOperation` for more information on asynchronous execution and
        the return type.
        """ # noqa: D401
        real_mode = CreateMode[mode].value
        future = self._api.create_database_role(
            self.database.name, database_role, StrictStr(real_mode), async_req=True)
        return PollingOperation(future, lambda _: self[database_role.name])

    def iter(
        self,
        *,
        limit: Optional[int] = None,
        from_name: Optional[str] = None,
    ) -> Iterator[DatabaseRole]:
        """Iterate through ``DatabaseRole`` objects from Snowflake, filtering on any optional 'from_name' pattern.

        Parameters
        __________
        show_limit: int, optional
            Limit of the maximum number of rows returned by iter(). The default is ``None``, which behaves equivalently
            to show_limit=10000. This value must be between ``1`` and ``10000``.
        from_name: str, optional
            Fetch rows only following the first row whose object name matches
            the specified string. This is case-sensitive and does not have to be the full name.

        Examples
        ________
        Showing all database roles that you have access to see:

        >>> database_roles = database_role_collection.iter()

        Showing roles from 'your-role-name-':

        >>> database_roles = database_role_collection.iter(from_name="your-role-name-")

        Using a for loop to retrieve information from iterator:

        >>> for database_role in database_roles:
        ...    print(database_role.name, database_role.comment)
        """
        database_roles = self._api.list_database_roles(
            self.database.name,
            limit,
            from_name=from_name,
            async_req=False,
        )
        return iter(database_roles)

    def iter_async(
        self,
        *,
        limit: Optional[int] = None,
        from_name: Optional[str] = None,
    ) -> PollingOperation[Iterator[DatabaseRole]]:
        """An asynchronous version of :func:`iter`.

        Refer to :class:`~snowflake.core.PollingOperation` for more information on asynchronous execution and
        the return type.
        """ # noqa: D401
        future = self._api.list_database_roles(
            self.database.name,
            limit,
            from_name=from_name,
            async_req=True,
        )
        return PollingOperations.iterator(future)


class DatabaseRoleResource(DatabaseObjectReferenceMixin[DatabaseRoleCollection]):
    """Represents a reference to a Snowflake database role.

    With this database role reference, you can delete roles.
    """

    def __init__(self, name: str, collection: DatabaseRoleCollection) -> None:
        self.name = name
        self.collection = collection

    @api_telemetry
    def drop(self, if_exists: Optional[bool] = None) -> None:
        """Drop this database role.

        Parameters
        __________
        if_exists : bool
            Check the existence of this database role before dropping it.
            Default is ``None``, which is equivalent to ``False``.

        Examples
        ________
        Deleting a database role using its reference:

        >>> database_role_reference.drop()

        Deleting a database role using its reference if it exists:

        >>> database_role_reference.drop(if_exists=True)
        """
        self.collection._api.delete_database_role(self.database.name, self.name, if_exists=if_exists, async_req=False)

    @api_telemetry
    def drop_async(self, if_exists: Optional[bool] = None) -> PollingOperation[None]:
        """An asynchronous version of :func:`drop`.

        Refer to :class:`~snowflake.core.PollingOperation` for more information on asynchronous execution and
        the return type.
        """ # noqa: D401
        future = self.collection._api.delete_database_role(
            self.database.name, self.name, if_exists=if_exists, async_req=True)
        return PollingOperations.empty(future)

    @api_telemetry
    def clone(
        self,
        target_database_role: str,
        target_database: Optional[str] = None,
        mode: CreateMode = CreateMode.error_if_exists,
    ) -> "DatabaseRoleResource":
        """Drop this database role.

        Parameters
        __________
        target_database_role: str
            The name of the target database role to clone the database role to.
        target_database: str, optional
            The name of the target database to clone the database role to. If not provided,
            the current database is used.
        mode: CreateMode, optional
            One of the following enum values.

            ``CreateMode.error_if_exists``: Throw an :class:`snowflake.core.exceptions.ConflictError`
            if the database role already exists in Snowflake.  Equivalent to SQL ``create database role <name> ...``.

            ``CreateMode.or_replace``: Replace if the database role already exists in Snowflake. Equivalent to SQL
            ``create or replace database role <name> ...``.

            ``CreateMode.if_not_exists``: Do nothing if the database role already exists in Snowflake.
            Equivalent to SQL ``create database role <name> if not exists...``

            Default is ``CreateMode.error_if_exists``.

        Examples
        ________
        Creating a database role clone using its reference:

        >>> new_database_role_reference = database_role_reference.clone("new-role-name")
        """
        real_mode = CreateMode[mode].value
        target_database_role_clone = DatabaseRoleClone(
            name=target_database_role,
        )
        self.collection._api.clone_database_role(
            self.database.name, self.name, target_database_role_clone, real_mode, target_database, async_req=False
        )
        if target_database is None:
            target_database = self.database.name
        return self.root.databases[target_database].database_roles[target_database_role]

    @api_telemetry
    def clone_async(
        self,
        target_database_role: str,
        target_database: Optional[str] = None,
        mode: CreateMode = CreateMode.error_if_exists,
    ) -> PollingOperation["DatabaseRoleResource"]:
        """An asynchronous version of :func:`clone`.

        Refer to :class:`~snowflake.core.PollingOperation` for more information on asynchronous execution and
        the return type.
        """ # noqa: D401
        real_mode = CreateMode[mode].value
        target_database_role_clone = DatabaseRoleClone(
            name=target_database_role,
        )
        future = self.collection._api.clone_database_role(
            self.database.name, self.name, target_database_role_clone, real_mode, target_database, async_req=True
        )

        def finalize(_: Any) -> DatabaseRoleResource:
            nonlocal target_database
            if target_database is None:
                target_database = self.database.name
            return self.root.databases[target_database].database_roles[target_database_role]

        return PollingOperation(future, finalize)

    @api_telemetry
    def grant_role(self, role_type: str, role: Securable) -> None:
        """
        Grant a role to this database role.

        Parameters
        __________
        role_type: str
            The type of role which would be granted.
        role: Securable
            The role which would be granted.

        Examples
        ________
        Using a database role reference to grant a role to it:

        >>> database_role_reference.grant("database role", Securable(name="test_role"))
        """
        grant = Grant(
            securable_type=role_type,
            securable=role,
        )
        self.collection._api.grant_privileges(self.database.name, self.name, grant, async_req=False)

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
        future = self.collection._api.grant_privileges(self.database.name, self.name, grant, async_req=True)
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
        Grant privileges on a securable to this database role.

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
        Using a database role reference to grant privileges to it:

        >>> database_role_reference.grant_privileges(["CREATE", "USAGE"], "database", Securable(database="test_db"))
        """
        grant = Grant(
            privileges=privileges,
            securable_type=securable_type,
            securable=securable,
            grant_option=grant_option,
        )
        self.collection._api.grant_privileges(self.database.name, self.name, grant, async_req=False)

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
        future = self.collection._api.grant_privileges(self.database.name, self.name, grant, async_req=True)
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
        Grant privileges on all securables matching the criteria to this database role.

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
        Using a database role reference to grant privileges on all schemas in a database to it:

        >>> database_role_reference.grant_privileges_on_all(["CREATE", "USAGE"], "schema", ContainingScope(database="test_db"))
        """  # noqa: E501
        grant = Grant(
            privileges=privileges,
            securable_type=securable_type,
            containing_scope=containing_scope,
            grant_option=grant_option,
        )
        self.collection._api.grant_privileges(self.database.name, self.name, grant, async_req=False)

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
        future = self.collection._api.grant_privileges(self.database.name, self.name, grant, async_req=True)
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
        Grant privileges on all future securables matching the criteria to this database role.

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
        Using a database role reference to grant privileges on all future schemas in a database to it:

        >>> database_role_reference.grant_future_privileges(["CREATE", "USAGE"], "schema", ContainingScope(database="test_db"))
        """  # noqa: E501
        grant = Grant(
            privileges=privileges,
            securable_type=securable_type,
            containing_scope=containing_scope,
            grant_option=grant_option,
        )
        self.collection._api.grant_future_privileges(self.database.name, self.name, grant, async_req=False)

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
        future = self.collection._api.grant_future_privileges(self.database.name, self.name, grant, async_req=True)
        return PollingOperations.empty(future)

    @api_telemetry
    def revoke_role(
        self,
        role_type: str,
        role: Securable,
    ) -> None:
        """
        Revoke a role from this database role.

        Parameters
        __________
        role_type: str
            The type of role which would be revoked.
        role: Securable
            The role which would be revoked.

        Examples
        ________
        Using a database role reference to revoke a role from it:

        >>> database_role_reference.revoke("database role", Securable(name="test_role"))
        """
        grant = Grant(
            securable_type=role_type,
            securable=role,
        )
        self.collection._api.revoke_grants(self.database.name, self.name, grant, async_req=False)

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
        future = self.collection._api.revoke_grants(self.database.name, self.name, grant, async_req=True)
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
        Revoke privileges on a securable from this database role.

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
        Using a database role reference to revoke privileges from it:

        >>> database_role_reference.revoke_privileges(["CREATE", "USAGE"], "database", Securable(database="test_db"))
        """
        grant = Grant(
            privileges=privileges,
            securable_type=securable_type,
            securable=securable,
        )
        self.collection._api.revoke_grants(self.database.name, self.name, grant, mode, async_req=False)

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
        future = self.collection._api.revoke_grants(self.database.name, self.name, grant, mode, async_req=True)
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
        Revoke privileges on all securables matching the criteria from this database role.

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
        Using a database role reference to revoke privileges on all schemas in a database from it:

        >>> database_role_reference.revoke_privileges_on_all(["CREATE", "USAGE"], "schema", ContainingScope(database="test_db"))
        """  # noqa: E501
        grant = Grant(
            privileges=privileges,
            securable_type=securable_type,
            containing_scope=containing_scope,
        )
        self.collection._api.revoke_grants(self.database.name, self.name, grant, mode, async_req=False)

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
        future = self.collection._api.revoke_grants(self.database.name, self.name, grant, mode, async_req=True)
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
        Revoke privileges on all future securables matching the criteria from this database role.

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
        Using a database role reference to revoke privileges on all future schemas in a database from it:

        >>> database_role_reference.revoke_future_privileges(["CREATE", "USAGE"], "schema", ContainingScope(database="test_db"))
        """  # noqa: E501
        grant = Grant(
            privileges=privileges,
            securable_type=securable_type,
            containing_scope=containing_scope,
        )
        self.collection._api.revoke_future_grants(self.database.name, self.name, grant, mode, async_req=False)

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
        future = self.collection._api.revoke_future_grants(self.database.name, self.name, grant, mode, async_req=True)
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
        Revoke grant option for privileges on a securable from this database role.

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
        Using a database role reference to revoke grant option for privileges from it:

        >>> database_role_reference.revoke_grant_option_for_privileges(["CREATE", "USAGE"], "database", Securable(database="test_db"))
        """  # noqa: E501
        grant = Grant(
            privileges=privileges,
            securable_type=securable_type,
            securable=securable,
            grant_option=True,
        )
        self.collection._api.revoke_grants(self.database.name, self.name, grant, mode, async_req=False)

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
        future = self.collection._api.revoke_grants(self.database.name, self.name, grant, mode, async_req=True)
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
        Revoke grant option for privileges on all securables matching the criteria from this database role.

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
        Using a database role reference to revoke grant option for privileges on all schemas in a database from it:

        >>> database_role_reference.revoke_grant_option_for_privileges_on_all(["CREATE", "USAGE"], "schema", ContainingScope(database="test_db"))
        """  # noqa: E501
        grant = Grant(
            privileges=privileges,
            securable_type=securable_type,
            containing_scope=containing_scope,
            grant_option=True,
        )
        self.collection._api.revoke_grants(self.database.name, self.name, grant, mode, async_req=False)

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
        future = self.collection._api.revoke_grants(self.database.name, self.name, grant, mode, async_req=True)
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
        Revoke grant option for privileges on all future securables matching the criteria from this database role.

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
        Using a database role reference to revoke grant option for privileges on all future schemas in a database from it:

        >>> database_role_reference.revoke_grant_option_for_future_privileges(["CREATE", "USAGE"], "schema", ContainingScope(database="test_db"))
        """  # noqa: E501
        grant = Grant(
            privileges=privileges,
            securable_type=securable_type,
            containing_scope=containing_scope,
            grant_option=True,
        )
        self.collection._api.revoke_future_grants(self.database.name, self.name, grant, mode, async_req=False)

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
        future = self.collection._api.revoke_future_grants(self.database.name, self.name, grant, mode, async_req=True)
        return PollingOperations.empty(future)

    @api_telemetry
    def iter_grants_to(self, show_limit: Optional[int] = None) -> Iterator[Grant]:
        """List grants to this database role.

        Lists all privileges and roles granted to the database role.

        Parameters
        __________
        show_limit: int, optional
            Limit of the maximum number of rows returned by iter(). The default is ``None``, which behaves equivalently
            to show_limit=10000. This value must be between ``1`` and ``10000``.

        Examples
        ________
        Using a database role reference to list grants to it:

        >>> database_role_reference.iter_grants_to()
        """
        grants_list = self.collection._api.list_grants(self.database.name, self.name, show_limit, async_req=False)
        return iter(grants_list)

    @api_telemetry
    def iter_grants_to_async(self, show_limit: Optional[int] = None) -> PollingOperation[Iterator[Grant]]:
        """An asynchronous version of :func:`iter_grants_to`.

        Refer to :class:`~snowflake.core.PollingOperation` for more information on asynchronous execution and
        the return type.
        """ # noqa: D401
        future = self.collection._api.list_grants(self.database.name, self.name, show_limit, async_req=True)
        return PollingOperations.iterator(future)

    @api_telemetry
    def iter_future_grants_to(self, show_limit: Optional[int] = None) -> Iterator[Grant]:
        """List future grants to this database role.

        Lists all privileges on new (i.e. future) objects of a specified type in a database
        or schema granted to the database role.

        Parameters
        __________
        show_limit: int, optional
            Limit of the maximum number of rows returned by iter(). The default is ``None``, which behaves equivalently
            to show_limit=10000. This value must be between ``1`` and ``10000``.

        Examples
        ________
        Using a database role reference to list future grants to it:

        >>> database_role_reference.iter_future_grants_to()
        """
        grants_list = self.collection._api.list_future_grants(
            self.database.name, self.name, show_limit, async_req=False
        )
        return iter(grants_list)

    @api_telemetry
    def iter_future_grants_to_async(self, show_limit: Optional[int] = None) -> PollingOperation[Iterator[Grant]]:
        """An asynchronous version of :func:`iter_future_grants_to`.

        Refer to :class:`~snowflake.core.PollingOperation` for more information on asynchronous execution and
        the return type.
        """ # noqa: D401
        future = self.collection._api.list_future_grants(
            self.database.name, self.name, show_limit, async_req=True
        )
        return PollingOperations.iterator(future)
