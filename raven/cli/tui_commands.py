"""Typer subcommand: `raven tui` — launch the Ink+React TUI subprocess.

Bootstrap stage (L2-α `tui-bootstrap`): pure Node spawn, no IPC.
IPC stage (`tui-ipc-bridge`): we additionally open two POSIX
pipes (request + notify), pass them to the Node child as fd 3 / fd 4, and
run a `RpcServer` in an asyncio task that handles `system.hello` (5 s
handshake timeout → exit 3) plus subsequent business RPC methods.

Exit codes (in addition to bootstrap's 0/1/2):
    3  — RPC handshake timeout / failure
"""

from __future__ import annotations

import asyncio
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
from pathlib import Path
from typing import Optional, Tuple

import typer

from raven.cli._log_file import redirect_loguru_to_file

tui_app = typer.Typer(name="tui", help="Launch Raven native TUI (Ink+React).")

# Path to ui-tui/ relative to this file: raven/cli/tui_commands.py -> ../../ui-tui/
_UI_TUI_DIR = Path(__file__).resolve().parent.parent.parent / "ui-tui"

_MIN_NODE_VERSION = (22, 0, 0)


def _stdout_isatty() -> bool:
    """Whether stdout is an interactive TTY (seam for the onboarding gate test;
    CliRunner swaps ``sys.stdout`` for a non-TTY buffer)."""
    return sys.stdout.isatty()


def find_node() -> Tuple[Optional[str], Optional[Tuple[int, int, int]]]:
    """Find a usable node executable (>= 22).

    Returns (path, version_tuple) or (None, None) if not found.
    """
    # Priority 1: RAVEN_NODE env var — explicit override, NO fallback.
    # When the user sets RAVEN_NODE they are forcing a specific binary;
    # if it is missing or unusable we must NOT silently fall back to
    # venv/PATH (that would mask misconfiguration).
    candidates: list[str] = []
    if env_node := os.environ.get("RAVEN_NODE"):
        candidates.append(env_node)
    else:
        # Priority 2: active venv
        if venv := os.environ.get("VIRTUAL_ENV"):
            candidates.append(str(Path(venv) / "bin" / "node"))

        # Priority 3: PATH
        if path_node := shutil.which("node"):
            candidates.append(path_node)

    for node_path in candidates:
        if not Path(node_path).exists():
            continue
        try:
            proc = subprocess.run(
                [node_path, "--version"],
                capture_output=True,
                text=True,
                timeout=5,
                check=True,
            )
            match = re.match(r"v(\d+)\.(\d+)\.(\d+)", proc.stdout.strip())
            if not match:
                continue
            version = (int(match.group(1)), int(match.group(2)), int(match.group(3)))
            return (node_path, version)
        except (subprocess.SubprocessError, FileNotFoundError, OSError):
            continue

    return (None, None)


def run_subprocess(
    node_path: str,
    args: list[str],
    cwd: Path,
    forward_signals: bool = True,
) -> int:
    """Spawn node subprocess, inherit stdio, forward signals, return exit code."""
    proc = subprocess.Popen(
        [node_path, *args],
        cwd=str(cwd),
        stdin=None,
        stdout=None,
        stderr=None,
    )

    if forward_signals:

        def _forward(sig, _frame):
            try:
                proc.send_signal(sig)
            except ProcessLookupError:
                pass

        signal.signal(signal.SIGINT, _forward)
        signal.signal(signal.SIGTERM, _forward)
        if hasattr(signal, "SIGHUP"):
            signal.signal(signal.SIGHUP, _forward)

    try:
        return proc.wait()
    except KeyboardInterrupt:
        # Already forwarded above; wait briefly for graceful exit.
        try:
            return proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            return proc.wait()


# ---------------------------------------------------------------------------
# tui-ipc-bridge: RPC handshake + asyncio server loop
# ---------------------------------------------------------------------------
#
# Topology:
#   parent ─── os.pipe() ──▶ Node (child fd 3) — child writes JSON-RPC requests
#   parent ◀── os.pipe() ─── Node (child fd 4) — parent writes responses + notif
#
# `pass_fds=(req_r, notif_w)` keeps the FDs open across fork+exec. Inside the
# child, Node maps them to fixed numbers (3 / 4) via the RAVEN_RPC_FD_*
# environment variables.

# Handshake budget: spec 5.1 — Node must send `system.hello` within 5 s of
# spawn or the parent aborts with exit 3.
_RPC_HANDSHAKE_TIMEOUT_S: float = 5.0
_RPC_HANDSHAKE_EXIT_CODE: int = 3

