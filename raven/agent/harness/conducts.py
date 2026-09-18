"""How the default roles read what several participants say about one step.

One place for the merge rules, read by the default modules and by a seat that
runs before any harness is bound (a plugin test driving its hook directly).
The rules are the hook composite's, restated over verbs: the first participant
that ends or resamples a step decides it; advice is joined in order with a
blank line; the first salvaged reply stands; an intake threads the text through
every participant until one ends the turn.

This is also where a participant's answer stops being pure data and becomes
something the shell can act on. Every verb hands back a mapping or None, so
that a generated function can answer as well as a plugin can; the readers here
vet those mappings and drop what they cannot read. A malformed answer is
silence, never a refusal -- the third invariant -- so a participant cannot
escalate by answering badly.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from raven.contracts.agent_conduct import AgentConduct, StepView

_KINDS = ("accept", "resample", "end")


@dataclass(frozen=True)
class Verdict:
    """One participant's answer about a step, as the shell reads it.

    Host-side: a participant answers with a mapping and this is what the seat
    and the loop work from. ``accept`` lets the step stand, ``resample`` sends
    the loop back to the model call, ``end`` closes the turn.
    """

    kind: str = "accept"
    reason: str | None = None
    inject: list[dict[str, Any]] | None = None
    overrides: dict[str, Any] | None = None
    reply: Any | None = None
    note: str | None = None

    @property
    def accepted(self) -> bool:
        return self.kind == "accept"


@dataclass(frozen=True)
class Intake:
    """What an ``intake`` or a ``system_addendum`` came to, as the shell reads it."""

    text: str = ""
    reply: Any | None = None
    note: str | None = None


def _text(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def read_verdict(answer: Any) -> Verdict:
    """A participant's verdict, or an accept when it said nothing readable.

    Anything unreadable reads as an accept, which is what "failure equals
    silence" means for a verb whose other answers halt a turn: a participant
    that returns nonsense must not be able to stop the loop with it.
    """
    if not isinstance(answer, Mapping):
        return Verdict()
    kind = answer.get("verdict")
    if kind not in _KINDS:
        return Verdict(note=_text(answer.get("note")))
    inject = answer.get("inject")
    overrides = answer.get("overrides")
    return Verdict(
        kind=kind,
        reason=_text(answer.get("reason")),
        inject=[row for row in inject if isinstance(row, Mapping)]
        if isinstance(inject, Sequence) and not isinstance(inject, str)
        else None,
        overrides=dict(overrides) if isinstance(overrides, Mapping) else None,
        reply=answer.get("reply"),
        note=_text(answer.get("note")),
    )


def read_intake(answer: Any, *, text: str = "") -> Intake | None:
    """What a participant made of the text it was handed, or None for nothing.

    ``text`` is what it was asked about, so a mapping that names no text of its
    own leaves the turn's own words alone rather than blanking them.
    """
    if not isinstance(answer, Mapping):
        return None
    said = answer.get("text")
    return Intake(
        text=said if isinstance(said, str) else text,
        reply=answer.get("reply"),
        note=_text(answer.get("note")),
    )


async def compose_intake(text: str, step: StepView, conducts: Sequence[AgentConduct]) -> Intake | None:
    current = text
    notes: list[str] = []
    changed = False
    for conduct in conducts:
        intake = read_intake(await conduct.intake(current, step), text=current)
        if intake is None:
            continue
        if intake.note:
            notes.append(intake.note)
        if intake.reply is not None:
            return Intake(text=current, reply=intake.reply, note="\n".join(notes) or None)
        if intake.text != current:
            current = intake.text
            changed = True
    if not changed and not notes:
        return None
    return Intake(text=current, note="\n".join(notes) or None)


async def compose_advice(step: StepView, conducts: Sequence[AgentConduct]) -> str | None:
    notes = [note for conduct in conducts if (note := _text(await conduct.advise(step)))]
    return "\n\n".join(notes) or None


async def compose_review(step: StepView, conducts: Sequence[AgentConduct]) -> Verdict:
    accepted: list[Verdict] = []
    for conduct in conducts:
        verdict = read_verdict(await conduct.review(step))
        if not verdict.accepted:
            return verdict
        accepted.append(verdict)
    if not accepted:
        return Verdict()
    # The first accepted verdict carries the merge, so an accept keeps whatever
    # else it was written with whether one participant answered or five.
    notes = [v.note for v in accepted if v.note]
    return Verdict(
        kind="accept",
        reason=accepted[0].reason,
        inject=accepted[0].inject,
        overrides=accepted[0].overrides,
        reply=accepted[0].reply,
        note="\n".join(notes) or None,
    )


async def compose_salvage(step: StepView, conducts: Sequence[AgentConduct]) -> str | None:
    for conduct in conducts:
        salvaged = await conduct.salvage(step)
        if isinstance(salvaged, str) and salvaged:
            return salvaged
    return None


async def compose_addendum(step: StepView, conducts: Sequence[AgentConduct]) -> Intake | None:
    """Every participant's system addendum, joined in order; the first that
    answers with a reply instead ends the turn and the joining stops there."""
    texts: list[str] = []
    notes: list[str] = []
    for conduct in conducts:
        addendum = read_intake(await conduct.system_addendum(step))
        if addendum is None:
            continue
        if addendum.note:
            notes.append(addendum.note)
        if addendum.reply is not None:
            return Intake(text="", reply=addendum.reply, note="\n".join(notes) or None)
        if addendum.text:
            texts.append(addendum.text)
    if not texts and not notes:
        return None
    return Intake(text="\n\n".join(texts), note="\n".join(notes) or None)


async def compose_record(
    step: StepView, reply: str | None, conducts: Sequence[AgentConduct]
) -> dict[str, dict[str, Any]] | None:
    """What every participant files on the turn's record, merged by observer
    name: a later one's counters join an earlier one's rather than replacing
    them, so two participants stamping the same name both survive."""
    filed: dict[str, dict[str, Any]] = {}
    for conduct in conducts:
        stamped = await conduct.archive(step, reply)
        if not isinstance(stamped, Mapping):
            continue
        for name, counters in stamped.items():
            if isinstance(counters, Mapping):
                filed.setdefault(str(name), {}).update(dict(counters))
    return filed or None


async def compose_tools(
    offered: list[dict[str, Any]], step: StepView, conducts: Sequence[AgentConduct]
) -> list[dict[str, Any]] | None:
    """The tool array each participant leaves for the next, threaded in order.
    None when nobody changed it, so the seat can tell "no opinion" from "this
    array"."""
    current = list(offered)
    changed = False
    for conduct in conducts:
        answer = await conduct.select_tools(list(current), step)
        if not isinstance(answer, Sequence) or isinstance(answer, str):
            continue
        rows = [row for row in answer if isinstance(row, Mapping)]
        if rows != current:
            current = [dict(row) for row in rows]
            changed = True
    return current if changed else None


__all__ = [
    "Intake",
    "Verdict",
    "compose_addendum",
    "compose_advice",
    "compose_intake",
    "compose_record",
    "compose_review",
    "compose_salvage",
    "compose_tools",
    "read_intake",
    "read_verdict",
]
