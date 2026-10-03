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
        "user_token_read_only": {
            "type": "boolean",
            "default": True,
            "sensitive": "turning it off lets Raven act as you on Slack",
        },
        "reply_in_thread": {"type": "boolean", "default": True},
        "react_emoji": {"type": "string", "default": "eyes"},
        "group_policy": {
            "type": "string",
            "default": "mention",
            "sensitive": "widening it lets more people instruct Raven",
        },
        "group_allow_from": {
            "type": "array",
            "default": [],
            "sensitive": "widening it lets more people instruct Raven",
        },
        "dm": {
            "type": "object",
            "fields": {
                "enabled": {
                    "type": "boolean",
                    "default": True,
                    "sensitive": "widening it lets more people instruct Raven",
                },
                "policy": {
                    "type": "string",
                    "default": "open",
                    "sensitive": "widening it lets more people instruct Raven",
                },
                "allow_from": {
                    "type": "array",
                    "default": [],
                    "sensitive": "widening it lets more people instruct Raven",
                },
            },
        },
    },
)
