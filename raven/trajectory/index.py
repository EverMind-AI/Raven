"""Per-session incremental index over the span logs.

A :class:`SessionIndex` owns one session's view of the trace store: it scans
the span log chain (archive files in name order, then the active file) with
its own cursor, decides which traces belong to the session, projects the
member spans into :class:`~raven.trajectory.entries.TrajectoryEntry` rows and
hands out consistent list snapshots plus per-change revisions. A
:class:`TrajectoryIndexer` holds the indexes of one state directory, bounds
how many live at once and runs each refresh in a worker thread under a byte
and time budget.

Protocol facts the readers rely on:

- Files are identified by ``(st_dev, st_ino)``, so the rotation rename keeps
  the same cursor; whether a file has more to read is decided by its current
  size against the consumed offset, never by a past EOF.
- Trace membership is three-valued. A span whose ``session.key`` matches
  makes its trace a member; a root span's ``trace.dispatched_in_trace_id``
  links a sub-agent trace to its parent, and the decision propagates down
  that link when the parent is decided. Undecided spans wait in a bounded
  buffer; when it overflows, the oldest trace is *spilled* to a log position
  and re-read from there if it later proves to be a member.
- ``epoch`` changes whenever the index is rebuilt (truncation, deletion,
  eviction, process restart); ``revision`` is a per-change counter inside an
  epoch, so a client resumes with ``changes(epoch, after_revision)`` and is
  told to reset when either no longer applies.
- A list snapshot freezes entry objects, not ids, so paging returns one
  consistent picture while the index moves on.
- Trimming to the entry limit keeps a lightweight skeleton of any removed
  span that still has descendants, so survivors keep their turn, depth and
  origin; turn numbering also remembers removed turn roots.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import os
import secrets
import threading
import time
from collections import OrderedDict, deque
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Callable, Sequence

from raven.tracing import config as tracing_config
from raven.trajectory import entries as _entries
from raven.trajectory import store as tstore

SCAN_CHUNK = 64 * 1024
MAX_LINE_BYTES = 2 * 1024 * 1024
DEFAULT_BUDGET_BYTES = 4 * 1024 * 1024
DEFAULT_BUDGET_SECONDS = 0.15
PREVIEW_READS_PER_REFRESH = 32
PREVIEW_BYTES_PER_REFRESH = 2 * 1024 * 1024
PENDING_SPAN_LIMIT = 10_000
DEPENDENCY_LIMIT = 200_000
ENTRY_LIMIT = 50_000
REMOVED_WINDOW = 1_000
SNAPSHOT_TTL_SECONDS = 120.0
MAX_SESSIONS = 8
IDLE_SECONDS = 60.0
THROTTLE_SECONDS = 0.3
FINGERPRINT_BYTES = 64

MEMBER = "member"
FOREIGN = "foreign"
PENDING = "pending"

PHASE_SCANNING = "scanning"
PHASE_READY = "ready"
PHASE_FAILED = "failed"

_SKELETON_ATTRS = (
    "session.key",
    "trace.dispatched_by_span_id",
    "trace.dispatched_in_trace_id",
    "turn.in_progress",
)
_OUTPUT_SLOTS = ("turn.output", "llm.output", "tool.output", "io.output")

FileKey = tuple[int, int]
SpanKey = tuple[str, str]

_log = logging.getLogger("raven.trajectory.index")


class CursorExpiredError(Exception):
    """The list cursor names a session, epoch or snapshot this index no longer serves."""


@dataclass
class Limits:
    budget_bytes: int = DEFAULT_BUDGET_BYTES
    budget_seconds: float = DEFAULT_BUDGET_SECONDS
    preview_reads: int = PREVIEW_READS_PER_REFRESH
    preview_bytes: int = PREVIEW_BYTES_PER_REFRESH
    pending_spans: int = PENDING_SPAN_LIMIT
    dependencies: int = DEPENDENCY_LIMIT
    entries: int = ENTRY_LIMIT
    removed_window: int = REMOVED_WINDOW
    snapshot_ttl: float = SNAPSHOT_TTL_SECONDS
    max_sessions: int = MAX_SESSIONS
    idle_seconds: float = IDLE_SECONDS
    throttle_seconds: float = THROTTLE_SECONDS
    max_line_bytes: int = MAX_LINE_BYTES


# ── scanning ──────────────────────────────────────────────────────────


@dataclass
class ScanBudget:
    max_bytes: int
    deadline: float | None
    now: Callable[[], float] = time.monotonic
    used: int = 0

    def remaining(self) -> int:
        return max(0, self.max_bytes - self.used)

    def allow(self, nbytes: int) -> bool:
        """Whether ``nbytes`` more may be read; the first chunk of a pass always may."""
        if nbytes <= 0 or self.used + nbytes > self.max_bytes:
            return False
        return self.used == 0 or self.deadline is None or self.now() < self.deadline

    def charge(self, nbytes: int) -> None:
        self.used += nbytes


@dataclass
class FileCursor:
    key: FileKey
    path: Path
    offset: int = 0
    size_seen: int = 0
    mtime_seen: int = -1
    fingerprint: str | None = None
    partial: bytearray = field(default_factory=bytearray)
    partial_start: int = 0
    discarding: bool = False


@dataclass(frozen=True)
class RawRecord:
    key: FileKey
    offset: int
    span: dict[str, Any]


@dataclass
class ScanBatch:
    records: list[RawRecord] = field(default_factory=list)
    done: bool = False
    scanned_bytes: int = 0
    total_bytes: int = 0
    generation_changed: bool = False
    oversized_dropped: int = 0


@dataclass
class RecoveryTask:
    trace_id: str
    key: FileKey
    offset: int
    partial: bytearray = field(default_factory=bytearray)
    discarding: bool = False


def _file_key(stat_result: Any) -> FileKey:
    return (stat_result.st_dev, stat_result.st_ino)


def _position(key: FileKey, offset: int) -> int:
    digest = hashlib.sha1(f"{key[0]}:{key[1]}".encode()).hexdigest()[:12]
    return (int(digest, 16) << 40) | offset


def _parse_line(line: bytes) -> dict[str, Any] | None:
    try:
        text = line.decode("utf-8").strip()
    except UnicodeDecodeError:
        return None
    if not text:
        return None
    try:
        span = json.loads(text)
    except (json.JSONDecodeError, RecursionError):
        return None
    return span if isinstance(span, dict) else None


def _stamp(span: dict[str, Any], key: FileKey, offset: int) -> dict[str, Any]:
    if tstore._str_value(span.get("traceId")) and tstore._str_value(span.get("spanId")):
        return span
    return {**span, _entries.POSITION_KEY: _position(key, offset)}


class _LineAssembler:
    """Turns byte chunks into complete lines, bounding the unterminated tail."""

    def __init__(self, cursor: Any, max_line: int) -> None:
        self.cursor = cursor
        self.max_line = max_line
        self.oversized = 0

    def feed(self, chunk: bytes, base: int) -> list[tuple[int, bytes]]:
        cur = self.cursor
        lines: list[tuple[int, bytes]] = []
        start = 0
        while True:
            newline = chunk.find(b"\n", start)
            if newline < 0:
                tail = chunk[start:]
                if not cur.discarding and tail:
                    if len(cur.partial) + len(tail) > self.max_line:
                        cur.partial.clear()
                        cur.discarding = True
                        self.oversized += 1
                    else:
                        if not cur.partial:
                            cur.partial_start = base + start
                        cur.partial.extend(tail)
                break
            segment = chunk[start:newline]
            if cur.discarding:
                cur.discarding = False
            else:
                if cur.partial:
                    line, offset = bytes(cur.partial) + segment, cur.partial_start
                    cur.partial.clear()
                else:
                    line, offset = segment, base + start
                if len(line) > self.max_line:
                    self.oversized += 1
                else:
                    lines.append((offset, line))
            start = newline + 1
        return lines


class SpanLogScanner:
    """One session's cursor over the span log chain; see the module docstring."""

    def __init__(self, state_dir: Path, *, max_line_bytes: int = MAX_LINE_BYTES) -> None:
        self.state_dir = state_dir
        self.max_line_bytes = max_line_bytes
        self.cursors: dict[FileKey, FileCursor] = {}
        self.active_key: FileKey | None = None
        self._active_path = state_dir / "logs" / "audit-spans.log"

    def _catalog(self) -> list[tuple[Path, FileKey, int, int]]:
        archive: list[tuple[Path, FileKey, int, int]] = []
        active: list[tuple[Path, FileKey, int, int]] = []
        for path in tstore.span_log_paths(self.state_dir):
            try:
                stat_result = path.stat()
            except OSError:
                continue
            item = (path, _file_key(stat_result), stat_result.st_size, stat_result.st_mtime_ns)
            (active if path == self._active_path else archive).append(item)
        return archive + active

    @staticmethod
    def _fingerprint(handle: Any) -> str | None:
        """sha1 of the first FINGERPRINT_BYTES of an open file, or None when it is shorter."""
        position = handle.tell()
        handle.seek(0)
        head = handle.read(FINGERPRINT_BYTES)
        handle.seek(position)
        if len(head) < FINGERPRINT_BYTES:
            return None
        return hashlib.sha1(head).hexdigest()

    def _generation_changed(self, catalog: Sequence[tuple[Path, FileKey, int, int]]) -> bool:
        present = {key: size for _, key, size, _ in catalog}
        for key, cursor in list(self.cursors.items()):
            if key not in present:
                if cursor.offset < cursor.size_seen or cursor.partial:
                    return True
                del self.cursors[key]
                continue
            if present[key] < cursor.offset:
                return True
        if self.active_key is not None and self.active_key not in present:
            cursor = self.cursors.get(self.active_key)
            if cursor is not None and cursor.offset < cursor.size_seen:
                return True
        return False

    def scan(self, budget: ScanBudget) -> ScanBatch:
        batch = ScanBatch()
        catalog = self._catalog()
        batch.total_bytes = sum(size for _, _, size, _ in catalog)
        if self._generation_changed(catalog):
            batch.generation_changed = True
            return batch
        for path, key, size, mtime_ns in catalog:
            cursor = self.cursors.get(key)
            if cursor is None:
                cursor = self.cursors[key] = FileCursor(key=key, path=path)
            cursor.path = path
            cursor.size_seen = max(cursor.size_seen, size)
            if size <= cursor.offset:
                # Nothing new to read, but a same-size rewrite must still be
                # caught. Kernel timestamps are coarse, so the active file (the
                # one a writer rewrites) is re-fingerprinted every pass -- one
                # open and 64 bytes; archive files are immutable after rotation
                # and only re-checked when their mtime moves.
                unchanged = mtime_ns == cursor.mtime_seen and path != self._active_path
                outcome = "ok" if unchanged else self._verify(cursor)
            else:
                outcome = self._read(cursor, budget, batch)
            if outcome != "ok":
                if outcome == "generation":
                    batch.generation_changed = True
                batch.scanned_bytes = sum(c.offset for c in self.cursors.values())
                return batch
        if catalog and catalog[-1][0] == self._active_path:
            self.active_key = catalog[-1][1]
        batch.done = True
        batch.scanned_bytes = sum(c.offset for c in self.cursors.values())
        return batch

    def _verify(self, cursor: FileCursor) -> str:
        """Re-check an unchanged-size file's identity and first bytes; "ok", "retry" or "generation"."""
        try:
            with cursor.path.open("rb") as handle:
                stat_result = os.fstat(handle.fileno())
                if _file_key(stat_result) != cursor.key:
                    return "retry"
                if stat_result.st_size < cursor.offset:
                    return "generation"
                if stat_result.st_size >= FINGERPRINT_BYTES:
                    fingerprint = self._fingerprint(handle)
                    if fingerprint is not None:
                        if cursor.fingerprint is None:
                            cursor.fingerprint = fingerprint
                        elif cursor.fingerprint != fingerprint:
                            return "generation"
                cursor.mtime_seen = stat_result.st_mtime_ns
        except OSError:
            return "ok"
        return "ok"

    def _read(self, cursor: FileCursor, budget: ScanBudget, batch: ScanBatch) -> str:
        """Read ``cursor``'s file forward; "ok", "stop" (budget), "retry" or "generation".

        Identity, size and fingerprint all come from the descriptor that is
        actually read: a rotation between the directory listing and the open
        hands back a different file at the same path, which must not be
        charged to the old cursor ("retry": the next pass re-lists), and a
        reused inode whose first bytes changed is a different log
        ("generation").
        """
        assembler = _LineAssembler(cursor, self.max_line_bytes)
        try:
            with cursor.path.open("rb") as handle:
                stat_result = os.fstat(handle.fileno())
                if _file_key(stat_result) != cursor.key:
                    return "retry"
                size = stat_result.st_size
                if size < cursor.offset:
                    return "generation"
                cursor.size_seen = size
                cursor.mtime_seen = stat_result.st_mtime_ns
                if size >= FINGERPRINT_BYTES:
                    fingerprint = self._fingerprint(handle)
                    if fingerprint is not None:
                        if cursor.fingerprint is None:
                            cursor.fingerprint = fingerprint
                        elif cursor.fingerprint != fingerprint:
                            return "generation"
                handle.seek(cursor.offset)
                while cursor.offset < size:
                    want = min(SCAN_CHUNK, size - cursor.offset, budget.remaining())
                    if not budget.allow(want):
                        batch.oversized_dropped += assembler.oversized
                        return "stop"
                    chunk = handle.read(want)
                    if not chunk:
                        break
                    budget.charge(len(chunk))
                    base = cursor.offset
                    cursor.offset += len(chunk)
                    for offset, line in assembler.feed(chunk, base):
                        span = _parse_line(line)
                        if span is not None:
                            batch.records.append(RawRecord(cursor.key, offset, _stamp(span, cursor.key, offset)))
        except OSError:
            return "ok"
        batch.oversized_dropped += assembler.oversized
        return "ok"

    def rescan(self, task: RecoveryTask, budget: ScanBudget) -> tuple[list[RawRecord], bool]:
        """Records of ``task.trace_id`` from the task's position to the chain end."""
        catalog = self._catalog()
        keys = [key for _, key, _, _ in catalog]
        if task.key not in keys:
            return [], True
        records: list[RawRecord] = []
        start_index = keys.index(task.key)
        for path, key, size, _ in catalog[start_index:]:
            if key != task.key:
                task.key, task.offset = key, 0
                task.partial.clear()
                task.discarding = False
            assembler = _LineAssembler(task, self.max_line_bytes)
            try:
                with path.open("rb") as handle:
                    stat_result = os.fstat(handle.fileno())
                    if _file_key(stat_result) != key:
                        return records, False
                    size = stat_result.st_size
                    handle.seek(task.offset)
                    while task.offset < size:
                        want = min(SCAN_CHUNK, size - task.offset, budget.remaining())
                        if not budget.allow(want):
                            return records, False
                        chunk = handle.read(want)
                        if not chunk:
                            break
                        budget.charge(len(chunk))
                        base = task.offset
                        task.offset += len(chunk)
                        for offset, line in assembler.feed(chunk, base):
                            span = _parse_line(line)
                            if span is not None and tstore._str_value(span.get("traceId")) == task.trace_id:
                                records.append(RawRecord(key, offset, span))
            except OSError:
                continue
        return records, True


