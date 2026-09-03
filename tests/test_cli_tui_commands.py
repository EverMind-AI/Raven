"""CLI tests for ``raven tui`` -- the launcher in ``raven/cli/tui_commands.py``.

``tui`` builds the agent loop a session runs on, decides whether to host that
loop in-process behind an RPC server or to attach to a gateway already hosting
one, and spawns the Node child that draws the Ink UI out of the vendored
``ui-tui`` tree.

These tests pin what ``_build_agent_loop`` hands the AgentLoop -- the memory
backend, the plugin tools, the single registry shared between them, and the
config slices the TUI must forward -- mirroring the agent-path coverage in
``test_cli_agent_commands.py``; the embedded backend lifecycle inside
``_run_rpc_server_until_done``, including the handshake it starts behind, the
teardown order it keeps, and the root stdout handler it strips; and the launch
surface itself -- ``--check``, ``--dev``, ``--standalone``, ``--workspace`` and
``--home``, the environment handed to the child, the log-path notice on an
abnormal exit, and the layout of the vendored ``ui-tui`` tree spawned from.

Nothing here builds the TypeScript bundle: these exercise the Python-side
launcher, not the Ink runtime, so they pass whether or not ``dist/entry.js`` is
freshly built.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, sentinel

import pytest
from typer.testing import CliRunner

from raven.cli.commands import app
from raven.cli.tui_commands import _UI_TUI_DIR
from raven.config.raven import TokenWiseConfig
from tests.conftest import wired_kwarg

runner = CliRunner()


@pytest.fixture
def patched_tui_loop_deps(monkeypatch: pytest.MonkeyPatch, tmp_path):
    """Patch all heavy deps of ``_build_agent_loop`` for isolation.

    Mirrors ``patched_tui_build_deps`` in ``test_tui_cron_tool_wired.py``
    but additionally stubs the plugin-stack helpers so we can assert their
    return values flow into the AgentLoop constructor kwargs.

    Returns ``captured`` dict the tests inspect.
    """
    monkeypatch.chdir(tmp_path)
    captured: dict[str, Any] = {}

    config = MagicMock()
    config.workspace_path = tmp_path
    config.agents.defaults.model = "stub-model"
    config.agents.defaults.max_tool_iterations = 5
    config.agents.defaults.context_window_tokens = 65_536
    config.agents.defaults.enable_personalization = False
    config.agents.defaults.max_concurrent_subagents = 2
    config.agents.defaults.max_subagent_spawns_per_hour = 10
    config.tools.web.search.api_key = None
    config.tools.web.proxy = None
    config.tools.exec = MagicMock()
    config.tools.restrict_to_workspace = True
    config.tools.mcp_servers = []
    config.tools.sandbox = MagicMock()
    config.channels = MagicMock()
    monkeypatch.setattr("raven.core.config_stack.load_runtime_config", lambda *a, **kw: config)
    monkeypatch.setattr("raven.providers.factory.make_provider", lambda _c: MagicMock())

    ec_config = MagicMock()
    ec_config.skill_forge = MagicMock()
    # A real one, not a MagicMock attribute: the loop's TokenWise stack reads
    # this config's numbers (a breakpoint budget it compares against 1), and a
    # mock answers every comparison with a TypeError.
    ec_config.token_wise = TokenWiseConfig()
    ec_config.runtime = MagicMock()
    monkeypatch.setattr("raven.config.raven.load_raven_config", lambda: ec_config)

    monkeypatch.setattr("raven.session.manager.SessionManager", lambda _wp, **_kw: MagicMock())
    cron_dir = tmp_path / "cron"
    cron_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr("raven.config.paths.get_cron_dir", lambda: cron_dir)

    # AgentLoop spy captures all ctor kwargs.
    class _AgentLoopSpy:
        def __init__(self, **kwargs):
            captured["agent_loop_kwargs"] = kwargs
            self.tools = MagicMock()
            self.configure_personalization = MagicMock()

    monkeypatch.setattr("raven.agent.loop.AgentLoop", _AgentLoopSpy)

    # Stub plugin-stack helpers at the source module so patching works
    # before and after the import is added to tui_commands.
    fake_registry = sentinel.fake_registry
    fake_backend = sentinel.fake_backend
    fake_tools = [sentinel.fake_tool_1]

    monkeypatch.setattr(
        "raven.core.plugin_stack.build_plugin_registry",
        lambda cfg: fake_registry,
    )
    monkeypatch.setattr(
        "raven.core.plugin_stack.maybe_build_memory_backend",
        lambda ws, cfg, *, registry=None, notify=None: fake_backend,
    )
    monkeypatch.setattr(
        "raven.core.plugin_stack.build_plugin_tools",
        lambda ws, cfg, *, registry=None, provider=None: fake_tools,
    )

    captured["fake_registry"] = fake_registry
    captured["fake_backend"] = fake_backend
    captured["fake_tools"] = fake_tools
    captured["config"] = config
    captured["ec_config"] = ec_config
    return captured


# ---------------------------------------------------------------------------
# memory backend wired into AgentLoop
# ---------------------------------------------------------------------------


def test_tui_agent_loop_receives_non_none_backend(patched_tui_loop_deps) -> None:
    """``_build_agent_loop`` must pass ``backend=<non-None>`` to AgentLoop
    when the plugin stack returns a backend (today it passes nothing, so
    ``AgentLoop.backend`` defaults to ``None`` and store/recall are no-ops)."""
    from raven.cli.tui_commands import _build_agent_loop

    _build_agent_loop()

    kwargs = patched_tui_loop_deps["agent_loop_kwargs"]
    assert wired_kwarg(kwargs, "backend") is not None, (
        "AgentLoop must receive backend= from _build_agent_loop; got None"
    )
    assert wired_kwarg(kwargs, "backend") is patched_tui_loop_deps["fake_backend"]


# ---------------------------------------------------------------------------
# plugin tools wired into AgentLoop
# ---------------------------------------------------------------------------


def test_tui_agent_loop_receives_plugin_tools(patched_tui_loop_deps) -> None:
    """``_build_agent_loop`` must pass ``plugin_tools=`` to AgentLoop
    so plugin-contributed tools are registered in the TUI agent's tool registry."""
    from raven.cli.tui_commands import _build_agent_loop

    _build_agent_loop()

    kwargs = patched_tui_loop_deps["agent_loop_kwargs"]
    assert wired_kwarg(kwargs, "plugin_tools") is not None, "AgentLoop must receive plugin_tools"
    assert wired_kwarg(kwargs, "plugin_tools") is patched_tui_loop_deps["fake_tools"]


