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
    # consumes. Socket fields (enabled / allow_from / workspace) stay with the
    # host. Defaults stay in the central model until the storage handover.
    config_schema={
        "mode": {"type": "string"},
        "webhook_path": {"type": "string"},
        "bot_token": {"type": "string", "required": True},
        "app_token": {"type": "string", "required": True},
        "user_token_read_only": {"type": "boolean"},
        "reply_in_thread": {"type": "boolean"},
        "react_emoji": {"type": "string"},
        "group_policy": {"type": "string"},
        "group_allow_from": {"type": "array"},
        "dm": {"type": "object"},
    },
)
