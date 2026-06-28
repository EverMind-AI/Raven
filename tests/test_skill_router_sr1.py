"""SR-1 — SkillSource Protocol shape, LocalSkillSource emission,
LocalSkillCatalog rendering.

The Local source/catalog own the :class:`LocalPool` /
:class:`SkillRegistry` plumbing; these tests check the **adapter seam**
and the catalog's rendering surface, not the upstream BM25 / scoring
logic (those have their own tests).
"""

from __future__ import annotations

import textwrap
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from raven.memory_engine.skill_forge import (
    LocalSkillCatalog,
    LocalSkillSource,
    RouterHit,
    SkillSource,
)


# ---------------------------------------------------------------------------
# RouterHit dataclass
# ---------------------------------------------------------------------------


class TestRouterHitDataclass:
    def test_minimal_fields(self) -> None:
        h = RouterHit(
            qualified_id="local/git-resolver",
            name="git-resolver",
            content="body",
            score=0.5,
        )
        assert h.qualified_id == "local/git-resolver"
        assert h.meta == {}

    def test_frozen(self) -> None:
        from dataclasses import FrozenInstanceError

        h = RouterHit(
            qualified_id="local/x", name="x", content="", score=0.0,
        )
        with pytest.raises(FrozenInstanceError):
            h.score = 1.0  # type: ignore[misc]

    def test_meta_default_independent_per_instance(self) -> None:
        a = RouterHit(qualified_id="x/1", name="x", content="", score=0.0)
        b = RouterHit(qualified_id="x/2", name="y", content="", score=0.0)
        a.meta["k"] = "v"
        assert "k" not in b.meta


# ---------------------------------------------------------------------------
# SkillSource Protocol runtime check
# ---------------------------------------------------------------------------


class _GoodSource:
    name = "good"
    weight = 1.0

    async def search(self, query, history, k):
        return []


class _MissingAttr:
    """Missing ``weight`` — fails the Protocol."""

    name = "x"

    async def search(self, query, history, k):
        return []


class TestSkillSourceProtocol:
    def test_complete_source_satisfies_protocol(self) -> None:
        assert isinstance(_GoodSource(), SkillSource)

    def test_missing_attribute_fails_protocol(self) -> None:
        assert not isinstance(_MissingAttr(), SkillSource)


# ---------------------------------------------------------------------------
# LocalSkillSource — end-to-end against real LocalPool + SkillRegistry
# ---------------------------------------------------------------------------


def _write_skill(root: Path, name: str, *, body: str, desc: str = "") -> None:
    d = root / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {desc or name}\n---\n\n{body}",
        encoding="utf-8",
    )


