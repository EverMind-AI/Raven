"""The five SegmentBuilders, tested in isolation.

Each builder is fed a fake :class:`AssemblyContext` and asserted to
reproduce the segment its old inline block in ``ContextBuilder`` emitted.
"""

from __future__ import annotations

from pathlib import Path

from raven.agent.context import ContextBuilder
from raven.context_engine.base import AssemblyContext
from raven.context_engine.segments import (
    ActiveSkillsSegmentBuilder,
    BootstrapSegmentBuilder,
    IdentitySegmentBuilder,
    MemorySegmentBuilder,
    SkillsSegmentBuilder,
    render,
)
from raven.memory_engine import Memory, TokenBudget
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
        # Empty list collapses to None too: an agent with zero tools is not a
        # real state, so treating it as "unknown" is the safe reading.
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
