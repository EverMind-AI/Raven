"""Resolve Raven-owned shell approval requests at the TUI RPC boundary.

This handler does not classify commands or grant authority by itself. It only
forwards the human's choice -- allow once, for this session, always with the
confirmed pattern, or a refusal -- to the broker that owns the pending
request. The opaque approval ID and conversation binding keep stale or
cross-session UI responses from resolving a different request.

``approval.revoke`` is the one step back: it removes the allow rule THIS
answer wrote, and nothing else -- the call that was allowed has run, and a rule
the person wrote themselves is not a prompt's to take away. It asks the broker
what the grant did rather than matching on the rule's text, and waits for that
word, because the gate persists after the answer has already reached the client.

``approval.pending`` is for a page that lost its sheets -- a reload, a fresh
socket: the requests still waiting, as they were first sent, so it can draw
them again and answer them. Scoped the way the original notification was
(``connection.conversation_scoped``): a request belongs to the surface the
conversation speaks through, and handing its id to another socket would let
that socket authorize a command it was never asked about.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from loguru import logger

from raven.rpc.connection import owns_conversation

if TYPE_CHECKING:
    from raven.rpc.approval_broker import ApprovalBroker
    from raven.rpc.dispatcher import Dispatcher


async def approval_respond(
    params: dict[str, Any],
    *,
    approval_broker: "ApprovalBroker",
) -> dict[str, bool]:
    """Resolve a pending request and report whether the broker accepted it."""

    approval_id = str(params.get("approval_id", ""))
    # session_id is the canonical TUI wire field. conversation_id remains a
    # compatibility fallback, with the broker enforcing the same binding.
    conversation_id = str(params.get("session_id") or params.get("conversation_id") or "")
    choice = str(params.get("choice", ""))
    feedback = str(params.get("feedback", "") or "")
    pattern = str(params.get("pattern", "") or "")
    if not approval_id or not conversation_id:
        return {"ok": False}
    return {
        "ok": approval_broker.resolve(
            approval_id,
            choice,
            conversation_id=conversation_id,
            feedback=feedback,
            pattern=pattern,
        )
    }


async def approval_revoke(
    params: dict[str, Any],
    *,
    approval_broker: "ApprovalBroker",
) -> dict[str, bool]:
    """Take back the rule one answer wrote; False when that answer wrote none.

    False covers every way there is nothing of this answer's to remove: it was
    not a persisted grant, the rule was already in the config and was merely
    kept, the undo came twice, or the gate never reported (see the broker's
    receipt timeout). None of those is an error the reader caused, and none of
    them may reach for a rule this prompt did not create.

    It also covers an undo for somebody else's conversation. An id is the whole
    of what an undo names, so without this a socket holding one could delete a
    rule written on a conversation it was never shown -- the same crossing
    ``pending`` is filtered for, on the way out instead of the way in.
    """
    from raven.config.update import remove_exec_pattern

    approval_id = str(params.get("approval_id") or "").strip()
    if not approval_id:
        return {"ok": False}
    if not owns_conversation(approval_broker.grant_conversation(approval_id)):
        return {"ok": False}
    pattern = await approval_broker.written_pattern(approval_id)
    if not pattern:
        return {"ok": False}
    try:
        removed = remove_exec_pattern(pattern)
    except Exception:  # noqa: BLE001 - the config file is the user's; a failed write is reported, not raised
        # The receipt is left unspent on purpose: the rule is still on disk and
        # this is the only record of whose it is, so pressing undo again is
        # worth something. A disk that is full or read-only now will not be in a
        # minute.
        logger.exception("approval: could not remove allow rule {!r}", pattern)
        return {"ok": False}
    approval_broker.forget_grant(approval_id)
    return {"ok": removed}


async def approval_pending(
    params: dict[str, Any],
    *,
    approval_broker: "ApprovalBroker",
) -> dict[str, list[dict[str, Any]]]:
    """The requests still open -- one conversation's, or all of them."""
    conversation_id = str(params.get("session_id") or params.get("conversation_id") or "")
    asked = approval_broker.pending(conversation_id or None)
    return {"requests": [r for r in asked if owns_conversation(r.get("conversation_id"))]}


def register_approval_methods(
    dispatcher: "Dispatcher",
    *,
    approval_broker: "ApprovalBroker",
) -> None:
    """Register the broker-backed approval endpoints: answer, undo, and what is still open."""

    async def _respond(params: dict[str, Any]) -> dict[str, bool]:
        return await approval_respond(params, approval_broker=approval_broker)

    async def _pending(params: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
        return await approval_pending(params, approval_broker=approval_broker)

    async def _revoke(params: dict[str, Any]) -> dict[str, bool]:
        return await approval_revoke(params, approval_broker=approval_broker)

    dispatcher.register("approval.respond", _respond)
    dispatcher.register("approval.revoke", _revoke)
    dispatcher.register("approval.pending", _pending)


__all__ = ["approval_pending", "approval_respond", "approval_revoke", "register_approval_methods"]
