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
    # host. Defaults stay in the central model until the storage handover.
    config_schema={
        "homeserver": {"type": "string"},
        "access_token": {"type": "string", "required": True},
        "user_id": {"type": "string", "required": True},
        "device_id": {"type": "string"},
        "e2ee_enabled": {"type": "boolean"},
        "sync_stop_grace_seconds": {"type": "integer"},
        "max_media_bytes": {"type": "integer"},
        "group_policy": {"type": "string"},
        "allow_room_mentions": {"type": "boolean"},
        "group_allow_from": {"type": "array"},
    },
)
