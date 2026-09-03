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
    capabilities=Capabilities(file_attachments=True),
    # Cargo declaration (config-with-cargo): the fields only this adapter
    # consumes, with their defaults, secrecy and nesting -- the declaration
    # is the only truth. Socket fields (enabled / allow_from / workspace)
    # stay with the host.
    config_schema={
        "token": {"type": "string", "default": "", "required": True, "secret": True},
        "proxy": {"type": "string", "default": None},
        "reply_to_message": {"type": "boolean", "default": False},
        "group_policy": {"type": "string", "default": "mention", "choices": ["open", "mention"]},
    },
)
