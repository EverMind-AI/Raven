"""Hook factory for the ``my_agent_flow`` contribution.

The hook inherits every phase from AgentHook (all six default to a
pass-through HookDecision) and overrides only ``after_send`` -- still a
no-op, kept as the worked example of where a phase override goes.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from raven.contracts.loop_hooks import AgentHook, AgentHookContext, HookDecision

if TYPE_CHECKING:
    from raven.plugins.context import PluginContext


class FlowHook(AgentHook):
    """Lifecycle hook: five phases inherited as no-ops, after_send overridden."""

    def __init__(self, ctx: "PluginContext") -> None:
        self._ctx = ctx

    @property
    def name(self) -> str:
        return "FlowHook"

    async def after_send(self, ctx: AgentHookContext) -> HookDecision:
        """Observe the outbound turn; decide nothing.

        A real agent reads ``ctx.outbound_content`` here (a report filed,
        a budget noted). Returning a default HookDecision passes through.
        """
        self._ctx.logger.debug("my-agent after_send observed session %s", ctx.session_key)
        return HookDecision()


def make_hook(ctx: "PluginContext") -> AgentHook | None:
    """Factory the manifest names: Callable[[PluginContext], AgentHook | None].

    Returning None declines the contribution for this run.
    """
    if not ctx.config.get("enabled", True):
        return None
    return FlowHook(ctx)