# ---------------------------------------------------------------------------
# tool_search config wired into AgentLoop
# ---------------------------------------------------------------------------


def test_tui_agent_loop_receives_tool_search_config(patched_tui_loop_deps) -> None:
    """``_build_agent_loop`` must forward ``tool_search_config=`` so the
    interactive TUI honors ``tools.tool_search`` (progressive disclosure) at
    parity with the ``agent`` / ``gateway`` entrypoints; else the feature is
    silently unavailable in the primary interactive surface."""
    from raven.cli.tui_commands import _build_agent_loop

    _build_agent_loop()

    kwargs = patched_tui_loop_deps["agent_loop_kwargs"]
    assert wired_kwarg(kwargs, "tool_search_config") is not None, "AgentLoop must receive tool_search_config"
    assert wired_kwarg(kwargs, "tool_search_config") is patched_tui_loop_deps["config"].tools.tool_search


# ---------------------------------------------------------------------------
# disabled_tools wired into AgentLoop
# ---------------------------------------------------------------------------


def test_tui_agent_loop_does_not_forward_disabled_tools(patched_tui_loop_deps) -> None:
    """The inverse of what this asserted before, and for the reason it asserted it.

    ``tools.disabled_tools`` used to be forwarded here so a blacklisted tool would
    not stay registered in the primary interactive surface. The loop now reads the
    file itself, once per assembled tool array, so the switch is honoured either
    way -- and forwarding it turns the switch one-way: the settings page can take a
    name back out of the file, but nothing can take it out of a set captured before
    the process started, so a tool that was off at launch could never be turned
    back on. The kwarg is left for callers with no config file at all
    (``benchmarks/appworld/agent_cli.py``).

    That the file's own list still takes effect is asserted where it now happens,
    in ``test_agent_loop_disabled_tools.py``."""
    from raven.cli.tui_commands import _build_agent_loop

    _build_agent_loop()

    assert "disabled_tools" not in patched_tui_loop_deps["agent_loop_kwargs"]


# ---------------------------------------------------------------------------
# config slices the TUI used to drop on the floor
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("kwarg", "attr_path"),
    [
        ("context_config", "context"),
        ("memory_config", "memory"),
    ],
)
def test_tui_agent_loop_forwards_raven_config_slices(patched_tui_loop_deps, kwarg: str, attr_path: str) -> None:
    """Slices of the loaded RavenConfig must reach AgentLoop.

    Omitting one is silent: AgentLoop falls back to a default (an empty
    ContextConfig, no memory config), so the user's configuration is ignored
    rather than rejected.
    """
    from raven.cli.tui_commands import _build_agent_loop

    _build_agent_loop()

    kwargs = patched_tui_loop_deps["agent_loop_kwargs"]
    assert wired_kwarg(kwargs, kwarg) is getattr(patched_tui_loop_deps["ec_config"], attr_path)


def test_tui_agent_loop_forwards_the_skill_forge_router_slice(patched_tui_loop_deps) -> None:
    """The TUI forwarded skill_forge_config but not the router slice of the same
    block, so SkillForge routing behaved differently here than in agent/gateway."""
    from raven.cli.tui_commands import _build_agent_loop

    _build_agent_loop()

    kwargs = patched_tui_loop_deps["agent_loop_kwargs"]
    assert wired_kwarg(kwargs, "skill_forge_router_config") is patched_tui_loop_deps["ec_config"].skill_forge.router


def test_tui_agent_loop_forwards_the_jina_key(patched_tui_loop_deps) -> None:
    """Without it web_fetch silently loses the Jina reader in the TUI only."""
    from raven.cli.tui_commands import _build_agent_loop

    _build_agent_loop()

    assert wired_kwarg(patched_tui_loop_deps["agent_loop_kwargs"], "jina_api_key") is not None


def test_tui_agent_loop_receives_a_router_slot(patched_tui_loop_deps) -> None:
    """Model routing (config.routing) used to be reachable from the gateway only,
    because its builder was module-private there. Routing disabled yields None --
    what matters is that the wiring exists at all."""
    from raven.cli.tui_commands import _build_agent_loop

    _build_agent_loop()

    assert "router" in patched_tui_loop_deps["agent_loop_kwargs"]


# ---------------------------------------------------------------------------
# third-party sub-agents wired into AgentLoop (gates run_subagent_dag)
# ---------------------------------------------------------------------------


def test_tui_agent_loop_receives_the_agent_config(patched_tui_loop_deps) -> None:
    """``_build_agent_loop`` must forward ``agents=``.

    ``AgentLoop.__init__`` registers ``run_subagent_dag`` only when that list is
    non-empty (``agent/loop/main.py`` -- the tool's nodes dispatch to those
    agents). Without the kwarg the TUI's loop keeps an empty roster, the tool is
    never registered, and the model has no way to call it at all -- while the
    gateway, which does pass it, can. The DAG progress events the TUI renders
    are emitted by that same tool, so they never fire either.
    """
    from raven.cli.tui_commands import _build_agent_loop

    _build_agent_loop()

    kwargs = patched_tui_loop_deps["agent_loop_kwargs"]
    assert wired_kwarg(kwargs, "agents") is not None, "AgentLoop must receive the agents wiring"
    assert wired_kwarg(kwargs, "agents") is patched_tui_loop_deps["config"].subagents.agents


# ---------------------------------------------------------------------------
# single shared plugin registry (build_plugin_registry called once)
# ---------------------------------------------------------------------------


