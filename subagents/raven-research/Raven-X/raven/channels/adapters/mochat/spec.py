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
)
