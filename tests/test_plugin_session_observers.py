"""Plugins observe session retirement: manifest, registry, stack, attachment.

The sixth contribution kind (paper: contracts/session_events.py). Pinned here:
the manifest entry (an observer without a usable name is refused at the door),
registry activation with cross-plugin conflict detection, the lenient stack
builder with its identity stamp, and the attachment discipline -- observers are
built inert at assembly (BIND touches no store), attached to the session store
only when a resident host starts the plugin services, detached when it stops
them, so a one-shot host that never starts services never attaches an observer.
The store-side notify semantics (every delete request, the removal outcome, a
raising observer logged and skipped) live with the store's own tests
(test_session_manager.py); the delete faces that must all reach the shared
store are pinned in test_acp_methods.py and test_rpc_session.py.
"""

from __future__ import annotations

import sys
import textwrap
import types
from pathlib import Path

import pytest
from pydantic import ValidationError

from raven.contracts.llm_provider import LLMResponse
from raven.contracts.session_events import SessionObserver
from raven.plugins import (
    Contributes,
    DiscoveredPlugin,
    ManifestOrigin,
    PluginConflictError,
    PluginManifest,
    PluginRegistry,
    SessionObserverContribution,
)
from raven.providers.base import LLMProvider

_INJECTED: set[str] = set()


def _install_module(name: str, attrs: dict[str, object]) -> None:
    mod = types.ModuleType(name)
    for k, v in attrs.items():
        setattr(mod, k, v)
    sys.modules[name] = mod
    _INJECTED.add(name)


@pytest.fixture(autouse=True)
def _cleanup_modules():
    yield
    for name in _INJECTED:
        sys.modules.pop(name, None)
    _INJECTED.clear()


def _discovered(plugin_id: str, observers: list[tuple[str, str]]) -> DiscoveredPlugin:
    mf = PluginManifest(
        id=plugin_id,
        version="0.1.0",
        enabled_by_default=True,
        contributes=Contributes(
            session_observers=[SessionObserverContribution(name=n, factory=f) for n, f in observers]
        ),
    )
    return DiscoveredPlugin(manifest=mf, source=ManifestOrigin.USER, location=None)


class _Observer:
    """A paper-shaped observer that records what it hears."""

    def __init__(self, tag: str = "obs", log: list | None = None) -> None:
        self.tag = tag
        self.log = log if log is not None else []

    def on_session_deleted(self, session_key: str, removed: bool) -> None:
        self.log.append((self.tag, session_key, removed))


class TestManifestSessionObservers:
    def test_parses_a_session_observers_contribution(self) -> None:
        mf = PluginManifest.from_toml_str(
            textwrap.dedent(
                """
                [plugin]
                id = "code-flow"
                version = "0.1.0"
                [[plugin.contributes.session_observers]]
                name = "workspace_release"
                factory = "some_flow.lifecycle:make_release_observer"
                """
            )
        )
        assert [o.name for o in mf.contributes.session_observers] == ["workspace_release"]

    def test_default_session_observers_empty(self) -> None:
        assert PluginManifest(id="p", version="0.1.0").contributes.session_observers == []

    def test_bad_factory_ref_rejected(self) -> None:
        with pytest.raises(ValidationError):
            SessionObserverContribution(name="o", factory="not-a-ref")

    def test_an_observer_without_a_usable_name_is_a_manifest_error(self) -> None:
        with pytest.raises(ValidationError, match="name"):
            PluginManifest.from_toml_str(
                textwrap.dedent(
                    """
                    [plugin]
                    id = "p"
                    version = "0.1.0"
                    [[plugin.contributes.session_observers]]
                    factory = "m:make"
                    """
                )
            )
        with pytest.raises(ValidationError, match="name"):
            SessionObserverContribution(name="", factory="m:make")

    def test_duplicate_session_observer_names_rejected(self) -> None:
        with pytest.raises(ValidationError, match="duplicate session_observer name"):
            PluginManifest(
                id="p",
                version="0.1.0",
                contributes=Contributes(
                    session_observers=[
                        SessionObserverContribution(name="dup", factory="m:a"),
                        SessionObserverContribution(name="dup", factory="m:b"),
                    ]
                ),
            )


class TestRegistrySessionObservers:
    def test_activates_and_builds(self) -> None:
        seen = {}

        def make(ctx):
            seen["config"] = ctx.config
            return "observer-instance"

        _install_module("_po_a", {"make": make})
        reg = PluginRegistry()
        reg.activate([_discovered("alpha", [("workspace_release", "_po_a:make")])])
        assert reg.session_observer_names() == ["workspace_release"]
        assert reg.session_observer_plugin_id("workspace_release") == "alpha"
        from raven.plugins import ServiceLocator

        built = reg.build_session_observer(
            "workspace_release",
            config={"mode": "strict"},
            services=ServiceLocator(workspace=Path("/w"), user_id="u", agent_id="a"),
        )
        assert built == "observer-instance"
        assert seen["config"] == {"mode": "strict"}

    def test_cross_plugin_name_conflict_raises(self) -> None:
        _install_module("_po_b", {"make": lambda ctx: None})
        reg = PluginRegistry()
        with pytest.raises(PluginConflictError, match="session_observer 'same'"):
            reg.activate([_discovered("one", [("same", "_po_b:make")]), _discovered("two", [("same", "_po_b:make")])])