def test_tui_build_plugin_registry_called_once(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """The plugin registry must be built once and shared between the backend
    and tools calls — avoids double discovery overhead and ensures coherence."""
    monkeypatch.chdir(tmp_path)

    config = MagicMock()
    config.workspace_path = tmp_path
    config.agents.defaults.model = "stub-model"
    config.agents.defaults.max_tool_iterations = 5
    config.agents.defaults.context_window_tokens = 65_536
    config.agents.defaults.enable_personalization = False
    config.agents.defaults.max_concurrent_subagents = 2
    config.agents.defaults.max_subagent_spawns_per_hour = 10
    config.tools.web.search.api_key = None
    config.tools.web.proxy = None
    config.tools.exec = MagicMock()
    config.tools.restrict_to_workspace = True
    config.tools.mcp_servers = []
    config.tools.sandbox = MagicMock()
    config.channels = MagicMock()
    monkeypatch.setattr("raven.core.config_stack.load_runtime_config", lambda *a, **kw: config)
    monkeypatch.setattr("raven.providers.factory.make_provider", lambda _c: MagicMock())

    ec_config = MagicMock()
    ec_config.skill_forge = MagicMock()
    # A real one, not a MagicMock attribute: the loop's TokenWise stack reads
    # this config's numbers (a breakpoint budget it compares against 1), and a
    # mock answers every comparison with a TypeError.
    ec_config.token_wise = TokenWiseConfig()
    ec_config.runtime = MagicMock()
    monkeypatch.setattr("raven.config.raven.load_raven_config", lambda: ec_config)

    monkeypatch.setattr("raven.session.manager.SessionManager", lambda _wp, **_kw: MagicMock())
    cron_dir = tmp_path / "cron"
    cron_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr("raven.config.paths.get_cron_dir", lambda: cron_dir)

    monkeypatch.setattr(
        "raven.agent.loop.AgentLoop",
        lambda **kw: MagicMock(tools=MagicMock(), configure_personalization=MagicMock()),
    )

    call_count = {"build": 0}
    passed_registries: list[Any] = []

    def _spy_registry(cfg):
        call_count["build"] += 1
        return sentinel.shared_registry

    def _spy_backend(ws, cfg, *, registry=None, notify=None):
        passed_registries.append(("backend", registry))
        return None

    def _spy_tools(ws, cfg, *, registry=None, provider=None):
        passed_registries.append(("tools", registry))
        return []

    monkeypatch.setattr("raven.core.plugin_stack.build_plugin_registry", _spy_registry)
    monkeypatch.setattr("raven.core.plugin_stack.maybe_build_memory_backend", _spy_backend)
    monkeypatch.setattr("raven.core.plugin_stack.build_plugin_tools", _spy_tools)

    from raven.cli.tui_commands import _build_agent_loop

    _build_agent_loop()

    assert call_count["build"] == 1, "build_plugin_registry should be called exactly once"
    backend_reg = next(r for name, r in passed_registries if name == "backend")
    tools_reg = next(r for name, r in passed_registries if name == "tools")
    assert backend_reg is sentinel.shared_registry
    assert tools_reg is sentinel.shared_registry


# ---------------------------------------------------------------------------
# _run_rpc_server_until_done — embedded backend lifecycle
# ---------------------------------------------------------------------------


@pytest.fixture
def rpc_server_deps(monkeypatch: pytest.MonkeyPatch):
    """Stub all heavy deps of ``_run_rpc_server_until_done`` so the function
    can be exercised in-process without a real socket or Node child.

    Returns a ``ctx`` dict with the spy backend and call-tracking lists.
    """
    ctx: dict[str, Any] = {
        "start_calls": [],
        "stop_calls": [],
    }

    class _SpyBackend:
        async def start(self):
            ctx["start_calls"].append("start")

        async def stop(self):
            ctx["stop_calls"].append("stop")

    spy_backend = _SpyBackend()
    ctx["backend"] = spy_backend

    fake_agent_loop = MagicMock()
    fake_agent_loop.backend = spy_backend
    # Awaited during teardown before backend.stop(); a bare MagicMock is not
    # awaitable and would swallow the stop it is supposed to precede.
    fake_agent_loop.drain_backend_stores = AsyncMock()
    fake_agent_loop.cron_service = None
    fake_agent_loop.tools.get.return_value = None
    fake_agent_loop.subagents.set_submit = MagicMock()
    ctx["agent_loop"] = fake_agent_loop

    monkeypatch.setattr(
        "raven.cli.tui_commands._build_agent_loop",
        lambda *a, **kw: fake_agent_loop,
    )

    # Stub RPC machinery so _run_rpc_server_until_done can import + construct
    # without a real socket transport.
    fake_dispatcher = MagicMock()
    fake_dispatcher.register = MagicMock()
    monkeypatch.setattr("raven.rpc.dispatcher.Dispatcher", lambda: fake_dispatcher)

    async def _fake_serve_forever():
        await asyncio.sleep(0)

    fake_server = MagicMock()
    fake_server.send_frame = AsyncMock()
    fake_server.serve_forever = _fake_serve_forever
    monkeypatch.setattr(
        "raven.rpc.server.RpcServer",
        lambda **kw: fake_server,
    )

    fake_emitter = MagicMock()
    monkeypatch.setattr(
        "raven.rpc.subscriptions.SubscriptionEmitter",
        lambda **kw: fake_emitter,
    )

    # Load the lazily imported module before patching its attributes; otherwise
    # this fixture can depend on test order through Python's import cache.
    from raven.rpc.methods import system as _system_module

    assert _system_module is not None
    fake_confirm_broker = MagicMock()
    fake_confirm_broker.cancel_all = MagicMock()
    monkeypatch.setattr(
        "raven.rpc.confirm_broker.ConfirmBroker",
        lambda **kw: fake_confirm_broker,
    )

    fake_approval_broker = MagicMock()
    fake_approval_broker.cancel_all = MagicMock()
    monkeypatch.setattr(
        "raven.rpc.approval_broker.ApprovalBroker",
        lambda **kw: fake_approval_broker,
    )

    fake_question_broker = MagicMock()
    monkeypatch.setattr(
        "raven.rpc.question_broker.QuestionBroker",
        lambda **kw: fake_question_broker,
    )

    async def _fake_system_hello(params):
        return {"version": "0.0.0"}

    monkeypatch.setattr("raven.rpc.methods.system.system_hello", _fake_system_hello)
    monkeypatch.setattr("raven.rpc.methods.system.system_ping", AsyncMock())
    monkeypatch.setattr("raven.rpc.methods.system.system_version", AsyncMock())
    monkeypatch.setattr(
        "raven.rpc.methods.register_aligned_methods_except_system",
        MagicMock(),
    )

    fake_turn_scheduler = MagicMock()
    fake_turn_hub = MagicMock()
    fake_turn_ids: dict = {}

    async def _fake_turn_teardown():
        pass

    fake_build_rpc_spine = MagicMock(
        return_value=(fake_turn_scheduler, fake_turn_hub, fake_turn_ids, _fake_turn_teardown)
    )
    monkeypatch.setattr("raven.rpc.spine.build_rpc_spine", fake_build_rpc_spine)

    monkeypatch.setattr("raven.core.cron_stack.make_on_cron_job", MagicMock())
    monkeypatch.setattr("raven.rpc.methods.turn.clear_active", MagicMock())

    # The snapshot backfill walks a real registry; the MagicMock loop has none.
    fake_schedule_backfill = MagicMock(return_value=None)
    monkeypatch.setattr(
        "raven.agent.subagent.probe.schedule_snapshot_verification",
        fake_schedule_backfill,
    )
    ctx["schedule_backfill"] = fake_schedule_backfill

    ctx["fake_server"] = fake_server
    ctx["fake_confirm_broker"] = fake_confirm_broker
    ctx["fake_approval_broker"] = fake_approval_broker
    ctx["fake_build_rpc_spine"] = fake_build_rpc_spine
    ctx["dispatcher"] = fake_dispatcher
    return ctx


async def test_the_acp_snapshot_backfill_is_scheduled_on_the_tui_server_path(
    monkeypatch: pytest.MonkeyPatch, rpc_server_deps
) -> None:
    """The TUI server is hand-wired rather than mounted through
    ``build_rpc_stack``, so the backfill hook has to be called here too --
    unscheduled, a fresh install holds no capability snapshot, every acp row
    reads stateless, and the ``/new-instance`` picker hides those agents."""
    ctx = rpc_server_deps

    await _run_until_done_with_immediate_proc_done(monkeypatch, ctx)

    ctx["schedule_backfill"].assert_called_once_with(ctx["agent_loop"].subagents)


async def _run_until_done_with_immediate_proc_done(monkeypatch, ctx):
    """Helper: drive ``_run_rpc_server_until_done`` with proc_done set immediately
    so the function exits as fast as possible (handshake timeout path — still
    exercises the full try/finally, including start/stop)."""
    from raven.cli.tui_commands import _run_rpc_server_until_done

    proc_done = asyncio.Event()
    proc_done.set()

    fake_sock = MagicMock()
    await _run_rpc_server_until_done(fake_sock, "test-token", 0.01, proc_done)


async def _run_until_done_with_handshake(monkeypatch, ctx):
    """Drive the runner through a successful handshake, then end the session.

    The memory backend is now started *after* the handshake (off the render
    path), so tests that assert backend lifecycle must simulate the handshake:
    we invoke the registered ``system.hello`` handler (which sets
    ``handshake_done``), let the background start run, then set ``proc_done``.
    """
    from raven.cli.tui_commands import _run_rpc_server_until_done

    proc_done = asyncio.Event()
    fake_sock = MagicMock()
    runner = asyncio.create_task(_run_rpc_server_until_done(fake_sock, "test-token", 1.0, proc_done))
    await asyncio.sleep(0.02)  # let the runner register handlers + start serving

    hello = next(c.args[1] for c in ctx["dispatcher"].register.call_args_list if c.args[0] == "system.hello")
    await hello({"client_version": "0.0.1"})  # sets handshake_done -> triggers background backend start
    await asyncio.sleep(0.05)  # let the background start run to completion

    proc_done.set()
    await runner


async def test_rpc_runner_starts_backend_after_handshake(rpc_server_deps, monkeypatch) -> None:
    """``backend.start()`` runs once, in the background after the handshake (off
    the render path), when ``agent_loop.backend`` is not None."""
    await _run_until_done_with_handshake(monkeypatch, rpc_server_deps)

    assert rpc_server_deps["start_calls"] == ["start"], "backend.start() must be called exactly once"


async def test_rpc_runner_calls_backend_stop_on_exit(rpc_server_deps, monkeypatch) -> None:
    """``_run_rpc_server_until_done`` must await ``backend.stop()`` in the finally
    block so the embedded index lock is released on normal exit."""
    await _run_until_done_with_immediate_proc_done(monkeypatch, rpc_server_deps)

    assert rpc_server_deps["stop_calls"] == ["stop"], "backend.stop() must be called exactly once in the finally block"


async def test_rpc_runner_wires_and_cancels_approval_broker(rpc_server_deps, monkeypatch) -> None:
    await _run_until_done_with_immediate_proc_done(monkeypatch, rpc_server_deps)

    approval_broker = rpc_server_deps["fake_approval_broker"]
    assert rpc_server_deps["fake_build_rpc_spine"].call_args.kwargs["approval_responder"] is approval_broker
    registration = __import__("raven.rpc.methods", fromlist=["register"])
    register_mock = registration.register_aligned_methods_except_system
    assert register_mock.call_args.kwargs["approval_broker"] is approval_broker
    approval_broker.cancel_all.assert_called_once_with()


async def test_rpc_runner_stop_called_even_when_serve_raises(rpc_server_deps, monkeypatch) -> None:
    """``backend.stop()`` must be awaited even when an exception propagates through
    the try block of ``_run_rpc_server_until_done``, proving the embedded index lock
    is released regardless of errors.

    ``asyncio.wait`` is patched to raise inside the try body so the exception
    genuinely propagates through the try block (not just through the finally during
    task cancellation). The exception is expected to surface out of the function.
    """
    exc = RuntimeError("simulated asyncio.wait failure")

    async def _raising_wait(*args, **kwargs):
        raise exc

    monkeypatch.setattr("raven.cli.tui_commands.asyncio.wait", _raising_wait)

    from raven.cli.tui_commands import _run_rpc_server_until_done

    proc_done = asyncio.Event()
    fake_sock = MagicMock()

    with pytest.raises(RuntimeError, match="simulated asyncio.wait failure"):
        await _run_rpc_server_until_done(fake_sock, "test-token", 0.01, proc_done)

    assert rpc_server_deps["stop_calls"] == ["stop"], (
        "backend.stop() must still run via finally even when an exception propagates through the try block"
    )


async def test_rpc_runner_start_and_stop_each_called_once(rpc_server_deps, monkeypatch) -> None:
    """Exactly one start and one stop — no double-start or double-stop."""
    await _run_until_done_with_handshake(monkeypatch, rpc_server_deps)

    assert len(rpc_server_deps["start_calls"]) == 1
    assert len(rpc_server_deps["stop_calls"]) == 1


async def test_rpc_runner_skips_lifecycle_when_no_backend(rpc_server_deps, monkeypatch) -> None:
    """When ``agent_loop.backend is None`` (no plugin wired), start/stop are skipped."""
    rpc_server_deps["agent_loop"].backend = None

    await _run_until_done_with_immediate_proc_done(monkeypatch, rpc_server_deps)

    assert rpc_server_deps["start_calls"] == []
    assert rpc_server_deps["stop_calls"] == []


# ---------------------------------------------------------------------------
# Root-logger TTY handler stripped after backend.start()
# ---------------------------------------------------------------------------


async def test_rpc_runner_strips_root_stdout_handler_after_backend_start(rpc_server_deps, monkeypatch) -> None:
    """``_run_rpc_server_until_done`` must strip a root stdout StreamHandler
    installed during ``backend.start()`` (mimicking everos configure_logging)
    before the RPC server begins serving."""
    import logging
    import sys

    installed_handler: list[logging.Handler] = []

    original_start = rpc_server_deps["backend"].start

    async def _start_with_root_handler():
        h = logging.StreamHandler(sys.stdout)
        logging.getLogger().addHandler(h)
        installed_handler.append(h)
        await original_start()

    rpc_server_deps["backend"].start = _start_with_root_handler

    await _run_until_done_with_handshake(monkeypatch, rpc_server_deps)

    assert len(installed_handler) == 1, "spy backend.start() did not run"
    assert installed_handler[0] not in logging.getLogger().handlers, (
        "_run_rpc_server_until_done must strip root stdout StreamHandler installed by backend.start()"
    )


# ---------------------------------------------------------------------------
# log-path notice: surfaced only on abnormal child exit (#131)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("exit_code", "abnormal"),
    [
        (0, False),
        (129, False),
        (130, False),
        (143, False),
        (3, False),
        (1, True),
        (137, True),
        (255, True),
    ],
)
def test_is_abnormal_child_exit(exit_code: int, abnormal: bool) -> None:
    """Clean (0), the graceful signal codes SIGHUP/SIGINT/SIGTERM (129/130/143)
    and the handshake path (3) are not abnormal; a hard SIGKILL (137) is."""
    from raven.cli.tui_commands import _is_abnormal_child_exit

    assert _is_abnormal_child_exit(exit_code) is abnormal


