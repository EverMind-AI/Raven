"""Declarative descriptor for the Email channel. Importing this module does not
import the channel implementation (IMAP/SMTP wiring) — that is deferred into the
factory."""

from __future__ import annotations

from raven.channels.contract import Capabilities, ChannelSpec


def _make(config):
    from raven.channels.adapters.email.channel import EmailChannel

    return EmailChannel(config)


SPEC = ChannelSpec(
    display_name="Email",
    factory=_make,
    capabilities=Capabilities(),
)
