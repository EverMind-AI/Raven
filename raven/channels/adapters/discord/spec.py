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
    capabilities=Capabilities(file_attachments=True),
    # Cargo declaration (config-with-cargo): the fields only this adapter
    # consumes. Socket fields (enabled / allow_from / workspace) stay with the
    # host. Defaults stay in the central model until the storage handover.
    config_schema={
        "token": {"type": "string", "required": True},
        "gateway_url": {"type": "string"},
        "intents": {"type": "integer"},
        "group_policy": {"type": "string"},
    },
)
