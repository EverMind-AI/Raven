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

Two inputs let an index feed the projection without re-reading files:
``cached_records`` (compact per-span records produced by
:func:`preview_records` under a :class:`ReadBudget`) and *skeleton* spans
(``SKELETON_KEY``), which take part in ancestry, turn numbering and origin but
produce no entries, so trimmed ancestors keep their descendants attributed.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

from raven.tracing import artifact_v2
from raven.trajectory import conversation as _conv

POSITION_KEY = "__position__"
SKELETON_KEY = "__skeleton__"
PREVIEW_LIMIT = 200
PREVIEW_READ_LIMIT = 64 * 1024

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

_IO_PAIR_SPANS = ("skill.rewrite", "skill.gate", "context.curate")
_COMPACT_KEYS = ("slot", "phase", "degraded", "error", "preview_text", "text", "payload")

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


class ReadBudget:
    """Reads and bytes one preview pass may spend, with a shared deadline.

    ``take`` reserves a read before it happens and refuses once the read or
    byte allowance is gone, or once the deadline has passed -- except for
    the first ``minimum_reads`` reads, so every pass makes progress.
    """

    def __init__(
        self,
        max_reads: int,
        max_bytes: int,
        deadline: float | None = None,
        *,
        now: Callable[[], float] = time.monotonic,
        minimum_reads: int = 1,
    ) -> None:
        self.max_reads = max_reads
        self.max_bytes = max_bytes
        self.deadline = deadline
        self.now = now
        self.minimum_reads = minimum_reads
        self.reads = 0
        self.bytes = 0
        self.exhausted = False

    def take(self, nbytes: int) -> bool:
        if self.reads >= self.max_reads or self.bytes + nbytes > self.max_bytes:
            self.exhausted = True
            return False
        if self.reads >= self.minimum_reads and self.deadline is not None and self.now() >= self.deadline:
            self.exhausted = True
            return False
        self.reads += 1
        self.bytes += nbytes
        return True


@dataclass
class SpanCache:
    """Compact records for one span plus how far its artifact slots were read.

    ``pending`` holds the half-finished two-phase read of a v2 ``llm.input``
    (shell parsed, prompt blob not yet read) so the next pass resumes at the
    blob instead of re-reading the shell.
    """

    records: list[dict[str, Any]] = field(default_factory=list)
    complete: bool = False
    cursor: int = 0
    pending: dict[str, Any] | None = None


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
    spans = list(spans)
    taken = {span.get(POSITION_KEY) for span in spans if isinstance(span.get(POSITION_KEY), int)}
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
                while position in taken:
                    position += 1
                stamped = position
                taken.add(stamped)
                span = {**span, POSITION_KEY: stamped}
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


def _owner_slot(records: Sequence[dict[str, Any]]) -> str | None:
    """The slot charged the span's duration; an input-phase slot never is."""
    slots = [r["slot"] for r in records]
    for slot in _OUTPUT_OWNER_SLOTS:
        if slot in slots:
            return slot
    if SLOT_ERROR in slots:
        return SLOT_ERROR
    single = [slot for slot in slots if slot in _SINGLE_EVENT_SLOTS]
    if single:
        return single[0]
    artifacts = sorted(
        r["slot"] for r in records if r["slot"].startswith("artifact:") and r["phase"] == _conv._PHASE_OUTPUT
    )
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


def _timing(info: _conv._SpanInfo) -> tuple[int | None, list[str]]:
    """(duration_ms, integrity codes) for the span's own clock.

    An in-progress checkpoint's endTime is the snapshot time, not the end of
    the operation, so it never yields a duration -- whatever status the span
    displays (an ERROR snapshot is still an unfinished turn).
    """
    codes: list[str] = []
    start = _parse_ts(info.start)
    end = _parse_ts(info.end)
    if (info.start and start is None) or (info.end and end is None):
        codes.append("bad_timestamp")
    if start is None or end is None or info.attrs.get("turn.in_progress") is True:
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


