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
    capabilities=Capabilities(interactive_login=True, file_attachments=True),
    # Cargo declaration (config-with-cargo): the fields only this adapter
    # consumes. Socket fields (enabled / allow_from / workspace) stay with the
    # host. Defaults and secrecy travel with the cargo; the central model
    # mirrors them until it retires.
    # route_tag is str | int in the central model; declared as string, the
    # widest scalar the flat vocabulary offers for a mixed union.
    config_schema={
        "base_url": {"type": "string", "default": "https://ilinkai.weixin.qq.com"},
        "cdn_base_url": {"type": "string", "default": "https://novac2c.cdn.weixin.qq.com/c2c"},
        "route_tag": {"type": "string", "default": None},
        "token": {"type": "string", "default": "", "secret": True},
        "state_dir": {"type": "string", "default": ""},
        "poll_timeout": {"type": "integer", "default": 35},
    },
)
