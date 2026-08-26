"""ask_user over ACP: the QuestionBroker bound to ``session/update`` frames.

The integration plan (ACP_INTEGRATION_PLAN.md §3) originally kept ask_user off
the protocol: the DR clarify goes out as the turn's reply and the next
``session/prompt`` carries the answer. That handoff stays the fallback. What
this module adds is the ``askUser.delivery = "tool"`` transport: the broker's
``clarify.request`` is translated into a ``session/update`` notification with
the extension kind ``ask_user_request``, and the client answers by calling
``_raven/clarify_respond`` -- the same notification-out / request-in shape the
TUI RPC layer uses, so no agent-initiated JSON-RPC request (and no
response-correlation plumbing in the read loop) is needed.

Armed only when the client declared ``clientCapabilities._meta.raven.askUser``
at initialize: an unarmed tool has no broker, the DR gate reads
``round_trip_ready`` False and short-circuits the handoff instead -- a question
frame must never be sent to a client with no UI to answer it, because the
600s fail-safe would silently answer every mandated clarify with its default.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from raven.acp import protocol
from raven.tui_rpc.question_broker import QuestionBroker

ASK_USER_UPDATE_KIND = "ask_user_request"
"""The ``sessionUpdate`` discriminator the question rides out on.

An extension kind, not spec surface: the consuming raven's dialect renders it;
a spec-only client ignores unknown update kinds, which is exactly the client
the capability gate keeps this away from.
"""

CLARIFY_RESPOND_METHOD = "_raven/clarify_respond"
"""The inbound extension method that resolves a pending question.

Underscore-prefixed per ACP extensibility convention. Params:
``requestId`` (from the update frame) or ``sessionId``, plus ``answer``.
"""


def build_question_broker(emit: Callable[[dict[str, Any]], None]) -> QuestionBroker:
    """A QuestionBroker whose questions leave as ``session/update`` frames.

    The broker keys pendings by conversation_id, and on this surface the
    conversation id IS the wire sessionId (``AcpSession``: one identity, no id
    map), so the translation is a re-wrap, not a lookup. ``emit`` is the
    connection's synchronous frame writer; the broker awaits its send, so the
    wrap is async in signature only.
    """

    async def send(frame: dict[str, Any]) -> None:
        params = frame.get("params") or {}
        emit(
            protocol.notification(
                "session/update",
                {
                    "sessionId": str(params.get("conversation_id") or ""),
                    "update": {
                        "sessionUpdate": ASK_USER_UPDATE_KIND,
                        "requestId": str(params.get("request_id") or ""),
                        "question": str(params.get("question") or ""),
                        "choices": list(params.get("choices") or []),
                    },
                },
            )
        )

    return QuestionBroker(send_frame=send)


__all__ = [
    "ASK_USER_UPDATE_KIND",
    "CLARIFY_RESPOND_METHOD",
    "build_question_broker",
]
