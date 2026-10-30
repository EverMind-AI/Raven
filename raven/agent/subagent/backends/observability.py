"""One span shape for every external agent, whichever transport ran it.

Both third-party transports record here rather than each writing its own
attributes, because the value of the record is that it is comparable: a
``subagent.external`` span should mean the same thing whether the agent was
reached over ACP or by shelling out, so a viewer or an audit does not need to
know which lane produced it.

The span is opened inside the backend, not by the caller. ``trace`` nests on a
contextvar, so it lands under the ``subagent.run`` of whatever ran the backend --
a ``spawn``'s run, or one attempt of a DAG node -- without either dispatch path
being changed.

The full transcript is an artifact, never an attribute. It is the audit record:
unbounded, and deliberately not subject to ``max_output_chars``, which exists to
protect the model's context -- and none of this enters the model's context.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any

from loguru import logger

from raven.tracing import context as tracing_context
from raven.tracing import spans as tracing_spans
from raven.tracing import trace

SPAN_NAME = "subagent.external"
_PREFIX = "subagent.external"
TRANSCRIPT_KEY = f"{_PREFIX}.transcript"
FRAMES_KEY = f"{_PREFIX}.frames"
STEPS_KEY = f"{_PREFIX}.steps"
"""How many steps a run wrote of itself while it ran; a backfill leaves such a run alone."""


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


_STEP_LIMIT = 400
"""A run that said more than this has already told the reader what it was."""
_PREVIEW_CHARS = 240


def _utc(at: object) -> datetime | None:
    """The collector stamps naive local times; spans are UTC."""
    if not isinstance(at, str) or not at:
        return None
    try:
        parsed = datetime.fromisoformat(at)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.astimezone()
    return parsed.astimezone(timezone.utc)


def _artifact(span: Any, key: str, payload: Any) -> dict[str, Any]:
    identity = span.identity()
    meta = {"traceId": identity["trace_id"], "sessionKey": identity["session_key"]}
    art = tracing_spans.persist_artifact(key, meta, payload, label=key)
    return tracing_spans.artifact_attributes(key, art)


def _step_span(
    span: Any,
    identity: dict[str, Any],
    name: str,
    span_type: str,
    start: datetime,
    end: datetime,
    attributes: dict[str, Any],
    span_id: str,
) -> None:
    tracing_spans.emit(
        tracing_spans.build_span(
            name,
            span_type,
            trace_id=identity["trace_id"],
            span_id=span_id,
            parent_span_id=identity["span_id"],
            session_key=identity["session_key"],
            channel=identity["channel"],
            chat_id=identity["chat_id"],
            surface=identity["surface"],
            start_time=start.isoformat(),
            end_time=max(start, end).isoformat(),
            status_code="ERROR" if attributes.get("tool.error") else "OK",
            status_message=str(attributes.get("tool.error") or ""),
            attributes=attributes,
        )
    )


def record_steps(
    span: Any,
    *,
    prompt: str | None,
    events: list[dict[str, Any]],
    elapsed_ms: int,
    started_at: datetime | None = None,
    ended_at: datetime | None = None,
    id_seed: str | None = None,
    skip_ids: set[str] | None = None,
    dry_run: bool = False,
) -> list[str]:
    """The run's own conversation, one span per step.

    Every tool call and every message the agent reported is a step of this run,
    not of the caller's turn: the timeline that shows the worker should show
    what it said and ran, where the caller only ever sees the answer. The
    collector has kept them in order, with the moment each arrived -- so each
    becomes a child span of the run, shaped exactly like the spans the main
    loop writes (``llm.call`` / ``tool.call`` with their artifacts), and the
    trajectory layer reads them with the same code.

    A step ends when the next one starts; the last one ends with the run.
    ``ended_at`` is that end when it is already known -- a backfill replaying a
    run long after it closed -- and every step is held inside it. A live run
    ends now. The first step, the prompt, starts when the run's span opened:
    ``elapsed_ms`` is timed from before that (the caller resolves its MCP grant
    first), and only stands in for a span that cannot say.

    A step's artifacts are written with the step: a dry run, and a step whose
    id is already on record (``skip_ids``), leave no file behind.

    Nothing here may fail a run: the caller's answer is already on its way.
    """
    emitted: list[str] = []
    try:
        identity = span.identity()
        if not identity.get("trace_id") or not identity.get("span_id"):
            return emitted
        close = ended_at or datetime.now(timezone.utc)
        floor = started_at or _utc(identity.get("start")) or close - timedelta(milliseconds=max(0, elapsed_ms))
        ats = [_utc(event.get("at")) for event in events]
        results = {event.get("id"): event for event in events if event.get("t") == "result"}
        steps: list[tuple[str, str, datetime, datetime, list[tuple[str, Any]], dict[str, Any]]] = []

        def span_end(index: int) -> datetime:
            for later in ats[index + 1 :]:
                if later is not None:
                    return later
            return close

        if prompt:
            first = next((at for at in ats if at is not None), close)
            steps.append(
                (
                    "llm.call",
                    "model",
                    floor,
                    first,
                    [("llm.input", {"messages": [{"role": "user", "content": prompt}]})],
                    {"llm.input_preview": prompt[:_PREVIEW_CHARS]},
                )
            )
        for index, event in enumerate(events):
            if len(steps) >= _STEP_LIMIT:
                break
            kind = event.get("t")
            at = ats[index] or (steps[-1][3] if steps else floor)
            end = span_end(index)
            if kind == "say":
                text = str(event.get("text") or "")
                steps.append(
                    (
                        "llm.call",
                        "model",
                        at,
                        end,
                        [("llm.output", {"content": text})],
                        {"llm.output_preview": text[:_PREVIEW_CHARS]},
                    )
                )
            elif kind == "thought":
                text = str(event.get("text") or "")
                steps.append(
                    (
                        "llm.call",
                        "model",
                        at,
                        end,
                        [("llm.output", {"content": "", "reasoning_content": text})],
                        {"llm.output_preview": text[:_PREVIEW_CHARS]},
                    )
                )
            elif kind == "user":
                text = str(event.get("text") or "")
                steps.append(
                    (
                        "llm.call",
                        "model",
                        at,
                        end,
                        [("llm.input", {"messages": [{"role": "user", "content": text}]})],
                        {"llm.input_preview": text[:_PREVIEW_CHARS]},
                    )
                )
            elif kind == "call":
                call = event.get("call")
                name = str(getattr(call, "name", None) or "tool")
                try:
                    arguments = call.arguments_json() if call is not None else "{}"
                except Exception:  # noqa: BLE001 — a call we cannot read is still a row
                    arguments = "{}"
                artifacts: list[tuple[str, Any]] = [("tool.input", {"name": name, "params": arguments or "{}"})]
                attributes: dict[str, Any] = {
                    "tool.args_preview": (arguments or "")[:_PREVIEW_CHARS],
                    "tool.name": name,
                }
                result = results.get(event.get("id"))
                if result is not None:
                    text = str(result.get("text") or "")
                    artifacts.append(("tool.output", {"result": text, "ok": result.get("ok")}))
                    attributes["tool.result_preview"] = text[:_PREVIEW_CHARS]
                    if result.get("ok") is False:
                        # Marked the way the main loop marks a failed call, so the
                        # trajectory reads it as one: the artifact's `ok` is not
                        # something it looks at.
                        attributes["tool.error"] = text[:_PREVIEW_CHARS] or "failed"
                    end = max(end, _utc(result.get("at")) or end)
                steps.append(("tool.call", "tool", at, end, artifacts, attributes))
        for index, (name, span_type, start, end, artifacts, attributes) in enumerate(steps):
            # Inside the run, both ends: a journal keeps naive local times, and
            # one read in another zone, or across a clock step, can fall outside.
            start = max(start, floor)
            if ended_at is not None:
                start = min(start, ended_at)
                end = min(end, ended_at)
            span_id = f"span-{id_seed}-{index}" if id_seed else tracing_context.new_span_id()
            if skip_ids is not None and span_id in skip_ids:
                continue
            emitted.append(span_id)
            if dry_run:
                continue
            for key, payload in artifacts:
                attributes.update(_artifact(span, key, payload))
            _step_span(span, identity, name, span_type, start, end, attributes, span_id)
    except Exception:  # noqa: BLE001 — an audit trail must not take down the run
        logger.debug("tracing: record_steps failed", exc_info=True)
    return emitted


__all__ = [
    "FRAMES_KEY",
    "SPAN_NAME",
    "STEPS_KEY",
    "TRANSCRIPT_KEY",
    "external_agent_span",
    "record_events",
    "record_outcome",
    "record_frames",
    "record_session",
    "record_steps",
    "record_transcript",
]
