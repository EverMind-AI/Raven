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
    # consumes. Socket fields (enabled / allow_from / workspace) stay with the
    # host. Defaults stay in the central model until the storage handover.
    config_schema={
        "base_url": {"type": "string"},
        "socket_url": {"type": "string"},
        "socket_path": {"type": "string"},
        "socket_disable_msgpack": {"type": "boolean"},
        "socket_reconnect_delay_ms": {"type": "integer"},
        "socket_max_reconnect_delay_ms": {"type": "integer"},
        "socket_connect_timeout_ms": {"type": "integer"},
        "refresh_interval_ms": {"type": "integer"},
        "watch_timeout_ms": {"type": "integer"},
        "watch_limit": {"type": "integer"},
        "retry_delay_ms": {"type": "integer"},
        "max_retry_attempts": {"type": "integer"},
        "claw_token": {"type": "string", "required": True},
        "agent_user_id": {"type": "string"},
        "reply_delay_mode": {"type": "string"},
        "reply_delay_ms": {"type": "integer"},
        "sessions": {"type": "array"},
        "panels": {"type": "array"},
        "mention": {"type": "object"},
        "groups": {"type": "object"},
    },
)
