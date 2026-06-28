"""ChannelOutletAdapter: a channel's outbound send surface as a spine Outlet, so
a turn's deliverables reach the channel through its uniform ``send`` interface.
Outbound only — the inbound (intake -> submit) side stays on the channel.

spine never imports channels; channels import the spine vocabulary here.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from raven.spine.delivery import Capabilities
from raven.spine.events import Deliverable, MediaOut, Text

if TYPE_CHECKING:
    from raven.channels.contract import Channel


class ChannelOutletAdapter:
    """Wraps a channel as an Outlet: renders Text / MediaOut by calling
    ``channel.send(...)``, eats the streaming / in-turn events
    (StreamDelta / Reasoning / ToolEvent / Notice) — a channel is non-streaming
    and shows only the final reply (edit-in-place streaming is not yet supported).
    A real send failure raises, which the hub retries; eating is not failure.

    The deliverable carries its target as ``source`` (the hub routes here by
    source.channel, so it is always set); the reply goes back to that channel /
    chat. reply_to threading belongs to the inbound side and is not handled here."""

    def __init__(self, channel: Channel) -> None:
        self._channel = channel
        self.name = channel.name
        self.capabilities = Capabilities(streaming=False)

    async def deliver(self, out: Deliverable) -> None:
        if isinstance(out, Text):
            await self._channel.send(out.source.chat_id, out.content)
        elif isinstance(out, MediaOut):
            await self._channel.send(
                out.source.chat_id, "", media=[m.path for m in out.media]
            )
        # StreamDelta / Reasoning / ToolEvent / Notice: eaten — render-can't path.
