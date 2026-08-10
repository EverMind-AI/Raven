"""ContextAssembler — factory wiring + SkillForgeRouter assembly.

The factory no longer dispatches on ``context.engine`` — it always
builds the single :class:`ContextAssembler` from a flat SegmentBuilder
list, assembling the ``SkillsSegmentBuilder``'s SkillForgeRouter from:

- Local (always),
- Mass (when ``mass.endpoint`` is set),
- Everos (when a backend is supplied).

With no backend the engine still constructs (recall lane yields [],
router runs Local-only). AgentLoop always delegates skill selection to
the engine (``_uses_default_engine`` is always True), and
``_collect_injected_skill_ids`` prefers the assembled-metadata stash.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from raven.agent.context import ContextBuilder
from raven.agent.loop import AgentLoop
from raven.config.raven import (
    ContextConfig,
    HubSourceConfig,
    MemoryConfig,
    SkillForgeConfig,
    SkillForgeRouterConfig,
)
from raven.context_engine import ContextAssembler
from raven.context_engine.factory import build_context_engine
from raven.context_engine.segments import MemorySegmentBuilder, SkillsSegmentBuilder
from raven.memory_engine.skill_forge import (
    EverosSkillSource,
    HubSkillSource,
    LocalSkillSource,
)

# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


class _FakeBackend:
    async def start(self):
        pass

    async def stop(self):
        pass

    async def feedback(self, signals):
        pass

    async def store(self, session_id, messages, *, metadata=None):
        pass

    async def recall(self, query, *, user_id=None, agent_id=None, top_k):
        return []


class _StubProvider:
    api_key = "test"

    def get_default_model(self) -> str:
        return "stub"

    async def chat(self, *args, **kwargs):
        raise NotImplementedError

    async def chat_with_retry(self, *args, **kwargs):
        raise NotImplementedError


def _stub_get_defs() -> list[dict]:
    return []


def _build_engine(
    tmp_path: Path,
    *,
    backend=None,
    hub_endpoint: str | None = None,
    memory_config: MemoryConfig | None = None,
    model: str = "stub",
    skill_forge_config: SkillForgeConfig | None = None,
) -> ContextAssembler:
    builder = ContextBuilder(workspace=tmp_path)
    engine = build_context_engine(
        workspace=tmp_path,
        config=ContextConfig(),
        builder=builder,
        provider=_StubProvider(),
        model=model,
        context_window_tokens=8192,
        get_tool_definitions=_stub_get_defs,
        backend=backend,
        memory_config=memory_config or MemoryConfig(),
        skill_forge_config=skill_forge_config,
        skill_forge_router_config=SkillForgeRouterConfig(
            hub=HubSourceConfig(endpoint=hub_endpoint),
        ),
    )
    assert isinstance(engine, ContextAssembler)
    return engine


def _router_sources(engine: ContextAssembler):
    skills = _skills_builder(engine)
    return [type(s) for s in skills._router._sources], skills._router._sources


def _skills_builder(engine: ContextAssembler) -> SkillsSegmentBuilder:
    return next(b for b in engine._builders if isinstance(b, SkillsSegmentBuilder))


def _memory_builder(engine: ContextAssembler) -> MemorySegmentBuilder:
    return next(b for b in engine._builders if isinstance(b, MemorySegmentBuilder))


# ---------------------------------------------------------------------------
# Factory — always builds the assembler
# ---------------------------------------------------------------------------


class TestFactory:
    def test_returns_assembler_with_backend(self, tmp_path: Path) -> None:
        assert isinstance(_build_engine(tmp_path, backend=_FakeBackend()), ContextAssembler)

    def test_returns_assembler_without_backend(self, tmp_path: Path) -> None:
        engine = _build_engine(tmp_path, backend=None)
        assert isinstance(engine, ContextAssembler)
        assert _memory_builder(engine)._backend is None

    def test_skill_forge_llms_inherit_agent_model(self, tmp_path: Path) -> None:
        engine = _build_engine(
            tmp_path,
            model="minimax/minimax-m3",
            skill_forge_config=SkillForgeConfig(),
        )
        skills = _skills_builder(engine)
        assert skills._rewriter._model == "minimax/minimax-m3"
        assert skills._gate._model == "minimax/minimax-m3"

    def test_gate_model_override_takes_priority(self, tmp_path: Path) -> None:
        engine = _build_engine(
            tmp_path,
            model="minimax/minimax-m3",
            skill_forge_config=SkillForgeConfig(llm_gate_model="anthropic/claude-sonnet-4-5"),
        )
        skills = _skills_builder(engine)
        assert skills._rewriter._model == "minimax/minimax-m3"
        assert skills._gate._model == "anthropic/claude-sonnet-4-5"


# ---------------------------------------------------------------------------
# SkillForgeRouter assembly — which sources are present
# ---------------------------------------------------------------------------


class TestSkillForgeRouterAssembly:
    def test_local_source_always_present(self, tmp_path: Path) -> None:
        types, _ = _router_sources(_build_engine(tmp_path, backend=_FakeBackend()))
        assert LocalSkillSource in types

    def test_everos_source_present_when_backend(self, tmp_path: Path) -> None:
        types, _ = _router_sources(_build_engine(tmp_path, backend=_FakeBackend()))
        assert EverosSkillSource in types

    def test_everos_source_absent_without_backend(self, tmp_path: Path) -> None:
        types, _ = _router_sources(_build_engine(tmp_path, backend=None))
        assert EverosSkillSource not in types

    def test_hub_source_omitted_when_endpoint_unset(self, tmp_path: Path) -> None:
        types, _ = _router_sources(_build_engine(tmp_path, backend=_FakeBackend(), hub_endpoint=None))
        assert HubSkillSource not in types

    def test_hub_source_present_when_endpoint_set(self, tmp_path: Path) -> None:
        types, _ = _router_sources(_build_engine(tmp_path, backend=_FakeBackend(), hub_endpoint="http://hub.test"))
        assert HubSkillSource in types

    def test_track_ids_from_memory_config(self, tmp_path: Path) -> None:
        engine = _build_engine(
            tmp_path,
            backend=_FakeBackend(),
            memory_config=MemoryConfig(
                user_id="alice",
                agent_id="robo",
            ),
        )
        assert _memory_builder(engine)._user_id == "alice"
        _, sources = _router_sources(engine)
        everos = next(s for s in sources if isinstance(s, EverosSkillSource))
        assert everos._agent_id == "robo"


# ---------------------------------------------------------------------------
# AgentLoop helpers
# ---------------------------------------------------------------------------


def _make_loop(tmp_path: Path, *, backend=None) -> AgentLoop:
    return AgentLoop(
        provider=_StubProvider(),
        workspace=tmp_path,
        model="stub",
        max_iterations=2,
        restrict_to_workspace=True,
        backend=backend,
        context_config=ContextConfig(),
        memory_config=MemoryConfig(),
        skill_forge_router_config=SkillForgeRouterConfig(),
    )


class TestAgentLoopEngineDetection:
    def test_uses_default_engine_always_true(self, tmp_path: Path) -> None:
        assert _make_loop(tmp_path, backend=_FakeBackend())._uses_default_engine() is True

    def test_uses_default_engine_true_without_backend(self, tmp_path: Path) -> None:
        assert _make_loop(tmp_path, backend=None)._uses_default_engine() is True


class TestSelectSkillsGating:
    async def test_skill_selection_short_circuits_to_none(self, tmp_path: Path) -> None:
        agent = _make_loop(tmp_path, backend=_FakeBackend())
        assert await agent._select_skills_for_turn("hi", []) is None


# ---------------------------------------------------------------------------
# Metadata-stash path for _collect_injected_skill_ids
# ---------------------------------------------------------------------------


class TestInjectedIdsFromMetadata:
    def test_returns_qualified_ids_when_stash_populated(self, tmp_path: Path) -> None:
        agent = _make_loop(tmp_path, backend=_FakeBackend())
        agent._last_injected_skill_ids = ["local/x", "everos/y"]
        ids = agent._collect_injected_skill_ids(None)
        assert "local/x" in ids
        assert "everos/y" in ids

    def test_falls_back_to_legacy_when_stash_none(self, tmp_path: Path) -> None:
        agent = _make_loop(tmp_path, backend=None)
        agent._last_injected_skill_ids = None
        fake_meta = MagicMock(spec_set=["source", "id"])
        fake_meta.source = "local"
        fake_meta.id = "git-resolver"
        ids = agent._collect_injected_skill_ids([fake_meta])
        assert "local/git-resolver" in ids
