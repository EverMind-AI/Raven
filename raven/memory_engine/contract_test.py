"""Shared contract test base class for :class:`MemoryBackend` adapters.

Subclass :class:`MemoryBackendContractTests` in your adapter's test
suite, override :meth:`make_backend` to return a fresh backend
instance, and pytest will run every cross-adapter contract assertion
against it. This is how the design's promise "extensible in theory ->
actually runs" turns into something CI can enforce.

Why a base class and not a fixture: subclassing keeps the test names
visible to test runners (``test_recall_returns_memory_list``) and
makes it obvious which backend a failure belongs to (e.g. the failure
will report ``TestMem0Backend::test_recall_returns_memory_list``). A
fixture-driven approach hides the same test under a parameter name.

The base class lives **inside the package** (not in ``tests/``) for two
reasons:

1. Plugin authors install ``raven`` and need to import it.
2. Pytest doesn't auto-collect from non-test packages, so the abstract
   base class never runs on its own — only concrete subclasses do.
"""

from __future__ import annotations

import pytest

from raven.memory_engine.backend import Memory, MemoryBackend


class MemoryBackendContractTests:
    """Cross-adapter contract assertions.

    Concrete subclasses override :meth:`make_backend` to construct a
    fresh backend (with whatever test scaffolding the adapter needs —
    tmp dirs, fake HTTP servers, in-memory stores). Each test gets its
    own backend via the ``backend`` fixture below so cross-test state
    leakage is impossible.
    """

    # ── Subclass hook ───────────────────────────────────────────────

    async def make_backend(self) -> MemoryBackend:
        """Construct a fresh backend for one test.

        Subclasses MUST override. The fixture awaits ``start()`` after
        construction and ``stop()`` after the test, so the override
        does *not* need to call them itself.
        """
        raise NotImplementedError(
            "MemoryBackendContractTests subclass must override "
            "make_backend()",
        )

    # ── Fixture ─────────────────────────────────────────────────────

    @pytest.fixture
    async def backend(self):
        b = await self.make_backend()
        await b.start()
        try:
            yield b
        finally:
            await b.stop()

    # ── Contract assertions ─────────────────────────────────────────

    async def test_satisfies_protocol(self, backend) -> None:
        """The returned object must be recognized as a MemoryBackend."""
        assert isinstance(backend, MemoryBackend)

    async def test_recall_returns_memory_list(self, backend) -> None:
        """``recall`` returns ``list[Memory]``. Empty is OK."""
        hits = await backend.recall(
            "anything", user_id="contract-test", top_k=5,
        )
        assert isinstance(hits, list)
        for h in hits:
            assert isinstance(h, Memory)
            assert isinstance(h.text, str)
            assert isinstance(h.score, float)
            assert isinstance(h.metadata, dict)

    async def test_recall_after_store_does_not_raise(self, backend) -> None:
        """``store`` followed by ``recall`` is the basic round-trip.

        We **do not** assert that the just-stored content surfaces —
        many backends asynchronously index, and some (e.g. mem0)
        require multi-turn boundaries before extraction lands. The
        contract is only that neither call raises.
        """
        await backend.store(
            "contract-session",
            [
                {"role": "user", "content": "I love Python"},
                {"role": "assistant", "content": "Noted."},
            ],
        )
        hits = await backend.recall(
            "programming", user_id="contract-test", top_k=5,
        )
        assert isinstance(hits, list)

    async def test_feedback_accepts_arbitrary_signals(self, backend) -> None:
        """No-op feedback is valid; any dict must be tolerated."""
        await backend.feedback({"unknown_signal": "should not crash"})
        await backend.feedback({})
        await backend.feedback({"kind": "skill_usage", "ids": ["x", "y"]})

    async def test_top_k_respected_or_bounded(self, backend) -> None:
        """``top_k`` upper-bounds the result; backends can return less."""
        hits = await backend.recall(
            "q", user_id="contract-test", top_k=3,
        )
        assert len(hits) <= 3

    async def test_recall_with_empty_owner_does_not_crash(
        self, backend,
    ) -> None:
        """Some hosts pass an unknown / never-stored-for owner."""
        hits = await backend.recall(
            "q", user_id="never-existed", top_k=5,
        )
        assert isinstance(hits, list)


class LifecycleContractTests:
    """Lifecycle tests run **without** the ``backend`` fixture so they
    can poke the raw ``start``/``stop`` pair directly. Separate base
    class so subclasses pick up these tests only if they want to
    assert idempotence."""

    async def make_backend(self) -> MemoryBackend:
        raise NotImplementedError

    async def test_start_stop_idempotent(self) -> None:
        b = await self.make_backend()
        await b.start()
        await b.stop()
        # Second cycle on a stopped backend should not raise.
        await b.start()
        await b.stop()

    async def test_stop_without_start_does_not_raise(self) -> None:
        """Defensive: a backend whose ``start`` failed (or never ran)
        should still ``stop`` cleanly so the host can shut down."""
        b = await self.make_backend()
        await b.stop()


__all__ = [
    "LifecycleContractTests",
    "MemoryBackendContractTests",
]
