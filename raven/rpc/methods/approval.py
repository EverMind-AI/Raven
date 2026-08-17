"""Resolve Raven-owned shell approval requests at the TUI RPC boundary.

This handler does not classify commands or grant authority by itself. It only
forwards an explicit allow-once or deny response to the broker that owns the
pending request. The opaque approval ID and conversation binding keep stale or
cross-session UI responses from resolving a different request.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

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
    if not approval_id or not conversation_id:
        return {"ok": False}
    return {
        "ok": approval_broker.resolve(
            approval_id,
            choice,
            conversation_id=conversation_id,
        )
    }


def register_approval_methods(
    dispatcher: "Dispatcher",
    *,
    approval_broker: "ApprovalBroker",
) -> None:
    """Register the broker-backed approval response endpoint."""

    async def _respond(params: dict[str, Any]) -> dict[str, bool]:
        return await approval_respond(params, approval_broker=approval_broker)

    dispatcher.register("approval.respond", _respond)


__all__ = ["approval_respond", "register_approval_methods"]
