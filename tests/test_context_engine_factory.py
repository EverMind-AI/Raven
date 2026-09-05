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

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from raven.agent.context import ContextBuilder
from raven.agent.loop import AgentLoop
from raven.agent.subagent.builtin_agents import GENERIC_AGENT
from raven.config.raven import (
    ContextConfig,
    HubSourceConfig,
    MemoryConfig,
    SkillForgeConfig,
    SkillForgeRouterConfig,
)
from raven.config.schema import SubagentsConfig
from raven.context_engine import ContextAssembler
from raven.context_engine.base import AssemblyContext
from raven.context_engine.factory import build_context_engine
from raven.context_engine.segments import (
    IdentitySegmentBuilder,
    MemorySegmentBuilder,
    SkillsSegmentBuilder,
)
from raven.context_engine.segments.curator import CuratorSegmentBuilder
from raven.memory_engine import TokenBudget
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
    rrf_k: int | None = None,
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
        skill_forge_router_config=SkillForgeRouterConfig(
            hub=HubSourceConfig(endpoint=hub_endpoint),
            **({} if rrf_k is None else {"rrf_k": rrf_k}),
        ),
        # Most assertions in this file exercise the push pipeline's wiring;
        # pull-mode wiring has its own tests below.
        skill_forge_config=skill_forge_config or SkillForgeConfig(discovery="push"),
    )
    assert isinstance(engine, ContextAssembler)
    return engine


