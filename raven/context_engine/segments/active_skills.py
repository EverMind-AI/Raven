"""Segment 4 — ``# Active Skills`` (always-on skills). Host-owned."""

from __future__ import annotations

from typing import TYPE_CHECKING

from raven.context_engine.base import AssemblyContext, Segment

if TYPE_CHECKING:
    from raven.memory_engine.skill_forge import LocalSkillCatalog


class ActiveSkillsSegmentBuilder:
    name = "active_skills"
    order = 4
    needs_prefix = False

    def __init__(self, skill_catalog: "LocalSkillCatalog") -> None:
        self._skills = skill_catalog

    async def build(self, ctx: AssemblyContext) -> Segment | None:
        always_skills = self._skills.get_always_skills()
        if not always_skills:
            return None
        cfg = getattr(self._skills, "_config", None)
        always_max = getattr(cfg, "always_max", 5) or 5
        content = self._skills.load_skills_for_context(always_skills, max_inject=always_max)
        if not content:
            return None
        return Segment(text=f"# Active Skills\n\n{content}")
