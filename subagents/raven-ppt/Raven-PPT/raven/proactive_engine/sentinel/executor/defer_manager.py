"""DeferManager — executes `action=nudge_defer` decisions.

A defer decision says: "user is mid-task on a different topic; wait until
this session's current thread settles, then send ``nudge_message`` as a
follow-up." This module holds pending defers, periodically checks whether
the target session has gone idle long enough, and dispatches via the
existing NudgeDispatcher.

Settlement detection (v1): **time-based idle** using
``Session.updated_at``. A session is "settled" when
``now - session.updated_at >= idle_threshold_seconds``. This is a simple
heuristic; v2 may add LLM-evaluated defer_condition (plan.md).

Lifecycle:
- ``register(decision, target_session, callback_on_dispatch=None) -> defer_id``
  Queues the decision. Returns a defer_id caller can use to cancel or track.
- ``cancel(defer_id) -> bool``
  Remove a pending defer.
- ``await tick()``
  One-shot: sweep pending and dispatch any that are settled + not expired.
  Callers that want a background loop use ``await run_forever()`` or
  ``start_background()``.
- ``pending_ids()`` / ``pending_count()`` — introspection.

Known limitations (v1):
- Not persisted — process restart loses all pending defers (documented).
- "Settled" is purely time-based (no LLM eval of defer_condition).
"""

from __future__ import annotations

import asyncio
import heapq
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Awaitable, Callable

from loguru import logger

from raven.proactive_engine.sentinel.executor.dispatcher import ExecutionResult, NudgeDispatcher
from raven.proactive_engine.sentinel.feedback.persistence import JsonStateStore
from raven.proactive_engine.sentinel.types import PlannerDecision


@dataclass(order=True)
class _PendingDefer:
    # Ordered by next_check_at for heap; non-ordering fields after field(compare=False).
    next_check_at: datetime
    defer_id: str = field(compare=False)
    decision: PlannerDecision = field(compare=False)
    target_session: str = field(compare=False)
    queued_at: datetime = field(compare=False)
    max_wait_until: datetime = field(compare=False)
    on_dispatch: Callable[[ExecutionResult], Awaitable[None]] | None = field(default=None, compare=False)


# Async session lookup surface. Keeping this a narrow Protocol so
# DeferManager doesn't depend directly on SessionManager (easier to test).
class _SessionLike:
    updated_at: datetime


SessionLookup = Callable[[str], _SessionLike | None]


