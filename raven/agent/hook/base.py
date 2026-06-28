"""AgentHook ABC + AgentHookContext + HookDecision.

This abstraction wires into AgentLoop, replacing the scattered callback
fields (``response_modifier`` / ``on_user_inbound`` / ``decision_consumer`` /
``enable_personalization``). eval_engine builds three concrete hook
implementations on top.

Design choices, deliberately kept narrow for the initial cut:

1. **All phases default to no-op.** Every method on ``AgentHook``
   returns a pass-through ``HookDecision()`` unless overridden. Subclasses
   only implement the phases they care about.

2. **Async everywhere.** Even phases that look synchronous today (e.g.
   ``response_modifier`` takes ``(str, str) -> str``) become ``async``
   here so an LLM-based hook (Eval judge, personalizer classifier) can
   be added without redoing the interface. Sync callers can simply
   ``return`` immediately.

3. **Three orthogonal return modes**, all expressed via
   ``HookDecision``:

   - *pass-through* — default. Let the next hook / main loop continue.
   - *short_circuit_result* — halt the chain and the AgentLoop returns
     this value as the outbound reply (or processes it as a final
     answer, depending on the phase). Used by
     ``decision_consumer`` short-circuit and the personalizer
     "ask a clarification question" branch.
   - *modified_content* — for ``after_send``: transform the outbound
     text. Used by Sentinel's nudge_inject (append nudge to reply).

4. **HookDecision is immutable from the hook's perspective.** Hooks
   build a fresh decision each call; ``CompositeHook`` is responsible
   for chaining modifications (next hook sees the previous hook's
   transformed content via ``ctx.outbound_content``).

5. **AgentHookContext fields are populated by phase.** Not every
   attribute is meaningful in every phase — e.g. ``turn_request`` is
   set during ``before_user_inbound`` but ``None`` during
   ``before_iteration``. Callers (AgentLoop) are responsible
   for setting the relevant fields before invoking a phase. Hooks
   should not panic on ``None`` for unused fields.
"""

from __future__ import annotations

from abc import ABC
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from raven.spine.turn import TurnRequest


# ---------------------------------------------------------------------------
# Context & Decision
# ---------------------------------------------------------------------------


@dataclass
class AgentHookContext:
    """State carried through a hook chain for one AgentLoop turn.

    ``session_key`` is the only universally required field. The rest are
    populated by phase — see each hook method's docstring on
    :class:`AgentHook` for what's set when.

    The context is mutable: callers (AgentLoop / CompositeHook) update
    fields between phases as new information becomes available (e.g.
    ``response`` is filled after the LLM returns; ``outbound_content``
    is filled before ``after_send``). Hooks may also mutate fields
    they own — but the canonical channel for "I changed the outbound
    text" is the ``HookDecision.modified_content`` return value, which
    ``CompositeHook`` propagates into the context for the next hook.
    """

    session_key: str

    # ── before_user_inbound ──
    turn_request: "TurnRequest | None" = None

    # ── before_iteration / before_execute_tools / after_iteration ──
    iteration: int | None = None
    messages: list[dict[str, Any]] | None = None
    tools: list[dict[str, Any]] | None = None
    response: Any | None = None  # LLMResponse or dict; left as Any to avoid
                                  # an import cycle from this base module.

    # ── after_send ──
    outbound_content: str | None = None

    # ── Free-form ──
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class HookDecision:
    """Result of a hook invocation.

    Three states (mutually compatible only in this combinatorial sense):

    - ``pass_through=True``, no short-circuit, no modification — default,
      continue to next hook / main loop.
    - ``short_circuit_result`` is set — halt the chain; AgentLoop treats
      this value as the final answer for the current phase.
    - ``modified_content`` is set — only meaningful for the ``after_send``
      phase; the next hook in the chain sees the modified text as
      ``ctx.outbound_content``.

    Hooks should not mix ``short_circuit_result`` and ``modified_content``
    in a single decision — short-circuit halts further processing, so
    a content modification would be moot.
    """

    pass_through: bool = True
    short_circuit_result: Any | None = None
    modified_content: str | None = None
    notes: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# AgentHook ABC
# ---------------------------------------------------------------------------


class AgentHook(ABC):
    """Base class for AgentLoop lifecycle hooks.

    All phases default to no-op (return a pass-through ``HookDecision``).
    Subclasses override only the methods they need.
    """

    @property
    def name(self) -> str:
        """Human-readable identifier — appears in error logs.

        Subclasses should override for clarity (e.g. "SentinelInjectHook"
        rather than "InjectHook" so the source subsystem is visible at
        a glance).
        """
        return type(self).__name__

    # ── User-inbound phase ─────────────────────────────────────────────

    async def before_user_inbound(
        self, ctx: AgentHookContext
    ) -> HookDecision:
        """Fires when a fresh user message arrives, before AgentLoop
        dispatches it to the LLM.

        Used for:
          - Sentinel ``decision_consumer`` returns a short-circuit
            reply when the inbound is a /pick reply to a previously
            dispatched task-discovery menu.
          - Sentinel ``FeedbackTracker`` observes (no short-circuit)
            to mark recent nudges as accepted / dismissed.
          - Personalizer Step 1+2 may short-circuit with a clarification
            question instead of running the main loop.

        Context fields populated: ``session_key``, ``turn_request``.
        """
        return HookDecision()

    # ── ReAct iteration phases ────────────────────────────────────────

    async def before_iteration(
        self, ctx: AgentHookContext
    ) -> HookDecision:
        """Fires before each LLM call in the ReAct loop.

        Used for:
          - Token budget check (refuse to start another iteration if
            we'd blow the budget).
          - Pruning (skip iteration when the work is clearly done).

        Context fields populated: ``session_key``, ``iteration``,
        ``messages``, ``tools``.
        """
        return HookDecision()

    async def before_execute_tools(
        self, ctx: AgentHookContext
    ) -> HookDecision:
        """Fires after the LLM returns ``tool_calls`` but before the
        tools actually execute.

        Used for:
          - Pre-tool-call audit / approval.

        Context fields populated: ``session_key``, ``iteration``,
        ``messages``, ``response`` (the LLM response carrying tool calls).
        """
        return HookDecision()

    async def after_iteration(
        self, ctx: AgentHookContext
    ) -> HookDecision:
        """Fires after each iteration completes (LLM call + any tool
        execution for that iteration).

        Used for:
          - Judging loop completion (is this turn done?).
          - Judging case success (did we succeed?) → writes to ``case.md``
            via memory_engine.

        Context fields populated: ``session_key``, ``iteration``,
        ``messages``, ``response``.
        """
        return HookDecision()

    # ── Outbound phase ─────────────────────────────────────────────────

    async def after_send(self, ctx: AgentHookContext) -> HookDecision:
        """Fires when the final outbound content has been assembled,
        before it is sent as the reply.

        Used for:
          - Sentinel ``NudgeInjector`` appends a queued nudge to the
            outbound reply (via ``modified_content``).
          - Personalizer ``post_learn`` observes the final exchange
            and updates user behaviors (no short-circuit, no mod).

        Context fields populated: ``session_key``, ``outbound_content``.
        Returning ``HookDecision(modified_content=...)`` rewrites the
        outbound text — ``CompositeHook`` chains modifications so a
        downstream hook sees the upstream one's output.
        """
        return HookDecision()


__all__ = ["AgentHook", "AgentHookContext", "HookDecision"]