def _turns(
    infos: Sequence[_conv._SpanInfo], extra: Sequence[tuple[str, str, str]] | None = None
) -> tuple[TurnInfo, ...]:
    roots = [
        (i.start, i.span_id, i.trace_id, i.attrs.get("turn.in_progress") is True)
        for i in infos
        if i.name == "session.turn" and i.span_id
    ]
    known = {span_id for _, span_id, _, _ in roots}
    for turn_span_id, trace_id, start in extra or ():
        if turn_span_id and turn_span_id not in known:
            known.add(turn_span_id)
            roots.append((start, turn_span_id, trace_id, False))
    roots.sort(key=lambda r: (r[0], r[1]))
    return tuple(
        TurnInfo(turn_span_id=span_id, trace_id=trace_id, number=number, start=start, in_progress=in_progress)
        for number, (start, span_id, trace_id, in_progress) in enumerate(roots, start=1)
    )


def _broken(info: _conv._SpanInfo) -> bool:
    return not info.span_id or not info.trace_id


def _entry_id(info: _conv._SpanInfo, slot: str) -> str:
    if _broken(info):
        position = info.raw.get(POSITION_KEY) if isinstance(info.raw, dict) else None
        return f"broken:{position}:{slot}"
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
    owner = _owner_slot(records)
    if status == STATUS_ERROR and owner is None:
        records = [*records, _error_record(info, evidence)]
        owner = SLOT_ERROR
    duration, timing_codes = _timing(info)
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
        integrity = _integrity_of(record)
        integrity.extend(code for code in timing_codes if code not in integrity)
        if _broken(info) and "malformed_span" not in integrity:
            integrity.append("malformed_span")
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


def project_entries(
    spans: Sequence[dict[str, Any]],
    *,
    state: Path,
    read: bool = False,
    cached_records: Mapping[tuple[str, str], Sequence[dict[str, Any]]] | None = None,
    extra_turns: Sequence[tuple[str, str, str]] | None = None,
) -> Projection:
    """Ordered trajectory entries for ``spans`` (physical records, any order).

    ``read=True`` loads artifact bodies through the conversation layer's
    bounded reader so previews come from full content; ``read=False`` uses
    recorded preview attributes only and touches no file. A span found in
    ``cached_records`` uses those records instead of being expanded. Spans
    marked with ``SKELETON_KEY`` and the ``(turn_span_id, trace_id, start)``
    triples in ``extra_turns`` shape ancestry and turn numbering without
    producing entries.
    """
    merged = merge_snapshots(spans)
    skeleton_keys = {
        (_conv._str(span.get("traceId")) or "", _conv._str(span.get("spanId")) or "")
        for span in merged
        if span.get(SKELETON_KEY) is True
    }
    infos = _conv._build_infos(merged)
    turns = _turns(infos, extra_turns)
    turn_numbers = {turn.turn_span_id: turn.number for turn in turns}
    subagent_traces = _subagent_traces(infos)
    blob_cache: dict[str, tuple[Any, str]] = {}
    entries: list[TrajectoryEntry] = []
    for info in infos:
        key = (info.trace_id, info.span_id)
        if key in skeleton_keys:
            continue
        if cached_records is not None and key in cached_records and not _broken(info):
            records = [dict(record) for record in cached_records[key]]
        else:
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


def _artifact_key(span_name: str, slot: str) -> str | None:
    """The artifact attribute key a slot's body lives under, or None."""
    if slot in ("turn.input", "turn.output", "llm.input", "llm.output", "tool.input", "tool.output", "skill.inject"):
        return slot
    if slot == "skill.read":
        return "tool.output"
    if slot in ("io.input", "io.output"):
        base = "personalize" if span_name.startswith("personalize.") else span_name
        return f"{base}.{slot.split('.', 1)[1]}"
    if slot.startswith("artifact:"):
        return slot[len("artifact:") :]
    return None


def _compact(record: dict[str, Any]) -> dict[str, Any]:
    out = {key: record.get(key) for key in _COMPACT_KEYS}
    text = out.get("text")
    out["text"] = preview_text(text) if isinstance(text, str) and text else (text or "")
    payload = out.get("payload")
    result = payload.get("result") if isinstance(payload, dict) else None
    out["payload"] = {"result": result[:16]} if isinstance(result, str) else None
    return out


