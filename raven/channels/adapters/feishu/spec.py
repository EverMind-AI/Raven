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
    # host. Defaults stay in the central model until the storage handover.
    config_schema={
        "app_id": {"type": "string", "required": True},
        "app_secret": {"type": "string", "required": True},
        "encrypt_key": {"type": "string"},
        "verification_token": {"type": "string"},
        "react_emoji": {"type": "string"},
        "group_policy": {"type": "string"},
    },
)
