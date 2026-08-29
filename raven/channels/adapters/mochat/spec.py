"""Declarative descriptor for the Mochat channel. Importing this module does not
import the channel implementation (API/socket client) — deferred into the
factory."""

from __future__ import annotations

from raven.channels.contract import Capabilities, ChannelSpec


def _make(config):
    from raven.channels.adapters.mochat.channel import MochatChannel

    return MochatChannel(config)


SPEC = ChannelSpec(
    display_name="Mochat",
    factory=_make,
    capabilities=Capabilities(),
    # Cargo declaration (config-with-cargo): the fields only this adapter
    # consumes, with their defaults, secrecy and nesting -- the declaration
    # is the only truth. Socket fields (enabled / allow_from / workspace)
    # stay with the host.
    config_schema={
        "base_url": {"type": "string", "default": "https://mochat.io"},
        "socket_url": {"type": "string", "default": ""},
        "socket_path": {"type": "string", "default": "/socket.io"},
        "socket_disable_msgpack": {"type": "boolean", "default": False},
        "socket_reconnect_delay_ms": {"type": "integer", "default": 1000},
        "socket_max_reconnect_delay_ms": {"type": "integer", "default": 10000},
        "socket_connect_timeout_ms": {"type": "integer", "default": 10000},
        "refresh_interval_ms": {"type": "integer", "default": 30000},
        "watch_timeout_ms": {"type": "integer", "default": 25000},
        "watch_limit": {"type": "integer", "default": 100},
        "retry_delay_ms": {"type": "integer", "default": 500},
        "max_retry_attempts": {"type": "integer", "default": 0},
        "claw_token": {"type": "string", "default": "", "required": True, "secret": True},
        "agent_user_id": {"type": "string", "default": ""},
        "sessions": {"type": "array", "default": []},
        "panels": {"type": "array", "default": []},
        "mention": {
            "type": "object",
            "fields": {
                "require_in_groups": {"type": "boolean", "default": False},
            },
        },
        "groups": {"type": "object", "default": {}},
        "reply_delay_mode": {"type": "string", "default": "non-mention"},
        "reply_delay_ms": {"type": "integer", "default": 120000},
    },
)
