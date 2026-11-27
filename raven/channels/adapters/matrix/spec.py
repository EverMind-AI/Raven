"""Declarative descriptor for the Matrix channel. Importing this module does not
import matrix-nio — the SDK import is deferred into the factory."""

from __future__ import annotations

from raven.channels.contract import Capabilities, ChannelSpec


def _make(config):
    from raven.channels.adapters.matrix.channel import MatrixChannel

    channel = MatrixChannel(config)
    from nio.crypto import ENCRYPTION_ENABLED

    # matrix-nio only raises for this once the client is built in start(), and
    # as an ImportWarning, which the missing-dependency probe cannot see.
    if channel.config.e2ee_enabled and not ENCRYPTION_ENABLED:
        raise ImportError(
            "end-to-end encryption is on but matrix-nio[e2e] is not installed; "
            "install it, or set e2eeEnabled to false to send unencrypted"
        )
    return channel


SPEC = ChannelSpec(
    display_name="Matrix",
    factory=_make,
    capabilities=Capabilities(file_attachments=True),
    # Cargo declaration (config-with-cargo): the fields only this adapter
    # consumes, with their defaults, secrecy and nesting -- the declaration
    # is the only truth. Socket fields (enabled / allow_from / workspace)
    # stay with the host.
    config_schema={
        "homeserver": {
            "type": "string",
            "default": "https://matrix.org",
            "sensitive": "sends this channel's credentials and messages to the address given",
        },
        "access_token": {"type": "string", "default": "", "required": True, "secret": True},
        "user_id": {"type": "string", "default": "", "required": True},
        "device_id": {"type": "string", "default": ""},
        "e2ee_enabled": {
            "type": "boolean",
            "default": True,
            "sensitive": "turning it off sends this channel's credentials or messages unencrypted",
        },
        "sync_stop_grace_seconds": {"type": "integer", "default": 2},
        "max_media_bytes": {"type": "integer", "default": 20971520},
        "group_policy": {
            "type": "string",
            "default": "open",
            "choices": ["open", "mention", "allowlist"],
            "sensitive": "widening it lets more people instruct Raven",
        },
        "group_allow_from": {
            "type": "array",
            "default": [],
            "sensitive": "widening it lets more people instruct Raven",
        },
        "allow_room_mentions": {
            "type": "boolean",
            "default": False,
            "sensitive": "widening it lets more people instruct Raven",
        },
    },
)
