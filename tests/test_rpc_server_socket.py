"""Regression: RpcServer must speak proper JSON-RPC over a unix domain socket.

P0 dogfood blocker (2026-05-15) — typing ``/cha`` in ``raven tui`` produced
repeating ``pipe closed by peer or os.write(pipe, data) raised exception.``
warnings (asyncio.unix_events) that bled into the Ink render. Root cause:
``connect_write_pipe`` builds a ``_UnixWritePipeTransport`` that registers a
reader callback on the WRITE fd to detect peer EOF; on a bidirectional
SOCK_STREAM socket the very first inbound byte trips that callback and the
write side silently closes. Every subsequent RPC response is dropped, and
after 5 dropped writes asyncio starts logging the "pipe closed" warning.

The production transport in ``tui_commands.run_subprocess_with_rpc`` is a
unix socket whose fd is dup'd into ``request_fd`` and ``notify_fd``, so this
bug was triggered on every interactive session. The fix is to detect the
socket fds and use ``connect_accepted_socket`` (full-duplex selector
transport) instead of the pipe pair.

These tests exercise the bug directly without spawning Node:

1. ``test_socket_roundtrip_after_inbound_byte`` — sends a request, reads the
   response. Pre-fix this hangs because the write transport closes on the
   first inbound byte and the response never reaches the client.
2. ``test_socket_no_asyncio_pipe_warnings`` — sends three requests in
   sequence and asserts the asyncio.unix_events logger emits no
   ``"pipe closed"`` warning.
"""

from __future__ import annotations

import asyncio
import json
import logging
import socket
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest

from raven.rpc.dispatcher import Dispatcher
from raven.rpc.methods import register_aligned_methods
from raven.rpc.server import RpcServer

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _wire_paired_socket() -> tuple[socket.socket, socket.socket, socket.socket, Path]:
    """Build the same fd topology that ``run_subprocess_with_rpc`` builds.

    Returns ``(listening_sock, client_sock, server_conn, tmp_dir)``; the caller
    hands ``server_conn`` to ``RpcServer`` the way the production path does.
    """
    tmp = Path(tempfile.mkdtemp(prefix="eve-test-rpc-"))
    spath = tmp / "sock"

    server_sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server_sock.bind(str(spath))
    server_sock.listen(1)
    server_sock.setblocking(False)

    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.setblocking(False)

    loop = asyncio.get_running_loop()
    await loop.sock_connect(client, str(spath))
    conn, _ = await loop.sock_accept(server_sock)
    conn.setblocking(False)
    return server_sock, client, conn, tmp


async def _read_one_frame(client: socket.socket, *, timeout: float = 2.0) -> bytes:
    """Read a single newline-terminated frame from ``client``."""
    loop = asyncio.get_running_loop()
    buf = bytearray()
    deadline = loop.time() + timeout
    while not buf.endswith(b"\n"):
        remaining = deadline - loop.time()
        if remaining <= 0:
            break
        try:
            chunk = await asyncio.wait_for(loop.sock_recv(client, 65536), timeout=remaining)
        except asyncio.TimeoutError:
            break
        if not chunk:
            break
        buf.extend(chunk)
    return bytes(buf)


async def _send(client: socket.socket, method: str, params: dict, rid: int) -> None:
    loop = asyncio.get_running_loop()
    frame = json.dumps({"jsonrpc": "2.0", "id": rid, "method": method, "params": params}) + "\n"
    await loop.sock_sendall(client, frame.encode())


