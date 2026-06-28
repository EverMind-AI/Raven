"""Global system-event queue feeding the heartbeat wake loop.

Single-consumer (the HeartbeatService loop), single-producer-process
(everything runs inside the gateway's asyncio loop). Events are ephemeral
by design: they never persist to disk and never enter session history —
losing them on crash is acceptable because the next interval tick re-reads
the world from HEARTBEAT.md anyway.

Consumption is two-phase (``peek_all`` → tick → ``ack``) so a failed
heartbeat tick leaves the events in place for the next attempt instead of
silently dropping them.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from loguru import logger

DEFAULT_MAX_EVENTS = 20


@dataclass
class SystemEvent:
    """One ephemeral fact the heartbeat session should know about."""

    text: str
    source: str  # "cron" | "subagent" | "manual" | ...
    # Events with the same context_key replace each other: only the latest
    # state of e.g. "cron:job123" is worth a wake-up.
    context_key: str | None = None
    ts: float = field(default_factory=time.time)
    # Assigned by the queue on enqueue. A replacement event gets a fresh seq
    # so an in-flight tick's ack (which only removes the seqs it saw) cannot
    # delete the newer payload.
    seq: int = 0


class SystemEventQueue:
    """Bounded in-memory FIFO with context-key dedup and peek/ack semantics."""

    def __init__(self, max_events: int = DEFAULT_MAX_EVENTS):
        self._events: list[SystemEvent] = []
        self._seq = 0
        self._max = max_events

    def enqueue(self, event: SystemEvent) -> None:
        self._seq += 1
        event.seq = self._seq
        if event.context_key is not None:
            self._events = [
                e for e in self._events if e.context_key != event.context_key
            ]
        self._events.append(event)
        if len(self._events) > self._max:
            dropped = self._events.pop(0)
            logger.warning(
                "system-events queue full ({}), dropped oldest event from {}",
                self._max,
                dropped.source,
            )

    def peek_all(self) -> list[SystemEvent]:
        """Snapshot the current events without consuming them."""
        return list(self._events)

    def ack(self, events: list[SystemEvent]) -> None:
        """Remove exactly the events a successful tick saw (by seq).

        Events enqueued — or replaced under the same context_key — while the
        tick was running carry seqs outside the snapshot and survive.
        """
        if not events:
            return
        seen = {e.seq for e in events}
        self._events = [e for e in self._events if e.seq not in seen]

    def discard(self, context_key: str) -> bool:
        """Drop a pending event a producer knows is stale (e.g. a cron
        failure superseded by a successful retry). Returns True if one
        was removed."""
        before = len(self._events)
        self._events = [e for e in self._events if e.context_key != context_key]
        return len(self._events) < before

    def __len__(self) -> int:
        return len(self._events)