# Q11 (2026-05-14): production transport is a per-session unix domain socket.
# The pass_fds variant below is retained for `--check` smoke parity and for
# the existing Python-only handshake-timeout test; the production path here
# replaces it because the Node child cannot reliably wrap an inherited bare
# pipe FD as a stream (see `scripts/run_v001_demo.py` file-top note + the
# v0.0.1 demo discovery log).
_RPC_SOCKET_DIR_PREFIX: str = "eve-rpc-"
_RPC_SOCKET_ENV: str = "RAVEN_RPC_SOCKET"


def _suppress_noisy_watchers() -> None:
    """Raise file-watcher loggers to INFO so ``watchfiles`` per-poll DEBUG
    chatter ('rust notify timeout') stays out of the log sink."""
    import logging as _stdlib_logging

    for _name in ("watchfiles", "watchfiles.main", "watchfiles.watcher", "watchdog", "notify"):
        _stdlib_logging.getLogger(_name).setLevel(_stdlib_logging.INFO)


def _drop_watcher_spam(record: dict) -> bool:
    """Sink filter dropping watchfiles poll-timeout chatter (TUI-only, so the
    shared gateway sink is unaffected)."""
    return "rust notify timeout" not in record["message"]


def _spawn_with_rpc_pipes(
    argv: list[str],
    cwd: Path,
) -> tuple[subprocess.Popen[bytes], int, int]:
    """Spawn `argv` with two private pipes wired for JSON-RPC.

    Returns (popen, parent_request_read_fd, parent_notify_write_fd).
    The two parent-side FDs are owned by the caller and must be closed.
    """
    # Pipe 1: Node → Python (requests). Node writes; Python reads.
    req_r, req_w = os.pipe()
    # Pipe 2: Python → Node (responses + notifications).
    notif_r, notif_w = os.pipe()

    # We must NOT inherit cloexec on the FDs we pass; Popen(pass_fds=...) will
    # clear cloexec on those automatically. We DO want cloexec on our parent-
    # side ends so a future fork doesn't leak them.
    for fd in (req_r, notif_w):
        os.set_inheritable(fd, False)

    env = os.environ.copy()
    # Inside the child these will appear as fd 3 / 4 (Popen remaps in order).
    env["RAVEN_RPC_FD_REQUEST"] = "3"
    env["RAVEN_RPC_FD_NOTIFY"] = "4"

    proc = subprocess.Popen(
        argv,
        cwd=str(cwd),
        stdin=None,
        stdout=None,
        stderr=None,
        env=env,
        pass_fds=(req_w, notif_r),
    )

    # Child has dup'd the inheritable ends; close them in the parent.
    os.close(req_w)
    os.close(notif_r)

    return proc, req_r, notif_w


# Narrow exception classes that represent recoverable init-time crashes —
# kwargs drift after AgentLoop ctor refactor, attribute path drift after
# config schema rename, ImportError on optional extras, missing config file,
# Pydantic ValidationError. All are surfaced as -32603 ``internal_error``
# with ``data.reason="tui_init_crash"`` so the UI can distinguish them from a
# legitimate -32008 ``model_not_available`` (no provider configured).
_TUI_INIT_CRASH_TYPES: tuple[type[BaseException], ...] = (
    TypeError,
    AttributeError,
    ImportError,
    FileNotFoundError,
    OSError,
)


async def _fanout_cron_delivered(emitter, *, job_id, name, text, fired_at) -> None:
    """Fan a ``cron.delivered`` event out to every active TUI session.

    Fan-out (rather than a session-keyed emit) is required because a cron turn
    runs in the ``cron:<job_id>`` conversation, which matches no user
    subscription key. TUI v0.1 is single-session per ``hermes-tui-rpc-architecture``
    5-domain fallback.
    """
    payload = {"job_id": job_id, "name": name, "text": text, "fired_at": fired_at}
    for session_key in list(emitter._by_session.keys()):
        await emitter.emit(session_key, {"type": "cron.delivered", "payload": payload})


def _build_cron_callback_spine(base_on_cron, emitter):
    """Wrap the spine cron callback so a delivering job's reply is fanned out as a
    ``cron.delivered`` event. ``base_on_cron`` (``make_on_cron_job`` with
    ``submit=``) runs the reminder as a CRON turn through the TUI scheduler and
    returns its reply (read back from the runner via ``readback_texts``); the cron
    turn's own hub deliverables target the ``cron:<job_id>`` conversation, which
    has no subscriber and so no-op, making this fan-out the only delivery path."""
    from datetime import datetime, timezone

    async def wrapped(job):
        response = await base_on_cron(job)
        if job.payload.deliver and response:
            await _fanout_cron_delivered(
                emitter,
                job_id=job.id,
                name=job.name,
                text=response,
                fired_at=datetime.now(timezone.utc).isoformat(),
            )
        return response

    return wrapped


