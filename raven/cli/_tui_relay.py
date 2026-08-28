"""Attach ``raven tui`` to a gateway that already hosts the page.

When ``~/.raven/serve.json`` names a live ``raven gateway`` (the recorded pid
holds the gateway lock) and its ``/health`` answers as ``raven-serve``, the TUI
parent does not build a second AgentLoop. It becomes a relay: the Node client
keeps its existing transport (newline-JSON over the loopback TCP socket the
parent binds, token as the first line), and every frame is forwarded to the
gateway's ``/rpc`` WebSocket — both sides speak the same dialect-A JSON-RPC,
so frames pass through verbatim.

Two frames are the exceptions, and both stay inside the declared contract:

* ``session.create`` gains a ``workdir`` param naming this terminal's launch
  directory. The stock client cannot say where it was launched, and without
  it the gateway's own workdir policy would run this terminal's turns in the
  gateway's directories.
* ``system.hello`` gains ``surface: "tui"`` (unless the client set one), so
  the gateway records this connection as a terminal and its turns' traces say
  so -- the page and the shell connect to the same gateway under their own
  names. The response is also watched so the relay can latch readiness on it,
  keeping the spinner and the 5s handshake deadline exactly as the embedded
  path has them.

Everything the embedded path owns — the engine, cron, the memory backend —
is the gateway's in this mode; the relay starts none of it.
"""

from __future__ import annotations

import asyncio
import json
import signal
import socket
import subprocess
import sys
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from loguru import logger

from raven.utils import asyncio_runner as bounded_asyncio

_HEALTH_TIMEOUT_S = 2.0
_WS_CONNECT_TIMEOUT_S = 3.0
_TOKEN_LINE_TIMEOUT_S = 10.0
_CHILD_EXIT_AFTER_DROP_S = 10.0


@dataclass(frozen=True)
class AttachPlan:
    """A gateway worth attaching to, and the directory this terminal works in."""

    port: int
    token: str
    workdir: Path


@dataclass
class _RelayState:
    """What the relay remembers from the frames passing through it."""

    hello_id: Any = None
    subscription_ids: set[str] = field(default_factory=set)
    relay_failed: bool = False


# Why the client-to-gateway pump stopped, and what the client is told about it.
# ``_CLIENT_EOF`` is the benign one (the child closed its socket on its way out);
# each other reason ends the session the way the embedded server ends it on the
# same read -- error event on every live stream, then EOF, then a nonzero exit.
_CLIENT_EOF = "client_eof"
_FAILURE_TEXT = {
    "client_frame_too_large": ("client frame over the frame limit", "client_frame_too_large"),
    "client_socket_error": ("client connection error", "client_socket_error"),
    "gateway_send_failed": ("gateway connection lost", "gateway_disconnected"),
    "client_pump_failed": ("relay stopped on an internal error", "relay_failed"),
    "gateway_disconnected": ("gateway connection lost", "gateway_disconnected"),
}


def plan_attach(workspace: str | None = None) -> AttachPlan | None:
    """The gateway this launch should relay to, or None to run embedded.

    Every check falls back to None rather than raising: the embedded loop is
    always a correct answer, and a launch must never fail because a state
    file was stale. Attaching requires, in order:

    1. ``tui.attach_gateway`` not switched off in config;
    2. a page-hosting gateway on record (serve.json pid == gateway lock pid,
       the same match ``raven serve`` uses before it declines to start);
    3. a launch directory the contract can express — ``validate_override``
       must accept it as a session workdir, or the attached engine could not
       honor where this terminal was started;
    4. a live ``/health`` naming ``raven-serve``.
    """
    try:
        from raven.config.loader import load_config

        config = load_config()
        if not config.tui.attach_gateway:
            return None

        from raven.cli.serve_commands import _gateway_hosted_page

        hosted = _gateway_hosted_page()
        if hosted is None:
            return None
        port, token = hosted

        from raven.agent.workdir import validate_override

        try:
            workdir = validate_override(workspace or Path.cwd(), config.workspace_path)
        except ValueError as exc:
            logger.info("tui: not attaching to the gateway ({}); running embedded", exc)
            return None

        if not asyncio.run(_healthy(port)):
            return None
        return AttachPlan(port=port, token=token, workdir=workdir)
    except Exception:
        logger.exception("tui: gateway discovery failed; running embedded")
        return None


