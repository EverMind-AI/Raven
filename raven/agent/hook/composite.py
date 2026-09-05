"""CompositeHook — combine multiple ``AgentHook`` instances into one.

Semantics:

- **Order is registration order.** ``CompositeHook([A, B, C])`` runs
  A → B → C for every phase. Late registrations via ``append`` go to
  the end.

- **Short-circuit halts the chain.** The first hook in a phase that
  returns ``HookDecision(short_circuit_result=…)`` wins; subsequent
  hooks in that phase are NOT called. This is critical for the
  ``before_user_inbound`` phase, where Sentinel's decision_consumer
  short-circuits a ``/pick`` reply and the personalizer must not run
  on what it would mis-classify as a fresh request.

- **Content modifications chain.** For phases that produce a
  ``modified_content`` (currently only ``after_send``), each hook's
  output becomes the next hook's input via
  ``ctx.outbound_content``. The final return value carries the
  fully-chained ``modified_content``.

- **Exceptions are isolated.** A hook that raises is logged and
  treated as a pass-through no-op; the chain continues with the next
  hook. This mirrors the EventBus contract and is what lets a single
  flaky hook (e.g. Personalizer's classifier hitting an LLM timeout)
  not take down the whole turn.
"""

from __future__ import annotations

import logging
from typing import Iterable

from raven.agent.hook.base import AgentHook, AgentHookContext, HookDecision

logger = logging.getLogger(__name__)


_CHAIN_MODIFIED_PHASES = frozenset({"after_send"})


class CompositeHook(AgentHook):
    """Aggregate hook that dispatches each phase to a list of children."""

    def __init__(self, hooks: Iterable[AgentHook] | None = None) -> None:
        self._hooks: list[AgentHook] = list(hooks or [])

    @property
    def name(self) -> str:
        if not self._hooks:
            return "CompositeHook(empty)"
        return "CompositeHook(" + ", ".join(h.name for h in self._hooks) + ")"

    def __len__(self) -> int:
        return len(self._hooks)

    def __iter__(self):
        return iter(self._hooks)

    def append(self, hook: AgentHook) -> None:
        """Add a hook to the end of the chain."""
        self._hooks.append(hook)

    def extend(self, hooks: Iterable[AgentHook]) -> None:
        """Add multiple hooks (in order) to the end of the chain."""
        for h in hooks:
            self._hooks.append(h)

    # ─────────────────────────────────────────────────────────────────
    # Phase dispatchers
    # ─────────────────────────────────────────────────────────────────

    async def before_user_inbound(self, ctx: AgentHookContext) -> HookDecision:
        return await self._run_phase("before_user_inbound", ctx)

    async def before_iteration(self, ctx: AgentHookContext) -> HookDecision:
        return await self._run_phase("before_iteration", ctx)

    async def before_execute_tools(self, ctx: AgentHookContext) -> HookDecision:
        return await self._run_phase("before_execute_tools", ctx)

    async def after_iteration(self, ctx: AgentHookContext) -> HookDecision:
        return await self._run_phase("after_iteration", ctx)

    async def after_send(self, ctx: AgentHookContext) -> HookDecision:
        return await self._run_phase("after_send", ctx)

    # ─────────────────────────────────────────────────────────────────
    # Core dispatcher
    # ─────────────────────────────────────────────────────────────────

    async def _run_phase(self, phase: str, ctx: AgentHookContext) -> HookDecision:
        """Invoke ``phase`` on every child hook, honoring short-circuit
        and content-chaining semantics.

        Returns the final ``HookDecision`` — short-circuited or
        pass-through (with ``modified_content`` populated if this phase
        supports content chaining and any hook produced a modification).
        """
        chain_content = phase in _CHAIN_MODIFIED_PHASES
        last_modified: str | None = None

        for hook in self._hooks:
            method = getattr(hook, phase)
            try:
                decision = await method(ctx)
            except Exception:
                logger.exception(
                    "hook %s.%s raised; treating as no-op and continuing",
                    hook.name,
                    phase,
                )
                continue

            if decision.short_circuit_result is not None:
                return decision

            if chain_content and decision.modified_content is not None:
                # Propagate to next hook in this phase
                ctx.outbound_content = decision.modified_content
                last_modified = decision.modified_content

        return HookDecision(modified_content=last_modified)


__all__ = ["CompositeHook"]
