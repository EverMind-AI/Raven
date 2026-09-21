"""Resolve Raven-owned shell approval requests at the TUI RPC boundary.

This handler does not classify commands or grant authority by itself. It only
forwards the human's choice -- allow once, for this session, always with the
confirmed pattern, or a refusal -- to the broker that owns the pending
request. The opaque approval ID and conversation binding keep stale or
cross-session UI responses from resolving a different request.

``approval.revoke`` is the one step back: it removes the allow rule a prompt
just wrote, and nothing else -- the call that was allowed has run.

``approval.pending`` is for a page that lost its sheets -- a reload, a fresh
socket: the requests still waiting, as they were first sent, so it can draw
them again and answer them.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from loguru import logger

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


async def approval_revoke(params: dict[str, Any]) -> dict[str, bool]:
    """Remove the exec allow rule a reader saved from a prompt; False when it was not there."""
    from raven.config.update import remove_exec_pattern

    pattern = str(params.get("pattern") or "").strip()
    if not pattern:
        return {"ok": False}
    try:
        return {"ok": remove_exec_pattern(pattern)}
    except Exception:  # noqa: BLE001 - the config file is the user's; a failed write is reported, not raised
        logger.exception("approval: could not remove allow rule {!r}", pattern)
        return {"ok": False}


async def approval_pending(
    params: dict[str, Any],
    *,
    approval_broker: "ApprovalBroker",
) -> dict[str, list[dict[str, Any]]]:
    """The requests still open -- one conversation's, or all of them."""
    conversation_id = str(params.get("session_id") or params.get("conversation_id") or "")
    return {"requests": approval_broker.pending(conversation_id or None)}


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

    dispatcher.register("approval.respond", _respond)
    dispatcher.register("approval.revoke", approval_revoke)
    dispatcher.register("approval.pending", _pending)


__all__ = ["approval_pending", "approval_respond", "approval_revoke", "register_approval_methods"]
