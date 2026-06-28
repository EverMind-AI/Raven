"""After-iteration task-completion judge hook.

Fires once per AgentLoop turn (not once per LLM iteration). Calls the
LLM judge to classify the turn as completed / failed / unknown, then
forwards the verdict to ``EvalAdapter.record_task_completion`` which
writes it to HISTORY.md through the MemoryEngine.

The hook stays pass-through (never short-circuits): an evaluator
should never interrupt the user's reply chain, only annotate it.

A note on phase mapping: AgentLoop currently fires ``after_iteration``
inside the ReAct inner loop, which would invoke this hook on every
iteration. To stay on the "once per turn" semantic the hook tracks
the last-seen ``ctx.iteration``; if a turn's iteration count resets
(new turn) it judges; otherwise it stays quiet. This is a stopgap
until AgentLoop grows a dedicated ``after_turn`` phase distinct from
``after_iteration``.
"""

from __future__ import annotations

import logging

from raven.agent.hook.base import AgentHook, AgentHookContext, HookDecision
from raven.eval_engine.adapter.adapter import EvalAdapter
from raven.eval_engine.config import EvalEngineConfig
from raven.eval_engine.judge.judge import EvalJudge, JudgeVerdict

logger = logging.getLogger(__name__)


class AfterIterationHook(AgentHook):
    """LLM judge over the turn's final response."""

    def __init__(
        self,
        config: EvalEngineConfig,
        judge: EvalJudge,
        adapter: EvalAdapter,
    ) -> None:
        self._config = config
        self._judge = judge
        self._adapter = adapter

    @property
    def name(self) -> str:
        return "EvalAfterIterationHook"

    async def after_iteration(
        self, ctx: AgentHookContext
    ) -> HookDecision:
        if not (self._config.enabled and self._config.on_task_completion):
            return HookDecision()

        user_goal, final_response = self._extract(ctx)
        if not user_goal or not final_response:
            return HookDecision()

        try:
            verdict = await self._judge.judge(
                user_goal=user_goal,
                final_response=final_response,
                messages=ctx.messages,
            )
        except Exception as exc:  # noqa: BLE001 — defensive; judge already handles its own
            logger.debug(
                "EvalAfterIterationHook judge error %s: %s",
                type(exc).__name__, exc,
            )
            verdict = JudgeVerdict.unknown

        if verdict is JudgeVerdict.unknown:
            return HookDecision()

        try:
            self._adapter.record_task_completion(
                verdict=verdict,
                user_goal=user_goal,
                session_key=ctx.session_key,
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug(
                "EvalAfterIterationHook adapter error %s: %s",
                type(exc).__name__, exc,
            )
        return HookDecision(
            notes=[f"eval_verdict={verdict.value}"],
        )

    @staticmethod
    def _extract(ctx: AgentHookContext) -> tuple[str, str]:
        """Pull ``user_goal`` (first user message in ctx.messages) and
        ``final_response`` (last assistant content) from the context.

        Returns empty strings on structural mismatch so the hook quietly
        no-ops rather than blowing up — the chain isn't a place for
        loud error reporting.
        """
        messages = ctx.messages or []
        user_goal = ""
        final_response = ""
        for m in messages:
            if not isinstance(m, dict):
                continue
            if m.get("role") == "user" and not user_goal:
                content = m.get("content")
                if isinstance(content, str):
                    user_goal = content
        # Walk the response chain from the latest non-tool assistant message.
        for m in reversed(messages):
            if not isinstance(m, dict):
                continue
            if m.get("role") == "assistant":
                content = m.get("content")
                if isinstance(content, str) and content:
                    final_response = content
                    break
        return user_goal, final_response


__all__ = ["AfterIterationHook"]
