"""Declarative descriptor for the Weixin (personal WeChat) channel. Importing
this module does not import httpx — the SDK import is deferred into the factory.
Declares interactive_login: pairing is via the iLink QR flow."""

from __future__ import annotations

from raven.channels.contract import Capabilities, ChannelSpec


def _make(config):
    from raven.channels.adapters.weixin.channel import WeixinChannel

    return WeixinChannel(config)


SPEC = ChannelSpec(
    display_name="WeChat",
    factory=_make,
    capabilities=Capabilities(interactive_login=True),
)
