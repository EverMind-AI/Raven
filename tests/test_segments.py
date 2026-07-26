"""The five SegmentBuilders, tested in isolation.

Each builder is fed a fake :class:`AssemblyContext` and asserted to
reproduce the segment its old inline block in ``ContextBuilder`` emitted.
"""

from __future__ import annotations

import types
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

    async def test_identity_with_backend_omits_stale_episodic_path(self, tmp_path: Path) -> None:
        # A wired MemoryBackend (e.g. EverOS) keeps episodes in its own
        # store; the workspace episodes.md file stays empty, so the
        # identity block must not point the agent at it.
        backend = _Backend([])
        seg = await IdentitySegmentBuilder(tmp_path, backend).build(_ctx(tmp_path))
        assert "user_memory/episodic/episodes.md" not in seg.text
        assert "# Memory" in seg.text

    async def test_identity_with_backend_matches_legacy_builder(self, tmp_path: Path) -> None:
        backend = _Backend([])
        seg = await IdentitySegmentBuilder(tmp_path, backend).build(_ctx(tmp_path))
        legacy = ContextBuilder(workspace=tmp_path, backend=backend)._get_identity()
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
