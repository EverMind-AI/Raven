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
from raven.memory_engine.skill_forge.visual_domain_selector import (
    SkillCard,
    VisualDomainSelection,
)


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


class _VisualSelector:
    def __init__(self, selection: VisualDomainSelection) -> None:
        self.selection = selection
        self.cards = (*selection.preferred, *selection.alternatives)
        self.queries: list[str] = []

    async def select(self, query: str) -> VisualDomainSelection:
        self.queries.append(query)
        return self.selection

    def set_provider(self, provider, model) -> None:
        pass


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
        legacy = ContextBuilder(workspace=tmp_path, start_watcher=False)._get_identity()
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
        legacy = ContextBuilder(workspace=tmp_path, start_watcher=False)._get_identity()
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
            ContextBuilder(workspace=tmp_path, start_watcher=False).memory,
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
        b = MemorySegmentBuilder(
            ContextBuilder(workspace=tmp_path, start_watcher=False).memory,
            backend=None,
        )
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

    async def test_visual_selector_renders_cards_without_bodies(self, tmp_path: Path) -> None:
        preferred = SkillCard("builtin/primary", "primary", "primary description")
        alternative = SkillCard("builtin/secondary", "secondary", "secondary description")
        selector = _VisualSelector(VisualDomainSelection((preferred,), (alternative,)))
        b = SkillsSegmentBuilder(None, visual_domain_selector=selector)

        seg = await b.build(_ctx(tmp_path, "make a visual"))

        assert selector.queries == ["make a visual"]
        assert "## Preferred Skills" in seg.text
        assert "`builtin/primary`: primary description" in seg.text
        assert "## Alternative Skills" in seg.text
        assert "`builtin/secondary`: secondary description" in seg.text
        assert "SKILL.md body" not in seg.text
        assert "`builtin/visual-artifact-design` and every Skill under Preferred Skills" in seg.text
        assert seg.meta["injected_skill_ids"] == []
        assert seg.meta["preferred_visual_skill_ids"] == ["builtin/primary"]
        assert seg.meta["alternative_visual_skill_ids"] == ["builtin/secondary"]

    async def test_visual_selector_keeps_non_domain_skillforge_results(self, tmp_path: Path) -> None:
        domain = SkillCard("builtin/domain", "domain", "domain description")
        selector = _VisualSelector(VisualDomainSelection((domain,), ()))
        hits = [
            RouterHit(
                qualified_id="local/domain",
                name="domain",
                content="domain body must stay hidden",
                score=1.0,
            ),
            RouterHit(
                qualified_id="local/weather",
                name="weather",
                content="public weather body",
                score=0.9,
            ),
        ]
        b = SkillsSegmentBuilder(
            SkillForgeRouter([_Source(hits)]),
            skill_top_k=5,
            visual_domain_selector=selector,
        )

        seg = await b.build(_ctx(tmp_path, "make a weather graphic"))

        assert "`builtin/domain`: domain description" in seg.text
        assert "## Other Routed Skills" in seg.text
        assert "public weather body" in seg.text
        assert "domain body must stay hidden" not in seg.text
        assert seg.meta["injected_skill_ids"] == ["local/weather"]

    async def test_visual_selector_valid_empty_result_renders_nothing(self, tmp_path: Path) -> None:
        selector = _VisualSelector(VisualDomainSelection((), ()))
        b = SkillsSegmentBuilder(None, visual_domain_selector=selector)

        seg = await b.build(_ctx(tmp_path, "non-visual question"))

        assert seg.text == ""
        assert seg.meta["described_skill_ids"] == []


class TestActiveSkills:
    def test_estimation_prompt_defers_visual_base_body(self, tmp_path: Path) -> None:
        prompt = ContextBuilder(workspace=tmp_path, start_watcher=False).build_system_prompt()

        assert "call `read_skill` with `builtin/visual-artifact-design`" in prompt
        assert "## 1. 先建立最小合同" not in prompt

    async def test_visual_base_body_is_deferred_but_other_always_skills_stay_inline(self, tmp_path: Path) -> None:
        base = types.SimpleNamespace(
            id="builtin/visual-artifact-design",
            name="visual-artifact-design",
            source="builtin",
            content="VISUAL BASE BODY MUST NOT BE INLINED",
        )
        other = types.SimpleNamespace(
            id="workspace/team-policy",
            name="team-policy",
            source="workspace",
            content="team policy body",
        )

        class _Catalog:
            _config = types.SimpleNamespace(always_max=5)

            def get_always_skills(self):
                return [base, other]

            def load_skills_for_context(self, skills, max_inject=None):
                return "\n".join(skill.content for skill in skills[:max_inject])

        seg = await ActiveSkillsSegmentBuilder(_Catalog()).build(_ctx(tmp_path))

        assert seg is not None
        assert "call `read_skill` with `builtin/visual-artifact-design`" in seg.text
        assert "Keep these cross-domain invariants active" in seg.text
        assert "VISUAL BASE BODY MUST NOT BE INLINED" not in seg.text
        assert "team policy body" in seg.text

    async def test_none_on_empty_workspace(self, tmp_path: Path) -> None:
        b = ActiveSkillsSegmentBuilder(ContextBuilder(workspace=tmp_path, start_watcher=False).skills)
        seg = await b.build(_ctx(tmp_path))
        # Built-in always-skills may exist; assert the builder either skips
        # or emits a well-formed # Active Skills block (never malformed).
        if seg is not None:
            assert seg.text.startswith("# Active Skills")
