"""Declarative descriptor for the Matrix channel. Importing this module does not
import matrix-nio — the SDK import is deferred into the factory."""

from __future__ import annotations

from raven.channels.contract import Capabilities, ChannelSpec


def _make(config):
    from raven.channels.adapters.matrix.channel import MatrixChannel

    return MatrixChannel(config)


SPEC = ChannelSpec(
    display_name="Matrix",
    factory=_make,
    capabilities=Capabilities(file_attachments=True),
    # Cargo declaration (config-with-cargo): the fields only this adapter
    # consumes. Socket fields (enabled / allow_from / workspace) stay with the
    # host. Defaults and secrecy travel with the cargo; the central model
    # mirrors them until it retires.
    config_schema={
        "homeserver": {"type": "string", "default": "https://matrix.org"},
        "access_token": {"type": "string", "default": "", "required": True, "secret": True},
        "user_id": {"type": "string", "default": "", "required": True},
        "device_id": {"type": "string", "default": ""},
        "e2ee_enabled": {"type": "boolean", "default": True},
        "sync_stop_grace_seconds": {"type": "integer", "default": 2},
        "max_media_bytes": {"type": "integer", "default": 20971520},
        "group_policy": {"type": "string", "default": "open", "choices": ["open", "mention", "allowlist"]},
        "group_allow_from": {"type": "array", "default": []},
        "allow_room_mentions": {"type": "boolean", "default": False},
    },
)
