"""``clarify.respond`` RPC handler — ask-user round-trip answer sink.

The frontend answers a ``clarify.request`` (emitted by :class:`QuestionBroker`
from inside a paused ask_user tool call) with
``clarify.respond {request_id, answer}``; this handler resolves the matching
pending future on the broker. ``clarify.request`` / ``clarify.respond`` is the
ui-tui frontend's existing multi-choice prompt contract (ClarifyPrompt), which
the broker reuses rather than introducing a new frontend card.

Registered via a closure that pre-binds the broker (mirrors
``register_confirm_methods``). Gated on a non-None broker by the umbrella, so
paths that build no broker do not register it.

This method is intentionally NOT in ``METHOD_MODELS`` / ``openrpc.json`` — like
the confirm pair, the question round-trip lives outside the cross-language
schema-parity contract.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from raven.tui_rpc.dispatcher import Dispatcher
    from raven.tui_rpc.question_broker import QuestionBroker


async def question_respond(
    params: dict[str, Any], *, question_broker: "QuestionBroker"
) -> dict:
    """Resolve a pending question. Unknown/expired key → ``{ok: False}``.

    Accepts either ``conversation_id`` or ``request_id`` as the handle.
    """
    key = str(params.get("conversation_id") or params.get("request_id") or "")
    answer = str(params.get("answer", ""))
    ok = question_broker.reply(key, answer)
    return {"ok": ok}


def register_question_methods(
    dispatcher: "Dispatcher", *, question_broker: "QuestionBroker"
) -> None:
    """Register ``clarify.respond`` with the broker pre-bound."""

    async def _respond(params: dict[str, Any]) -> dict:
        return await question_respond(params, question_broker=question_broker)

    dispatcher.register("clarify.respond", _respond)


__all__ = ["question_respond", "register_question_methods"]