def _preview_source(slot: str, obj: Any, text: str) -> str:
    if slot in ("turn.input", "turn.output"):
        return _conv._payload_field(obj, text, "content")
    if slot == "tool.input":
        return _conv._payload_field(obj, text, "params")
    if slot in _TOOL_RESULT_SLOTS:
        return _conv._payload_field(obj, text, "result")
    if slot == "llm.output" and isinstance(obj, dict):
        lines = []
        content = _conv._display(obj.get("content"))
        if content:
            lines.append(content)
        tool_calls = obj.get("tool_calls")
        if isinstance(tool_calls, list):
            lines.extend(_conv._tool_call_line(tc) for tc in tool_calls)
        return "\n".join(lines)
    if slot == "llm.input" and isinstance(obj, dict):
        prompt = obj.get("prompt")
        if isinstance(prompt, str):
            return prompt
        if isinstance(prompt, dict):
            return _conv._display(prompt.get("content"))
        messages = obj.get("messages")
        if isinstance(messages, list) and messages:
            last = messages[-1]
            return _conv._display(last.get("content")) if isinstance(last, dict) else _conv._compact(last)
        return text
    return text if obj is None else _conv._compact(obj)


def _last_message_ref(obj: dict[str, Any]) -> str | None:
    sha1 = artifact_v2.ref_sha1(obj.get("prompt"))
    if sha1 is None:
        messages = obj.get("messages")
        if isinstance(messages, list) and messages:
            sha1 = artifact_v2.ref_sha1(messages[-1])
    return sha1


def _loaded_record(record: dict[str, Any], preview: str, *, capped: bool, payload: Any) -> dict[str, Any]:
    out = dict(record)
    out["text"] = preview
    out["preview_text"] = preview
    out["degraded"] = _conv.NOT_LOADED if capped else None
    out["payload"] = payload
    return out


def _targets(info: _conv._SpanInfo, records: list[dict[str, Any]]) -> list[tuple[int, str]]:
    out: list[tuple[int, str]] = []
    for index, record in enumerate(records):
        if record["slot"] == SLOT_THINKING:
            continue
        key = _artifact_key(info.name, record["slot"])
        if key is not None and f"{key}.artifact_path" in info.attrs:
            out.append((index, key))
    return out


def _failed_record(record: dict[str, Any], reason: str) -> dict[str, Any]:
    """The skeleton record carrying the reader's reason, so integrity stays visible."""
    out = dict(record)
    if record.get("preview_text") is not None:
        out["degraded"] = f"{reason} — truncated preview"
    else:
        out["degraded"] = f"content unavailable — {reason}"
    return out


def _finish_llm_input(record: dict[str, Any], message: Any, note: str | None, *, capped: bool) -> dict[str, Any]:
    source = _conv._display(message.get("content")) if isinstance(message, dict) else _conv._display(message)
    out = _loaded_record(record, preview_text(source), capped=capped, payload=None)
    if note is not None:
        out["degraded"] = note
    return out


def _read_blob(state: Path, sha1: str) -> tuple[Any, str | None, bool]:
    """(message, degraded note, capped) for one referenced message blob."""
    path = artifact_v2.message_path(state / "logs" / "audit-artifacts", sha1)
    text, reason = _conv._read_artifact(state, str(path), PREVIEW_READ_LIMIT)
    if text is None:
        return artifact_v2.placeholder(sha1), "1 message blob(s) missing — placeholders shown", False
    if reason is not None:
        return {"role": "unknown", "content": text}, None, True
    parsed, ok = _conv._parse_json(text)
    if not ok:
        return artifact_v2.placeholder(sha1), "1 message blob(s) missing — placeholders shown", False
    return parsed, None, False


