"""The pluggable execution contract for a spawned sub-agent."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from contextvars import ContextVar
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    from raven.providers.base import LLMProvider

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


def bounded_delta(
    on_delta: "Callable[[str], Awaitable[None]] | None", limit: int
) -> "Callable[[str], Awaitable[None]] | None":
    """Wrap a delta callback so what streams stays a prefix of what returns.

    The backends that cap a reply truncate their return value to
    ``max_output_chars``. An uncapped stream would render text the record
    never stores and the next turn's history never replays -- the transcript on
    screen would be the only place that text ever existed, and it would vanish
    on the next switch into the instance.

    Returns ``None`` unchanged, so a caller can wrap unconditionally.
    """
    if on_delta is None:
        return None

    remaining = limit

    async def emit(text: str) -> None:
        nonlocal remaining
        if remaining <= 0 or not text:
            return
        chunk = text[:remaining]
        remaining -= len(chunk)
        await on_delta(chunk)

    return emit


class SubagentNoAnswerError(Exception):
    """The run spent its whole round budget and never produced an answer.

    Raised rather than returned so the node fails. It used to fall out of the
    loop and return "Task completed but no final response was generated" as the
    run's *output* -- so a step that had fetched thirty-five pages and written
    nothing was recorded ``completed``, wearing a green tick, and the step
    downstream merged that sentence as if it were the research. A wrong answer
    that announces itself is recoverable; one that reads as done is not.
    """


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

    streams: bool = False
    """Whether this backend reports its reply through ``on_delta`` as it forms.

    A fact about the transport, not about the agent, and so a class attribute
    rather than anything an operator writes: a cli agent prints its transcript
    once at exit and cannot be made to stream by declaring that it does. Same
    rule as ``AgentMeta.live_progress``.

    False by default so a duck-typed backend is read as non-streaming; the
    manager then never wires ``on_delta``, and the caller falls back to
    delivering the return value whole. Nothing else in the stack branches on it.
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
        provider: LLMProvider | None = None,
        model: str | None = None,
        on_delta: Callable[[str], Awaitable[None]] | None = None,
    ) -> str: ...
