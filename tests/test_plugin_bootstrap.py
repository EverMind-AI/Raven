"""PG-3 — end-to-end bootstrap + build_memory_backend integration."""

from __future__ import annotations

import sys
import textwrap
import types
from pathlib import Path

import pytest

from raven.plugin import (
    PluginConflictError,
    PluginNotFoundError,
    PluginRegistry,
    ServiceLocator,
    assemble_plugin_registry,
)


def _write_manifest(
    root: Path,
    plugin_id: str,
    *,
    factory_ref: str,
    backend_name: str = "everos",
    bundled: bool = True,
    enabled: bool = True,
) -> None:
    sub = root / plugin_id
    sub.mkdir(parents=True, exist_ok=True)
    flags = f"bundled = {str(bundled).lower()}\nenabled_by_default = {str(enabled).lower()}\n"
    (sub / "raven-plugin.toml").write_text(
        textwrap.dedent(f"""
        [plugin]
        id = "{plugin_id}"
        version = "0.1.0"
        {flags}

        [[plugin.contributes.memory_backends]]
        name = "{backend_name}"
        factory = "{factory_ref}"
    """),
        encoding="utf-8",
    )


def _install_test_module(name: str, attrs: dict[str, object]) -> None:
    mod = types.ModuleType(name)
    for k, v in attrs.items():
        setattr(mod, k, v)
    sys.modules[name] = mod


@pytest.fixture(autouse=True)
def _cleanup_modules():
    snapshot = set(sys.modules)
    yield
    extras = set(sys.modules) - snapshot
    for k in extras:
        sys.modules.pop(k, None)


# ---------------------------------------------------------------------------
# End-to-end: discover → activate → build
# ---------------------------------------------------------------------------


class TestEndToEnd:
    def test_discover_activate_build(self, tmp_path: Path) -> None:
        """A single bundled plugin reaches the constructed backend."""

        def fake_factory(ctx):
            return {"workspace": str(ctx.services.workspace), **ctx.config}

        _install_test_module("_test_eos", {"make_backend": fake_factory})
        bundled = tmp_path / "bundled"
        _write_manifest(bundled, "everos-memory", factory_ref="_test_eos:make_backend")

        registry = assemble_plugin_registry(
            bundled_dir=bundled,
            entry_points_group=None,
        )
        assert registry.activated_ids() == ["everos-memory"]

        ws = tmp_path / "ws"
        ws.mkdir()
        backend = registry.build_memory_backend(
            "everos",
            config={"mode": "embedded"},
            services=ServiceLocator(workspace=ws),
        )
        assert backend == {"workspace": str(ws), "mode": "embedded"}

    def test_unknown_backend_name(self, tmp_path: Path) -> None:
        registry = assemble_plugin_registry(
            bundled_dir=tmp_path,
            entry_points_group=None,
        )
        with pytest.raises(PluginNotFoundError):
            registry.build_memory_backend(
                "everos",
                config={},
                services=ServiceLocator(workspace=tmp_path),
            )

    def test_disabled_plugin_does_not_register(self, tmp_path: Path) -> None:
        def fake_factory(ctx):
            return "should-not-be-built"

        _install_test_module("_test_disabled", {"mk": fake_factory})
        bundled = tmp_path / "bundled"
        _write_manifest(bundled, "myplug", factory_ref="_test_disabled:mk")

        registry = assemble_plugin_registry(
            bundled_dir=bundled,
            entry_points_group=None,
            disabled=frozenset({"myplug"}),
        )
        assert registry.activated_ids() == []
        assert registry.memory_backend_names() == []


# ---------------------------------------------------------------------------
# Cross-source: bundled wins over user-level for same id
# ---------------------------------------------------------------------------


class TestCrossSource:
    def test_bundled_shadows_user(self, tmp_path: Path) -> None:
        def bundled_factory(ctx):
            return "BUNDLED"

        def user_factory(ctx):
            return "USER"

        _install_test_module("_bp", {"mk": bundled_factory})
        _install_test_module("_up", {"mk": user_factory})

        bundled = tmp_path / "bundled"
        user = tmp_path / "user"
        _write_manifest(bundled, "everos-memory", factory_ref="_bp:mk")
        _write_manifest(user, "everos-memory", factory_ref="_up:mk")

        registry = assemble_plugin_registry(
            bundled_dir=bundled,
            user_dir=user,
            entry_points_group=None,
        )
        # Bundled wins — the user copy is shadowed and never imported.
        result = registry.build_memory_backend(
            "everos",
            config={},
            services=ServiceLocator(workspace=tmp_path),
        )
        assert result == "BUNDLED"


# ---------------------------------------------------------------------------
# Conflict: two activated plugins same backend name
# ---------------------------------------------------------------------------


class TestConflict:
    def test_two_plugins_same_backend_name_fails(self, tmp_path: Path) -> None:
        def fa(ctx):
            return "a"

        def fb(ctx):
            return "b"

        _install_test_module("_pa", {"mk": fa})
        _install_test_module("_pb", {"mk": fb})

        bundled = tmp_path / "bundled"
        _write_manifest(
            bundled,
            "plug-a",
            factory_ref="_pa:mk",
            backend_name="everos",
        )
        _write_manifest(
            bundled,
            "plug-b",
            factory_ref="_pb:mk",
            backend_name="everos",
        )
        with pytest.raises(PluginConflictError, match="everos"):
            assemble_plugin_registry(
                bundled_dir=bundled,
                entry_points_group=None,
            )


# ---------------------------------------------------------------------------
# Empty bootstrap
# ---------------------------------------------------------------------------


class TestEmpty:
    def test_no_sources_returns_empty_registry(self) -> None:
        registry = assemble_plugin_registry(entry_points_group=None)
        assert isinstance(registry, PluginRegistry)
        assert registry.activated_ids() == []
