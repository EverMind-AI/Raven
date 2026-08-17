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
)