# ── session index ─────────────────────────────────────────────────────


@dataclass(frozen=True)
class Removed:
    entry_id: str
    revision: int
    replaced_by: str | None = None


@dataclass(frozen=True)
class IndexState:
    phase: str
    scanned_bytes: int
    total_bytes: int
    head_truncated: int
    recovering_traces: int
    unresolved_traces: int
    unresolved_dropped: int
    oversized_lines_dropped: int
    preview_pending: int
    failure: str | None = None


@dataclass
class Snapshot:
    snapshot_id: str
    revision: int
    items: tuple[_entries.TrajectoryEntry, ...]
    created: float
    last_access: float


@dataclass(frozen=True)
class EntryView:
    """One entry and everything the details layer needs about it, captured
    under a single lock acquisition so no field can come from a later refresh.
    """

    epoch: str
    entry: _entries.TrajectoryEntry
    span: dict[str, Any] | None
    siblings: tuple[_entries.TrajectoryEntry, ...]
    turn: _entries.TurnInfo | None
    parent_llm_calls: tuple[dict[str, Any], ...]
    owner: _entries.TrajectoryEntry | None
    turn_skill_injects: tuple[dict[str, Any], ...] = ()


@dataclass(frozen=True)
class ListPage:
    epoch: str
    snapshot_revision: int
    entries: tuple[_entries.TrajectoryEntry, ...]
    next_cursor: str | None
    index_state: IndexState
    complete: bool


