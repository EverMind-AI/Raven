"""CLI plugin-stack helper.

Exercises :func:`build_plugin_registry` and
:func:`maybe_build_memory_backend` against the ``raven_everos`` plugin,
which the dev environment installs from ``plugins-dist/everos-memory`` and
which discovery finds through the ``raven.plugins`` entry-point group.
"""

from __future__ import annotations

import ast
import logging
from pathlib import Path

import pytest

import raven
from raven.config.raven import (
    MemoryConfig,
    PluginsConfig,
    RavenConfig,
)
from raven.config.schema import Config, MediaGenConfig, ToolsConfig, WebToolsConfig
from raven.contracts.memory import MemoryBackend
from raven.core.plugin_stack import (
    build_plugin_hooks,
    build_plugin_registry,
    build_plugin_tools,
    everos_plugin_installed,
    everos_plugin_missing_note,
    maybe_build_memory_backend,
    named_plugin_roots,
)
from raven.plugins import PluginRegistry
from tests._everos_presence import everos_plugin_absent, everos_plugin_broken

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _config(
    *,
    memory_backend: str | None = "everos",
    disabled: list[str] | None = None,
    plugin_config: dict | None = None,
    dirs: list[str] | None = None,
    tools: ToolsConfig | None = None,
) -> RavenConfig:
    return RavenConfig(
        memory=MemoryConfig(backend=memory_backend),
        plugins=PluginsConfig(
            disabled=list(disabled or []),
            config=dict(plugin_config or {}),
            dirs=list(dirs or []),
        ),
        base=Config(tools=tools) if tools is not None else Config(),
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

    def test_a_missing_backend_is_said_out_loud(self, tmp_path: Path, capsys) -> None:
        cfg = _config(memory_backend="ghost")
        backend = maybe_build_memory_backend(tmp_path, cfg, registry=PluginRegistry())
        assert backend is None
        err = capsys.readouterr().err
        assert "memory.backend" in err and "ghost" in err

    def test_the_shipped_default_missing_names_what_to_install(self, tmp_path: Path, capsys) -> None:
        """The commonest way to reach that line is an install without the plugin.

        Naming only the backends that ARE installed leaves such a reader with
        nothing to do; doctor and the wizard both answer it in one sentence, and
        a turn is the surface that actually reaches this code.
        """
        from tests._everos_presence import everos_plugin_absent

        with everos_plugin_absent():
            backend = maybe_build_memory_backend(tmp_path, _config(memory_backend="everos"), registry=PluginRegistry())

        assert backend is None
        assert "everos-memory" in capsys.readouterr().err


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
            '[plugin]\nid = "shelf"\nversion = "0.1.0"\n',
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
        self._seen.append(("hook", services))
        return object()

    def build_tool(self, name, *, config, services):
        self._seen.append(("tool", services))
        return object()


class TestProviderGrant:
    def test_the_connections_provider_reaches_hooks_and_tools(self, tmp_path: Path) -> None:
        seen: list = []
        lent = object()
        build_plugin_hooks(tmp_path, _config(), registry=_GrantWatch(seen), provider=lent)
        build_plugin_tools(tmp_path, _config(), registry=_GrantWatch(seen), provider=lent)
        assert [(kind, services.provider) for kind, services in seen] == [("hook", lent), ("tool", lent)]

    def test_no_provider_lends_none(self, tmp_path: Path) -> None:
        seen: list = []
        build_plugin_hooks(tmp_path, _config(), registry=_GrantWatch(seen))
        assert [(kind, services.provider) for kind, services in seen] == [("hook", None)]


class TestHostToolsGrants:
    """The host side of ``media_proxy``, ``web_config`` and ``media_config``.

    The plugin side is tested with a hand-built locator; this is the link nothing
    else pins, and the one that was broken: every entrance hands the stack its
    ``RavenConfig``, whose ``tools`` lives under ``base``, so reading ``tools`` off
    the extension object lent None to every deployment.
    """

    def test_the_hosts_media_proxy_and_web_section_reach_tools(self, tmp_path: Path) -> None:
        seen: list = []
        tools = ToolsConfig(
            media=MediaGenConfig(proxy="http://127.0.0.1:7890"),
            web=WebToolsConfig(proxy="socks5://127.0.0.1:1080"),
        )
        config = _config(tools=tools)
        build_plugin_tools(tmp_path, config, registry=_GrantWatch(seen))

        [(kind, services)] = seen
        assert kind == "tool"
        assert services.media_proxy == "http://127.0.0.1:7890"
        assert services.web_config is not None
        assert services.web_config() is config.base.tools.web
        assert services.web_config().proxy == "socks5://127.0.0.1:1080"
        assert callable(services.media_config)

    def test_a_host_without_a_proxy_lends_none_and_its_web_section(self, tmp_path: Path) -> None:
        seen: list = []
        config = _config()
        build_plugin_tools(tmp_path, config, registry=_GrantWatch(seen))

        [(_, services)] = seen
        assert services.media_proxy is None
        assert services.web_config is not None
        assert services.web_config() is config.base.tools.web

    def test_a_base_config_is_read_as_itself(self, tmp_path: Path) -> None:
        """A caller holding the base ``Config`` rather than the extension is read
        directly; the two lanes name the same section."""
        from raven.core.plugin_stack import _host_config

        base = Config(tools=ToolsConfig(media=MediaGenConfig(proxy="http://10.0.0.1:3128")))
        assert _host_config(base) is base
        assert _host_config(_config(tools=base.tools)).tools.media.proxy == "http://10.0.0.1:3128"


# ---------------------------------------------------------------------------
# everos plugin presence
# ---------------------------------------------------------------------------


class TestEverosPresence:
    def test_the_dev_environment_carries_the_plugin(self) -> None:
        assert everos_plugin_installed() is True

    def test_an_uninstalled_distribution_reads_as_absent(self) -> None:
        with everos_plugin_absent():
            assert everos_plugin_installed() is False

    def test_a_plugin_that_is_broken_inside_still_reads_as_installed(self) -> None:
        """The two failures a host must not confuse: gone, and here but broken.

        A package whose own imports fail is present -- the guard says so, and the
        import the caller then makes raises the plugin's own error instead of
        being reported as an absence nobody can act on.
        """
        with everos_plugin_broken():
            assert everos_plugin_installed() is True
            with pytest.raises(ImportError):
                from raven_everos.health import capability_available  # noqa: F401

    def test_no_host_module_imports_the_plugin_at_module_level(self) -> None:
        """Every one of these imports has to stay inside a function.

        A module-level one fails at ``import raven.cli``, before any guard can
        run, and no amount of degrading downstream can recover from that.
        """
        root = Path(raven.__file__).parent
        offenders = [
            f"{path.relative_to(root)}:{node.lineno}"
            for path in sorted(root.rglob("*.py"))
            for node in ast.parse(path.read_text(encoding="utf-8")).body
            if isinstance(node, (ast.Import, ast.ImportFrom))
            for name in ([node.module or ""] if isinstance(node, ast.ImportFrom) else [a.name for a in node.names])
            if name.startswith("raven_everos")
        ]

        assert offenders == []

    def test_the_note_names_the_distribution_and_the_default(self) -> None:
        note = everos_plugin_missing_note()
        assert "everos-memory" in note
        assert "memory.backend" in note


def test_build_plugin_tools_stamps_the_contributing_plugin(tmp_path):
    """[seam-1] The wake grant's namespace comes from this stamp; a plugin
    that could name its own namespace could steal another's partition."""
    from types import SimpleNamespace

    from raven.core.plugin_stack import build_plugin_tools

    class _Tool:
        name = "t1"

    class _Reg:
        def tool_names(self):
            return ["t1"]

        def tool_plugin_id(self, name):
            return "plug-a"

        def build_tool(self, name, config, services):
            return _Tool()

    cfg = SimpleNamespace(
        memory=SimpleNamespace(user_id="u", agent_id="a"),
        plugins=SimpleNamespace(config={}),
    )
    tools = build_plugin_tools(tmp_path, cfg, registry=_Reg(), provider=None)
    assert len(tools) == 1
    assert tools[0].contributed_by == "plug-a"


class _OnboardReg:
    """A registry stand-in contributing one onboard step."""

    def __init__(self, step) -> None:
        self._step = step

    def onboard_names(self):
        return ["everos"]

    def onboard_plugin_id(self, name):
        return None

    def activated_ids(self):
        return []

    def manifest_for(self, plugin_id):
        return None

    def build_onboard_step(self, name, *, config, services):
        if isinstance(self._step, Exception):
            raise self._step
        return self._step


def test_build_onboard_steps_yields_every_activated_step(tmp_path):
    from raven.core.plugin_stack import build_onboard_steps

    step = object()
    assert build_onboard_steps(tmp_path, _config(), registry=_OnboardReg(step)) == [("everos", step)]


def test_build_onboard_steps_skips_a_raising_factory(tmp_path, caplog):
    from raven.core.plugin_stack import build_onboard_steps

    with caplog.at_level("WARNING"):
        assert build_onboard_steps(tmp_path, _config(), registry=_OnboardReg(RuntimeError("boom"))) == []
    assert "boom" in caplog.text


def test_build_onboard_steps_hands_each_plugin_its_own_config_slice(tmp_path):
    """[mrbot] Two plugins, differently named: A owns backend 'a-backend'
    (no onboard screen), B owns both backend and onboard 'b-backend'. B's
    onboard step must receive plugins.config['B'], never A's slice --
    exercises the fix (resolve via the onboard entry's own plugin id)
    directly, independent of the cross-owner case activation now refuses."""
    import sys as _sys
    import types

    from raven.core.plugin_stack import build_onboard_steps
    from raven.plugins import (
        Contributes,
        DiscoveredPlugin,
        ManifestOrigin,
        MemoryBackendContribution,
        OnboardContribution,
        PluginManifest,
        PluginRegistry,
    )

    mod = types.ModuleType("_test_onboard_slice_owner")
    mod.make_backend_a = lambda ctx: "a"
    mod.make_backend_b = lambda ctx: "b"
    mod.make_onboard_step_b = lambda ctx: ("step-b", ctx.config)
    _sys.modules["_test_onboard_slice_owner"] = mod
    try:
        mf_a = PluginManifest(
            id="A",
            version="0.1",
            contributes=Contributes(
                memory_backends=[
                    MemoryBackendContribution(name="a-backend", factory="_test_onboard_slice_owner:make_backend_a"),
                ],
            ),
        )
        mf_b = PluginManifest(
            id="B",
            version="0.1",
            contributes=Contributes(
                memory_backends=[
                    MemoryBackendContribution(name="b-backend", factory="_test_onboard_slice_owner:make_backend_b"),
                ],
                onboard=[
                    OnboardContribution(name="b-backend", factory="_test_onboard_slice_owner:make_onboard_step_b"),
                ],
            ),
        )
        reg = PluginRegistry()
        reg.activate(
            [
                DiscoveredPlugin(manifest=mf_a, source=ManifestOrigin.USER, location=None),
                DiscoveredPlugin(manifest=mf_b, source=ManifestOrigin.USER, location=None),
            ]
        )
        cfg = _config(
            plugin_config={
                "A": {"marker": "a-slice"},
                "B": {"marker": "b-slice"},
            }
        )
        steps = build_onboard_steps(tmp_path, cfg, registry=reg)
        assert [name for name, _ in steps] == ["b-backend"]
        _step_name, received_config = steps[0][1]
        assert received_config == {"marker": "b-slice"}
    finally:
        _sys.modules.pop("_test_onboard_slice_owner", None)


class TestStartBackendDetached:
    """The resident hosts start the memory backend without waiting on it.

    Awaited, the start held every resident boot for the readiness budget --
    10s on a machine whose first everos start has to compile its bytecode --
    and a session that overran it reported no long-term memory while the child
    was still coming up. Detached, the backend's own state machine covers that
    window: ``store`` answers False so the loop retries, ``recall`` returns no
    hits and schedules a probe.
    """

    @pytest.mark.asyncio
    async def test_the_caller_is_not_held_until_the_start_finishes(self) -> None:
        """The whole point: control comes back before ``start()`` is done."""
        import asyncio

        from raven.core.plugin_stack import start_backend_detached

        released = asyncio.Event()
        finished = asyncio.Event()

        class _SlowBackend:
            async def start(self) -> None:
                await released.wait()
                finished.set()

        start_backend_detached(_SlowBackend(), logger=logging.getLogger(__name__))

        # Still blocked inside start(), and we are already here.
        assert not finished.is_set()
        released.set()
        await asyncio.wait_for(finished.wait(), timeout=2)

    @pytest.mark.asyncio
    async def test_the_task_is_held_so_it_cannot_be_collected_mid_flight(self) -> None:
        """asyncio keeps only a weak reference; a dropped task is a lost start."""
        import asyncio
        import gc

        from raven.core import plugin_stack

        gate = asyncio.Event()
        ran = asyncio.Event()

        class _Backend:
            async def start(self) -> None:
                await gate.wait()
                ran.set()

        plugin_stack.start_backend_detached(_Backend(), logger=logging.getLogger(__name__))
        gc.collect()
        assert plugin_stack._PENDING_BACKEND_STARTS, "the in-flight start was not held"
        gate.set()
        await asyncio.wait_for(ran.wait(), timeout=2)
        await asyncio.sleep(0)
        assert not plugin_stack._PENDING_BACKEND_STARTS, "a finished start was not released"

    @pytest.mark.asyncio
    async def test_a_start_that_raises_is_logged_and_not_re_raised(self) -> None:
        """The three call sites all swallowed it; the helper owes them the same."""
        import asyncio

        from raven.core import plugin_stack

        class _Failing:
            async def start(self) -> None:
                raise RuntimeError("no service here")

        logged: list[str] = []

        class _Logger:
            def exception(self, msg: str) -> None:
                logged.append(msg)

        plugin_stack.start_backend_detached(_Failing(), logger=_Logger())
        for _ in range(20):
            await asyncio.sleep(0)
            if logged:
                break
        assert logged and "memory backend start failed" in logged[0]

    @pytest.mark.asyncio
    async def test_no_backend_is_nothing_to_do(self) -> None:
        """``backend`` is None when no memory plugin is wired."""
        from raven.core import plugin_stack

        plugin_stack.start_backend_detached(None, logger=logging.getLogger(__name__))
        assert not plugin_stack._PENDING_BACKEND_STARTS

    @pytest.mark.asyncio
    async def test_cancel_retires_a_start_still_in_flight(self) -> None:
        """A start left running polls an address for a generation that is gone."""
        import asyncio

        from raven.core import plugin_stack

        entered = asyncio.Event()
        cancelled = asyncio.Event()

        class _Hanging:
            async def start(self) -> None:
                entered.set()
                try:
                    await asyncio.sleep(30)
                except asyncio.CancelledError:
                    cancelled.set()
                    raise

        plugin_stack.start_backend_detached(_Hanging(), logger=logging.getLogger(__name__))
        await asyncio.wait_for(entered.wait(), timeout=2)
        plugin_stack.cancel_pending_backend_starts()
        await asyncio.wait_for(cancelled.wait(), timeout=2)
