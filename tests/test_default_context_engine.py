"""UnifiedContextEngine — EverOS lane (recall + SkillForgeRouter) behavior.

Formerly the ``DefaultContextEngine`` two-track tests. The two-track
gather (``backend.recall`` for ``# Memory`` + ``SkillForgeRouter`` for
``# Skills``) is now the EverOS lane of the single
:class:`UnifiedContextEngine`. These tests exercise that lane through
the fast path (empty history → no Curator LLM call), which is where the
recall / router outputs land in the prompt.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any

import pytest

from raven.agent.context import ContextBuilder
from raven.config.raven import ContextConfig
from raven.context_engine import ContextAssembler, TurnContext
from raven.context_engine.segments import (
    ActiveSkillsSegmentBuilder,
    BootstrapSegmentBuilder,
    IdentitySegmentBuilder,
    MemorySegmentBuilder,
    SkillsSegmentBuilder,
)
from raven.context_engine.segments.curator import CuratorSegmentBuilder
from raven.memory_engine import Memory, TokenBudget
from raven.memory_engine.skill_forge import RouterHit, SkillForgeRouter


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


class _StubSource:
    """Configurable SkillSource — records call args + delay."""

    def __init__(
        self,
        name: str,
        weight: float = 1.0,
        hits: list[RouterHit] | None = None,
        delay_s: float = 0.0,
    ) -> None:
        self.name = name
        self.weight = weight
        self._hits = hits or []
        self._delay = delay_s
        self.calls: list[tuple[str, int]] = []

    async def search(self, query, history, k):
        await asyncio.sleep(self._delay)
        self.calls.append((query, k))
        return list(self._hits)


class _StubBackend:
    """Minimal MemoryBackend mock — recall returns canned hits."""

    def __init__(
        self,
        recall_response: list[Memory] | None = None,
        delay_s: float = 0.0,
        recall_raises: Exception | None = None,
    ) -> None:
        self._recall_response = recall_response or []
        self._delay = delay_s
        self._recall_raises = recall_raises
        self.recall_calls: list[dict[str, Any]] = []

    async def start(self): pass
    async def stop(self): pass
    async def feedback(self, signals): pass
    async def store(self, session_id, messages): pass

    async def recall(self, query, *, user_id=None, agent_id=None, top_k):
        await asyncio.sleep(self._delay)
        self.recall_calls.append({
            "query": query, "user_id": user_id, "agent_id": agent_id, "top_k": top_k,
        })
        if self._recall_raises is not None:
            raise self._recall_raises
        return list(self._recall_response)


class _StubProvider:
    api_key = "test"

    def get_default_model(self) -> str:
        return "stub"

    async def chat_with_retry(self, *args, **kwargs):
        # Only reached on the slow path; these tests stay on fast path.
        raise NotImplementedError


def _budget() -> TokenBudget:
    return TokenBudget(
        context_length=100_000,
        reserved_output=4_000,
        reserved_tools=2_000,
        reserved_system=1_000,
        available_history=93_000,
    )


def _turn(msg: str = "hi", **kw) -> TurnContext:
    return TurnContext(
        current_message=msg,
        media=kw.get("media"),
        channel=kw.get("channel"),
        chat_id=kw.get("chat_id"),
        selected_skills=kw.get("selected_skills"),
    )


@pytest.fixture
def builder(tmp_path: Path) -> ContextBuilder:
    return ContextBuilder(workspace=tmp_path)


def _engine(
    builder: ContextBuilder,
    *,
    router: SkillForgeRouter,
    backend: _StubBackend | None,
    user_id: str = "default",
    skill_top_k: int = 5,
    memory_top_k: int = 5,
) -> ContextAssembler:
    builders = [
        IdentitySegmentBuilder(builder.workspace),
        BootstrapSegmentBuilder(builder.workspace),
        MemorySegmentBuilder(
            builder.memory, backend,
            user_id=user_id, memory_top_k=memory_top_k,
        ),
        ActiveSkillsSegmentBuilder(builder.skills),
        SkillsSegmentBuilder(router, skill_top_k=skill_top_k),
        CuratorSegmentBuilder(
            workspace=builder.workspace,
            config=ContextConfig(),
            provider=_StubProvider(),
            model="stub",
            context_window_tokens=100_000,
            get_tool_definitions=lambda: [],
        ),
    ]
    return ContextAssembler(builders, lambda: [])


# ---------------------------------------------------------------------------
# Identity / lifecycle
# ---------------------------------------------------------------------------


class TestEngineIdentity:
    def test_name(self, builder: ContextBuilder) -> None:
        eng = _engine(builder, router=SkillForgeRouter([]), backend=_StubBackend())
        assert eng.name == "context_assembler"

    def test_owns_compaction_is_true(self, builder: ContextBuilder) -> None:
        # The unified engine owns its own archival compaction (Curator
        # lane), so AgentLoop hands it the full append-only log and skips
        # the host MemoryConsolidator.
        eng = _engine(builder, router=SkillForgeRouter([]), backend=_StubBackend())
        assert eng.owns_compaction is True


# ---------------------------------------------------------------------------
# Two-track concurrency (recall + router.select)
# ---------------------------------------------------------------------------


class TestTwoTrackConcurrency:
    async def test_skill_and_memory_run_concurrently(
        self, builder: ContextBuilder,
    ) -> None:
        slow_source = _StubSource("local", hits=[], delay_s=0.10)
        slow_backend = _StubBackend(recall_response=[], delay_s=0.10)
        eng = _engine(
            builder, router=SkillForgeRouter([slow_source]), backend=slow_backend,
        )
        t0 = time.monotonic()
        await eng.assemble(
            session_key="s1",
            session_messages=[],
            budget=_budget(),
            turn=_turn(),
        )
        elapsed = time.monotonic() - t0
        # Serial would be ~0.20 s. Concurrent ~0.10 s. Loose bound 0.15.
        assert elapsed < 0.15

    async def test_track_ids_passed_to_recall(
        self, builder: ContextBuilder,
    ) -> None:
        backend = _StubBackend()
        eng = _engine(
            builder, router=SkillForgeRouter([]), backend=backend,
            user_id="alice",
        )
        await eng.assemble("s", [], _budget(), turn=_turn("git resolver"))
        assert backend.recall_calls == [
            {
                "query": "git resolver", "user_id": "alice",
                "agent_id": None, "top_k": 5,
            },
        ]

    async def test_top_k_propagated_per_track(
        self, builder: ContextBuilder,
    ) -> None:
        source = _StubSource("local", hits=[])
        backend = _StubBackend()
        eng = _engine(
            builder, router=SkillForgeRouter([source]), backend=backend,
            skill_top_k=3, memory_top_k=7,
        )
        await eng.assemble("s", [], _budget(), turn=_turn("q"))
        # SkillForgeRouter applies an over-fetch factor; the source sees k*2
        # by default.
        assert source.calls[0][1] == 6  # 3 × default over_fetch_factor 2
        assert backend.recall_calls[0]["top_k"] == 7


# ---------------------------------------------------------------------------
# AssembledContext metadata
# ---------------------------------------------------------------------------


class TestAssembledMetadata:
    async def test_injected_skill_ids(self, builder: ContextBuilder) -> None:
        hits = [
            RouterHit(qualified_id="local/a", name="a", content="", score=0.5),
            RouterHit(qualified_id="everos/b", name="b", content="", score=0.5),
        ]
        source = _StubSource("local", hits=hits)
        eng = _engine(builder, router=SkillForgeRouter([source]), backend=_StubBackend())
        ac = await eng.assemble("s", [], _budget(), turn=_turn())
        assert set(ac.metadata["injected_skill_ids"]) == {"local/a", "everos/b"}

    async def test_memory_hits_count(self, builder: ContextBuilder) -> None:
        memories = [Memory(text=f"fact-{i}") for i in range(3)]
        eng = _engine(
            builder, router=SkillForgeRouter([]),
            backend=_StubBackend(recall_response=memories),
        )
        ac = await eng.assemble("s", [], _budget(), turn=_turn())
        assert ac.metadata["memory_hits"] == 3

    async def test_engine_label(self, builder: ContextBuilder) -> None:
        eng = _engine(builder, router=SkillForgeRouter([]), backend=_StubBackend())
        ac = await eng.assemble("s", [], _budget(), turn=_turn())
        assert ac.metadata["engine"] == "context_assembler"


# ---------------------------------------------------------------------------
# Block rendering — recall → # Memory, router → # Skills
# ---------------------------------------------------------------------------


class TestRendering:
    async def test_recalled_memory_merged_into_memory_segment(
        self, builder: ContextBuilder,
    ) -> None:
        backend = _StubBackend(
            recall_response=[Memory(text="user likes espresso")],
        )
        eng = _engine(builder, router=SkillForgeRouter([]), backend=backend)
        ac = await eng.assemble("s", [], _budget(), turn=_turn())
        sys_msg = ac.messages[0]
        assert sys_msg["role"] == "system"
        assert "# Memory" in sys_msg["content"]
        assert "# Recalled memory" not in sys_msg["content"]
        assert "user likes espresso" in sys_msg["content"]

    async def test_router_skills_in_skills_segment(
        self, builder: ContextBuilder,
    ) -> None:
        hits = [
            RouterHit(
                qualified_id="local/git-resolver",
                name="git-resolver",
                content="resolves git refs.",
                score=0.8,
            ),
        ]
        source = _StubSource("local", hits=hits)
        eng = _engine(builder, router=SkillForgeRouter([source]), backend=_StubBackend())
        ac = await eng.assemble("s", [], _budget(), turn=_turn())
        sys_content = ac.messages[0]["content"]
        # No addendum channel and no "# Retrieved skills" heading — the
        # router body lands in segment 5 (# Skills).
        assert ac.system_prompt_addition is None
        assert "# Retrieved skills" not in sys_content
        assert "# Skills" in sys_content
        assert "git-resolver" in sys_content
        assert "[local/git-resolver]" in sys_content
        assert "resolves git refs" in sys_content

    async def test_no_skills_addition_when_empty(
        self, builder: ContextBuilder,
    ) -> None:
        eng = _engine(builder, router=SkillForgeRouter([]), backend=_StubBackend())
        ac = await eng.assemble("s", [], _budget(), turn=_turn())
        assert ac.system_prompt_addition is None

    async def test_no_recalled_block_when_empty(
        self, builder: ContextBuilder,
    ) -> None:
        eng = _engine(
            builder, router=SkillForgeRouter([]),
            backend=_StubBackend(recall_response=[]),
        )
        ac = await eng.assemble("s", [], _budget(), turn=_turn())
        assert "# Recalled memory" not in ac.messages[0]["content"]


# ---------------------------------------------------------------------------
# Graceful degrade — no backend wired
# ---------------------------------------------------------------------------


class TestNoBackendDegrade:
    async def test_recall_skipped_router_local_only(
        self, builder: ContextBuilder,
    ) -> None:
        """With ``backend=None`` the recall lane yields [] and the local
        router still feeds # Skills."""
        hits = [RouterHit(
            qualified_id="local/x", name="x", content="body", score=0.5,
        )]
        source = _StubSource("local", hits=hits)
        eng = _engine(builder, router=SkillForgeRouter([source]), backend=None)
        ac = await eng.assemble("s", [], _budget(), turn=_turn())
        assert ac.metadata["memory_hits"] == 0
        assert "local/x" in ac.metadata["injected_skill_ids"]
        assert "# Skills" in ac.messages[0]["content"]