def _build_tui_agent_loop():
    """Construct the AgentLoop singleton served by ``turn.send``.

    Mirrors the minimal slice of ``raven agent`` setup needed to handle
    TUI chat turns: config / provider / session manager / AgentLoop.
    Wires a TUI-scoped ``CronService(allowed_channels={"tui"})`` so the
    agent can register reminders from within a TUI turn. Sentinel /
    channel-adapter wiring (which lives in ``cli/agent_commands.py``) is
    intentionally absent — the gateway process owns Sentinel proactivity
    in v0.1. ``run_turn`` lazily ``_start_executor`` + ``_connect_mcp``
    on first call so we do not need an asyncio context here.

    Raises ``InternalError`` (-32603) when AgentLoop construction fails —
    ``_run_rpc_server_until_done`` catches and latches the error onto the
    factory closure passed to ``register_aligned_methods_except_system`` so
    ``turn.send`` can emit it through the subscription emitter (the launcher
    has no live client connection at startup time).
    """
    from pydantic import ValidationError

    from raven.tui_rpc.errors import InternalError

    try:
        from raven.agent.loop import AgentLoop
        from raven.agent.loop.recovery import limits_from_defaults
        from raven.cli._helpers import load_runtime_config, make_provider
        from raven.config.raven import load_raven_config
        from raven.config.paths import get_cron_dir
        from raven.proactive_engine.schedulers.cron.service import CronService
        from raven.proactive_engine.schedulers.cron.tool import CronTool
        from raven.session.manager import SessionManager

        config = load_runtime_config(None, None)
        ec_config = load_raven_config()
        skill_forge_cfg = ec_config.skill_forge

        provider = make_provider(config)
        session_manager = SessionManager(config.workspace_path)

        cron = CronService(
            get_cron_dir() / "jobs.json",
            allowed_channels={"tui"},
        )

        agent_loop = AgentLoop(
            provider=provider,
            workspace=config.workspace_path,
            model=config.agents.defaults.model,
            max_iterations=config.agents.defaults.max_tool_iterations,
            empty_recovery=limits_from_defaults(config.agents.defaults),
            context_window_tokens=config.agents.defaults.context_window_tokens,
            max_concurrent_subagents=config.agents.defaults.max_concurrent_subagents,
            max_subagent_spawns_per_hour=config.agents.defaults.max_subagent_spawns_per_hour,
            brave_api_key=config.tools.web.search.api_key or None,
            web_proxy=config.tools.web.proxy or None,
            media_config=config.effective_media_config(),
            exec_config=config.tools.exec,
            cron_service=cron,
            restrict_to_workspace=config.tools.restrict_to_workspace,
            session_manager=session_manager,
            mcp_servers=config.tools.mcp_servers,
            sandbox_config=config.tools.sandbox,
            channels_config=config.channels,
            skill_forge_config=skill_forge_cfg,
            runtime_config=ec_config.runtime,
            # TUI is always a multi-turn interactive session.
            interactive=True,
        )
        agent_loop.configure_personalization(
            config.agents.defaults.enable_personalization,
        )

        registered_cron_tool = agent_loop.tools.get("cron")
        if isinstance(registered_cron_tool, CronTool):
            registered_cron_tool.set_context("tui", "default")

        # cron.on_job is wired in _run_rpc_server_until_done once the spine
        # scheduler exists: a reminder runs as a CRON turn through the
        # scheduler and its reply is fanned out as a cron.delivered event.

        return agent_loop
    except (*_TUI_INIT_CRASH_TYPES, ValidationError) as e:
        from loguru import logger as _logger

        _logger.exception(
            "tui: _build_tui_agent_loop init crash ({}); surfacing as -32603 internal_error",
            type(e).__name__,
        )
        raise InternalError(
            detail=str(e),
            data={
                "reason": "tui_init_crash",
                "exception_type": type(e).__name__,
                "exception_message": str(e),
                "log_path": "~/.raven/logs/tui.log",
            },
        ) from e
    except Exception as e:
        from loguru import logger as _logger

        _logger.exception(
            "tui: _build_tui_agent_loop uncaught exception; surfacing as -32603 internal_error",
        )
        raise InternalError(
            detail=str(e),
            data={
                "reason": "uncaught",
                "exception_type": type(e).__name__,
                "exception_message": str(e),
                "log_path": "~/.raven/logs/tui.log",
            },
        ) from e


