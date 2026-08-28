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
    capabilities=Capabilities(),
)
