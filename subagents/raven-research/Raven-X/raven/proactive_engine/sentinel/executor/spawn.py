"""ProactiveSpawn — executes ``action=spawn_agent`` decisions.

Thin wrapper over ``SubagentManager`` that adds policy gating and proper
session routing. When Planner emits ``action=spawn_agent`` with a
``spawn_task``, ProactiveSpawn:

1. Asks NudgePolicy whether dispatch is allowed (reuses the same quotas
   that gate plain nudges; proactive spawns count against the budget).
2. Parses ``target_session`` into (channel, chat_id) for the subagent's
   return path.
3. Calls ``SubagentManager.spawn(task, origin_channel, origin_chat_id,
   session_key)``. SubagentManager runs the task in the background and
   announces the result by submitting a SUBAGENT-origin turn to the spine
   for the originating session — no additional delivery logic needed here.
4. Records the dispatch in NudgePolicy.

Caller (usually SentinelRunner) should also record to NudgeFeedbackTracker
using the returned ExecutionResult's ``details["task_id"]`` as the nudge id.
"""

from __future__ import annotations

from datetime import datetime
from typing import Callable

from loguru import logger

from raven.agent.subagent import SubagentManager
from raven.proactive_engine.sentinel.executor.dispatcher import ExecutionResult, split_session_key
from raven.proactive_engine.sentinel.trigger_policy.policy import NudgePolicy
from raven.proactive_engine.sentinel.types import PlannerDecision


class ProactiveSpawn:
    """Dispatch ``spawn_agent`` decisions through SubagentManager."""

    def __init__(
        self,
        subagent_manager: SubagentManager,
        policy: NudgePolicy,
        *,
        now_fn: Callable[[], datetime] | None = None,
    ) -> None:
        self.subagent_manager = subagent_manager
        self.policy = policy
        self._now_fn = now_fn or datetime.now

    async def dispatch(self, decision: PlannerDecision) -> ExecutionResult:
        """Gate + spawn.

        Contract: caller supplies only spawn_agent decisions; anything else
        is rejected cheaply (wrong-action).
        """
        if decision.action != "spawn_agent":
            return ExecutionResult(
                delivered=False,
                reason=f"wrong_action:{decision.action}",
            )
        if not decision.spawn_task:
            return ExecutionResult(delivered=False, reason="empty_spawn_task")

        target = decision.target_session or "sentinel:direct"
        # Policy uses spawn_task as the "content" for dedup purposes so
        # identical spawn requests within the window get absorbed.
        check = self.policy.check(
            decision.action,
            target,
            decision.spawn_task,
            decision.priority,
            topic_tag=decision.topic_tag,
        )
        if check.verdict == "deny":
            return ExecutionResult(
                delivered=False,
                reason=f"policy:{check.reason}",
                details={"session_key": target},
            )

        channel, chat_id = split_session_key(target)
        label = self._build_label(decision)

        try:
            task_id = await self.subagent_manager.spawn(
                task=decision.spawn_task,
                label=label,
                origin_channel=channel,
                origin_chat_id=chat_id,
                session_key=target,
            )
        except Exception as exc:
            logger.warning(
                "ProactiveSpawn: SubagentManager.spawn raised {}: {}",
                type(exc).__name__,
                exc,
            )
            return ExecutionResult(
                delivered=False,
                reason=f"spawn_error:{type(exc).__name__}",
                details={"session_key": target, "error": str(exc)[:200]},
            )

        self.policy.record_fired(
            decision.action,
            target,
            decision.spawn_task,
            topic_tag=decision.topic_tag,
        )
        now = self._now_fn()
        logger.info(
            "spawn_dispatched session_key={} priority={} score={:.2f} task_id={} label={!r}",
            target,
            decision.priority,
            decision.proactivity_score,
            task_id,
            label,
        )
        return ExecutionResult(
            delivered=True,
            reason="spawned",
            delivery_time=now,
            details={
                "session_key": target,
                "task_id": task_id,
                "priority": decision.priority,
                "spawn_task": decision.spawn_task,
            },
        )

    @staticmethod
    def _build_label(decision: PlannerDecision) -> str:
        """Short human-readable label for SubagentManager logs / UI."""
        base = (decision.reason or decision.spawn_task or "sentinel")[:40]
        return f"sentinel: {base}".rstrip()


__all__ = ["ProactiveSpawn"]