async def _run_rpc_server_until_done(
    request_fd: int,
    notify_fd: int,
    handshake_deadline_s: float,
    proc_done: asyncio.Event,
) -> bool:
    """Run RpcServer until the child exits or we abort on handshake timeout.

    Returns True if handshake succeeded (system.hello was received within the
    deadline); False if it timed out.
    """
    # Lazy import: keeps tui_commands importable without pulling tui_rpc on
    # users who never touch the TUI (e.g. CLI-only workflows).
    from raven.tui_rpc.confirm_broker import ConfirmBroker
    from raven.tui_rpc.question_broker import QuestionBroker
    from raven.tui_rpc.dispatcher import Dispatcher
    from raven.tui_rpc.methods import register_aligned_methods_except_system
    from raven.tui_rpc.methods.system import (
        system_hello as _orig_hello,
    )
    from raven.tui_rpc.methods.system import (
        system_ping,
        system_version,
    )
    from raven.tui_rpc.server import RpcServer
    from raven.tui_rpc.spine import build_tui
    from raven.tui_rpc.subscriptions import SubscriptionEmitter

    handshake_done = asyncio.Event()

    async def hello_then_signal(params: dict) -> dict:
        result = await _orig_hello(params)
        handshake_done.set()
        return result

    dispatcher = Dispatcher()
    # Server is constructed before umbrella registration so the
    # SubscriptionEmitter can bind its send_frame method as the notification
    # sink. serve_forever() is still started LAST (after all handlers are
    # registered) — RpcServer.send_frame raises until serve_forever has set
    # up the write transport, but emitter only emits after a subscribe call,
    # which can only happen post-handshake / post-serve.
    server = RpcServer(request_fd, notify_fd, dispatcher)
    emitter = SubscriptionEmitter(send_frame=server.send_frame)
    # ConfirmBroker shares the same send_frame sink; it lets a paused
    # cli.dispatch (typer.confirm) emit a confirm.request and await the
    # confirm.respond. cancel_all() in the finally fail-safes any
    # pending confirm to its default when the connection drops.
    confirm_broker = ConfirmBroker(send_frame=server.send_frame)
    # QuestionBroker shares the same send_frame sink: the ask_user tool emits a
    # clarify.request and awaits clarify.respond, mirroring ConfirmBroker.
    question_broker = QuestionBroker(send_frame=server.send_frame)

    # Wire AgentLoop for turn.send streaming (CAP-CHAT-1). _build_tui_agent_loop
    # mirrors the minimal subset of `raven agent` boilerplate needed to
    # serve chat turns from a TUI subprocess (no sentinel/cron — those are
    # the gateway's responsibility). Eager build at server bring-up so a
    # bad provider config surfaces immediately rather than on first chat.
    # An init crash is latched into ``build_error`` and re-raised by the
    # factory closure on first ``turn.send``; ``_spawn_agent_loop_task`` emits
    # the typed -32603 error event to the UI through the subscription emitter.
    from raven.tui_rpc.errors import RpcError

    agent_loop = None
    build_error: RpcError | None = None
    try:
        agent_loop = _build_tui_agent_loop()
    except RpcError as e:
        build_error = e

    # Late-bind the QuestionBroker into the ask_user tool now that the loop
    # (and its tool registry) exists; the broker itself was built up-front.
    if agent_loop is not None and (ask_tool := agent_loop.tools.get("ask_user")) is not None:
        if hasattr(ask_tool, "set_broker"):
            ask_tool.set_broker(question_broker)

    def _agent_loop_factory():
        if agent_loop is not None:
            return agent_loop
        if build_error is not None:
            raise build_error
        return None

    # Wire the spine turn path: build_tui assembles the Scheduler + delivery hub
    # + streaming sink the turn.* handlers submit onto. Only when an agent loop
    # exists — otherwise turn.send surfaces the build error / -32008 itself.
    from raven.tui_rpc.methods import turn as turn_module

    turn_scheduler = None
    turn_ids: dict[str, str] = {}
    turn_teardown = None
    if agent_loop is not None:
        from types import SimpleNamespace

        from raven.cli._cron_handler import make_on_cron_job

        # Build the spine before wiring cron: a reminder submits a CRON turn
        # through this scheduler, captured non-streaming and read back via
        # cron_readback so the wrapper can fan it out as a cron.delivered event.
        cron_readback: dict[str, str] = {}
        turn_scheduler, turn_hub, turn_ids, turn_teardown = build_tui(
            agent_loop,
            emitter,
            on_turn_end=turn_module.clear_active,
            readback_texts=cron_readback,
        )
        # Subagent result re-injection submits a SUBAGENT-origin turn.
        agent_loop.subagents.set_submit(turn_scheduler.submit)
        # Cron reminders run as CRON turns through the scheduler; the wrapper fans
        # the reply out as cron.delivered to every session. on_job must be wired
        # before cron.start() so an immediately-firing job has its callback.
        if agent_loop.cron_service is not None:
            base_on_cron = make_on_cron_job(
                agent_loop,
                turn_hub,
                submit=turn_scheduler.submit,
                readback_texts=cron_readback,
                channel_manager=SimpleNamespace(enabled_channels=["tui"]),
                default_channel="tui",
            )
            agent_loop.cron_service.on_job = _build_cron_callback_spine(base_on_cron, emitter)
            await agent_loop.cron_service.start()

    # Wrap system.hello to latch the handshake event; the umbrella below
    # registers everything else (cli.dispatch + setup.status + reload.mcp +
    # config.* + session.* + terminal.* + stubs + slash routing + turn.*).
    # Keeping production aligned with the umbrella means any future
    # register_*_methods helper added in raven/tui_rpc/methods/__init__.py
    # is picked up automatically — no more registration drift where new
    # handlers worked in the demo runner but returned -32601 in `raven tui`.
    dispatcher.register("system.hello", hello_then_signal)
    dispatcher.register("system.ping", system_ping)
    dispatcher.register("system.version", system_version)
    register_aligned_methods_except_system(
        dispatcher,
        emitter=emitter,
        agent_loop_factory=_agent_loop_factory,
        confirm_broker=confirm_broker,
        question_broker=question_broker,
        scheduler=turn_scheduler,
        turn_ids=turn_ids,
        build_error=build_error,
    )

    serve_task = asyncio.create_task(server.serve_forever())

    try:
        # Wait until EITHER handshake completes OR deadline expires OR child exits.
        done, pending = await asyncio.wait(
            {
                asyncio.create_task(handshake_done.wait()),
                asyncio.create_task(proc_done.wait()),
            },
            timeout=handshake_deadline_s,
            return_when=asyncio.FIRST_COMPLETED,
        )
        for t in pending:
            t.cancel()
        # Drain cancelled tasks to suppress warnings.
        for t in pending:
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass

        if not handshake_done.is_set():
            return False
        # Handshake OK — continue serving until child exits.
        await proc_done.wait()
        return True
    finally:
        # Fail-safe any pending confirm so a paused dispatch's worker thread
        # is released when the connection drops.
        confirm_broker.cancel_all()
        if agent_loop is not None and agent_loop.cron_service is not None:
            try:
                agent_loop.cron_service.stop()
            except Exception:
                pass
        if turn_teardown is not None:
            try:
                await turn_teardown()
            except Exception:
                pass
        serve_task.cancel()
        try:
            await serve_task
        except (asyncio.CancelledError, Exception):
            pass


