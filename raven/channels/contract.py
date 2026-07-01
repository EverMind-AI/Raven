"""The channel contract — what a chat-channel adapter must satisfy and declare.

A channel implements the :class:`Channel` protocol (``start``/``stop``/``send``)
and declares its :class:`Capabilities`; optional behaviours are separate
``Supports*`` protocols a channel opts into. Each channel package exports a
:class:`ChannelSpec` — a lightweight descriptor whose ``factory`` defers the
heavy SDK import — consumed by the registry.

Composition over inheritance: there is no base class to subclass. Adapters
satisfy the protocols structurally and inject the framework services
(:mod:`.intake`, transcription) they need.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

# Capabilities and SupportsStreaming live in spine.delivery (their consumer is
# the delivery hub); re-exported here so channels keep importing from one place.
from raven.spine.delivery import Capabilities, SupportsStreaming


@runtime_checkable
class Channel(Protocol):
    """Minimal required contract every channel satisfies."""

    name: str
    capabilities: Capabilities

    async def start(self) -> None: ...
    async def stop(self) -> None: ...
    async def send(self, chat_id: str, content: str, media: list[str] | None = None) -> None: ...


@runtime_checkable
class SupportsLogin(Protocol):
    """Opt-in interactive (QR/scan) login, run once via CLI before ``start``."""

    async def login(self, force: bool = False) -> bool: ...


@dataclass(frozen=True)
class ChannelSpec:
    """Declarative descriptor a channel package exports as ``SPEC``.

    ``factory`` defers the channel's heavy SDK import, so collecting specs
    (listing / onboarding / login routing) stays cheap. Carries only what can't
    be located elsewhere: the channel's name is its package name (the registry
    key); dependency/setup guidance is derived by the CLI from capabilities +
    the config schema.
    """

    display_name: str
    factory: Callable[[Any], Channel]  # (config) -> Channel
    capabilities: Capabilities = field(default_factory=Capabilities)


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
