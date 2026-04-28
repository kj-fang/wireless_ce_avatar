"""Manages Snowflake Network Policies."""

from ..network_policy._generated.models import NetworkPolicy
from ._network_policy import NetworkPolicyCollection, NetworkPolicyResource


__all__ = [
    "NetworkPolicy",
    "NetworkPolicyResource",
    "NetworkPolicyCollection",
]
