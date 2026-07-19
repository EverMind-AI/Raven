"""CLI tests for ``raven tui`` commands — ``_build_tui_agent_loop`` wiring.

Verifies the memory backend and plugin tools are wired into the AgentLoop
constructed by ``_build_tui_agent_loop``, mirroring the agent-path coverage
in ``test_cli_agent_commands.py``.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock, sentinel

import pytest


@pytest.fixture
def patched_tui_loop_deps(monkeypatch: pytest.MonkeyPatch, tmp_path):
    """Patch all heavy deps of ``_build_tui_agent_loop`` for isolation.

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
    monkeypatch.setattr("raven.cli._helpers.load_runtime_config", lambda _a, _b: config)
    monkeypatch.setattr("raven.cli._helpers.make_provider", lambda _c: MagicMock())

    ec_config = MagicMock()
    ec_config.skill_forge = MagicMock()
    ec_config.runtime = MagicMock()
    monkeypatch.setattr("raven.config.raven.load_raven_config", lambda: ec_config)

    monkeypatch.setattr("raven.session.manager.SessionManager", lambda _wp: MagicMock())
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
        "raven.cli._plugin_stack.build_plugin_registry",
        lambda cfg: fake_registry,
    )
    monkeypatch.setattr(
        "raven.cli._plugin_stack.maybe_build_memory_backend",
        lambda ws, cfg, *, registry=None: fake_backend,
    )
    monkeypatch.setattr(
        "raven.cli._plugin_stack.build_plugin_tools",
        lambda ws, cfg, *, registry=None: fake_tools,
    )

    captured["fake_registry"] = fake_registry
    captured["fake_backend"] = fake_backend
    captured["fake_tools"] = fake_tools
    captured["config"] = config
    return captured


# ---------------------------------------------------------------------------
# memory backend wired into AgentLoop
# ---------------------------------------------------------------------------


def test_tui_agent_loop_receives_non_none_backend(patched_tui_loop_deps) -> None:
    """``_build_tui_agent_loop`` must pass ``backend=<non-None>`` to AgentLoop
    when the plugin stack returns a backend (today it passes nothing, so
    ``AgentLoop.backend`` defaults to ``None`` and store/recall are no-ops)."""
    from raven.cli.tui_commands import _build_tui_agent_loop

    _build_tui_agent_loop()

    kwargs = patched_tui_loop_deps["agent_loop_kwargs"]
    assert kwargs.get("backend") is not None, "AgentLoop must receive backend= from _build_tui_agent_loop; got None"
    assert kwargs["backend"] is patched_tui_loop_deps["fake_backend"]


# ---------------------------------------------------------------------------
# plugin tools wired into AgentLoop
# ---------------------------------------------------------------------------


def test_tui_agent_loop_receives_plugin_tools(patched_tui_loop_deps) -> None:
    """``_build_tui_agent_loop`` must pass ``plugin_tools=`` to AgentLoop
    so plugin-contributed tools are registered in the TUI agent's tool registry."""
    from raven.cli.tui_commands import _build_tui_agent_loop

    _build_tui_agent_loop()

    kwargs = patched_tui_loop_deps["agent_loop_kwargs"]
    assert "plugin_tools" in kwargs, "AgentLoop must receive plugin_tools kwarg"
    assert kwargs["plugin_tools"] is patched_tui_loop_deps["fake_tools"]


# ---------------------------------------------------------------------------
# tool_search config wired into AgentLoop
# ---------------------------------------------------------------------------


