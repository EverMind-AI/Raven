"""PG-2 — multi-source plugin discovery + conflict resolution."""

from __future__ import annotations

import textwrap
from pathlib import Path

from raven.plugin import (
    DiscoveredPlugin,
    PluginDiscovery,
    Source,
)


def _write_manifest(root: Path, plugin_id: str, *, extra: str = "") -> Path:
    """Drop a minimal valid manifest at ``root/<plugin_id>/raven-plugin.toml``."""
    sub = root / plugin_id
    sub.mkdir(parents=True, exist_ok=True)
    body = textwrap.dedent(f"""
        [plugin]
        id = "{plugin_id}"
        version = "0.1.0"
        {extra}
    """)
    path = sub / "raven-plugin.toml"
    path.write_text(body, encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# File-based scanning
# ---------------------------------------------------------------------------


class TestSingleSource:
    def test_empty_dir_returns_empty_list(self, tmp_path: Path) -> None:
        d = PluginDiscovery(bundled_dir=tmp_path)
        assert d.discover() == []

    def test_nonexistent_dir_returns_empty_list(self, tmp_path: Path) -> None:
        d = PluginDiscovery(bundled_dir=tmp_path / "missing")
        assert d.discover() == []

    def test_finds_single_manifest(self, tmp_path: Path) -> None:
        _write_manifest(tmp_path, "foo")
        d = PluginDiscovery(bundled_dir=tmp_path)
        out = d.discover()
        assert len(out) == 1
        assert out[0].manifest.id == "foo"
        assert out[0].source == Source.BUNDLED
        assert out[0].location is not None
        assert out[0].location.name == "raven-plugin.toml"

    def test_finds_multiple_manifests_sorted_by_id(self, tmp_path: Path) -> None:
        for pid in ("zeta", "alpha", "mid"):
            _write_manifest(tmp_path, pid)
        d = PluginDiscovery(user_dir=tmp_path)
        ids = [p.manifest.id for p in d.discover()]
        assert ids == ["alpha", "mid", "zeta"]

    def test_subdir_without_manifest_ignored(self, tmp_path: Path) -> None:
        (tmp_path / "not-a-plugin").mkdir()
        _write_manifest(tmp_path, "real")
        d = PluginDiscovery(bundled_dir=tmp_path)
        assert [p.manifest.id for p in d.discover()] == ["real"]

    def test_malformed_manifest_skipped_silently(
        self,
        tmp_path: Path,
        caplog,
    ) -> None:
        sub = tmp_path / "broken"
        sub.mkdir()
        (sub / "raven-plugin.toml").write_text(
            "not valid toml [[[",
            encoding="utf-8",
        )
        _write_manifest(tmp_path, "ok")
        d = PluginDiscovery(bundled_dir=tmp_path)
        out = d.discover()
        # Broken one is skipped; valid one returned.
        assert [p.manifest.id for p in out] == ["ok"]


# ---------------------------------------------------------------------------
# Cross-source conflict resolution
# ---------------------------------------------------------------------------


class TestConflictResolution:
    def test_bundled_shadows_user(self, tmp_path: Path, caplog) -> None:
        bundled = tmp_path / "bundled"
        user = tmp_path / "user"
        _write_manifest(bundled, "everos")
        _write_manifest(user, "everos")
        d = PluginDiscovery(bundled_dir=bundled, user_dir=user)
        out = d.discover()
        assert len(out) == 1
        # Bundled wins per the "builtin shadow rule".
        assert out[0].source == Source.BUNDLED

    def test_user_shadows_project(self, tmp_path: Path) -> None:
        user = tmp_path / "user"
        project = tmp_path / "project"
        _write_manifest(user, "myplug")
        _write_manifest(project, "myplug")
        d = PluginDiscovery(user_dir=user, project_dir=project)
        out = d.discover()
        assert len(out) == 1
        assert out[0].source == Source.USER

    def test_priority_order_full_chain(self, tmp_path: Path) -> None:
        bundled = tmp_path / "bundled"
        user = tmp_path / "user"
        project = tmp_path / "project"
        # Same id across three sources — bundled must win.
        _write_manifest(bundled, "x")
        _write_manifest(user, "x")
        _write_manifest(project, "x")
        # Add unique ones at each level to confirm non-conflicting
        # plugins all surface.
        _write_manifest(bundled, "b-only")
        _write_manifest(user, "u-only")
        _write_manifest(project, "p-only")
        d = PluginDiscovery(
            bundled_dir=bundled,
            user_dir=user,
            project_dir=project,
        )
        out = d.discover()
        by_id = {p.manifest.id: p.source for p in out}
        assert by_id == {
            "b-only": Source.BUNDLED,
            "u-only": Source.USER,
            "p-only": Source.PROJECT,
            "x": Source.BUNDLED,
        }


# ---------------------------------------------------------------------------
# Subdir-name vs manifest-id mismatch
# ---------------------------------------------------------------------------


class TestSubdirNameMismatch:
    def test_id_in_manifest_wins(self, tmp_path: Path) -> None:
        # Directory called "wrong-dirname" but manifest declares id "correct"
        sub = tmp_path / "wrong-dirname"
        sub.mkdir()
        (sub / "raven-plugin.toml").write_text(
            textwrap.dedent("""
            [plugin]
            id = "correct"
            version = "0.1"
        """),
            encoding="utf-8",
        )
        d = PluginDiscovery(bundled_dir=tmp_path)
        out = d.discover()
        assert out[0].manifest.id == "correct"


# ---------------------------------------------------------------------------
# Frozen record sanity
# ---------------------------------------------------------------------------


class TestDiscoveredPluginRecord:
    def test_record_is_frozen(self, tmp_path: Path) -> None:
        from dataclasses import FrozenInstanceError

        import pytest

        _write_manifest(tmp_path, "x")
        out = PluginDiscovery(bundled_dir=tmp_path).discover()
        rec: DiscoveredPlugin = out[0]
        with pytest.raises(FrozenInstanceError):
            rec.source = Source.USER  # type: ignore[misc]