def preview_records(
    span: dict[str, Any], *, state: Path, budget: ReadBudget, cache: SpanCache | None = None
) -> SpanCache:
    """Bounded preview reads for one span, resumable across budget passes.

    Every artifact-bearing slot is inspected once (``PREVIEW_READ_LIMIT``
    bytes each, one ``budget.take`` per read) whatever preview attributes it
    already carries -- an ``llm.output`` with a recorded preview still has
    to be opened to discover ``thinking_blocks``. A v2 ``llm.input`` shell
    resolves only its ``prompt`` reference (one more bounded read); when the
    budget runs out between the two reads the parsed shell is kept in
    ``cache.pending`` so the next pass reads only the blob. A read that
    fails keeps the recorded preview but carries the reader's reason, so
    integrity codes survive into the projection. Reads capped at the
    preview limit are marked NOT_LOADED, not as data problems.
    """
    infos = _conv._build_infos([span])
    info = infos[0]
    if cache is None:
        skeleton = _conv.span_records(info, state, None, {}, read=False, dedup=False)
        cache = SpanCache(records=[_compact(r) for r in skeleton], complete=False, cursor=0)
        if _broken(info):
            cache.complete = True
            return cache
    records = cache.records
    if cache.pending is not None:
        pending = cache.pending
        if not budget.take(PREVIEW_READ_LIMIT):
            return cache
        message, note, capped = _read_blob(state, pending["sha1"])
        records[pending["index"]] = _finish_llm_input(records[pending["index"]], message, note, capped=capped)
        cache.pending = None
        cache.cursor += 1
    position = cache.cursor
    while True:
        targets = _targets(info, records)
        if position >= len(targets):
            break
        index, key = targets[position]
        record = records[index]
        pointer = _conv._str(info.attrs.get(f"{key}.artifact_path"))
        if pointer is None:
            position += 1
            continue
        if not budget.take(PREVIEW_READ_LIMIT):
            break
        text, reason = _conv._read_artifact(state, pointer, PREVIEW_READ_LIMIT)
        if text is None:
            records[index] = _failed_record(record, reason or "artifact missing")
            position += 1
            continue
        capped = reason is not None
        obj, ok = _conv._parse_json(text) if not capped else (None, False)
        if record["slot"] == "llm.input" and isinstance(obj, dict) and artifact_v2.is_v2(obj):
            sha1 = _last_message_ref(obj)
            if sha1 is None:
                records[index] = _loaded_record(record, "", capped=capped, payload=None)
                position += 1
                continue
            if not budget.take(PREVIEW_READ_LIMIT):
                cache.pending = {"index": index, "sha1": sha1}
                cache.cursor = position
                return cache
            message, note, blob_capped = _read_blob(state, sha1)
            records[index] = _finish_llm_input(record, message, note, capped=capped or blob_capped)
            position += 1
            continue
        if not capped and not ok:
            # A generic artifact slot takes the body whole, so plain text is a
            # valid body; a slot that expects a structured payload cannot use it.
            loaded = _loaded_record(record, preview_text(text), capped=False, payload=None)
            if not record["slot"].startswith("artifact:"):
                loaded["degraded"] = "artifact is not valid JSON — shown raw"
            records[index] = loaded
            position += 1
            continue
        source = _preview_source(record["slot"], obj if ok else None, text)
        payload = None
        if record["slot"] in _TOOL_RESULT_SLOTS and isinstance(obj, dict) and isinstance(obj.get("result"), str):
            payload = {"result": obj["result"][:16]}
        records[index] = _loaded_record(record, preview_text(source), capped=capped, payload=payload)
        if record["slot"] == "llm.output" and isinstance(obj, dict):
            thinking = _conv._thinking_text(obj)
            existing = next((i for i, r in enumerate(records) if r["slot"] == SLOT_THINKING), None)
            if thinking and existing is None:
                records.insert(
                    index,
                    _loaded_record(
                        {**record, "slot": SLOT_THINKING, "error": None},
                        preview_text(thinking),
                        capped=True,
                        payload=None,
                    ),
                )
            elif thinking and existing is not None:
                records[existing] = _loaded_record(records[existing], preview_text(thinking), capped=True, payload=None)
        position += 1
    cache.cursor = position
    cache.complete = cache.pending is None and position >= len(_targets(info, records))
    return cache


__all__ = [
    "POSITION_KEY",
    "PREVIEW_LIMIT",
    "PREVIEW_READ_LIMIT",
    "SKELETON_KEY",
    "Projection",
    "ReadBudget",
    "SpanCache",
    "TrajectoryEntry",
    "TurnInfo",
    "merge_snapshots",
    "preview_records",
    "preview_text",
    "project_entries",
]