def test_tui_agent_loop_receives_tool_search_config(patched_tui_loop_deps) -> None:
    """``_build_tui_agent_loop`` must forward ``tool_search_config=`` so the
    interactive TUI honors ``tools.tool_search`` (progressive disclosure) at
    parity with the ``agent`` / ``gateway`` entrypoints; else the feature is
    silently unavailable in the primary interactive surface."""
    from raven.cli.tui_commands import _build_tui_agent_loop

    _build_tui_agent_loop()

    kwargs = patched_tui_loop_deps["agent_loop_kwargs"]
    assert "tool_search_config" in kwargs, "AgentLoop must receive tool_search_config kwarg"
    assert kwargs["tool_search_config"] is patched_tui_loop_deps["config"].tools.tool_search


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
    monkeypatch.setattr("raven.cli._helpers.load_runtime_config", lambda _a, _b: config)
    monkeypatch.setattr("raven.cli._helpers.make_provider", lambda _c: MagicMock())

    ec_config = MagicMock()
    ec_config.skill_forge = MagicMock()
    ec_config.runtime = MagicMock()
    monkeypatch.setattr("raven.config.raven.load_raven_config", lambda: ec_config)

    monkeypatch.setattr("raven.session.manager.SessionManager", lambda _wp: MagicMock())
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

    def _spy_backend(ws, cfg, *, registry=None):
        passed_registries.append(("backend", registry))
        return None

    def _spy_tools(ws, cfg, *, registry=None):
        passed_registries.append(("tools", registry))
        return []

    monkeypatch.setattr("raven.cli._plugin_stack.build_plugin_registry", _spy_registry)
    monkeypatch.setattr("raven.cli._plugin_stack.maybe_build_memory_backend", _spy_backend)
    monkeypatch.setattr("raven.cli._plugin_stack.build_plugin_tools", _spy_tools)

    from raven.cli.tui_commands import _build_tui_agent_loop

    _build_tui_agent_loop()

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
    fake_agent_loop.cron_service = None
    fake_agent_loop.tools.get.return_value = None
    fake_agent_loop.subagents.set_submit = MagicMock()
    ctx["agent_loop"] = fake_agent_loop

    monkeypatch.setattr(
        "raven.cli.tui_commands._build_tui_agent_loop",
        lambda: fake_agent_loop,
    )

    # Stub RPC machinery so _run_rpc_server_until_done can import + construct
    # without a real socket transport.
    fake_dispatcher = MagicMock()
    fake_dispatcher.register = MagicMock()
    monkeypatch.setattr("raven.tui_rpc.dispatcher.Dispatcher", lambda: fake_dispatcher)

    async def _fake_serve_forever():
        await asyncio.sleep(0)

    fake_server = MagicMock()
    fake_server.send_frame = AsyncMock()
    fake_server.serve_forever = _fake_serve_forever
    monkeypatch.setattr(
        "raven.tui_rpc.server.RpcServer",
        lambda **kw: fake_server,
    )

    fake_emitter = MagicMock()
    monkeypatch.setattr(
        "raven.tui_rpc.subscriptions.SubscriptionEmitter",
        lambda **kw: fake_emitter,
    )

    fake_confirm_broker = MagicMock()
    fake_confirm_broker.cancel_all = MagicMock()
    monkeypatch.setattr(
        "raven.tui_rpc.confirm_broker.ConfirmBroker",
        lambda **kw: fake_confirm_broker,
    )

    fake_question_broker = MagicMock()
    monkeypatch.setattr(
        "raven.tui_rpc.question_broker.QuestionBroker",
        lambda **kw: fake_question_broker,
    )

    async def _fake_system_hello(params):
        return {"version": "0.0.0"}

    monkeypatch.setattr("raven.tui_rpc.methods.system.system_hello", _fake_system_hello)
    monkeypatch.setattr("raven.tui_rpc.methods.system.system_ping", AsyncMock())
    monkeypatch.setattr("raven.tui_rpc.methods.system.system_version", AsyncMock())
    monkeypatch.setattr(
        "raven.tui_rpc.methods.register_aligned_methods_except_system",
        MagicMock(),
    )

    fake_turn_scheduler = MagicMock()
    fake_turn_hub = MagicMock()
    fake_turn_ids: dict = {}

    async def _fake_turn_teardown():
        pass

    monkeypatch.setattr(
        "raven.tui_rpc.spine.build_tui",
        lambda *a, **kw: (fake_turn_scheduler, fake_turn_hub, fake_turn_ids, _fake_turn_teardown),
    )

    monkeypatch.setattr("raven.cli._cron_handler.make_on_cron_job", MagicMock())
    monkeypatch.setattr("raven.tui_rpc.methods.turn.clear_active", MagicMock())

    ctx["fake_server"] = fake_server
    ctx["fake_confirm_broker"] = fake_confirm_broker
    ctx["dispatcher"] = fake_dispatcher
    return ctx


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
# fd-level stdout/stderr redirect spans the serve region
# ---------------------------------------------------------------------------


async def test_rpc_runner_activates_fd_redirect_before_backend_start(rpc_server_deps, monkeypatch, tmp_path) -> None:
    """``_run_rpc_server_until_done`` must activate redirect_terminal_fds_to_file
    before calling backend.start() so that everos structlog PrintLogger output
    during start and serve lands in the log file, not on the terminal.

    Spies on the CM entry/exit and on start/stop to assert ordering:
    redirect_enter < start ... stop < redirect_exit (held through the finally).
    """
    import contextlib

    call_log: list[str] = []

    original_start = rpc_server_deps["backend"].start
    original_stop = rpc_server_deps["backend"].stop

    async def _spy_start():
        call_log.append("start")
        await original_start()

    async def _spy_stop():
        call_log.append("stop")
        await original_stop()

    rpc_server_deps["backend"].start = _spy_start
    rpc_server_deps["backend"].stop = _spy_stop

    @contextlib.contextmanager
    def _spy_redirect(path):
        call_log.append("redirect_enter")
        try:
            yield
        finally:
            call_log.append("redirect_exit")

    monkeypatch.setattr(
        "raven.cli.tui_commands.redirect_terminal_fds_to_file",
        _spy_redirect,
    )
    monkeypatch.setattr(
        "raven.config.paths.get_logs_dir",
        lambda: tmp_path,
    )

    await _run_until_done_with_handshake(monkeypatch, rpc_server_deps)

    assert "redirect_enter" in call_log, "redirect_terminal_fds_to_file must be entered"
    enter_idx = call_log.index("redirect_enter")
    start_idx = call_log.index("start")
    assert enter_idx < start_idx, "redirect must be activated BEFORE backend.start()"

    assert "redirect_exit" in call_log, "redirect_terminal_fds_to_file must be exited (restored)"
    exit_idx = call_log.index("redirect_exit")
    stop_idx = call_log.index("stop")
    assert stop_idx < exit_idx, "redirect must be held THROUGH backend.stop()"
