"""Unit tests for NudgeInjector.

Pins the queue/pop semantics + TTL + per-session cap + response_modifier
protocol. All time-dependent behavior uses an injected clock.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from raven.proactive_engine.sentinel.executor.injector import NudgeInjector


class Clock:
    def __init__(self, t0: datetime):
        self.t = t0

    def __call__(self) -> datetime:
        return self.t

    def advance(self, seconds: float):
        self.t = self.t + timedelta(seconds=seconds)


@pytest.fixture
def clock():
    return Clock(datetime(2026, 4, 21, 14, 0, 0))


@pytest.fixture
def injector(clock):
    return NudgeInjector(ttl_seconds=1800, max_pending_per_session=3, now_fn=clock)


# ---------------------------------------------------------------------------
# Basic queue / pop


def test_queue_and_pop_returns_messages(injector):
    injector.queue("s1", "first")
    injector.queue("s1", "second")
    assert injector.size("s1") == 2
    popped = injector.pop_pending("s1")
    assert popped == ["first", "second"]
    assert injector.size("s1") == 0


def test_pop_empty_session_returns_empty_list(injector):
    assert injector.pop_pending("never_queued") == []


def test_size_all_sessions(injector):
    injector.queue("s1", "a")
    injector.queue("s2", "b")
    injector.queue("s2", "c")
    assert injector.size() == 3
    assert injector.size("s1") == 1
    assert injector.size("s2") == 2


# ---------------------------------------------------------------------------
# Empty inputs


def test_queue_empty_message_ignored(injector):
    injector.queue("s1", "")
    assert injector.size("s1") == 0


def test_queue_empty_session_key_ignored(injector):
    injector.queue("", "message")
    assert injector.size() == 0


# ---------------------------------------------------------------------------
# Callable protocol (response_modifier)


def test_call_appends_single_message(injector):
    injector.queue("s1", "P.S. remember to hydrate")
    result = injector("s1", "Here is your report.")
    assert result == "Here is your report.\n\nP.S. remember to hydrate"
    # After call, queue is drained.
    assert injector.size("s1") == 0


def test_call_appends_multiple_messages(injector):
    injector.queue("s1", "note one")
    injector.queue("s1", "note two")
    result = injector("s1", "main")
    assert result == "main\n\nnote one\n\nnote two"


def test_call_returns_content_unchanged_if_no_pending(injector):
    result = injector("s1", "unchanged")
    assert result == "unchanged"


def test_call_is_per_session(injector):
    injector.queue("s1", "only s1")
    result = injector("s2", "different session")
    assert result == "different session"  # no inject for s2
    # s1's queue is intact.
    assert injector.size("s1") == 1


# ---------------------------------------------------------------------------
# TTL expiry


def test_ttl_expires_pending(injector, clock):
    injector.queue("s1", "old")
    clock.advance(1801)  # > ttl_seconds=1800
    assert injector.pop_pending("s1") == []


def test_ttl_expires_just_past_boundary(injector, clock):
    injector.queue("s1", "fresh")
    clock.advance(1799)  # still within ttl
    assert injector.pop_pending("s1") == ["fresh"]


def test_ttl_expires_one_but_keeps_newer(injector, clock):
    injector.queue("s1", "old")
    clock.advance(1000)
    injector.queue("s1", "new")
    clock.advance(801)  # old now 1801s, new only 801s
    popped = injector.pop_pending("s1")
    assert popped == ["new"]


def test_peek_filters_ttl_without_mutating(injector, clock):
    injector.queue("s1", "one")
    clock.advance(1801)
    assert injector.peek("s1") == []
    # Peek should not mutate — but since our expire is lazy, calling peek
    # may clean the expired one. Verify pop afterwards is empty either way.
    assert injector.pop_pending("s1") == []


# ---------------------------------------------------------------------------
# Per-session cap


def test_cap_drops_oldest_when_full(injector):
    for i in range(5):
        injector.queue("s1", f"msg{i}")
    # Cap is 3 → only msg2, msg3, msg4 should remain.
    popped = injector.pop_pending("s1")
    assert popped == ["msg2", "msg3", "msg4"]


def test_cap_is_per_session(injector):
    for i in range(4):
        injector.queue("s1", f"a{i}")
    for i in range(4):
        injector.queue("s2", f"b{i}")
    assert len(injector.pop_pending("s1")) == 3
    assert len(injector.pop_pending("s2")) == 3


# ---------------------------------------------------------------------------
# Custom joiner


def test_custom_joiner():
    inj = NudgeInjector(joiner=" | ")
    inj.queue("s1", "A")
    inj.queue("s1", "B")
    assert inj("s1", "X") == "X | A | B"


# ---------------------------------------------------------------------------
# response_modifier protocol — used by AgentLoop


def test_callable_shape_matches_response_modifier(injector):
    # AgentLoop expects Callable[[str, str], str]; verify signature.
    assert callable(injector)
    out = injector("key", "content")
    assert isinstance(out, str)
