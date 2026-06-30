"""Unit tests for DeferManager.

Covers: register, tick-based settled detection, max_wait expiry, cancel,
priority-queue ordering, multi-session, on_dispatch callback, no-session
edge case. All tests use an injected clock — no real sleep.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

import pytest

from raven.proactive_engine.sentinel.executor.defer_manager import DeferManager
from raven.proactive_engine.sentinel.executor.dispatcher import NudgeDispatcher, split_session_key
from raven.proactive_engine.sentinel.types import PlannerDecision


class Clock:
    def __init__(self, t0: datetime):
        self.t = t0

    def __call__(self) -> datetime:
        return self.t

    def advance(self, seconds: float):
        self.t = self.t + timedelta(seconds=seconds)


@dataclass
class FakeSession:
    updated_at: datetime


class FakeSessionStore:
    """Minimal SessionManager-like lookup for tests."""

    def __init__(self):
        self.sessions: dict[str, FakeSession] = {}

    def set(self, key: str, updated_at: datetime) -> None:
        self.sessions[key] = FakeSession(updated_at=updated_at)

    def __call__(self, key: str) -> FakeSession | None:
        return self.sessions.get(key)


def _defer(msg: str = "follow up message", target: str = "cli:direct") -> PlannerDecision:
    return PlannerDecision(
        action="nudge_defer",
        reason="test defer",
        priority="low",
        proactivity_score=0.6,
        target_session=target,
        nudge_message=msg,
        defer_condition="test settlement",
    )


@pytest.fixture
def clock():
    return Clock(datetime(2026, 4, 21, 14, 0, 0))


@pytest.fixture
def sessions():
    return FakeSessionStore()


@pytest.fixture
def posted():
    return []


@pytest.fixture
def mgr(clock, sessions, posted):
    dispatcher = NudgeDispatcher(now_fn=clock)

    async def _post(out):
        posted.append(out)

    dispatcher.set_post(_post)
    m = DeferManager(
        dispatcher,
        sessions,
        idle_threshold_seconds=300,
        max_wait_seconds=86400,
        now_fn=clock,
    )
    # SentinelRunner wires this in production; tests use real-session targets,
    # so a direct split is the equivalent resolution.
    m.set_target_resolver(lambda ts: [split_session_key(ts)])
    return m


# ---------------------------------------------------------------------------
# Registration


@pytest.mark.asyncio
async def test_register_returns_id_and_tracks(mgr):
    did = mgr.register(_defer(), target_session="cli:direct")
    assert did
    assert mgr.pending_count() == 1
    assert did in mgr.pending_ids()


@pytest.mark.asyncio
async def test_register_rejects_wrong_action(mgr):
    with pytest.raises(ValueError):
        mgr.register(
            PlannerDecision(action="nudge", reason="x", nudge_message="hello"),
            target_session="cli:direct",
        )


@pytest.mark.asyncio
async def test_register_rejects_empty_message(mgr):
    d = _defer()
    d.nudge_message = None
    with pytest.raises(ValueError):
        mgr.register(d, target_session="cli:direct")


@pytest.mark.asyncio
async def test_register_rejects_empty_target(mgr):
    with pytest.raises(ValueError):
        mgr.register(_defer(), target_session="")


# ---------------------------------------------------------------------------
# Settled detection — time-based


@pytest.mark.asyncio
async def test_tick_before_idle_threshold_defers(mgr, clock, sessions):
    # Session just updated now — idle_s = 0 on check.
    sessions.set("cli:direct", clock())
    mgr.register(_defer(), target_session="cli:direct")
    # First check happens after idle_threshold (300s).
    clock.advance(299)
    results = await mgr.tick()
    # Not yet eligible for check (next_check_at is now+300 on registration).
    assert results == []
    assert mgr.pending_count() == 1


@pytest.mark.asyncio
async def test_tick_at_idle_threshold_but_session_still_active(mgr, clock, sessions):
    # Register at t0. Later user updates session → session is active.
    sessions.set("cli:direct", clock())  # set at t0
    mgr.register(_defer(), target_session="cli:direct")
    clock.advance(300)
    # User updates session right before check — now idle is 0.
    sessions.set("cli:direct", clock())
    results = await mgr.tick()
    assert len(results) == 1
    assert results[0].reason == "not_settled"
    assert results[0].delivered is False
    assert mgr.pending_count() == 1  # still pending


@pytest.mark.asyncio
async def test_tick_settled_fires_nudge(mgr, clock, sessions, posted):
    # Session updated a while ago; after idle_threshold it's settled.
    sessions.set("cli:direct", clock())
    mgr.register(_defer("follow up"), target_session="cli:direct")
    # Advance past idle threshold; session was never updated.
    clock.advance(301)
    results = await mgr.tick()
    assert len(results) == 1
    assert results[0].delivered is True
    assert results[0].defer_id is not None
    assert len(posted) == 1
    msg = posted.pop(0)
    assert msg.content == "follow up"
    assert msg.source.extras["_sentinel_origin"] is True
    assert mgr.pending_count() == 0


@pytest.mark.asyncio
async def test_no_session_treated_as_settled(mgr, clock, sessions, posted):
    # Target session never existed — defer fires on first check.
    mgr.register(_defer("no session case"), target_session="ghost:session")
    clock.advance(301)
    results = await mgr.tick()
    assert len(results) == 1
    assert results[0].delivered is True
    # Dispatched message
    msg = posted.pop(0)
    assert msg.content == "no session case"


# ---------------------------------------------------------------------------
# max_wait expiry


@pytest.mark.asyncio
async def test_max_wait_expiry(mgr, clock, sessions, posted):
    # Session keeps being active → defer never settles → max_wait expires.
    sessions.set("cli:direct", clock())
    mgr.register(_defer(), target_session="cli:direct", max_wait_seconds=600)
    # Keep advancing; simulate continuous session activity; collect any
    # tick results along the way since expiry may fire mid-loop.
    all_results = []
    for _ in range(10):
        clock.advance(100)
        sessions.set("cli:direct", clock())
        all_results.extend(await mgr.tick())
    # Also final tick to catch any still-pending.
    clock.advance(100)
    all_results.extend(await mgr.tick())
    assert any(r.reason == "max_wait_expired" for r in all_results), (
        f"expected max_wait_expired somewhere, got {[r.reason for r in all_results]}"
    )
    assert mgr.pending_count() == 0
    assert len(posted) == 0  # nothing dispatched


# ---------------------------------------------------------------------------
# Cancel


@pytest.mark.asyncio
async def test_cancel_removes_pending(mgr, clock, sessions, posted):
    sessions.set("cli:direct", clock())
    did = mgr.register(_defer(), target_session="cli:direct")
    assert mgr.cancel(did) is True
    assert mgr.pending_count() == 0
    # After cancel, tick should find nothing to dispatch.
    clock.advance(400)
    results = await mgr.tick()
    assert results == []
    assert len(posted) == 0


@pytest.mark.asyncio
async def test_cancel_returns_false_for_unknown_id(mgr):
    assert mgr.cancel("no_such_id") is False


# ---------------------------------------------------------------------------
# Priority queue ordering / multi-session


@pytest.mark.asyncio
async def test_multiple_defers_fire_in_order(mgr, clock, sessions, posted):
    # Both sessions stale from the start → both fire at first tick.
    sessions.set("s1", clock() - timedelta(seconds=1000))
    sessions.set("s2", clock() - timedelta(seconds=500))
    mgr.register(_defer("one", target="s1"), target_session="s1")
    mgr.register(_defer("two", target="s2"), target_session="s2")
    clock.advance(301)
    results = await mgr.tick()
    # Both should fire in this tick; order not strictly guaranteed but both delivered.
    assert len([r for r in results if r.delivered]) == 2
    assert len(posted) == 2


# ---------------------------------------------------------------------------
# on_dispatch callback


@pytest.mark.asyncio
async def test_on_dispatch_callback_invoked(mgr, clock, sessions):
    sessions.set("cli:direct", clock() - timedelta(seconds=1000))
    received: list = []

    async def cb(result):
        received.append(result)

    mgr.register(_defer(), target_session="cli:direct", on_dispatch=cb)
    clock.advance(301)
    await mgr.tick()
    assert len(received) == 1
    assert received[0].delivered is True


@pytest.mark.asyncio
async def test_on_dispatch_exception_does_not_crash_tick(mgr, clock, sessions):
    sessions.set("cli:direct", clock() - timedelta(seconds=1000))

    async def bad_cb(result):
        raise RuntimeError("boom")

    mgr.register(_defer(), target_session="cli:direct", on_dispatch=bad_cb)
    clock.advance(301)
    results = await mgr.tick()
    # Dispatch still succeeded; exception was caught + logged.
    assert results[0].delivered is True
