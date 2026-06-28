"""Declarative descriptor for the Telegram channel. Importing this module does
not import python-telegram-bot — the SDK import is deferred into the factory."""

from __future__ import annotations

from raven.channels.contract import Capabilities, ChannelSpec


def _make(config):
    from raven.channels.adapters.telegram.channel import TelegramChannel

    return TelegramChannel(config)


SPEC = ChannelSpec(
    display_name="Telegram",
    factory=_make,
    capabilities=Capabilities(),
)