async def _healthy(port: int) -> bool:
    import aiohttp

    timeout = aiohttp.ClientTimeout(total=_HEALTH_TIMEOUT_S)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(f"http://127.0.0.1:{port}/health") as resp:
                return resp.status == 200 and (await resp.json()).get("service") == "raven-serve"
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
        return False


def _rewrite_outbound(text: str, workdir: Path, state: _RelayState) -> str:
    """One client frame on its way to the gateway; almost always verbatim."""
    try:
        frame = json.loads(text)
    except json.JSONDecodeError:
        return text
    if not isinstance(frame, dict):
        return text
    method = frame.get("method")
    if method == "system.hello":
        if frame.get("id") is not None:
            state.hello_id = frame["id"]
        from raven.cli.tui_commands import TERMINAL_SURFACE

        params = frame.get("params")
        if not isinstance(params, dict):
            params = {}
        # The stock client predates the param; the relay knows what it is
        # fronting. setdefault so a future client that declares wins.
        params.setdefault("surface", TERMINAL_SURFACE)
        frame["params"] = params
        return json.dumps(frame, ensure_ascii=False)
    elif method == "session.create":
        params = frame.get("params")
        if not isinstance(params, dict):
            params = {}
        params["workdir"] = str(workdir)
        frame["params"] = params
        return json.dumps(frame, ensure_ascii=False)
    elif isinstance(method, str) and method.endswith(".unsubscribe"):
        params = frame.get("params")
        if isinstance(params, dict) and isinstance(params.get("subscription_id"), str):
            state.subscription_ids.discard(params["subscription_id"])
    return text


def _observe_inbound(text: str, state: _RelayState, handshake_done: asyncio.Event) -> None:
    """One gateway frame on its way to the client; forwarded untouched, but the
    hello response latches the handshake and subscription grants are remembered
    so a dead gateway can be reported on every live stream."""
    try:
        frame = json.loads(text)
    except json.JSONDecodeError:
        return
    if not isinstance(frame, dict):
        return
    if state.hello_id is not None and frame.get("id") == state.hello_id and "result" in frame:
        handshake_done.set()
    result = frame.get("result")
    if isinstance(result, dict) and isinstance(result.get("subscription_id"), str):
        state.subscription_ids.add(result["subscription_id"])


async def _pump_client_to_gateway(reader: asyncio.StreamReader, ws, workdir: Path, state: _RelayState) -> str:
    """Forward client frames to the gateway; returns why it stopped.

    The reason matters to the caller: only ``_CLIENT_EOF`` is a peer on its way
    out. Every other exit leaves the client talking into a pump that is gone --
    an overrun in particular leaves the oversized bytes in the reader's buffer,
    so the stream cannot be resynchronised even if the client retries.
    """
    while True:
        try:
            line = await reader.readuntil(b"\n")
        except asyncio.IncompleteReadError:
            return _CLIENT_EOF
        except asyncio.LimitOverrunError:
            from raven.rpc.server import MAX_FRAME_BYTES

            logger.error("tui relay: client frame exceeds the {} byte limit", MAX_FRAME_BYTES)
            return "client_frame_too_large"
        except ConnectionError:
            return "client_socket_error"
        text = line.decode("utf-8", errors="replace").strip()
        if not text:
            continue
        try:
            await ws.send_str(_rewrite_outbound(text, workdir, state))
        except (ConnectionError, RuntimeError, ValueError):
            return "gateway_send_failed"


async def _pump_gateway_to_client(
    ws,
    writer: asyncio.StreamWriter,
    state: _RelayState,
    handshake_done: asyncio.Event,
) -> None:
    from aiohttp import WSMsgType

    async for msg in ws:
        if msg.type != WSMsgType.TEXT:
            continue
        writer.write(msg.data.encode("utf-8") + b"\n")
        try:
            await writer.drain()
        except ConnectionError:
            return
        _observe_inbound(msg.data, state, handshake_done)


