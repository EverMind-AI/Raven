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
    SkillForgeRouterConfig,
)
from raven.context_engine import ContextAssembler
from raven.context_engine.factory import build_context_engine
from raven.context_engine.segments import (
    BootstrapSegmentBuilder,
    MemorySegmentBuilder,
    SkillsSegmentBuilder,
)
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
) -> ContextAssembler:
    builder = ContextBuilder(workspace=tmp_path)
    engine = build_context_engine(
        workspace=tmp_path,
        config=ContextConfig(),
        builder=builder,
        provider=_StubProvider(),
        model="stub",
        context_window_tokens=8192,
        get_tool_definitions=_stub_get_defs,
        backend=backend,
        memory_config=memory_config or MemoryConfig(),
        skill_forge_router_config=SkillForgeRouterConfig(
            hub=HubSourceConfig(endpoint=hub_endpoint),
        ),
    )
    assert isinstance(engine, ContextAssembler)
    return engine


def _router_sources(engine: ContextAssembler):
    skills = next(b for b in engine._builders if isinstance(b, SkillsSegmentBuilder))
    return [type(s) for s in skills._router._sources], skills._router._sources


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


class TestRepoRulesBootstrap:
    """Raven is coding-only: segment 2 is the repository's own rules files,
    read-only and size-capped. Nothing is ever seeded to the workspace."""

    def test_injects_repo_rules_files(self, tmp_path: Path) -> None:
        engine = _build_engine(tmp_path)
        builder = next(b for b in engine._builders if isinstance(b, BootstrapSegmentBuilder))
        assert builder._bootstrap_files is None  # renderer default
        from raven.context_engine.segments import render

        # Same list opencode reads at project level (its AGENTS.md convention,
        # plus claude-code's CLAUDE.md and its own CONTEXT.md).
        assert render.BOOTSTRAP_FILES == ["AGENTS.md", "CLAUDE.md", "CONTEXT.md"]

    def test_reads_repo_agents_md(self, tmp_path: Path) -> None:
        from raven.context_engine.segments import render

        (tmp_path / "AGENTS.md").write_text("# Repo rules\nuse uv only", encoding="utf-8")
        text = render.load_bootstrap_files(tmp_path)
        assert "## AGENTS.md" in text and "use uv only" in text

    def test_empty_repo_injects_nothing(self, tmp_path: Path) -> None:
        """No substitute text of raven's own when the repo has no rules file."""
        from raven.context_engine.segments import render

        assert render.load_bootstrap_files(tmp_path) == ""
        assert not (tmp_path / "AGENTS.md").exists()

    def test_injected_file_is_size_capped(self, tmp_path: Path) -> None:
        from raven.context_engine.segments import render

        (tmp_path / "AGENTS.md").write_text("x" * 60_000, encoding="utf-8")
        text = render.load_bootstrap_files(tmp_path)
        assert len(text) < 30_000 and "truncated" in text

    def test_context_builder_reads_the_same_files(self, tmp_path: Path) -> None:
        (tmp_path / "CLAUDE.md").write_text("repo rule", encoding="utf-8")
        assert "repo rule" in ContextBuilder(workspace=tmp_path)._load_bootstrap_files()

    def test_reads_repo_context_md(self, tmp_path: Path) -> None:
        from raven.context_engine.segments import render

        (tmp_path / "CONTEXT.md").write_text("# Terms\nsegment: one prompt block", encoding="utf-8")
        text = render.load_bootstrap_files(tmp_path)
        assert "## CONTEXT.md" in text and "one prompt block" in text

    def test_engine_construction_leaves_the_workspace_untouched(self, tmp_path: Path) -> None:
        """Raven is coding-only: the workspace is the user's repository, so
        building the context engine must not create a single file in it.
        Regression guard for the four paths AgentEval had to git-exclude
        (``sessions``, ``agent_memory``, ``user_memory``, ``memory/.curator``)."""
        _build_engine(tmp_path, backend=_FakeBackend())
        assert list(tmp_path.iterdir()) == []

    def test_memory_store_creates_nothing_until_a_write(self, tmp_path: Path) -> None:
        from raven.memory_engine.consolidate.consolidator import MemoryStore

        store = MemoryStore(tmp_path)
        assert list(tmp_path.iterdir()) == []
        assert store.read_long_term() == ""  # reads still work: absent -> empty
        store.write_long_term("remembered")
        assert store.memory_file.read_text(encoding="utf-8") == "remembered"

    def test_history_is_bounded_by_the_budget(self, tmp_path: Path) -> None:
        """The Curator's slow path also kept a long session inside the budget.
        Dropping oldest-exchange-first replaces it without an LLM call."""
        import asyncio

        from raven.context_engine.base import AssemblyContext
        from raven.context_engine.segments.history import HistorySegmentBuilder
        from raven.memory_engine import TokenBudget

        messages: list[dict] = []
        for i in range(12):
            messages.append({"role": "user", "content": f"request {i} " + "x" * 4_000})
            messages.append({"role": "assistant", "content": f"answer {i}"})

        def _ctx(available_history: int) -> AssemblyContext:
            return AssemblyContext(
                session_key="cli:c",
                current_message="next",
                media=None,
                channel="cli",
                chat_id="c",
                session_messages=messages,
                budget=TokenBudget(
                    context_length=32_768,
                    reserved_output=4_096,
                    reserved_tools=100,
                    reserved_system=500,
                    available_history=available_history,
                ),
            )

        seg = asyncio.run(HistorySegmentBuilder().build(_ctx(3_000)))
        assert len(seg.history) < len(messages)
        assert seg.meta["dropped_exchanges"] > 0
        # Oldest first, and never opening mid tool-exchange.
        assert seg.history[0]["role"] == "user"
        assert "request 0 " not in seg.history[0]["content"]
        assert seg.history[-1]["content"] == "answer 11"

        # Roomy budget: nothing is dropped.
        roomy = asyncio.run(HistorySegmentBuilder().build(_ctx(200_000)))
        assert len(roomy.history) == len(messages)
        assert "dropped_exchanges" not in roomy.meta

        # A non-positive budget cannot be satisfied by dropping, so the history
        # passes through rather than vanishing.
        starved = asyncio.run(HistorySegmentBuilder().build(_ctx(0)))
        assert len(starved.history) == len(messages)

    def test_history_builder_writes_nothing_at_all(self, tmp_path: Path) -> None:
        """The Curator wrote a manifest, archives and per-turn traces. Its
        deterministic replacement holds no state, so there is nothing to place
        outside the workspace -- and nothing to leak into it."""
        import inspect

        from raven.context_engine.segments.history import HistorySegmentBuilder

        source = inspect.getsource(HistorySegmentBuilder)
        for writer in ("open(", "write_text", "mkdir", "append_trace"):
            assert writer not in source, writer

    def test_history_is_projected_and_starts_at_a_user_message(self, tmp_path: Path) -> None:
        """Provider-safe keys only, and never opening mid tool-exchange -- the
        two properties the Curator's fast path provided."""
        import asyncio

        from raven.context_engine.base import AssemblyContext
        from raven.context_engine.segments.history import HistorySegmentBuilder
        from raven.memory_engine import TokenBudget

        messages = [
            {"role": "assistant", "content": "orphan tail of an earlier turn", "internal": "drop me"},
            {"role": "user", "content": "the request", "timestamp": "2026-08-05T00:00:00"},
            {"role": "assistant", "content": "the answer", "cost_usd": 0.1},
        ]
        ctx = AssemblyContext(
            session_key="cli:c",
            current_message="the request",
            media=None,
            channel="cli",
            chat_id="c",
            session_messages=messages,
            budget=TokenBudget(
                context_length=4096,
                reserved_output=512,
                reserved_tools=100,
                reserved_system=500,
                available_history=2984,
            ),
        )

        seg = asyncio.run(HistorySegmentBuilder().build(ctx))

        assert seg.text == ""
        assert [m["role"] for m in seg.history] == ["user", "assistant"]
        assert all("timestamp" not in m and "cost_usd" not in m and "internal" not in m for m in seg.history)

    def test_identity_is_the_coding_identity(self, tmp_path: Path) -> None:
        from raven.context_engine.segments import render

        text = render.identity_text(tmp_path)
        assert "software engineering tasks" in text
        assert "personal AI assistant" not in text