def _create_rpc_socket_dir() -> Path:
    """Create a 0700-mode tempdir to hold a session-private rpc socket.

    Each call mints a fresh directory under ``$TMPDIR`` (mkdtemp prefixes
    with ``eve-rpc-``). The 0700 perms ensure no other local user can even
    enumerate the socket path. Caller is responsible for removing the dir
    on teardown.
    """
    dir_path = Path(tempfile.mkdtemp(prefix=_RPC_SOCKET_DIR_PREFIX))
    # mkdtemp already creates with 0700 on POSIX; chmod is defensive.
    try:
        os.chmod(dir_path, 0o700)
    except OSError:
        pass
    return dir_path


def _spawn_with_rpc_socket(
    node_path: str,
    args: list[str],
    cwd: Path,
) -> tuple[subprocess.Popen[bytes], socket.socket, Path]:
    """Spawn `[node_path, *args]` with a per-session unix domain socket.

    Topology:

        parent: socket()/bind()/listen() on `<tmpdir>/sock` (mode 0700/0600)
                exports the path as ``RAVEN_RPC_SOCKET`` env var
        child:  reads ``RAVEN_RPC_SOCKET``, ``net.createConnection(path)``,
                emits JSON-RPC frames + reads responses on the same socket

    Returns ``(popen, listening_server_socket, sock_dir_path)``. Caller must
    eventually:

        * ``server_sock.close()``
        * ``shutil.rmtree(sock_dir_path, ignore_errors=True)``

    The accepted client connection is NOT created here — that's done by
    :func:`_run_rpc_server_until_done` once the asyncio loop is up so the
    accept can be cancelled cleanly on handshake timeout.
    """
    sock_dir = _create_rpc_socket_dir()
    sock_path = sock_dir / "sock"

    server_sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server_sock.bind(str(sock_path))
    server_sock.listen(1)
    # 0600 on the socket file — only the owning user can connect.
    try:
        os.chmod(sock_path, 0o600)
    except OSError:
        pass

    env = os.environ.copy()
    env[_RPC_SOCKET_ENV] = str(sock_path)

    proc = subprocess.Popen(
        [node_path, *args],
        cwd=str(cwd),
        stdin=None,
        stdout=None,
        stderr=None,
        env=env,
    )

    return proc, server_sock, sock_dir