class TestBuildPluginSessionObservers:
    def _config(self, plugin_config: dict | None = None):
        from raven.config.raven import PluginsConfig, RavenConfig

        return RavenConfig(plugins=PluginsConfig(config=dict(plugin_config or {})))

    def test_builds_observers_with_the_plugins_slice_and_stamps_identity(self, tmp_path: Path) -> None:
        from raven.core.plugin_stack import build_plugin_session_observers

        def make(ctx):
            return _Observer(f"obs::{ctx.config.get('flag')}")

        _install_module("_po_c", {"make": make})
        reg = PluginRegistry()
        reg.activate([_discovered("myplugin", [("o1", "_po_c:make")])])
        built = build_plugin_session_observers(tmp_path, self._config({"myplugin": {"flag": "on"}}), registry=reg)
        assert [o.tag for o in built] == ["obs::on"]
        assert built[0].contributed_by == "myplugin"
        assert isinstance(built[0], SessionObserver), "the paper face is on_session_deleted"

    def test_failing_and_declining_factories_are_skipped(self, tmp_path: Path) -> None:
        """Declining (a factory returning None) is the sanctioned opt-out:
        nothing to release means no observer at all."""
        from raven.core.plugin_stack import build_plugin_session_observers

        def boom(ctx):
            raise RuntimeError("nope")

        _install_module("_po_d", {"boom": boom, "decline": lambda ctx: None})
        reg = PluginRegistry()
        reg.activate([_discovered("p", [("a", "_po_d:boom"), ("b", "_po_d:decline")])])
        assert build_plugin_session_observers(tmp_path, self._config(), registry=reg) == []

    @pytest.mark.parametrize("fake", [object(), "observer-as-str"])
    def test_an_observer_that_cannot_carry_the_stamp_still_boards(self, tmp_path: Path, fake) -> None:
        from raven.core.plugin_stack import build_plugin_session_observers

        def make(ctx):
            return fake

        _install_module("_po_e", {"make": make})
        reg = PluginRegistry()
        reg.activate([_discovered("p", [("o", "_po_e:make")])])
        built = build_plugin_session_observers(tmp_path, self._config(), registry=reg)
        assert built == [fake]
        assert not hasattr(built[0], "contributed_by")

    def test_a_stub_registry_contributes_no_observers(self, tmp_path: Path) -> None:
        from raven.core.plugin_stack import build_plugin_session_observers

        assert build_plugin_session_observers(tmp_path, self._config(), registry=object()) == []


class _Provider(LLMProvider):
    def __init__(self) -> None:
        super().__init__(api_key="test")

    async def chat(
        self,
        messages,
        tools=None,
        model=None,
        max_tokens=4096,
        temperature=0.7,
        reasoning_effort=None,
        tool_choice=None,
    ):
        return LLMResponse(content="ok", finish_reason="stop")

    def get_default_model(self) -> str:
        return "fake/default"


DEMO_OBSERVER = """
class DemoObserver:
    def __init__(self):
        self.heard = []

    def on_session_deleted(self, session_key, removed):
        self.heard.append((session_key, removed))

def make(ctx):
    return DemoObserver()
"""


@pytest.mark.asyncio
async def test_a_plugin_observer_rides_the_built_runtime_and_hears_a_delete(tmp_path: Path, monkeypatch) -> None:
    """End to end: a plugin directory's observer rides discovery -> activation
    -> build -> the loop's inert tuple, satisfies the paper's face, stays off
    the store until the resident host starts the services, hears a live delete
    with its outcome, and stops hearing after the host stops them."""
    from raven.config.raven import RavenConfig
    from raven.config.schema import Config
    from raven.core import plugin_stack, runtime

    plug = tmp_path / "plugins" / "demoobs"
    plug.mkdir(parents=True)
    plug.joinpath("raven-plugin.toml").write_text(
        '[plugin]\nid = "demoobs"\nversion = "1.0"\nenabled_by_default = true\n'
        "[[plugin.contributes.session_observers]]\n"
        'name = "demo_observer"\nfactory = "demoobs_mod:make"\n'
    )
    plug.joinpath("demoobs_mod.py").write_text(DEMO_OBSERVER)
    _INJECTED.add("demoobs_mod")
    monkeypatch.setattr(
        plugin_stack,
        "plugin_discovery_sources",
        lambda: {
            "bundled_dir": tmp_path / "none",
            "user_dir": tmp_path / "plugins",
            "project_dir": tmp_path / "none",
            "entry_points_group": None,
        },
    )
    monkeypatch.setattr(runtime.token_wise_stack, "install_from_config", lambda *a, **k: None)
    monkeypatch.setattr(runtime.token_wise_stack, "caching_probe", lambda *a, **k: False)

    config = Config()
    config.agents.defaults.workspace = str(tmp_path / "ws")
    rt = runtime.build_runtime(config, RavenConfig(), provider=_Provider())
    try:
        observers = rt.loop.session_observers
        assert len(observers) == 1
        assert observers[0].contributed_by == "demoobs"
        assert isinstance(observers[0], SessionObserver)
        assert rt.loop.sessions._delete_observers == (), "BIND touches no store"

        session = rt.loop.sessions.get_or_create("tui:e2e_obs")
        session.add_message("user", "hi")
        rt.loop.sessions.save(session)

        await rt.loop.start_plugin_services()
        assert rt.loop.sessions.delete("tui:e2e_obs") is True
        assert rt.loop.sessions.delete("tui:e2e_obs") is False
        assert observers[0].heard == [("tui:e2e_obs", True), ("tui:e2e_obs", False)]

        await rt.loop.stop_plugin_services()
        rt.loop.sessions.delete("tui:unheard")
        assert len(observers[0].heard) == 2, "a stopped host's observers hear nothing"
    finally:
        rt.discard()
