"""The channel paper on the shelf: the adapter surface re-exported, plus the capability proof.

The shapes live in :mod:`raven.contracts.channel` (the papers own them) and
are re-exported so adapters keep one import path. :func:`capability_violations`
is the shelf's own check that a declared capability and its ``Supports*``
protocol agree -- a proof the per-channel tests run, not a shape.
"""

from raven.contracts.channel import Channel, ChannelSpec, SupportsLogin  # noqa: F401
from raven.spine.delivery import Capabilities, SupportsStreaming  # noqa: F401

# Each capability flag must agree with its matching opt-in protocol. Adding a
# capability = add one row; the check below covers both directions for it.
_CAP_PROTOCOLS: tuple[tuple[str, type], ...] = (
    ("interactive_login", SupportsLogin),
    ("streaming", SupportsStreaming),
)


def capability_violations(channel: object, caps: Capabilities | None = None) -> list[str]:
    """Return mismatches between declared capabilities and implemented protocols.

    A channel declaring a capability must implement the matching ``Supports*``
    protocol, and vice-versa. Empty list = consistent. Used by the per-channel
    capability-proof tests.
    """
    caps = caps if caps is not None else getattr(channel, "capabilities", Capabilities())
    out: list[str] = []
    for flag, proto in _CAP_PROTOCOLS:
        declared = getattr(caps, flag)
        implemented = isinstance(channel, proto)
        if declared and not implemented:
            out.append(f"declares {flag} but does not implement {proto.__name__}")
        if implemented and not declared:
            out.append(f"implements {proto.__name__} but does not declare {flag}")
    return out


__all__ = ["Capabilities", "SupportsStreaming", "Channel", "ChannelSpec", "SupportsLogin", "capability_violations"]
