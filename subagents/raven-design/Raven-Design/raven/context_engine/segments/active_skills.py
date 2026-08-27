"""Segment 4 — ``# Active Skills`` (always-on skills). Host-owned."""

from __future__ import annotations

from typing import TYPE_CHECKING

from raven.context_engine.base import AssemblyContext, Segment
from raven.context_engine.segments import render
from raven.tracing import semconv, trace

if TYPE_CHECKING:
    from raven.memory_engine.skill_forge import LocalSkillCatalog


class ActiveSkillsSegmentBuilder:
    name = "active_skills"
    order = 4
    needs_prefix = False

    def __init__(self, skill_catalog: "LocalSkillCatalog") -> None:
        self._skills = skill_catalog

    @trace.instrument("skill.inject", kind="skill", detached=True, extract=semconv.skill_inject_active)
    async def build(self, ctx: AssemblyContext) -> Segment | None:
        content = render.render_active_skills(self._skills)
        if not content:
            return None
        return Segment(text=f"# Active Skills\n\n{content}")
