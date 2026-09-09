"""Route a runtime-submitted turn to the spine that owns its conversation.

The gateway hosting the page runs two spines over one engine: its own, whose
hub delivers to the IM channels, and the page's (``build_rpc_stack``), whose
lanes carry the browser's turns. A turn the runtime submits on a conversation's
behalf -- a sub-agent's result relay, a deep-research delivery -- used to go to
the gateway spine whatever the conversation, so a relay into a page session ran
on a lane of its own while the page's next turn ran on the page spine's lane for
the same session: two turns in one conversation at once, the session written
from both sides, and ``turn.send``'s one-turn-per-session guard seeing neither
(2026-09-08, the same tool calls drawn twice and the result delivered twice).

Routing by conversation key, the way ``RoutingQuestionBroker`` routes questions:
a page session is ``tui:<chat_id>``, everything else is a channel's. The key is
the scheduler's own (``raven.spine.conversation_id``), so the router and the
lane it routes onto cannot read one request two ways. The lane then serialises
the relay behind whatever the page is running for that session.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Callable

from raven.spine import conversation_id

if TYPE_CHECKING:
    from raven.spine import TurnRequest

Submit = Callable[["TurnRequest"], Any]

PAGE_PREFIX = "tui:"


def route_submit(*, page: Submit | None, channel: Submit, page_prefix: str = PAGE_PREFIX) -> Submit:
    """A submit that hands page-session turns to ``page`` and the rest to ``channel``.

    With no page submit (a page mounted over a stack that built no spine) every
    turn goes to the channel spine, which is what the gateway did before.
    """
    if page is None:
        return channel

    def submit(req: "TurnRequest") -> Any:
        target = page if conversation_id(req).startswith(page_prefix) else channel
        return target(req)

    return submit


__all__ = ["PAGE_PREFIX", "Submit", "route_submit"]