@pytest.mark.parametrize(
    ("exit_code", "should_announce"),
    [(0, False), (129, False), (130, False), (143, False), (1, True), (137, True)],
)
def test_tui_announces_log_path_only_on_abnormal_exit(
    monkeypatch: pytest.MonkeyPatch, tmp_path, exit_code: int, should_announce: bool
) -> None:
    """The log-path notice reaches stderr only on an abnormal child exit; a
    clean exit (0) or Ctrl+C (130) leaves stderr silent."""
    from typer.testing import CliRunner

    from raven.cli import tui_commands

    monkeypatch.setattr(tui_commands, "_stdout_isatty", lambda: False)
    monkeypatch.setattr(tui_commands, "find_node", lambda: ("/fake/node", (22, 0, 0)))
    monkeypatch.setattr(tui_commands, "resolve_dist_entry", lambda: tmp_path / "entry.js")
    monkeypatch.setattr(tui_commands, "_suppress_noisy_watchers", lambda: None)
    monkeypatch.setattr(tui_commands, "redirect_loguru_to_file", lambda *a, **k: tmp_path / "tui.log")
    monkeypatch.setattr(tui_commands, "_diagnose_crash", lambda *a, **k: None)
    monkeypatch.setattr(tui_commands, "run_subprocess_with_rpc", lambda *a, **k: exit_code)

    result = CliRunner(mix_stderr=False).invoke(tui_commands.tui_app, [])

    assert result.exit_code == exit_code
    if should_announce:
        assert "TUI logs" in result.stderr
        assert f"(exit {exit_code})" in result.stderr
    else:
        assert "TUI logs" not in result.stderr


