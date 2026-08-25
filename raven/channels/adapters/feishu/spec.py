"""Declarative descriptor for the Feishu channel. Importing this module does not
import lark_oapi — the SDK import is deferred into the factory."""

from __future__ import annotations

from raven.channels.contract import Capabilities, ChannelSpec


def _make(config):
    from raven.channels.adapters.feishu.channel import FeishuChannel

    return FeishuChannel(config)


SPEC = ChannelSpec(
    display_name="Feishu",
    factory=_make,
    capabilities=Capabilities(),
)