async def _accept_with_timeout(
    server_sock: socket.socket,
    timeout_s: float,
) -> socket.socket | None:
    """Accept one connection on `server_sock` or return None on timeout.

    Uses ``loop.sock_accept`` so the wait is cooperatively cancellable.
    """
    server_sock.setblocking(False)
    loop = asyncio.get_running_loop()
    try:
        conn, _addr = await asyncio.wait_for(loop.sock_accept(server_sock), timeout=timeout_s)
    except asyncio.TimeoutError:
        return None
    conn.setblocking(False)
    return conn


def run_subprocess_with_rpc(
    node_path: str,
    args: list[str],
    cwd: Path,
    forward_signals: bool = True,
) -> int:
    """Spawn Node child with a per-session unix socket; run RpcServer; enforce handshake.

    Q11 (2026-05-14): production transport is a unix domain socket. The Node
    side cannot reliably wrap inherited pipe FDs as Node streams, so the
    parent listens on ``<tmpdir>/sock`` (mode 0700/0600) and exports the
    path via ``RAVEN_RPC_SOCKET``. The wire framing (newline JSON) is
    unchanged from the pass_fds variant; ``RpcServer`` itself is reused
    verbatim by dup-ing the accepted socket fd into separate read / write
    ends so ``connect_read_pipe`` + ``connect_write_pipe`` still apply.

    Returns the child's exit code, OR ``_RPC_HANDSHAKE_EXIT_CODE`` (3) if
    the handshake times out (either because the child never connected, or
    because it connected but never sent ``system.hello``).
    """
    proc, server_sock, sock_dir = _spawn_with_rpc_socket(node_path, args, cwd)

    if forward_signals:

        def _forward(sig, _frame):
            try:
                proc.send_signal(sig)
            except ProcessLookupError:
                pass

        signal.signal(signal.SIGINT, _forward)
        signal.signal(signal.SIGTERM, _forward)
        if hasattr(signal, "SIGHUP"):
            signal.signal(signal.SIGHUP, _forward)

    proc_done = asyncio.Event()

    def _waiter() -> None:
        try:
            proc.wait()
        finally:
            try:
                loop = _loop_holder.get("loop")
                if loop is not None and not loop.is_closed():
                    loop.call_soon_threadsafe(proc_done.set)
            except RuntimeError:
                pass

    _loop_holder: dict[str, asyncio.AbstractEventLoop] = {}

    # Track FDs we allocate inside _main so the outer cleanup can close them.
    _fd_holder: dict[str, int] = {}
    _conn_holder: dict[str, socket.socket] = {}

    async def _main() -> bool:
        _loop_holder["loop"] = asyncio.get_running_loop()

        # Wait for child to connect within the handshake deadline. We race
        # `accept` against `proc.wait()` so an early-exiting child returns
        # immediately instead of stalling for the full 5 s.
        accept_task = asyncio.create_task(
            _accept_with_timeout(server_sock, _RPC_HANDSHAKE_TIMEOUT_S)
        )
        proc_done_task = asyncio.create_task(proc_done.wait())
        done, pending = await asyncio.wait(
            {accept_task, proc_done_task},
            return_when=asyncio.FIRST_COMPLETED,
            timeout=_RPC_HANDSHAKE_TIMEOUT_S,
        )
        for t in pending:
            t.cancel()
        for t in pending:
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass

        if accept_task not in done:
            return False
        conn = accept_task.result()
        if conn is None:
            return False
        _conn_holder["conn"] = conn

        # dup once for read, once for write — both ends of the same socket fd
        # — so RpcServer's connect_read_pipe + connect_write_pipe (each of
        # which takes ownership of its fd) cannot trigger a double-close.
        req_fd = os.dup(conn.fileno())
        notif_fd = os.dup(conn.fileno())
        _fd_holder["req"] = req_fd
        _fd_holder["notif"] = notif_fd

        # We've duped what we need; the original `conn` can be closed by the
        # outer scope.
        return await _run_rpc_server_until_done(
            req_fd, notif_fd, _RPC_HANDSHAKE_TIMEOUT_S, proc_done
        )

    waiter = threading.Thread(target=_waiter, daemon=True)
    waiter.start()

    handshake_ok = False
    try:
        handshake_ok = asyncio.run(_main())
    finally:
        # 1) Close the parent-side conn (server already dup'd what it needs).
        if "conn" in _conn_holder:
            try:
                _conn_holder["conn"].close()
            except OSError:
                pass
        # 2) Close the listening socket.
        try:
            server_sock.close()
        except OSError:
            pass
        # 3) Remove the tempdir + socket file.
        try:
            shutil.rmtree(sock_dir, ignore_errors=True)
        except OSError:
            pass

    if not handshake_ok:
        print(
            "✗ RPC handshake timeout "
            f"({_RPC_HANDSHAKE_TIMEOUT_S:.0f}s); is the Node side using the new IPC bridge?",
            file=sys.stderr,
        )
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            pass
        return _RPC_HANDSHAKE_EXIT_CODE

    waiter.join(timeout=5)
    return proc.returncode if proc.returncode is not None else 0


