"""CLI plugin-stack helper.

Exercises :func:`build_plugin_registry` and
:func:`maybe_build_memory_backend` against the bundled
``raven.plugin.memory.everos`` plugin installed via entry points.
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("raven.plugin.memory.everos")

from raven.cli._plugin_stack import (
    build_plugin_registry,
    maybe_build_memory_backend,
)
from raven.config.raven import (
    MemoryConfig,
    PluginsConfig,
    RavenConfig,
)
from raven.memory_engine import MemoryBackend
from raven.plugin import PluginRegistry

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _config(
    *,
    memory_backend: str | None = "everos",
    disabled: list[str] | None = None,
    plugin_config: dict | None = None,
) -> RavenConfig:
    return RavenConfig(
        memory=MemoryConfig(backend=memory_backend),
        plugins=PluginsConfig(
            disabled=list(disabled or []),
            config=dict(plugin_config or {}),
        ),
    )


# ---------------------------------------------------------------------------
# build_plugin_registry
# ---------------------------------------------------------------------------


class TestBuildRegistry:
    def test_returns_registry_with_everos_activated(self) -> None:
        reg = build_plugin_registry(_config())
        assert isinstance(reg, PluginRegistry)
        assert "everos-memory" in reg.activated_ids()
        assert "everos" in reg.memory_backend_names()

    def test_disabled_plugin_id_skipped(self) -> None:
        reg = build_plugin_registry(
            _config(disabled=["everos-memory"]),
        )
        assert "everos-memory" not in reg.activated_ids()
        assert "everos" not in reg.memory_backend_names()


# ---------------------------------------------------------------------------
# maybe_build_memory_backend
# ---------------------------------------------------------------------------


class TestMaybeBuildBackend:
    def test_default_config_builds_everos(self, tmp_path: Path) -> None:
        backend = maybe_build_memory_backend(tmp_path, _config())
        assert backend is not None
        assert isinstance(backend, MemoryBackend)

    def test_memory_backend_none_returns_none(
        self,
        tmp_path: Path,
    ) -> None:
        backend = maybe_build_memory_backend(
            tmp_path,
            _config(memory_backend=None),
        )
        assert backend is None

    def test_unknown_backend_returns_none_no_raise(
        self,
        tmp_path: Path,
    ) -> None:
        """A user-config typo / missing plugin must NOT crash boot —
        the helper logs + degrades, AgentLoop falls back to legacy."""
        backend = maybe_build_memory_backend(
            tmp_path,
            _config(memory_backend="nonexistent"),
        )
        assert backend is None

    def test_disabled_backend_returns_none(self, tmp_path: Path) -> None:
        backend = maybe_build_memory_backend(
            tmp_path,
            _config(
                memory_backend="everos",
                disabled=["everos-memory"],
            ),
        )
        assert backend is None


# ---------------------------------------------------------------------------
# Per-plugin config slice resolution
# ---------------------------------------------------------------------------


class TestConfigSliceResolution:
    def test_config_by_plugin_id(self, tmp_path: Path) -> None:
        backend = maybe_build_memory_backend(
            tmp_path,
            _config(
                plugin_config={
                    "everos-memory": {"mode": "embedded", "base_url": "http://x"},
                }
            ),
        )
        # Backend received the config slice keyed by plugin id.
        assert backend._config["mode"] == "embedded"
        assert backend._config["base_url"] == "http://x"

    def test_config_by_backend_name_fallback(
        self,
        tmp_path: Path,
    ) -> None:
        """When the user uses the shorter key (backend name), the
        helper still finds it. Useful for handwritten configs."""
        backend = maybe_build_memory_backend(
            tmp_path,
            _config(
                plugin_config={
                    "everos": {"mode": "http", "base_url": "http://y"},
                }
            ),
        )
        assert backend._config["mode"] == "http"
        assert backend._config["base_url"] == "http://y"

    def test_plugin_id_takes_precedence_over_backend_name(
        self,
        tmp_path: Path,
    ) -> None:
        """If both keys are present, the canonical (plugin id) wins."""
        backend = maybe_build_memory_backend(
            tmp_path,
            _config(
                plugin_config={
                    "everos-memory": {"marker": "canonical"},
                    "everos": {"marker": "fallback"},
                }
            ),
        )
        assert backend._config["marker"] == "canonical"

    def test_no_config_slice_yields_empty_dict(
        self,
        tmp_path: Path,
    ) -> None:
        backend = maybe_build_memory_backend(tmp_path, _config())
        assert backend._config == {}


# ---------------------------------------------------------------------------
# Registry injection — caller can pass a pre-built registry
# ---------------------------------------------------------------------------


class TestRegistryInjection:
    def test_caller_supplied_registry_used(self, tmp_path: Path) -> None:
        reg = build_plugin_registry(_config())
        backend = maybe_build_memory_backend(
            tmp_path,
            _config(),
            registry=reg,
        )
        # Backend constructed via the explicit registry.
        assert backend is not None

    def test_default_construction_creates_internal_registry(
        self,
        tmp_path: Path,
    ) -> None:
        # Sanity: with no registry passed, the helper still works.
        backend = maybe_build_memory_backend(tmp_path, _config())
        assert backend is not None


# ---------------------------------------------------------------------------
# Workspace plumbing through ServiceLocator
# ---------------------------------------------------------------------------


class TestServiceLocatorPlumbing:
    def test_workspace_reaches_backend(self, tmp_path: Path) -> None:
        backend = maybe_build_memory_backend(tmp_path, _config())
        # EverosBackend stores ctx.services on construction.
        assert backend._services.workspace == tmp_path
