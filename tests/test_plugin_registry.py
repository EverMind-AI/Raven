"""PG-2 — PluginRegistry activation + factory resolution + conflict detection."""

from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

from raven.plugins import (
    Contributes,
    DiscoveredPlugin,
    ManifestOrigin,
    MemoryBackendContribution,
    OnboardContribution,
    PluginContext,
    PluginManifest,
    PluginNotFoundError,
    PluginRegistry,
    ServiceLocator,
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
    onboard: list[tuple[str, str]] | None = None,
    bundled: bool = False,
) -> DiscoveredPlugin:
    mf = PluginManifest(
        id=plugin_id,
        version="0.1.0",
        bundled=bundled,
        contributes=Contributes(
            memory_backends=[MemoryBackendContribution(name=n, factory=f) for n, f in (backends or [])],
            onboard=[OnboardContribution(name=n, factory=f) for n, f in (onboard or [])],
        ),
    )
    return DiscoveredPlugin(
        manifest=mf,
        source=ManifestOrigin.BUNDLED if bundled else ManifestOrigin.USER,
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
        reg.activate(
            [
                _make_discovered(
                    "alpha",
                    backends=[
                        ("everos", "_test_plugin_a:make_backend"),
                    ],
                ),
            ]
        )
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
        reg.activate(
            [
                _make_discovered(
                    "plug",
                    backends=[
                        ("everos", "_test_plugin_b:make_backend"),
                    ],
                ),
            ]
        )
        ctx = PluginContext(
            config={"mode": "embedded"},
            services=ServiceLocator(workspace=tmp_path, user_id="default", agent_id="default"),
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
                _make_discovered(
                    "plug",
                    backends=[
                        ("everos", "_test_plugin_c:make_backend"),
                    ],
                ),
            ],
            disabled=frozenset({"plug"}),
        )
        assert reg.activated_ids() == []
        assert reg.memory_backend_names() == []

    def test_every_discovered_plugin_not_disabled_is_activated(self) -> None:
        def fake_factory(ctx):
            return "x"

        _install_test_module("_test_plugin_d", {"make_backend": fake_factory})
        reg = PluginRegistry()
        reg.activate(
            [
                _make_discovered(
                    "plug",
                    backends=[
                        ("everos", "_test_plugin_d:make_backend"),
                    ],
                ),
            ]
        )
        assert reg.activated_ids() == ["plug"]


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
        reg.activate(
            [
                _make_discovered(
                    "alpha",
                    backends=[
                        ("everos", "_test_plugin_e:make_backend"),
                    ],
                ),
                _make_discovered(
                    "beta",
                    backends=[
                        ("everos", "_test_plugin_f:make_backend"),
                    ],
                ),
            ]
        )
        assert reg.activated_ids() == ["alpha"]
        assert reg.get_memory_backend_factory("everos") is fake_a
        [failure] = reg.activation_failures()
        assert failure.plugin_id == "beta"
        assert "memory_backend 'everos' contributed by both 'alpha' and 'beta'" in failure.reason


# ---------------------------------------------------------------------------
# One plugin's failure is its own
# ---------------------------------------------------------------------------


