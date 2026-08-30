"""Fetch-gate hook: withhold ``web_search`` until a page has been opened.

The decision lives in ``research_flow.support.fetch_gate_core.FetchGate``; this
module is the seam that feeds it tool results and turns its verdict into an
action. Split that way so the rule is testable without an agent loop, and so the
action - removing a tool from one iteration's schema - is the only thing that
needs the loop.
"""

from __future__ import annotations

import logging

from raven.contracts.loop_hooks import AgentHook, AgentHookContext, HookDecision
from raven.security.trust import unwrap_untrusted
from research_flow.support.fetch_gate_core import FetchGate
from research_flow.support.harness_text import SUFFICIENCY_PREFIX, fetch_gate_notice
from research_flow.tools.web import fetch_result_ok

logger = logging.getLogger(__name__)

_GATED_TOOL = "web_search"


class FetchGateObserver(AgentHook):
    """Close ``web_search`` on a long unread-search streak; reopen on a fetch."""

    def __init__(self, gate: FetchGate) -> None:
        self._gate = gate

    @property
    def name(self) -> str:
        return "FetchGateObserver"

    async def before_execute_tools(self, ctx: AgentHookContext) -> HookDecision:
        """Record a ``web_search`` call made on an iteration where it was withheld.

        ★ 20260825: **observation only, deliberately** — same scope as
        ``AskUserGate.before_execute_tools``, which leaves such a call to execute
        "so the model reads the fallback string and goes back to work".

        It exists because this gate's design note claimed that withholding the tool
        makes re-requesting it "not an available move", and that is false:
        ``ToolRegistry.execute`` resolves against the REGISTRY, and
        ``HookDecision.modified_tools`` is scoped to the iteration and never edits
        the registry (``raven.contracts.loop_hooks`` says so). A withheld tool can
        still be named and still runs.

        Worse, before this counter the bypass was **invisible**: ``observe_search``
        counts every ``web_search`` result including bypassed ones, so the streak
        keeps climbing and ``gate_closed_now`` stays True — a fully bypassed turn and
        a fully honoured turn produced identical counters. Observed willingness of
        this model to name un-offered tools on dr@3.4 is low but non-zero (``exec``
        ×9 on base128, a hallucinated ``search`` ×1 on dr128), and a tool that was
        advertised, called 15+ times, then silently withdrawn is an untested
        condition. Whoever enables this knob has to be able to see it.
        """
        state = ctx.metadata.setdefault("fetch_gate", {})
        if not state.get("gate_closed_now"):
            return HookDecision()
        proposed = list(getattr(ctx.response, "tool_calls", None) or [])
        n = sum(1 for c in proposed if getattr(c, "name", "") == _GATED_TOOL)
        if n:
            state["gate_called_when_closed"] = int(state.get("gate_called_when_closed") or 0) + n
        return HookDecision()

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
        # The scan starts at ``ctx.turn_base``, never 0: everything below it is
        # the session's persisted history, and a previous turn that ended on a
        # long unread-search tail would otherwise close ``web_search`` on this
        # turn's very first iteration - silently, because the newest message at
        # that point is the user's question, not a tool result to hang the
        # notice on.
        watermark = int(state.get("watermark", ctx.turn_base or 0))
        for m in messages[watermark:]:
            if not isinstance(m, dict) or m.get("role") != "tool":
                continue
            if m.get("name") == _GATED_TOOL:
                self._gate.observe_search()
            elif m.get("name") == "web_fetch":
                # Same predicate the client-side ledger writes its ``ok`` column
                # from, imported rather than re-spelled; see ``fetch_result_ok``.
                # ★ 20260825: unfence FIRST. ``fetch_result_ok`` decides by
                # ``json.loads``, and what lands in ``messages`` is the FENCED
                # string, so before this line the predicate returned False for
                # 100% of real fetches - the streak was never zeroed, the gate
                # never reopened, and the release valve was unreachable. Measured
                # on dr@3.4: intended 11.1% of items vs shipped 38.9%, with the
                # action permanent instead of released on the next page opened.
                # The two callers of ``fetch_result_ok`` were reading two different
                # STRINGS while sharing one predicate - the ledger gets the raw
                # return, this seam gets the fenced one - which is the failure the
                # predicate's own docstring says it exists to prevent, one layer up.
                self._gate.observe_fetch(fetch_result_ok(unwrap_untrusted(m.get("content"))))
        state["watermark"] = len(messages)

        closed = self._gate.evaluate()
        # Written every iteration, fired or not: a counter that appears only on
        # firing cannot distinguish "did not fire" from "was not installed".
        state.update(self._gate.counters())
        if not closed:
            return HookDecision()

        tools = [t for t in (ctx.tools or []) if (t.get("function") or {}).get("name") != _GATED_TOOL]
        if len(tools) == len(ctx.tools or []):
            # Nothing was removed, so the gate is closed over a turn that cannot
            # see the tool anyway. Report it rather than pretending to act: a rule
            # whose action is a no-op reads, in every downstream field, exactly
            # like a rule that acted and did not help.
            state["gate_tool_absent"] = True
            return HookDecision()

        note: str | None = None
        if state.get("notice_at") != self._gate.fired:
            # Once per firing, not once per gated iteration. The saturation stop
            # re-emitted its refusal on every suppressed call and one turn
            # accumulated 195 of them; the cost of a repeated harness sentence is
            # paid in context on the arm that is already closest to overflowing.
            #
            # ``notice_at`` advances only when the notice actually lands: when the
            # newest message is not a tool result there is nothing to hang it on,
            # and advancing anyway would skip the explanation for this firing
            # instead of deferring it to the next iteration that can carry it.
            #
            # A body already carrying the sufficiency release note defers the same
            # way: that note says "stop opening pages, write" and this one says
            # "open a page" - two harness sentences pointing opposite ways on one
            # body. This is the only seam that can see the stack (the release is
            # written in after_iteration, this notice in the NEXT before_iteration,
            # both onto the same newest tool result), and the condition can only be
            # true with the sufficiency knob on, so every measured arm is
            # byte-identical.
            #
            # The notice travels as ``append_note``: the loop lands it on that
            # same newest tool result, a blank line between, so the transcript
            # is never mutated from inside the hook.
            if messages and messages[-1].get("role") == "tool":
                body = messages[-1].get("content") or ""
                if SUFFICIENCY_PREFIX not in body:
                    state["notice_at"] = self._gate.fired
                    note = fetch_gate_notice()
            logger.warning(
                "fetch-gate: closed web_search after %d unread searches (fire %d)",
                self._gate.streak,
                self._gate.fired,
            )
        return HookDecision(modified_tools=tools, append_note=note)


__all__ = ["FetchGateObserver"]
