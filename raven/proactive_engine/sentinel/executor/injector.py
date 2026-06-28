"""NudgeInjector — executes `action=nudge_inject` decisions.

An inject decision says: "user is mid-conversation; append this P.S.-style
message to the agent's next reply in ``target_session``". The implementation
is simple: NudgeInjector is a callable that AgentLoop registers as its
``response_modifier``. Each time AgentLoop produces a final assistant
content, it asks NudgeInjector: "any pending inject messages for this
session_key?" — if yes, append them.

Lifecycle:
- ``queue(session_key, message, source)``      — Sentinel calls this after
  the Planner emits action=nudge_inject. Queue is keyed by session_key.
- ``__call__(session_key, content) -> content`` — AgentLoop calls this via
  the response_modifier hook. Pops all pending for session_key, appends
  them, returns combined content.
- ``size(session_key)``                        — introspection.

Policy enforcement lives in NudgePolicy (caller enforces before queuing).
Invariants:
- TTL: messages older than ``ttl_seconds`` are dropped on every access.
- Per-session cap: ``max_pending_per_session`` enforced at queue time; older
  entries dropped FIFO to make room.
- Idempotent pop: pop_pending returns messages and clears the queue.
"""

from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Callable

from loguru import logger

from raven.proactive_engine.sentinel.feedback.persistence import JsonStateStore


@dataclass
class _PendingInject:
    message: str
    source: str                  # free-text provenance (e.g., "planner_tick" / "feedback")
    queued_at: datetime


class NudgeInjector:
    """Callable that AgentLoop uses as response_modifier."""

    _STATE_KEY = "injector"

    def __init__(
        self,
        *,
        ttl_seconds: int = 1800,
        max_pending_per_session: int = 3,
        now_fn: Callable[[], datetime] | None = None,
        joiner: str = "\n\n",
        store: JsonStateStore | None = None,
    ) -> None:
        """``store`` is an optional JsonStateStore shared with NudgePolicy /
        DeferManager, used to share the pending queue across processes so
        that one process doesn't drop a nudge the other already queued."""
        self._ttl = timedelta(seconds=ttl_seconds)
        self._max_pending = max_pending_per_session
        self._now_fn = now_fn or datetime.now
        self._joiner = joiner
        self._store = store
        self._pending: dict[str, deque[_PendingInject]] = defaultdict(deque)

        if self._store is not None:
            self._reload_from_store()

    # ------------------------------------------------------------------
    # Sentinel-facing API

    def queue(self, session_key: str, message: str, source: str = "planner") -> None:
        """Enqueue an inject for ``session_key``. Caller should have already
        consulted NudgePolicy.check() and decided allow."""
        if not message or not session_key:
            return
        if self._store is None:
            now = self._now_fn()
            self._expire(session_key, now)
            q = self._pending[session_key]
            while len(q) >= self._max_pending:
                dropped = q.popleft()
                logger.warning(
                    "inject queue full for {}; dropping oldest (source={}, age={}s)",
                    session_key, dropped.source, int((now - dropped.queued_at).total_seconds()),
                )
            q.append(_PendingInject(message=message, source=source, queued_at=now))
            logger.info(
                "inject_queued session_key={} source={} pending={} msg={!r}",
                session_key, source, len(q), message[:80],
            )
            return

        def _mutate(state: dict[str, Any]) -> dict[str, Any]:
            self._hydrate_from_blob(state.get(self._STATE_KEY) or {})
            now = self._now_fn()
            self._expire(session_key, now)
            q = self._pending[session_key]
            while len(q) >= self._max_pending:
                dropped = q.popleft()
                logger.warning(
                    "inject queue full for {}; dropping oldest (source={}, age={}s)",
                    session_key, dropped.source, int((now - dropped.queued_at).total_seconds()),
                )
            q.append(_PendingInject(message=message, source=source, queued_at=now))
            logger.info(
                "inject_queued session_key={} source={} pending={} msg={!r}",
                session_key, source, len(q), message[:80],
            )
            state[self._STATE_KEY] = self._dump_to_blob()
            return state

        self._store.update(_mutate)

    def size(self, session_key: str | None = None) -> int:
        """Pending count for a session (or total if None)."""
        if session_key is None:
            return sum(len(q) for q in self._pending.values())
        return len(self._pending.get(session_key, ()))

    def peek(self, session_key: str) -> list[str]:
        """Introspection (tests + debugging) — does not mutate."""
        now = self._now_fn()
        q = self._pending.get(session_key, ())
        return [p.message for p in q if (now - p.queued_at) < self._ttl]

    # ------------------------------------------------------------------
    # AgentLoop-facing API (response_modifier protocol)

    def __call__(self, session_key: str, content: str) -> str:
        """Pop all pending inject messages for ``session_key`` and append to content."""
        popped = self.pop_pending(session_key)
        if not popped:
            return content
        return content + self._joiner + self._joiner.join(popped)

    def pop_pending(self, session_key: str) -> list[str]:
        """Return pending messages (TTL-filtered) and clear the queue.

        With a store configured, the pop is atomic under the store's lock
        so peer processes can't drain the same queue twice.
        """
        if self._store is None:
            now = self._now_fn()
            self._expire(session_key, now)
            q = self._pending.pop(session_key, deque())
            messages = [p.message for p in q]
            if messages:
                logger.info(
                    "inject_applied session_key={} count={}",
                    session_key, len(messages),
                )
            return messages

        captured: list[str] = []

        def _mutate(state: dict[str, Any]) -> dict[str, Any]:
            self._hydrate_from_blob(state.get(self._STATE_KEY) or {})
            now = self._now_fn()
            self._expire(session_key, now)
            q = self._pending.pop(session_key, deque())
            captured.extend(p.message for p in q)
            state[self._STATE_KEY] = self._dump_to_blob()
            return state

        self._store.update(_mutate)
        if captured:
            logger.info(
                "inject_applied session_key={} count={}",
                session_key, len(captured),
            )
        return captured

    # ------------------------------------------------------------------
    # Internals

    def _expire(self, session_key: str, now: datetime) -> None:
        q = self._pending.get(session_key)
        if q is None:
            return
        while q and (now - q[0].queued_at) >= self._ttl:
            stale = q.popleft()
            logger.debug(
                "inject_expired session_key={} source={} age={}s",
                session_key, stale.source,
                int((now - stale.queued_at).total_seconds()),
            )
        if not q:
            self._pending.pop(session_key, None)

    # ------------------------------------------------------------------
    # Persistence serialization

    def _reload_from_store(self) -> None:
        if self._store is None:
            return
        blob = (self._store.load() or {}).get(self._STATE_KEY) or {}
        self._hydrate_from_blob(blob)

    def _hydrate_from_blob(self, blob: dict[str, Any]) -> None:
        self._pending = defaultdict(deque)
        for key, items in (blob.get("pending") or {}).items():
            if not isinstance(items, list):
                continue
            self._pending[key] = deque(
                _PendingInject(
                    message=i["message"],
                    source=i.get("source", ""),
                    queued_at=datetime.fromisoformat(i["queued_at"]),
                )
                for i in items
                if isinstance(i, dict) and "message" in i and "queued_at" in i
            )

    def _dump_to_blob(self) -> dict[str, Any]:
        return {
            "pending": {
                key: [
                    {
                        "message": p.message,
                        "source": p.source,
                        "queued_at": p.queued_at.isoformat(),
                    }
                    for p in q
                ]
                for key, q in self._pending.items() if q
            }
        }


__all__ = ["NudgeInjector"]
