"""The sub-agent backend paper: the surface a nested agent's host calls.

Extracted from the backends package so a third-party backend types against a
paper, not a machine module.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    from raven.contracts.llm_provider import LLMProvider


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

    ``mode`` is the operating profile to run under, for a transport that has
    one. Only ``acp`` does; the others accept it and ignore it, the way they
    already do with ``provider`` / ``model`` -- the manager passes one keyword
    set to whichever backend it resolved, so a backend missing the parameter
    fails the dispatch with a TypeError before the agent is ever contacted.
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
        mcps: list[str] | None = None,
        mcp_grant: Any = None,
        mode: str | None = None,
        on_delta: Callable[[str], Awaitable[None]] | None = None,
    ) -> str: ...


__tier__ = "contract"
__all__ = ["SubagentActionAbortedError", "SubagentBackend", "SubagentNoAnswerError"]
