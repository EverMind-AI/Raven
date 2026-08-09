"""The model a piece of work is running on, scoped to that work.

A model id and the credential that serves it are one pair, so they travel
together as a :class:`ModelBinding` rather than as two attributes someone
can update by halves.

The binding is held in a :class:`~contextvars.ContextVar` rather than
passed down, for two reasons. The turn path reads it at sixteen places
across three packages -- ``raven.agent`` (the loop and the subagent
manager), ``raven.context_engine`` (the curator and the history trimmer)
and ``raven.memory_engine`` (the skill gate, the rewriter and the
consolidator) -- and threading a parameter through all of them would touch
far more code than it explains.
More importantly, ``asyncio.create_task`` copies the current context, so
work detached during a turn (a subagent, a consolidation task) keeps the
binding it was started under for its whole life, which is exactly the
semantics those tasks need: a subagent spawned before a model switch must
not finish on the model chosen after it.

Nothing here builds providers. :mod:`raven.providers.pool` does that, and
:class:`~raven.agent.loop.main.AgentLoop` decides which binding a turn
runs under.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import TYPE_CHECKING, Iterator

if TYPE_CHECKING:
    from raven.providers.base import LLMProvider


@dataclass(frozen=True)
class ModelBinding:
    """A model id and the provider whose credential serves it."""

    provider: "LLMProvider"
    model: str

    def __post_init__(self) -> None:
        if not self.model:
            raise ValueError("a ModelBinding needs a model id")


_ACTIVE: ContextVar["ModelBinding | None"] = ContextVar("raven_active_model_binding", default=None)


def active_binding() -> "ModelBinding | None":
    """The binding the current turn runs under, or None outside a turn.

    Callers outside a turn (startup, a CLI one-shot, a test) get None and
    should fall back to whatever default they were built with.
    """
    return _ACTIVE.get()


@contextmanager
def use_binding(binding: "ModelBinding") -> Iterator["ModelBinding"]:
    """Run a block -- and everything it awaits or spawns -- on one binding."""
    token = _ACTIVE.set(binding)
    try:
        yield binding
    finally:
        _ACTIVE.reset(token)


def resolve(pin: "ModelBinding | None", fallback: "ModelBinding") -> "ModelBinding":
    """Which binding should a subsystem use for this call?

    Precedence is the configured rule: a subsystem with a model of its own,
    paired with credentials of its own, uses that; otherwise it follows the
    model of the turn it is running under; outside a turn it uses the fallback
    it was built with.

    ``pin`` is already a pair -- a bare pinned model id never reaches here,
    because a model id without a credential is what mis-pairs one vendor's key
    with another's endpoint.
    """
    if pin is not None:
        return pin
    return _ACTIVE.get() or fallback
