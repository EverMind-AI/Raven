"""CLI plugin-stack helper.

Exercises :func:`build_plugin_registry` and
:func:`maybe_build_memory_backend` against the bundled
``raven.plugin.memory.everos`` plugin installed via entry points.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

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

    def test_no_config_slice_still_carries_the_memory_identity(
        self,
        tmp_path: Path,
    ) -> None:
        """The slice is never bare: the host folds in the ids the recall side
        uses so both sides address the same track."""
        backend = maybe_build_memory_backend(tmp_path, _config())
        assert backend._config == {"user_id": "default", "agent_id": "default"}


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


# ---------------------------------------------------------------------------
# Hard off: memory.backend=None must leave no trace
# ---------------------------------------------------------------------------


class TestHardOff:
    """``memory.backend`` is the single knob that turns EverOS memory on and
    off. Off has to mean *nothing happened*: an evaluation run must be
    byte-identical to one built before the plugin existed."""

    def test_disabled_backend_writes_nothing_to_disk(
        self,
        tmp_path: Path,
        monkeypatch,
    ) -> None:
        """The understand_media tool factory seeded EverOS's home eagerly, so
        turning the memory backend off still created ``~/.everos/raven`` with
        two config files the moment plugin tools were built."""
        from raven.cli._plugin_stack import build_plugin_tools
        from raven.config.paths import get_data_dir

        home = tmp_path / "home"
        home.mkdir()
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.delenv("EVEROS_ROOT", raising=False)

        config = _config(memory_backend=None)
        registry = build_plugin_registry(config)
        assert maybe_build_memory_backend(workspace, config, registry=registry) is None
        build_plugin_tools(workspace, config, registry=registry)

        # Scoped to EverOS's own artifacts: raven's tracing store creates
        # ~/.raven/traces on import and that is a separate subsystem.
        assert not (home / ".everos").exists()
        assert not (get_data_dir() / "everos").exists()
        assert list(workspace.iterdir()) == []

    def test_disabled_backend_sets_no_everos_env(
        self,
        tmp_path: Path,
        monkeypatch,
    ) -> None:
        from raven.cli._plugin_stack import build_plugin_tools

        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.delenv("EVEROS_ROOT", raising=False)

        config = _config(memory_backend=None)
        registry = build_plugin_registry(config)
        maybe_build_memory_backend(tmp_path, config, registry=registry)
        build_plugin_tools(tmp_path, config, registry=registry)

        import os

        assert "EVEROS_ROOT" not in os.environ

    def test_disabled_backend_never_resolves_everos_settings(
        self,
        tmp_path: Path,
    ) -> None:
        """EverOS's settings loader is cached: whoever calls it first pins the
        data root for the whole process. With memory off nobody may call it, or
        a later deliberate enable inherits a root nothing asked for.

        The parser module itself does get imported -- registering
        ``understand_media`` requires knowing whether the extra is installed --
        which is exactly why "no import" is the wrong invariant and "settings
        never resolved" is the right one.
        """
        import subprocess
        import sys

        probe = (
            "import sys, json, os;"
            "from pathlib import Path;"
            "from raven.cli._plugin_stack import ("
            " build_plugin_registry, build_plugin_tools, maybe_build_memory_backend);"
            "from raven.config.raven import MemoryConfig, RavenConfig;"
            "c = RavenConfig(memory=MemoryConfig(backend=None));"
            "r = build_plugin_registry(c);"
            "b = maybe_build_memory_backend(Path('.'), c, registry=r);"
            "build_plugin_tools(Path('.'), c, registry=r);"
            "m = sys.modules.get('everos.config.settings');"
            "calls = None if m is None else "
            "m.load_settings.cache_info().hits + m.load_settings.cache_info().misses;"
            "print(json.dumps({'backend': b, 'settings_calls': calls,"
            " 'root_env': os.environ.get('EVEROS_ROOT')}))"
        )
        import os

        env = {k: v for k, v in os.environ.items() if k != "EVEROS_ROOT"}
        out = subprocess.run(
            [sys.executable, "-c", probe],
            capture_output=True,
            text=True,
            check=True,
            cwd=tmp_path,
            env=env,
        )
        payload = json.loads(out.stdout.strip().splitlines()[-1])
        assert payload["backend"] is None
        assert payload["settings_calls"] in (None, 0)
        assert payload["root_env"] is None


class TestIdentityIsSingleSourced:
    """Recall reads the ids from ``memory``; store read them from the plugin
    slice. A config that set only one wrote to a track it never searched."""

    def test_memory_ids_reach_the_backend(self, tmp_path: Path) -> None:
        config = RavenConfig(
            memory=MemoryConfig(backend="everos", user_id="alice", agent_id="agt"),
        )
        backend = maybe_build_memory_backend(tmp_path, config)
        assert backend is not None
        assert backend._user_id == "alice"
        assert backend._agent_id == "agt"

    def test_explicit_plugin_slice_still_wins(self, tmp_path: Path) -> None:
        config = RavenConfig(
            memory=MemoryConfig(backend="everos", user_id="alice"),
            plugins=PluginsConfig(config={"everos-memory": {"user_id": "override"}}),
        )
        backend = maybe_build_memory_backend(tmp_path, config)
        assert backend is not None
        assert backend._user_id == "override"


# ---------------------------------------------------------------------------
# Keyless default degrade (swarm line: memory defaults on)
# ---------------------------------------------------------------------------


class TestKeylessDefaultDegrade:
    """memory.backend defaults to "everos" on this line, but a local EverOS
    missing either model key refuses to boot and the one-shot path is
    fail-fast -- honoring the default on a keyless install would turn every
    -m run into a 30s hang + exit 1. The DEFAULT therefore degrades to
    no-memory; explicit intent and remote deployments do not."""

    def test_unset_default_without_keys_degrades_to_none(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import raven.config.update_everos as ue

        monkeypatch.setattr(ue, "everos_models_configured", lambda: False)
        backend = maybe_build_memory_backend(tmp_path, RavenConfig())
        assert backend is None

    def test_unset_default_with_keys_builds(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import raven.config.update_everos as ue

        monkeypatch.setattr(ue, "everos_models_configured", lambda: True)
        backend = maybe_build_memory_backend(tmp_path, RavenConfig())
        assert backend is not None

    def test_explicit_everos_without_keys_still_builds(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Stated intent must fail loudly at start(), not be silently
        un-chosen here."""
        import raven.config.update_everos as ue

        monkeypatch.setattr(ue, "everos_models_configured", lambda: False)
        backend = maybe_build_memory_backend(
            tmp_path, _config(memory_backend="everos")
        )
        assert backend is not None

    def test_unset_default_with_remote_base_url_builds(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A configured base_url names a remote service; local model keys
        are irrelevant to it."""
        import raven.config.update_everos as ue

        monkeypatch.setattr(ue, "everos_models_configured", lambda: False)
        backend = maybe_build_memory_backend(
            tmp_path,
            RavenConfig(
                plugins=PluginsConfig(
                    config={"everos-memory": {"base_url": "http://mem.example:9"}},
                ),
            ),
        )
        assert backend is not None
