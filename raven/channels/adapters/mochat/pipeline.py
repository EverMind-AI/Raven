"""Inbound pipeline pieces for the Mochat channel.

Building blocks the channel orchestrates: per-target message dedup (passive)
and the per-target delay buffer (owns its flush timers; talks back only
through an injected flush callback, never holds the channel).
"""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from raven.channels.adapters.mochat.parsing import MochatBufferedEntry

_MAX_SEEN_PER_TARGET = 2000


class Dedup:
    """Per-target seen-message-id memory, FIFO-capped per target."""

    def __init__(self, cap: int = _MAX_SEEN_PER_TARGET):
        self._cap = cap
        self._sets: dict[str, set[str]] = {}
        self._queues: dict[str, deque[str]] = {}

    def seen(self, key: str, message_id: str) -> bool:
        """Record *message_id* under *key*; return True if already seen."""
        seen_set = self._sets.setdefault(key, set())
        queue = self._queues.setdefault(key, deque())
        if message_id in seen_set:
            return True
        seen_set.add(message_id)
        queue.append(message_id)
        while len(queue) > self._cap:
            seen_set.discard(queue.popleft())
        return False


@dataclass
class _DelayState:
    """Per-key debounce state: pending entries + a lock + the flush timer."""

    target_id: str
    target_kind: str
    entries: list[MochatBufferedEntry] = field(default_factory=list)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    timer: asyncio.Task | None = None


FlushCallback = Callable[[str, str, "list[MochatBufferedEntry]", bool], Awaitable[None]]


class DelayBuffer:
    """Debounced per-target entry buffering; owns the flush timers.

    Invariants the implementation must keep (pinned by tests):

    - ``flush_cb(target_id, target_kind, entries, was_mentioned)`` runs
      OUTSIDE the per-key state lock — the callback may re-enter ``enqueue``
      on the same key without deadlocking.
    - A timer-fired flush never cancels its own task (a self-cancel would
      raise CancelledError at the callback await and abort the dispatch);
      only a foreign flush (``flush_now``) cancels the pending timer.
    - Net effect: every entry is delivered to ``flush_cb`` exactly once.
    """

    def __init__(self, delay_ms: Callable[[], float], flush_cb: FlushCallback):
        self._delay_ms = delay_ms
        self._flush_cb = flush_cb
        self._states: dict[str, _DelayState] = {}

    def _state(self, key: str, target_id: str, target_kind: str) -> _DelayState:
        return self._states.setdefault(key, _DelayState(target_id, target_kind))

    async def enqueue(self, key: str, target_id: str, target_kind: str, entry: MochatBufferedEntry) -> None:
        state = self._state(key, target_id, target_kind)
        async with state.lock:
            state.entries.append(entry)
            if state.timer:
                state.timer.cancel()
            state.timer = asyncio.create_task(self._flush_after(key))

    async def _flush_after(self, key: str) -> None:
        await asyncio.sleep(max(0, self._delay_ms()) / 1000.0)
        await self._flush(key, was_mentioned=False, entry=None)

    async def flush_now(self, key: str, target_id: str, target_kind: str, entry: MochatBufferedEntry) -> None:
        """Drain buffered entries plus the triggering *entry* immediately
        (the mention path), cancelling any pending timer."""
        self._state(key, target_id, target_kind)
        await self._flush(key, was_mentioned=True, entry=entry)

    async def _flush(self, key: str, *, was_mentioned: bool, entry: MochatBufferedEntry | None) -> None:
        state = self._states[key]
        async with state.lock:
            if entry:
                state.entries.append(entry)
            current = asyncio.current_task()
            if state.timer and state.timer is not current:
                state.timer.cancel()
            state.timer = None
            entries = state.entries[:]
            state.entries.clear()
        if entries:
            await self._flush_cb(state.target_id, state.target_kind, entries, was_mentioned)

    async def cancel_all(self) -> None:
        for state in self._states.values():
            if state.timer:
                state.timer.cancel()
        self._states.clear()
