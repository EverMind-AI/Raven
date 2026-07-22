"""SR-4 — EverosSkillSource wraps MemoryBackend.recall + emits RouterHit."""

from __future__ import annotations

from typing import Any

import pytest

from raven.memory_engine import Memory
from raven.memory_engine.skill_forge import (
    EverosSkillSource,
    SkillSource,
)

# ---------------------------------------------------------------------------
# Stub MemoryBackend
# ---------------------------------------------------------------------------


class _FakeBackend:
    """Records calls and returns the canned ``recall_response``."""

    def __init__(self) -> None:
        self.recall_calls: list[dict[str, Any]] = []
        self.recall_response: list[Memory] = []
        self.recall_raises: Exception | None = None

    async def start(self) -> None:
        pass

    async def stop(self) -> None:
        pass

    async def feedback(self, signals) -> None:
        pass

    async def store(self, session_id, messages, *, metadata=None) -> None:
        pass

    async def recall(self, query, *, user_id=None, agent_id=None, top_k):
        self.recall_calls.append(
            {
                "query": query,
                "user_id": user_id,
                "agent_id": agent_id,
                "top_k": top_k,
            }
        )
        if self.recall_raises is not None:
            raise self.recall_raises
        return list(self.recall_response)


@pytest.fixture
def backend() -> _FakeBackend:
    return _FakeBackend()


@pytest.fixture
def source(backend) -> EverosSkillSource:
    return EverosSkillSource(backend=backend, agent_id="agent:default")


# ---------------------------------------------------------------------------
# Protocol shape
# ---------------------------------------------------------------------------


class TestProtocolShape:
    def test_satisfies_skill_source_protocol(self, source) -> None:
        assert isinstance(source, SkillSource)

    def test_name_and_weight(self, source) -> None:
        assert source.name == "everos"
        assert source.weight == 0.9


# ---------------------------------------------------------------------------
# Recall call shape
# ---------------------------------------------------------------------------


class TestRecallCall:
    async def test_passes_agent_id_from_constructor(
        self,
        source,
        backend,
    ) -> None:
        await source.search("git", history=[], k=5)
        assert backend.recall_calls == [
            {
                "query": "git",
                "user_id": None,
                "agent_id": "agent:default",
                "top_k": 5,
            },
        ]

    async def test_agent_id_does_not_change_across_calls(
        self,
        source,
        backend,
    ) -> None:
        await source.search("a", history=[], k=1)
        await source.search("b", history=[], k=3)
        agents = [c["agent_id"] for c in backend.recall_calls]
        assert agents == ["agent:default", "agent:default"]
        assert all(c["user_id"] is None for c in backend.recall_calls)

    async def test_history_not_forwarded(self, source, backend) -> None:
        """For SR-4 we deliberately don't pass history through; the
        MemoryBackend Protocol has no field for it. Test pins the
        decision so future changes are conscious."""
        history = [{"role": "user", "content": "earlier"}]
        await source.search("now", history=history, k=1)
        assert set(backend.recall_calls[0]) == {
            "query",
            "user_id",
            "agent_id",
            "top_k",
        }


# ---------------------------------------------------------------------------
# Hit mapping
# ---------------------------------------------------------------------------


class TestHitMapping:
    async def test_uses_metadata_id(self, source, backend) -> None:
        backend.recall_response = [
            Memory(
                text="Use `git rerere` to remember conflict resolutions.",
                score=0.7,
                metadata={"id": "skill-abc", "name": "git-rerere"},
            ),
        ]
        hits = await source.search("git", history=[], k=5)
        assert len(hits) == 1
        h = hits[0]
        assert h.qualified_id == "everos/skill-abc"
        assert h.name == "git-rerere"
        assert h.content.startswith("Use `git rerere`")
        assert h.score == pytest.approx(0.7)

    async def test_falls_back_to_text_hash_when_id_missing(
        self,
        source,
        backend,
    ) -> None:
        text = "an evolved skill without an upstream id"
        backend.recall_response = [Memory(text=text, score=0.4)]
        hits = await source.search("q", history=[], k=5)
        # qualified_id is stable across calls with the same text
        qid = hits[0].qualified_id
        assert qid.startswith("everos/")
        assert len(qid.split("/")[1]) == 12  # 12 hex chars
        hits2 = await source.search("q", history=[], k=5)
        assert hits2[0].qualified_id == qid

    async def test_falls_back_to_text_prefix_when_name_missing(
        self,
        source,
        backend,
    ) -> None:
        backend.recall_response = [
            Memory(
                text="Detailed body of a skill\nwith multiple lines",
                score=0.5,
                metadata={"id": "x"},
            ),
        ]
        hits = await source.search("q", history=[], k=5)
        # First non-blank line truncated to 40 chars
        assert hits[0].name == "Detailed body of a skill"

    async def test_meta_carries_original_metadata(
        self,
        source,
        backend,
    ) -> None:
        backend.recall_response = [
            Memory(
                text="body",
                score=0.5,
                metadata={
                    "id": "abc",
                    "name": "n",
                    "owner_type": "agent",
                    "confidence": 0.92,
                    "episode_type": "skill",
                },
            ),
        ]
        hits = await source.search("q", history=[], k=5)
        m = hits[0].meta
        assert m["source"] == "everos"
        assert m["owner_type"] == "agent"
        assert m["confidence"] == 0.92
        assert m["episode_type"] == "skill"

    async def test_empty_backend_response_returns_empty_list(
        self,
        source,
    ) -> None:
        hits = await source.search("nothing", history=[], k=5)
        assert hits == []


# ---------------------------------------------------------------------------
# Error propagation
# ---------------------------------------------------------------------------


class TestErrorPropagation:
    async def test_backend_exception_propagates(self, source, backend) -> None:
        """SkillForgeRouter._safe_search will catch this; the source itself
        passes through so the router gets to log + isolate."""
        backend.recall_raises = RuntimeError("backend down")
        with pytest.raises(RuntimeError, match="backend down"):
            await source.search("q", history=[], k=5)
