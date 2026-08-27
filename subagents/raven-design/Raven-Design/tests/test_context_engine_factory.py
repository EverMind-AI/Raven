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

import pytest

from raven.agent.context import ContextBuilder
from raven.agent.loop import AgentLoop
from raven.config.raven import (
    ContextConfig,
    HubSourceConfig,
    MemoryConfig,
    SkillForgeConfig,
    SkillForgeRouterConfig,
    VisualDomainSelectorConfig,
)
from raven.context_engine import ContextAssembler
from raven.context_engine.factory import build_context_engine
from raven.context_engine.segments import MemorySegmentBuilder, SkillsSegmentBuilder
from raven.context_engine.segments.curator import CuratorSegmentBuilder
from raven.memory_engine.skill_forge import (
    EverosSkillSource,
    HubSkillSource,
    LocalSkillSource,
)
from raven.providers.binding import ModelBinding, use_binding

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
    provider_pool=None,
    rrf_k: int | None = None,
) -> ContextAssembler:
    builder = ContextBuilder(workspace=tmp_path, start_watcher=False)
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
        skill_forge_router_config=SkillForgeRouterConfig(
            hub=HubSourceConfig(endpoint=hub_endpoint),
            **({} if rrf_k is None else {"rrf_k": rrf_k}),
        ),
        skill_forge_config=skill_forge_config,
        provider_pool=provider_pool,
    )
    assert isinstance(engine, ContextAssembler)
    return engine


def _router_sources(engine: ContextAssembler):
    skills = next(b for b in engine._builders if isinstance(b, SkillsSegmentBuilder))
    return [type(s) for s in skills._router._sources], skills._router._sources


def _skills_builder(engine: ContextAssembler) -> SkillsSegmentBuilder:
    return next(b for b in engine._builders if isinstance(b, SkillsSegmentBuilder))


def _memory_builder(engine: ContextAssembler) -> MemorySegmentBuilder:
    return next(b for b in engine._builders if isinstance(b, MemorySegmentBuilder))


def _curator_builder(engine: ContextAssembler) -> CuratorSegmentBuilder:
    return next(b for b in engine._builders if isinstance(b, CuratorSegmentBuilder))


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


# ---------------------------------------------------------------------------
# SF6: set_context_window cascades down to the Curator's trimmer -- a
# /model switch must not leave it budgeting against the pre-switch window.
# ---------------------------------------------------------------------------


class TestSetContextWindow:
    def test_engine_cascades_into_the_curator_trimmer(self, tmp_path: Path) -> None:
        engine = _build_engine(tmp_path)
        curator = _curator_builder(engine)
        assert curator.context_window_tokens == 8192
        assert curator.assembler.context_window_tokens == 8192
        assert curator.assembler.trimmer.context_window_tokens == 8192

        engine.set_context_window(4096)

        assert curator.context_window_tokens == 4096
        assert curator.assembler.context_window_tokens == 4096
        assert curator.assembler.trimmer.context_window_tokens == 4096

    def test_engine_cascade_does_not_raise_for_builders_without_the_hook(self, tmp_path: Path) -> None:
        """seg1-5 carry no budget and have no ``set_context_window`` -- the
        cascade must skip them rather than assume every builder has it."""
        engine = _build_engine(tmp_path)
        non_curator = [b for b in engine._builders if not isinstance(b, CuratorSegmentBuilder)]
        assert non_curator, "fixture should include builders other than the Curator"

        engine.set_context_window(4096)  # must not raise


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

    def test_rrf_k_forwarded_from_config(self, tmp_path: Path) -> None:
        engine = _build_engine(tmp_path, backend=_FakeBackend(), rrf_k=25)
        skills = next(b for b in engine._builders if isinstance(b, SkillsSegmentBuilder))
        assert skills._router._rrf_k == 25

    def test_rrf_k_defaults_to_config_default(self, tmp_path: Path) -> None:
        engine = _build_engine(tmp_path, backend=_FakeBackend())
        skills = next(b for b in engine._builders if isinstance(b, SkillsSegmentBuilder))
        assert skills._router._rrf_k == SkillForgeRouterConfig().rrf_k

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


