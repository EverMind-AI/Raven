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
from typing import Any

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
    phase: str
    """Which moment of the turn this is: ``user_inbound``, ``iteration``,
    ``execute_tools``, ``after_iteration``, ``answerless`` or ``sent``. Two
    verbs are asked at more than one of them -- ``review`` before a step's tools
    run and again after, ``advise`` before the call and after it -- and a
    participant that behaves differently at each should read this rather than
    infer it from which other fields happen to be set."""
    tools: Sequence[dict[str, Any]] = ()
    """The tool definitions this call will carry, for a conduct sizing what it adds."""
    window: int | None = None
    """The context window the loop was told to assume, in tokens -- the sizing
    fallback for a caller with no active model binding to consult."""
    max_iterations: int | None = None

    @property
    def tools_ran(self) -> bool:
        """Whether this step's tool calls have run, for the two ``review``
        moments. Derived from ``phase`` so the two cannot disagree."""
        return self.phase == "after_iteration"

    """The loop's iteration cap this turn, for a conduct that paces itself against it."""


Answer = Mapping[str, Any]
"""What a verb hands back: a plain mapping, or None for "nothing to say".

Pure data on purpose. A participant is not only a plugin: a dispatch's own
judgements and anything generated for one answer the same verbs, and a
generated function can build a dict where it cannot build a host class. The
host reads these into its own shapes, vets them there, and treats a mapping it
cannot read as silence -- the third invariant, and the reason nothing here
carries behaviour.

The keys each verb reads are named on it. The four builders below write them,
so a hand-written conduct keeps saying ``Resample("too thin")`` rather than
spelling the mapping out.
"""


def Intake(text: str, reply: Any | None = None, note: str | None = None) -> dict[str, Any]:  # noqa: N802
    """The text a turn goes on with, or a reply that ends it before any model
    call (a fail-closed sentinel, a command)."""
    return {"text": text, "reply": reply, "note": note}


def Accept(note: str | None = None) -> dict[str, Any]:  # noqa: N802 - reads as the verdict it is
    """The step stands."""
    return {"verdict": "accept", "note": note}


def Resample(  # noqa: N802
    reason: str,
    *,
    inject: Sequence[dict[str, Any]] | None = None,
    overrides: dict[str, Any] | None = None,
    note: str | None = None,
) -> dict[str, Any]:
    """Send the loop back to the model call, with ``inject`` appended to the
    transcript and ``overrides`` on that one call. The loop bounds how many
    resamples a turn may spend; a participant only pronounces."""
    return {
        "verdict": "resample",
        "reason": reason,
        "inject": list(inject) if inject else None,
        "overrides": dict(overrides) if overrides else None,
        "note": note,
    }


def End(reply: Any, note: str | None = None) -> dict[str, Any]:  # noqa: N802
    """Close the turn with this reply."""
    return {"verdict": "end", "reply": reply, "note": note}


class AgentConduct:
    """Base for a sub-agent's conduct: every verb answers "nothing to say".

    A subclass overrides only the verbs it has a judgement for. ``review`` is
    asked twice per iteration when the model called tools -- once before they
    run (``step.tools_ran`` False) and once after (True) -- and once when it
    did not; a conduct that only judges finished drafts checks ``tool_calls``
    and accepts the rest. ``advise`` is asked before the call (``step.response``
    None) and after it.
    """

    async def intake(self, text: str, step: StepView) -> Answer | None:
        """The inbound text, reshaped, or a reply that ends the turn here."""
        return None

    async def select_tools(self, offered: list[dict[str, Any]], step: StepView) -> list[dict[str, Any]] | None:
        """The tool array this iteration carries, or None to leave ``offered``.

        Usually narrower, and not required to be: a conduct may hand back an
        array carrying a tool its own product contributes, which is what the
        research flow does with its escalation tool. Handing a definition back
        is not granting it -- ``ToolRegistry.execute`` still adjudicates every
        call, so the product's own withholding stands whatever this returns.

        Definitions rather than names, and so deliberately out of step with the
        Charter's ``tools`` field, which is a name tuple. A name is enough to
        narrow and not enough to contribute: the tool a participant adds for one
        iteration has no entry in the registry to look a schema up from. Aligning
        the two would mean moving that contribution to Capability, which is a
        larger change than this seam, and is why the mismatch is written down
        here rather than papered over."""
        return None

    async def advise(self, step: StepView) -> str | None:
        """One note for the next model call, or nothing."""
        return None

    async def system_addendum(self, step: StepView) -> Answer | None:
        """Text this agent adds after the system prefix for this call, or a reply
        that ends the turn because nothing of it fits. ``step.transcript`` shows
        the prefix without any earlier addendum of this conduct's: the host takes
        the previous one out before asking and splices the new one in after."""
        return None

    async def review(self, step: StepView) -> Answer | None:
        """Whether this step stands: ``Accept()``, ``Resample(...)`` or
        ``End(reply)``, or None for the same thing ``Accept()`` says."""
        return None

    async def salvage(self, step: StepView) -> str | None:
        """The reply text for a turn that ended without one, or nothing. A
        string rather than any object the host happens to accept, so that what
        a generated participant can say is what every participant may say."""
        return None

    async def outbound(self, reply: str, step: StepView) -> str | None:
        """What this turn sends in place of ``reply``, or None to leave it.

        Whole rather than a suffix: a conduct that appends returns
        ``reply + its own separator + its text``, and one that rewrites returns
        the rewrite, so neither has to be recovered from a prefix the host
        guesses at. The reply has already gone out; this is what the record and
        every later reader see.

        The one verb with no module seat, on purpose. The four roles are the
        turn's *decisions* -- the window, the guidance, the tools, the model
        call -- and none of them owns what a turn finally delivers to a person.
        The host applies this one directly, which also means it is the one verb
        a generated participant cannot be given: there is nothing to compose it
        with and nothing to vet it."""
        return None

    async def archive(self, step: StepView, reply: str | None) -> Mapping[str, Mapping[str, Any]] | None:
        """The turn is over; file what this conduct keeps across turns, and hand
        back what it stamps on the turn's record -- observer name to counters --
        for the host to file with the reply."""
        return None

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
    "Answer",
    "StepView",
]
