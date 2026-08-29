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
    capabilities=Capabilities(file_attachments=True),
    # Cargo declaration (config-with-cargo): the fields only this adapter
    # consumes. Socket fields (enabled / allow_from / workspace) stay with the
    # host. Defaults and secrecy travel with the cargo; the central model
    # mirrors them until it retires.
    config_schema={
        "app_id": {"type": "string", "required": True},
        "app_secret": {"type": "string", "required": True, "secret": True},
        "encrypt_key": {"type": "string", "default": "", "secret": True},
        "verification_token": {"type": "string", "default": "", "secret": True},
        "react_emoji": {"type": "string", "default": "THUMBSUP"},
        "group_policy": {"type": "string", "default": "mention"},
    },
)