class DeferManager:
    """Priority-queue based defer manager with async tick loop.

    Intentionally unaware of threading and event loops until ``start_background``
    is called — tests can drive it synchronously via ``await tick()``.
    """

    _STATE_KEY = "defer"

    def __init__(
        self,
        dispatcher: NudgeDispatcher,
        session_lookup: SessionLookup,
        *,
        idle_threshold_seconds: int = 300,
        max_wait_seconds: int = 86400,
        default_sleep_seconds: float = 60.0,
        now_fn: Callable[[], datetime] | None = None,
        store: JsonStateStore | None = None,
    ) -> None:
        """``store`` is an optional JsonStateStore used to persist the pending
        defer heap across processes. Without it, pending defers are lost on
        process restart (v1 behavior)."""
        self.dispatcher = dispatcher
        self.session_lookup = session_lookup
        self.idle_threshold = timedelta(seconds=idle_threshold_seconds)
        self.max_wait = timedelta(seconds=max_wait_seconds)
        self.default_sleep = default_sleep_seconds
        self._now_fn = now_fn or datetime.now
        self._store = store
        self._heap: list[_PendingDefer] = []
        self._by_id: dict[str, _PendingDefer] = {}
        self._stopped = False
        self._wakeup: asyncio.Event | None = None
        # Late-bound by SentinelRunner so a defer fires through the same routing
        # as a direct nudge. Resolved at fire time, not register time, since
        # session/channel state can change while waiting.
        self._resolve_targets: Callable[[str], list[tuple[str, str]]] | None = None

        if self._store is not None:
            self._reload_from_store()

    def set_target_resolver(
        self,
        resolver: Callable[[str], list[tuple[str, str]]],
    ) -> None:
        """Inject the nudge target resolver (see ``_resolve_targets``)."""
        self._resolve_targets = resolver

    # ------------------------------------------------------------------
    # Public API

    def register(
        self,
        decision: PlannerDecision,
        target_session: str,
        *,
        on_dispatch: Callable[[ExecutionResult], Awaitable[None]] | None = None,
        max_wait_seconds: int | None = None,
    ) -> str:
        """Queue a defer decision. Returns a defer_id."""
        if decision.action != "nudge_defer":
            raise ValueError(f"register() expects action=nudge_defer, got {decision.action!r}")
        if not decision.nudge_message:
            raise ValueError("nudge_defer decision must have nudge_message")
        if not target_session:
            raise ValueError("target_session required")

        now = self._now_fn()
        mw = timedelta(seconds=max_wait_seconds) if max_wait_seconds is not None else self.max_wait
        defer_id = uuid.uuid4().hex[:12]
        # First check happens after idle_threshold (earliest possible settled moment).
        first_check = now + self.idle_threshold

        entry = _PendingDefer(
            next_check_at=first_check,
            defer_id=defer_id,
            decision=decision,
            target_session=target_session,
            queued_at=now,
            max_wait_until=now + mw,
            on_dispatch=on_dispatch,
        )
        heapq.heappush(self._heap, entry)
        self._by_id[defer_id] = entry
        logger.info(
            "defer_registered id={} session={} first_check_in={}s max_wait={}s",
            defer_id,
            target_session,
            int(self.idle_threshold.total_seconds()),
            int(mw.total_seconds()),
        )
        self._persist()
        self._signal_wakeup()
        return defer_id

    def cancel(self, defer_id: str) -> bool:
        """Remove a pending defer. Returns True if found and removed."""
        entry = self._by_id.pop(defer_id, None)
        if entry is None:
            return False
        # Lazy heap cleanup — we rely on pop-time skip via _by_id check.
        logger.info("defer_cancelled id={}", defer_id)
        self._persist()
        return True

    def pending_count(self) -> int:
        return len(self._by_id)

    def pending_ids(self) -> list[str]:
        return list(self._by_id.keys())

    # ------------------------------------------------------------------
    # Tick — one-shot; unit-testable

    async def tick(self) -> list[ExecutionResult]:
        """Sweep pending defers once. Returns ExecutionResult list for those
        that fired or expired this tick. Does NOT sleep.
        """
        now = self._now_fn()
        results: list[ExecutionResult] = []
        mutated = False

        while self._heap:
            top = self._heap[0]
            # Drop cancelled entries (lazy cleanup).
            if top.defer_id not in self._by_id:
                heapq.heappop(self._heap)
                mutated = True
                continue
            if top.next_check_at > now:
                break
            heapq.heappop(self._heap)
            mutated = True
            result = await self._evaluate(top, now)
            results.append(result)
            if top.on_dispatch:
                try:
                    await top.on_dispatch(result)
                except Exception as exc:
                    logger.warning(
                        "defer on_dispatch callback raised {}: {}",
                        type(exc).__name__,
                        exc,
                    )
        if mutated:
            self._persist()
        return results

    async def _evaluate(self, entry: _PendingDefer, now: datetime) -> ExecutionResult:
        # Expired past max_wait — give up.
        if now >= entry.max_wait_until:
            self._by_id.pop(entry.defer_id, None)
            logger.info(
                "defer_expired id={} session={} waited={}s",
                entry.defer_id,
                entry.target_session,
                int((now - entry.queued_at).total_seconds()),
            )
            return ExecutionResult(
                delivered=False,
                reason="max_wait_expired",
                defer_id=entry.defer_id,
                details={"session_key": entry.target_session, "waited_s": int((now - entry.queued_at).total_seconds())},
            )

        session = self.session_lookup(entry.target_session)
        if session is None:
            # Target session doesn't exist — treat as "settled" (no thread to interrupt).
            return await self._fire(entry, reason_code="no_session")

        # Compare wall-clock now against session.updated_at.
        idle_s = (now - session.updated_at).total_seconds()
        if idle_s >= self.idle_threshold.total_seconds():
            return await self._fire(entry, reason_code="idle_threshold_met")

        # Not yet settled — reschedule for the exact moment it would become idle.
        next_check = session.updated_at + self.idle_threshold
        # If session keeps being updated, next_check may be < now; clamp to now+default_sleep.
        if next_check <= now:
            next_check = now + timedelta(seconds=self.default_sleep)
        entry.next_check_at = next_check
        heapq.heappush(self._heap, entry)
        logger.debug(
            "defer_not_settled id={} session={} idle={}s next_check_in={}s",
            entry.defer_id,
            entry.target_session,
            int(idle_s),
            int((next_check - now).total_seconds()),
        )
        return ExecutionResult(
            delivered=False,
            reason="not_settled",
            defer_id=entry.defer_id,
            details={"session_key": entry.target_session, "idle_s": int(idle_s)},
        )

    async def _fire(self, entry: _PendingDefer, reason_code: str) -> ExecutionResult:
        # Build a plain-nudge decision with the same payload and dispatch.
        now = self._now_fn()
        self._by_id.pop(entry.defer_id, None)

        plain = PlannerDecision(
            action="nudge",
            reason=f"defer_fired[{reason_code}]: {entry.decision.reason}",
            priority=entry.decision.priority,
            proactivity_score=entry.decision.proactivity_score,
            target_session=entry.target_session,
            nudge_message=entry.decision.nudge_message,
        )
        if self._resolve_targets is None:
            logger.warning(
                "defer_fire id={} session={} — no target resolver wired, dropping",
                entry.defer_id,
                entry.target_session,
            )
            return ExecutionResult(
                delivered=False,
                reason="no_resolver",
                defer_id=entry.defer_id,
            )
        targets = self._resolve_targets(entry.target_session)
        if not targets:
            logger.warning(
                "defer_fire id={} session={} — no delivery target, dropping",
                entry.defer_id,
                entry.target_session,
            )
            return ExecutionResult(
                delivered=False,
                reason="no_delivery_target",
                defer_id=entry.defer_id,
            )
        result = await self.dispatcher.dispatch(plain, targets)
        result.defer_id = entry.defer_id
        # Annotate so downstream knows this was a defer-triggered fire.
        if result.details is None:
            result.details = {}
        result.details["defer_reason"] = reason_code
        result.details["defer_wait_s"] = int((now - entry.queued_at).total_seconds())
        logger.info(
            "defer_fired id={} session={} reason={} wait={}s",
            entry.defer_id,
            entry.target_session,
            reason_code,
            int((now - entry.queued_at).total_seconds()),
        )
        return result

    # ------------------------------------------------------------------
    # Background loop — optional

    async def run_forever(self) -> None:
        """Long-running loop. Sleeps between ticks based on next deadline."""
        self._stopped = False
        self._wakeup = asyncio.Event()
        while not self._stopped:
            await self.tick()
            await self._sleep_until_next()

    def stop(self) -> None:
        self._stopped = True
        self._signal_wakeup()

    def _signal_wakeup(self) -> None:
        if self._wakeup is not None:
            try:
                self._wakeup.set()
            except RuntimeError:
                pass  # loop not running

    async def _sleep_until_next(self) -> None:
        now = self._now_fn()
        next_deadline: datetime | None = None
        # Heap[0] might be a stale cancelled entry; find first live.
        for entry in self._heap:
            if entry.defer_id in self._by_id:
                next_deadline = entry.next_check_at
                break
        sleep_s = self.default_sleep if next_deadline is None else max(0.0, (next_deadline - now).total_seconds())
        try:
            assert self._wakeup is not None
            await asyncio.wait_for(self._wakeup.wait(), timeout=sleep_s)
            self._wakeup.clear()
        except asyncio.TimeoutError:
            pass

    # ------------------------------------------------------------------
    # Persistence serialization

    def _reload_from_store(self) -> None:
        if self._store is None:
            return
        blob = (self._store.load() or {}).get(self._STATE_KEY) or {}
        entries_data = blob.get("entries") or []
        self._heap = []
        self._by_id = {}
        for d in entries_data:
            try:
                entry = _PendingDefer(
                    next_check_at=datetime.fromisoformat(d["next_check_at"]),
                    defer_id=d["defer_id"],
                    decision=_decision_from_blob(d["decision"]),
                    target_session=d["target_session"],
                    queued_at=datetime.fromisoformat(d["queued_at"]),
                    max_wait_until=datetime.fromisoformat(d["max_wait_until"]),
                    on_dispatch=None,  # callbacks not serializable; lost on reload
                )
            except (KeyError, TypeError, ValueError) as exc:
                logger.warning("defer entry skipped on reload: {}", exc)
                continue
            heapq.heappush(self._heap, entry)
            self._by_id[entry.defer_id] = entry

    def _persist(self) -> None:
        if self._store is None:
            return
        blob = self._dump_to_blob()

        def _mutate(state: dict[str, Any]) -> dict[str, Any]:
            state[self._STATE_KEY] = blob
            return state

        self._store.update(_mutate)

    def _dump_to_blob(self) -> dict[str, Any]:
        return {
            "entries": [
                {
                    "defer_id": e.defer_id,
                    "target_session": e.target_session,
                    "next_check_at": e.next_check_at.isoformat(),
                    "queued_at": e.queued_at.isoformat(),
                    "max_wait_until": e.max_wait_until.isoformat(),
                    "decision": _decision_to_blob(e.decision),
                }
                for e in self._heap
                if e.defer_id in self._by_id
            ],
        }


def _decision_to_blob(d: PlannerDecision) -> dict[str, Any]:
    return {
        "action": d.action,
        "reason": d.reason,
        "priority": d.priority,
        "proactivity_score": d.proactivity_score,
        "target_session": d.target_session,
        "nudge_message": d.nudge_message,
        "spawn_task": d.spawn_task,
        "defer_condition": d.defer_condition,
    }


def _decision_from_blob(blob: dict[str, Any]) -> PlannerDecision:
    return PlannerDecision(
        action=blob["action"],
        reason=blob.get("reason", ""),
        priority=blob.get("priority", "low"),
        proactivity_score=float(blob.get("proactivity_score", 0.0)),
        target_session=blob.get("target_session"),
        nudge_message=blob.get("nudge_message"),
        spawn_task=blob.get("spawn_task"),
        defer_condition=blob.get("defer_condition"),
    )


__all__ = ["DeferManager"]
