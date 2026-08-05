"""The pluggable execution contract for a spawned sub-agent."""

from __future__ import annotations

from contextvars import ContextVar
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

IN_SUBAGENT_RUN: ContextVar[bool] = ContextVar("raven_in_subagent_run", default=False)
"""True while an in-process backend is executing a sub-agent task.

Orchestration tools (``run_subagent_dag``) read this to refuse re-entry: a
sub-agent must not fan out another graph of sub-agents. Only in-process
backends can set it — an out-of-process CLI/HTTP agent runs its own tools
beyond Raven's reach, so the flag makes no claim about those."""


ABORTED_ACTION_RESULT = (
    "The subtask stopped because a safety decision terminated the requested operation. "
    "No alternative method was attempted."
)


class SubagentActionAbortedError(Exception):
    """A safety decision terminated the operation the sub-agent asked for.

    Raised instead of returning so the run unwinds at once: handing the refusal
    back to the model would let it translate a rejected operation into another
    command or interpreter, and finishing the batch would execute the siblings
    it already proposed. The manager announces ``ABORTED_ACTION_RESULT`` for
    this rather than an error string built from the exception -- the run
    stopped on purpose, and the text is what the main agent must read.
    """


@runtime_checkable
class SubagentBackend(Protocol):
    """How a spawned sub-agent actually executes a task.

    ``run`` returns the sub-agent's final text result. It raises on failure —
    the manager catches it and announces an error turn. Progress/announcement,
    the spawn semaphore, and rate limiting are the manager's concern, not the
    backend's.
    """

    async def run(
        self,
        task: str,
        *,
        task_id: str,
        workspace: Path,
        executor: Any,
        session_key: str | None = None,
        instance: str | None = None,
    ) -> str: ...
