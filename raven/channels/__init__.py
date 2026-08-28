"""Chat channels module.

Public contract surface — import channel types from here, not from internal
modules, so the file layout can change without breaking callers.
"""

from raven.channels.base import ChannelBase
from raven.channels.contract import (
    Capabilities,
    Channel,
    ChannelSpec,
    SupportsLogin,
    SupportsStreaming,
)

# Public surface = the contract types adapters implement. Validation helpers
# (capability_violations) live in channels.contract. ChannelManager moved to
# raven.gateway.manager -- re-exporting it here would make this package import
# the gateway package, which imports the contract back (a cycle).
__all__ = [
    "Capabilities",
    "Channel",
    "ChannelBase",
    "ChannelSpec",
    "SupportsLogin",
    "SupportsStreaming",
]
