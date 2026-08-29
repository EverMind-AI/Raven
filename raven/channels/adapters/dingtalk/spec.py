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
    capabilities=Capabilities(file_attachments=True),
    # Cargo declaration (config-with-cargo): the fields only this adapter
    # consumes. Socket fields (enabled / allow_from / workspace) stay with the
    # host. Defaults and secrecy travel with the cargo; the central model
    # mirrors them until it retires.
    config_schema={
        "client_id": {"type": "string", "default": "", "required": True},
        "client_secret": {"type": "string", "default": "", "required": True, "secret": True},
    },
)
