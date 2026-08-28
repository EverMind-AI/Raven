"""Declarative descriptor for the WeCom channel. Importing this module does not
import wecom_aibot_sdk — the SDK import is deferred into the factory."""

from __future__ import annotations

from raven.channels.contract import Capabilities, ChannelSpec


def _make(config):
    from raven.channels.adapters.wecom.channel import WecomChannel

    return WecomChannel(config)


SPEC = ChannelSpec(
    display_name="WeCom",
    factory=_make,
    capabilities=Capabilities(),
)
