"""Writing an old external run's steps back into the trace log.

An external ACP run recorded before spans carried its steps left only a pointer
at its wire journal (``subagent.external.frames.*``). The Replay block reads
that journal on demand for one entry; this walk turns the same journal into the
spans the run should have written, so the timeline shows its prompt, messages
and tool calls as the rows Raven's own runs get.

Idempotent by construction: every synthetic span id is derived from the run's
own span id, so a second pass finds them all and writes nothing -- except a
step an earlier pass left running past its run's end, which it writes again
closed at that end. The index keeps the later record of a span, so the
correction supersedes the old one without rewriting the log.
"""

from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from raven.agent.subagent.backends import observability
from raven.tracing import spans as tracing_spans
from raven.trajectory import frames_replay
from raven.trajectory import store as tstore

FRAMES_KEY = "subagent.external.frames."

# What `record_steps` names a backfilled step: the run's seed and the step's place.
_STEP_ID = re.compile(r"^span-([0-9a-f]{10})-\d+$")


class _Handle:
    """The identity a synthesized span needs, read from the run's own record."""

    def __init__(self, identity: dict[str, Any]) -> None:
        self._identity = identity

    def identity(self) -> dict[str, Any]:
        return self._identity


def _int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _stamp(span: dict[str, Any], key: str) -> datetime | None:
    raw = span.get(key)
    if not isinstance(raw, str) or not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _identity(span: dict[str, Any], attrs: dict[str, Any]) -> dict[str, Any]:
    return {
        "trace_id": span.get("traceId"),
        "span_id": span.get("spanId"),
        "session_key": attrs.get("session.key"),
        "channel": attrs.get("channel"),
        "chat_id": attrs.get("chat_id"),
        "surface": attrs.get("surface"),
    }


def _seed(span_id: Any) -> str:
    return hashlib.sha1(str(span_id).encode("utf-8")).hexdigest()[:10]


def backfill(
    state_dir: Path | None = None,
    *,
    session_key: str | None = None,
    dry_run: bool = False,
) -> dict[str, int]:
    """Write every replayable run's steps as spans.

    Returns ``runs`` (external runs with a journal still on disk), ``written``
    (runs this pass added spans for), ``steps`` (spans added), ``already``
    (runs whose steps were all on record) and ``repaired`` (steps on record
    that ended after their run and were closed at its end), so a dry run
    reports the same numbers a real one would write.
    """
    existing: set[str] = set()
    steps_on_record: dict[str, dict[str, Any]] = {}
    run_ends: dict[str, datetime] = {}
    runs: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for span in tstore.iter_spans(state_dir, session_key=session_key):
        span_id = span.get("spanId")
        if isinstance(span_id, str):
            existing.add(span_id)
            if _STEP_ID.match(span_id):
                steps_on_record[span_id] = span
        if span.get("name") != observability.SPAN_NAME:
            continue
        ended = _stamp(span, "endTime")
        if isinstance(span_id, str) and ended is not None:
            run_ends[_seed(span_id)] = ended
        attrs = tstore._span_attrs(span)
        path = attrs.get(FRAMES_KEY + "path")
        if isinstance(path, str) and path and Path(path).exists():
            runs.append((span, attrs))

    written = steps = 0
    for span, attrs in runs:
        session_id = attrs.get(FRAMES_KEY + "session_id")
        prompt, events = frames_replay.events(
            attrs[FRAMES_KEY + "path"],
            session_id=session_id if isinstance(session_id, str) else None,
            start=_int(attrs.get(FRAMES_KEY + "start")),
            end=_int(attrs.get(FRAMES_KEY + "end")),
        )
        if prompt is None and not events:
            continue
        added = observability.record_steps(
            _Handle(_identity(span, attrs)),
            prompt=prompt,
            events=events,
            elapsed_ms=0,
            started_at=_stamp(span, "startTime"),
            ended_at=_stamp(span, "endTime"),
            id_seed=_seed(span.get("spanId")),
            skip_ids=existing,
            dry_run=dry_run,
        )
        if added:
            written += 1
            steps += len(added)
            existing.update(added)

    repaired = 0
    for span_id, record in steps_on_record.items():
        match = _STEP_ID.match(span_id)
        run_end = run_ends.get(match.group(1)) if match else None
        closed = _stamp(record, "endTime")
        if run_end is None or closed is None or closed <= run_end:
            continue
        repaired += 1
        if not dry_run:
            opened = _stamp(record, "startTime")
            tracing_spans.emit({**record, "endTime": max(opened or run_end, run_end).isoformat()})

    return {
        "runs": len(runs),
        "written": written,
        "steps": steps,
        "already": len(runs) - written,
        "repaired": repaired,
    }


__all__ = ["FRAMES_KEY", "backfill"]
