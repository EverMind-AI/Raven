"""Turn-scoped state the permission gate reads: who can be asked, and what was
already refused this turn.

Bound by an entrance inside the task that runs the turn (the same discipline as
``ToolRegistry.session_scope``): a request handler's context does not reach the
turn's task, so binding anywhere else binds nothing. A background origin binds
``None`` explicitly rather than leaving the previous turn's capability in the
context.

Sub-agent tasks inherit the parent turn's context by asyncio's own rule, so a
responder bound here can be visible inside a spawned agent's task. That is why
interactivity is not decided by this module alone: a registry built for an
unattended surface carries ``allow_ask=False`` on its gate, which outranks
whatever this context holds.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from contextvars import ContextVar
from dataclasses import dataclass, field

from raven.contracts.asking import ApprovalResponder


@dataclass
class PermissionTurn:
    """One turn's approval capability and its refusal memory.

    ``denied_digests`` suppresses re-asking about a call already refused in this
    turn; a later user turn binds a fresh object and gets a fresh boundary.

    ``lapsed_digests`` is the subset nobody actually answered. Both suppress the
    second ask -- a request that expired unseen will expire again, and forty
    iterations of a 35-second wait is a turn spent waiting rather than working --
    but they are not the same fact, and the sentence the model is given about the
    second ask has to be the true one for the first.
    """

    responder: ApprovalResponder | None = None
    conversation_id: str = ""
    turn_id: str = ""
    denied_digests: set[str] = field(default_factory=set)
    lapsed_digests: set[str] = field(default_factory=set)
    # Purely presentational: lets a watching surface say "the reviewer is
    # looking at this" instead of an unexplained pause. Never load-bearing --
    # the gate swallows its errors and decides identically without it.
    on_review: Callable[[str, str], Awaitable[None]] | None = None


_TURN: ContextVar[PermissionTurn | None] = ContextVar("permission_turn", default=None)

# Per-call, not per-turn: the provider call id of the tool call being
# dispatched right now. Set by the loop beside its dispatch, inherited by a
# forwarding tool's nested execute, and read by the gate so an approval frame
# names the row the client already drew.
_TOOL_CALL_ID: ContextVar[str] = ContextVar("permission_tool_call_id", default="")


def start_permission_turn(
    responder: ApprovalResponder | None,
    *,
    conversation_id: str,
    turn_id: str,
    on_review: Callable[[str, str], Awaitable[None]] | None = None,
) -> None:
    """Bind or revoke the asking capability for the current turn's task."""
    _TURN.set(
        PermissionTurn(
            responder=responder,
            conversation_id=conversation_id,
            turn_id=turn_id,
            on_review=on_review,
        )
    )


def current_turn() -> PermissionTurn:
    """This task's binding, or an unattended one when nothing bound."""
    turn = _TURN.get()
    return turn if turn is not None else PermissionTurn()


def set_current_tool_call_id(tool_call_id: str) -> None:
    """Record which provider call is being dispatched, for the approval frame."""
    _TOOL_CALL_ID.set(tool_call_id or "")


def current_tool_call_id() -> str:
    return _TOOL_CALL_ID.get()


__all__ = [
    "PermissionTurn",
    "current_tool_call_id",
    "current_turn",
    "set_current_tool_call_id",
    "start_permission_turn",
]
