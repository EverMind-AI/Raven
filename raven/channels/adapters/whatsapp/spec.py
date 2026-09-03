"""Declarative descriptor for the WhatsApp channel. Importing this module does
not import the channel implementation (Node bridge client) — deferred into the
factory. Declares interactive_login: pairing is via the bridge's QR flow."""

from __future__ import annotations

from raven.channels.contract import Capabilities, ChannelSpec


def _make(config):
    from raven.channels.adapters.whatsapp.channel import WhatsAppChannel

    return WhatsAppChannel(config)


SPEC = ChannelSpec(
    display_name="WhatsApp",
    factory=_make,
    capabilities=Capabilities(interactive_login=True),
    # Cargo declaration (config-with-cargo): the fields only this adapter
    # consumes, with their defaults, secrecy and nesting -- the declaration
    # is the only truth. Socket fields (enabled / allow_from / workspace)
    # stay with the host.
    config_schema={
        "bridge_url": {"type": "string", "default": "ws://localhost:3001"},
        "bridge_token": {"type": "string", "default": "", "secret": True},
        "group_policy": {"type": "string", "default": "open", "choices": ["open", "mention"]},
    },
)