@dataclass(frozen=True)
class ChangeBatch:
    epoch: str
    from_revision: int
    to_revision: int
    upserts: tuple[_entries.TrajectoryEntry, ...]
    removed: tuple[Removed, ...]
    has_more: bool
    reset_required: bool
    index_state: IndexState


_EPOCH_PREFIX = secrets.token_hex(4)
_epoch_counter = 0


def _new_epoch() -> str:
    global _epoch_counter
    _epoch_counter += 1
    return f"{_EPOCH_PREFIX}-{_epoch_counter}"


def _encode_cursor(session_key: str, epoch: str, snapshot_id: str, offset: int) -> str:
    raw = json.dumps({"s": session_key, "e": epoch, "n": snapshot_id, "o": offset}).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _decode_cursor(cursor: str) -> dict[str, Any]:
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        data = json.loads(base64.urlsafe_b64decode(padded.encode()).decode())
    except (ValueError, TypeError):
        raise CursorExpiredError("malformed cursor") from None
    if not isinstance(data, dict):
        raise CursorExpiredError("malformed cursor")
    return data


def _skeleton_of(span: dict[str, Any]) -> dict[str, Any]:
    attrs = tstore._span_attrs(span)
    return {
        "traceId": span.get("traceId"),
        "spanId": span.get("spanId"),
        "parentSpanId": span.get("parentSpanId"),
        "name": span.get("name"),
        "startTime": span.get("startTime"),
        "endTime": span.get("endTime"),
        "status": span.get("status"),
        "attributes": {key: attrs[key] for key in _SKELETON_ATTRS if key in attrs},
        _entries.SKELETON_KEY: True,
    }


