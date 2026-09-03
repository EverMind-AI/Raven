"""Off-turn delivery of finished turns to the plugin :class:`MemoryBackend`.

The turn hands a slice over and returns; everything after that -- ordering,
retries, bounds, and what shutdown does with whatever is left -- lives here.
Indexing latency belongs to this module, not to the user's input box.

The per-session deques here are not Lanes (CONTEXT.md reserves that word for
the spine's scheduling domains): nothing in this module schedules a turn.
"""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Callable
from typing import Any, NamedTuple

from loguru import logger

from raven.observability import semconv
from raven.tracing import trace

# How many turns' worth of unindexed writes one session may hold before the
# oldest is dropped. Bounded because a slow memory service must not be able to
# grow a backlog behind a fast typist.
MAX_QUEUED: int = 64
# Ceiling on unindexed writes across every session, and on how many of them may
# be on the wire at once. Per-session bounds alone are not a bound: a gateway
# fielding N chats multiplies them, so a slow backend became N concurrent
# requests and N times the extraction fan-out. The concurrency figure is the
# global in-flight cap this pipeline replaced, kept at its old value.
MAX_TOTAL_QUEUED: int = 256
MAX_CONCURRENCY: int = 4
# How long ``drain`` waits on workers it has already cancelled, after their own
# budget ran out. Short because nothing is riding on it: the writes are lost
# either way and are counted below regardless, so this only buys the tidy
# collection of a worker that was about to stop anyway.
_LEFTOVER_COLLECT_S: float = 1.0
# Backoff between retries of one record. Retries are serial per session, so a
# long tail here blocks that session's later writes -- the total must stay well
# under a conversation's natural gap.
BACKOFF_S: tuple[float, ...] = (2.0, 5.0, 15.0, 30.0)


class _Record(NamedTuple):
    """One queued turn plus the trace context it was enqueued under.

    The worker outlives the turn that spawned it, so the contextvars snapshot
    it was forked with names the wrong turn from the second record onwards --
    each record has to carry its own.
    """

    messages: list[dict]
    trace_ctx: Any


