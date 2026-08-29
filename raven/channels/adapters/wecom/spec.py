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
    # Cargo declaration (config-with-cargo): the fields only this adapter
    # consumes. Socket fields (enabled / allow_from / workspace) stay with the
    # host. Defaults and secrecy travel with the cargo; the central model
    # mirrors them until it retires.
    config_schema={
        "bot_id": {"type": "string", "default": "", "required": True},
        "secret": {"type": "string", "default": "", "required": True, "secret": True},
        "welcome_message": {"type": "string", "default": ""},
    },
)
