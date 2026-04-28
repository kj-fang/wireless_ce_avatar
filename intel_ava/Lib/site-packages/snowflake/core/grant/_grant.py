from typing import Any, Optional

from snowflake.core.database_role._generated.models.grant import Grant as GrantModel
from snowflake.core.grant._grantee import Grantee
from snowflake.core.grant._privileges import Privileges
from snowflake.core.grant._securables import Securable


class Grant:
    """
    Represents Snowflake Grant Operation.

    Parameters
    __________
    grantee: Grantee
        The role that the ``Grant`` is being applied to.
    securable: Securable
        The ``Securable`` object.
    privileges: Privileges
        The ``Privileges`` being granted.
    grant_option: bool, optional
        If ``True``, grantee can pass this privilege down.
        Default is ``None``, which is equivalent to ``False``.

    Examples
    ________
    Granting privileges to a test role:

    >>> from snowflake.core.grant import Grantees, Privileges, Securables
    >>> Grant(grantee=Grantees.role(
    ...     name="test_role",
    ...     securable=Securables.current_account,
    ...     privileges=[Privileges.create_database],
    ... )
    """

    def __init__(
        self,
        grantee: Grantee,
        securable: Securable,
        privileges: Optional[list[Privileges]] = None,
        grant_option: bool = False,
    ):
        self._grantee = grantee
        self._securable = securable
        self._privileges = privileges
        self._grant_option = grant_option

    @property
    def grantee(self) -> Grantee:
        return self._grantee

    @property
    def securable(self) -> Securable:
        return self._securable

    @property
    def privileges(self) -> Optional[list[Privileges]]:
        return self._privileges

    @property
    def grant_option(self) -> bool:
        return self._grant_option

    def to_dict(
        self,
        hide_readonly_properties: bool = False,
    ) -> dict[str, Any]:
        privileges = [privilege.value for privilege in self.privileges] if self.privileges else None
        request_body = GrantModel(
            privileges=privileges,
            grant_option=self.grant_option,
            securable_type=self.securable.securable_type
        )
        return request_body.to_dict(hide_readonly_properties=hide_readonly_properties)