def _pump_stop_cause(task: asyncio.Task) -> str:
    """Why ``_pump_client_to_gateway`` ended, with a crash read as a failure."""
    if task.exception() is not None:
        logger.error("tui relay: client pump raised: {}", task.exception())
        return "client_pump_failed"
    return str(task.result())


async def _surface_failure(writer: asyncio.StreamWriter, state: _RelayState, cause: str) -> None:
    """Tell every live stream the relay is done, in the error shape the
    client already renders, before the transport closes under it."""
    message, reason = _FAILURE_TEXT.get(cause, _FAILURE_TEXT["client_pump_failed"])
    event = {"type": "error", "payload": {"code": -32603, "message": message, "reason": reason}}
    for sub_id in sorted(state.subscription_ids):
        frame = {"jsonrpc": "2.0", "method": "event", "params": {"subscription_id": sub_id, "event": event}}
        writer.write((json.dumps(frame, ensure_ascii=False) + "\n").encode("utf-8"))
    try:
        await writer.drain()
    except ConnectionError:
        pass


async def _relay_until_done(
    conn: socket.socket,
    auth_token: str,
    ws,
    workdir: Path,
    handshake_deadline_s: float,
    proc_done: asyncio.Event,
) -> tuple[bool, bool]:
    """Pump frames both ways until the child exits or a pump dies.

    Returns ``(handshake_ok, relay_failed)``. Owns ``conn`` from here on
    (the stream pair closes it).
    """
    from raven.rpc.server import MAX_FRAME_BYTES

    conn.setblocking(False)
    reader, writer = await asyncio.open_connection(sock=conn, limit=MAX_FRAME_BYTES)

    try:
        # The same trust gate RpcServer applies: the loopback port is reachable
        # by any local process, so the child proves itself with the shared
        # secret before a single frame is relayed.
        try:
            first = await asyncio.wait_for(reader.readuntil(b"\n"), timeout=_TOKEN_LINE_TIMEOUT_S)
        except (asyncio.TimeoutError, asyncio.IncompleteReadError, asyncio.LimitOverrunError):
            logger.error("tui relay: auth token not received; closing")
            return False, False
        if first.rstrip(b"\n") != auth_token.encode("utf-8"):
            logger.error("tui relay: auth token mismatch; closing")
            return False, False

        handshake_done = asyncio.Event()
        state = _RelayState()
        to_gateway = asyncio.create_task(_pump_client_to_gateway(reader, ws, workdir, state))
        to_client = asyncio.create_task(_pump_gateway_to_client(ws, writer, state, handshake_done))
        proc_task = asyncio.create_task(proc_done.wait())
        hs_task = asyncio.create_task(handshake_done.wait())

        try:
            await asyncio.wait(
                {hs_task, proc_task, to_client, to_gateway},
                timeout=handshake_deadline_s,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if not handshake_done.is_set():
                return False, False

            # Both pumps are watched, not just the gateway-facing one: the
            # client-facing pump can die on its own (a frame over the limit,
            # a socket error) while the child and the WebSocket both live, and
            # an unwatched death there is exactly the silent hang this relay
            # promises never to produce.
            done, _ = await asyncio.wait({proc_task, to_client, to_gateway}, return_when=asyncio.FIRST_COMPLETED)
            cause: str | None = None
            if not proc_done.is_set():
                if to_client in done:
                    cause = "gateway_disconnected"
                elif to_gateway in done:
                    cause = _pump_stop_cause(to_gateway)
            if cause == _CLIENT_EOF:
                # The child closed its socket while still running: the embedded
                # server closes the connection on the same read, so the client
                # meets the EOF it is already written to handle. Not a failure --
                # the launch keeps the child's own exit code.
                writer.close()
            elif cause is not None:
                # Fail visibly. Every live stream gets the error event, then EOF
                # on the client socket ends the UI rather than leaving it hung.
                state.relay_failed = True
                logger.error("tui relay: {}; surfacing to the client and shutting down", cause)
                await _surface_failure(writer, state, cause)
                writer.close()
                try:
                    await asyncio.wait_for(proc_done.wait(), timeout=_CHILD_EXIT_AFTER_DROP_S)
                except asyncio.TimeoutError:
                    pass
            return True, state.relay_failed
        finally:
            for task in (to_gateway, to_client, proc_task, hs_task):
                task.cancel()
            for task in (to_gateway, to_client, proc_task, hs_task):
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except (ConnectionError, OSError):
            pass


def run_subprocess_attached(
    node_path: str,
    args: list[str],
    cwd: Path,
    plan: AttachPlan,
    forward_signals: bool = True,
    workspace: str | None = None,
) -> int:
    """Spawn the Node child exactly as the embedded path does, but relay its
    frames to the gateway named by ``plan`` instead of serving them locally.

    The child-facing half is identical to ``run_subprocess_with_rpc`` — same
    ``RAVEN_RPC_SOCKET`` / ``RAVEN_RPC_TOKEN`` env handshake, same 5s deadline,
    same exit-3 on a failed handshake — so the Node client cannot tell the
    difference. If the gateway is gone by the time the WebSocket connects
    (it answered ``/health`` moments ago), the accepted socket is untouched
    and the embedded server takes over on it, so the launch still succeeds.

    A relay that cannot carry frames any more -- the gateway dying mid-session,
    or a client frame over the frame limit -- is surfaced (error event per live
    subscription, then EOF) and the launch exits nonzero. Closing the TUI
    only closes the WebSocket; the gateway is never signalled.
    """
    from raven.cli.tui_commands import (
        _RPC_HANDSHAKE_EXIT_CODE,
        _RPC_HANDSHAKE_TIMEOUT_S,
        _accept_with_timeout,
        _run_rpc_server_until_done,
        _spawn_with_rpc_socket,
    )

    proc, server_sock, auth_token = _spawn_with_rpc_socket(node_path, args, cwd)

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
    _loop_holder: dict[str, asyncio.AbstractEventLoop] = {}

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

    async def _main() -> tuple[bool, bool]:
        import aiohttp

        _loop_holder["loop"] = asyncio.get_running_loop()

        accept_task = asyncio.create_task(_accept_with_timeout(server_sock, _RPC_HANDSHAKE_TIMEOUT_S))
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
            return False, False
        conn = accept_task.result()
        if conn is None:
            return False, False

        # No session-level total timeout: it would count the socket's whole
        # lifetime, and this socket lives as long as the TUI does. The connect
        # itself is bounded by the wait_for below instead.
        session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=None))
        try:
            ws = await asyncio.wait_for(
                session.ws_connect(
                    f"http://127.0.0.1:{plan.port}/rpc",
                    headers={"X-Raven-Token": plan.token},
                    heartbeat=30,
                ),
                timeout=_WS_CONNECT_TIMEOUT_S,
            )
        except (aiohttp.ClientError, asyncio.TimeoutError):
            # The gateway answered /health at discovery and is gone now. The
            # accepted socket has not been read, so the embedded server can
            # still own it — the launch degrades instead of failing.
            await session.close()
            logger.warning("tui relay: gateway vanished before attach; falling back to the embedded engine")
            ok = await _run_rpc_server_until_done(
                conn, auth_token, _RPC_HANDSHAKE_TIMEOUT_S, proc_done, workspace=workspace
            )
            return ok, False

        try:
            return await _relay_until_done(conn, auth_token, ws, plan.workdir, _RPC_HANDSHAKE_TIMEOUT_S, proc_done)
        finally:
            try:
                await ws.close()
            except Exception:
                pass
            await session.close()

    waiter = threading.Thread(target=_waiter, daemon=True)
    waiter.start()

    handshake_ok = False
    relay_failed = False
    try:
        handshake_ok, relay_failed = bounded_asyncio.run(_main())
    finally:
        try:
            server_sock.close()
        except OSError:
            pass

    if not handshake_ok:
        print(
            f"✗ RPC handshake timeout ({_RPC_HANDSHAKE_TIMEOUT_S:.0f}s); is the Node side using the new IPC bridge?",
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
    exit_code = proc.returncode if proc.returncode is not None else 0
    if relay_failed and exit_code == 0:
        # The session lost the engine under it; a clean child exit must not
        # report the launch as fine.
        return 1
    return exit_code


__all__ = ["AttachPlan", "plan_attach", "run_subprocess_attached"]
