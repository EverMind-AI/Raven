"""MB-1 — MemoryBackend Protocol surface + Memory data class."""

from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from raven.memory_engine import Memory, MemoryBackend

# ---------------------------------------------------------------------------
# Memory dataclass
# ---------------------------------------------------------------------------


class TestMemoryDataclass:
    def test_minimum_fields(self) -> None:
        m = Memory(text="hello")
        assert m.text == "hello"
        assert m.score == 0.0
        assert m.metadata == {}

    def test_all_fields(self) -> None:
        m = Memory(
            text="user likes espresso",
            score=0.87,
            metadata={"id": "abc", "episode_type": "Conversation"},
        )
        assert m.score == 0.87
        assert m.metadata["episode_type"] == "Conversation"

    def test_frozen(self) -> None:
        m = Memory(text="x")
        with pytest.raises(FrozenInstanceError):
            m.text = "y"  # type: ignore[misc]

    def test_metadata_default_is_independent_per_instance(self) -> None:
        # Catches the classic mutable-default footgun: ``field(default_factory=dict)``
        # rather than ``= {}`` in the dataclass body.
        a = Memory(text="a")
        b = Memory(text="b")
        a.metadata["key"] = "value"
        assert "key" not in b.metadata


# ---------------------------------------------------------------------------
# Protocol shape
# ---------------------------------------------------------------------------


class _CompleteBackend:
    """Minimum acceptable surface — every method present and async."""

    async def recall(self, query, *, user_id=None, agent_id=None, top_k):
        return [Memory(text=f"hit:{query}", score=0.5)]

    async def store(self, session_id, messages):
        return None

    async def feedback(self, signals):
        return None

    async def start(self):
        return None

    async def stop(self):
        return None


class _IncompleteBackend:
    """Missing ``feedback`` — should NOT satisfy the Protocol."""

    async def recall(self, query, *, user_id=None, agent_id=None, top_k):
        return []

    async def store(self, session_id, messages):
        return None

    async def start(self):
        return None

    async def stop(self):
        return None


class TestProtocolRuntimeCheck:
    def test_complete_backend_satisfies_protocol(self) -> None:
        assert isinstance(_CompleteBackend(), MemoryBackend)

    def test_incomplete_backend_fails_protocol(self) -> None:
        # @runtime_checkable Protocols check attribute presence —
        # a missing method must reject.
        assert not isinstance(_IncompleteBackend(), MemoryBackend)


# ---------------------------------------------------------------------------
# Behavioral surface — exercises every method
# ---------------------------------------------------------------------------


class TestBackendCallable:
    @pytest.fixture
    def backend(self) -> MemoryBackend:
        return _CompleteBackend()

    async def test_recall_returns_memory_list(self, backend) -> None:
        hits = await backend.recall("coffee", user_id="alice", top_k=3)
        assert isinstance(hits, list)
        assert all(isinstance(h, Memory) for h in hits)
        assert hits[0].text == "hit:coffee"

    async def test_store_returns_none(self, backend) -> None:
        result = await backend.store(
            "sess-1",
            [{"role": "user", "content": "hi"}],
        )
        assert result is None

    async def test_feedback_accepts_arbitrary_signal_dict(self, backend) -> None:
        result = await backend.feedback({"any": "signal", "kind": "skill_usage"})
        assert result is None

    async def test_lifecycle(self, backend) -> None:
        await backend.start()
        await backend.stop()
        # Idempotent — second cycle must not raise.
        await backend.start()
        await backend.stop()


# ---------------------------------------------------------------------------
# Documentation-level contract: empty recall is valid
# ---------------------------------------------------------------------------


class _EmptyRecallBackend:
    """Adapter whose backend has nothing for the query."""

    async def recall(self, query, *, user_id=None, agent_id=None, top_k):
        return []

    async def store(self, session_id, messages):
        pass

    async def feedback(self, signals):
        pass

    async def start(self):
        pass

    async def stop(self):
        pass


class TestEmptyRecall:
    async def test_empty_list_is_valid_response(self) -> None:
        b: MemoryBackend = _EmptyRecallBackend()
        hits = await b.recall("anything", user_id="nobody", top_k=10)
        assert hits == []
