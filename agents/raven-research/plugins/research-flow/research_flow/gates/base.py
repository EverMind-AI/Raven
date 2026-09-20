"""What a research gate is: the chain the participant runs, not a hook the loop holds.

A gate keeps the six phase methods it always had -- the chain's order and its
composite merge are the measured surface, and every gate carries a version
label describing a distribution -- but it is no longer an ``AgentHook`` the
loop holds. The loop holds one ``ParticipantHook``; the research participant builds a
``GateCtx`` for each phase from the read-only fields of its ``StepView``
and its own turn-private ``facts`` dict, and runs the chain over that. The
attribute surface is the hook context's, so a gate reads what it always read;
where the values come from is what changed, and the loop's own context is no
longer something a gate can write.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from raven.contracts.loop_hooks import HookDecision


@dataclass
class GateCtx:
    """One phase's view of the turn, as the research chain reads it.

    Built by the participant, never by the loop. ``metadata`` is the participant's
    turn-private facts dict: the gates' counters, the cross-gate flags
    (``ask_user``, ``plain_first``, ``verify_gate``), and the three loop
    readings the chain consults (``mode``, ``mode_overlay``, ``hook_rollbacks``),
    seeded from the step each time.
    """

    session_key: str
    turn_request: Any | None = None
    inbound_content: str | None = None
    session_history: list[dict[str, Any]] | None = None
    iteration: int | None = None
    messages: list[dict[str, Any]] | None = None
    tools: list[dict[str, Any]] | None = None
    response: Any | None = None
    turn_question: str = ""
    turn_base: int = 0
    max_iterations: int | None = None
    context_window_tokens: int | None = None
    outbound_content: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


class Gate:
    """A research gate: six phases, all no-op unless overridden."""

    @property
    def name(self) -> str:
        return type(self).__name__

    async def before_user_inbound(self, ctx: GateCtx) -> HookDecision:
        return HookDecision()

    async def before_iteration(self, ctx: GateCtx) -> HookDecision:
        return HookDecision()

    async def before_execute_tools(self, ctx: GateCtx) -> HookDecision:
        return HookDecision()

    async def after_iteration(self, ctx: GateCtx) -> HookDecision:
        return HookDecision()

    async def terminal_answerless(self, ctx: GateCtx) -> HookDecision:
        return HookDecision()

    async def after_send(self, ctx: GateCtx) -> HookDecision:
        return HookDecision()


__all__ = ["Gate", "GateCtx"]
