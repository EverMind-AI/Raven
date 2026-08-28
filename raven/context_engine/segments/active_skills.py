"""Segment 4 — ``# Active Skills`` (always-on skills). Host-owned."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Callable

from raven.context_engine.base import AssemblyContext, Segment
from raven.context_engine.segments import render
from raven.memory_engine.skill_local.registry import filter_by_required_tools
from raven.tracing import semconv, trace

if TYPE_CHECKING:
    from raven.memory_engine.skill_forge import LocalSkillCatalog
    from raven.memory_engine.skill_local.types import SkillMeta


class ActiveSkillsSegmentBuilder:
    name = "active_skills"
    order = 4
    needs_prefix = False
    # The always-on skills do not depend on the message -- but this segment
    # sits behind `memory`, so the stable run has already ended by the time the
    # assembler reaches it and the flag buys nothing until the two are
    # reordered. Declared honestly anyway: the flag describes the segment, and
    # a reorder should not also have to discover what this one is.
    stable = True

    def __init__(
        self,
        skill_catalog: "LocalSkillCatalog",
        get_tool_definitions: Callable[[], list[Any]] | None = None,
    ) -> None:
        self._skills = skill_catalog
        self._get_tool_definitions = get_tool_definitions

    @trace.instrument("skill.inject", kind="skill", detached=True, extract=semconv.skill_inject_active)
    async def build(self, ctx: AssemblyContext) -> Segment | None:
        always_skills = self._require_tools(self._skills.get_always_skills())
        if not always_skills:
            return None
        cfg = getattr(self._skills, "_config", None)
        always_max = getattr(cfg, "always_max", 5) or 5
        content = self._skills.load_always_block(always_skills, max_inject=always_max)
        if not content:
            return None
        return Segment(text=f"# Active Skills\n\n{content}")

    def _require_tools(self, skills: "list[SkillMeta]") -> "list[SkillMeta]":
        """Drop always-skills whose ``requires.tools`` are not registered.

        An always-skill is resident unconditionally, so without this it can
        advertise a tool the agent does not hold — ``run_subagent_dag`` only
        registers when third-party sub-agents are configured, yet the skill
        that tells the agent when to reach for it ships enabled. Segment 5
        gets the same protection from the LLM gate's hard-constraint block;
        segment 5's is advisory, this one is a filter.

        Unlike ``requires.bins`` / ``requires.env`` (process-static, resolved
        in the registry) the tool set is live and hot-appliable, so it is
        checked here, per turn, against the definitions actually being sent.
        """
        return filter_by_required_tools(skills, render.collect_tool_names(self._get_tool_definitions))
