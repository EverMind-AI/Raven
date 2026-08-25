"""ChannelOutletAdapter: a channel's outbound send surface as a spine Outlet, so
a turn's deliverables reach the channel through its uniform ``send`` interface.
Outbound only — the inbound (intake -> submit) side stays on the channel.

spine never imports channels; channels import the spine vocabulary here.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from raven.spine.delivery import Capabilities
from raven.spine.events import Deliverable, MediaOut, Notice, NoticeKind, Text

if TYPE_CHECKING:
    from raven.channels.contract import Channel


# The sentence for the one notice kind a channel has to render. Written here
# rather than looked up, the same way the terminal writes its own copy: this repo
# has no i18n layer, and the wording belongs to the surface that renders it.
#
# It is the response, not a fallback for a missing detail. ``detail`` is the
# blocking tool's first error line -- it says *which* operation was stopped, and
# nothing else. Sent on its own it reduces the turn to
# "Error: User denied this command or the approval request expired", so the
# person never learns that no alternative will be attempted, and never gets the
# offer to continue with the parts that do not need it. Both are the runtime's
# controlled answer to a blocked turn, and both have to survive the trip.
_BLOCKED_SENTENCE = (
    "A safety rule stopped this operation, so the turn ended here. "
    "Say the word and I will carry on with the parts that do not need it."
)


class ChannelOutletAdapter:
    """Wraps a channel as an Outlet: renders Text / MediaOut by calling
    ``channel.send(...)``, eats the streaming / in-turn events
    (StreamDelta / Reasoning / ToolEvent, and every Notice kind but one) — a
    channel is non-streaming and shows only the final reply (edit-in-place
    streaming is not yet supported).
    A real send failure raises, which the hub retries; eating is not failure.

    ``NoticeKind.ACTION_BLOCKED`` is the exception, because it replaces the
    answer instead of accompanying it: the runtime ends the turn on a safety
    decision and the reply it would have sent is this notice. Eaten like the
    others, the turn arrives as nothing at all -- the user asked for something,
    the runtime refused, and the channel says a refusal and an empty answer with
    the same silence.

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
            await self._channel.send(out.source.chat_id, "", media=[m.path for m in out.media])
        elif isinstance(out, Notice) and out.kind is NoticeKind.ACTION_BLOCKED:
            # Composed the way the terminal composes it: the sentence, then the
            # line naming what was stopped. A tool can abort with nothing
            # readable to say, and an empty second half would put whitespace in
            # front of a person as though it were an explanation.
            detail = (out.detail or "").strip()
            body = f"{_BLOCKED_SENTENCE}\n{detail}" if detail else _BLOCKED_SENTENCE
            await self._channel.send(out.source.chat_id, body)
        # StreamDelta / Reasoning / ToolEvent / other Notice kinds: eaten —
        # render-can't path.
