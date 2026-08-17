"""Refusal re-roll observer — opt-in, never in the default chain.

In rollout/eval harnesses a terse refusal to a benign research task is
usually a decode artifact worth one re-roll. In interactive product use
a refusal can be the *correct* answer (safety, capability honesty), so
this observer must be explicitly registered by the harness that wants
it via ``loop_observers=[...]`` — wiring it by default would override
legitimate refusals.
"""

from __future__ import annotations

import logging
import re

from raven.agent.hook.base import AgentHook, AgentHookContext, HookDecision

logger = logging.getLogger(__name__)

_THINK_BLOCK_RE = re.compile(r"<think>.*?</think>", re.S)

_DEFAULT_PATTERNS = (
    "i can't",
    "i cannot",
    "i'm unable",
    "i am unable",
    "i won't be able",
    "我无法",
    "无法完成",
    "无法帮助",
    "不能帮助",
    "抱歉,我不能",
)


class RefusalObserver(AgentHook):
    """One bounded re-roll when the candidate final answer is a terse
    refusal: short (``max_chars``), no tool calls, matches a refusal
    pattern. Past the budget the refusal stands."""

    def __init__(
        self,
        max_rollbacks: int = 1,
        retry_temperature: float = 1.0,
        max_chars: int = 500,
        patterns: tuple[str, ...] = _DEFAULT_PATTERNS,
    ) -> None:
        self._max_rollbacks = max_rollbacks
        self._retry_temperature = retry_temperature
        self._max_chars = max_chars
        self._patterns = tuple(p.lower() for p in patterns)

    @property
    def name(self) -> str:
        return "RefusalObserver"

    async def after_iteration(self, ctx: AgentHookContext) -> HookDecision:
        if getattr(ctx.response, "has_tool_calls", False):
            return HookDecision()
        content = getattr(ctx.response, "content", None)
        if content is None and isinstance(ctx.response, dict):
            content = ctx.response.get("content")
        if not isinstance(content, str) or not content:
            return HookDecision()

        answer = _THINK_BLOCK_RE.sub("", content).strip()
        if not answer or len(answer) > self._max_chars:
            return HookDecision()
        lowered = answer.lower()
        if not any(p in lowered for p in self._patterns):
            return HookDecision()

        state = ctx.metadata.setdefault("refusal", {"rollbacks": 0})
        if state["rollbacks"] >= self._max_rollbacks:
            return HookDecision(notes=["refusal_stands"])
        state["rollbacks"] += 1
        logger.warning(
            "refusal: terse refusal candidate (%d chars); rollback %d/%d",
            len(answer),
            state["rollbacks"],
            self._max_rollbacks,
        )
        return HookDecision(
            rollback=True,
            rollback_overrides={"temperature": self._retry_temperature},
            notes=[f"refusal_reroll {state['rollbacks']}/{self._max_rollbacks}"],
        )


__all__ = ["RefusalObserver"]
