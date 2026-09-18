"""How the default roles compose what several conducts say about one step.

One place for the merge rules, read by the default modules and by a seat that
runs before any harness is bound (a plugin test driving its hook directly).
The rules are the hook composite's, restated over verbs: the first conduct
that ends or resamples a step decides it; advice is joined in order with a
blank line; the first salvaged reply stands; an intake threads the text
through every conduct until one ends the turn.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace
from typing import Any

from raven.contracts.agent_conduct import Accept, AgentConduct, Intake, StepView, Verdict


async def compose_intake(text: str, step: StepView, conducts: Sequence[AgentConduct]) -> Intake | None:
    current = text
    notes: list[str] = []
    changed = False
    for conduct in conducts:
        intake = await conduct.intake(current, step)
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
    notes = [note for conduct in conducts if (note := await conduct.advise(step))]
    return "\n\n".join(notes) or None


async def compose_review(step: StepView, conducts: Sequence[AgentConduct]) -> Verdict:
    accepted: list[Verdict] = []
    for conduct in conducts:
        verdict = await conduct.review(step)
        if not verdict.accepted:
            return verdict
        accepted.append(verdict)
    if not accepted:
        return Accept()
    # The first accepted verdict carries the merge, so an accept keeps whatever
    # else it was written with whether one conduct answered or five. Rebuilding
    # a bare ``Accept`` above one and not the other made the same conduct behave
    # differently in a process with a second plugin loaded.
    notes = [v.note for v in accepted if v.note]
    return replace(accepted[0], note="\n".join(notes) or None)


async def compose_salvage(step: StepView, conducts: Sequence[AgentConduct]) -> Any | None:
    for conduct in conducts:
        salvaged = await conduct.salvage(step)
        if salvaged is not None:
            return salvaged
    return None


__all__ = ["compose_advice", "compose_intake", "compose_review", "compose_salvage"]
