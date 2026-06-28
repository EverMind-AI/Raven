"""Tool-call audit hook.

Default policy: a deterministic deny-list. When ``config.on_tool_audit``
is on, any tool call whose name appears in ``config.tool_denylist``
short-circuits the iteration with an explanatory message. A future
expansion can fall through to an LLM safety check (see
``prompts/tool_safety.py``); the scaffold for that lives but is
unwired until there's a concrete need.

The hook inspects ``ctx.response`` for ``tool_calls`` because
``before_execute_tools`` fires AFTER the LLM has produced its turn's
tool-call list, which ``AgentHookContext`` threads through.
"""

from __future__ import annotations

import logging
from typing import Any

from raven.agent.hook.base import AgentHook, AgentHookContext, HookDecision
from raven.eval_engine.config import EvalEngineConfig

logger = logging.getLogger(__name__)


class ToolAuditHook(AgentHook):
    """Deny-list-based tool audit (deterministic, no LLM)."""

    def __init__(self, config: EvalEngineConfig) -> None:
        self._config = config

    @property
    def name(self) -> str:
        return "EvalToolAuditHook"

    async def before_execute_tools(
        self, ctx: AgentHookContext
    ) -> HookDecision:
        if not (self._config.enabled and self._config.on_tool_audit):
            return HookDecision()
        denylist = set(self._config.tool_denylist)
        if not denylist:
            return HookDecision()

        offenders = _extract_offending_tool_names(ctx.response, denylist)
        if not offenders:
            return HookDecision()

        logger.warning(
            "EvalEngine tool audit: blocking tool calls %s",
            sorted(offenders),
        )
        return HookDecision(
            short_circuit_result=(
                "I tried to invoke a tool that's been blocked by policy: "
                f"{', '.join(sorted(offenders))}. "
                "Please rephrase or escalate if you believe this is intended."
            ),
            notes=[f"tool_denylist_hit names={sorted(offenders)}"],
        )


def _extract_offending_tool_names(response: Any, denylist: set[str]) -> set[str]:
    """Walk ``response.tool_calls`` (LLMResponse) or
    ``response["tool_calls"]`` (dict) and return any names that
    appear in ``denylist``.

    Returns an empty set on any structural mismatch so a malformed
    response doesn't crash the hook.
    """
    if response is None:
        return set()
    tool_calls = getattr(response, "tool_calls", None)
    if tool_calls is None and isinstance(response, dict):
        tool_calls = response.get("tool_calls")
    if not tool_calls:
        return set()

    offenders: set[str] = set()
    for tc in tool_calls:
        name = (
            getattr(tc, "name", None)
            or (tc.get("name") if isinstance(tc, dict) else None)
            or (
                tc.get("function", {}).get("name")
                if isinstance(tc, dict) else None
            )
        )
        if isinstance(name, str) and name in denylist:
            offenders.add(name)
    return offenders


__all__ = ["ToolAuditHook"]
