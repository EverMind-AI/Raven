"""AgentConduct: the narrow verbs a sub-agent implements instead of the six hook phases.

Factory-loop tier: Versioned with the factory loop, not frozen for every
loop. The hook phases (``loop_hooks``) are the *timing* contract -- when the
loop asks, in what order, what a refusal does. This paper is the *judgement*
contract: given one step of the turn, what does this agent say about it. The
two are kept apart so that a timing change never rewrites an agent's
judgement and an agent's judgement never reaches into the loop's mechanism.

Nine verbs, all optional, each answered against a ``StepView``: fourteen
read-only fields where the hook context had fourteen writable ones. What a
conduct returns is the whole of its effect -- a text to go in, a narrower tool
array to go out, a note, a verdict, a salvaged reply, the reply to send -- and
it holds no handle to the transcript, the window's state, or the loop.

One instance per turn. The plugin's entry point returns a ``ConductFactory``
and the host calls it at each turn's start, so what this turn has already done
is an attribute on ``self`` and dies with the turn. A plugin no longer keeps
turn state in the hook context's free-form ``metadata`` dict: the host parks
the conduct there and the plugin never reads it.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal

__tier__ = "factory_loop"


@dataclass(frozen=True)
class StepView:
    """What a conduct sees of the turn at the moment it is asked.

    Read-only by construction: ``transcript`` and ``history`` are tuples,
    ``response`` is the provider's own object and is not to be mutated.
    ``rollbacks``, ``mode`` and ``mode_overlay`` are the loop readings the
    surveyed hooks needed and could not derive. ``tools_ran`` tells the two
    ``review`` moments of a tool-calling step apart: False when the model has
    proposed the calls and nothing has run, True once their results are in the
    transcript -- a conduct that reads results waits for the second.
    """

    session_key: str
    iteration: int
    response: Any | None
    transcript: Sequence[dict[str, Any]]
    history: Sequence[dict[str, Any]]
    turn_base: int
    question: str
    rollbacks: int
    mode: str | None
    mode_overlay: Mapping[str, Any] | None
    tools_ran: bool
    tools: Sequence[dict[str, Any]] = ()
    """The tool definitions this call will carry, for a conduct sizing what it adds."""
    window: int | None = None
    """The context window the loop was told to assume, in tokens -- the sizing
    fallback for a caller with no active model binding to consult."""
    max_iterations: int | None = None
    """The loop's iteration cap this turn, for a conduct that paces itself against it."""


@dataclass(frozen=True)
class Intake:
    """What ``intake`` hands back: the text the turn goes on with, or a reply
    that ends it before any model call (a fail-closed sentinel, a command)."""

    text: str
    reply: Any | None = None
    note: str | None = None


@dataclass(frozen=True)
class Verdict:
    """What ``review`` hands back about one step.

    ``accept`` lets the step stand. ``resample`` sends the loop back to the
    model call with ``inject`` appended to the transcript (a reviewer's
    objection, a reconcile nudge) and ``overrides`` on the next call.
    ``end`` closes the turn with ``reply``. The loop executes the verdict and
    bounds how many resamples a turn may spend; the conduct only pronounces.
    """

    kind: Literal["accept", "resample", "end"]
    reason: str | None = None
    inject: Sequence[dict[str, Any]] | None = None
    overrides: dict[str, Any] | None = None
    reply: Any | None = None
    note: str | None = None

    @property
    def accepted(self) -> bool:
        return self.kind == "accept"


def Accept(note: str | None = None) -> Verdict:  # noqa: N802 - reads as the verdict it is
    return Verdict("accept", note=note)


def Resample(  # noqa: N802
    reason: str,
    *,
    inject: Sequence[dict[str, Any]] | None = None,
    overrides: dict[str, Any] | None = None,
    note: str | None = None,
) -> Verdict:
    return Verdict("resample", reason=reason, inject=inject, overrides=overrides, note=note)


def End(reply: Any, note: str | None = None) -> Verdict:  # noqa: N802
    return Verdict("end", reply=reply, note=note)


class AgentConduct:
    """Base for a sub-agent's conduct: every verb answers "nothing to say".

    A subclass overrides only the verbs it has a judgement for. ``review`` is
    asked twice per iteration when the model called tools -- once before they
    run (``step.tools_ran`` False) and once after (True) -- and once when it
    did not; a conduct that only judges finished drafts checks ``tool_calls``
    and accepts the rest. ``advise`` is asked before the call (``step.response``
    None) and after it.
    """

    async def intake(self, text: str, step: StepView) -> Intake | None:
        """The inbound text, reshaped, or a reply that ends the turn here."""
        return None

    async def select_tools(self, offered: list[dict[str, Any]], step: StepView) -> list[dict[str, Any]] | None:
        """The tool array this iteration, narrowed. Never wider than ``offered``."""
        return None

    async def advise(self, step: StepView) -> str | None:
        """One note for the next model call, or nothing."""
        return None

    async def system_addendum(self, step: StepView) -> Intake | None:
        """Text this agent adds after the system prefix for this call, or a reply
        that ends the turn because nothing of it fits. ``step.transcript`` shows
        the prefix without any earlier addendum of this conduct's: the host takes
        the previous one out before asking and splices the new one in after."""
        return None

    async def review(self, step: StepView) -> Verdict:
        """Whether this step stands."""
        return Accept()

    async def salvage(self, step: StepView) -> Any | None:
        """A reply for a turn that ended without one, or nothing."""
        return None

    async def outbound(self, reply: str, step: StepView) -> str | None:
        """What this turn sends in place of ``reply``, or None to leave it.

        Whole rather than a suffix: a conduct that appends returns
        ``reply + its own separator + its text``, and one that rewrites returns
        the rewrite, so neither has to be recovered from a prefix the host
        guesses at. The reply has already gone out; this is what the record and
        every later reader see."""
        return None

    async def archive(self, step: StepView, reply: str | None) -> Mapping[str, Mapping[str, Any]] | None:
        """The turn is over; file what this conduct keeps across turns, and hand
        back what it stamps on the turn's record -- observer name to counters --
        for the host to file with the reply."""
        return None

    async def observe(self, step: StepView) -> None:
        """Record without deciding."""

    def note(self, text: str) -> None:
        """A line for the turn's diagnostic trail, beside whatever the verb
        returns. Collected by the host after each verb and never shown to the
        model; a conduct with nothing to say leaves the trail alone."""
        self.__dict__.setdefault("_trail", []).append(text)

    def drain_trail(self) -> list[str]:
        """The lines noted since the last drain (host side)."""
        return self.__dict__.pop("_trail", [])


ConductFactory = Callable[[], AgentConduct]
"""Called once at each turn's start; the object it returns lives for that turn."""


__all__ = [
    "Accept",
    "AgentConduct",
    "ConductFactory",
    "End",
    "Intake",
    "Resample",
    "StepView",
    "Verdict",
]