class TestFailureIsolation:
    def test_a_broken_plugin_leaves_the_others_activated(self) -> None:
        _install_test_module("_test_iso_ok", {"make_backend": lambda ctx: "ok"})
        reg = PluginRegistry()
        reg.activate(
            [
                _make_discovered("alpha", backends=[("a", "_test_iso_ok:make_backend")]),
                _make_discovered("broken", backends=[("b", "_nonexistent_iso_zzz:make_backend")]),
                _make_discovered("gamma", backends=[("c", "_test_iso_ok:make_backend")]),
            ]
        )
        assert reg.activated_ids() == ["alpha", "gamma"]
        assert reg.memory_backend_names() == ["a", "c"]
        assert [f.plugin_id for f in reg.activation_failures()] == ["broken"]

    def test_a_plugin_that_fails_halfway_registers_nothing(self) -> None:
        _install_test_module("_test_iso_half", {"make_backend": lambda ctx: "half"})
        reg = PluginRegistry()
        reg.activate(
            [
                _make_discovered(
                    "half",
                    backends=[("half", "_test_iso_half:make_backend")],
                    onboard=[("half", "_test_iso_half:missing_step")],
                ),
            ]
        )
        assert reg.activated_ids() == []
        assert reg.manifest_for("half") is None
        assert reg.memory_backend_names() == []
        assert reg.onboard_names() == []
        [failure] = reg.activation_failures()
        assert failure.manifest.id == "half"

    def test_an_attribute_lookup_that_raises_is_a_failure_not_a_crash(self) -> None:
        mod = types.ModuleType("_test_iso_getattr")

        def boom(name: str):
            raise RuntimeError(f"lazy import of {name} failed")

        mod.__getattr__ = boom  # type: ignore[attr-defined]
        sys.modules["_test_iso_getattr"] = mod
        _install_test_module("_test_iso_ok2", {"make_backend": lambda ctx: "ok"})
        reg = PluginRegistry()
        reg.activate(
            [
                _make_discovered("lazy", backends=[("lazy", "_test_iso_getattr:make_backend")]),
                _make_discovered("steady", backends=[("steady", "_test_iso_ok2:make_backend")]),
            ]
        )
        assert reg.activated_ids() == ["steady"]
        [failure] = reg.activation_failures()
        assert "lazy import of make_backend failed" in failure.reason

    def test_a_failed_file_plugin_takes_its_directory_back_off_sys_path(self, tmp_path: Path) -> None:
        broken_dir = tmp_path / "broken"
        (broken_dir / "_iso_broken_pkg").mkdir(parents=True)
        (broken_dir / "_iso_broken_pkg" / "__init__.py").write_text("raise ImportError('boom')\n")
        good_dir = tmp_path / "good"
        (good_dir / "_iso_good_pkg").mkdir(parents=True)
        (good_dir / "_iso_good_pkg" / "__init__.py").write_text("def make(ctx):\n    return 'good'\n")

        def file_plugin(plugin_id: str, root: Path, ref: str) -> DiscoveredPlugin:
            d = _make_discovered(plugin_id, backends=[(plugin_id, ref)])
            return DiscoveredPlugin(
                manifest=d.manifest, source=ManifestOrigin.USER, location=root / "raven-plugin.toml"
            )

        before = list(sys.path)
        try:
            reg = PluginRegistry()
            reg.activate(
                [
                    file_plugin("broken", broken_dir, "_iso_broken_pkg:make"),
                    file_plugin("good", good_dir, "_iso_good_pkg:make"),
                ]
            )
            assert reg.activated_ids() == ["good"]
            assert str(broken_dir) not in sys.path
            assert str(good_dir) in sys.path
        finally:
            sys.path[:] = before


# ---------------------------------------------------------------------------
# Factory resolution errors
# ---------------------------------------------------------------------------


class TestFactoryResolutionErrors:
    def test_missing_module(self) -> None:
        reg = PluginRegistry()
        reg.activate(
            [
                _make_discovered(
                    "plug",
                    backends=[
                        ("everos", "_nonexistent_module_zzz:make_backend"),
                    ],
                ),
            ]
        )
        assert reg.activated_ids() == []
        assert reg.memory_backend_names() == []
        [failure] = reg.activation_failures()
        assert failure.plugin_id == "plug"
        assert "importing '_nonexistent_module_zzz' failed" in failure.reason

    def test_module_lacks_attribute(self) -> None:
        _install_test_module("_test_plugin_g", {"other_thing": object()})
        reg = PluginRegistry()
        reg.activate(
            [
                _make_discovered(
                    "plug",
                    backends=[
                        ("everos", "_test_plugin_g:make_backend"),
                    ],
                ),
            ]
        )
        assert reg.activated_ids() == []
        assert reg.memory_backend_names() == []
        [failure] = reg.activation_failures()
        assert failure.plugin_id == "plug"
        assert "'_test_plugin_g' has no attribute 'make_backend'" in failure.reason

    def test_attribute_not_callable(self) -> None:
        _install_test_module("_test_plugin_h", {"make_backend": 42})
        reg = PluginRegistry()
        reg.activate(
            [
                _make_discovered(
                    "plug",
                    backends=[
                        ("everos", "_test_plugin_h:make_backend"),
                    ],
                ),
            ]
        )
        assert reg.activated_ids() == []
        assert reg.memory_backend_names() == []
        [failure] = reg.activation_failures()
        assert failure.plugin_id == "plug"
        assert "resolved to a non-callable int" in failure.reason


