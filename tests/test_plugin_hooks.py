"""Plugins contribute hooks: manifest, registry, stack, and the loop's chain.

The loop fires six phases through one chain; without a plugin contribution
kind for hooks, product code launched as `raven acp --config` could not reach
them. Pinned here: the manifest entry, registry activation with cross-plugin
conflict detection, the lenient stack builder, and the end-to-end boarding --
a plugin directory's hook ends up in the built loop's chain.
"""

from __future__ import annotations

import sys
import textwrap
import types
from pathlib import Path

import pytest
from pydantic import ValidationError

from raven.plugins import (
    Contributes,
    DiscoveredPlugin,
    HookContribution,
    ManifestOrigin,
    PluginConflictError,
    PluginManifest,
    PluginRegistry,
)

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


def _discovered(plugin_id: str, hooks: list[tuple[str, str]]) -> DiscoveredPlugin:
    mf = PluginManifest(
        id=plugin_id,
        version="0.1.0",
        enabled_by_default=True,
        contributes=Contributes(hooks=[HookContribution(name=n, factory=f) for n, f in hooks]),
    )
    return DiscoveredPlugin(manifest=mf, source=ManifestOrigin.USER, location=None)


class TestManifestHooks:
    def test_parses_a_hooks_contribution(self) -> None:
        mf = PluginManifest.from_toml_str(
            textwrap.dedent(
                """
                [plugin]
                id = "research-flow"
                version = "0.1.0"
                [[plugin.contributes.hooks]]
                name = "budget_note"
                factory = "research_flow.hooks:make_budget_note"
                """
            )
        )
        assert [h.name for h in mf.contributes.hooks] == ["budget_note"]

    def test_default_hooks_empty(self) -> None:
        assert PluginManifest(id="p", version="0.1.0").contributes.hooks == []

    def test_bad_factory_ref_rejected(self) -> None:
        with pytest.raises(ValidationError):
            HookContribution(name="h", factory="not-a-ref")

    def test_duplicate_hook_names_rejected(self) -> None:
        with pytest.raises(ValidationError, match="duplicate hook name"):
            PluginManifest(
                id="p",
                version="0.1.0",
                contributes=Contributes(
                    hooks=[HookContribution(name="dup", factory="m:a"), HookContribution(name="dup", factory="m:b")]
                ),
            )


class TestRegistryHooks:
    def test_activates_and_builds(self) -> None:
        seen = {}

        def make(ctx):
            seen["config"] = ctx.config
            return "hook-instance"

        _install_module("_ph_a", {"make": make})
        reg = PluginRegistry()
        reg.activate([_discovered("alpha", [("budget_note", "_ph_a:make")])])
        assert reg.hook_names() == ["budget_note"]
        assert reg.hook_plugin_id("budget_note") == "alpha"
        from raven.plugins import ServiceLocator

        built = reg.build_hook(
            "budget_note",
            config={"warn": 0.8},
            services=ServiceLocator(workspace=Path("/w"), user_id="u", agent_id="a"),
        )
        assert built == "hook-instance"
        assert seen["config"] == {"warn": 0.8}

    def test_cross_plugin_name_conflict_raises(self) -> None:
        _install_module("_ph_b", {"make": lambda ctx: None})
        reg = PluginRegistry()
        with pytest.raises(PluginConflictError, match="hook 'same'"):
            reg.activate([_discovered("one", [("same", "_ph_b:make")]), _discovered("two", [("same", "_ph_b:make")])])


class TestBuildPluginHooks:
    def _config(self, plugin_config: dict | None = None):
        from raven.config.raven import PluginsConfig, RavenConfig

        return RavenConfig(plugins=PluginsConfig(config=dict(plugin_config or {})))

    def test_builds_hooks_with_the_plugins_slice(self, tmp_path: Path) -> None:
        from raven.core.plugin_stack import build_plugin_hooks

        def make(ctx):
            return f"hook::{ctx.config.get('flag')}"

        _install_module("_ph_c", {"make": make})
        reg = PluginRegistry()
        reg.activate([_discovered("myplugin", [("h1", "_ph_c:make")])])
        assert build_plugin_hooks(tmp_path, self._config({"myplugin": {"flag": "on"}}), registry=reg) == ["hook::on"]

    def test_failing_and_declining_factories_are_skipped(self, tmp_path: Path) -> None:
        from raven.core.plugin_stack import build_plugin_hooks

        def boom(ctx):
            raise RuntimeError("nope")

        _install_module("_ph_d", {"boom": boom, "decline": lambda ctx: None})
        reg = PluginRegistry()
        reg.activate([_discovered("p", [("a", "_ph_d:boom"), ("b", "_ph_d:decline")])])
        assert build_plugin_hooks(tmp_path, self._config(), registry=reg) == []


def test_the_user_plugin_dir_follows_the_home(tmp_path: Path, monkeypatch) -> None:
    from raven.core.plugin_stack import plugin_discovery_sources

    monkeypatch.setenv("RAVEN_HOME", str(tmp_path / "home"))
    assert plugin_discovery_sources()["user_dir"] == tmp_path / "home" / "plugins"


DEMO_HOOK = """
from raven.agent.hook import AgentHook, HookDecision

class NoteHook(AgentHook):
    fired: list = []

    @property
    def name(self) -> str:
        return "demo-note"

    async def before_iteration(self, ctx):
        NoteHook.fired.append(ctx.iteration)
        return HookDecision()

def make(ctx):
    return NoteHook()
"""


@pytest.mark.asyncio
async def test_a_plugin_hook_boards_the_built_loops_chain(tmp_path: Path, monkeypatch) -> None:
    """End to end: a plugin directory's hook is in the runtime's chain and
    fires on the loop's iteration phase."""
    from raven.config.raven import RavenConfig
    from raven.config.schema import Config
    from raven.contracts.llm_provider import LLMResponse
    from raven.core import plugin_stack, runtime
    from raven.providers.base import LLMProvider
    from raven.spine import ChatType, Origin, Source, TurnRequest

    plug = tmp_path / "plugins" / "demohook"
    plug.mkdir(parents=True)
    plug.joinpath("raven-plugin.toml").write_text(
        '[plugin]\nid = "demohook"\nversion = "1.0"\nenabled_by_default = true\n'
        "[[plugin.contributes.hooks]]\n"
        'name = "demo-note"\nfactory = "demohook_mod:make"\n'
    )
    plug.joinpath("demohook_mod.py").write_text(DEMO_HOOK)
    _INJECTED.add("demohook_mod")
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

    config = Config()
    config.agents.defaults.workspace = str(tmp_path / "ws")
    rt = runtime.build_runtime(config, RavenConfig(), provider=_Provider())
    try:
        names = [h.name for h in rt.loop.hooks]
        assert "demo-note" in names
        await rt.loop._process_message(
            TurnRequest(
                origin=Origin.USER,
                source=Source(channel="test", chat_id="c1", sender_id="user", chat_type=ChatType.DM),
                text="hello",
            ),
            session_key="test:c1",
        )
        hook = next(h for h in rt.loop.hooks if h.name == "demo-note")
        assert type(hook).fired == [1]
    finally:
        rt.discard()
