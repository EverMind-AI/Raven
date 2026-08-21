"""One span shape for every external agent, whichever transport ran it.

Both third-party transports record here rather than each writing its own
attributes, because the value of the record is that it is comparable: a
``subagent.external`` span should mean the same thing whether the agent was
reached over ACP or by shelling out, so a viewer or an audit does not need to
know which lane produced it.

The span is opened inside the backend, not by the caller. ``trace`` nests on a
contextvar, so it lands under ``subagent.run`` for a ``spawn`` and under
``tool.call`` for a DAG node without either dispatch path being changed -- and a
DAG node has no span of its own at all, so this is the only observability it has.

The full transcript is an artifact, never an attribute. It is the audit record:
unbounded, and deliberately not subject to ``max_output_chars``, which exists to
protect the model's context -- and none of this enters the model's context.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from raven.tracing import trace

SPAN_NAME = "subagent.external"
_PREFIX = "subagent.external"
TRANSCRIPT_KEY = f"{_PREFIX}.transcript"
FRAMES_KEY = f"{_PREFIX}.frames"


@contextmanager
def external_agent_span(
    *,
    agent: str,
    transport: str,
    task_id: str,
    instance: str,
) -> Iterator[Any]:
    """Open the span for one external-agent run."""
    with trace.span(
        SPAN_NAME,
        kind="subagent",
        **{
            f"{_PREFIX}.agent": agent,
            f"{_PREFIX}.transport": transport,
            f"{_PREFIX}.task_id": task_id,
            f"{_PREFIX}.instance": instance,
        },
    ) as span:
        yield span


def record_session(span: Any, *, session_id: str | None, resumed: bool) -> None:
    span.set(**{f"{_PREFIX}.session_id": session_id, f"{_PREFIX}.resumed": resumed})


def record_outcome(
    span: Any,
    *,
    answer_chars: int,
    elapsed_ms: int,
    stop_reason: Any = None,
    update_counts: dict[str, int] | None = None,
    tool_calls: list[str] | None = None,
    thought_chars: int = 0,
    usage: dict[str, Any] | None = None,
    exit_code: int | None = None,
) -> None:
    """Write one run's outcome.

    Every field is optional except the two both transports can always answer, so
    a lane that cannot report something leaves it absent rather than reporting a
    zero that reads as "none happened". That distinction is the point for the cli
    lane: it has no per-step visibility at all, which is why its rows are tagged
    ``no-progress`` in the roster -- an empty timeline there is a property of the
    transport, not a quiet run.
    """
    attrs: dict[str, Any] = {
        f"{_PREFIX}.answer_chars": answer_chars,
        f"{_PREFIX}.elapsed_ms": elapsed_ms,
    }
    if stop_reason is not None:
        attrs[f"{_PREFIX}.stop_reason"] = stop_reason
    if exit_code is not None:
        attrs[f"{_PREFIX}.exit_code"] = exit_code
    if update_counts is not None:
        attrs[f"{_PREFIX}.update_counts"] = update_counts
    if tool_calls is not None:
        attrs[f"{_PREFIX}.tool_calls"] = tool_calls
        attrs[f"{_PREFIX}.tool_call_count"] = len(tool_calls)
    if thought_chars:
        attrs[f"{_PREFIX}.thought_chars"] = thought_chars
    if usage:
        attrs[f"{_PREFIX}.usage"] = usage
    span.set(**attrs)


def record_events(span: Any, *, transport: str, kinds: dict[str, int]) -> None:
    """One event per distinct step kind, so a timeline reads without the artifact."""
    for kind in kinds:
        span.event(f"{transport}.{kind}")


def record_transcript(span: Any, payload: dict[str, Any]) -> None:
    """Persist the run's raw material out of line.

    The cli lane's only way to keep what it saw: it reconstructs a run from
    whatever the command printed, so the payload *is* the evidence. The acp lane
    uses :func:`record_frames` instead, because its evidence is already a file.
    """
    span.artifact(TRANSCRIPT_KEY, payload)


def record_frames(span: Any, frames: dict[str, Any] | None) -> None:
    """Point at the run's own stretch of its connection's wire journal.

    Attributes rather than an artifact: the frames are already a file, written as
    they crossed the wire, so copying the same bytes into a second per-call file
    would double the audit trail's size and give two places for it to disagree.
    """
    if not frames:
        return
    span.set(**{f"{FRAMES_KEY}.{key}": value for key, value in frames.items()})


__all__ = [
    "FRAMES_KEY",
    "SPAN_NAME",
    "TRANSCRIPT_KEY",
    "external_agent_span",
    "record_events",
    "record_outcome",
    "record_frames",
    "record_session",
    "record_transcript",
]
