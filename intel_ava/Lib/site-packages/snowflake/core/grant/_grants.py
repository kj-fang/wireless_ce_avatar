from collections.abc import Iterable
from typing import TYPE_CHECKING, Optional

from pydantic import StrictStr

from snowflake.core._common import DeleteMode
from snowflake.core.grant._generated import GrantApi
from snowflake.core.grant._generated.api_client import StoredProcApiClient
from snowflake.core.grant._generated.models import Grant as GrantModel
from snowflake.core.grant._generated.models.grant import GrantModel as GrantModel_generated
from snowflake.core.grant._grant import Grant
from snowflake.core.grant._grantee import Grantee


if TYPE_CHECKING:
    from snowflake.core import Root


class Grants:
    """The entry point of the Snowflake Core Python APIs to manage Snowflake Grants."""

    def __init__(self, root: "Root") -> None:
        self._root = root
        self._api = GrantApi(root=self._root, resource_class=None, sproc_client=StoredProcApiClient(root=self._root))

    def grant(self, grant: Grant) -> None:
        """
        Grant the specified privilege(s) on the named securable to the named grantee.

        Parameters
        __________
        grant: Grant
            The ``Grant`` object, together with the ``Grant``'s properties:
            grantee, securable, privilege; grant_option is optional.

        Examples
        ________
        Applying a grant to a test role:

        >>> root.grants.grant(
        ...     Grant(
        ...         grantee=Grantees.role(name=role_name),
        ...         securable=Securables.current_account,
        ...         privileges=[Privileges.create_database],
        ...     )
        ... )
        """
        privileges = [privilege.value for privilege in grant.privileges] if grant.privileges else None
        request_body = GrantModel(privileges=privileges, grant_option=grant.grant_option)

        if grant.securable.scope is not None:
            self._api.grant_group_privilege(
                grantee_type=grant.grantee.grantee_type,
                grantee_name=grant.grantee.name,
                bulk_grant_type=grant.securable.name,
                securable_type_plural=grant.securable.securable_type,
                scope_type=grant.securable.scope.securable_type,
                scope_name=grant.securable.scope.name,
                grant=request_body,
            )
        else:
            self._api.grant_privilege(
                grantee_type=grant.grantee.grantee_type,
                grantee_name=grant.grantee.name,
                securable_type=grant.securable.securable_type,
                securable_name=grant.securable.name,
                grant=request_body,
            )

    def revoke(self, grant: Grant, mode: DeleteMode = DeleteMode.restrict) -> None:
        """
        Revoke the specified privilege(s) on the named securable to the named grantee.

        Parameters
        __________
        grant: Grant
            The ``Grant`` object, together with the ``Grant``'s properties:
            grantee, securable, privilege; grant_option is optional.

        Examples
        ________
        Revoking a Privilege from test role:

        >>> root.grants.revoke(
        ...     Grant(
        ...         grantee=Grantees.role(name=role_name),
        ...         securable=Securables.current_account,
        ...         privileges=[Privileges.create_database],
        ...     )
        ... )
        """
        privileges = [privilege.value for privilege in grant.privileges] if grant.privileges else []

        real_mode = mode.value
        for privilege in privileges:
            if grant.securable.scope is not None:
                self._api.revoke_group_privilege(
                    grantee_type=grant.grantee.grantee_type,
                    grantee_name=grant.grantee.name,
                    bulk_grant_type=grant.securable.name,
                    securable_type_plural=grant.securable.securable_type,
                    scope_type=grant.securable.scope.securable_type,
                    scope_name=grant.securable.scope.name,
                    privilege=privilege,
                    delete_mode=StrictStr(real_mode),
                )
            else:
                self._api.revoke_privilege(
                    grantee_type=grant.grantee.grantee_type,
                    grantee_name=grant.grantee.name,
                    securable_type=grant.securable.securable_type,
                    securable_name=grant.securable.name,
                    privilege=privilege,
                    delete_mode=StrictStr(real_mode),
                )

    def revoke_grant_option(self, grant: Grant, mode: DeleteMode = DeleteMode.restrict) -> None:
        """
        Revoke the grant option on the specified privilege(s) on the named securable to the named grantee.

        Parameters
        __________
        grant: Grant
            The ``Grant`` object, together with the ``Grant``'s properties:
            grantee, securable, privilege; grant_option is optional.

        Examples
        ________
        Revoking grant option for a Privilege from test role:

        >>> root.grants.revoke(
        ...     Grant(
        ...         grantee=Grantees.role(name=role_name),
        ...         securable=Securables.current_account,
        ...         privileges=[Privileges.create_database],
        ...     )
        ... )
        """
        privileges = [privilege.value for privilege in grant.privileges] if grant.privileges else []

        real_mode = mode.value
        for privilege in privileges:
            if grant.securable.scope is not None:
                self._api.revoke_group_privilege_grant_option(
                    grantee_type=grant.grantee.grantee_type,
                    grantee_name=grant.grantee.name,
                    bulk_grant_type=grant.securable.name,
                    securable_type_plural=grant.securable.securable_type,
                    scope_type=grant.securable.scope.securable_type,
                    scope_name=grant.securable.scope.name,
                    privilege=privilege,
                    delete_mode=StrictStr(real_mode),
                )
            else:
                self._api.revoke_privilege_grant_option(
                    grantee_type=grant.grantee.grantee_type,
                    grantee_name=grant.grantee.name,
                    securable_type=grant.securable.securable_type,
                    securable_name=grant.securable.name,
                    privilege=privilege,
                    delete_mode=StrictStr(real_mode),
                )

    def to(self, grantee: Grantee, limit: Optional[int] = None) -> Iterable[GrantModel_generated]:
        """
        List the roles and privileges granted to the specified grantee.

        Parameters
        __________
        grantee: Grantee
            The ``Grantee`` to list the roles and privileges of.

        Examples
        ________
        Listing the roles and privileges granted to the grantee:

        >>> root.grants.to(
        ...    Grantee(
        ...         name="test-user",
        ...         grantee_type="user",
        ...     ),
        ...     limit = 10,
        ... )
        """
        grants = self._api.list_grants_to(
            grantee_type=grantee.grantee_type, grantee_name=grantee.name, show_limit=limit
        )
        return map(GrantModel_generated._from_model, iter(grants))
