"""Asking about an action a tool finds while it runs.

The gate decides about a call before it is dispatched. Some actions only appear
inside the tool's body -- a sandboxed command's connection that the sandbox
refused -- and are asked about through the same gate, so the same mode, session
grants and responder answer them (``PermissionGate.authorize_runtime``).

The registry binds the gate that admitted the call for the length of its body,
together with the deadline the body runs under, and a tool reaches the gate
through :func:`current_gate` rather than holding it. A person deciding stops
that deadline: an approval asked before dispatch is never timed by the tool's
ceiling, and one asked from inside the body must not be either.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from raven.permissions.gate import PermissionGate


class RuntimeChoice(StrEnum):
    """What came of asking about an action a tool found while it ran."""

    ONCE = "once"
    SESSION = "session"
    ALWAYS = "always"
    DENY = "deny"
    DENY_STOP = "deny_stop"
    # Asked, and nobody answered: not the user's refusal, and not to be told as one.
    LAPSED = "lapsed"
    UNATTENDED = "unattended"

    @property
    def approved(self) -> bool:
        return self in (RuntimeChoice.ONCE, RuntimeChoice.SESSION, RuntimeChoice.ALWAYS)


@dataclass(frozen=True)
class RuntimeAnswer:
    """The choice, and what the person said with it, verbatim."""

    choice: RuntimeChoice
    feedback: str = ""


_GATE: ContextVar[PermissionGate | None] = ContextVar("runtime_permission_gate", default=None)
_DEADLINE: ContextVar[asyncio.Timeout | None] = ContextVar("runtime_tool_deadline", default=None)


def current_gate() -> PermissionGate | None:
    """The gate that admitted the tool call running in this task; None outside a tool body."""
    return _GATE.get()


@contextmanager
def tool_body(gate: PermissionGate | None, deadline: asyncio.Timeout | None = None) -> Iterator[None]:
    """Bind ``gate`` and the body's ``deadline`` for the length of one tool body."""
    gate_token = _GATE.set(gate)
    deadline_token = _DEADLINE.set(deadline)
    try:
        yield
    finally:
        _DEADLINE.reset(deadline_token)
        _GATE.reset(gate_token)


@contextmanager
def deadline_stopped() -> Iterator[None]:
    """Stop the bound tool deadline while a person decides, then give back what was left of it."""
    deadline = _DEADLINE.get()
    when = deadline.when() if deadline is not None and not deadline.expired() else None
    if deadline is None or when is None:
        yield
        return
    loop = asyncio.get_running_loop()
    left = max(when - loop.time(), 0.0)
    deadline.reschedule(None)
    try:
        yield
    finally:
        deadline.reschedule(loop.time() + left)


__all__ = ["RuntimeAnswer", "RuntimeChoice", "current_gate", "deadline_stopped", "tool_body"]
