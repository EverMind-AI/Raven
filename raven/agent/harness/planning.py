"""The default Planning strategy: pass the turn's messages through.

Raven has no global planner and this is a faithful default rather than a
placeholder. Planning is the model's own, said in its own text, and the
position ahead of the iterations was measured to be the wrong one for a
harness to take it: the playbook funnel that used to sit there judged one
message with no history and, on a hit, replaced the whole turn -- so the
party with the least context made the most expensive call. It was removed.

What a harness-side planner *can* do without repeating that mistake is ask
again per iteration, once history exists. That seam is the hook chain's
``before_iteration``, which products already use for exactly this (the
research flow's sufficiency and spin-breaker gates ride it). So this module
stays a pass-through, and a replacement that wants to plan is expected to
change the messages a turn runs on, not to intercept ahead of it.

``advise`` is that per-iteration seat, made a verb: what the conducts of this
agent want said to the model before its next call, composed here.
"""

from __future__ import annotations

from collections.abc import Sequence

from raven.agent.harness.conducts import compose_advice
from raven.contracts.agent_conduct import AgentConduct, StepView
from raven.contracts.harness import PlanningModule, PlanningRequest, PlanningResult


class DefaultPlanning:
    """Return the turn's messages unchanged: zero model calls, zero rewrites."""

    async def prepare(self, request: PlanningRequest) -> PlanningResult:
        return PlanningResult(messages=request.messages)

    async def advise(self, step: StepView, conducts: Sequence[AgentConduct]) -> str | None:
        return await compose_advice(step, conducts)


def bind(planning: DefaultPlanning) -> PlanningModule:
    """Admit a built Planning role, naming a missing member at assembly rather
    than as an AttributeError inside somebody's turn.

    The same guard the other two roles get. It matters here for the reason it
    matters for Action: the composite catches whatever a hook phase raises and
    logs it under the *plugin's* name, so a role missing ``advise`` would read
    as a broken plugin rather than as a role that cannot serve.
    """
    if not isinstance(planning, PlanningModule):
        raise TypeError(
            f"{type(planning).__name__} cannot serve as the Planning role: it must provide prepare and advise"
        )
    return planning


__all__ = ["DefaultPlanning", "bind"]
