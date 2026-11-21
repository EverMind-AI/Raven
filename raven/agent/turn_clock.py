"""The wall-clock deadline of the turn running in this task, for tools that must fit inside it.

The loop reads a turn's budgets once, at turn start, and checks the deadline
between iterations -- never mid-call, because cancelling a call in flight throws
away a finished generation. A tool that can run for minutes is the one place
that overrun is actually spent, so it needs the same deadline.

Set once per turn, the way the tool registry records the turn's channel: a turn
with no clock sets ``None``, so a value cannot survive into the next turn of the
same task. Each turn runs in its own task, and a task starts from the value its
creator held -- the scheduler, which never runs a turn body -- so a turn never
inherits another's clock.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

_DEADLINE: ContextVar[float | None] = ContextVar("raven_turn_deadline", default=None)
_SESSION: ContextVar[str] = ContextVar("raven_turn_session", default="")
_EXECUTION: ContextVar[str | None] = ContextVar("raven_turn_execution", default=None)


def set_deadline(deadline: float | None) -> None:
    """Record when this turn must be finished, as a ``time.monotonic()`` value."""
    _DEADLINE.set(deadline)


def set_session(session_key: str) -> None:
    """Record which session this turn serves, for work that outlives the call."""
    _SESSION.set(session_key)


def session_key() -> str:
    """The session this turn serves, or "" outside a turn."""
    return _SESSION.get()


def execution_id() -> str | None:
    """The delegation whose commands this turn owns, when it has one."""
    return _EXECUTION.get()


def set_execution_id(value: str | None) -> None:
    """Bind this turn's delegated execution for background command custody."""
    _EXECUTION.set(value)


@contextmanager
def execution_scope(session: str, value: str | None) -> Iterator[None]:
    """Bind a nested backend's command ownership until its own call returns."""
    session_token = _SESSION.set(session)
    execution_token = _EXECUTION.set(value)
    try:
        yield
    finally:
        _EXECUTION.reset(execution_token)
        _SESSION.reset(session_token)


def remaining() -> float | None:
    """Seconds left in this turn, or ``None`` when it has no clock."""
    deadline = _DEADLINE.get()
    if deadline is None:
        return None
    return max(0.0, deadline - time.monotonic())
