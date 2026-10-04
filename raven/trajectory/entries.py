"""Project logical spans into stable trajectory entries.

Three layers are kept apart here. A *physical record* is one line of a span
log; a *logical span* is ``traceId + spanId`` with its checkpoints merged
(last write wins, except that an in-progress snapshot never overrides a
terminal one); a *trajectory entry* is one row of the trajectory view, and a
span expands into several (``llm.input``, ``llm.thinking``, ``llm.output``,
...). :func:`project_entries` takes an already-selected span collection and
returns ordered entries; it never scans directories or decides session
membership, and with ``read=False`` it performs no file I/O at all.

Per-span expansion is shared with the CLI preview through
:func:`raven.trajectory.conversation.span_records`, so both views see the same
slots, the same degradations and the same v2 message resolution. On top of
that this module adds what the Web view needs and the text preview does not:

- a stable identity ``trace:span:slot`` (no sequence number, no text hash);
- the owning operation's status with its evidence, derived from raw span
  fields, attributes and structured payloads -- never from display text;
- data-integrity codes kept separate from status;
- a Timing Owner per span: the single entry charged the span's whole
  ``end - start`` duration, so a span's time is counted at most once;
- a summary entry for base spans that expand to nothing, and an error entry
  for a failed span that has no completion slot, so every attributable
  record has a row and every failure has exactly one carrier.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence

from raven.trajectory import conversation as _conv

POSITION_KEY = "__position__"
PREVIEW_LIMIT = 200

STATUS_RUNNING = "running"
STATUS_OK = "ok"
STATUS_ERROR = "error"
STATUS_CANCELLED = "cancelled"
STATUS_UNKNOWN = "unknown"

BASIS_ZERO = "zero"
BASIS_SPAN_FULL = "span_full"
BASIS_SHARED = "shared"
BASIS_NOT_RECORDED = "not_recorded"
BASIS_UNKNOWN = "unknown"

ORIGIN_MAIN = "main"
ORIGIN_SUBAGENT = "subagent"

SLOT_SUMMARY = "summary"
SLOT_ERROR = "error"
SLOT_THINKING = "llm.thinking"

_OUTPUT_OWNER_SLOTS = ("turn.output", "llm.output", "tool.output", "io.output")
_SINGLE_EVENT_SLOTS = ("skill.read", "skill.inject", "subagent.run")
_EVIDENCE_SLOTS = ("malformed", "unreadable")
_OUTER_ONLY_PREFIXES = ("memory.feedback", "memory.enqueue", "personalize.")
_TOOL_RESULT_SPANS = ("tool.call", "skill.read")
_TOOL_RESULT_SLOTS = ("tool.output", "skill.read")

_INTEGRITY_RULES = (
    ("outside the trace store", "artifact_outside_store"),
    ("artifact missing", "artifact_missing"),
    ("not a regular file", "artifact_unreadable"),
    ("content over 512 KiB", "artifact_truncated"),
    ("not valid JSON", "artifact_unreadable"),
    ("content unavailable", "artifact_unreadable"),
    ("blob(s) missing", "blob_missing"),
    ("over the 512 KiB cap", "blob_missing"),
)


@dataclass(frozen=True)
class TrajectoryEntry:
    """One row of the trajectory view; see the module docstring for the rules."""

    entry_id: str
    kind: str
    span_name: str
    slot: str
    trace_id: str
    span_id: str
    parent_span_id: str | None
    turn_span_id: str | None
    turn_number: int | None
    turn_start: bool
    origin: str
    sort_key: tuple[Any, ...]
    event_time: str
    preview: str | None
    operation_status: str
    status_evidence: tuple[str, ...]
    failure_entry: bool
    integrity: tuple[str, ...]
    operation_start: str | None
    operation_end: str | None
    duration_ms: int | None
    charged_ms: int | None
    timing_basis: str
    duration_owner: str | None
    meta: dict[str, Any]
    revision: int = 0


@dataclass(frozen=True)
class TurnInfo:
    turn_span_id: str
    trace_id: str
    number: int
    start: str
    in_progress: bool


@dataclass(frozen=True)
class Projection:
    entries: tuple[TrajectoryEntry, ...]
    turns: tuple[TurnInfo, ...]


def preview_text(text: str) -> str:
    """``text`` folded onto one line and capped at :data:`PREVIEW_LIMIT`."""
    flat = " ".join(text.split())
    if len(flat) > PREVIEW_LIMIT:
        return flat[: PREVIEW_LIMIT - 1] + "…"
    return flat


def _in_progress(span: dict[str, Any]) -> bool:
    attrs = span.get("attributes")
    return isinstance(attrs, dict) and attrs.get("turn.in_progress") is True


def merge_snapshots(spans: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Collapse physical records into logical spans, keeping input order.

    Last write wins per ``(traceId, spanId)``, except that an in-progress
    snapshot never replaces a record that already reached a terminal state
    -- a late or re-read checkpoint must not turn a finished turn back into a
    running one. Records without a usable identity are kept as they are and
    addressed by position (``POSITION_KEY``, stamped here when absent).
    """
    logical: dict[tuple[str, Any], dict[str, Any]] = {}
    order: list[tuple[str, Any]] = []
    position = 0
    for span in spans:
        trace_id = _conv._str(span.get("traceId"))
        span_id = _conv._str(span.get("spanId"))
        if trace_id and span_id:
            key: tuple[str, Any] = (trace_id, span_id)
            previous = logical.get(key)
            if previous is not None and _in_progress(span) and not _in_progress(previous):
                continue
        else:
            stamped = span.get(POSITION_KEY)
            if not isinstance(stamped, int):
                stamped = position
                span = {**span, POSITION_KEY: stamped}
            position += 1
            key = ("", stamped)
        if key not in logical:
            order.append(key)
        logical[key] = span
    return [logical[key] for key in order]


