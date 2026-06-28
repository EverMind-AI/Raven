"""Phase A acceptance tests for SkillForge.

Phase A is the structural migration: ``agent/skills.py`` is dismantled
into ``skill_forge/{store,service,types}.py`` while preserving the exact
external behavior of the legacy ``SkillsLoader``. These tests pin that
behavior down so future refactors can't drift.

No LLM calls — everything runs offline against a synthetic workspace
plus the real built-in skills directory.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from raven.memory_engine.skill_local import SkillRegistry
from raven.memory_engine.skill_local.registry import _strip_frontmatter
from raven.memory_engine.skill_forge import LocalSkillCatalog
from raven.memory_engine.skill_forge.catalog import _escape_xml
from raven.memory_engine.skill_local.registry import (
    _check_requirements,
    _missing_requirements,
    _parse_frontmatter,
    _parse_nested_metadata,
    _resolve_always,
)


# ----------------------------------------------------------------------
# Fixtures
# ----------------------------------------------------------------------


def _write_skill(
    root: Path,
    name: str,
    *,
    description: str = "",
    frontmatter_extra: str = "",
    body: str = "Body text.",
):
    """Create ``<root>/<name>/SKILL.md`` with a simple frontmatter block."""
    skill_dir = root / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    front = f"name: {name}\ndescription: {description}\n"
    if frontmatter_extra:
        front += frontmatter_extra + "\n"
    (skill_dir / "SKILL.md").write_text(
        f"---\n{front}---\n\n{body}\n", encoding="utf-8"
    )


@pytest.fixture
def tmp_workspace(tmp_path: Path):
    """A workspace with a single workspace-layer skill."""
    workspace = tmp_path / "workspace"
    (workspace / "skills").mkdir(parents=True)
    _write_skill(
        workspace / "skills",
        "user_custom",
        description="A workspace-only skill",
    )
    return workspace


@pytest.fixture
def tmp_builtin(tmp_path: Path):
    """An isolated builtin directory with several synthetic skills."""
    builtin = tmp_path / "builtin"
    builtin.mkdir()

    _write_skill(builtin, "simple", description="A simple builtin skill")

    # ``always: true`` in top-level frontmatter
    _write_skill(
        builtin,
        "always_flag_top",
        description="Always-loaded skill (top-level)",
        frontmatter_extra="always: true",
    )

    # ``always: true`` inside nested metadata JSON
    _write_skill(
        builtin,
        "always_flag_nested",
        description="Always-loaded skill (nested)",
        frontmatter_extra='metadata: {"raven":{"always":true}}',
    )

    # Requires a fake binary that almost certainly doesn't exist
    _write_skill(
        builtin,
        "needs_fake_bin",
        description="Needs a missing binary",
        frontmatter_extra='metadata: {"raven":{"requires":{"bins":["this_bin_does_not_exist_xyz"]}}}',
    )

    # Same name as workspace skill to test precedence
    _write_skill(
        builtin,
        "user_custom",
        description="Builtin fallback — should be shadowed by workspace",
    )

    return builtin


# ----------------------------------------------------------------------
# Module-level parsers
# ----------------------------------------------------------------------


class TestFrontmatterParsing:
    def test_empty_returns_none(self):
        assert _parse_frontmatter("no frontmatter here") is None

    def test_simple_fields(self):
        content = textwrap.dedent("""\
            ---
            name: foo
            description: "quoted value"
            ---
            body
        """)
        fm = _parse_frontmatter(content)
        assert fm == {"name": "foo", "description": "quoted value"}

    def test_nested_metadata_raven_wins(self):
        raw = '{"raven": {"emoji": "🔧"}, "nanobot": {"emoji": "🤖"}}'
        assert _parse_nested_metadata(raw) == {"emoji": "🔧"}

    def test_nested_metadata_fallback_order(self):
        raw = '{"nanobot": {"emoji": "🤖"}, "openclaw": {"emoji": "🦞"}}'
        assert _parse_nested_metadata(raw) == {"emoji": "🤖"}

    def test_nested_metadata_bad_json_returns_empty(self):
        assert _parse_nested_metadata("not json") == {}
        assert _parse_nested_metadata("") == {}

    def test_resolve_always_checks_both_layers(self):
        assert _resolve_always({}, {"always": True}) is True
        assert _resolve_always({"always": "true"}, {}) is True
        assert _resolve_always({}, {}) is False


class TestRequirementsHelpers:
    def test_no_requires_is_available(self):
        assert _check_requirements({}) is True
        assert _missing_requirements({}) == ""

    def test_missing_bin_unavailable(self):
        requires = {"bins": ["this_bin_does_not_exist_xyz"]}
        assert _check_requirements(requires) is False
        assert "CLI: this_bin_does_not_exist_xyz" in _missing_requirements(requires)

    def test_missing_env_unavailable(self, monkeypatch):
        monkeypatch.delenv("DEFINITELY_UNSET_VAR_XYZ", raising=False)
        requires = {"env": ["DEFINITELY_UNSET_VAR_XYZ"]}
        assert _check_requirements(requires) is False
        assert "ENV: DEFINITELY_UNSET_VAR_XYZ" in _missing_requirements(requires)


# ----------------------------------------------------------------------
# SkillRegistry — data layer
# ----------------------------------------------------------------------


class TestSkillRegistryListAndPrecedence:
    def test_lists_workspace_and_builtin(self, tmp_workspace, tmp_builtin):
        store = SkillRegistry(tmp_workspace, builtin_skills_dir=tmp_builtin)
        names = {m.name for m in store.list_all()}
        # Both sources represented
        assert "user_custom" in names
        assert "simple" in names
        assert "always_flag_top" in names

    def test_workspace_shadows_builtin_on_same_name(self, tmp_workspace, tmp_builtin):
        store = SkillRegistry(tmp_workspace, builtin_skills_dir=tmp_builtin)
        user_custom = next(m for m in store.list_all() if m.name == "user_custom")
        assert user_custom.source == "workspace"
        assert "workspace-only" in user_custom.description

    def test_builtin_only_skills_reported_as_builtin(self, tmp_workspace, tmp_builtin):
        store = SkillRegistry(tmp_workspace, builtin_skills_dir=tmp_builtin)
        simple = next(m for m in store.list_all() if m.name == "simple")
        assert simple.source == "builtin"


class TestSkillRegistryCacheInvalidation:
    def test_invalidate_cache_full_reset(self, tmp_workspace, tmp_builtin):
        store = SkillRegistry(tmp_workspace, builtin_skills_dir=tmp_builtin)
        store.list_all()
        assert store._metas_cache is not None
        store.invalidate_cache()
        assert store._metas_cache is None
        assert store._by_full_key is None
        assert store._by_name is None

    def test_invalidate_source_picks_up_newly_added_skill(
        self, tmp_workspace, tmp_builtin,
    ):
        """A new SKILL.md materialized after the cache is primed should
        surface on the next list_all when its source is invalidated —
        without re-scanning unchanged sources."""
        store = SkillRegistry(tmp_workspace, builtin_skills_dir=tmp_builtin)
        before = {(m.source, m.name) for m in store.list_all()}
        # Sanity: nested-mode source ``everos`` not present yet.
        assert not any(s == "everos" for s, _ in before)

        # Materialize a new skill under nested layout
        # ``<workspace>/skills/everos/<X>/SKILL.md``.
        nested_dir = tmp_workspace / "skills" / "everos" / "42"
        nested_dir.mkdir(parents=True)
        (nested_dir / "SKILL.md").write_text(
            "---\n"
            "name: dynamic-skill\n"
            "description: added at runtime\n"
            "---\n\nbody\n",
            encoding="utf-8",
        )

        # Without invalidation the cache hides the new file.
        names = {m.name for m in store.list_all()}
        assert "dynamic-skill" not in names

        store.invalidate_source("everos")
        after = store.list_all()
        names_after = {m.name for m in after}
        assert "dynamic-skill" in names_after
        # All previously-cached entries are still present (no full rescan
        # collateral damage).
        assert before.issubset({(m.source, m.name) for m in after})

    def test_invalidate_source_drops_removed_skill(
        self, tmp_workspace, tmp_builtin,
    ):
        """If a SKILL.md disappears, the source's incremental rescan
        should drop the stale entry."""
        # Seed an everos skill, prime cache, then delete + invalidate.
        nested_dir = tmp_workspace / "skills" / "everos" / "1"
        nested_dir.mkdir(parents=True)
        (nested_dir / "SKILL.md").write_text(
            "---\nname: gone-soon\ndescription: temp\n---\n\nbody\n",
            encoding="utf-8",
        )
        store = SkillRegistry(tmp_workspace, builtin_skills_dir=tmp_builtin)
        assert any(m.name == "gone-soon" for m in store.list_all())

        # Wipe disk — emulate retire path.
        import shutil
        shutil.rmtree(nested_dir)
        store.invalidate_source("everos")
        assert not any(m.name == "gone-soon" for m in store.list_all())
        # Other sources untouched.
        assert any(m.name == "simple" for m in store.list_all())

    def test_invalidate_source_when_cache_unprimed_is_noop(
        self, tmp_workspace, tmp_builtin,
    ):
        """``invalidate_source`` before any list_all must not crash;
        the very first list_all is still a full scan."""
        store = SkillRegistry(tmp_workspace, builtin_skills_dir=tmp_builtin)
        store.invalidate_source("everos")  # no-op
        assert store._metas_cache is None
        names = {m.name for m in store.list_all()}
        assert "simple" in names


class TestResolveSourceForPath:
    """``resolve_source_for_path`` powers the file-watcher's per-source
    invalidation — it must mirror ``_iter_skill_dirs``'s flat/nested
    rules across all three layer roots."""

    def test_workspace_flat_resolves_to_workspace(self, tmp_workspace, tmp_builtin):
        store = SkillRegistry(tmp_workspace, builtin_skills_dir=tmp_builtin)
        path = tmp_workspace / "skills" / "user_custom" / "SKILL.md"
        assert store.resolve_source_for_path(path) == "workspace"

    def test_workspace_nested_resolves_to_nested_source(
        self, tmp_workspace, tmp_builtin,
    ):
        store = SkillRegistry(tmp_workspace, builtin_skills_dir=tmp_builtin)
        # Path need not exist on disk — resolution is lexical so
        # delete events can route too.
        path = tmp_workspace / "skills" / "everos" / "42" / "SKILL.md"
        assert store.resolve_source_for_path(path) == "everos"

    def test_workspace_deeply_nested_uses_first_segment(
        self, tmp_workspace, tmp_builtin,
    ):
        store = SkillRegistry(tmp_workspace, builtin_skills_dir=tmp_builtin)
        path = (
            tmp_workspace / "skills" / "awesome" / "foo" / "bar" / "SKILL.md"
        )
        assert store.resolve_source_for_path(path) == "awesome"

    def test_builtin_path_resolves_to_builtin(self, tmp_workspace, tmp_builtin):
        store = SkillRegistry(tmp_workspace, builtin_skills_dir=tmp_builtin)
        path = tmp_builtin / "simple" / "SKILL.md"
        assert store.resolve_source_for_path(path) == "builtin"

    def test_external_path_resolves_to_external(self, tmp_path, tmp_workspace, tmp_builtin):
        ext = tmp_path / "external"
        ext.mkdir()
        store = SkillRegistry(
            tmp_workspace,
            builtin_skills_dir=tmp_builtin,
            external_skills_dir=ext,
        )
        path = ext / "anthropics" / "pdf" / "SKILL.md"
        assert store.resolve_source_for_path(path) == "anthropics"

    def test_unknown_path_returns_none(self, tmp_path, tmp_workspace, tmp_builtin):
        store = SkillRegistry(tmp_workspace, builtin_skills_dir=tmp_builtin)
        # Sibling of workspace, outside any layer root.
        assert store.resolve_source_for_path(
            tmp_path / "elsewhere" / "SKILL.md"
        ) is None

    def test_skill_md_at_layer_root_returns_none(
        self, tmp_workspace, tmp_builtin,
    ):
        """``_iter_skill_dirs`` skips SKILL.md directly under a layer
        root (``len(parts) == 0``) — the resolver must agree, else
        the watcher would invalidate a phantom source."""
        store = SkillRegistry(tmp_workspace, builtin_skills_dir=tmp_builtin)
        bogus = tmp_workspace / "skills" / "SKILL.md"
        assert store.resolve_source_for_path(bogus) is None


class TestSkillRegistrySingleReads:
    def test_get_returns_meta(self, tmp_workspace, tmp_builtin):
        store = SkillRegistry(tmp_workspace, builtin_skills_dir=tmp_builtin)
        m = store.get("simple")
        assert m is not None
        assert m.name == "simple"
        assert m.description == "A simple builtin skill"

    def test_get_returns_none_for_missing(self, tmp_workspace, tmp_builtin):
        store = SkillRegistry(tmp_workspace, builtin_skills_dir=tmp_builtin)
        assert store.get("nonexistent") is None

    def test_get_body_includes_frontmatter(self, tmp_workspace, tmp_builtin):
        store = SkillRegistry(tmp_workspace, builtin_skills_dir=tmp_builtin)
        body = store.get_body("simple")
        assert body is not None
        assert body.startswith("---")
        assert "Body text." in body

    def test_get_raw_metadata(self, tmp_workspace, tmp_builtin):
        store = SkillRegistry(tmp_workspace, builtin_skills_dir=tmp_builtin)
        meta = store.get_raw_metadata("simple")
        assert meta is not None
        assert meta["name"] == "simple"
        assert meta["description"] == "A simple builtin skill"

    def test_check_available_true_when_no_deps(self, tmp_workspace, tmp_builtin):
        store = SkillRegistry(tmp_workspace, builtin_skills_dir=tmp_builtin)
        assert store.check_available("simple") is True

    def test_check_available_false_when_missing_bin(self, tmp_workspace, tmp_builtin):
        store = SkillRegistry(tmp_workspace, builtin_skills_dir=tmp_builtin)
        assert store.check_available("needs_fake_bin") is False

    def test_missing_requirements_string(self, tmp_workspace, tmp_builtin):
        store = SkillRegistry(tmp_workspace, builtin_skills_dir=tmp_builtin)
        s = store.get_missing_requirements("needs_fake_bin")
        assert "this_bin_does_not_exist_xyz" in s


class TestSkillRegistryAlwaysResolution:
    def test_always_top_level(self, tmp_workspace, tmp_builtin):
        store = SkillRegistry(tmp_workspace, builtin_skills_dir=tmp_builtin)
        m = store.get("always_flag_top")
        assert m is not None and m.always is True

    def test_always_nested(self, tmp_workspace, tmp_builtin):
        store = SkillRegistry(tmp_workspace, builtin_skills_dir=tmp_builtin)
        m = store.get("always_flag_nested")
        assert m is not None and m.always is True

    def test_non_always_is_false(self, tmp_workspace, tmp_builtin):
        store = SkillRegistry(tmp_workspace, builtin_skills_dir=tmp_builtin)
        m = store.get("simple")
        assert m is not None and m.always is False


# ----------------------------------------------------------------------
# SkillService — facade (legacy-compatible API)
# ----------------------------------------------------------------------


class TestSkillServiceLegacyAPI:
    def test_list_skills_returns_legacy_shape(self, tmp_workspace, tmp_builtin):
        svc = LocalSkillCatalog(tmp_workspace, builtin_skills_dir=tmp_builtin)
        items = svc.list_skills()
        # Shape: list[dict] with exactly three keys
        assert all(set(i.keys()) == {"name", "path", "source"} for i in items)
        # Unavailable ones filtered by default
        assert not any(i["name"] == "needs_fake_bin" for i in items)

    def test_list_skills_unfiltered_includes_unavailable(
        self, tmp_workspace, tmp_builtin
    ):
        svc = LocalSkillCatalog(tmp_workspace, builtin_skills_dir=tmp_builtin)
        items = svc.list_skills(filter_unavailable=False)
        assert any(i["name"] == "needs_fake_bin" for i in items)

    def test_load_skill_returns_body(self, tmp_workspace, tmp_builtin):
        svc = LocalSkillCatalog(tmp_workspace, builtin_skills_dir=tmp_builtin)
        body = svc.load_skill("simple")
        assert body and "Body text." in body

    def test_load_skill_missing_returns_none(self, tmp_workspace, tmp_builtin):
        svc = LocalSkillCatalog(tmp_workspace, builtin_skills_dir=tmp_builtin)
        assert svc.load_skill("does_not_exist") is None

    def test_get_always_skills_excludes_unavailable(
        self, tmp_workspace, tmp_builtin
    ):
        svc = LocalSkillCatalog(tmp_workspace, builtin_skills_dir=tmp_builtin)
        always = {m.name for m in svc.get_always_skills()}
        assert "always_flag_top" in always
        assert "always_flag_nested" in always
        # The non-always skills should not show up
        assert "simple" not in always
        assert "user_custom" not in always

    def test_load_skills_for_context_strips_frontmatter(
        self, tmp_workspace, tmp_builtin
    ):
        svc = LocalSkillCatalog(tmp_workspace, builtin_skills_dir=tmp_builtin)
        metas = [
            m for m in svc._registry.list_all()
            if m.name in {"simple", "always_flag_top"}
        ]
        out = svc.load_skills_for_context(metas)
        assert "### Skill: simple" in out
        assert "### Skill: always_flag_top" in out
        assert "Body text." in out
        # Frontmatter markers must be gone
        assert "---" not in out.replace("\n\n---\n\n", "")
        # Separator between skills
        assert "\n\n---\n\n" in out

    def test_get_skill_metadata(self, tmp_workspace, tmp_builtin):
        svc = LocalSkillCatalog(tmp_workspace, builtin_skills_dir=tmp_builtin)
        meta = svc.get_skill_metadata("simple")
        assert meta is not None and meta["name"] == "simple"


class TestBuildSkillsSummary:
    def test_full_directory_by_default(self, tmp_workspace, tmp_builtin):
        svc = LocalSkillCatalog(tmp_workspace, builtin_skills_dir=tmp_builtin)
        xml = svc.build_skills_summary()
        assert xml.startswith("<skills>") and xml.endswith("</skills>")
        for name in ("simple", "always_flag_top", "user_custom", "needs_fake_bin"):
            assert f"<name>{name}</name>" in xml

    def test_unavailable_skill_marked_false_with_requires(
        self, tmp_workspace, tmp_builtin
    ):
        svc = LocalSkillCatalog(tmp_workspace, builtin_skills_dir=tmp_builtin)
        xml = svc.build_skills_summary()
        assert 'available="false"' in xml
        assert "this_bin_does_not_exist_xyz" in xml

    def test_only_filter_narrows_output(self, tmp_workspace, tmp_builtin):
        svc = LocalSkillCatalog(tmp_workspace, builtin_skills_dir=tmp_builtin)
        simple_metas = [m for m in svc._registry.list_all() if m.name == "simple"]
        xml = svc.build_skills_summary(only=simple_metas)
        assert "<name>simple</name>" in xml
        assert "<name>always_flag_top</name>" not in xml
        assert "<name>user_custom</name>" not in xml

    def test_only_empty_list_yields_empty_string(self, tmp_workspace, tmp_builtin):
        svc = LocalSkillCatalog(tmp_workspace, builtin_skills_dir=tmp_builtin)
        assert svc.build_skills_summary(only=[]) == ""


# ----------------------------------------------------------------------
# Rendering helpers
# ----------------------------------------------------------------------


class TestRenderingHelpers:
    def test_strip_frontmatter_removes_header(self):
        src = "---\nname: x\n---\n\nBody"
        # registry's _strip_frontmatter keeps the body verbatim, only strips leading frontmatter
        assert _strip_frontmatter(src) == "\nBody"

    def test_strip_frontmatter_passes_through_when_absent(self):
        assert _strip_frontmatter("just body") == "just body"

    def test_escape_xml(self):
        assert _escape_xml("a & b < c > d") == "a &amp; b &lt; c &gt; d"


# ----------------------------------------------------------------------
# ContextBuilder integration — skill_names must actually take effect
# ----------------------------------------------------------------------


class TestContextBuilderIntegration:
    def test_skill_names_narrows_xml_directory(
        self, tmp_workspace, tmp_builtin, monkeypatch
    ):
        # ContextBuilder uses the real built-in directory by default; point it
        # at our synthetic one so the test is deterministic.
        from raven.agent.context import ContextBuilder

        cb = ContextBuilder(tmp_workspace)
        # Swap the skills service for one that uses our synthetic builtin.
        from raven.memory_engine.skill_forge import LocalSkillCatalog

        cb.skills = LocalSkillCatalog(tmp_workspace, builtin_skills_dir=tmp_builtin)

        # With selected_skills=None → full directory
        prompt_full = cb.build_system_prompt(selected_skills=None)
        assert "<name>simple</name>" in prompt_full
        assert "<name>always_flag_top</name>" in prompt_full

        # With selected_skills=[simple_meta] → only simple shows up in the XML
        # (always-loaded skills are still injected in the # Active Skills
        # section — that's separate from the XML directory)
        simple_meta = [m for m in cb.skills._registry.list_all() if m.name == "simple"]
        prompt_filtered = cb.build_system_prompt(selected_skills=simple_meta)
        # XML directory: only simple
        xml_start = prompt_filtered.index("<skills>")
        xml_end = prompt_filtered.index("</skills>") + len("</skills>")
        xml_block = prompt_filtered[xml_start:xml_end]
        assert "<name>simple</name>" in xml_block
        assert "<name>always_flag_top</name>" not in xml_block

    def test_empty_skill_names_falls_back_to_full_directory(
        self, tmp_workspace, tmp_builtin
    ):
        """Phase A stub selector returns [] — must fall back, not strip all."""
        from raven.agent.context import ContextBuilder
        from raven.memory_engine.skill_forge import LocalSkillCatalog

        cb = ContextBuilder(tmp_workspace)
        cb.skills = LocalSkillCatalog(tmp_workspace, builtin_skills_dir=tmp_builtin)

        prompt = cb.build_system_prompt(selected_skills=[])
        # Non-empty XML block — this is the safety net
        assert "<skills>" in prompt
        assert "<name>simple</name>" in prompt


# Note: the remote-skill materialization tests (``_materialize_remote``
# / ``_is_remote``) were retired alongside the EverOS HTTP integration.
# Only file-system-backed skills flow through ``SkillService`` now.


# ----------------------------------------------------------------------
# Real built-in directory smoke test
# ----------------------------------------------------------------------


class TestRealBuiltinSmokeTest:
    """Quick sanity check against the actual shipped skills."""

    def test_real_builtin_has_known_skills(self, tmp_path):
        workspace = tmp_path / "ws"
        workspace.mkdir()
        store = SkillRegistry(workspace)  # default builtin dir
        names = {m.name for m in store.list_all()}
        # weather is the only builtin still shipped after the 8 stale
        # builtins were retired.
        assert "weather" in names