class TestVisualDomainSelectorWiring:
    def test_default_skill_forge_config_enables_the_selector(self, tmp_path: Path) -> None:
        skills = _skills_builder(_build_engine(tmp_path, skill_forge_config=SkillForgeConfig()))

        assert skills._visual_domain_selector is not None
        assert len(skills._visual_domain_selector.cards) == 15
        assert skills._rewriter is not None
        assert skills._gate is not None

    def test_disabling_the_selector_keeps_general_skill_forge(self, tmp_path: Path) -> None:
        config = SkillForgeConfig(
            visual_domain_selector=VisualDomainSelectorConfig(enabled=False),
            rewrite_enabled=True,
            llm_gate_enabled=True,
        )
        skills = _skills_builder(_build_engine(tmp_path, skill_forge_config=config))

        assert skills._visual_domain_selector is None
        assert skills._rewriter is not None
        assert skills._gate is not None

    def test_selector_uses_its_configured_model_and_provider_pin(self, tmp_path: Path) -> None:
        from raven.config.schema import Config
        from raven.providers.pool import ProviderPool

        provider_config = Config()
        provider_config.agents.defaults.provider = "auto"
        provider_config.providers.anthropic.api_key = "sk-ant"
        provider_config.providers.openrouter.api_key = "sk-or"
        config = SkillForgeConfig(
            visual_domain_selector=VisualDomainSelectorConfig(
                model="claude-haiku-4-5",
                provider="openrouter",
            )
        )

        skills = _skills_builder(
            _build_engine(
                tmp_path,
                skill_forge_config=config,
                provider_pool=ProviderPool(provider_config),
            )
        )

        selector = skills._visual_domain_selector
        assert selector._pin is not None
        assert selector._pin.model == "claude-haiku-4-5"
        assert selector._pin.provider.api_key == "sk-or"


# ---------------------------------------------------------------------------
# Rewriter / gate model wiring — both must follow the agent's main model
# unless the gate has its own dedicated override.
# ---------------------------------------------------------------------------


class TestRewriterGateModelWiring:
    def test_the_rewriter_carries_no_model_of_its_own(self, tmp_path: Path) -> None:
        """It follows the conversation, so it must not be pinned to the model
        that happened to build the engine -- a session on another model would
        otherwise have its query rewritten by the first session's model."""
        engine = _build_engine(
            tmp_path,
            model="main-model",
            skill_forge_config=SkillForgeConfig(
                visual_domain_selector=VisualDomainSelectorConfig(enabled=False),
                rewrite_enabled=True,
                llm_gate_enabled=False,
            ),
        )
        skills = next(b for b in engine._builders if isinstance(b, SkillsSegmentBuilder))
        assert not hasattr(skills._rewriter, "_model")

    def test_an_unset_gate_model_stays_unset(self, tmp_path: Path) -> None:
        """Same rule one subsystem over: unset means follow the turn, not
        "inherit whatever build_context_engine was called with"."""
        engine = _build_engine(
            tmp_path,
            model="main-model",
            skill_forge_config=SkillForgeConfig(
                visual_domain_selector=VisualDomainSelectorConfig(enabled=False),
                rewrite_enabled=False,
                llm_gate_enabled=True,
                llm_gate_model=None,
            ),
        )
        skills = next(b for b in engine._builders if isinstance(b, SkillsSegmentBuilder))
        assert skills._gate._model is None

    def test_gate_prefers_dedicated_llm_gate_model(self, tmp_path: Path) -> None:
        engine = _build_engine(
            tmp_path,
            model="main-model",
            skill_forge_config=SkillForgeConfig(
                visual_domain_selector=VisualDomainSelectorConfig(enabled=False),
                rewrite_enabled=False,
                llm_gate_enabled=True,
                llm_gate_model="gate-only-model",
            ),
        )
        skills = next(b for b in engine._builders if isinstance(b, SkillsSegmentBuilder))
        assert skills._gate._model == "gate-only-model"


# ---------------------------------------------------------------------------
# AgentLoop helpers
# ---------------------------------------------------------------------------


def _make_loop(
    tmp_path: Path,
    *,
    backend=None,
    context_window_tokens: int | None = None,
) -> AgentLoop:
    return AgentLoop(
        provider=_StubProvider(),
        workspace=tmp_path,
        model="stub",
        max_iterations=2,
        restrict_to_workspace=True,
        backend=backend,
        context_window_tokens=context_window_tokens,
        context_config=ContextConfig(),
        memory_config=MemoryConfig(),
        skill_forge_router_config=SkillForgeRouterConfig(),
    )


