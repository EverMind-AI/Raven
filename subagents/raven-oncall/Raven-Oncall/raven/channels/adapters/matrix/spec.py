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
    capabilities=Capabilities(),
)
