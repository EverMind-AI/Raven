"""AgentLoop ``backend`` wiring + ``_dispatch_backend_store``.

The two after-turn callsites (system-message path + REPL path) now call
:meth:`AgentLoop._dispatch_backend_store` as the third peer step in the
after-turn pipeline (alongside ``context_engine.after_turn`` and
``maybe_consolidate``). This file exercises the dispatcher in isolation
— the full end-to-end "AgentLoop processes a turn and the backend
ultimately sees it" path is left to integration tests that wire a real
LLM provider; here we keep things small + focused.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from raven.agent.loop import AgentLoop
from raven.tracing import spans as _spans
from raven.tracing import trace


@pytest.fixture
def trace_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("RAVEN_TRACING", "1")
    monkeypatch.setenv("RAVEN_TRACING_DIR", str(tmp_path / "traces"))
    _spans._store = None  # force the store to re-init against the temp dir
    yield tmp_path / "traces"
    _spans._store = None


def _spans_written(trace_dir: Path) -> list[dict]:
    log = trace_dir / "logs" / "audit-spans.log"
    if not log.exists():
        return []
    return [json.loads(line) for line in log.read_text().splitlines() if line.strip()]


# ---------------------------------------------------------------------------
# Stubs
# ---------------------------------------------------------------------------


class _StubProvider:
    api_key = "test"

    def get_default_model(self) -> str:
        return "stub"

    async def chat(self, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError("not invoked in this dispatcher smoke test")

    async def chat_with_retry(self, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError("not invoked in this dispatcher smoke test")


class _FakeBackend:
    def __init__(self) -> None:
        self.store_calls: list[dict[str, Any]] = []
        self.store_raises: Exception | None = None

    async def start(self) -> None:
        pass

    async def stop(self) -> None:
        pass

    async def feedback(self, signals):
        pass

    async def recall(self, query, *, user_id=None, agent_id=None, top_k):
        return []

    async def store(self, session_id, messages, *, metadata=None):
        self.store_calls.append(
            {
                "session_id": session_id,
                "messages": messages,
            }
        )
        if self.store_raises is not None:
            raise self.store_raises


def _make_loop(workspace: Path, *, backend=None) -> AgentLoop:
    return AgentLoop(
        provider=_StubProvider(),
        workspace=workspace,
        model="stub",
        max_iterations=2,
        restrict_to_workspace=True,
        backend=backend,
    )


# ---------------------------------------------------------------------------
# Constructor wiring
# ---------------------------------------------------------------------------


class TestConstructorWiring:
    def test_default_backend_is_none(self, tmp_path: Path) -> None:
        agent = _make_loop(tmp_path)
        assert agent.backend is None

    def test_explicit_backend_stored(self, tmp_path: Path) -> None:
        b = _FakeBackend()
        agent = _make_loop(tmp_path, backend=b)
        assert agent.backend is b


# ---------------------------------------------------------------------------
# _dispatch_backend_store
# ---------------------------------------------------------------------------


class TestDispatcher:
    async def test_no_backend_is_noop(self, tmp_path: Path) -> None:
        agent = _make_loop(tmp_path, backend=None)
        agent._dispatch_backend_store("session-1", [{"role": "user", "content": "hi"}])

    async def test_empty_messages_skips_backend(self, tmp_path: Path) -> None:
        b = _FakeBackend()
        agent = _make_loop(tmp_path, backend=b)
        agent._dispatch_backend_store("session-1", [])
        await agent.drain_backend_stores(timeout=5.0)
        assert b.store_calls == []

    async def test_calls_backend_store_with_full_slice(self, tmp_path: Path) -> None:
        b = _FakeBackend()
        agent = _make_loop(tmp_path, backend=b)
        slice_ = [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "there"},
        ]
        agent._dispatch_backend_store("session-key-x", slice_)
        await agent.drain_backend_stores(timeout=5.0)
        assert len(b.store_calls) == 1
        call = b.store_calls[0]
        assert call["session_id"] == "session-key-x"
        assert call["messages"] == slice_

    async def test_backend_exception_is_retried_not_raised(self, tmp_path: Path, monkeypatch) -> None:
        """A backend failure must not derail the AgentLoop. The turn is already
        saved to the session log; plugin-side indexing is best-effort."""
        from raven.memory_engine import store_pipeline

        monkeypatch.setattr(store_pipeline, "BACKOFF_S", (0.01, 0.01, 0.01, 0.01))
        b = _FakeBackend()
        b.store_raises = RuntimeError("evermem down")
        agent = _make_loop(tmp_path, backend=b)
        agent._dispatch_backend_store("s", [{"role": "user", "content": "x"}])

        worker = agent._store_pipeline._workers["s"]
        await worker

        assert len(b.store_calls) == 5
        assert agent._store_pipeline.dropped == 1


# ---------------------------------------------------------------------------
# Legacy compatibility -- callsites predating the backend keyword still pass
# ---------------------------------------------------------------------------


class TestLegacyCompat:
    def test_construction_without_backend_unchanged(
        self,
        tmp_path: Path,
    ) -> None:
        """Construction without the ``backend=`` keyword still works
        end-to-end. After Phase B-3 the ``self.memory`` facade is gone;
        we now assert against the direct subsystem fields AgentLoop
        holds (``memory_consolidator`` + ``context.skills``)."""
        from raven.memory_engine.consolidate.consolidator import (
            MemoryConsolidator,
        )

        agent = _make_loop(tmp_path)
        assert isinstance(agent.memory_consolidator, MemoryConsolidator)
        assert agent.context.skills is not None
        assert agent.backend is None


class TestTheTurnNeverWaitsOnIndexing:
    """Dispatch is an enqueue, not a write.

    The turn used to await the write for up to five seconds, which put the
    memory backend's latency in front of ``message.complete`` and therefore
    in front of the user getting their input box back.
    """

    async def test_dispatch_returns_before_the_write_lands(self, tmp_path: Path) -> None:
        import asyncio

        started = asyncio.Event()
        release = asyncio.Event()

        class _Slow:
            def __init__(self) -> None:
                self.finished = False

            async def store(self, session_id, messages, **kw):
                started.set()
                await release.wait()
                self.finished = True
                return True

        b = _Slow()
        agent = _make_loop(tmp_path, backend=b)

        agent._dispatch_backend_store("s", [{"role": "user", "content": "hi"}])
        assert not b.finished, "dispatch must not wait on the write"

        await asyncio.sleep(0.05)
        assert started.is_set(), "the worker should have picked the record up"

        release.set()
        await agent.drain_backend_stores(timeout=5.0)
        assert b.finished

    async def test_same_session_writes_stay_in_order(self, tmp_path: Path) -> None:
        import asyncio

        seen: list[str] = []

        class _Jittery:
            async def store(self, session_id, messages, **kw):
                n = messages[0]["content"]
                await asyncio.sleep(0.05 if n == "1" else 0.0)
                seen.append(n)
                return True

        agent = _make_loop(tmp_path, backend=_Jittery())
        for n in ("1", "2", "3"):
            agent._dispatch_backend_store("s", [{"role": "user", "content": n}])

        await agent.drain_backend_stores(timeout=5.0)
        assert seen == ["1", "2", "3"]

    async def test_ten_quick_turns_all_land(self, tmp_path: Path) -> None:
        """The old in-flight cap cancelled writes once four were outstanding,
        so a fast typist silently lost turns."""
        import asyncio

        landed: list[str] = []

        class _Slowish:
            async def store(self, session_id, messages, **kw):
                await asyncio.sleep(0.01)
                landed.append(messages[0]["content"])
                return True

        agent = _make_loop(tmp_path, backend=_Slowish())
        for i in range(10):
            agent._dispatch_backend_store("s", [{"role": "user", "content": str(i)}])

        await agent.drain_backend_stores(timeout=10.0)
        assert landed == [str(i) for i in range(10)]

    async def test_a_failing_write_is_retried_five_times_then_dropped(self, tmp_path: Path, monkeypatch) -> None:
        from raven.memory_engine import store_pipeline

        monkeypatch.setattr(store_pipeline, "BACKOFF_S", (0.01, 0.01, 0.01, 0.01))

        class _Never:
            def __init__(self) -> None:
                self.attempts = 0

            async def store(self, session_id, messages, **kw):
                self.attempts += 1
                return False

        b = _Never()
        agent = _make_loop(tmp_path, backend=b)
        agent._dispatch_backend_store("s", [{"role": "user", "content": "x"}])

        worker = agent._store_pipeline._workers["s"]
        await worker

        assert b.attempts == 5
        assert agent._store_pipeline.dropped == 1

    async def test_retries_are_numbered_by_attempt(self, tmp_path: Path, monkeypatch) -> None:
        """A backend that needs to tell a retry apart from a fresh turn (a
        turn-based flush cadence, notably) needs to know which attempt this
        is. The worker must report it 0-indexed, one number per try."""
        from raven.memory_engine import store_pipeline

        monkeypatch.setattr(store_pipeline, "BACKOFF_S", (0.01, 0.01, 0.01, 0.01))

        class _AlwaysFails:
            def __init__(self) -> None:
                self.attempts: list[int | None] = []

            async def store(self, session_id, messages, *, metadata=None):
                self.attempts.append(metadata.get("attempt") if metadata else None)
                return False

        b = _AlwaysFails()
        agent = _make_loop(tmp_path, backend=b)
        agent._dispatch_backend_store("s", [{"role": "user", "content": "x"}])

        worker = agent._store_pipeline._workers["s"]
        await worker

        assert b.attempts == [0, 1, 2, 3, 4]

    async def test_a_backend_that_reports_nothing_is_not_retried(self, tmp_path: Path) -> None:
        """Only an explicit ``False`` means the write failed. A backend that
        returns ``None`` never claimed failure, so retrying it would invent one."""
        b = _FakeBackend()
        agent = _make_loop(tmp_path, backend=b)
        agent._dispatch_backend_store("s", [{"role": "user", "content": "hi"}])

        await agent.drain_backend_stores(timeout=5.0)
        assert len(b.store_calls) == 1
        assert agent._store_pipeline.dropped == 0

    async def test_a_run_of_failing_turns_still_raises_the_health_alarm(self, tmp_path: Path, monkeypatch) -> None:
        """Retries must not drown the alarm, and must not trip it early.

        The alarm counts turns whose write failed. One record burning five
        retries is one failed turn, so it must report exactly once.
        """
        import asyncio

        from raven.memory_engine import store_pipeline

        monkeypatch.setattr(store_pipeline, "BACKOFF_S", (0.01, 0.01, 0.01, 0.01))
        b = _FakeBackend()
        b.store_raises = RuntimeError("everos unreachable")
        agent = _make_loop(tmp_path, backend=b)
        events: list[tuple[str, dict]] = []

        async def sink(method: str, params: dict) -> None:
            events.append((method, params))

        agent.set_mcp_event_sink(sink)

        msg = [{"role": "user", "content": "x"}]
        for _ in range(agent._MEMORY_FAILURES_BEFORE_ALARM + 2):
            agent._dispatch_backend_store("s", msg)
        await agent._store_pipeline._workers["s"]
        await asyncio.sleep(0)

        alarms = [p for m, p in events if m == "memory.health"]
        assert len(alarms) == 1, f"expected one alarm for {agent._MEMORY_FAILURES_BEFORE_ALARM} failed turns"
        assert alarms[0]["ok"] is False
        assert "everos unreachable" in alarms[0]["error"]

        b.store_raises = None
        agent._dispatch_backend_store("s", msg)
        await agent._store_pipeline._workers["s"]
        await asyncio.sleep(0)

        recovered = [p for m, p in events if m == "memory.health"]
        assert len(recovered) == 2
        assert recovered[1]["ok"] is True
        assert agent._memory_fail_streak == 0

    async def test_the_queue_drops_the_oldest_when_full(self, tmp_path: Path, monkeypatch) -> None:
        from raven.memory_engine import store_pipeline

        monkeypatch.setattr(store_pipeline, "MAX_QUEUED", 4)
        landed: list[str] = []

        class _Recording:
            async def store(self, session_id, messages, **kw):
                landed.append(messages[0]["content"])
                return True

        agent = _make_loop(tmp_path, backend=_Recording())
        # All ten enqueues happen with no await between them, so the worker
        # cannot start draining until the loop yields -- the queue therefore
        # sees all ten and keeps only the newest four.
        for i in range(10):
            agent._dispatch_backend_store("s", [{"role": "user", "content": str(i)}])

        await agent.drain_backend_stores(timeout=5.0)
        assert landed == ["6", "7", "8", "9"]
        assert agent._store_pipeline.dropped == 6

    async def test_overflow_trips_the_health_alarm(self, tmp_path: Path, monkeypatch) -> None:
        """Before this, ``_note_memory_ok()`` reset the failure streak on
        every landed write while overflow silently shed the oldest turn on
        every enqueue -- so a backend that eventually lands every write, just
        too slowly, could shed turns forever without ever tripping the alarm.
        """
        import asyncio

        from raven.memory_engine import store_pipeline

        monkeypatch.setattr(store_pipeline, "MAX_QUEUED", 4)
        events: list[tuple[str, dict]] = []

        async def sink(method: str, params: dict) -> None:
            events.append((method, params))

        class _Recording:
            async def store(self, session_id, messages, **kw):
                return True

        agent = _make_loop(tmp_path, backend=_Recording())
        agent.set_mcp_event_sink(sink)

        for i in range(10):
            agent._dispatch_backend_store("s", [{"role": "user", "content": str(i)}])
        await agent.drain_backend_stores(timeout=5.0)
        await asyncio.sleep(0)

        failures = [p for m, p in events if m == "memory.health" and p["ok"] is False]
        assert len(failures) == 1
        assert "queue limit" in failures[0]["error"]


class TestEachWriteIsTracedToItsOwnTurn:
    """The worker is long-lived; the trace context must not be.

    ``asyncio.create_task`` snapshots contextvars, so a worker spawned inside
    turn 1's ``memory.enqueue`` span kept that span as the parent of every
    later turn's write: turn 40's ``memory.store`` was emitted into turn 1's
    trace, and turn 40's own trace showed an enqueue with nothing under it.
    """

    async def test_stores_are_attributed_to_the_enqueuing_turn(self, tmp_path: Path, trace_dir: Path) -> None:
        b = _FakeBackend()
        agent = _make_loop(tmp_path, backend=b)

        # No await between the two turns, so the worker spawned by the first
        # is still the one that drains the second -- the exact shape that used
        # to glue both writes onto the first turn's trace.
        with trace.span("session.turn", session_key="s") as first:
            agent._dispatch_backend_store("s", [{"role": "user", "content": "1"}])
        with trace.span("session.turn", session_key="s") as second:
            agent._dispatch_backend_store("s", [{"role": "user", "content": "2"}])

        await agent.drain_backend_stores(timeout=5.0)
        assert len(b.store_calls) == 2

        written = _spans_written(trace_dir)
        stores = [sp for sp in written if sp["name"] == "memory.store"]
        enqueues = [sp for sp in written if sp["name"] == "memory.enqueue"]
        assert len(stores) == 2
        assert len(enqueues) == 2

        assert {sp["traceId"] for sp in stores} == {first.trace_id, second.trace_id}
        enqueue_by_trace = {sp["traceId"]: sp for sp in enqueues}
        for sp in stores:
            assert sp["parentSpanId"] == enqueue_by_trace[sp["traceId"]]["spanId"]


class TestShutdownDoesNotBlockTheUser:
    """Quitting is the user's action; it must not wait on bookkeeping.

    The budget only covers a request genuinely on the wire. A worker parked in
    a retry backoff is doing nothing, and must not be allowed to spend it.
    """

    async def test_drain_returns_within_its_budget_while_a_write_is_backing_off(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        import asyncio
        import time

        from raven.memory_engine import store_pipeline

        monkeypatch.setattr(store_pipeline, "BACKOFF_S", (30.0, 30.0, 30.0, 30.0))

        class _Failing:
            async def store(self, session_id, messages, **kw):
                return False

        agent = _make_loop(tmp_path, backend=_Failing())
        agent._dispatch_backend_store("s", [{"role": "user", "content": "x"}])
        await asyncio.sleep(0.05)  # let the worker reach its first backoff

        t0 = time.monotonic()
        await agent.drain_backend_stores()
        elapsed = time.monotonic() - t0

        assert elapsed < 1.0, f"drain spent {elapsed:.1f}s waiting on a sleeping worker"
        assert agent._store_pipeline.dropped == 1

    async def test_drain_still_lets_a_quick_write_land(self, tmp_path: Path) -> None:
        b = _FakeBackend()
        agent = _make_loop(tmp_path, backend=b)
        agent._dispatch_backend_store("s", [{"role": "user", "content": "hi"}])

        await agent.drain_backend_stores()
        assert len(b.store_calls) == 1

    async def test_drain_gives_up_on_a_write_that_outlives_the_budget(self, tmp_path: Path) -> None:
        import asyncio
        import time

        class _Wedged:
            async def store(self, session_id, messages, **kw):
                await asyncio.sleep(30)
                return True

        agent = _make_loop(tmp_path, backend=_Wedged())
        agent._dispatch_backend_store("s", [{"role": "user", "content": "x"}])

        t0 = time.monotonic()
        await agent.drain_backend_stores(timeout=0.2)
        assert time.monotonic() - t0 < 1.0

        for task in list(agent._store_pipeline._workers.values()):
            task.cancel()

    async def test_drain_gives_up_on_a_write_that_swallows_its_cancellation(self, tmp_path: Path) -> None:
        """The budgeted wait is only half the drain.

        What follows it cancels the leftover workers and collects them, and
        `cancel` merely schedules that: a store suppressing `CancelledError`
        held the collection -- and every host teardown awaiting this drain --
        for as long as its own request ran. `_Wedged` above cannot catch this;
        its `sleep` honours the cancellation and returns at once.
        """
        import asyncio

        release = asyncio.Event()

        class _Deaf:
            async def store(self, session_id, messages, **kw):
                # Deaf for the length of the drain, then releasable: a task
                # left permanently deaf would hang this test's own runner on
                # the way out, which is the very failure being measured.
                while True:
                    try:
                        await release.wait()
                        return True
                    except asyncio.CancelledError:
                        if release.is_set():
                            raise
                        continue

        agent = _make_loop(tmp_path, backend=_Deaf())
        agent._dispatch_backend_store("s", [{"role": "user", "content": "x"}])
        await asyncio.sleep(0.05)

        # `asyncio.wait` on a task, not `wait_for` on the coroutine: without the
        # bound this drain never returns, and `wait_for` would then hang too --
        # it cancels what it is waiting on and awaits the result, which is the
        # very thing that does not arrive. `wait` cancels nothing, so the
        # assertion below can report a failure instead of costing the CI job its
        # whole timeout.
        drain = asyncio.create_task(agent.drain_backend_stores(timeout=0.1))
        done, _ = await asyncio.wait({drain}, timeout=5.0)

        if not done:
            release.set()
            await asyncio.wait({drain}, timeout=2.0)
            drain.cancel()
            raise AssertionError("the drain waited out a worker that never stops")

        release.set()
        workers = [t for t in agent._store_pipeline._workers.values() if not t.done()]
        if workers:
            await asyncio.wait(workers, timeout=2.0)

    async def test_drain_counts_records_still_queued_behind_a_wedged_write(self, tmp_path: Path) -> None:
        """A wedged first write parks the worker mid-attempt, so it never gets
        back to the nine records behind it in the queue. Those nine were
        neither indexed nor reported: ``_store_dropped`` stayed at 0 while the
        queue silently held onto them. The tenth is the one the worker had
        already taken off the queue, which counting the queues alone missed.
        """
        import asyncio
        import time

        class _Wedged:
            async def store(self, session_id, messages, **kw):
                await asyncio.sleep(30)
                return True

        agent = _make_loop(tmp_path, backend=_Wedged())
        for i in range(10):
            agent._dispatch_backend_store("s", [{"role": "user", "content": str(i)}])

        t0 = time.monotonic()
        await agent.drain_backend_stores(timeout=0.2)
        assert time.monotonic() - t0 < 1.0

        assert agent._store_pipeline.dropped == 10


class TestTheGiveUpMessageIsHonest:
    """The plugin used to print its own "N turn(s) were not written" summary,
    counting every failed *attempt* rather than every failed *turn* -- one
    lost turn could read as five. That message now comes from here instead,
    driven by ``_store_dropped``, which only grows on a genuine give-up.
    """

    async def test_drain_prints_the_real_give_up_count(
        self, tmp_path: Path, monkeypatch, capsys: pytest.CaptureFixture
    ) -> None:
        from raven.memory_engine import store_pipeline

        monkeypatch.setattr(store_pipeline, "BACKOFF_S", (0.01, 0.01, 0.01, 0.01))

        class _Never:
            async def store(self, session_id, messages, **kw):
                return False

        agent = _make_loop(tmp_path, backend=_Never())
        agent._dispatch_backend_store("s", [{"role": "user", "content": "x"}])

        dropped = await agent.drain_backend_stores(timeout=5.0)

        assert agent._store_pipeline.dropped == 1
        assert dropped == 1  # the host renders the notice from this count; the loop prints nothing
        assert "were not written" not in capsys.readouterr().err

    async def test_a_turn_that_succeeds_on_retry_is_not_counted(
        self, tmp_path: Path, monkeypatch, capsys: pytest.CaptureFixture
    ) -> None:
        from raven.memory_engine import store_pipeline

        monkeypatch.setattr(store_pipeline, "BACKOFF_S", (0.01, 0.01, 0.01, 0.01))

        class _FailsOnceThenLands:
            def __init__(self) -> None:
                self.attempts = 0

            async def store(self, session_id, messages, **kw):
                self.attempts += 1
                return self.attempts > 1

        agent = _make_loop(tmp_path, backend=_FailsOnceThenLands())
        agent._dispatch_backend_store("s", [{"role": "user", "content": "x"}])

        # Let the retry land on its own -- draining immediately would cut the
        # backoff short and turn this into the unrelated stop-interrupted-a-
        # backoff case, not the "retry succeeded" case this test is about.
        worker = agent._store_pipeline._workers["s"]
        await worker

        await agent.drain_backend_stores(timeout=5.0)

        assert agent._store_pipeline.dropped == 0
        assert "were not written" not in capsys.readouterr().err


class TestWhatGetsIndexedMatchesWhatWasSaved:
    """Two copies of one turn is one copy too many.

    The slice handed to the backend was the raw one; the session log holds the
    trimmed one. A hundred kilobytes of tool output belongs in neither.
    """

    async def test_a_huge_tool_result_reaches_the_backend_trimmed(self, tmp_path: Path) -> None:
        b = _FakeBackend()
        agent = _make_loop(tmp_path, backend=b)
        session = agent.sessions.get_or_create("cli:t")

        huge = "x" * (agent._TOOL_RESULT_MAX_CHARS + 500)
        all_msgs = [
            {"role": "user", "content": "run it"},
            {"role": "tool", "content": huge},
        ]
        prev_len = len(session.messages)
        agent._save_turn(session, all_msgs, 0)
        agent._dispatch_backend_store(session.key, session.messages[prev_len:])
        await agent.drain_backend_stores(timeout=5.0)

        sent = b.store_calls[0]["messages"]
        assert sent == session.messages[prev_len:]
        assert len(sent[1]["content"]) < len(huge)


class TestTheBoundsAreGlobalNotJustPerSession:
    """Per-session bounds are not a bound.

    The queue replaced a global in-flight cap of four with a per-session
    maxlen, so a gateway fielding N chats multiplied it: twenty session keys
    started twenty concurrent stores against one backend, and each of those is
    a socket and a server-side extraction.
    """

    async def test_writes_across_sessions_share_one_concurrency_budget(self, tmp_path: Path) -> None:
        import asyncio

        from raven.memory_engine import store_pipeline

        class _Counting:
            def __init__(self) -> None:
                self.live = 0
                self.peak = 0

            async def store(self, session_id, messages, **kw):
                self.live += 1
                self.peak = max(self.peak, self.live)
                try:
                    await asyncio.sleep(0.05)
                finally:
                    self.live -= 1
                return True

        b = _Counting()
        agent = _make_loop(tmp_path, backend=b)
        for i in range(20):
            agent._dispatch_backend_store(f"chat-{i}", [{"role": "user", "content": "x"}])

        await agent.drain_backend_stores(timeout=10.0)
        assert b.peak <= store_pipeline.MAX_CONCURRENCY, f"peaked at {b.peak} concurrent writes"

    async def test_the_backlog_has_a_ceiling_across_sessions(self, tmp_path: Path, monkeypatch) -> None:
        import asyncio

        from raven.memory_engine import store_pipeline

        monkeypatch.setattr(store_pipeline, "MAX_TOTAL_QUEUED", 8)

        class _Wedged:
            async def store(self, session_id, messages, **kw):
                await asyncio.sleep(30)

        agent = _make_loop(tmp_path, backend=_Wedged())
        for i in range(40):
            agent._dispatch_backend_store(f"chat-{i % 4}", [{"role": "user", "content": str(i)}])

        queued = sum(len(q) for q in agent._store_pipeline._queues.values())
        assert queued <= store_pipeline.MAX_TOTAL_QUEUED, queued
        assert agent._store_pipeline.dropped > 0

        for task in list(agent._store_pipeline._workers.values()):
            task.cancel()


class TestShutdownAccountsForEveryTurnItLoses:
    """A worker has already taken its current record off the queue, so counting
    only the queues under-reports by one per worker -- and leaving that worker
    running lets it race the HTTP client the caller closes next.
    """

    async def test_every_enqueued_turn_is_accounted_for(self, tmp_path: Path) -> None:
        import asyncio

        class _Wedged:
            async def store(self, session_id, messages, **kw):
                await asyncio.sleep(30)

        agent = _make_loop(tmp_path, backend=_Wedged())
        for i in range(10):
            agent._dispatch_backend_store("s", [{"role": "user", "content": str(i)}])
        await asyncio.sleep(0.05)

        await agent.drain_backend_stores(timeout=0.2)
        assert agent._store_pipeline.dropped == 10, f"reported {agent._store_pipeline.dropped} of 10 lost turns"

    async def test_no_worker_outlives_the_drain(self, tmp_path: Path) -> None:
        import asyncio

        class _Wedged:
            async def store(self, session_id, messages, **kw):
                await asyncio.sleep(30)

        agent = _make_loop(tmp_path, backend=_Wedged())
        agent._dispatch_backend_store("s", [{"role": "user", "content": "x"}])
        await asyncio.sleep(0.05)

        await agent.drain_backend_stores(timeout=0.2)
        live = [t for t in agent._store_pipeline._workers.values() if not t.done()]
        assert live == [], "a worker survived the drain and will race backend.stop()"


class TestAdmissionHoldsUnderTheReviewersRepros:
    """Two ways the global ceiling was not a ceiling.

    A worker pops its record before waiting for a concurrency slot, so counting
    only the deques reads zero exactly when the backlog is at its worst. And
    with both ceilings full, one enqueue took the global eviction and then the
    per-session one, costing two stored turns to admit a single new one.
    """

    async def test_many_sessions_yielding_between_enqueues_still_hit_the_ceiling(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        import asyncio

        from raven.memory_engine import store_pipeline

        monkeypatch.setattr(store_pipeline, "MAX_TOTAL_QUEUED", 16)

        class _Wedged:
            def __init__(self) -> None:
                self.started = 0

            async def store(self, session_id, messages, **kw):
                self.started += 1
                await asyncio.sleep(30)

        b = _Wedged()
        agent = _make_loop(tmp_path, backend=b)
        pipe = agent._store_pipeline
        for i in range(60):
            agent._dispatch_backend_store(f"chat-{i}", [{"role": "user", "content": "x"}])
            await asyncio.sleep(0)

        assert pipe._outstanding() <= store_pipeline.MAX_TOTAL_QUEUED, (
            f"outstanding={pipe._outstanding()} "
            f"(queued={sum(len(q) for q in pipe._queues.values())}, active={len(pipe._active)})"
        )
        assert pipe.dropped > 0, "the ceiling was reached but nothing was shed"
        assert b.started <= store_pipeline.MAX_CONCURRENCY

        for task in list(pipe._workers.values()):
            task.cancel()

    async def test_both_ceilings_full_sheds_exactly_one_turn(self, tmp_path: Path, monkeypatch) -> None:
        import asyncio

        from raven.memory_engine import store_pipeline

        monkeypatch.setattr(store_pipeline, "MAX_QUEUED", 2)
        monkeypatch.setattr(store_pipeline, "MAX_TOTAL_QUEUED", 4)

        class _Wedged:
            async def store(self, session_id, messages, **kw):
                await asyncio.sleep(30)

        agent = _make_loop(tmp_path, backend=_Wedged())
        pipe = agent._store_pipeline
        # No await between these, so no worker runs and every record stays put.
        for key in ("a", "a", "b", "b"):
            agent._dispatch_backend_store(key, [{"role": "user", "content": key}])
        assert {k: len(v) for k, v in pipe._queues.items()} == {"a": 2, "b": 2}
        assert pipe.dropped == 0

        agent._dispatch_backend_store("b", [{"role": "user", "content": "b3"}])

        assert pipe.dropped == 1, "one enqueue must cost at most one stored turn"
        assert sum(len(q) for q in pipe._queues.values()) == 4, {k: len(v) for k, v in pipe._queues.items()}
        assert {k: len(v) for k, v in pipe._queues.items()} == {"a": 2, "b": 2}

        for task in list(pipe._workers.values()):
            task.cancel()


class TestRefusedSessionsAreNotRetained:
    """Refused traffic is exactly the high-cardinality kind.

    Creating the per-session deque before admission left one behind on every
    refusal: a wedged backend plus a thousand new chats left a thousand empty
    entries that nothing prunes and that every later admission scan walks.
    """

    async def test_a_thousand_refused_sessions_leave_nothing_behind(self, tmp_path: Path, monkeypatch) -> None:
        import asyncio

        from raven.memory_engine import store_pipeline

        monkeypatch.setattr(store_pipeline, "MAX_TOTAL_QUEUED", 4)

        class _Wedged:
            async def store(self, session_id, messages, **kw):
                await asyncio.sleep(30)

        agent = _make_loop(tmp_path, backend=_Wedged())
        pipe = agent._store_pipeline
        for i in range(4):
            agent._dispatch_backend_store(f"live-{i}", [{"role": "user", "content": "x"}])
            await asyncio.sleep(0)
        assert len(pipe._active) == 4, pipe._active

        for i in range(1000):
            agent._dispatch_backend_store(f"refused-{i}", [{"role": "user", "content": "x"}])

        assert not [k for k in pipe._queues if k.startswith("refused-")], (
            f"{len([k for k in pipe._queues if k.startswith('refused-')])} refused sessions were retained"
        )
        assert pipe.dropped == 1000
        assert pipe._outstanding() == 4

        for task in list(pipe._workers.values()):
            task.cancel()


class TestABurstOfNewSessionsInOneTickIsBounded:
    """The ceiling has to bound tasks and keys, not only records.

    Every enqueue for an unseen session costs a deque and a worker on top of
    the record. Replacing a victim instead of refusing the newcomer bounded
    records at the ceiling while a thousand distinct enqueues in a single loop
    iteration still allocated a thousand of each, none of which could be
    pruned until the loop got control.
    """

    async def test_a_thousand_new_sessions_without_a_yield(self, tmp_path: Path, monkeypatch) -> None:
        import asyncio

        from raven.memory_engine import store_pipeline

        monkeypatch.setattr(store_pipeline, "MAX_TOTAL_QUEUED", 4)

        class _Wedged:
            async def store(self, session_id, messages, **kw):
                await asyncio.sleep(30)

        agent = _make_loop(tmp_path, backend=_Wedged())
        pipe = agent._store_pipeline
        for i in range(1000):
            agent._dispatch_backend_store(f"chat-{i}", [{"role": "user", "content": "x"}])

        cap = store_pipeline.MAX_TOTAL_QUEUED
        assert pipe._outstanding() <= cap
        assert len(pipe._workers) <= cap, f"{len(pipe._workers)} workers for a ceiling of {cap}"
        assert len(pipe._queues) <= cap, f"{len(pipe._queues)} queue entries for a ceiling of {cap}"
        assert pipe.dropped == 1000 - cap

        for task in list(pipe._workers.values()):
            task.cancel()

    async def test_a_session_already_being_served_still_displaces_a_backlog(self, tmp_path: Path, monkeypatch) -> None:
        """Refusing newcomers must not also stop a known session from shedding:
        one session hoarding the ceiling still gives way to its own peers."""
        import asyncio

        from raven.memory_engine import store_pipeline

        monkeypatch.setattr(store_pipeline, "MAX_TOTAL_QUEUED", 4)

        class _Wedged:
            async def store(self, session_id, messages, **kw):
                await asyncio.sleep(30)

        agent = _make_loop(tmp_path, backend=_Wedged())
        pipe = agent._store_pipeline
        for _ in range(3):
            agent._dispatch_backend_store("hog", [{"role": "user", "content": "x"}])
        agent._dispatch_backend_store("quiet", [{"role": "user", "content": "x"}])
        assert {k: len(v) for k, v in pipe._queues.items()} == {"hog": 3, "quiet": 1}

        agent._dispatch_backend_store("quiet", [{"role": "user", "content": "again"}])

        assert {k: len(v) for k, v in pipe._queues.items()} == {"hog": 2, "quiet": 2}
        assert pipe.dropped == 1

        for task in list(pipe._workers.values()):
            task.cancel()
