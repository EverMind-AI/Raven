"""ChannelOutletAdapter: a channel's outbound send surface as a spine Outlet, so
a turn's deliverables reach the channel through its uniform ``send`` interface.
Outbound only — the inbound (intake -> submit) side stays on the channel.

spine never imports channels; channels import the spine vocabulary here.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from raven.spine.delivery import Capabilities
from raven.spine.events import Deliverable, MediaOut, Text, ToolEvent, ToolPhase

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
        self.capabilities = Capabilities(
            streaming=False,
            file_attachments=channel.capabilities.file_attachments,
        )

    async def deliver(self, out: Deliverable) -> None:
        if isinstance(out, Text):
            await self._channel.send(out.source.chat_id, out.content)
        elif isinstance(out, MediaOut):
            await self._channel.send(out.source.chat_id, "", media=[m.path for m in out.media])
        elif isinstance(out, ToolEvent) and out.phase is ToolPhase.COMPLETE:
            delivery = (out.metadata or {}).get("raven_delivery")
            if not isinstance(delivery, dict):
                return
            files = [item for item in delivery.get("files") or [] if isinstance(item, dict)]
            if not files:
                return
            message = str(delivery.get("message") or "").strip()
            if self.capabilities.file_attachments:
                paths = [str(item.get("path") or "") for item in files if item.get("path")]
                await self._channel.send(out.source.chat_id, message, media=paths)
                return
            names = ", ".join(str(item.get("name") or item.get("path") or "file") for item in files)
            note = f"Files ready: {names}. This channel cannot attach files; open the same session in Raven UI or TUI."
            await self._channel.send(out.source.chat_id, f"{message}\n{note}".strip())
        # StreamDelta / Reasoning / ToolEvent / Notice: eaten — render-can't path.
