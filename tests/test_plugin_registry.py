"""PG-2 — PluginRegistry activation + factory resolution + conflict detection."""

from __future__ import annotations

import sys
import textwrap
import types
from pathlib import Path

import pytest

from raven.plugin import (
    Contributes,
    DiscoveredPlugin,
    MemoryBackendContribution,
    PluginConflict,
    PluginContext,
    PluginFactoryImportError,
    PluginManifest,
    PluginNotFound,
    PluginRegistry,
    ServiceLocator,
    Source,
)


# ---------------------------------------------------------------------------
# In-memory test plugin modules
# ---------------------------------------------------------------------------


def _install_test_module(name: str, attrs: dict[str, object]) -> None:
    """Inject a fake module into ``sys.modules`` for factory resolution
    tests. The module is removed in the per-test cleanup fixture."""
    mod = types.ModuleType(name)
    for k, v in attrs.items():
        setattr(mod, k, v)
    sys.modules[name] = mod


@pytest.fixture(autouse=True)
def _cleanup_modules():
    """Remove every test module we inject so cross-test pollution is
    impossible."""
    snapshot = set(sys.modules)
    yield
    extras = set(sys.modules) - snapshot
    for k in extras:
        sys.modules.pop(k, None)


def _make_discovered(
    plugin_id: str,
    *,
    backends: list[tuple[str, str]] | None = None,
    enabled: bool = True,
    bundled: bool = False,
) -> DiscoveredPlugin:
    mf = PluginManifest(
        id=plugin_id,
        version="0.1.0",
        bundled=bundled,
        enabled_by_default=enabled,
        contributes=Contributes(
            memory_backends=[
                MemoryBackendContribution(name=n, factory=f)
                for n, f in (backends or [])
            ],
        ),
    )
    return DiscoveredPlugin(
        manifest=mf,
        source=Source.BUNDLED if bundled else Source.USER,
        location=None,
    )


# ---------------------------------------------------------------------------
# Successful activation
# ---------------------------------------------------------------------------


class TestActivation:
    def test_activates_single_plugin_with_one_backend(self) -> None:
        def fake_factory(ctx):
            return "backend-instance"

        _install_test_module("_test_plugin_a", {"make_backend": fake_factory})

        reg = PluginRegistry()
        reg.activate([
            _make_discovered("alpha", backends=[
                ("everos", "_test_plugin_a:make_backend"),
            ]),
        ])
        assert reg.activated_ids() == ["alpha"]
        assert reg.memory_backend_names() == ["everos"]
        factory = reg.get_memory_backend_factory("everos")
        assert factory is fake_factory

    def test_factory_invoked_with_context(self, tmp_path: Path) -> None:
        captured = {}

        def fake_factory(ctx: PluginContext):
            captured["ctx"] = ctx
            return ("backend", ctx.config)

        _install_test_module("_test_plugin_b", {"make_backend": fake_factory})

        reg = PluginRegistry()
        reg.activate([
            _make_discovered("plug", backends=[
                ("everos", "_test_plugin_b:make_backend"),
            ]),
        ])
        ctx = PluginContext(
            config={"mode": "embedded"},
            services=ServiceLocator(workspace=tmp_path),
        )
        result = reg.get_memory_backend_factory("everos")(ctx)
        assert result == ("backend", {"mode": "embedded"})
        assert captured["ctx"] is ctx


# ---------------------------------------------------------------------------
# Opt-out / opt-in gating
# ---------------------------------------------------------------------------


class TestEnablement:
    def test_disabled_plugin_is_skipped(self) -> None:
        def fake_factory(ctx):
            return "x"

        _install_test_module("_test_plugin_c", {"make_backend": fake_factory})
        reg = PluginRegistry()
        reg.activate(
            [
                _make_discovered("plug", backends=[
                    ("everos", "_test_plugin_c:make_backend"),
                ]),
            ],
            disabled=frozenset({"plug"}),
        )
        assert reg.activated_ids() == []
        assert reg.memory_backend_names() == []

    def test_non_default_plugin_is_skipped(self) -> None:
        def fake_factory(ctx):
            return "x"

        _install_test_module("_test_plugin_d", {"make_backend": fake_factory})
        reg = PluginRegistry()
        reg.activate([
            _make_discovered("plug", backends=[
                ("everos", "_test_plugin_d:make_backend"),
            ], enabled=False),
        ])
        assert reg.activated_ids() == []


# ---------------------------------------------------------------------------
# Conflicts
# ---------------------------------------------------------------------------


class TestConflicts:
    def test_two_plugins_contribute_same_backend_name(self) -> None:
        def fake_a(ctx):
            return "a"

        def fake_b(ctx):
            return "b"

        _install_test_module("_test_plugin_e", {"make_backend": fake_a})
        _install_test_module("_test_plugin_f", {"make_backend": fake_b})

        reg = PluginRegistry()
        with pytest.raises(PluginConflict, match="everos"):
            reg.activate([
                _make_discovered("alpha", backends=[
                    ("everos", "_test_plugin_e:make_backend"),
                ]),
                _make_discovered("beta", backends=[
                    ("everos", "_test_plugin_f:make_backend"),
                ]),
            ])


# ---------------------------------------------------------------------------
# Factory resolution errors
# ---------------------------------------------------------------------------


class TestFactoryResolutionErrors:
    def test_missing_module(self) -> None:
        reg = PluginRegistry()
        with pytest.raises(PluginFactoryImportError, match="importing"):
            reg.activate([
                _make_discovered("plug", backends=[
                    ("everos", "_nonexistent_module_zzz:make_backend"),
                ]),
            ])

    def test_module_lacks_attribute(self) -> None:
        _install_test_module("_test_plugin_g", {"other_thing": object()})
        reg = PluginRegistry()
        with pytest.raises(PluginFactoryImportError, match="attribute"):
            reg.activate([
                _make_discovered("plug", backends=[
                    ("everos", "_test_plugin_g:make_backend"),
                ]),
            ])

    def test_attribute_not_callable(self) -> None:
        _install_test_module("_test_plugin_h", {"make_backend": 42})
        reg = PluginRegistry()
        with pytest.raises(PluginFactoryImportError, match="non-callable"):
            reg.activate([
                _make_discovered("plug", backends=[
                    ("everos", "_test_plugin_h:make_backend"),
                ]),
            ])


# ---------------------------------------------------------------------------
# Lookups
# ---------------------------------------------------------------------------


class TestLookup:
    def test_unknown_name_raises_not_found(self) -> None:
        reg = PluginRegistry()
        with pytest.raises(PluginNotFound, match="no memory_backend named 'x'"):
            reg.get_memory_backend_factory("x")

    def test_manifest_for_returns_none_when_missing(self) -> None:
        reg = PluginRegistry()
        assert reg.manifest_for("nope") is None

    def test_manifest_for_after_activation(self) -> None:
        def fake(ctx):
            return "x"

        _install_test_module("_test_plugin_i", {"make_backend": fake})
        reg = PluginRegistry()
        reg.activate([
            _make_discovered("plug", backends=[
                ("everos", "_test_plugin_i:make_backend"),
            ]),
        ])
        mf = reg.manifest_for("plug")
        assert mf is not None
        assert mf.id == "plug"