class TestLocalSkillSource:
    @pytest.fixture
    def pool_and_registry(self, tmp_path: Path):
        """A real LocalPool + SkillRegistry with two on-disk SKILL.md."""
        from raven.memory_engine.skill_local.local_pool import LocalPool
        from raven.memory_engine.skill_local.registry import SkillRegistry

        ws = tmp_path / "ws"
        (ws / "skills").mkdir(parents=True)
        builtin = tmp_path / "builtin"
        builtin.mkdir()
        _write_skill(
            ws / "skills", "pdf-tool", body="generate pdf", desc="pdf gen",
        )
        _write_skill(
            ws / "skills", "weather-tool",
            body="weather forecast", desc="weather",
        )
        reg = SkillRegistry(
            workspace=ws,
            builtin_skills_dir=builtin,
        )
        pool = LocalPool(reg)
        return pool, reg

    async def test_emits_qualified_id_with_local_prefix(
        self, pool_and_registry,
    ) -> None:
        pool, reg = pool_and_registry
        src = LocalSkillSource(pool, reg)
        hits = await src.search("pdf", history=[], k=5)
        assert len(hits) >= 1
        assert all(h.qualified_id.startswith("local/") for h in hits)

    async def test_emits_content_from_registry(
        self, pool_and_registry,
    ) -> None:
        pool, reg = pool_and_registry
        src = LocalSkillSource(pool, reg)
        hits = await src.search("pdf", history=[], k=5)
        # The matching skill carries the body text we wrote.
        pdf_hit = next(h for h in hits if h.name == "pdf-tool")
        assert "generate pdf" in pdf_hit.content

    async def test_meta_records_source_label(
        self, pool_and_registry,
    ) -> None:
        pool, reg = pool_and_registry
        src = LocalSkillSource(pool, reg)
        hits = await src.search("pdf", history=[], k=5)
        for h in hits:
            assert h.meta["source"] == "local"
            assert "physical_source" in h.meta
            assert h.meta["always"] is False

    async def test_missing_from_registry_skipped(self, tmp_path: Path) -> None:
        """Race: BM25 returns a hit whose meta has vanished. Source
        skips rather than emit half-populated."""
        from raven.memory_engine.skill_local.types import ScoredSkill

        fake_pool = MagicMock()
        fake_pool.search.return_value = [
            ScoredSkill(name="ghost", score=1.0, source="workspace"),
        ]
        fake_registry = MagicMock()
        fake_registry.get.return_value = None  # vanished
        src = LocalSkillSource(fake_pool, fake_registry)
        hits = await src.search("anything", history=[], k=5)
        assert hits == []

    async def test_k_passes_through(self, pool_and_registry) -> None:
        pool, reg = pool_and_registry
        # Wrap pool.search to spy on the top_k it receives.
        from raven.memory_engine.skill_local.local_pool import LocalPool

        spy = []
        orig = pool.search

        def wrapped(query, top_k=50):
            spy.append(top_k)
            return orig(query, top_k)

        pool.search = wrapped  # type: ignore[method-assign]
        src = LocalSkillSource(pool, reg)
        await src.search("anything", history=[], k=3)
        assert spy == [3]


# ---------------------------------------------------------------------------
# LocalSkillCatalog — standalone owner of the local pool + rendering
# ---------------------------------------------------------------------------


class TestLocalSkillCatalog:
    @pytest.fixture
    def catalog(self, tmp_path: Path):
        """A standalone catalog over a real workspace + builtin dir."""
        ws = tmp_path / "ws"
        (ws / "skills").mkdir(parents=True)
        builtin = tmp_path / "builtin"
        builtin.mkdir()
        _write_skill(ws / "skills", "pdf-tool", body="generate pdf", desc="pdf gen")
        # An always-on skill in the builtin layer.
        d = builtin / "memory"
        d.mkdir(parents=True)
        (d / "SKILL.md").write_text(
            "---\nname: memory\ndescription: mem\nalways: true\n---\n\nremember things",
            encoding="utf-8",
        )
        return LocalSkillCatalog(
            ws, builtin_skills_dir=builtin, start_watcher=False,
        )

    def test_pool_and_registry_exposed(self, catalog) -> None:
        # LocalSkillSource reuses these — they must be live, not None.
        assert catalog.pool is catalog._local_pool
        assert catalog.registry is catalog._registry

    def test_get_always_skills(self, catalog) -> None:
        always = {m.name for m in catalog.get_always_skills()}
        assert "memory" in always
        assert "pdf-tool" not in always

    def test_load_skills_for_context_renders_body(self, catalog) -> None:
        metas = [m for m in catalog.registry.list_all() if m.name == "pdf-tool"]
        out = catalog.load_skills_for_context(metas)
        assert "### Skill: pdf-tool" in out
        assert "generate pdf" in out

    def test_build_summary_full_and_subset(self, catalog) -> None:
        full = catalog.build_skills_summary()
        assert "<name>pdf-tool</name>" in full
        only = [m for m in catalog.registry.list_all() if m.name == "pdf-tool"]
        subset = catalog.build_skills_summary(only=only)
        assert "<name>pdf-tool</name>" in subset
        assert "<name>memory</name>" not in subset
