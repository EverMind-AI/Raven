"""Fetch-gate hook: withhold ``web_search`` until a page has been opened.

The decision lives in ``raven.agent.fetch_gate.FetchGate``; this module is the
seam that feeds it tool results and turns its verdict into an action. Split that
way so the rule is testable without an agent loop, and so the action - removing
a tool from one iteration's schema - is the only thing that needs the loop.
"""

from __future__ import annotations

import logging

from raven.agent.fetch_gate import FetchGate
from raven.agent.harness_text import fetch_gate_notice
from raven.agent.hook.base import AgentHook, AgentHookContext, HookDecision
from raven.agent.tools.web import fetch_result_ok

logger = logging.getLogger(__name__)

_GATED_TOOL = "web_search"


class FetchGateObserver(AgentHook):
    """Close ``web_search`` on a long unread-search streak; reopen on a fetch."""

    def __init__(self, gate: FetchGate) -> None:
        self._gate = gate

    @property
    def name(self) -> str:
        return "FetchGateObserver"

    async def before_iteration(self, ctx: AgentHookContext) -> HookDecision:
        messages = ctx.messages or []
        state = ctx.metadata.setdefault("fetch_gate", {})
        if "watermark" not in state:
            # First iteration of a turn. The gate object is built once at flow
            # assembly and outlives the turn, while ``ctx.metadata`` is the thing
            # that is actually per-turn - so turn scope is taken from the metadata
            # dict rather than asserted separately, which is how ``fetch_floor``
            # gets it for free by keeping all its state there. Without this, a
            # gateway serving a second question would start it already closed, and
            # the symptom would be a rule that fires before its first search.
            self._gate.reset()
        watermark = int(state.get("watermark", 0))
        for m in messages[watermark:]:
            if not isinstance(m, dict) or m.get("role") != "tool":
                continue
            if m.get("name") == _GATED_TOOL:
                self._gate.observe_search()
            elif m.get("name") == "web_fetch":
                # Same predicate the client-side ledger writes its ``ok`` column
                # from, imported rather than re-spelled; see ``fetch_result_ok``.
                self._gate.observe_fetch(fetch_result_ok(m.get("content")))
        state["watermark"] = len(messages)

        closed = self._gate.evaluate()
        # Written every iteration, fired or not: a counter that appears only on
        # firing cannot distinguish "did not fire" from "was not installed".
        state.update(self._gate.counters())
        if not closed:
            return HookDecision()

        tools = [
            t for t in (ctx.tools or [])
            if (t.get("function") or {}).get("name") != _GATED_TOOL
        ]
        if len(tools) == len(ctx.tools or []):
            # Nothing was removed, so the gate is closed over a turn that cannot
            # see the tool anyway. Report it rather than pretending to act: a rule
            # whose action is a no-op reads, in every downstream field, exactly
            # like a rule that acted and did not help.
            state["gate_tool_absent"] = True
            return HookDecision()

        if state.get("notice_at") != self._gate.fired:
            # Once per firing, not once per gated iteration. The saturation stop
            # re-emitted its refusal on every suppressed call and one turn
            # accumulated 195 of them; the cost of a repeated harness sentence is
            # paid in context on the arm that is already closest to overflowing.
            state["notice_at"] = self._gate.fired
            if messages and messages[-1].get("role") == "tool":
                body = messages[-1].get("content") or ""
                messages[-1]["content"] = f"{body}\n\n{fetch_gate_notice()}"
            logger.warning(
                "fetch-gate: closed web_search after %d unread searches (fire %d)",
                self._gate.streak,
                self._gate.fired,
            )
        return HookDecision(modified_tools=tools)


__all__ = ["FetchGateObserver"]