def _llm_by_parent(spans: dict[SpanKey, dict[str, Any]]) -> dict[tuple[str, str], tuple[dict[str, Any], ...]]:
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for (trace, _), span in spans.items():
        if span.get("name") != "llm.call":
            continue
        parent = tstore._str_value(span.get("parentSpanId"))
        if trace and parent:
            grouped.setdefault((trace, parent), []).append(span)
    return {key: tuple(items) for key, items in grouped.items()}


def _skill_injects_by_turn(
    projection: _entries.Projection, spans: dict[SpanKey, dict[str, Any]]
) -> dict[tuple[str, str], tuple[dict[str, Any], ...]]:
    """The skill.inject spans of each turn, by (trace, turn span): where a
    feedback step's skill ids find their names, and nowhere else."""
    grouped: dict[tuple[str, str], dict[str, dict[str, Any]]] = {}
    for entry in projection.entries:
        if entry.span_name != "skill.inject" or entry.turn_span_id is None:
            continue
        span = spans.get((entry.trace_id, entry.span_id))
        if span is not None:
            grouped.setdefault((entry.trace_id, entry.turn_span_id), {})[entry.span_id] = span
    return {key: tuple(items.values()) for key, items in grouped.items()}


def _has_artifacts(span: dict[str, Any]) -> bool:
    attrs = tstore._span_attrs(span)
    return any(isinstance(key, str) and key.endswith(".artifact_path") for key in attrs)


