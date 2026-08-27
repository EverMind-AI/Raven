"""Declarative descriptor for the DingTalk channel. Importing this module does
not import dingtalk_stream — the SDK import is deferred into the factory."""

from __future__ import annotations

from raven.channels.contract import Capabilities, ChannelSpec


def _make(config):
    from raven.channels.adapters.dingtalk.channel import DingTalkChannel

    return DingTalkChannel(config)


SPEC = ChannelSpec(
    display_name="DingTalk",
    factory=_make,
    capabilities=Capabilities(),
)
