"""AG-1 — AgentLoop ``backend`` wiring + ``_dispatch_backend_store``.

The two after-turn callsites (system-message path + REPL path) now call
:meth:`AgentLoop._dispatch_backend_store` as the third peer step in the
after-turn pipeline (alongside ``context_engine.after_turn`` and
``maybe_consolidate``). This file exercises the dispatcher in isolation
— the full end-to-end "AgentLoop processes a turn and the backend
ultimately sees it" path is left to integration tests that wire a real
LLM provider; here we keep things small + focused.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from raven.agent.loop import AgentLoop
from raven.agent.loop import main as loop_main

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
        # Held open so a test can observe a write that is still in flight,
        # which is the only way to tell a detached write from an awaited one.
        self.gate: asyncio.Event | None = None
        self.completed: list[str] = []

    async def start(self) -> None:
        pass

    async def stop(self) -> None:
        pass

    async def feedback(self, signals):
        pass

    async def recall(self, query, *, user_id=None, agent_id=None, top_k):
        return []

    async def store(self, session_id, messages):
        self.store_calls.append(
            {
                "session_id": session_id,
                "messages": messages,
            }
        )
        if self.store_raises is not None:
            raise self.store_raises
        if self.gate is not None:
            await self.gate.wait()
        self.completed.append(session_id)


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
        # Should not raise; just returns silently.
        await agent._dispatch_backend_store(
            "session-1",
            [{"role": "user", "content": "hi"}],
        )

    async def test_empty_messages_skips_backend(
        self,
        tmp_path: Path,
    ) -> None:
        b = _FakeBackend()
        agent = _make_loop(tmp_path, backend=b)
        await agent._dispatch_backend_store("session-1", [])
        # Adapter never invoked when slice is empty.
        assert b.store_calls == []

    async def test_calls_backend_store_with_full_slice(
        self,
        tmp_path: Path,
    ) -> None:
        b = _FakeBackend()
        agent = _make_loop(tmp_path, backend=b)
        slice_ = [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "there"},
        ]
        await agent._dispatch_backend_store("session-key-x", slice_)
        assert len(b.store_calls) == 1
        call = b.store_calls[0]
        assert call["session_id"] == "session-key-x"
        assert call["messages"] == slice_

    async def test_backend_exception_swallowed(
        self,
        tmp_path: Path,
    ) -> None:
        """A backend failure must not derail the AgentLoop. The turn is
        already saved to the session log; plugin-side indexing is
        best-effort."""
        b = _FakeBackend()
        b.store_raises = RuntimeError("evermem down")
        agent = _make_loop(tmp_path, backend=b)
        # The dispatcher swallows the exception (and logs an exception
        # traceback). The call must return normally.
        await agent._dispatch_backend_store(
            "s",
            [{"role": "user", "content": "x"}],
        )
        # Verify the adapter was hit (so we know exception came from store).
        assert len(b.store_calls) == 1


# ---------------------------------------------------------------------------
# Detachment + drain
# ---------------------------------------------------------------------------


@pytest.fixture
def tight_store_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    """Shrink the turn budget so a gated write is observably detached."""
    monkeypatch.setattr(loop_main, "_STORE_TURN_BUDGET_S", 0.05)


class TestDetachedStore:
    async def test_slow_store_does_not_hold_the_turn(
        self,
        tmp_path: Path,
        tight_store_budget: None,
    ) -> None:
        """A write that outruns the budget keeps running while the turn goes on."""
        b = _FakeBackend()
        b.gate = asyncio.Event()
        agent = _make_loop(tmp_path, backend=b)

        await agent._dispatch_backend_store("s1", [{"role": "user", "content": "x"}])

        # Entered the backend, but not finished: the dispatcher stopped waiting
        # rather than abandoning it.
        assert len(b.store_calls) == 1
        assert b.completed == []
        assert len(agent._store_inflight) == 1

        b.gate.set()
        await agent.drain_backend_stores()
        assert b.completed == ["s1"]
        assert agent._store_inflight == set()

    async def test_fast_store_still_completes_inline(self, tmp_path: Path) -> None:
        """The common case is unchanged — no gate, so the write lands in-turn."""
        b = _FakeBackend()
        agent = _make_loop(tmp_path, backend=b)
        await agent._dispatch_backend_store("s1", [{"role": "user", "content": "x"}])
        assert b.completed == ["s1"]
        assert agent._store_inflight == set()

    async def test_drain_is_a_noop_without_pending_writes(self, tmp_path: Path) -> None:
        agent = _make_loop(tmp_path, backend=_FakeBackend())
        await agent.drain_backend_stores()
        assert agent._store_dropped == 0

    async def test_saturated_queue_drops_rather_than_stalling(
        self,
        tmp_path: Path,
        tight_store_budget: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Past the in-flight cap the queue gives, not the turn.

        Exact counts, not ``>= 1``: with a cap of 1 and a gate nothing can pass,
        the first write starts and the next two are refused, so anything other
        than 1 started / 2 dropped is a bug -- including the one where a write
        that actually succeeded gets counted as dropped.
        """
        monkeypatch.setattr(loop_main, "_STORE_MAX_INFLIGHT", 1)
        b = _FakeBackend()
        b.gate = asyncio.Event()
        agent = _make_loop(tmp_path, backend=b)

        for i in range(3):
            await agent._dispatch_backend_store(f"s{i}", [{"role": "user", "content": "x"}])

        assert agent._store_dropped == 2
        # A refused write never reached the backend at all -- the point of
        # deciding before the task exists.
        assert [c["session_id"] for c in b.store_calls] == ["s0"]

        b.gate.set()
        await agent.drain_backend_stores()
        # And the one that did start completed: refusal cost the new writes, not
        # the running one.
        assert b.completed == ["s0"]
        assert agent._store_dropped == 0

    async def test_a_write_that_lands_during_backpressure_is_not_dropped(
        self,
        tmp_path: Path,
        tight_store_budget: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The saturation re-check must observe completions, not stale set size.

        Regression: the cap was re-checked with ``len(self._store_inflight)``,
        whose done-callback discard runs a loop iteration late. A write that
        finished while the turn waited still read as in flight, so a perfectly
        successful write was cancelled and counted as dropped.
        """
        monkeypatch.setattr(loop_main, "_STORE_MAX_INFLIGHT", 1)
        b = _FakeBackend()
        b.gate = asyncio.Event()
        agent = _make_loop(tmp_path, backend=b)

        await agent._dispatch_backend_store("s0", [{"role": "user", "content": "x"}])
        assert agent._store_inflight_count() == 1

        # Release the first write, then dispatch a second: the queue drained
        # during the backpressure wait, so the second write must start.
        b.gate.set()
        await agent._dispatch_backend_store("s1", [{"role": "user", "content": "x"}])

        await agent.drain_backend_stores()
        assert b.completed == ["s0", "s1"]
        assert agent._store_dropped == 0

    async def test_drain_counts_writes_it_could_not_finish(
        self,
        tmp_path: Path,
        tight_store_budget: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A write still running when the drain budget expires is data loss.

        ``backend.stop()`` closes the client pool right after, so these fail
        with only the backend's own warning to show for it -- the host has to
        say the turn was not indexed.
        """
        warnings: list[str] = []
        monkeypatch.setattr(
            loop_main.logger,
            "warning",
            lambda msg, *a, **k: warnings.append(msg),
        )
        b = _FakeBackend()
        b.gate = asyncio.Event()
        agent = _make_loop(tmp_path, backend=b)
        await agent._dispatch_backend_store("s1", [{"role": "user", "content": "x"}])

        await agent.drain_backend_stores(timeout=0.05)
        assert b.completed == []
        assert any("were not indexed" in w for w in warnings)
        b.gate.set()

    async def test_drain_timeout_reads_the_constant_at_call_time(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The drain budget must be monkeypatchable like the other two.

        It was a default argument, so it bound at ``def`` time and was the one
        constant of the three that a test could not shrink.
        """
        monkeypatch.setattr(loop_main, "_STORE_DRAIN_BUDGET_S", 0.05)
        b = _FakeBackend()
        b.gate = asyncio.Event()
        agent = _make_loop(tmp_path, backend=b)
        await agent._dispatch_backend_store("s1", [{"role": "user", "content": "x"}])

        # Returns on the patched budget rather than hanging for 15s.
        await asyncio.wait_for(agent.drain_backend_stores(), timeout=5.0)
        assert b.completed == []
        b.gate.set()

    async def test_drain_reports_the_dropped_count_once(
        self,
        tmp_path: Path,
        tight_store_budget: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Two drains (boundary flush, then teardown) must not double-report."""
        monkeypatch.setattr(loop_main, "_STORE_MAX_INFLIGHT", 1)
        b = _FakeBackend()
        b.gate = asyncio.Event()
        agent = _make_loop(tmp_path, backend=b)
        for i in range(3):
            await agent._dispatch_backend_store(f"s{i}", [{"role": "user", "content": "x"}])
        assert agent._store_dropped >= 1

        b.gate.set()
        await agent.drain_backend_stores()
        assert agent._store_dropped == 0
        await agent.drain_backend_stores()
        assert agent._store_dropped == 0


# ---------------------------------------------------------------------------
# One process, several loops, one backend
# ---------------------------------------------------------------------------


class TestSharedInflightSet:
    def test_each_loop_gets_its_own_set_by_default(self, tmp_path: Path) -> None:
        first = _make_loop(tmp_path)
        second = _make_loop(tmp_path)
        assert first._store_inflight is not second._store_inflight

    async def test_a_shared_set_caps_the_process_not_each_loop(
        self,
        tmp_path: Path,
        tight_store_budget: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """N loops against ONE backend must not multiply the cap by N.

        With a cap of 1 and a gate nothing can pass, the first loop's write
        starts and the second loop's is refused -- the whole point of handing
        both loops the same set. Per-loop sets would let both through and put
        2N writes on a service the cap exists to protect.
        """
        monkeypatch.setattr(loop_main, "_STORE_MAX_INFLIGHT", 1)
        shared: set = set()
        b = _FakeBackend()
        b.gate = asyncio.Event()
        first = AgentLoop(
            provider=_StubProvider(),
            workspace=tmp_path,
            model="stub",
            backend=b,
            store_inflight=shared,
        )
        second = AgentLoop(
            provider=_StubProvider(),
            workspace=tmp_path,
            model="stub",
            backend=b,
            store_inflight=shared,
        )
        assert first._store_inflight is second._store_inflight

        await first._dispatch_backend_store("s0", [{"role": "user", "content": "x"}])
        await second._dispatch_backend_store("s1", [{"role": "user", "content": "x"}])

        assert [c["session_id"] for c in b.store_calls] == ["s0"]
        assert second._store_dropped == 1

        b.gate.set()
        await first.drain_backend_stores()
        assert b.completed == ["s0"]

    async def test_a_given_up_write_is_reported_once_across_every_loop(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Found end to end: three engines sharing the set each drained it and
        each warned about the same lost turn, so one loss read as three.

        The count is per loop and the set is shared, so whoever gives up on a
        task claims it. The task stays in the set -- it is still running and
        still holds a slot against the backpressure cap, which is the other
        reader of that set.
        """
        monkeypatch.setattr(loop_main, "_STORE_DRAIN_BUDGET_S", 0.05)
        shared: set = set()
        b = _FakeBackend()
        b.gate = asyncio.Event()
        loops = [
            AgentLoop(
                provider=_StubProvider(),
                workspace=tmp_path,
                model="stub",
                backend=b,
                store_inflight=shared,
            )
            for _ in range(3)
        ]

        await loops[0]._dispatch_backend_store("s0", [{"role": "user", "content": "x"}])
        assert len(shared) == 1

        await loops[0].drain_backend_stores(timeout=0.05)
        assert loops[0]._store_dropped == 0, "the count is cleared by its own report"
        assert len(shared) == 1, "a claimed write still counts against the cap"
        assert loops[1]._store_inflight_count() == 1

        # The other two find the write claimed, so they report nothing.
        for other in loops[1:]:
            await other.drain_backend_stores(timeout=0.05)
            assert other._store_dropped == 0

        b.gate.set()

    async def test_concurrent_drains_report_a_lost_write_once(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Teardown closes every engine at once, which is the case that broke.

        ``AcpLoops.aclose`` gathers the drains, so all of them snapshot the
        shared set before any of them has given up on anything. A claim taken
        after the wait is therefore taken by all of them at once, and each one
        warns about the same lost turn -- three engines reported one loss as
        three end to end.
        """
        from loguru import logger

        monkeypatch.setattr(loop_main, "_STORE_DRAIN_BUDGET_S", 0.05)
        shared: set = set()
        b = _FakeBackend()
        b.gate = asyncio.Event()
        loops = [
            AgentLoop(
                provider=_StubProvider(),
                workspace=tmp_path,
                model="stub",
                backend=b,
                store_inflight=shared,
            )
            for _ in range(3)
        ]

        await loops[0]._dispatch_backend_store("s0", [{"role": "user", "content": "x"}])
        assert len(shared) == 1

        seen: list[str] = []
        sink = logger.add(lambda m: seen.append(m.record["message"]), level="WARNING")
        try:
            await asyncio.gather(*(one.drain_backend_stores(timeout=0.05) for one in loops))
        finally:
            logger.remove(sink)

        losses = [line for line in seen if "were not indexed" in line]
        assert losses == ["1 turn(s) were not indexed: the memory service never caught up"]
        # The read back the claim must not break: the write is still running, so
        # it still counts against the cap every loop shares.
        assert loops[1]._store_inflight_count() == 1

        b.gate.set()

    async def test_a_claimed_write_still_holds_its_slot_against_the_cap(
        self,
        tmp_path: Path,
        tight_store_budget: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A drain is not only teardown -- a boundary flush drains a live process.

        The cap is counted off the same shared set the drain claims from, so
        claiming by dropping the task would take a running write out of the
        count and let the next turn past a cap that is still full.
        """
        monkeypatch.setattr(loop_main, "_STORE_MAX_INFLIGHT", 1)
        monkeypatch.setattr(loop_main, "_STORE_DRAIN_BUDGET_S", 0.05)
        shared: set = set()
        b = _FakeBackend()
        b.gate = asyncio.Event()
        first, second = (
            AgentLoop(
                provider=_StubProvider(),
                workspace=tmp_path,
                model="stub",
                backend=b,
                store_inflight=shared,
            )
            for _ in range(2)
        )

        await first._dispatch_backend_store("s0", [{"role": "user", "content": "x"}])
        await first.drain_backend_stores(timeout=0.05)

        await second._dispatch_backend_store("s1", [{"role": "user", "content": "x"}])
        assert [c["session_id"] for c in b.store_calls] == ["s0"]
        assert second._store_dropped == 1

        b.gate.set()


# ---------------------------------------------------------------------------
# Legacy compatibility — pre-AG-1 callsites still pass
# ---------------------------------------------------------------------------


class TestLegacyCompat:
    def test_construction_without_backend_unchanged(
        self,
        tmp_path: Path,
    ) -> None:
        """Pre-AG-1 construction (no ``backend=`` keyword) still works
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
