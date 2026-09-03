"""Declarative descriptor for the QQ channel. Importing this module does not
import botpy — the SDK import is deferred into the factory."""

from __future__ import annotations

from raven.channels.contract import Capabilities, ChannelSpec


def _make(config):
    from raven.channels.adapters.qq.channel import QQChannel

    return QQChannel(config)


SPEC = ChannelSpec(
    display_name="QQ",
    factory=_make,
    capabilities=Capabilities(),
    # Cargo declaration (config-with-cargo): the fields only this adapter
    # consumes, with their defaults, secrecy and nesting -- the declaration
    # is the only truth. Socket fields (enabled / allow_from / workspace)
    # stay with the host.
    config_schema={
        "app_id": {"type": "string", "default": "", "required": True},
        "secret": {"type": "string", "default": "", "required": True, "secret": True},
    },
)
