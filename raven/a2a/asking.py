"""The A2A transport's question broker.

``QuestionResponder`` is structural, and each transport brings its own: the TUI
has one, the gateway has one, this is A2A's. Nothing in ``AskUserTool`` changes.

The turn is never suspended. It awaits a future here while its task is reported
as ``INPUT_REQUIRED``, and a later ``SendMessage`` against the same task id
resolves that future -- so what looks like resuming a task is answering a turn
that never stopped running.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

from loguru import logger


class A2aQuestionBroker:
    """Puts a turn's question to an A2A caller by parking the task."""

    def __init__(self, on_park: Callable[[str], None]) -> None:
        self._on_park = on_park
        self._waiting: dict[str, asyncio.Future[str]] = {}

    async def await_question(
        self,
        conversation_id: str,
        *,
        prompt: str,
        choices: list[str] | None = None,
        default: str = "",
        timeout_s: float | None = None,
        header: str = "",
        recommended: str = "",
        index: int = 0,
        total: int = 1,
        batch: list[dict[str, Any]] | None = None,
    ) -> str:
        """Park `conversation_id`'s task and wait for the caller's next message.

        `choices`, `header`, `recommended`, `index`, `total`, and `batch` describe
        richer question shapes (multiple choice, batched sub-questions, a
        recommended default among choices) that a text-only A2A caller cannot
        render beyond `prompt` itself; they are accepted to satisfy
        `QuestionResponder` but do not change how this transport asks.
        """
        loop = asyncio.get_running_loop()
        future: asyncio.Future[str] = loop.create_future()
        self._waiting[conversation_id] = future
        self._on_park(conversation_id)
        try:
            return await asyncio.wait_for(future, timeout=timeout_s)
        except (TimeoutError, asyncio.TimeoutError):
            logger.info("a2a question on task {} timed out; using the default", conversation_id)
            return default
        finally:
            self._waiting.pop(conversation_id, None)

    def answer(self, task_id: str, text: str) -> bool:
        """Resolve the question a turn is waiting on. False if nothing was waiting."""
        future = self._waiting.get(task_id)
        if future is None or future.done():
            return False
        future.set_result(text)
        return True