class SessionIndex:
    """One session's incremental entry index; see the module docstring."""

    def __init__(
        self,
        session_key: str,
        state_dir: Path,
        *,
        limits: Limits | None = None,
        now: Callable[[], float] = time.monotonic,
    ) -> None:
        self.session_key = session_key
        self.state_dir = state_dir
        self.limits = limits or Limits()
        self._now = now
        self._lock = threading.Lock()
        # The scan and apply phases mutate the cursor and the span tables
        # outside `_lock`. A refresh whose awaiting task was cancelled leaves
        # its worker thread running, so the next refresh must wait for it here.
        self._refresh_lock = threading.Lock()
        self._reset()

    # -- lifecycle ---------------------------------------------------------

    def _reset(self) -> None:
        self.epoch = _new_epoch()
        self.phase = PHASE_SCANNING
        self.failure: str | None = None
        self.scanner = SpanLogScanner(self.state_dir, max_line_bytes=self.limits.max_line_bytes)
        self._counter = 0
        self._entries: dict[str, _entries.TrajectoryEntry] = {}
        self._order: tuple[str, ...] = ()
        self._removed: deque[Removed] = deque(maxlen=self.limits.removed_window)
        self._snapshot: Snapshot | None = None
        self._snapshot_seq = 0
        self.spans: dict[SpanKey, dict[str, Any]] = {}
        self.skeletons: dict[SpanKey, dict[str, Any]] = {}
        self.turn_meta: dict[str, tuple[str, str]] = {}
        self.membership: dict[str, str] = {}
        self.trace_link: dict[str, str | None] = {}
        self.children_of: dict[str, set[str]] = {}
        self.pending: dict[str, list[dict[str, Any]]] = {}
        self.pending_first: dict[str, tuple[int, FileKey, int]] = {}
        self.pending_count = 0
        self.pending_seq = 0
        self.spilled: dict[str, tuple[FileKey, int]] = {}
        self.recovery_queue: deque[RecoveryTask] = deque()
        self.preview_cache: dict[SpanKey, _entries.SpanCache] = {}
        self._by_parent: dict[tuple[str, str], set[str]] = {}
        self.head_truncated = 0
        self.oversized_lines_dropped = 0
        self.unresolved_dropped = 0
        self.scanned_bytes = 0
        self.total_bytes = 0
        self._dirty = False
        self._published_spans: dict[SpanKey, dict[str, Any]] = {}
        self._published_llm_by_parent: dict[tuple[str, str], tuple[dict[str, Any], ...]] = {}
        self._published_injects_by_turn: dict[tuple[str, str], tuple[dict[str, Any], ...]] = {}
        self._published_turns: dict[str, _entries.TurnInfo] = {}
        self._entries_by_span: dict[SpanKey, tuple[str, ...]] = {}
        self._published_state = self._compute_state()

    def mark_failed(self, exc: BaseException) -> None:
        with self._lock:
            self.phase = PHASE_FAILED
            self.failure = type(exc).__name__
            self._published_state = replace(self._published_state, phase=PHASE_FAILED, failure=self.failure)

    # -- refresh (worker thread) ------------------------------------------

    def refresh_sync(self, deadline: float | None = None) -> None:
        with self._refresh_lock:
            self._refresh_locked(deadline)

    def _refresh_locked(self, deadline: float | None) -> None:
        budget = ScanBudget(self.limits.budget_bytes, deadline, self._now)
        batch = self.scanner.scan(budget)
        if batch.generation_changed:
            with self._lock:
                self._reset()
            return
        self.oversized_lines_dropped += batch.oversized_dropped
        self.scanned_bytes, self.total_bytes = batch.scanned_bytes, batch.total_bytes
        for record in batch.records:
            self._apply(record)
        self._prune_dependencies()
        while self.recovery_queue and budget.allow(1):
            task = self.recovery_queue[0]
            records, finished = self.scanner.rescan(task, budget)
            for record in records:
                self._add_span(record.span)
            if not finished:
                break
            self.recovery_queue.popleft()
        self._fill_previews(deadline)
        projection = self._reproject() if self._dirty else None
        if projection is not None:
            self._schedule_preview_repairs(projection)
        ready = batch.done and not self.recovery_queue
        self.phase = PHASE_READY if ready else PHASE_SCANNING
        self.failure = None
        state = self._compute_state()
        spans_view = dict(self.spans) if projection is not None else None
        llm_by_parent = _llm_by_parent(spans_view) if spans_view is not None else None
        injects_by_turn = (
            _skill_injects_by_turn(projection, spans_view)
            if projection is not None and spans_view is not None
            else None
        )
        with self._lock:
            if projection is not None:
                self._publish(projection)
                self._published_spans = spans_view or {}
                self._published_llm_by_parent = llm_by_parent or {}
                self._published_injects_by_turn = injects_by_turn or {}
                self._published_turns = {turn.turn_span_id: turn for turn in projection.turns}
            self._published_state = state
        self._dirty = False

    def _apply(self, record: RawRecord) -> None:
        span = record.span
        attrs = tstore._span_attrs(span)
        trace = tstore._str_value(span.get("traceId"))
        span_id = tstore._str_value(span.get("spanId"))
        if not trace or not span_id:
            if attrs.get("session.key") == self.session_key:
                self._add_span(span)
            return
        if attrs.get("session.key") == self.session_key:
            self._set_membership(trace, MEMBER)
        if tstore._str_value(span.get("parentSpanId")) is None:
            parent = tstore._str_value(attrs.get("trace.dispatched_in_trace_id"))
            self.trace_link[trace] = parent
            if self.membership.get(trace, PENDING) == PENDING:
                if parent is None:
                    self._set_membership(trace, FOREIGN)
                elif self.membership.get(parent) in (MEMBER, FOREIGN):
                    self._set_membership(trace, self.membership[parent])
                else:
                    self.children_of.setdefault(parent, set()).add(trace)
                    self.membership.setdefault(trace, PENDING)
        state = self.membership.get(trace, PENDING)
        if state == MEMBER:
            self._add_span(span)
        elif state == PENDING:
            self.membership.setdefault(trace, PENDING)
            if trace in self.spilled:
                return
            self.pending.setdefault(trace, []).append(span)
            if trace not in self.pending_first:
                self.pending_seq += 1
                self.pending_first[trace] = (self.pending_seq, record.key, record.offset)
            self.pending_count += 1
            self._spill_if_needed()

    def _set_membership(self, trace: str, state: str) -> None:
        if self.membership.get(trace) == state:
            return
        self.membership[trace] = state
        buffered = self.pending.pop(trace, [])
        self.pending_count -= len(buffered)
        first = self.pending_first.pop(trace, None)
        spilled = self.spilled.pop(trace, None)
        if state == MEMBER:
            for span in buffered:
                self._add_span(span)
            if spilled is not None:
                self.recovery_queue.append(RecoveryTask(trace, spilled[0], spilled[1]))
        del first
        for child in self.children_of.pop(trace, set()):
            self._set_membership(child, state)

    def _spill_if_needed(self) -> None:
        while self.pending_count > self.limits.pending_spans and self.pending_first:
            trace = min(self.pending_first, key=lambda t: self.pending_first[t][0])
            _, key, offset = self.pending_first.pop(trace)
            buffered = self.pending.pop(trace, [])
            self.pending_count -= len(buffered)
            self.spilled.setdefault(trace, (key, offset))

    def _dependency_size(self) -> int:
        return (
            len(self.trace_link)
            + sum(len(children) for children in self.children_of.values())
            + len(self.spilled)
            + len(self.membership)
        )

    def _forget_trace(self, trace: str) -> None:
        self.trace_link.pop(trace, None)
        self.children_of.pop(trace, None)
        for children in self.children_of.values():
            children.discard(trace)
        self.spilled.pop(trace, None)
        self.membership.pop(trace, None)
        buffered = self.pending.pop(trace, [])
        self.pending_count -= len(buffered)
        self.pending_first.pop(trace, None)

    def _prune_dependencies(self) -> None:
        while self._dependency_size() > self.limits.dependencies:
            victim = next((t for t, s in self.membership.items() if s == FOREIGN), None)
            if victim is None:
                victim = next((t for t, s in self.membership.items() if s == PENDING), None)
                if victim is not None:
                    self.unresolved_dropped += 1
            if victim is None:
                live = {trace for trace, _ in self.spans} | {trace for trace, _ in self.skeletons}
                victim = next((t for t, s in self.membership.items() if s == MEMBER and t not in live), None)
            if victim is None:
                return
            self._forget_trace(victim)

    def _add_span(self, span: dict[str, Any]) -> None:
        trace = tstore._str_value(span.get("traceId")) or ""
        span_id = tstore._str_value(span.get("spanId")) or ""
        key: SpanKey = (trace, span_id) if trace and span_id else ("", str(span.get(_entries.POSITION_KEY)))
        existing = self.spans.get(key)
        if existing is not None and _entries._in_progress(span) and not _entries._in_progress(existing):
            return
        self.spans[key] = span
        self.skeletons.pop(key, None)
        self.preview_cache.pop(key, None)
        if trace and span_id:
            parent = tstore._str_value(span.get("parentSpanId"))
            if parent:
                self._by_parent.setdefault((trace, parent), set()).add(span_id)
            if span.get("name") == "session.turn":
                self.turn_meta[span_id] = (trace, tstore._str_value(span.get("startTime")) or "")
        self._dirty = True

    def _fill_previews(self, deadline: float | None) -> None:
        budget = _entries.ReadBudget(self.limits.preview_reads, self.limits.preview_bytes, deadline, now=self._now)
        newest_first = sorted(
            self.spans.items(), key=lambda item: tstore._str_value(item[1].get("startTime")) or "", reverse=True
        )
        for key, span in newest_first:
            if not _has_artifacts(span):
                continue
            cache = self.preview_cache.get(key)
            if cache is not None and cache.complete:
                continue
            before = cache.version if cache is not None else -1
            try:
                updated = _entries.preview_records(span, state=self.state_dir, budget=budget, cache=cache)
            except Exception as exc:  # noqa: BLE001 -- one span's artifacts must not stop the session's index
                reason = f"artifact unreadable ({type(exc).__name__})"
                updated = _entries.preview_failed(span, state=self.state_dir, cache=cache, reason=reason)
            self.preview_cache[key] = updated
            if before != updated.version:
                self._dirty = True
            if budget.exhausted:
                break

    def _schedule_preview_repairs(self, projection: _entries.Projection) -> None:
        """Queue the one blob read a model input still needs for its row.

        The preview pass read each input's prompt before anything was known
        about the input before it; once the projection has decided what the
        input adds, the row should show the first message of that which is
        not a system one and has text (the whole list for a first or an
        independent call, the added messages for a continued one), and a
        continued input needs to know whether the message right after its
        predecessor's input is that predecessor's own output. Each read opens
        one message and records its role and the start of its text; the
        next projection picks them up, and the next read, if any, follows.
        The probes kept are the scan window and that one message, so they
        stay bounded however the decision moves. A read that fails is
        recorded as failed, a terminal state: nothing schedules it again. An
        input read whole needs no read at all: its messages are at hand.
        """
        for entry in projection.entries:
            if entry.slot != "llm.input":
                continue
            start = _entries.preview_start(entry.meta)
            if start is None:
                continue
            cache = self.preview_cache.get(self._span_key_of(entry))
            if cache is None or cache.pending is not None:
                continue
            at = next((i for i, r in enumerate(cache.records) if r["slot"] == "llm.input"), None)
            if at is None:
                continue
            record = cache.records[at]
            if _entries.inline_messages(record) is not None:
                continue
            refs = record.get("refs")
            if not isinstance(refs, list):
                continue
            target = _entries.preview_target(record, start, len(refs))
            if target is None:
                continue
            base = None
            if entry.meta.get("delta") == _entries.DELTA_CONTINUED:
                echo_at = entry.meta.get("echo_at")
                base = echo_at if isinstance(echo_at, int) else entry.meta.get("new_from")
            cache.pending = {
                "index": at,
                "sha1": refs[target],
                "kind": "probe",
                "probe_index": target,
                "start": start,
                "base": base,
            }
            cache.complete = False

    def _extra_turns(self) -> list[tuple[str, str, str]]:
        return [
            (turn_span_id, trace, start)
            for turn_span_id, (trace, start) in self.turn_meta.items()
            if (trace, turn_span_id) not in self.spans and (trace, turn_span_id) not in self.skeletons
        ]

    def _project(self) -> _entries.Projection:
        return _entries.project_entries(
            [*self.spans.values(), *self.skeletons.values()],
            state=self.state_dir,
            read=False,
            cached_records={key: cache.records for key, cache in self.preview_cache.items()},
            extra_turns=self._extra_turns(),
        )

    @staticmethod
    def _span_key_of(entry: _entries.TrajectoryEntry) -> SpanKey:
        if entry.entry_id.startswith("broken:"):
            return ("", entry.entry_id.split(":", 2)[1])
        return (entry.trace_id, entry.span_id)

    def _reproject(self) -> _entries.Projection:
        projection = self._project()
        while len(projection.entries) > self.limits.entries:
            excess = len(projection.entries) - self.limits.entries
            victims: dict[SpanKey, None] = {}
            for entry in projection.entries[:excess]:
                victims[self._span_key_of(entry)] = None
            before = len(projection.entries)
            dropped = sum(1 for e in projection.entries if self._span_key_of(e) in victims)
            for key in victims:
                self._trim_span(key)
            projection = self._project()
            if len(projection.entries) >= before:
                _log.warning("trajectory index %s: entry limit trim made no progress", self.session_key)
                break
            self.head_truncated += dropped
        return projection

    def _has_children(self, key: SpanKey) -> bool:
        return bool(self._by_parent.get(key))

    def _trim_span(self, key: SpanKey) -> None:
        span = self.spans.pop(key, None)
        self.preview_cache.pop(key, None)
        if span is None:
            return
        trace, span_id = key
        if trace and span_id and self._has_children(key):
            self.skeletons[key] = _skeleton_of(span)
            return
        self._unlink(span)

    def _unlink(self, span: dict[str, Any]) -> None:
        trace = tstore._str_value(span.get("traceId")) or ""
        span_id = tstore._str_value(span.get("spanId")) or ""
        parent = tstore._str_value(span.get("parentSpanId"))
        if not trace or not span_id or not parent:
            return
        siblings = self._by_parent.get((trace, parent))
        if siblings is not None:
            siblings.discard(span_id)
            if not siblings:
                del self._by_parent[(trace, parent)]
                parent_key = (trace, parent)
                skeleton = self.skeletons.get(parent_key)
                if skeleton is not None and parent_key not in self.spans:
                    del self.skeletons[parent_key]
                    self._unlink(skeleton)

    def _publish(self, projection: _entries.Projection) -> None:
        old = self._entries
        new_ids = {entry.entry_id for entry in projection.entries}
        new_by_span: dict[SpanKey, list[_entries.TrajectoryEntry]] = {}
        for entry in projection.entries:
            new_by_span.setdefault((entry.trace_id, entry.span_id), []).append(entry)
        for entry_id in old:
            if entry_id in new_ids:
                continue
            previous = old[entry_id]
            replaced_by = None
            if previous.slot == _entries.SLOT_ERROR:
                for candidate in new_by_span.get((previous.trace_id, previous.span_id), []):
                    if candidate.slot in _OUTPUT_SLOTS and candidate.entry_id not in old:
                        replaced_by = candidate.entry_id
                        break
            self._counter += 1
            self._removed.append(Removed(entry_id, self._counter, replaced_by))
        published: dict[str, _entries.TrajectoryEntry] = {}
        order: list[str] = []
        for entry in projection.entries:
            previous = old.get(entry.entry_id)
            if previous is not None and replace(previous, revision=0) == replace(entry, revision=0):
                published[entry.entry_id] = previous
            else:
                self._counter += 1
                published[entry.entry_id] = replace(entry, revision=self._counter)
            order.append(entry.entry_id)
        self._entries = published
        self._order = tuple(order)
        by_span: dict[SpanKey, list[str]] = {}
        for entry_id in order:
            entry = published[entry_id]
            by_span.setdefault(self._span_key_of(entry), []).append(entry_id)
        self._entries_by_span = {key: tuple(ids) for key, ids in by_span.items()}

    # -- reads (any thread) -------------------------------------------------

    def capture(self, entry_id: str) -> EntryView | None:
        """The entry plus its raw span, siblings, turn, same-parent LLM calls and owner, all from one publish."""
        with self._lock:
            entry = self._entries.get(entry_id)
            if entry is None:
                return None
            key = self._span_key_of(entry)
            span = self._published_spans.get(key)
            siblings = tuple(self._entries[other] for other in self._entries_by_span.get(key, ()) if other != entry_id)
            parent = tstore._str_value(span.get("parentSpanId")) if span is not None else None
            llm_calls = self._published_llm_by_parent.get((entry.trace_id, parent), ()) if parent else ()
            owner = self._entries.get(entry.duration_owner) if entry.duration_owner else None
            turn = self._published_turns.get(entry.turn_span_id) if entry.turn_span_id else None
            injects = (
                self._published_injects_by_turn.get((entry.trace_id, entry.turn_span_id), ())
                if entry.turn_span_id
                else ()
            )
            return EntryView(self.epoch, entry, span, siblings, turn, llm_calls, owner, injects)

    def _compute_state(self) -> IndexState:
        """Built by the worker from its own containers; readers see the published copy."""
        pending_previews = sum(
            1
            for key, span in self.spans.items()
            if _has_artifacts(span) and not (self.preview_cache.get(key) or _entries.SpanCache()).complete
        )
        unresolved = sum(1 for s in self.membership.values() if s == PENDING) if self.phase == PHASE_READY else 0
        return IndexState(
            phase=self.phase,
            scanned_bytes=self.scanned_bytes,
            total_bytes=self.total_bytes,
            head_truncated=self.head_truncated,
            recovering_traces=len(self.recovery_queue),
            unresolved_traces=unresolved,
            unresolved_dropped=self.unresolved_dropped,
            oversized_lines_dropped=self.oversized_lines_dropped,
            preview_pending=pending_previews,
            failure=self.failure,
        )

    def index_state(self) -> IndexState:
        with self._lock:
            return self._published_state

    def entry(self, entry_id: str) -> _entries.TrajectoryEntry | None:
        with self._lock:
            return self._entries.get(entry_id)

    def span(self, trace_id: str, span_id: str) -> dict[str, Any] | None:
        with self._lock:
            return self._published_spans.get((trace_id, span_id))

    def entries(self) -> tuple[_entries.TrajectoryEntry, ...]:
        with self._lock:
            return tuple(self._entries[entry_id] for entry_id in self._order)

    def list_page(self, cursor: str | None, limit: int) -> ListPage:
        with self._lock:
            now = self._now()
            if cursor is None:
                self._snapshot_seq += 1
                items = tuple(self._entries[entry_id] for entry_id in self._order)
                snapshot = Snapshot(f"snap-{self._snapshot_seq}", self._counter, items, now, now)
                self._snapshot = snapshot
                offset = 0
            else:
                data = _decode_cursor(cursor)
                snapshot = self._snapshot
                if (
                    snapshot is None
                    or data.get("s") != self.session_key
                    or data.get("e") != self.epoch
                    or data.get("n") != snapshot.snapshot_id
                    or not isinstance(data.get("o"), int)
                    or now - snapshot.last_access > self.limits.snapshot_ttl
                ):
                    raise CursorExpiredError("the list snapshot is no longer available")
                offset = data["o"]
                snapshot.last_access = now
            page = snapshot.items[offset : offset + limit]
            end = offset + len(page)
            complete = end >= len(snapshot.items)
            next_cursor = None if complete else _encode_cursor(self.session_key, self.epoch, snapshot.snapshot_id, end)
            if complete:
                self._snapshot = None
            return ListPage(self.epoch, snapshot.revision, page, next_cursor, self._published_state, complete)

    def changes(self, epoch: str, after_revision: int, limit: int) -> ChangeBatch:
        with self._lock:
            state = self._published_state
            window_lost = len(self._removed) == self._removed.maxlen and self._removed[0].revision > after_revision + 1
            if epoch != self.epoch or window_lost:
                return ChangeBatch(self.epoch, after_revision, after_revision, (), (), False, True, state)
            items: list[tuple[int, Any]] = [
                (entry.revision, entry) for entry in self._entries.values() if entry.revision > after_revision
            ]
            items.extend((removed.revision, removed) for removed in self._removed if removed.revision > after_revision)
            items.sort(key=lambda pair: pair[0])
            page = items[:limit]
            upserts = tuple(item for _, item in page if isinstance(item, _entries.TrajectoryEntry))
            removed = tuple(item for _, item in page if isinstance(item, Removed))
            to_revision = page[-1][0] if page else after_revision
            return ChangeBatch(
                self.epoch, after_revision, to_revision, upserts, removed, len(items) > limit, False, state
            )


