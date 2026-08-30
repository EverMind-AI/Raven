"""CLI plugin-stack helper.

Exercises :func:`build_plugin_registry` and
:func:`maybe_build_memory_backend` against the ``raven_everos`` plugin,
which the dev environment installs from ``plugins-dist/everos-memory`` and
which discovery finds through the ``raven.plugins`` entry-point group.
"""

from __future__ import annotations

from pathlib import Path

from raven.config.raven import (
    MemoryConfig,
    PluginsConfig,
    RavenConfig,
)
from raven.contracts.memory import MemoryBackend
from raven.core.plugin_stack import (
    build_plugin_hooks,
    build_plugin_registry,
    build_plugin_tools,
    maybe_build_memory_backend,
    named_plugin_roots,
)
from raven.plugins import PluginRegistry

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _config(
    *,
    memory_backend: str | None = "everos",
    disabled: list[str] | None = None,
    plugin_config: dict | None = None,
    dirs: list[str] | None = None,
) -> RavenConfig:
    return RavenConfig(
        memory=MemoryConfig(backend=memory_backend),
        plugins=PluginsConfig(
            disabled=list(disabled or []),
            config=dict(plugin_config or {}),
            dirs=list(dirs or []),
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

    def test_the_two_identities_do_not_arrive_swapped(self, tmp_path: Path) -> None:
        """The whole point of moving identity into ServiceLocator is that a
        mismatch makes every written memory unrecallable with no warning. Until
        this assertion existed, swapping the two arguments at the only
        production wiring point left the entire suite green."""
        config = _config()
        config.memory.user_id = "u-distinct"
        config.memory.agent_id = "a-distinct"
        backend = maybe_build_memory_backend(tmp_path, config)
        assert backend._services.user_id == "u-distinct"
        assert backend._services.agent_id == "a-distinct"


# ---------------------------------------------------------------------------
# plugins.dirs
# ---------------------------------------------------------------------------


class TestConfiguredDirs:
    def test_named_roots_resolve_from_config(self, tmp_path: Path) -> None:
        assert named_plugin_roots(_config(dirs=[str(tmp_path)])) == (tmp_path,)
        assert named_plugin_roots(_config()) == ()
        assert named_plugin_roots(None) == ()

    def test_a_plugin_under_a_named_root_activates(self, tmp_path: Path) -> None:
        root = tmp_path / "plugins"
        (root / "shelf").mkdir(parents=True)
        (root / "shelf" / "raven-plugin.toml").write_text(
            '[plugin]\nid = "shelf"\nversion = "0.1.0"\nenabled_by_default = true\n',
            encoding="utf-8",
        )
        assert "shelf" in build_plugin_registry(_config(dirs=[str(root)])).activated_ids()
        assert "shelf" not in build_plugin_registry(_config()).activated_ids()


# ---------------------------------------------------------------------------
# The provider grant
# ---------------------------------------------------------------------------


class _GrantWatch:
    """A registry stand-in that records what locator each factory was handed."""

    def __init__(self, seen: list) -> None:
        self._seen = seen

    def hook_names(self):
        return ["h"]

    def tool_names(self):
        return ["t"]

    def hook_plugin_id(self, name):
        return "p"

    def tool_plugin_id(self, name):
        return "p"

    def build_hook(self, name, *, config, services):
        self._seen.append(("hook", services.provider))
        return object()

    def build_tool(self, name, *, config, services):
        self._seen.append(("tool", services.provider))
        return object()


class TestProviderGrant:
    def test_the_connections_provider_reaches_hooks_and_tools(self, tmp_path: Path) -> None:
        seen: list = []
        lent = object()
        build_plugin_hooks(tmp_path, _config(), registry=_GrantWatch(seen), provider=lent)
        build_plugin_tools(tmp_path, _config(), registry=_GrantWatch(seen), provider=lent)
        assert seen == [("hook", lent), ("tool", lent)]

    def test_no_provider_lends_none(self, tmp_path: Path) -> None:
        seen: list = []
        build_plugin_hooks(tmp_path, _config(), registry=_GrantWatch(seen))
        assert seen == [("hook", None)]
