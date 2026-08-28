"""The channel paper: the uniform adapter surface and its spec.

The definitions moved to :mod:`raven.contracts.channel` (the papers own the
shapes); this module re-exports them so existing import paths keep resolving.
"""

from raven.contracts.channel import (  # noqa: F401
    Channel,
    ChannelSpec,
    SupportsLogin,
    capability_violations,
)
from raven.spine.delivery import Capabilities, SupportsStreaming  # noqa: F401

__all__ = ["Capabilities", "SupportsStreaming", "Channel", "ChannelSpec", "SupportsLogin", "capability_violations"]
