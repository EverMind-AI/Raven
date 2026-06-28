"""Heartbeat service - periodic agent wake-up to check for tasks."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Coroutine

from loguru import logger

if TYPE_CHECKING:
    from raven.proactive_engine.system_events import SystemEvent, SystemEventQueue
    from raven.proactive_engine.wake import WakeScheduler
    from raven.providers.base import LLMProvider

_HEARTBEAT_TOOL = [
    {
        "type": "function",
        "function": {
            "name": "heartbeat",
            "description": "Report heartbeat decision after reviewing tasks.",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["skip", "run"],
                        "description": "skip = nothing to do, run = has active tasks",
                    },
                    "tasks": {
                        "type": "string",
                        "description": "Natural-language summary of active tasks (required for run)",
                    },
                },
                "required": ["action"],
            },
        },
    }
]


class HeartbeatService:
    """
    Periodic heartbeat service that wakes the agent to check for tasks.

    Phase 1 (decision): reads HEARTBEAT.md and asks the LLM — via a virtual
    tool call — whether there are active tasks.  This avoids free-text parsing
    and the unreliable HEARTBEAT_OK token.

    Phase 2 (execution): only triggered when Phase 1 returns ``run``.  The
    ``on_execute`` callback runs the task through the full agent loop and
    returns the result to deliver.

    Event wake (optional): when constructed with a ``WakeScheduler`` and a
    ``SystemEventQueue``, the loop sleeps on the scheduler's wake event
    instead of a bare timer, so producers (cron completions, subagent
    completions) can end the sleep early.  Pending system events are fed
    into the Phase 1 prompt and only acknowledged after the tick finishes —
    a failed tick leaves them queued for the next attempt.  Without these
    two collaborators the service behaves exactly like the plain
    interval-only version.
    """

    def __init__(
        self,
        workspace: Path,
        provider: LLMProvider,
        model: str,
        on_execute: Callable[[str], Coroutine[Any, Any, str]] | None = None,
        on_notify: Callable[[str], Coroutine[Any, Any, None]] | None = None,
        interval_s: int = 30 * 60,
        enabled: bool = True,
        wake: WakeScheduler | None = None,
        system_events: SystemEventQueue | None = None,
    ):
        self.workspace = workspace
        self.provider = provider
        self.model = model
        self.on_execute = on_execute
        self.on_notify = on_notify
        self.interval_s = interval_s
        self.enabled = enabled
        self._wake = wake
        self._system_events = system_events
        self._running = False
        self._task: asyncio.Task | None = None

    @property
    def heartbeat_file(self) -> Path:
        return self.workspace / "HEARTBEAT.md"

    def _read_heartbeat_file(self) -> str | None:
        if self.heartbeat_file.exists():
            try:
                return self.heartbeat_file.read_text(encoding="utf-8")
            except Exception:
                return None
        return None

    def _peek_events(self) -> list[SystemEvent]:
        return self._system_events.peek_all() if self._system_events else []

    def _ack_events(self, events: list[SystemEvent]) -> None:
        if events and self._system_events:
            self._system_events.ack(events)

    async def _decide(
        self, content: str, events: list[SystemEvent] | None = None
    ) -> tuple[str, str]:
        """Phase 1: ask LLM to decide skip/run via virtual tool call.

        Returns (action, tasks) where action is 'skip' or 'run'.
        """
        user_msg = (
            "Review the following HEARTBEAT.md and decide whether there are active tasks.\n\n"
            f"{content or '(HEARTBEAT.md is empty)'}"
        )
        if events:
            lines = "\n".join(f"- [{e.source}] {e.text}" for e in events)
            user_msg += (
                "\n\n## System events (just happened)\n"
                f"{lines}\n\n"
                "If any event warrants a follow-up action or a user-visible "
                "notice, choose 'run' and describe it in `tasks`. If the "
                "events need no action, choose 'skip'."
            )
        response = await self.provider.chat_with_retry(
            messages=[
                {"role": "system", "content": "You are a heartbeat agent. Call the heartbeat tool to report your decision."},
                {"role": "user", "content": user_msg},
            ],
            tools=_HEARTBEAT_TOOL,
            model=self.model,
        )

        if not response.has_tool_calls:
            return "skip", ""

        args = response.tool_calls[0].arguments
        return args.get("action", "skip"), args.get("tasks", "")

    async def start(self) -> None:
        """Start the heartbeat service."""
        if not self.enabled:
            logger.info("Heartbeat disabled")
            return
        if self._running:
            logger.warning("Heartbeat already running")
            return

        self._running = True
        self._task = asyncio.create_task(self._run_loop())
        logger.info(
            "Heartbeat started (every {}s{})",
            self.interval_s,
            ", event wake enabled" if self._wake else "",
        )

    def stop(self) -> None:
        """Stop the heartbeat service."""
        self._running = False
        if self._wake:
            self._wake.stop()
        if self._task:
            self._task.cancel()
            self._task = None

    async def _sleep_until_wake(self, timeout: float) -> str:
        """Wait for the next tick. Returns the wake reason ("interval" when
        the timer ran out, otherwise the comma-joined producer reasons)."""
        if self._wake is None:
            await asyncio.sleep(timeout)
            return "interval"
        try:
            await asyncio.wait_for(self._wake.wake_event.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            # Don't clear here: a set() racing the timeout stays set, so the
            # next loop iteration wakes immediately with its reasons intact.
            return "interval"
        self._wake.wake_event.clear()
        return ",".join(self._wake.consume_reasons()) or "wake"

    async def _run_loop(self) -> None:
        """Main heartbeat loop."""
        next_tick_at = time.monotonic() + self.interval_s
        while self._running:
            try:
                timeout = max(0.0, next_tick_at - time.monotonic())
                reason = await self._sleep_until_wake(timeout)
                if not self._running:
                    break

                events = self._peek_events()
                if reason != "interval" and not events:
                    # Spurious wake (producer raced an in-flight tick that
                    # already drained its event) — skip without an LLM call.
                    logger.debug("Heartbeat: spurious wake, no pending events")
                    continue

                await self._tick(reason=reason, events=events)
                # Ack only after a successful tick — a failed tick leaves the
                # events queued so the next wake or interval retries them.
                self._ack_events(events)
                # Any tick counts as "looked at the world": re-anchor the
                # interval so a wake tick isn't followed by an immediate
                # interval tick.
                next_tick_at = time.monotonic() + self.interval_s
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("Heartbeat error: {}", e)
                next_tick_at = time.monotonic() + self.interval_s

    async def _tick(
        self, reason: str = "interval", events: list[SystemEvent] | None = None
    ) -> None:
        """Execute a single heartbeat tick.

        Raises on failure so the caller can decide whether to ack events.
        """
        events = events or []
        content = self._read_heartbeat_file()
        if not content and not events:
            logger.debug("Heartbeat: HEARTBEAT.md missing or empty")
            return

        logger.info("Heartbeat: checking for tasks (reason: {})...", reason)

        action, tasks = await self._decide(content or "", events)

        if action != "run":
            logger.info("Heartbeat: OK (nothing to report)")
            return

        logger.info("Heartbeat: tasks found, executing...")
        if self.on_execute:
            response = await self.on_execute(tasks)
            if response and self.on_notify:
                logger.info("Heartbeat: completed, delivering response")
                await self.on_notify(response)

    async def trigger_now(self) -> str | None:
        """Manually trigger a heartbeat."""
        content = self._read_heartbeat_file()
        events = self._peek_events()
        if not content and not events:
            return None
        action, tasks = await self._decide(content or "", events)
        if action != "run" or not self.on_execute:
            # Decision was made (skip): the events were seen and judged.
            self._ack_events(events)
            return None
        result = await self.on_execute(tasks)
        self._ack_events(events)
        return result