def _print_node_help(out=None) -> None:
    """Print the friendly Node-missing error message."""
    msg = (
        "✗ TUI 启动失败：未找到 Node.js ≥ 22。\n"
        "  安装：https://nodejs.org/  或  brew install node@22  或  nvm install 22\n"
        "  或：临时使用行式 REPL  ->  raven agent --legacy-repl\n"
    )
    typer.echo(msg, file=out)


def _diagnose_crash(node_path: str, dist_entry: Path) -> None:
    """When `tui` child exits non-zero, re-run capturing stderr for diagnosis."""
    try:
        proc = subprocess.run(
            [node_path, str(dist_entry)],
            cwd=str(_UI_TUI_DIR),
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        stderr_tail = "\n".join(proc.stderr.splitlines()[-20:])
        if stderr_tail:
            print(
                f"\n--- child stderr (last 20 lines) ---\n{stderr_tail}\n",
                file=sys.stderr,
            )
    except (subprocess.SubprocessError, OSError):
        # Diagnose failure is best-effort; never raise.
        pass


@tui_app.callback(invoke_without_command=True)
def tui(
    ctx: typer.Context,
    check: bool = typer.Option(
        False,
        "--check",
        help="Smoke test: boot child then exit (no interactive TTY required).",
    ),
    dev: bool = typer.Option(
        False,
        "--dev",
        help="Dev mode: run from TS source via tsx (no build step) instead of compiled dist.",
    ),
    color: Optional[str] = typer.Option(
        None,
        "--color",
        help="Force color output: auto | truecolor | 256 | 16 | none.",
    ),
    print_colors: bool = typer.Option(
        False,
        "--print-colors",
        help="Print the resolved color palette as swatches and exit (no TTY needed).",
    ),
    preview_colors: bool = typer.Option(
        False,
        "--preview-colors",
        help="Preview color tokens in their real UI contexts and exit (no TTY needed).",
    ),
) -> None:
    """Launch Raven native TUI."""
    if ctx.invoked_subcommand is not None:
        return

    # Startup gate: launch the onboarding wizard first when the required
    # config (a provider key + default model) is missing. Skipped for the
    # no-TTY diagnostic spawns (--check / --print-colors / --preview-colors).
    if not (check or print_colors or preview_colors) and _stdout_isatty():
        from raven.cli.onboard_commands import (
            ensure_configured_or_onboard,
            _is_config_populated,
        )

        if not _is_config_populated():
            ensure_configured_or_onboard()

    node_path, version = find_node()
    if node_path is None:
        _print_node_help()
        raise typer.Exit(code=1)
    if version is None or version < _MIN_NODE_VERSION:
        ver_str = ".".join(map(str, version)) if version else "<unknown>"
        typer.echo(
            f"✗ Node 版本过低（找到 {ver_str}，需要 >= 22）。\n"
            f"  请升级：nvm install 22  或  brew upgrade node\n",
        )
        raise typer.Exit(code=1)

    if not _UI_TUI_DIR.exists():
        print(f"✗ TUI 资源缺失：{_UI_TUI_DIR}", file=sys.stderr)
        raise typer.Exit(code=2)

    # Color override flows to the child via env (entry.tsx -> colorTier.ts).
    # Only set it when --color was passed so a shell-level RAVEN_TUI_COLOR
    # isn't clobbered by the "auto" default.
    if color is not None:
        os.environ["RAVEN_TUI_COLOR"] = color

    # `--check` is a smoke test: tell the child to boot, prove stub init,
    # then exit 0 (no Ink render, no interactive TTY). The child reads
    # RAVEN_TUI_CHECK from the environment it inherits from this process.
    # See ui-tui/src/entry.tsx for the matching handler.
    if check:
        os.environ["RAVEN_TUI_CHECK"] = "1"

    # `--print-colors` / `--preview-colors` are no-IPC diagnostics: the child
    # dumps the resolved palette (swatches / in-context) and exits. Like
    # --check they skip the RPC handshake.
    if print_colors:
        os.environ["RAVEN_TUI_PRINT_COLORS"] = "1"
    if preview_colors:
        os.environ["RAVEN_TUI_COLOR_PREVIEW"] = "1"

    # --check / --print-colors / --preview-colors are no-RPC, stdio-only spawns.
    no_rpc = check or print_colors or preview_colors

    # Redirect parent loguru to a file so RpcServer / cli.dispatch / etc.
    # logs don't corrupt the Ink reconciler. (Skipped for the no-RPC paths
    # which exit before Ink renders.)
    if not no_rpc:
        _suppress_noisy_watchers()
        log_path = redirect_loguru_to_file(
            "tui.log",
            retention=3,
            record_filter=_drop_watcher_spam,
        )
        typer.echo(f"📝 TUI logs → {log_path}", err=True)

    if dev:
        # tsx watch via local node_modules.
        # Derive npx from the validated node_path so RAVEN_NODE's
        # version-pin semantics are honored end-to-end. Only fall back to
        # PATH when the derived path is absent (rare; e.g. operator points
        # RAVEN_NODE at a standalone node binary with no sibling npx).
        derived_npx = Path(node_path).parent / "npx"
        if derived_npx.exists():
            npx = str(derived_npx)
        else:
            fallback = shutil.which("npx")
            if fallback is not None and os.environ.get("RAVEN_NODE"):
                typer.echo(
                    f"⚠ RAVEN_NODE was set but no `npx` next to {node_path};\n"
                    f"  falling back to PATH npx at {fallback}. Node version\n"
                    f"  used by tsx may differ from the validated one.\n",
                    err=True,
                )
            npx = fallback or "npx"
        # Use npx to run tsx (source mode, no build step); npx ships with
        # node >= 22. `--watch` is intentionally dropped: the interactive path
        # requires a one-shot RPC handshake (parent accepts a
        # single socket connection), and a watch-triggered restart would drop
        # that connection. --check stays on the plain spawn because entry.tsx
        # short-circuits on RAVEN_TUI_CHECK before the socket guard, so it
        # needs no RPC server; the interactive path must open the socket or
        # entry.tsx exits 2 ("RAVEN_RPC_SOCKET env var required").
        tsx_args = ["tsx", "src/entry.tsx"]
        if no_rpc:
            exit_code = run_subprocess(npx, tsx_args, cwd=_UI_TUI_DIR)
        else:
            exit_code = run_subprocess_with_rpc(npx, tsx_args, cwd=_UI_TUI_DIR)
    else:
        dist_entry = _UI_TUI_DIR / "dist" / "entry.js"
        if not dist_entry.exists() and not check:
            print(
                f"✗ TUI 构建产物缺失：{dist_entry}\n"
                f"  请先运行：cd {_UI_TUI_DIR} && npm install && npm run build\n",
                file=sys.stderr,
            )
            raise typer.Exit(code=2)
        # `--check` smoke path keeps the simple stdio-only spawn so the
        # bootstrap-era tests (which don't speak JSON-RPC) still pass; the
        # interactive run path opens the RPC pipes and enforces handshake.
        if no_rpc:
            exit_code = run_subprocess(node_path, [str(dist_entry)], cwd=_UI_TUI_DIR)
        else:
            exit_code = run_subprocess_with_rpc(node_path, [str(dist_entry)], cwd=_UI_TUI_DIR)
        if exit_code != 0 and exit_code != 130 and exit_code != _RPC_HANDSHAKE_EXIT_CODE:
            _diagnose_crash(node_path, dist_entry)

    if check:
        # --check passes when Node was found and child process spawned,
        # regardless of child's exit code (the child may exit early on
        # non-TTY stdin, which is the expected smoke path in CI).
        raise typer.Exit(code=0)

    raise typer.Exit(code=exit_code)
