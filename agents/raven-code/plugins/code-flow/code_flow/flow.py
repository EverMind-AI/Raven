"""The turn-frame hook: the session key, captured for the gate's identity.

The fork's gate learned which conversation a call belonged to through a cid
hook the fork loop wired (fork loop/main.py:1684-1689, reading the ask_user
tool's conversation id). On trunk the identity rides the hook chain instead:
this hook copies ``ctx.session_key`` into the plugin's own ContextVar at the
phases that open a turn, in the same task the registry later dispatches tools
from, and the gate reads it back (code_flow.gate.CURRENT_SESSION_KEY).

One contributed hook row on purpose: the fork's other loop-side axes
(completion-gate injection, time budget, todo re-render) board this same hook
in later product waves, and the registry serves hook names sorted -- the axis
order will be part of this class, not of the manifest.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from loguru import logger
from pydantic import ValidationError

from code_flow.config import FlowConfig
from code_flow.gate import bind_session_key
from raven.contracts.loop_hooks import AgentHook, AgentHookContext, HookDecision

if TYPE_CHECKING:
    from raven.plugins.context import PluginContext


class CodeFlowHook(AgentHook):
    """Captures the turn's session key for the write gate; no-op otherwise."""

    @property
    def name(self) -> str:
        return "code_flow"

    async def before_user_inbound(self, ctx: AgentHookContext) -> HookDecision:
        bind_session_key(ctx.session_key)
        return HookDecision()

    async def before_iteration(self, ctx: AgentHookContext) -> HookDecision:
        # Re-stamped every iteration: an internally-initiated turn (a wake, a
        # cron fanout) never fires the inbound phase, and the latest turn in
        # this task must always win.
        bind_session_key(ctx.session_key)
        return HookDecision()


def make_flow_hook(ctx: "PluginContext") -> CodeFlowHook | None:
    """Factory for the ``code_flow`` hook contribution.

    Declines (returns None) when the slice leaves the flow off: a disabled
    product casts no surface at all (D6). A slice that does not parse
    declines too -- the fail-closed seat on that error is the gate's
    (code_flow.gate.MisconfiguredGate), not this hook's: an absent capture
    hook only degrades the gate's identity to the unkeyed fallback.
    """
    try:
        cfg = FlowConfig.from_slice(dict(ctx.config or {}))
    except ValidationError as exc:
        logger.warning("code-flow: config slice is malformed; declining the hook: {}", exc)
        return None
    if not cfg.enabled:
        return None
    return CodeFlowHook()


__all__ = ["CodeFlowHook", "make_flow_hook"]