# ── indexer ───────────────────────────────────────────────────────────


@dataclass
class _Slot:
    index: SessionIndex
    lock: asyncio.Lock
    last_refresh: float | None = None
    last_access: float = 0.0


class TrajectoryIndexer:
    """The session indexes of one state directory, bounded and refreshed on demand."""

    def __init__(
        self,
        state_dir: Path,
        *,
        limits: Limits | None = None,
        now: Callable[[], float] = time.monotonic,
        to_thread: Callable[..., Any] = asyncio.to_thread,
    ) -> None:
        self.state_dir = state_dir
        self.limits = limits or Limits()
        self._now = now
        self._to_thread = to_thread
        self._slots: OrderedDict[str, _Slot] = OrderedDict()

    def _slot(self, session_key: str) -> _Slot:
        slot = self._slots.get(session_key)
        if slot is None:
            while len(self._slots) >= self.limits.max_sessions:
                self._slots.popitem(last=False)
            slot = _Slot(SessionIndex(session_key, self.state_dir, limits=self.limits, now=self._now), asyncio.Lock())
            self._slots[session_key] = slot
        self._slots.move_to_end(session_key)
        slot.last_access = self._now()
        return slot

    def evict_idle(self) -> None:
        now = self._now()
        for key in [k for k, s in self._slots.items() if now - s.last_access > self.limits.idle_seconds]:
            del self._slots[key]

    def session(self, session_key: str) -> SessionIndex:
        return self._slot(session_key).index

    async def refresh(self, session_key: str) -> SessionIndex:
        self.evict_idle()
        slot = self._slot(session_key)
        if slot.last_refresh is not None and self._now() - slot.last_refresh < self.limits.throttle_seconds:
            return slot.index
        async with slot.lock:
            if slot.last_refresh is not None and self._now() - slot.last_refresh < self.limits.throttle_seconds:
                return slot.index
            deadline = self._now() + self.limits.budget_seconds
            try:
                await self._to_thread(slot.index.refresh_sync, deadline)
            except Exception as exc:  # noqa: BLE001 — a broken index must report, not take the RPC down
                slot.index.mark_failed(exc)
            slot.last_refresh = self._now()
        return slot.index


_INDEXERS: dict[Path, TrajectoryIndexer] = {}


def indexer_for(state_dir: Path | None = None) -> TrajectoryIndexer:
    base = (state_dir if state_dir is not None else tracing_config.state_dir()).resolve()
    indexer = _INDEXERS.get(base)
    if indexer is None:
        indexer = _INDEXERS[base] = TrajectoryIndexer(base)
    return indexer


def _reset_for_tests() -> None:
    _INDEXERS.clear()


__all__ = [
    "ChangeBatch",
    "CursorExpiredError",
    "EntryView",
    "IndexState",
    "Limits",
    "ListPage",
    "Removed",
    "ScanBatch",
    "ScanBudget",
    "SessionIndex",
    "SpanLogScanner",
    "TrajectoryIndexer",
    "indexer_for",
]
