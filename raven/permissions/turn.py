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


@dataclass(frozen=True)
class Refusal:
    """One call the gate turned down, as a reader is owed it.

    ``action`` is the same summary line an approval prompt would have shown and
    ``reason`` the sentence the model was given, so a refusal report and a
    prompt describe one call the same way.
    """

    tool_name: str
    action: str
    reason: str
    source: str = ""


@dataclass
class PermissionTurn:
    """One turn's approval capability and its refusal memory.

    ``denied_digests`` suppresses re-asking about a call already refused in this
    turn; a later user turn binds a fresh object and gets a fresh boundary.

    ``lapsed_digests`` is the subset nobody actually answered -- a transport that
    went away, or a host that set a ceiling of its own. Both suppress the second
    ask, since a request nobody could answer will be unanswerable again, but they
    are not the same fact, and the sentence the model is given about the second
    ask has to be the true one for the first.

    ``refusals`` is the audit those two sets cannot be: the digests say a call
    was refused, this says which one and why. A surface with no human on it --
    the one-shot ``-m`` path is the case -- has nothing else to read them from,
    and a turn whose mutations were all refused otherwise reports success.
    """

    responder: ApprovalResponder | None = None
    conversation_id: str = ""
    turn_id: str = ""
    # Who this turn speaks for, as the approval prompt names it: the request's
    # origin ("user", "subagent", ...) and, for a sub-agent, the agent's name.
    origin: str = ""
    origin_name: str = ""
    denied_digests: set[str] = field(default_factory=set)
    lapsed_digests: set[str] = field(default_factory=set)
    refusals: list[Refusal] = field(default_factory=list)
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
    origin: str = "",
    origin_name: str = "",
) -> PermissionTurn:
    """Bind or revoke the asking capability for the current turn's task.

    Returns the bound object. The gate appends to it from inside the turn's own
    task, where a context bound here is visible and the caller's context is not
    (the scheduler builds the turn's task, so an entrance cannot read its own
    ``current_turn()`` back afterwards). Holding the returned object is how a
    surface reads what the turn refused.
    """
    turn = PermissionTurn(
        responder=responder,
        conversation_id=conversation_id,
        turn_id=turn_id,
        origin=origin,
        origin_name=origin_name,
        on_review=on_review,
    )
    _TURN.set(turn)
    return turn


def current_turn() -> PermissionTurn:
    """This task's binding, or an unattended one when nothing bound."""
    turn = _TURN.get()
    return turn if turn is not None else PermissionTurn()


def set_current_tool_call_id(tool_call_id: str) -> None:
    """Record which provider call is being dispatched, for the approval frame."""
    _TOOL_CALL_ID.set(tool_call_id or "")


def note_refusal(tool_name: str, action: str, reason: str, source: str = "") -> None:
    """Record a refused call on this task's turn, for whoever reports it.

    Written where the decision is made rather than by the caller, because the
    callers that matter are the ones that cannot see it: a refusal replaces a
    tool's result, so the turn continues and ends the same way it would have
    ended had the call been allowed.

    Nothing bound means nobody to tell: a digests-only turn object is discarded
    on the spot, and the append lands on it and is dropped. That is the
    unattended default and not a leak -- a turn that never bound a capability
    also has no reader waiting for this list.
    """
    current_turn().refusals.append(Refusal(tool_name=tool_name, action=action, reason=reason, source=source))


def current_tool_call_id() -> str:
    return _TOOL_CALL_ID.get()


__all__ = [
    "PermissionTurn",
    "Refusal",
    "current_tool_call_id",
    "current_turn",
    "note_refusal",
    "set_current_tool_call_id",
    "start_permission_turn",
]