# ---------------------------------------------------------------------------
# deliver_files is available in the TUI
# ---------------------------------------------------------------------------


def test_tui_agent_loop_receives_deliverables_store(patched_tui_loop_deps) -> None:
    from raven.agent.tools.deliverables import DeliverableStore
    from raven.cli.tui_commands import _build_agent_loop

    _build_agent_loop()

    kwargs = patched_tui_loop_deps["agent_loop_kwargs"]
    assert isinstance(wired_kwarg(kwargs, "deliverables"), DeliverableStore)


# ---------------------------------------------------------------------------
# --workspace / --home flags
# ---------------------------------------------------------------------------


def test_tui_exposes_a_workspace_flag():
    from typer.testing import CliRunner

    from raven.cli import tui_commands

    r = CliRunner(mix_stderr=False).invoke(tui_commands.tui_app, ["--help"])
    assert r.exit_code == 0
    assert "--workspace" in r.output
    assert "--home" in r.output


def test_the_tui_is_told_which_raven_to_call_back_into(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """The picker signs a user in by running ``raven provider login``, and that
    writes a credential. Resolved through PATH it can be a different install --
    one that writes it where this process does not look, so the login reports
    success and the provider stays unauthenticated."""
    from raven.cli import tui_commands

    entry = tmp_path / "raven"
    entry.write_text("#!/bin/sh\n", encoding="utf-8")
    monkeypatch.setattr("sys.argv", [str(entry), "tui"])
    monkeypatch.delenv("RAVEN_BIN", raising=False)

    assert tui_commands.child_env()["RAVEN_BIN"] == str(entry)


def test_an_explicitly_pointed_raven_bin_is_left_alone(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    from raven.cli import tui_commands

    entry = tmp_path / "raven"
    entry.write_text("#!/bin/sh\n", encoding="utf-8")
    monkeypatch.setattr("sys.argv", [str(entry), "tui"])
    monkeypatch.setenv("RAVEN_BIN", "/somewhere/else/raven")

    assert tui_commands.child_env()["RAVEN_BIN"] == "/somewhere/else/raven"


def test_an_unnameable_entry_point_leaves_the_variable_unset(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """Unset, not empty: the TUI reads ``RAVEN_BIN?.trim() || 'raven'``, so an
    empty value falls back to PATH while looking like it had been answered."""
    from raven.cli import tui_commands

    monkeypatch.setattr("sys.argv", ["/nowhere/pytest", "tui"])
    monkeypatch.setattr("sys.executable", str(tmp_path / "bin" / "python"))
    monkeypatch.delenv("RAVEN_BIN", raising=False)

    assert "RAVEN_BIN" not in tui_commands.child_env()


def test_python_dash_m_finds_the_script_beside_the_interpreter(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """``python -m raven`` puts a module path in argv[0]; the console script sits
    in the same directory as the interpreter that is running."""
    from raven.cli import tui_commands

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "raven").write_text("#!/bin/sh\n", encoding="utf-8")
    monkeypatch.setattr("sys.argv", [str(tmp_path / "raven" / "__main__.py"), "tui"])
    monkeypatch.setattr("sys.executable", str(bin_dir / "python"))
    monkeypatch.delenv("RAVEN_BIN", raising=False)

    assert tui_commands.child_env()["RAVEN_BIN"] == str(bin_dir / "raven")


def test_every_interactive_spawn_names_the_binary(monkeypatch: pytest.MonkeyPatch) -> None:
    """Both RPC transports build the child environment, and a third spawn site
    added later must not be the one that forgets."""
    import inspect

    from raven.cli import tui_commands

    source = inspect.getsource(tui_commands._spawn_with_rpc_socket)
    assert "child_env()" in source, "the spawn builds its own env"
    assert "os.environ.copy()" not in source, "the spawn bypasses child_env()"


def test_bare_raven_passes_a_plain_value_for_every_tui_option(monkeypatch: pytest.MonkeyPatch) -> None:
    """Bare ``raven`` calls ``tui`` as a plain function, so typer never fills
    its defaults: any option the call site forgets arrives as an ``OptionInfo``
    sentinel instead. That is not inert -- ``--home``'s sentinel is truthy, so
    ``load_runtime_config`` assigns it over ``agents.defaults.workspace`` and
    the next ``Path()`` raises TypeError before the TUI ever starts.

    Asserted over the whole signature rather than the two flags that broke, so
    the next option added to ``tui`` fails here instead of at a user's prompt.
    """
    import inspect

    from typer.models import OptionInfo
    from typer.testing import CliRunner

    from raven.cli import commands, tui_commands

    options = {
        name
        for name, param in inspect.signature(tui_commands.tui).parameters.items()
        if isinstance(param.default, OptionInfo)
    }
    received: dict[str, Any] = {}

    def recorder(_ctx, **kwargs: Any) -> None:
        received.update(kwargs)

    monkeypatch.setattr(tui_commands, "tui", recorder)

    r = CliRunner(mix_stderr=False).invoke(commands.app, [])

    assert r.exit_code == 0, r.output
    assert received.keys() == options
    assert [name for name, value in received.items() if isinstance(value, OptionInfo)] == []


# ---------------------------------------------------------------------------
# attaching to a running gateway (G2): which engine a launch gets
# ---------------------------------------------------------------------------


@pytest.fixture
def tui_launch_spies(monkeypatch: pytest.MonkeyPatch, tmp_path):
    """Stub everything past the engine decision so ``tui`` can be invoked
    headlessly: a fake Node, a fake dist bundle, and recorders in place of
    both launch paths. Returns the ``calls`` dict the tests inspect."""
    from raven.cli import _tui_relay, tui_commands

    monkeypatch.setenv("RAVEN_HOME", str(tmp_path / "home"))
    calls: dict[str, Any] = {}

    monkeypatch.setattr(tui_commands, "find_node", lambda: ("/usr/bin/node", (22, 0, 0)))
    dist = tmp_path / "entry.js"
    dist.write_text("", encoding="utf-8")
    monkeypatch.setattr(tui_commands, "resolve_dist_entry", lambda: dist)
    monkeypatch.setattr("raven.updates.update_notice.maybe_refresh_async", lambda: None)

    def embedded(*args: Any, **kwargs: Any) -> int:
        calls["embedded"] = kwargs
        return 0

    def attached(*args: Any, **kwargs: Any) -> int:
        calls["attached"] = kwargs
        return 0

    monkeypatch.setattr(tui_commands, "run_subprocess_with_rpc", embedded)
    monkeypatch.setattr(_tui_relay, "run_subprocess_attached", attached)
    return calls


def test_tui_attaches_when_a_gateway_hosts_the_page(tui_launch_spies, monkeypatch, tmp_path) -> None:
    from typer.testing import CliRunner

    from raven.cli import _tui_relay, tui_commands
    from raven.cli._tui_relay import AttachPlan

    plan = AttachPlan(port=18999, token="tok", workdir=tmp_path)
    monkeypatch.setattr(_tui_relay, "plan_attach", lambda workspace=None: plan)

    r = CliRunner(mix_stderr=False).invoke(tui_commands.tui_app, [])

    assert r.exit_code == 0, r.output
    assert "attached" in tui_launch_spies and "embedded" not in tui_launch_spies
    assert tui_launch_spies["attached"]["plan"] == plan


def test_tui_stays_embedded_when_no_gateway_is_found(tui_launch_spies, monkeypatch) -> None:
    from typer.testing import CliRunner

    from raven.cli import _tui_relay, tui_commands

    monkeypatch.setattr(_tui_relay, "plan_attach", lambda workspace=None: None)

    r = CliRunner(mix_stderr=False).invoke(tui_commands.tui_app, [])

    assert r.exit_code == 0, r.output
    assert "embedded" in tui_launch_spies and "attached" not in tui_launch_spies


def test_standalone_flag_skips_discovery_entirely(tui_launch_spies, monkeypatch) -> None:
    from typer.testing import CliRunner

    from raven.cli import _tui_relay, tui_commands

    def _must_not_be_called(workspace=None):
        raise AssertionError("--standalone must not discover a gateway")

    monkeypatch.setattr(_tui_relay, "plan_attach", _must_not_be_called)

    r = CliRunner(mix_stderr=False).invoke(tui_commands.tui_app, ["--standalone"])

    assert r.exit_code == 0, r.output
    assert "embedded" in tui_launch_spies and "attached" not in tui_launch_spies


def test_a_pointed_home_skips_discovery_entirely(tui_launch_spies, monkeypatch, tmp_path) -> None:
    """--home points at another instance's data; the recorded gateway serves
    the default one, so an attached engine would be the wrong engine."""
    from typer.testing import CliRunner

    from raven.cli import _tui_relay, tui_commands

    def _must_not_be_called(workspace=None):
        raise AssertionError("--home must not discover a gateway")

    monkeypatch.setattr(_tui_relay, "plan_attach", _must_not_be_called)

    r = CliRunner(mix_stderr=False).invoke(tui_commands.tui_app, ["--home", str(tmp_path / "other")])

    assert r.exit_code == 0, r.output
    assert "embedded" in tui_launch_spies and "attached" not in tui_launch_spies


def test_the_tui_shutdown_stops_subagents_before_it_drains_memory() -> None:
    """A run still going can hand the backend another write, so draining while
    the sub-agents live is draining into a queue still being filled -- and
    closing the adapter under such a write fails it for a reason that has
    nothing to do with the memory service."""
    src = (Path(__file__).resolve().parents[1] / "raven" / "cli" / "tui_commands.py").read_text(encoding="utf-8")
    cancel = src.index("await agent_loop.subagents.cancel_all()")
    pool = src.index("await close_pool()")
    drain = src.index("await agent_loop.drain_backend_stores()")
    stop = src.index("await agent_loop.backend.stop()")

    assert cancel < pool < drain < stop


# ---------------------------------------------------------------------------
# the vendored ui-tui tree the launcher spawns from
# ---------------------------------------------------------------------------


def test_ui_tui_dir_resolves_under_repo_root():
    """Sanity: the launcher's _UI_TUI_DIR points to the vendored ui-tui tree."""
    assert _UI_TUI_DIR.name == "ui-tui"
    assert (_UI_TUI_DIR / "package.json").exists(), f"After fork, ui-tui/package.json should exist at {_UI_TUI_DIR}"
    # Post-fork sanity — vendored hermes-ink package present.
    assert (_UI_TUI_DIR / "packages" / "hermes-ink" / "package.json").exists(), (
        "Vendored @hermes/ink package must be present after fork."
    )
    # GatewayClientStub source present.
    assert (_UI_TUI_DIR / "src" / "gatewayClientStub.ts").exists(), "GatewayClientStub source must exist post-fork."


def test_packages_hermes_ink_dist_gitignored_but_buildable(tmp_path: Path):
    """The vendored @hermes/ink ships an esbuild-built dist/ that is NOT in git
    history (gitignored). Sanity: source entry exists and is buildable.

    We do NOT actually build it here (slow + needs npm); we just confirm the
    source layout expected by `npm run build --prefix packages/hermes-ink`.
    """
    pkg = _UI_TUI_DIR / "packages" / "hermes-ink"
    assert (pkg / "src" / "entry-exports.ts").exists(), "hermes-ink src/entry-exports.ts (esbuild entry) must exist."
    assert (pkg / "package.json").exists()


# ---------------------------------------------------------------------------
# --check / --dev: which spawn path the launcher takes
# ---------------------------------------------------------------------------


def test_check_exits_zero_when_node_ok_and_run_succeeds(monkeypatch):
    """--check returns 0 when node found, version OK, and child spawned."""
    monkeypatch.setattr(
        "raven.cli.tui_commands.find_node",
        lambda: ("/usr/bin/node", (22, 5, 0)),
    )
    monkeypatch.setattr(
        "raven.cli.tui_commands.run_subprocess",
        lambda *_a, **_kw: 0,
    )
    result = runner.invoke(app, ["tui", "--check"])
    assert result.exit_code == 0, result.output


def test_check_exits_two_when_dist_missing_and_no_dev(monkeypatch, tmp_path):
    """--check without --dev requires dist/entry.js (i.e. `npm run build` must
    have run). When missing, exit code is 2 with a helpful 'npm run build' hint.
    """
    # Point _UI_TUI_DIR at an empty tmp dir so dist/entry.js is missing.
    monkeypatch.setattr(
        "raven.cli.tui_commands._UI_TUI_DIR",
        tmp_path / "ui-tui-fake",
    )
    monkeypatch.setattr(
        "raven.cli.tui_commands.find_node",
        lambda: ("/usr/bin/node", (22, 5, 0)),
    )
    # The fake dir doesn't exist either, but we want to hit the dist-missing
    # branch specifically — so create the parent dir but NOT dist/entry.js.
    fake_dir = tmp_path / "ui-tui-fake"
    fake_dir.mkdir()
    result = runner.invoke(app, ["tui"])
    assert result.exit_code == 2, result.output


def test_dev_mode_uses_rpc_socket_post_fork(monkeypatch, tmp_path):
    """Interactive `--dev` must spawn via run_subprocess_with_rpc, not the
    plain run_subprocess. entry.tsx requires RAVEN_RPC_SOCKET,
    so a plain spawn exits 2 ("spawn via parent"). This guards the regression
    where `--dev` was left on the pre-RPC plain-spawn branch. `--watch` is
    dropped because watch restarts are incompatible with the one-shot RPC
    handshake (a restart drops the accepted socket connection).
    """
    fake_bin = tmp_path / "fake-node" / "bin"
    fake_bin.mkdir(parents=True)
    (fake_bin / "node").write_text("#!/bin/sh\necho ok\n")
    (fake_bin / "npx").write_text("#!/bin/sh\necho ok\n")

    monkeypatch.setattr(
        "raven.cli.tui_commands.find_node",
        lambda: (str(fake_bin / "node"), (22, 5, 0)),
    )

    captured: dict[str, object] = {}

    def fake_run_subprocess_with_rpc(binary, args, cwd, **_kw):
        captured["args"] = args
        return 0

    def fail_plain(*_a, **_kw):
        raise AssertionError("interactive --dev must not use plain run_subprocess")

    monkeypatch.setattr(
        "raven.cli.tui_commands.run_subprocess_with_rpc",
        fake_run_subprocess_with_rpc,
    )
    monkeypatch.setattr(
        "raven.cli.tui_commands.run_subprocess",
        fail_plain,
    )

    result = runner.invoke(app, ["tui", "--dev"])
    assert result.exit_code == 0, result.output
    args = captured["args"]
    assert "tsx" in args, f"--dev should invoke tsx, got args={args!r}"
    assert "src/entry.tsx" in args, f"--dev should run src/entry.tsx, got args={args!r}"
    assert "--watch" not in args, f"--dev must drop --watch (RPC is one-shot), got args={args!r}"


def test_dev_check_mode_keeps_plain_spawn(monkeypatch, tmp_path):
    """`--dev --check` stays on the plain run_subprocess: entry.tsx short-
    circuits on RAVEN_TUI_CHECK before the socket guard, so the smoke path
    needs no RPC server. Opening one would be wasted setup."""
    fake_bin = tmp_path / "fake-node" / "bin"
    fake_bin.mkdir(parents=True)
    (fake_bin / "node").write_text("#!/bin/sh\necho ok\n")
    (fake_bin / "npx").write_text("#!/bin/sh\necho ok\n")

    monkeypatch.setattr(
        "raven.cli.tui_commands.find_node",
        lambda: (str(fake_bin / "node"), (22, 5, 0)),
    )

    captured: dict[str, object] = {}

    def fake_run_subprocess(binary, args, cwd, **_kw):
        captured["args"] = args
        return 0

    def fail_rpc(*_a, **_kw):
        raise AssertionError("--dev --check must not open an RPC socket")

    monkeypatch.setattr(
        "raven.cli.tui_commands.run_subprocess",
        fake_run_subprocess,
    )
    monkeypatch.setattr(
        "raven.cli.tui_commands.run_subprocess_with_rpc",
        fail_rpc,
    )

    result = runner.invoke(app, ["tui", "--dev", "--check"])
    assert result.exit_code == 0, result.output
    args = captured["args"]
    assert "tsx" in args, f"--dev --check should invoke tsx, got args={args!r}"
    assert "src/entry.tsx" in args, f"--dev --check should run src/entry.tsx, got args={args!r}"


def test_check_sets_raven_tui_check_env_var(monkeypatch):
    """`--check` must propagate RAVEN_TUI_CHECK=1 into the environment so
    the Node child (ui-tui/src/entry.tsx) takes the early-exit smoke path
    instead of starting the Ink UI. Without this, `--check` would block the
    terminal until the user hit Ctrl+C — a real bug reported during
    manual smoke that this test guards against regression."""
    monkeypatch.delenv("RAVEN_TUI_CHECK", raising=False)
    monkeypatch.setattr(
        "raven.cli.tui_commands.find_node",
        lambda: ("/usr/bin/node", (22, 5, 0)),
    )

    captured_env: dict[str, str] = {}

    def fake_run_subprocess(*_a, **_kw):
        # Snapshot env at call time.
        import os as _os

        captured_env.update(_os.environ)
        return 0

    monkeypatch.setattr(
        "raven.cli.tui_commands.run_subprocess",
        fake_run_subprocess,
    )

    result = runner.invoke(app, ["tui", "--check"])
    assert result.exit_code == 0, result.output
    assert captured_env.get("RAVEN_TUI_CHECK") == "1", (
        "--check must export RAVEN_TUI_CHECK=1 so the node child can take "
        "the early-exit smoke path; missing means the smoke test hangs."
    )


def test_backend_start_task_is_held_and_settled_before_the_drain() -> None:
    """[N7-F6] The memory-backend start used to be a bare create_task: the
    loop holds only a weak reference (the documented GC hazard), and nothing
    joined it -- quitting during the up-to-30s EverOS spawn let the finally's
    drain/stop race a still-running start(). The task must be held, and the
    teardown must cancel-and-await it before the backend is drained and
    stopped."""
    import inspect

    from raven.cli import tui_commands

    src = inspect.getsource(tui_commands._run_rpc_server_until_done)
    assert "backend_start_task = asyncio.create_task(_start_backend())" in src
    cancel_at = src.index("backend_start_task.cancel()")
    drain_at = src.index("drain_backend_stores()")
    assert cancel_at < drain_at, "the start must be settled before the backend is drained and stopped"