def _router_sources(engine: ContextAssembler):
    skills = next(b for b in engine._builders if isinstance(b, SkillsSegmentBuilder))
    return [type(s) for s in skills._router._sources], skills._router._sources


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
            skill_forge_config=SkillForgeConfig(discovery="push", rewrite_enabled=True, llm_gate_enabled=False),
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
                discovery="push", rewrite_enabled=False, llm_gate_enabled=True, llm_gate_model=None
            ),
        )
        skills = next(b for b in engine._builders if isinstance(b, SkillsSegmentBuilder))
        assert skills._gate._model is None

    def test_gate_prefers_dedicated_llm_gate_model(self, tmp_path: Path) -> None:
        engine = _build_engine(
            tmp_path,
            model="main-model",
            skill_forge_config=SkillForgeConfig(
                discovery="push",
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


def _make_loop(tmp_path: Path, *, backend=None, agents=None, skill_forge_config=None) -> AgentLoop:
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
        skill_forge_config=skill_forge_config,
        agents=agents,
    )


class TestSubagentRosterReachesTheGate:
    """The join the segment-builder stubs cannot make: a real agent table, read
    through the real builder. The roster is collected lazily because the loop
    builds its context engine before its ``SubagentManager`` exists, so a
    snapshot taken at wiring time would be empty every run.

    Push discovery, explicitly: pull is the default and builds no skills
    segment at all, so there is no gate there to hand a roster to."""

    @staticmethod
    def _push(tmp_path: Path, agents) -> AgentLoop:
        return _make_loop(tmp_path, agents=agents, skill_forge_config=SkillForgeConfig(discovery="push"))

    def test_a_configured_specialist_reaches_the_gate(self, tmp_path: Path) -> None:
        agents = SubagentsConfig(
            agents=[
                {
                    "name": "Scribe",
                    "kind": "cli",
                    "command": "echo hi",
                    "description": "turns a source document into a deck",
                }
            ]
        ).agents
        agent = self._push(tmp_path, agents)
        skills = next(b for b in agent.context_engine._builders if isinstance(b, SkillsSegmentBuilder))
        roster = skills._collect_subagent_roster()
        assert roster is not None
        assert "Scribe" in roster
        assert "turns a source document into a deck" in roster

    def test_the_generic_agent_is_not_offered_as_an_overlap(self, tmp_path: Path) -> None:
        agents = SubagentsConfig(agents=[{"name": "Scribe", "kind": "cli", "command": "echo hi"}]).agents
        agent = self._push(tmp_path, agents)
        skills = next(b for b in agent.context_engine._builders if isinstance(b, SkillsSegmentBuilder))
        roster = skills._collect_subagent_roster() or ""
        assert "Scribe" in roster
        assert "no capability bias" not in roster


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


# ---------------------------------------------------------------------------
# SF6: the window every holder reads must be the bound model's, not a copy
# taken when the holder was built.
# ---------------------------------------------------------------------------


class TestTheWindowFollowsTheTurnsBinding:
    """The window is a fact about the model, so every holder has to read the
    one the running turn is bound to. Held as a copy taken at construction, the
    trimmer and the consolidator would size a 1M session against whatever the
    session that built them happened to run on."""

    def test_every_holder_reads_the_window_of_the_bound_model(self, tmp_path: Path, monkeypatch) -> None:
        from raven.providers import rates

        windows = {"stub": 8192, "other-model": 4096}
        monkeypatch.setattr(rates, "resolve_context_window", lambda model, **kw: windows.get(model))

        agent = _make_loop(tmp_path, backend=None)
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

    def test_a_pinned_window_answers_for_every_model(self, tmp_path: Path, monkeypatch) -> None:
        """An explicit ``context_window_tokens`` is a deliberate override -- the
        model a session switched to does not get to discard it."""
        from raven.providers import rates

        monkeypatch.setattr(
            rates,
            "resolve_context_window",
            lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not be asked when pinned")),
        )

        agent = AgentLoop(
            provider=_StubProvider(),
            workspace=tmp_path,
            model="stub",
            max_iterations=2,
            restrict_to_workspace=True,
            context_window_tokens=8192,
            context_config=ContextConfig(),
            memory_config=MemoryConfig(),
            skill_forge_router_config=SkillForgeRouterConfig(),
        )
        curator = _curator_builder(agent.context_engine)

        with use_binding(ModelBinding(_StubProvider(), "other-model", agent._configured_window)):
            assert agent.context_window_tokens == 8192
            assert curator.context_window_tokens == 8192
            assert curator.assembler.trimmer.context_window_tokens == 8192
            assert agent.memory_consolidator.context_window_tokens == 8192


# ---------------------------------------------------------------------------
# Pull discovery (the default): no skills segment, scent + router exposed
# ---------------------------------------------------------------------------


def test_pull_default_drops_the_skills_segment_and_wires_scent(tmp_path):
    engine = _build_engine(tmp_path, skill_forge_config=SkillForgeConfig())
    assert not any(isinstance(b, SkillsSegmentBuilder) for b in engine._builders)
    assert engine._scent is not None
    assert engine.skills_router is not None


def test_push_config_restores_the_legacy_pipeline(tmp_path):
    engine = _build_engine(tmp_path, skill_forge_config=SkillForgeConfig(discovery="push"))
    assert any(isinstance(b, SkillsSegmentBuilder) for b in engine._builders)
    assert engine._scent is None
    assert engine.skills_router is not None


# ---------------------------------------------------------------------------
# Ownership reaching the identity prompt, and the paths it depends on
# ---------------------------------------------------------------------------


def _assembly_ctx() -> AssemblyContext:
    return AssemblyContext(
        session_key="s",
        current_message="hi",
        media=None,
        channel=None,
        chat_id=None,
        session_messages=[],
        budget=TokenBudget(100_000, 4_000, 2_000, 1_000, 93_000),
    )


class TestOwnershipReachesTheIdentityPrompt:
    """The join no stub can make: a real config row, through the real registry,
    into the assembled prompt -- and a real tool table deciding whether the
    prohibition it carries is one the model can act on."""

    OWNS = "owns decks. Do not build the deck yourself."

    @pytest.fixture(autouse=True)
    def _isolated_switches(self, tmp_path: Path, monkeypatch) -> None:
        """Point the live off-switch read at a file of our own.

        Every case here goes through the real tool table, which reads
        ``tools.disabledTools`` from whatever config file is current -- so without
        this they consult the developer's own ``~/.raven/config.json``, and a
        locally disabled ``spawn`` makes them red on one machine and green in CI.
        """
        self._switches = tmp_path / "config.json"
        self._disable()
        monkeypatch.setattr("raven.config.loader._current_config_path", self._switches)

    def _disable(self, *names: str) -> None:
        self._switches.write_text(json.dumps({"tools": {"disabledTools": list(names)}}), encoding="utf-8")

    @staticmethod
    def _identity(agent: AgentLoop) -> IdentitySegmentBuilder:
        return next(b for b in agent.context_engine._builders if isinstance(b, IdentitySegmentBuilder))

    async def _text(self, agent: AgentLoop) -> str:
        return (await self._identity(agent).build(_assembly_ctx())).text

    def _scribe(self, kind: str = "cli") -> list:
        row = {"name": "Scribe", "kind": kind, "owns": self.OWNS}
        if kind == "cli":
            row["command"] = "echo hi"
        return SubagentsConfig(agents=[row]).agents

    async def test_a_custom_builtin_specialist_gets_its_line(self, tmp_path: Path) -> None:
        """A built-in row is on the same dispatchable table as an external one, so
        narrowing one into a specialist has to be declarable the same way."""
        assert f"`Scribe` {self.OWNS}" in await self._text(_make_loop(tmp_path, agents=self._scribe("builtin")))

    async def test_an_external_specialist_gets_its_line(self, tmp_path: Path) -> None:
        """Regression guard for the path that already worked, not a proof of the
        fix: this one is green on either side of it."""
        assert f"`Scribe` {self.OWNS}" in await self._text(_make_loop(tmp_path, agents=self._scribe()))

    async def test_ownership_on_the_generic_row_claims_nothing(self, tmp_path: Path) -> None:
        """Paired with a real specialist so the absence is selective: asserting
        only that the claim is missing would also pass on an install where the
        whole section failed to render."""
        agents = SubagentsConfig(
            agents=[
                {"name": GENERIC_AGENT, "kind": "builtin", "owns": "owns everything. Do not do anything yourself."},
                {"name": "Scribe", "kind": "cli", "command": "echo hi", "owns": self.OWNS},
            ]
        ).agents
        text = await self._text(_make_loop(tmp_path, agents=agents))
        assert f"`Scribe` {self.OWNS}" in text
        assert "owns everything" not in text

    async def test_withholding_every_dispatch_path_retires_the_prohibition(self, tmp_path: Path) -> None:
        """Read from the file per turn, so a switch flipped on the settings page
        takes the section away and putting it back brings it back -- without which
        the prompt forbids work the turn has no way to hand off."""
        agent = _make_loop(tmp_path, agents=self._scribe())
        offered = {d["function"]["name"] for d in agent.tools.get_definitions()}
        assert {"spawn", "run_subagent_dag"} <= offered

        assert "## Delegation" in await self._text(agent)

        self._disable("spawn", "run_subagent_dag")
        assert "## Delegation" not in await self._text(agent)

        self._disable()
        assert "## Delegation" in await self._text(agent)

    async def test_withholding_the_whole_tool_table_retires_it_too(self, tmp_path: Path) -> None:
        """Naming the two dispatch tools is not the only way to reach zero live
        paths. Withholding every registered tool reaches it through a successful
        but empty lookup, which the gate first shipped reporting as both paths
        live -- so the prohibition survived on a turn offering no tools at all.
        """
        agent = _make_loop(tmp_path, agents=self._scribe())
        every = sorted({d["function"]["name"] for d in agent.tools.get_definitions()})
        assert {"spawn", "run_subagent_dag"} <= set(every)
        assert "## Delegation" in await self._text(agent)

        self._disable(*every)
        assert agent.tools.get_definitions() == []
        assert "## Delegation" not in await self._text(agent)

    async def test_withholding_one_path_keeps_the_other_named(self, tmp_path: Path) -> None:
        self._disable("spawn")
        text = await self._text(_make_loop(tmp_path, agents=self._scribe()))
        assert f"`Scribe` {self.OWNS}" in text
        assert "`run_subagent_dag`" in text
        assert "`spawn`" not in text
