"""ActionExecutor — execute the option a user picked from a discovery menu.

One method, ``execute(option, decision, channel, to)``, dispatches by
``option.exec_kind``:

- ``reply`` — submit ``exec_payload.prompt`` as a user-origin TurnRequest
  (``sentinel.action_origin=True``) so AgentLoop processes it normally; the
  agent's reply is what the user ultimately sees.
- ``routine_confirm`` — call ``RoutineStore.upgrade(routine_id, ...)``
  to flip a candidate Routine to active. Optionally adds a CronService
  job when ``exec_payload.make_cron=True``.
- ``tool`` / ``spawn`` — not yet implemented (returns a clear error
  ActionExecutionResult).

Side-effects that mutate persistent state are recorded in
``ActionExecutionResult.side_effects`` for telemetry and
end-of-turn user feedback.
"""

from __future__ import annotations

import time
from datetime import datetime
from typing import TYPE_CHECKING, Any, Callable

from loguru import logger

from raven.proactive_engine.sentinel.types import (
    ActionExecutionResult,
    PendingDecision,
    TaskOption,
)

if TYPE_CHECKING:
    from raven.agent.subagent import SubagentManager
    from raven.agent.tools.registry import ToolRegistry
    from raven.proactive_engine.schedulers.cron.service import CronService
    from raven.proactive_engine.sentinel.predictor.routine_store import RoutineStore


