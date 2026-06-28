"""PG-1 — PluginManifest parsing + schema validation."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest
from pydantic import ValidationError

from raven.plugin import (
    Contributes,
    MemoryBackendContribution,
    PluginManifest,
)


# ---------------------------------------------------------------------------
# Round-trip: minimal valid manifest
# ---------------------------------------------------------------------------


class TestMinimalManifest:
    def test_parses_id_and_version(self) -> None:
        toml = textwrap.dedent("""
            [plugin]
            id = "everos-memory"
            version = "0.1.0"
        """)
        mf = PluginManifest.from_toml_str(toml)
        assert mf.id == "everos-memory"
        assert mf.version == "0.1.0"
        # Defaults
        assert mf.bundled is False
        assert mf.enabled_by_default is False
        assert mf.contributes.memory_backends == []
        assert mf.config_schema == {}

    def test_id_required(self) -> None:
        toml = "[plugin]\nversion = \"0.1.0\"\n"
        with pytest.raises(ValidationError):
            PluginManifest.from_toml_str(toml)

    def test_version_required(self) -> None:
        toml = "[plugin]\nid = \"x\"\n"
        with pytest.raises(ValidationError):
            PluginManifest.from_toml_str(toml)

    def test_id_must_be_non_empty(self) -> None:
        toml = "[plugin]\nid = \"\"\nversion = \"0.1\"\n"
        with pytest.raises(ValidationError):
            PluginManifest.from_toml_str(toml)


# ---------------------------------------------------------------------------
# Top-level [plugin] table required
# ---------------------------------------------------------------------------


class TestTopLevelTable:
    def test_missing_plugin_table_rejected(self) -> None:
        toml = "id = \"x\"\nversion = \"0.1\"\n"
        with pytest.raises(ValueError, match=r"top-level \[plugin\]"):
            PluginManifest.from_toml_str(toml)


# ---------------------------------------------------------------------------
# memory_backends contributions
# ---------------------------------------------------------------------------


class TestMemoryBackends:
    def test_single_contribution(self) -> None:
        toml = textwrap.dedent("""
            [plugin]
            id = "everos-memory"
            version = "0.1.0"

            [[plugin.contributes.memory_backends]]
            name = "everos"
            factory = "raven.plugin.memory.everos.backend:make_backend"
        """)
        mf = PluginManifest.from_toml_str(toml)
        assert len(mf.contributes.memory_backends) == 1
        c = mf.contributes.memory_backends[0]
        assert c.name == "everos"
        assert c.factory == "raven.plugin.memory.everos.backend:make_backend"

    def test_factory_format_rejected_without_colon(self) -> None:
        toml = textwrap.dedent("""
            [plugin]
            id = "x"
            version = "0.1"
            [[plugin.contributes.memory_backends]]
            name = "x"
            factory = "raven.plugin.memory.everos.backend.make_backend"
        """)
        with pytest.raises(ValidationError, match="module.path:callable"):
            PluginManifest.from_toml_str(toml)

    def test_factory_format_rejected_with_empty_callable(self) -> None:
        toml = textwrap.dedent("""
            [plugin]
            id = "x"
            version = "0.1"
            [[plugin.contributes.memory_backends]]
            name = "x"
            factory = "raven.plugin.memory.everos.backend:"
        """)
        with pytest.raises(ValidationError):
            PluginManifest.from_toml_str(toml)

    def test_duplicate_contribution_name_rejected(self) -> None:
        toml = textwrap.dedent("""
            [plugin]
            id = "x"
            version = "0.1"
            [[plugin.contributes.memory_backends]]
            name = "everos"
            factory = "a.b:c"
            [[plugin.contributes.memory_backends]]
            name = "everos"
            factory = "a.b:d"
        """)
        with pytest.raises(ValidationError, match="duplicate memory_backend"):
            PluginManifest.from_toml_str(toml)

    def test_multiple_contributions_different_names(self) -> None:
        toml = textwrap.dedent("""
            [plugin]
            id = "x"
            version = "0.1"
            [[plugin.contributes.memory_backends]]
            name = "primary"
            factory = "a.b:c"
            [[plugin.contributes.memory_backends]]
            name = "fallback"
            factory = "a.b:d"
        """)
        mf = PluginManifest.from_toml_str(toml)
        assert [c.name for c in mf.contributes.memory_backends] == [
            "primary",
            "fallback",
        ]


# ---------------------------------------------------------------------------
# Bundled / enabled_by_default / config_schema passthrough
# ---------------------------------------------------------------------------


class TestFlagsAndSchema:
    def test_bundled_and_default_enabled(self) -> None:
        toml = textwrap.dedent("""
            [plugin]
            id = "everos-memory"
            version = "0.1.0"
            bundled = true
            enabled_by_default = true
        """)
        mf = PluginManifest.from_toml_str(toml)
        assert mf.bundled is True
        assert mf.enabled_by_default is True

    def test_config_schema_passthrough(self) -> None:
        toml = textwrap.dedent("""
            [plugin]
            id = "everos-memory"
            version = "0.1.0"

            [plugin.config_schema]
            mode = "string"
        """)
        mf = PluginManifest.from_toml_str(toml)
        assert mf.config_schema == {"mode": "string"}

    def test_extra_top_level_fields_silently_dropped(self) -> None:
        # Forward-compat: a newer manifest with extra fields should
        # still parse against the older host.
        toml = textwrap.dedent("""
            [plugin]
            id = "x"
            version = "0.1"
            future_field = "ignored"
            another_field = 42
        """)
        mf = PluginManifest.from_toml_str(toml)
        assert mf.id == "x"  # parsed cleanly


# ---------------------------------------------------------------------------
# File-on-disk parsing
# ---------------------------------------------------------------------------


class TestFromTomlPath:
    def test_reads_file(self, tmp_path: Path) -> None:
        path = tmp_path / "raven-plugin.toml"
        path.write_text(textwrap.dedent("""
            [plugin]
            id = "x"
            version = "0.1"
        """), encoding="utf-8")
        mf = PluginManifest.from_toml_path(path)
        assert mf.id == "x"

    def test_missing_file_raises_filenotfound(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError):
            PluginManifest.from_toml_path(tmp_path / "nope.toml")


# ---------------------------------------------------------------------------
# Direct model construction (for tests that don't go through TOML)
# ---------------------------------------------------------------------------


class TestDirectConstruction:
    def test_construct_with_contributes_dataclass_style(self) -> None:
        mf = PluginManifest(
            id="x",
            version="0.1",
            contributes=Contributes(
                memory_backends=[
                    MemoryBackendContribution(name="x", factory="a.b:c"),
                ],
            ),
        )
        assert mf.contributes.memory_backends[0].name == "x"

    def test_frozen_model(self) -> None:
        # frozen=True on the base class — assignment after construction
        # must fail. Catches accidental mutation in registry code.
        mf = PluginManifest(id="x", version="0.1")
        with pytest.raises(ValidationError):
            mf.id = "y"  # type: ignore[misc]
