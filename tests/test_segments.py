"""The five SegmentBuilders, tested in isolation.

Each builder is fed a fake :class:`AssemblyContext` and asserted to
reproduce the segment its old inline block in ``ContextBuilder`` emitted.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from raven.agent.context import ContextBuilder
from raven.context_engine.base import AssemblyContext
from raven.context_engine.segments import (
    ActiveSkillsSegmentBuilder,
    BootstrapSegmentBuilder,
    IdentitySegmentBuilder,
    MemorySegmentBuilder,
    SkillsSegmentBuilder,
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
        self.calls.append({
            "query": query, "user_id": user_id, "agent_id": agent_id, "top_k": top_k,
        })
        return list(self._mems)


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
            backend=backend, user_id="alice", memory_top_k=7,
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


class TestActiveSkills:
    async def test_none_on_empty_workspace(self, tmp_path: Path) -> None:
        b = ActiveSkillsSegmentBuilder(ContextBuilder(workspace=tmp_path).skills)
        seg = await b.build(_ctx(tmp_path))
        # Built-in always-skills may exist; assert the builder either skips
        # or emits a well-formed # Active Skills block (never malformed).
        if seg is not None:
            assert seg.text.startswith("# Active Skills")
