"""The five SegmentBuilders, tested in isolation.

Each builder is fed a fake :class:`AssemblyContext` and asserted to
reproduce the segment its old inline block in ``ContextBuilder`` emitted.
"""

from __future__ import annotations

import types
from pathlib import Path

from raven.agent.context import ContextBuilder
from raven.agent.subagent.builtin_agents import GENERIC_AGENT
from raven.context_engine.segments import (
    ActiveSkillsSegmentBuilder,
    BootstrapSegmentBuilder,
    IdentitySegmentBuilder,
    MemorySegmentBuilder,
    SkillsSegmentBuilder,
    render,
)
from raven.contracts.assembled import TokenBudget
from raven.contracts.context import AssemblyContext
from raven.contracts.memory import Memory
from raven.memory_engine.skill_forge import RouterHit, SkillForgeRouter


def _ctx(tmp_path: Path, msg: str = "hi", session=None) -> AssemblyContext:
    return AssemblyContext(
        session_key="s",
        current_message=msg,
        media=None,
        channel=None,
        chat_id=None,
        session_messages=session or [],
        budget=TokenBudget(100_000, 4_000, 2_000, 1_000, 93_000),
    )


class _Backend:
    def __init__(self, mems):
        self._mems = mems
        self.calls = []

    async def recall(self, query, *, user_id=None, agent_id=None, top_k):
        self.calls.append(
            {
                "query": query,
                "user_id": user_id,
                "agent_id": agent_id,
                "top_k": top_k,
            }
        )
        return list(self._mems)


def _tool_defs(*names: str):
    """A ``get_tool_definitions`` callable in OpenAI function-call shape."""
    return lambda: [{"type": "function", "function": {"name": n, "parameters": {}}} for n in names]


class _Source:
    name = "local"
    weight = 1.0

    def __init__(self, hits):
        self._hits = hits

    async def search(self, query, history, k):
        return list(self._hits)


# ---------------------------------------------------------------------------


def _provider_config(model: str, provider: str, api_key: str | None = None, api_base: str | None = None):
    """A config stand-in exposing exactly what _resolved_model_id reads."""
    cfg = types.SimpleNamespace(agents=types.SimpleNamespace(defaults=types.SimpleNamespace(model=model)))
    cfg.get_provider_name = lambda m=None: provider
    cfg.get_api_key = lambda m=None: api_key
    cfg.get_api_base = lambda m=None: api_base
    return cfg