# ---------------------------------------------------------------------------
# Lookups
# ---------------------------------------------------------------------------


class TestLookup:
    def test_unknown_name_raises_not_found(self) -> None:
        reg = PluginRegistry()
        with pytest.raises(PluginNotFoundError, match="no memory_backend named 'x'"):
            reg.get_memory_backend_factory("x")

    def test_manifest_for_returns_none_when_missing(self) -> None:
        reg = PluginRegistry()
        assert reg.manifest_for("nope") is None

    def test_manifest_for_after_activation(self) -> None:
        def fake(ctx):
            return "x"

        _install_test_module("_test_plugin_i", {"make_backend": fake})
        reg = PluginRegistry()
        reg.activate(
            [
                _make_discovered(
                    "plug",
                    backends=[
                        ("everos", "_test_plugin_i:make_backend"),
                    ],
                ),
            ]
        )
        mf = reg.manifest_for("plug")
        assert mf is not None
        assert mf.id == "plug"


def test_build_onboard_step_calls_the_factory(tmp_path: Path) -> None:
    _install_test_module(
        "_test_onboard",
        {
            "make_onboard_step": lambda ctx: ("step", ctx.config),
            "make_backend": lambda ctx: "backend",
        },
    )
    reg = PluginRegistry()
    reg.activate(
        [
            _make_discovered(
                "plug",
                backends=[("plug", "_test_onboard:make_backend")],
                onboard=[("plug", "_test_onboard:make_onboard_step")],
            )
        ]
    )
    assert reg.onboard_names() == ["plug"]
    assert reg.onboard_plugin_id("plug") == "plug"
    step = reg.build_onboard_step(
        "plug",
        config={"k": 1},
        services=ServiceLocator(workspace=tmp_path, user_id="default", agent_id="default"),
    )
    assert step == ("step", {"k": 1})


def test_build_onboard_step_rejects_an_unknown_name(tmp_path: Path) -> None:
    reg = PluginRegistry()
    with pytest.raises(PluginNotFoundError):
        reg.build_onboard_step(
            "nobody",
            config={},
            services=ServiceLocator(workspace=tmp_path, user_id="default", agent_id="default"),
        )


def test_two_plugins_contribute_same_onboard_name() -> None:
    """mrbot's cross-owner scenario: plugin A owns backend 'shared'; plugin
    B, to satisfy the manifest rule that its onboard name matches its own
    backend name, must also contribute a backend named 'shared'. That
    backend collides with A's before either onboard step registers, so
    activation refuses -- B can never receive A's config slice because B
    never activates at all."""
    _install_test_module("_test_onboard_a", {"make_backend": lambda ctx: "a"})
    _install_test_module(
        "_test_onboard_b",
        {"make_backend": lambda ctx: "b", "make_onboard_step": lambda ctx: "b"},
    )
    reg = PluginRegistry()
    reg.activate(
        [
            _make_discovered("alpha", backends=[("shared", "_test_onboard_a:make_backend")]),
            _make_discovered(
                "beta",
                backends=[("shared", "_test_onboard_b:make_backend")],
                onboard=[("shared", "_test_onboard_b:make_onboard_step")],
            ),
        ]
    )
    assert reg.activated_ids() == ["alpha"]
    assert reg.onboard_names() == []
    [failure] = reg.activation_failures()
    assert failure.plugin_id == "beta"
    assert "memory_backend 'shared'" in failure.reason
