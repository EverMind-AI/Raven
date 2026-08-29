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
    # host. Defaults and secrecy travel with the cargo; the central model
    # mirrors them until it retires.
    config_schema={
        "token": {"type": "string", "default": "", "required": True, "secret": True},
        "gateway_url": {"type": "string", "default": "wss://gateway.discord.gg/?v=10&encoding=json"},
        "intents": {"type": "integer", "default": 37377},
        "group_policy": {"type": "string", "default": "mention", "choices": ["mention", "open"]},
    },
)