class TestIdentityBootstrap:
    async def test_identity_matches_legacy(self, tmp_path: Path) -> None:
        seg = await IdentitySegmentBuilder(tmp_path).build(_ctx(tmp_path))
        legacy = ContextBuilder(workspace=tmp_path)._get_identity()
        assert seg.text == legacy

    async def test_identity_names_both_directories(self, tmp_path: Path) -> None:
        """The model is told where it works and where its memory lives, and the
        two are not the same directory."""
        from raven.agent.workdir import bind

        home = tmp_path / "home"
        home.mkdir()
        project = tmp_path / "project"
        project.mkdir()

        with bind(project):
            seg = await IdentitySegmentBuilder(home).build(_ctx(home))

        assert f"Working directory: {project}" in seg.text
        assert f"Agent home: {home}" in seg.text
        assert f"{home}/user_memory/profile/user.md" in seg.text
        assert str(project / "user_memory") not in seg.text

    async def test_identity_falls_back_to_agent_home_when_unbound(self, tmp_path: Path) -> None:
        """No binding means the pre-split single-directory behaviour."""
        seg = await IdentitySegmentBuilder(tmp_path).build(_ctx(tmp_path))
        assert f"Working directory: {tmp_path}" in seg.text

    async def test_bootstrap_none_when_no_files(self, tmp_path: Path) -> None:
        seg = await BootstrapSegmentBuilder(tmp_path).build(_ctx(tmp_path))
        assert seg is None

    async def test_bootstrap_renders_existing(self, tmp_path: Path) -> None:
        (tmp_path / "TOOLS.md").write_text("tool docs", encoding="utf-8")
        seg = await BootstrapSegmentBuilder(tmp_path).build(_ctx(tmp_path))
        assert seg is not None
        assert "## TOOLS.md" in seg.text
        assert "tool docs" in seg.text

    def test_identity_contains_model_id(self, tmp_path: Path) -> None:
        prompt = render.identity_text(tmp_path, model="openrouter/some-model")
        assert "openrouter/some-model" in prompt

    def test_identity_default_model_resolved_lazily(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setattr(render, "_resolved_model_id", lambda: "openrouter/acme/lazy-model")
        assert "openrouter/acme/lazy-model" in render.identity_text(tmp_path)

    def test_legacy_identity_contains_model_id(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setattr(render, "_resolved_model_id", lambda: "openrouter/acme/lazy-model")
        legacy = ContextBuilder(workspace=tmp_path)._get_identity()
        assert "openrouter/acme/lazy-model" in legacy

    def test_resolved_model_id_codex_matches_wire_form(self, monkeypatch) -> None:
        """openai_codex bypasses LiteLLM and its client strips the provider
        prefix before sending; the identity line must report that wire form,
        not the stored one."""
        cfg = _provider_config("openai-codex/gpt-5.1-codex", "openai_codex")
        monkeypatch.setattr("raven.config.loader.load_config", lambda: cfg)
        assert render._resolved_model_id() == "gpt-5.1-codex"

    def test_resolved_model_id_azure_matches_wire_form(self, monkeypatch) -> None:
        """azure_openai sends the id as a URL deployment name with the prefix
        stripped; the identity line must match."""
        cfg = _provider_config("azure_openai/gpt-4o", "azure_openai")
        monkeypatch.setattr("raven.config.loader.load_config", lambda: cfg)
        assert render._resolved_model_id() == "gpt-4o"

    def test_resolved_model_id_gateway_prefix_applied(self, monkeypatch) -> None:
        cfg = _provider_config("acme/some-model", "openrouter", api_key="sk-or-v1-abc")
        monkeypatch.setattr("raven.config.loader.load_config", lambda: cfg)
        assert render._resolved_model_id() == "openrouter/acme/some-model"


class TestMemory:
    async def test_recall_merged_under_memory_heading(self, tmp_path: Path) -> None:
        backend = _Backend([Memory(text="likes espresso")])
        b = MemorySegmentBuilder(
            ContextBuilder(workspace=tmp_path).memory,
            backend=backend,
            user_id="alice",
            memory_top_k=7,
        )
        seg = await b.build(_ctx(tmp_path, "coffee"))
        assert "# Memory" in seg.text
        assert "- likes espresso" in seg.text
        assert "# Recalled memory" not in seg.text
        assert seg.meta["memory_hits"] == 1
        assert backend.calls == [
            {"query": "coffee", "user_id": "alice", "agent_id": None, "top_k": 7},
        ]

    async def test_no_backend_empty_text(self, tmp_path: Path) -> None:
        b = MemorySegmentBuilder(ContextBuilder(workspace=tmp_path).memory, backend=None)
        seg = await b.build(_ctx(tmp_path))
        # Empty workspace + no recall → no memory block.
        assert seg.text == ""
        assert seg.meta["memory_hits"] == 0


class TestRecallHasATurnBudget:
    """Memory is an enhancement, not a precondition for answering.

    The recall was awaited with no bound at all, in front of the model call --
    a backend that hung left the turn with nothing to show and no way out but
    the user cancelling it.
    """

    async def test_a_hanging_backend_degrades_to_no_hits(self, monkeypatch, tmp_path) -> None:
        import asyncio

        from raven.context_engine.segments import memory as memory_segment

        monkeypatch.setattr(memory_segment, "_RECALL_BUDGET_S", 0.05)

        class _Hanging:
            async def recall(self, query, *, user_id=None, agent_id=None, top_k):
                await asyncio.sleep(30)

        builder = memory_segment.MemorySegmentBuilder(
            ContextBuilder(workspace=tmp_path).memory,
            backend=_Hanging(),
        )
        assert await builder._recall("hi") == []

    async def test_a_raising_backend_degrades_to_no_hits(self, tmp_path) -> None:
        from raven.context_engine.segments import memory as memory_segment

        class _Broken:
            async def recall(self, query, *, user_id=None, agent_id=None, top_k):
                raise RuntimeError("memory service down")

        builder = memory_segment.MemorySegmentBuilder(
            ContextBuilder(workspace=tmp_path).memory,
            backend=_Broken(),
        )
        assert await builder._recall("hi") == []

    async def test_a_timeout_and_a_programming_error_log_distinguishably(self, monkeypatch, tmp_path, caplog) -> None:
        """A permanent bug (a mismatched ``recall`` signature, an
        ``AttributeError`` in a plugin) must not read the same as a slow-but-
        healthy service: only a timeout should say "budget", and only the
        other should name the exception that actually happened.
        """
        import asyncio
        import logging

        from loguru import logger

        from raven.context_engine.segments import memory as memory_segment

        monkeypatch.setattr(memory_segment, "_RECALL_BUDGET_S", 0.05)

        class _Hanging:
            async def recall(self, query, *, user_id=None, agent_id=None, top_k):
                await asyncio.sleep(30)

        class _WrongSignature:
            async def recall(self, query, *, user_id=None, agent_id=None, top_k):
                raise TypeError("recall() got an unexpected keyword argument 'foo'")

        # Bridge loguru -> stdlib caplog (loguru doesn't write to logging by default)
        handler_id = logger.add(lambda msg: logging.getLogger("loguru.bridge").warning(msg), level="WARNING")
        try:
            with caplog.at_level(logging.WARNING, logger="loguru.bridge"):
                timeout_builder = memory_segment.MemorySegmentBuilder(
                    ContextBuilder(workspace=tmp_path).memory,
                    backend=_Hanging(),
                )
                await timeout_builder._recall("hi")
                timeout_text = caplog.records[-1].message
                caplog.clear()

                error_builder = memory_segment.MemorySegmentBuilder(
                    ContextBuilder(workspace=tmp_path).memory,
                    backend=_WrongSignature(),
                )
                await error_builder._recall("hi")
                error_text = caplog.records[-1].message
        finally:
            logger.remove(handler_id)

        assert timeout_text != error_text
        assert "budget" in timeout_text
        assert "TypeError" in error_text
        assert "unexpected keyword argument" in error_text

    async def test_a_healthy_backend_is_untouched(self, tmp_path) -> None:
        from raven.context_engine.segments import memory as memory_segment

        class _Fine:
            async def recall(self, query, *, user_id=None, agent_id=None, top_k):
                return ["hit"]

        builder = memory_segment.MemorySegmentBuilder(
            ContextBuilder(workspace=tmp_path).memory,
            backend=_Fine(),
        )
        assert await builder._recall("hi") == ["hit"]

    def test_the_plugin_budget_is_strictly_inside_the_framework_one(self) -> None:
        """Equal budgets would let the framework's cancellation pre-empt the
        plugin's own timeout handling, which is what demotes the service and
        makes every later turn cost nothing."""
        from raven.context_engine.segments import memory as memory_segment
        from raven_everos import backend as everos_backend

        assert everos_backend._RECALL_TIMEOUT_S < memory_segment._RECALL_BUDGET_S


class TestSkills:
    async def test_router_hits_render_into_skills(self, tmp_path: Path) -> None:
        hits = [RouterHit(qualified_id="local/g", name="g", content="how to git", score=0.9)]
        b = SkillsSegmentBuilder(SkillForgeRouter([_Source(hits)]), skill_top_k=5)
        seg = await b.build(_ctx(tmp_path))
        assert seg.text.startswith("# Skills")
        assert "### Skill: g  [local/g]" in seg.text
        assert "how to git" in seg.text
        assert seg.meta["injected_skill_ids"] == ["local/g"]

    async def test_empty_hits_empty_text(self, tmp_path: Path) -> None:
        b = SkillsSegmentBuilder(SkillForgeRouter([]), skill_top_k=5)
        seg = await b.build(_ctx(tmp_path))
        assert seg.text == ""
        assert seg.meta["injected_skill_ids"] == []


class TestCollectToolNames:
    """Shared by segments 4 and 5, with opposite consequences for a wrong
    answer: segment 5 loses a gate hint, segment 4 loses content. Both read
    ``None`` as 'do not gate', so the empty-vs-unknown distinction is load
    bearing and pinned here."""

    def test_reads_openai_and_flat_shapes(self) -> None:
        got = render.collect_tool_names(lambda: [{"function": {"name": "a"}}, {"name": "b"}])
        assert got == ["a", "b"]

    def test_unwired_and_raising_and_empty_are_all_none(self) -> None:
        def _boom():
            raise RuntimeError("x")

        assert render.collect_tool_names(None) is None
        assert render.collect_tool_names(_boom) is None
        # Empty list collapses to None too -- not because zero tools is
        # unreachable (naming every registered tool in tools.disabled_tools
        # reaches it) but because this helper's consumer has no always-skill to
        # withhold when the agent holds nothing. A caller that must tell the two
        # apart reads _tool_names; see live_dispatch_tools.
        assert render.collect_tool_names(lambda: []) is None

    def test_malformed_entries_are_skipped_not_fatal(self) -> None:
        got = render.collect_tool_names(lambda: ["junk", {"function": "notadict"}, {"name": "ok"}])
        assert got == ["ok"]


class TestActiveSkills:
    async def test_none_on_empty_workspace(self, tmp_path: Path) -> None:
        b = ActiveSkillsSegmentBuilder(ContextBuilder(workspace=tmp_path).skills)
        seg = await b.build(_ctx(tmp_path))
        # Built-in always-skills may exist; assert the builder either skips
        # or emits a well-formed # Active Skills block (never malformed).
        if seg is not None:
            assert seg.text.startswith("# Active Skills")

    async def test_dag_skill_is_resident_as_a_digest(self, tmp_path: Path) -> None:
        """With its tool registered, the shipped orchestration skill reaches the
        system prompt every turn — as description + routes only, body on disk."""
        b = ActiveSkillsSegmentBuilder(
            ContextBuilder(workspace=tmp_path).skills,
            get_tool_definitions=_tool_defs("read_file", "run_subagent_dag"),
        )
        seg = await b.build(_ctx(tmp_path))

        assert seg is not None
        assert "### Skill: subagent-dag-orchestration" in seg.text
        assert "run_subagent_dag" in seg.text  # from the description
        assert 'read_skill("local/subagent-dag-orchestration")' in seg.text
        # A distinctive line from deep in SKILL.md, i.e. the body proper.
        assert "## When to use" not in seg.text

    async def test_skill_withheld_when_its_required_tool_is_absent(self, tmp_path: Path) -> None:
        """``run_subagent_dag`` only registers when third-party sub-agents are
        configured, but the skill advertising it ships always-on. Without this
        gate the agent is told every turn to reach for a tool it cannot call."""
        b = ActiveSkillsSegmentBuilder(
            ContextBuilder(workspace=tmp_path).skills,
            get_tool_definitions=_tool_defs("read_file", "spawn", "exec"),
        )
        seg = await b.build(_ctx(tmp_path))

        assert seg is None or "subagent-dag-orchestration" not in seg.text

    async def test_unwired_tool_lookup_does_not_gate(self, tmp_path: Path) -> None:
        """No callable wired → unknown, not empty. A wiring gap must degrade to
        showing the skill, never to silently blanking the segment."""
        b = ActiveSkillsSegmentBuilder(ContextBuilder(workspace=tmp_path).skills)
        seg = await b.build(_ctx(tmp_path))

        assert seg is not None
        assert "subagent-dag-orchestration" in seg.text

    async def test_malformed_requires_does_not_break_or_hide_a_skill(self, tmp_path: Path) -> None:
        """A hand-authored ``requires`` of the wrong shape must not raise into
        prompt assembly, nor silently withhold the skill."""
        skill_dir = tmp_path / "skills" / "wonky"
        skill_dir.mkdir(parents=True)
        skill_dir.joinpath("SKILL.md").write_text(
            '---\nname: wonky\ndescription: d\nmetadata: {"raven":{"always":true,"requires":{"tools":7}}}\n---\n\nbody\n',
            encoding="utf-8",
        )
        b = ActiveSkillsSegmentBuilder(
            ContextBuilder(workspace=tmp_path).skills,
            get_tool_definitions=_tool_defs("read_file"),
        )
        seg = await b.build(_ctx(tmp_path))

        assert seg is not None
        assert "### Skill: wonky" in seg.text

    async def test_raising_tool_lookup_does_not_gate(self, tmp_path: Path) -> None:
        def _boom() -> list[dict]:
            raise RuntimeError("registry unavailable")

        b = ActiveSkillsSegmentBuilder(
            ContextBuilder(workspace=tmp_path).skills,
            get_tool_definitions=_boom,
        )
        seg = await b.build(_ctx(tmp_path))

        assert seg is not None
        assert "subagent-dag-orchestration" in seg.text


class TestIdentityDelegationSection:
    """A sub-agent that declares what it owns gets a line in the prompt telling
    the model not to do that work itself. Only declared agents appear, so an
    install with none reads exactly as it did before."""

    @staticmethod
    def _meta(name: str, owns: str = ""):
        from raven.agent.subagent.backends import AgentMeta

        return AgentMeta(name, f"{name} description", True, True, False, owns)

    def test_declared_agents_get_an_ownership_line(self, tmp_path: Path) -> None:
        text = render.identity_text(
            tmp_path,
            specialists=[("Scribe", "owns decks. Do not build the deck yourself.")],
        )
        assert "## Delegation" in text
        assert "`Scribe` owns decks. Do not build the deck yourself." in text
        assert "You are an orchestrator first" in text

    def test_nothing_declared_keeps_the_joint_the_section_took_over(self, tmp_path: Path) -> None:
        """An install with no declaring agent must read exactly as it did before.

        This test used to compare ``identity_text`` against itself -- ``specialists=[]``
        against the default -- which agrees whatever the output is. It passed while
        the section had moved the blank line before ``## Raven Guidelines`` inside
        itself and dropped it on the empty branch, so the prompt every install
        without a specialist renders was one blank line short. The joint is the
        thing to assert; the self-comparison is kept only as the weaker half.
        """
        plain = render.identity_text(tmp_path, specialists=[])
        assert plain == render.identity_text(tmp_path)
        assert "## Delegation" not in plain
        # Three newlines, not two: the platform policy block ends with one of its
        # own, so asserting two passes with the separator dropped as well.
        assert "\n\n\n## Raven Guidelines" in plain

    def test_the_section_sits_above_the_guidelines(self, tmp_path: Path) -> None:
        """A rule about which agent to name has to be read before the general
        guidelines it qualifies, not after them."""
        text = render.identity_text(tmp_path, specialists=[("Scribe", "owns decks.")])
        assert text.index("## Delegation") < text.index("## Raven Guidelines")

    async def test_builder_collects_only_agents_that_declare_ownership(self, tmp_path: Path) -> None:
        roster = [self._meta("Scribe", "owns decks."), self._meta("Nomad")]
        builder = IdentitySegmentBuilder(tmp_path, list_subagents=lambda: roster)
        text = (await builder.build(_ctx(tmp_path))).text
        assert "`Scribe` owns decks." in text
        assert "Nomad" not in text

    async def test_builder_survives_a_roster_lookup_that_raises(self, tmp_path: Path) -> None:
        """Prompt assembly must not fail because the agent table is mid-rebuild."""

        def _boom():
            raise RuntimeError("table is being rebuilt")

        builder = IdentitySegmentBuilder(tmp_path, list_subagents=_boom)
        assert "## Delegation" not in (await builder.build(_ctx(tmp_path))).text

    async def test_the_generic_row_is_never_a_specialist(self, tmp_path: Path) -> None:
        """It carries no capability bias, so it owns no kind of work -- and a row
        that declared some would render a prohibition aimed at the agent reading
        it."""
        builder = IdentitySegmentBuilder(
            tmp_path, list_subagents=lambda: [self._meta(GENERIC_AGENT, "owns everything.")]
        )
        assert "## Delegation" not in (await builder.build(_ctx(tmp_path))).text


class TestDelegationNeedsSomewhereToDelegateTo:
    """The section prohibits a specialist's kind of work, so it must not outlive
    the tools that carry the work away. With every dispatch path withheld a
    request has no compliant action left: the model either does the forbidden
    thing or abandons the task."""

    SPEC = [("Scribe", "owns decks. Do not build the deck yourself.")]

    @staticmethod
    def _defs(*names: str) -> list[dict]:
        return [{"function": {"name": n}} for n in names]

    def test_no_live_path_reads_like_an_install_without_specialists(self, tmp_path: Path) -> None:
        assert render.identity_text(tmp_path, specialists=self.SPEC, dispatch_tools=()) == render.identity_text(
            tmp_path
        )

    def test_only_the_paths_that_exist_are_named(self, tmp_path: Path) -> None:
        spawn_only = render.identity_text(tmp_path, specialists=self.SPEC, dispatch_tools=("spawn",))
        assert "`Scribe` owns decks." in spawn_only
        assert "`spawn`" in spawn_only
        assert "run_subagent_dag" not in spawn_only

    def test_the_guard_is_not_too_tight_either(self, tmp_path: Path) -> None:
        """``spawn`` withheld while the graph tool is live is still a delegable
        install -- suppressing the section there would lose a rule that applies."""
        dag_only = render.identity_text(tmp_path, specialists=self.SPEC, dispatch_tools=("run_subagent_dag",))
        assert "`Scribe` owns decks." in dag_only
        assert "`run_subagent_dag`" in dag_only
        assert "`spawn`" not in dag_only

    def test_an_unknowable_tool_surface_does_not_gate(self) -> None:
        """A wiring gap must degrade to saying too much: the alternative deletes
        the rule on every install that never wired the lookup up."""

        def _boom():
            raise RuntimeError("registry mid-rebuild")

        assert render.live_dispatch_tools(None) == render.DISPATCH_TOOLS
        assert render.live_dispatch_tools(_boom) == render.DISPATCH_TOOLS

    def test_a_readable_but_empty_tool_table_does_gate(self) -> None:
        """This case was asserted the other way round when the gate was added,
        which is how it shipped broken. An empty table is not an unreadable one:
        it is a turn that can delegate nothing, reachable by naming every
        registered tool in ``tools.disabledTools`` -- and a request needing no
        tools at all is exactly the one the prohibition would then strand.

        ``collect_tool_names`` still folds empty into ``None`` for the
        always-skills filter, so both halves of the divergence are pinned here:
        it is a deliberate difference between two questions, not a drift.
        """
        assert render.live_dispatch_tools(lambda: []) == ()
        assert render.collect_tool_names(lambda: []) is None

    def test_a_tool_list_without_any_dispatch_path_gates(self) -> None:
        assert render.live_dispatch_tools(lambda: self._defs("grep", "read_file")) == ()

    async def test_builder_drops_the_section_when_the_turn_offers_no_dispatch_tool(self, tmp_path: Path) -> None:
        from raven.agent.subagent.backends import AgentMeta

        builder = IdentitySegmentBuilder(
            tmp_path,
            list_subagents=lambda: [AgentMeta("Scribe", "d", True, True, False, "owns decks.")],
            get_tool_definitions=lambda: self._defs("grep"),
        )
        assert "## Delegation" not in (await builder.build(_ctx(tmp_path))).text

    async def test_builder_keeps_it_when_the_turn_offers_one(self, tmp_path: Path) -> None:
        from raven.agent.subagent.backends import AgentMeta

        builder = IdentitySegmentBuilder(
            tmp_path,
            list_subagents=lambda: [AgentMeta("Scribe", "d", True, True, False, "owns decks.")],
            get_tool_definitions=lambda: self._defs("spawn"),
        )
        text = (await builder.build(_ctx(tmp_path))).text
        assert "`Scribe` owns decks." in text
        assert "run_subagent_dag" not in text
