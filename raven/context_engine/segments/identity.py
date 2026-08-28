"""Segment 1 — ``# Raven`` identity / runtime. Host-owned."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from raven.context_engine.base import AssemblyContext, Segment
from raven.context_engine.segments import render


class IdentitySegmentBuilder:
    name = "identity"
    order = 1
    needs_prefix = False

    def __init__(
        self,
        workspace: Path,
        list_subagents: Any | None = None,
        get_tool_definitions: Any | None = None,
    ) -> None:
        self._workspace = workspace
        self._list_subagents = list_subagents
        self._get_tool_definitions = get_tool_definitions

    def _specialists(self) -> list[tuple[str, str]]:
        """``(name, owns)`` for every agent that declares what it owns.

        Read through the callable per turn rather than captured: the loop builds
        this segment before its ``SubagentManager`` exists, and the agent table
        is rebuilt on a hot config apply. A lookup that raises means "say nothing
        about delegation" -- prompt assembly must not fail because the table is
        mid-rebuild.

        The generic row is never one, on the same grounds ``spawn`` states and the
        skill gate applies: it carries no capability bias, so it owns no kind of
        work. Excluded by name rather than left to the config, because a row that
        did declare ownership would render a prohibition whose subject is the
        agent reading it -- "you must not do this, hand it to yourself".
        """
        from raven.agent.subagent.builtin_agents import GENERIC_AGENT

        if self._list_subagents is None:
            return []
        try:
            metas = self._list_subagents() or []
        except Exception:
            return []
        return [(m.name, m.owns) for m in metas if getattr(m, "owns", "") and getattr(m, "name", "") != GENERIC_AGENT]

    async def build(self, ctx: AssemblyContext) -> Segment | None:
        # The playbook library used to be listed here as well as in the tool.
        # Two resident surfaces describing the same thing is how they drift, and
        # this one drifted first: it told the model that playbooks are "triggered
        # by asking" and that a disabled one runs on an explicit ask, both of
        # which described the passive matcher that no longer exists. The listing
        # lives in ``load_playbook``'s description, where it can also carry the
        # parameter table and be narrowed per turn -- and where the model is
        # standing when it has to choose.
        return Segment(
            text=render.identity_text(
                self._workspace,
                specialists=self._specialists(),
                dispatch_tools=render.live_dispatch_tools(self._get_tool_definitions),
            )
        )
