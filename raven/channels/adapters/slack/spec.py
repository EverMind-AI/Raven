"""Declarative descriptor for the Slack channel. Importing this module does not
import slack_sdk — the SDK import is deferred into the factory."""

from __future__ import annotations

from raven.channels.contract import Capabilities, ChannelSpec


def _make(config):
    from raven.channels.adapters.slack.channel import SlackChannel

    return SlackChannel(config)


SPEC = ChannelSpec(
    display_name="Slack",
    factory=_make,
    capabilities=Capabilities(file_attachments=True),
    # Cargo declaration (config-with-cargo): the fields only this adapter
    # consumes, with their defaults, secrecy and nesting -- the declaration
    # is the only truth. Socket fields (enabled / allow_from / workspace)
    # stay with the host.
    config_schema={
        "mode": {"type": "string", "default": "socket"},
        "webhook_path": {"type": "string", "default": "/slack/events"},
        "bot_token": {"type": "string", "default": "", "required": True, "secret": True},
        "app_token": {"type": "string", "default": "", "required": True, "secret": True},
        "user_token_read_only": {"type": "boolean", "default": True},
        "reply_in_thread": {"type": "boolean", "default": True},
        "react_emoji": {"type": "string", "default": "eyes"},
        "group_policy": {"type": "string", "default": "mention"},
        "group_allow_from": {"type": "array", "default": []},
        "dm": {
            "type": "object",
            "fields": {
                "enabled": {"type": "boolean", "default": True},
                "policy": {"type": "string", "default": "open"},
                "allow_from": {"type": "array", "default": []},
            },
        },
    },
)