@pytest.fixture
def loop_factory(tmp_path: Path):
    agents = []

    def make(*, backend=None, context_window_tokens: int | None = None):
        agent = _make_loop(
            tmp_path,
            backend=backend,
            context_window_tokens=context_window_tokens,
        )
        agents.append(agent)
        return agent

    yield make
    for agent in agents:
        agent.context.skills.stop_file_watcher()


class TestAgentLoopEngineDetection:
    def test_uses_default_engine_always_true(self, loop_factory) -> None:
        assert loop_factory(backend=_FakeBackend())._uses_default_engine() is True

    def test_uses_default_engine_true_without_backend(self, loop_factory) -> None:
        assert loop_factory(backend=None)._uses_default_engine() is True


class TestSelectSkillsGating:
    async def test_skill_selection_short_circuits_to_none(self, loop_factory) -> None:
        agent = loop_factory(backend=_FakeBackend())
        assert await agent._select_skills_for_turn("hi", []) is None


# ---------------------------------------------------------------------------
# Metadata-stash path for _collect_injected_skill_ids
# ---------------------------------------------------------------------------


class TestInjectedIdsFromMetadata:
    def test_returns_qualified_ids_when_stash_populated(self, loop_factory) -> None:
        agent = loop_factory(backend=_FakeBackend())
        agent._last_injected_skill_ids = ["local/x", "everos/y"]
        ids = agent._collect_injected_skill_ids(None)
        assert "local/x" in ids
        assert "everos/y" in ids

    def test_falls_back_to_legacy_when_stash_none(self, loop_factory) -> None:
        agent = loop_factory(backend=None)
        agent._last_injected_skill_ids = None
        fake_meta = MagicMock(spec_set=["source", "id"])
        fake_meta.source = "local"
        fake_meta.id = "git-resolver"
        ids = agent._collect_injected_skill_ids([fake_meta])
        assert "local/git-resolver" in ids


# ---------------------------------------------------------------------------
# SF6: AgentLoop.refresh_context_window must cascade into the Curator's
# trimmer and the MemoryConsolidator, not just AgentLoop.context_window_tokens
# -- both are built once at construction and hold a snapshot int.
# ---------------------------------------------------------------------------


class TestTheWindowFollowsTheTurnsBinding:
    """The window is a fact about the model, so every holder has to read the
    one the running turn is bound to. Held as a copy taken at construction, the
    trimmer and the consolidator would size a 1M session against whatever the
    session that built them happened to run on."""

    def test_every_holder_reads_the_window_of_the_bound_model(self, loop_factory, monkeypatch) -> None:
        from raven.providers import rates

        windows = {"stub": 8192, "other-model": 4096}
        monkeypatch.setattr(rates, "resolve_context_window", lambda model, **kw: windows.get(model))

        agent = loop_factory(backend=None)
        curator = _curator_builder(agent.context_engine)

        assert agent.context_window_tokens == 8192
        assert curator.context_window_tokens == 8192
        assert curator.assembler.trimmer.context_window_tokens == 8192
        assert agent.memory_consolidator.context_window_tokens == 8192

        with use_binding(ModelBinding(_StubProvider(), "other-model")):
            assert agent.context_window_tokens == 4096
            assert curator.context_window_tokens == 4096
            assert curator.assembler.context_window_tokens == 4096
            assert curator.assembler.trimmer.context_window_tokens == 4096
            assert agent.memory_consolidator.context_window_tokens == 4096

        # And back: leaving the turn leaves nothing behind on the holders.
        assert agent.context_window_tokens == 8192
        assert curator.assembler.trimmer.context_window_tokens == 8192

    def test_a_pinned_window_answers_for_every_model(self, loop_factory, monkeypatch) -> None:
        """An explicit ``context_window_tokens`` is a deliberate override -- the
        model a session switched to does not get to discard it."""
        from raven.providers import rates

        monkeypatch.setattr(
            rates,
            "resolve_context_window",
            lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not be asked when pinned")),
        )

        agent = loop_factory(
            context_window_tokens=8192,
        )
        curator = _curator_builder(agent.context_engine)

        with use_binding(ModelBinding(_StubProvider(), "other-model", agent._configured_window)):
            assert agent.context_window_tokens == 8192
            assert curator.context_window_tokens == 8192
            assert curator.assembler.trimmer.context_window_tokens == 8192
            assert agent.memory_consolidator.context_window_tokens == 8192
