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
)