# ---------------------------------------------------------------------------
# Failure semantics
# ---------------------------------------------------------------------------


class TestFailureSemantics:
    async def test_backend_recall_exception_propagates(
        self, builder: ContextBuilder,
    ) -> None:
        """Memory backend outage surfaces to AgentLoop. SkillForgeRouter has
        its own per-source isolation; the backend recall does NOT."""
        backend = _StubBackend(recall_raises=RuntimeError("backend down"))
        eng = _engine(builder, router=SkillForgeRouter([]), backend=backend)
        with pytest.raises(RuntimeError, match="backend down"):
            await eng.assemble("s", [], _budget(), turn=_turn())

    async def test_single_skill_source_failure_isolated(
        self, builder: ContextBuilder,
    ) -> None:
        """SkillForgeRouter's _safe_search swallows per-source exceptions;
        assemble still returns an AssembledContext."""

        class _Failing:
            name = "broken"
            weight = 1.0
            async def search(self, q, h, k):
                raise RuntimeError("source dead")

        good_hits = [RouterHit(
            qualified_id="local/x", name="x", content="", score=0.5,
        )]
        eng = _engine(
            builder,
            router=SkillForgeRouter([_Failing(), _StubSource("local", hits=good_hits)]),
            backend=_StubBackend(),
        )
        ac = await eng.assemble("s", [], _budget(), turn=_turn())
        assert "local/x" in ac.metadata["injected_skill_ids"]


# ---------------------------------------------------------------------------
# Turn fields passthrough
# ---------------------------------------------------------------------------


class TestTurnPassthrough:
    async def test_channel_chat_id_propagate_to_builder(
        self, builder: ContextBuilder,
    ) -> None:
        eng = _engine(builder, router=SkillForgeRouter([]), backend=_StubBackend())
        ac = await eng.assemble(
            "s", [], _budget(),
            turn=_turn("hello", channel="slack", chat_id="C123"),
        )
        joined = "\n".join(str(m.get("content")) for m in ac.messages)
        assert "slack" in joined
        assert "C123" in joined
