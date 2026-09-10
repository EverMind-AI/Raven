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
"""

from __future__ import annotations

from raven.contracts.harness import PlanningRequest, PlanningResult


class DefaultPlanning:
    """Return the turn's messages unchanged: zero model calls, zero rewrites."""

    async def prepare(self, request: PlanningRequest) -> PlanningResult:
        return PlanningResult(messages=request.messages)


__all__ = ["DefaultPlanning"]