@pytest.fixture()
def _capture_asyncio_warnings() -> Iterator[list[str]]:
    """Capture WARNING records emitted by the ``asyncio`` logger."""
    captured: list[str] = []

    class _H(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            captured.append(record.getMessage())

    handler = _H(level=logging.WARNING)
    log = logging.getLogger("asyncio")
    log.addHandler(handler)
    prev_level = log.level
    log.setLevel(logging.WARNING)
    try:
        yield captured
    finally:
        log.removeHandler(handler)
        log.setLevel(prev_level)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_send_frame_waits_for_a_peer_that_stopped_reading() -> None:
    """A client that stops reading must stall the emitter, not fill a buffer.

    The TUI stops reading whenever its event loop is busy. Without flow control
    ``send_frame`` returns as fast as the runtime can produce frames, the
    backlog accumulates in this process, and the whole batch lands on the
    client at once when it recovers -- which is how a slow client becomes a
    stuck one. Here the peer never reads, so sends must stop; once it drains,
    they must resume.
    """
    server_sock, client, conn, tmp = await _wire_paired_socket()
    try:
        disp = Dispatcher()
        register_aligned_methods(disp)
        server = RpcServer(disp, sock=conn)
        serve_task = asyncio.create_task(server.serve_forever())
        await server.started.wait()

        sent = 0

        async def pump() -> None:
            nonlocal sent
            payload = "x" * 4_000
            # Bounded so a regression fails on the assertions below instead of
            # buffering until the run is killed -- which is what losing the
            # wait actually does.
            while sent < 5_000:
                await server.send_frame({"jsonrpc": "2.0", "method": "event", "params": {"t": payload}})
                sent += 1

        pump_task = asyncio.create_task(pump())
        try:
            # Asserted as "stops moving", not as a frame count: how much the
            # socket and the transport absorb before the high-water mark is
            # platform-dependent, but with no peer reading it must settle.
            await asyncio.sleep(0.3)
            stalled_at = sent
            assert stalled_at > 0, "nothing was written at all"

            await asyncio.sleep(0.2)
            assert sent == stalled_at, f"emitter kept writing to an unread peer ({stalled_at} -> {sent})"

            # Drain the peer; the emitter must pick up again.
            loop = asyncio.get_running_loop()
            for _ in range(200):
                if sent > stalled_at:
                    break
                try:
                    await asyncio.wait_for(loop.sock_recv(client, 1 << 20), timeout=0.1)
                except asyncio.TimeoutError:
                    pass
            assert sent > stalled_at, "emitter did not resume once the peer drained"
        finally:
            pump_task.cancel()
            try:
                await pump_task
            except asyncio.CancelledError:
                pass

        serve_task.cancel()
        try:
            await serve_task
        except asyncio.CancelledError:
            pass
    finally:
        client.close()
        conn.close()
        server_sock.close()


@pytest.mark.asyncio
async def test_socket_roundtrip_after_inbound_byte() -> None:
    """Hello + slash-style requests round-trip with responses delivered.

    Pre-fix this hangs: the write transport closed on first inbound byte and
    no response ever reached the client. The 2 s per-frame timeout in
    ``_read_one_frame`` would expire and the assertion would fail.
    """
    server_sock, client, conn, tmp = await _wire_paired_socket()
    try:
        disp = Dispatcher()
        register_aligned_methods(disp)
        server = RpcServer(disp, sock=conn)
        serve_task = asyncio.create_task(server.serve_forever())
        await server.started.wait()

        # 1) handshake
        await _send(client, "system.hello", {"client_version": "0.0.2"}, 1)
        hello = await _read_one_frame(client)
        assert hello, "no handshake response — write transport closed early"
        hello_obj = json.loads(hello.decode().strip())
        assert hello_obj["id"] == 1
        assert "result" in hello_obj
        assert hello_obj["result"]["server_version"]

        # 2) three follow-up requests (simulating /cha autocomplete keystrokes)
        for rid in (2, 3, 4):
            await _send(client, "system.ping", {}, rid)
            resp = await _read_one_frame(client)
            assert resp, f"no response for ping #{rid} — transport closed"
            obj = json.loads(resp.decode().strip())
            assert obj["id"] == rid
            assert obj["result"]["pong"] is True

        serve_task.cancel()
        try:
            await serve_task
        except asyncio.CancelledError:
            pass
    finally:
        client.close()
        conn.close()
        server_sock.close()


@pytest.mark.asyncio
async def test_socket_no_asyncio_pipe_warnings(_capture_asyncio_warnings: list[str]) -> None:
    """Three sequential RPC requests must NOT trigger the asyncio pipe warning.

    The pre-fix warning text is:
        ``"pipe closed by peer or os.write(pipe, data) raised exception."``
    """
    server_sock, client, conn, tmp = await _wire_paired_socket()
    try:
        disp = Dispatcher()
        register_aligned_methods(disp)
        server = RpcServer(disp, sock=conn)
        serve_task = asyncio.create_task(server.serve_forever())
        await server.started.wait()

        await _send(client, "system.hello", {"client_version": "0.0.2"}, 1)
        await _read_one_frame(client)
        # Fire 10 pings; pre-fix the asyncio warning starts after 5 dropped writes.
        for rid in range(2, 12):
            await _send(client, "system.ping", {}, rid)
            await _read_one_frame(client)

        serve_task.cancel()
        try:
            await serve_task
        except asyncio.CancelledError:
            pass

        pipe_warnings = [m for m in _capture_asyncio_warnings if "pipe closed by peer" in m]
        assert not pipe_warnings, f"asyncio.unix_events emitted pipe-closed warnings: {pipe_warnings!r}"
    finally:
        client.close()
        conn.close()
        server_sock.close()
