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
    """A model id, the provider whose credential serves it, and how much of it
    that model can hold.

    The window belongs here for the same reason the credential does: it is a
    fact about this model, and two sessions on models of different sizes are
    now a normal thing to have at once. Held on the loop instead, one int would
    have to answer for both -- which is the mis-sizing the window ladder exists
    to prevent, arriving through a different door.
    """

    provider: "LLMProvider"
    model: str
    #: An explicit ``agents.defaults.contextWindowTokens``. A user who pinned a
    #: number meant it for whatever they run, so it outranks the model's own
    #: size here exactly as it does in ``rates.effective_context_window``.
    configured_window: int | None = None

    def __post_init__(self) -> None:
        if not self.model:
            raise ValueError("a ModelBinding needs a model id")

    @property
    def context_window(self) -> int:
        """This model's real window, resolved once and remembered.

        Resolved on demand rather than at construction: building a provider is
        what imports LiteLLM, and a lazily-built one has not done it yet, so a
        window resolved eagerly would be the catalogue-miss default for every
        model. By the first read there is a turn in flight and the import has
        happened.

        ``allow_fetch=False`` keeps this off the network -- it is read inside a
        running turn, on the event loop. A miss is therefore not cached: the
        next read, once a catalogue is warm, can still find the real number.
        """
        from raven.providers.rates import DEFAULT_CONTEXT_WINDOW_TOKENS, resolve_context_window

        if self.configured_window:
            return self.configured_window
        remembered = self.__dict__.get("_context_window")
        if remembered:
            return int(remembered)
        window = resolve_context_window(self.model, allow_fetch=False)
        if window:
            object.__setattr__(self, "_context_window", window)
            return window
        return DEFAULT_CONTEXT_WINDOW_TOKENS


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


def active_window(fallback: int) -> int:
    """How much the running turn's model can hold, or ``fallback`` outside one.

    The counterpart of :func:`active_binding` for the subsystems that size
    themselves against the window -- the history trimmer, the curator's
    assembler and the consolidator. Each is built once and outlives any number
    of turns, so a window copied at construction answers for the session that
    happened to build it and for no other.
    """
    binding = _ACTIVE.get()
    return binding.context_window if binding is not None else fallback


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