def _parse_ts(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _utc(value: str, parsed: datetime | None) -> str:
    return parsed.isoformat() if parsed is not None else value


def _kind_of(span_name: str, slot: str) -> str:
    if slot == "turn.input":
        return "user.input"
    if slot == "turn.output":
        return "agent.reply"
    if slot in ("io.input", "io.output"):
        return f"{span_name}.{slot.split('.', 1)[1]}"
    if slot.startswith("artifact:"):
        return slot[len("artifact:") :]
    if slot == SLOT_SUMMARY:
        return f"{span_name}.summary"
    if slot in (SLOT_ERROR, *_EVIDENCE_SLOTS):
        return f"span.{slot}"
    return slot


def _scalar(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, list):
        return [v for v in value if v is None or isinstance(v, (str, int, float, bool))]
    return str(value)


def _meta_of(info: _conv._SpanInfo) -> dict[str, Any]:
    attrs = info.attrs
    keys: tuple[tuple[str, str], ...] = ()
    if info.name == "llm.call":
        keys = (
            ("model", "llm.model"),
            ("input_tokens", "llm.usage.input_tokens"),
            ("output_tokens", "llm.usage.output_tokens"),
        )
    elif info.name in _TOOL_RESULT_SPANS:
        keys = (("tool", "tool.name"), ("skill", "skill.name"))
    elif info.name == "subagent.run":
        keys = (("label", "subagent.label"), ("task_id", "subagent.task_id"))
    elif info.name == "session.turn":
        keys = (("tool_count", "turn.tool_count"), ("skill_count", "turn.skill_count"))
    return {name: _scalar(attrs[key]) for name, key in keys if key in attrs}


def _tool_result(records: list[dict[str, Any]], attrs: dict[str, Any]) -> Any:
    for record in records:
        if record["slot"] in _TOOL_RESULT_SLOTS and isinstance(record["payload"], dict):
            result = record["payload"].get("result")
            if isinstance(result, str):
                return result
    return attrs.get("tool.result_preview")


def _status_of(info: _conv._SpanInfo, records: list[dict[str, Any]]) -> tuple[str, tuple[str, ...]]:
    """(operation_status, evidence) from raw span data, never from record text."""
    evidence: list[str] = []
    if info.error:
        evidence.append("span_error")
    if info.attrs.get("tool.error"):
        evidence.append("tool_error")
    if info.name in _TOOL_RESULT_SPANS:
        result = _tool_result(records, info.attrs)
        if isinstance(result, str) and result.startswith("Error"):
            evidence.append("tool_result_error")
    if evidence:
        return STATUS_ERROR, tuple(evidence)
    if info.name == "session.turn" and info.attrs.get("turn.in_progress") is True:
        return STATUS_RUNNING, ("turn_in_progress",)
    status = info.raw.get("status") if isinstance(info.raw, dict) else None
    if isinstance(status, dict) and status.get("code") == "OK":
        outer = ("outer_only",) if info.name.startswith(_OUTER_ONLY_PREFIXES) else ()
        return STATUS_OK, outer
    return STATUS_UNKNOWN, ()


def _owner_slot(slots: Sequence[str]) -> str | None:
    for slot in _OUTPUT_OWNER_SLOTS:
        if slot in slots:
            return slot
    if SLOT_ERROR in slots:
        return SLOT_ERROR
    single = [slot for slot in slots if slot in _SINGLE_EVENT_SLOTS]
    if single:
        return single[0]
    artifacts = sorted(slot for slot in slots if slot.startswith("artifact:"))
    if artifacts:
        return artifacts[-1]
    if SLOT_SUMMARY in slots:
        return SLOT_SUMMARY
    return None


def _integrity_of(record: dict[str, Any]) -> list[str]:
    codes: list[str] = []
    if record["slot"] == "malformed":
        codes.append("malformed_span")
    if record["slot"] == "unreadable":
        codes.append("span_unreadable")
    degraded = record["degraded"]
    if isinstance(degraded, str) and degraded != _conv.NOT_LOADED:
        for needle, code in _INTEGRITY_RULES:
            if needle in degraded and code not in codes:
                codes.append(code)
    return codes


def _preview_of(record: dict[str, Any]) -> str | None:
    slot = record["slot"]
    if slot == SLOT_ERROR:
        return preview_text(record["error"] or record["text"] or "")
    if slot in _EVIDENCE_SLOTS:
        return preview_text(record["degraded"] or "")
    if record["degraded"] is None:
        return preview_text(record["text"])
    if record["preview_text"] is not None:
        return preview_text(record["preview_text"])
    return None


def _summary_record(info: _conv._SpanInfo) -> dict[str, Any]:
    records: list[dict[str, Any]] = []
    summary = _conv._domain_summary(info.attrs, info.name)
    _conv._emit(info, records, _conv._label(info.name), _conv._PHASE_OUTPUT, summary or "", slot=SLOT_SUMMARY)
    if summary is None:
        records[0]["preview_text"] = None
        records[0]["degraded"] = _conv.NOT_LOADED
    return records[0]


def _error_record(info: _conv._SpanInfo, evidence: Sequence[str]) -> dict[str, Any]:
    records: list[dict[str, Any]] = []
    message = info.error or _conv._str(info.attrs.get("tool.error")) or ", ".join(evidence)
    _conv._emit(info, records, _conv._label(info.name), _conv._PHASE_OUTPUT, "", slot=SLOT_ERROR, error=message)
    return records[0]


def _timing(info: _conv._SpanInfo, status: str) -> tuple[int | None, list[str]]:
    """(duration_ms, integrity codes) for the span's own clock."""
    codes: list[str] = []
    start = _parse_ts(info.start)
    end = _parse_ts(info.end)
    if (info.start and start is None) or (info.end and end is None):
        codes.append("bad_timestamp")
    if start is None or end is None or status == STATUS_RUNNING:
        return None, codes
    if end < start:
        codes.append("clock_skew")
        return None, codes
    return int((end - start).total_seconds() * 1000), codes


def _subagent_traces(infos: Sequence[_conv._SpanInfo]) -> set[str]:
    return {
        info.trace_id
        for info in infos
        if info.parent_id is None and _conv._str(info.attrs.get("trace.dispatched_in_trace_id")) is not None
    }


def _turns(infos: Sequence[_conv._SpanInfo]) -> tuple[TurnInfo, ...]:
    roots = sorted((i for i in infos if i.name == "session.turn" and i.span_id), key=lambda i: (i.start, i.span_id))
    return tuple(
        TurnInfo(
            turn_span_id=info.span_id,
            trace_id=info.trace_id,
            number=number,
            start=info.start,
            in_progress=info.attrs.get("turn.in_progress") is True,
        )
        for number, info in enumerate(roots, start=1)
    )


def _entry_id(info: _conv._SpanInfo, slot: str) -> str:
    if not info.span_id or not info.trace_id:
        position = info.raw.get(POSITION_KEY) if isinstance(info.raw, dict) else None
        return f"broken:{position}"
    return f"{info.trace_id}:{info.span_id}:{slot}"


def _span_entries(
    info: _conv._SpanInfo,
    records: list[dict[str, Any]],
    *,
    turn_numbers: dict[str, int],
    subagent_traces: set[str],
) -> list[TrajectoryEntry]:
    if not records:
        records = [_summary_record(info)]
    status, evidence = _status_of(info, records)
    owner = _owner_slot([r["slot"] for r in records])
    if status == STATUS_ERROR and owner is None:
        records = [*records, _error_record(info, evidence)]
        owner = SLOT_ERROR
    duration, timing_codes = _timing(info, status)
    owner_id = _entry_id(info, owner) if owner is not None else None
    turn_number = turn_numbers.get(info.turn_span_id or "")
    origin = ORIGIN_SUBAGENT if info.trace_id in subagent_traces else ORIGIN_MAIN
    start_dt = _parse_ts(info.start)
    end_dt = _parse_ts(info.end)
    meta = _meta_of(info)
    entries: list[TrajectoryEntry] = []
    for rank, record in enumerate(records):
        slot = record["slot"]
        is_input = record["phase"] == _conv._PHASE_INPUT
        is_owner = owner is not None and slot == owner
        if is_owner:
            charged, basis = duration, (BASIS_SPAN_FULL if duration is not None else BASIS_UNKNOWN)
        elif owner is None:
            charged, basis = None, BASIS_UNKNOWN
        elif slot == SLOT_THINKING:
            charged, basis = 0, BASIS_NOT_RECORDED
        elif is_input:
            charged, basis = 0, BASIS_ZERO
        else:
            charged, basis = 0, BASIS_SHARED
        event_raw = info.start if is_input else (info.end or info.start)
        event_dt = start_dt if is_input else (end_dt or start_dt)
        depth = info.depth if is_input else -info.depth
        integrity = _integrity_of(record) + [c for c in timing_codes if c not in _integrity_of(record)]
        if turn_number is None:
            integrity.append("turn_unknown")
        entries.append(
            TrajectoryEntry(
                entry_id=_entry_id(info, slot),
                kind=_kind_of(info.name, slot),
                span_name=info.name,
                slot=slot,
                trace_id=info.trace_id,
                span_id=info.span_id,
                parent_span_id=info.parent_id,
                turn_span_id=info.turn_span_id,
                turn_number=turn_number,
                turn_start=False,
                origin=origin,
                sort_key=(
                    _utc(event_raw, event_dt),
                    record["phase"],
                    depth,
                    _utc(info.start, start_dt),
                    info.trace_id,
                    info.span_id,
                    rank,
                ),
                event_time=event_raw,
                preview=_preview_of(record),
                operation_status=status,
                status_evidence=evidence,
                failure_entry=status == STATUS_ERROR and is_owner,
                integrity=tuple(integrity),
                operation_start=info.start or None,
                operation_end=info.end or None,
                duration_ms=duration,
                charged_ms=charged,
                timing_basis=basis,
                duration_owner=owner_id,
                meta=meta,
            )
        )
    return entries


def project_entries(spans: Sequence[dict[str, Any]], *, state: Path, read: bool = False) -> Projection:
    """Ordered trajectory entries for ``spans`` (physical records, any order).

    ``read=True`` loads artifact bodies through the conversation layer's
    bounded reader so previews come from full content; ``read=False`` uses
    recorded preview attributes only and touches no file.
    """
    infos = _conv._build_infos(merge_snapshots(spans))
    turns = _turns(infos)
    turn_numbers = {turn.turn_span_id: turn.number for turn in turns}
    subagent_traces = _subagent_traces(infos)
    blob_cache: dict[str, tuple[Any, str]] = {}
    entries: list[TrajectoryEntry] = []
    for info in infos:
        records = _conv.span_records(info, state, None, blob_cache, read=read, dedup=False)
        entries.extend(_span_entries(info, records, turn_numbers=turn_numbers, subagent_traces=subagent_traces))
    entries.sort(key=lambda entry: entry.sort_key)
    seen_turns: set[str] = set()
    for index, entry in enumerate(entries):
        if entry.turn_span_id is None or entry.turn_number is None or entry.turn_span_id in seen_turns:
            continue
        seen_turns.add(entry.turn_span_id)
        entries[index] = replace(entry, turn_start=True)
    return Projection(entries=tuple(entries), turns=turns)


__all__ = [
    "POSITION_KEY",
    "PREVIEW_LIMIT",
    "Projection",
    "TrajectoryEntry",
    "TurnInfo",
    "merge_snapshots",
    "preview_text",
    "project_entries",
]
