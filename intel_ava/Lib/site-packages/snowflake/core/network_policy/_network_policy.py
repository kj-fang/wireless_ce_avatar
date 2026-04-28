from collections.abc import Iterator
from typing import TYPE_CHECKING, Optional

from pydantic import StrictStr

from snowflake.core import PollingOperation
from snowflake.core._common import (
    AccountObjectCollectionParent,
    CreateMode,
    ObjectReferenceMixin,
)
from snowflake.core._operation import PollingOperations

from .._internal.telemetry import api_telemetry
from ._generated.api import NetworkPolicyApi
from ._generated.api_client import StoredProcApiClient
from ._generated.models.network_policy import NetworkPolicy


if TYPE_CHECKING:
    from snowflake.core._root import Root

class NetworkPolicyCollection(AccountObjectCollectionParent["NetworkPolicyResource"]):
    """Represents the collection operations on the Snowflake Network Policy resource.

    With this collection, you can create, iterate through, and fetch network policies
    that you have access to in the current context.

    Examples
    ________
    Creating a network policy instance with only a single ip allowed:

    >>> network_policies = root.network_policies
    >>> new_network_policy = NetworkPolicy(
    ...     name = 'single_ip_policy',
    ...     allowed_ip_list=['192.168.1.32/32'],
    ...     blocked_ip_list=['0.0.0.0'],
    ... )
    >>> network_policies.create(new_network_policy)
    """

    def __init__(self, root: "Root"):
        super().__init__(root, ref_class=NetworkPolicyResource)
        self._api = NetworkPolicyApi(
            root = self.root,
            resource_class = self._ref_class,
            sproc_client = StoredProcApiClient(root = self.root)
        )


    @api_telemetry
    def create(
        self,
        network_policy: NetworkPolicy,
        mode: CreateMode=CreateMode.error_if_exists,
    ) -> "NetworkPolicyResource":
        """Create a network policy in Snowflake.

        Parameters
        __________
        network_policy: NetworkPolicy
            The ``NetworkPolicy`` object, together with ``NetworkPolicy``'s properties:
            name; allowed_network_rule_list, blocked_network_rule_list, allowed_ip_list,
            blocked_ip_list, comment are optional

        mode: CreateMode, optional
            One of the following enum values:

            ``CreateMode.error_if_exists``: Throw an :class:`snowflake.core.exceptions.ConflictError`
            if the network policy already exists in Snowflake.  Equivalent to SQL ``create network policy <name> ...``.

            ``CreateMode.or_replace``: Replace if the network policy already exists in Snowflake. Equivalent to SQL
            ``create or replace network policy <name> ...``.

            ``CreateMode.if_not_exists``: Do nothing if the network policy already exists in Snowflake.
            Equivalent to SQL ``create network policy <name> if not exists...``

            Default is ``CreateMode.error_if_exists``.

        Examples
        ________
        Create a Network Policy instance:

        >>> root.network_policies.create(NetworkPolicy(
        ...     name = 'my_network_policy',
        ...     allowed_network_rule_list = allowed_rules,
        ...     blocked_network_rule_list = blocked_rules,
        ...     allowed_ip_list=['8.8.8.8'],
        ...     blocked_ip_list=['0.0.0.0'],
        ... ))
        """
        real_mode = CreateMode[mode].value

        self._api.create_network_policy(
            network_policy, create_mode=StrictStr(real_mode),
            async_req=False
        )

        return NetworkPolicyResource(network_policy.name, self)


    @api_telemetry
    def create_async(
        self,
        network_policy: NetworkPolicy,
        mode: CreateMode=CreateMode.error_if_exists,
    ) -> PollingOperation["NetworkPolicyResource"]:
        """An asynchronous version of :func:`create`.

        Refer to :class:`~snowflake.core.PollingOperation` for more information on asynchronous execution and
        the return type.
        """ # noqa: D401
        real_mode = CreateMode[mode].value

        future =self._api.create_network_policy(
            network_policy, create_mode=StrictStr(real_mode),
            async_req=True
        )
        return PollingOperation(future, lambda _: NetworkPolicyResource(network_policy.name, self))


    @api_telemetry
    def iter(self) -> Iterator[NetworkPolicy]:
        """Iterate through ``NetworkPolicy`` objects from Snowflake.

        Examples
        ________
        Printing the names of all visible network policies:

        >>> for network_policy in root.network_policies.iter():
        >>>     print(network_policy.name)
        """
        network_policies = self._api.list_network_policies(async_req=False)
        return iter(network_policies)


    @api_telemetry
    def iter_async(self) -> PollingOperation[Iterator[NetworkPolicy]]:
        """An asynchronous version of :func:`iter`.

        Refer to :class:`~snowflake.core.PollingOperation` for more information on asynchronous execution and
        the return type.
        """ # noqa: D401
        future = self._api.list_network_policies(async_req=True)
        return PollingOperations.iterator(future)


class NetworkPolicyResource(ObjectReferenceMixin[NetworkPolicyCollection]):
    """Represents a reference to a Snowflake Network Policy resource.

    With this network policy reference, you can create, update, and fetch information about network policies, as well
    as perform certain actions on them.
    """

    def __init__(
            self,
            name: StrictStr,
            collection: NetworkPolicyCollection
        ) -> None:
        self.name = name
        self.collection = collection

    @api_telemetry
    def fetch(self) -> NetworkPolicy:
        """Fetch the details of a network policy.

        Examples
        ________
        Fetching a network policy reference to print its time of creation:

        >>> print(network_policy_reference.fetch().created_on)
        """
        return self.collection._api.fetch_network_policy(
            self.name, async_req=False
        )

    @api_telemetry
    def fetch_async(self) -> PollingOperation[NetworkPolicy]:
        """An asynchronous version of :func:`fetch`.

        Refer to :class:`~snowflake.core.PollingOperation` for more information on asynchronous execution and
        the return type.
        """ # noqa: D401
        future = self.collection._api.fetch_network_policy(
            self.name, async_req=True
        )
        return PollingOperations.identity(future)

    @api_telemetry
    def drop(self, if_exists: Optional[bool] = None) -> None:
        """Drop this network policy.

        Parameters
        __________
        if_exists: bool, optional
            Check the existence of this network policy before dropping it.
            Default is ``None``, which is equivalent to ``False``.

        Examples
        ________
        Deleting a network policy using its reference:

        >>> network_policy_reference.drop()

        Deleting a network policy using its reference if it exists:

        >>> network_policy_reference.drop(if_exists=True)
        """
        self.collection._api.delete_network_policy(self.name, if_exists=if_exists, async_req=False)

    @api_telemetry
    def drop_async(self, if_exists: Optional[bool] = None) -> PollingOperation[None]:
        """An asynchronous version of :func:`drop`.

        Refer to :class:`~snowflake.core.PollingOperation` for more information on asynchronous execution and
        the return type.
        """ # noqa: D401
        future = self.collection._api.delete_network_policy(self.name, if_exists=if_exists, async_req=True)
        return PollingOperations.empty(future)
