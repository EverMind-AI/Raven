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
from raven.spine.delivery import Capabilities


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
    # The cargo-consumed slice of this channel's config, declared where the
    # consumer lives (config-with-cargo): key -> {type, default?, required?,
    # secret?, choices?, fields?}, the same vocabulary plugin manifests use.
    # This declaration is
    # the only truth: the door dispenses from it and the writer validates
    # through the same door.
    config_schema: dict[str, dict[str, Any]] = field(default_factory=dict)


__tier__ = "contract"
__all__ = ["Channel", "ChannelSpec", "SupportsLogin"]
