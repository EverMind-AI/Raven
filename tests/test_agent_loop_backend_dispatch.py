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

from pathlib import Path
from typing import Any

from raven.agent.loop import AgentLoop

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


class TestTheTurnDoesNotWaitOnIndexing:
    """Indexing a turn must not hold the turn open.

    ``backend.store`` was awaited inline in the after-turn pipeline with a 60s
    HTTP budget, so a memory service that hung stalled the end of every turn --
    and on the no-model delivery path the await sat *before* ``emit``, so it
    stalled the user seeing the reply at all. The write still gets its own
    budget; what changed is that the turn stops paying for it.
    """

    async def test_a_slow_write_releases_the_turn(self, tmp_path: Path, monkeypatch) -> None:
        import asyncio

        from raven.agent.loop import main as loop_main

        started = asyncio.Event()
        release = asyncio.Event()

        class _SlowBackend:
            def __init__(self) -> None:
                self.finished = False

            async def store(self, session_id, messages, **kw):
                started.set()
                await release.wait()
                self.finished = True

        b = _SlowBackend()
        agent = _make_loop(tmp_path, backend=b)
        monkeypatch.setattr(loop_main, "_STORE_TURN_BUDGET_S", 0.05)

        await agent._dispatch_backend_store("s", [{"role": "user", "content": "hi"}])

        assert started.is_set(), "the write should have been launched"
        assert not b.finished, "the turn returned before the write finished"

        release.set()
        await asyncio.gather(*agent._store_inflight)
        assert b.finished

    async def test_a_fast_write_still_completes_within_the_turn(self, tmp_path: Path) -> None:
        """The budget is an upper bound, not a delay: a healthy write finishes
        inside the turn exactly as it did before."""
        b = _FakeBackend()
        agent = _make_loop(tmp_path, backend=b)
        await agent._dispatch_backend_store("s", [{"role": "user", "content": "hi"}])
        assert len(b.store_calls) == 1

    async def test_outstanding_writes_are_drained_on_teardown(self, tmp_path: Path, monkeypatch) -> None:
        """A detached write that outlives the turn must not outlive the process:
        without a drain, exiting right after a slow turn silently loses it."""
        import asyncio

        from raven.agent.loop import main as loop_main

        release = asyncio.Event()

        class _SlowBackend:
            def __init__(self) -> None:
                self.finished = False

            async def store(self, session_id, messages, **kw):
                await release.wait()
                self.finished = True

        b = _SlowBackend()
        agent = _make_loop(tmp_path, backend=b)
        monkeypatch.setattr(loop_main, "_STORE_TURN_BUDGET_S", 0.05)
        await agent._dispatch_backend_store("s", [{"role": "user", "content": "hi"}])

        release.set()
        await agent.drain_backend_stores()
        assert b.finished
