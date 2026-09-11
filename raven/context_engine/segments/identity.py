"""Segment 1 — ``# Raven`` identity / runtime. Host-owned."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from raven.context_engine.segments import render
from raven.contracts.context import AssemblyContext, Segment


def _charter_brief() -> str:
    """What this dispatch's charter adds to the identity, if anything.

    Appended, never substituted. The text above carries the runtime facts a
    turn cannot work without -- the working directory, the platform policy, the
    rule about untrusted content -- so a brief that replaced it would buy a
    narrower job at the cost of the agent knowing where it is standing.

    Imported inside the call: a charter belongs to the dispatch layer and the
    context engine is reached from places that must not pull it in.
    """
    try:
        from raven.agent.subagent.charter import current_charter
    except Exception:  # noqa: BLE001 - no charter support is not a reason to lose the identity
        return ""
    charter = current_charter()
    if charter is None or not (charter.prompt or charter.stop_when):
        return ""
    lines = ["", "## This task", ""]
    if charter.prompt:
        lines.append(charter.prompt)
    if charter.stop_when:
        lines.append("")
        lines.append(f"Done when: {charter.stop_when}")
    return "\n".join(lines) + "\n"


class IdentitySegmentBuilder:
    name = "identity"
    order = 1
    needs_prefix = False
    # SOUL.md and the identity header: the same text on every turn.
    stable = True

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
        # Playbooks are listed in ``load_playbook``'s tool description and
        # nowhere else: one resident surface, where the parameter table lives,
        # where it can be narrowed per turn, and where the model is standing
        # when it has to choose. A second listing here would drift against it.
        text = render.identity_text(
            self._workspace,
            specialists=self._specialists(),
            dispatch_tools=render.live_dispatch_tools(self._get_tool_definitions),
        )
        return Segment(text=text + _charter_brief())
