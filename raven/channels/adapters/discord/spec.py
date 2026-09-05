"""Declarative descriptor for the Discord channel. Importing this module does
not import httpx/websockets — the heavy imports are deferred into the factory."""

from __future__ import annotations

from raven.channels.contract import Capabilities, ChannelSpec


def _make(config):
    from raven.channels.adapters.discord.channel import DiscordChannel

    return DiscordChannel(config)


SPEC = ChannelSpec(
    display_name="Discord",
    factory=_make,
    capabilities=Capabilities(),
)