class ActionExecutor:
    """Execute one TaskOption end-to-end."""

    def __init__(
        self,
        *,
        routine_store: "RoutineStore | None" = None,
        cron_service: "CronService | None" = None,
        tool_registry: "ToolRegistry | None" = None,
        subagent_manager: "SubagentManager | None" = None,
        now_fn: Callable[[], datetime] | None = None,
    ) -> None:
        self.routine_store = routine_store
        self.cron_service = cron_service
        self.tool_registry = tool_registry
        self.subagent_manager = subagent_manager
        self._now_fn = now_fn or datetime.now
        # Spine submit, late-bound (the gateway scheduler only exists once the
        # event loop is running; this executor is built in the sync prologue).
        # Wired via set_submit before any dispatch; exec_kind=reply submits a
        # USER-origin turn.
        self._submit: "Callable[[Any], Any] | None" = None

    def set_submit(self, submit: "Callable[[Any], Any] | None") -> None:
        self._submit = submit

    async def execute(
        self,
        option: TaskOption,
        *,
        decision: PendingDecision,
        channel: str | None = None,
        to: str | None = None,
    ) -> ActionExecutionResult:
        """Dispatch by ``exec_kind``. Never raises — failures degrade to
        ``status='error'`` ActionExecutionResult so the caller can render
        a graceful user message."""
        ch = channel or decision.channel
        target = to or decision.to
        started = time.monotonic()
        try:
            if option.exec_kind == "reply":
                return self._finish(
                    started,
                    await self._execute_reply(option, channel=ch, to=target),
                )
            if option.exec_kind == "routine_confirm":
                return self._finish(
                    started,
                    await self._execute_routine_confirm(option, decision=decision),
                )
            if option.exec_kind == "tool":
                return self._finish(
                    started,
                    await self._execute_tool(option, channel=ch, to=target),
                )
            if option.exec_kind == "spawn":
                return self._finish(
                    started,
                    await self._execute_spawn(option, channel=ch, to=target),
                )
            return self._finish(
                started,
                ActionExecutionResult(
                    status="error",
                    exec_kind=option.exec_kind,
                    error=f"unknown exec_kind {option.exec_kind!r}",
                ),
            )
        except Exception as exc:
            logger.exception("ActionExecutor.execute crashed: {}", exc)
            return self._finish(
                started,
                ActionExecutionResult(
                    status="error",
                    exec_kind=option.exec_kind,
                    error=f"{type(exc).__name__}: {exc}",
                ),
            )

    @staticmethod
    def _finish(
        started: float,
        result: ActionExecutionResult,
    ) -> ActionExecutionResult:
        result.elapsed_ms = int((time.monotonic() - started) * 1000)
        return result

    # ------------------------------------------------------------------
    # exec_kind=reply

    async def _execute_reply(self, option: TaskOption, *, channel: str, to: str) -> ActionExecutionResult:
        prompt = (option.exec_payload or {}).get("prompt", "").strip()
        if not prompt:
            return ActionExecutionResult(
                status="error",
                exec_kind="reply",
                error="exec_payload.prompt missing or empty",
            )
        # Inject as the user's intent (filtered through their explicit menu
        # pick): AgentLoop processes it like a normal user request, so
        # Personalizer / after_send run. It carries sentinel.action_origin so
        # Sentinel's own hooks don't re-count it as engagement (the accept was
        # recorded at /pick) and the menu router doesn't re-consume it.
        assert self._submit is not None
        from raven.spine import ChatType, Origin, Source, TurnRequest
        from raven.spine.turn import SentinelExtras

        req = TurnRequest(
            origin=Origin.USER,
            source=Source(channel=channel, chat_id=to, sender_id="user", chat_type=ChatType.DM),
            text=prompt,
            conversation=f"{channel}:{to}",
            sentinel=SentinelExtras(action_origin=True),
        )
        # Fire-and-forget: do NOT await the turn. We run inside the /pick turn's
        # hook (decision_consumer.before_user_inbound), and this turn's
        # conversation is the same user lane — awaiting result() would serialize
        # behind the running /pick turn and self-deadlock. The reply rides
        # emit -> hub -> outlet; the executor's own result is a fixed string,
        # not the turn's output.
        self._submit(req)
        return ActionExecutionResult(
            status="ok",
            exec_kind="reply",
            output_text=f"已为您发起：{option.title}",
            side_effects=[f"injected user prompt ({len(prompt)} chars)"],
        )

    # ------------------------------------------------------------------
    # exec_kind=routine_confirm

    async def _execute_routine_confirm(
        self,
        option: TaskOption,
        *,
        decision: PendingDecision,
    ) -> ActionExecutionResult:
        if self.routine_store is None:
            return ActionExecutionResult(
                status="error",
                exec_kind="routine_confirm",
                error="routine_store not configured",
            )
        payload = option.exec_payload or {}
        routine_id = payload.get("routine_id")
        if not routine_id:
            return ActionExecutionResult(
                status="error",
                exec_kind="routine_confirm",
                error="exec_payload.routine_id missing",
            )

        now_ms = int(self._now_fn().timestamp() * 1000)
        upgraded = self.routine_store.upgrade(
            routine_id,
            confirmed_at_ms=now_ms,
        )
        if not upgraded:
            return ActionExecutionResult(
                status="error",
                exec_kind="routine_confirm",
                error=f"routine {routine_id!r} not found in store",
            )
        side_effects = [f"upgraded routine {routine_id} to active"]

        # Optional cron job creation. Skip if the user disabled cron at
        # the option level OR no cron_service was wired.
        if payload.get("make_cron") and self.cron_service is not None:
            cron_expr = payload.get("cron_expr")
            cron_msg = payload.get("cron_message") or option.title
            if cron_expr:
                try:
                    job = self.cron_service.add_job(
                        name=option.title[:30] or routine_id,
                        schedule=_build_cron_schedule(cron_expr, payload.get("tz")),
                        message=cron_msg,
                        deliver=True,
                        channel=decision.channel,
                        to=decision.to,
                    )
                    side_effects.append(f"created cron job {job.id} ({cron_expr})")
                except Exception as exc:
                    side_effects.append(f"cron job creation failed: {type(exc).__name__}: {exc}")
            else:
                side_effects.append("cron requested but exec_payload.cron_expr missing")

        return ActionExecutionResult(
            status="ok",
            exec_kind="routine_confirm",
            output_text=f"已确认习惯：{option.title}",
            side_effects=side_effects,
        )

    # ------------------------------------------------------------------
    # exec_kind=tool

    async def _execute_tool(self, option: TaskOption, *, channel: str, to: str) -> ActionExecutionResult:
        if self.tool_registry is None:
            return ActionExecutionResult(
                status="error",
                exec_kind="tool",
                error="tool_registry not configured",
            )
        payload = option.exec_payload or {}
        tool_name = payload.get("tool")
        args = payload.get("args") or {}
        if not tool_name:
            return ActionExecutionResult(
                status="error",
                exec_kind="tool",
                error="exec_payload.tool missing",
            )
        if not isinstance(args, dict):
            return ActionExecutionResult(
                status="error",
                exec_kind="tool",
                error="exec_payload.args must be an object",
            )
        if not self.tool_registry.has(tool_name):
            return ActionExecutionResult(
                status="error",
                exec_kind="tool",
                error=f"tool {tool_name!r} not registered",
            )
        # Tools surface their own errors as strings; we don't transmute
        # them into ExecutionResult.error because the user mostly cares
        # about the verbatim tool output.
        try:
            output = await self.tool_registry.execute(tool_name, args)
        except Exception as exc:
            return ActionExecutionResult(
                status="error",
                exec_kind="tool",
                error=f"{type(exc).__name__}: {exc}",
            )
        return ActionExecutionResult(
            status="ok",
            exec_kind="tool",
            output_text=output if output else f"已执行 {tool_name}",
            side_effects=[f"called tool {tool_name}({args})"],
        )

    # ------------------------------------------------------------------
    # exec_kind=spawn

    async def _execute_spawn(self, option: TaskOption, *, channel: str, to: str) -> ActionExecutionResult:
        if self.subagent_manager is None:
            return ActionExecutionResult(
                status="error",
                exec_kind="spawn",
                error="subagent_manager not configured",
            )
        payload = option.exec_payload or {}
        task_description = payload.get("task_description", "").strip()
        if not task_description:
            return ActionExecutionResult(
                status="error",
                exec_kind="spawn",
                error="exec_payload.task_description missing or empty",
            )
        try:
            ack = await self.subagent_manager.spawn(
                task=task_description,
                label=option.title or None,
                origin_channel=channel,
                origin_chat_id=to,
                session_key=f"{channel}:{to}",
            )
        except Exception as exc:
            return ActionExecutionResult(
                status="error",
                exec_kind="spawn",
                error=f"{type(exc).__name__}: {exc}",
            )
        return ActionExecutionResult(
            status="ok",
            exec_kind="spawn",
            output_text=ack or f"已派出后台任务：{option.title}",
            side_effects=[f"spawned subagent for: {task_description[:80]}"],
        )


def _build_cron_schedule(expr: str, tz: str | None):
    """Defer the import so this module doesn't load CronSchedule when
    cron isn't installed (defensive — keep the sentinel module
    self-contained for tests that don't care about cron)."""
    from raven.proactive_engine.schedulers.cron.types import CronSchedule

    return CronSchedule(kind="cron", expr=expr, tz=tz)


__all__ = ["ActionExecutor"]