class StorePipeline:
    """Per-session serial delivery of turns to a :class:`MemoryBackend`.

    Serial per session because the EverOS write is an append followed by a
    flush: running one session's turns concurrently lets a later turn's flush
    overtake an earlier turn's append, and the extraction then reads a
    conversation with a hole in it.

    ``on_ok`` / ``on_failure`` are the host's health signals. They fire once
    per record, never once per retry -- the alarm behind them counts turns
    whose write failed, and a record burning its retries is still one turn.
    """

    def __init__(
        self,
        get_backend: Callable[[], Any | None],
        *,
        on_ok: Callable[[], None],
        on_failure: Callable[[str], None],
    ) -> None:
        self._get_backend = get_backend
        self._on_ok = on_ok
        self._on_failure = on_failure
        self._queues: dict[str, deque[_Record]] = {}
        self._workers: dict[str, asyncio.Task] = {}
        self._stopping = asyncio.Event()
        # The record a worker has taken off its queue but not finished. It is
        # in neither the queue nor the completed count, so without this a
        # teardown that times out under-reports what it lost by one per worker.
        self._active: dict[str, _Record] = {}
        self._slots = asyncio.Semaphore(MAX_CONCURRENCY)
        self._dropped = 0

    @property
    def dropped(self) -> int:
        """Turns that will never be indexed: given up on, shed, or abandoned."""
        return self._dropped

    def _outstanding(self) -> int:
        """Turns admitted and not yet settled.

        Counts records a worker has taken but not finished, not just the ones
        still in a deque. A worker pops before it waits for a concurrency slot,
        so a queue-only count reads zero exactly when the backlog is at its
        worst: N sessions each hand their record to a worker that then parks on
        the semaphore, and the ceiling never fires.
        """
        return sum(len(q) for q in self._queues.values()) + len(self._active)

    def _shed(self, reason: str) -> None:
        self._dropped += 1
        self._on_failure(reason)
        logger.warning("backend.store shed an unindexed turn: {}", reason)

    def enqueue(self, session_key: str, messages_slice: list[dict]) -> None:
        """Take a finished turn. Synchronous by contract -- never awaits the backend.

        At most one turn is shed per call. The two ceilings are checked as
        alternatives, not in sequence: when a session is at its own limit the
        ``deque`` evicts on append, and that eviction already frees a global
        slot, so taking the global branch as well would cost two stored turns
        to admit one.
        """
        if self._get_backend() is None:
            return
        if not messages_slice:
            return
        # Admission runs before the session key is retained. Creating the
        # deque first left one behind on every refusal, and refused traffic is
        # exactly the high-cardinality kind: a wedged backend plus a thousand
        # new chats left a thousand empty entries that nothing prunes and that
        # every later scan walks.
        q = self._queues.get(session_key)
        if q is not None and len(q) == q.maxlen:
            # The append below evicts this queue's oldest. That is this call's
            # one victim.
            self._shed(f"session {session_key} is at its queue limit of {MAX_QUEUED}")
        elif self._outstanding() >= MAX_TOTAL_QUEUED:
            if q is None:
                # A session never seen before costs a queue and a worker on top
                # of the record, so admitting one at the ceiling bounds records
                # while leaving the rest unbounded -- a burst of new chats in a
                # single loop iteration allocated a task apiece before any of
                # them could run. It also starves whoever is already here:
                # replacement means the newest arrival always wins.
                self._shed(
                    f"the write backlog is at its global ceiling of {MAX_TOTAL_QUEUED}; "
                    "refusing a turn from a session not already being served"
                )
                return
            # Shed from whichever session is furthest behind rather than from
            # this one: the backlog belongs to the queue that grew it.
            longest = max(self._queues.values(), key=len)
            if longest:
                longest.popleft()
                self._shed(f"the write backlog is at its global ceiling of {MAX_TOTAL_QUEUED}")
            else:
                # Every outstanding turn is already being written, so there is
                # nothing evictable. Refusing the newcomer is the only way to
                # hold the ceiling; growing past it is what the ceiling exists
                # to prevent.
                self._shed(
                    f"the write backlog is at its global ceiling of {MAX_TOTAL_QUEUED} "
                    "with every outstanding turn already in flight"
                )
                return
        if q is None:
            q = self._queues[session_key] = deque(maxlen=MAX_QUEUED)
        q.append(_Record(messages_slice, trace.current()))
        if session_key not in self._workers:
            self._workers[session_key] = asyncio.create_task(self._worker(session_key))

    async def _worker(self, session_key: str) -> None:
        """Drain one session's records in order.

        No await point separates the ``while`` test from the ``finally``, and
        ``enqueue`` is synchronous, so a record can never be stranded by
        arriving while this worker is on its way out. The queue prune in the
        ``finally`` depends on that too: an edit that inserts an await there
        would lose records rather than merely delay them.
        """
        q = self._queues[session_key]
        try:
            while q:
                record = q.popleft()
                self._active[session_key] = record
                announced = False
                for attempt, delay in enumerate((*BACKOFF_S, None)):
                    landed = False
                    reason = "the backend reported the write did not land"
                    try:
                        # The slot covers the request only. Holding it across a
                        # backoff would let one wedged session starve every
                        # other one out of the global budget.
                        async with self._slots:
                            with trace.use_context(record.trace_ctx):
                                landed = await self._store_once(session_key, record.messages, attempt=attempt)
                    except Exception as e:  # noqa: BLE001 - the turn must survive a failed index
                        reason = str(e)
                        logger.exception(
                            "backend.store failed for session {}; turn data preserved in "
                            "session log, plugin-side indexing will be retried",
                            session_key,
                        )
                    if landed:
                        self._on_ok()
                        break
                    if not announced:
                        self._on_failure(reason)
                        announced = True
                    if delay is None:
                        self._dropped += 1
                        logger.warning(
                            "backend.store gave up on a turn for session {} after {} attempts; "
                            "the turn stays in the session log but is not indexed",
                            session_key,
                            len(BACKOFF_S) + 1,
                        )
                        break
                    try:
                        await asyncio.wait_for(self._stopping.wait(), timeout=delay)
                    except asyncio.TimeoutError:
                        continue
                    self._dropped += 1
                    break
                # Reached only when the record is settled -- landed, given up
                # on, or abandoned at shutdown. A cancelled worker leaves its
                # entry behind on purpose, so the drain can count it.
                self._active.pop(session_key, None)
        finally:
            self._workers.pop(session_key, None)
            if not q:
                self._queues.pop(session_key, None)

    @trace.instrument("memory.store", extract=semconv.memory_store)
    async def _store_once(self, session_key: str, messages_slice: list[dict], *, attempt: int) -> bool:
        """One attempt at one write, kept separate as the ``memory.store`` span
        boundary: a span per attempt, rather than one span covering a record's
        retries and the backoff waits between them.

        ``attempt`` (0 on the first try) is passed through as metadata so a
        backend that tracks its own turn-based state -- EverOS's flush cadence,
        notably -- can advance that state once per record instead of once per
        attempt. A backend that ignores metadata sees no difference.
        """
        backend = self._get_backend()
        # Only an explicit False is a failure: a backend that returns nothing
        # never claimed the write was lost.
        return await backend.store(session_key, messages_slice, metadata={"attempt": attempt}) is not False

    async def drain(self, timeout: float) -> int:
        """Settle what is outstanding and report how many turns were lost.

        Retries are cut short first: a worker parked in a backoff is doing
        nothing, and would otherwise spend the whole budget asleep while the
        user waits on a quit they already asked for.
        """
        self._stopping.set()
        workers = {t for t in self._workers.values() if not t.done()}
        if workers:
            await asyncio.wait(workers, timeout=timeout)
        # A worker still running here is inside a request that outlived the
        # budget. Cancel and collect it rather than leaving it: the caller
        # closes the backend's HTTP client next, and a request still in flight
        # against a closed client fails as a transport error nobody reads.
        leftover = {t for t in self._workers.values() if not t.done()}
        for task in leftover:
            task.cancel()
        if leftover:
            # Bounded like the wait above, and for the same reason: `cancel`
            # only schedules the cancellation, so a store that suppresses it
            # would hold this gather -- and the host teardown awaiting this
            # drain -- for as long as its own request runs. Collecting the
            # results is worth a moment; waiting out a worker that will not
            # stop is the hang this whole path is built to avoid.
            done, _ = await asyncio.wait(leftover, timeout=_LEFTOVER_COLLECT_S)
            for task in done:
                if not task.cancelled():
                    task.exception()
        # Anything still queued, plus whatever each cancelled worker had
        # already taken off its queue, was never going to be written.
        abandoned = sum(len(q) for q in self._queues.values()) + len(self._active)
        if abandoned:
            self._dropped += abandoned
            self._active.clear()
        return self._dropped
