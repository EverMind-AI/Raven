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
3. ``test_pipe_path_still_works`` — keeps the legacy bare ``os.pipe()``
   transport path covered so non-socket callers (e.g. the v0.0.1 demo
   runner) keep working.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import socket
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest

from raven.tui_rpc.dispatcher import Dispatcher
from raven.tui_rpc.methods import register_aligned_methods
from raven.tui_rpc.server import RpcServer

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _wire_paired_socket() -> tuple[socket.socket, socket.socket, socket.socket, Path]:
    """Build the same fd topology that ``run_subprocess_with_rpc`` builds.

    Returns ``(listening_sock, client_sock, server_conn, tmp_dir)``. The
    caller still needs to ``os.dup(server_conn.fileno())`` twice to mirror
    the production path.
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
async def test_socket_roundtrip_after_inbound_byte() -> None:
    """Hello + slash-style requests round-trip with responses delivered.

    Pre-fix this hangs: the write transport closed on first inbound byte and
    no response ever reached the client. The 2 s per-frame timeout in
    ``_read_one_frame`` would expire and the assertion would fail.
    """
    server_sock, client, conn, tmp = await _wire_paired_socket()
    try:
        req_fd = os.dup(conn.fileno())
        notif_fd = os.dup(conn.fileno())

        disp = Dispatcher()
        register_aligned_methods(disp)
        server = RpcServer(req_fd, notif_fd, disp)
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
        req_fd = os.dup(conn.fileno())
        notif_fd = os.dup(conn.fileno())

        disp = Dispatcher()
        register_aligned_methods(disp)
        server = RpcServer(req_fd, notif_fd, disp)
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


@pytest.mark.asyncio
async def test_pipe_path_still_works() -> None:
    """Bare ``os.pipe()`` fds keep the legacy connect_*_pipe path alive.

    The v0.0.1 demo runner and any future test that wires unidirectional
    pipes (e.g. ``test_handshake_timeout_*``) depend on this fallback.
    """
    # Node→Python (requests)
    req_r, req_w = os.pipe()
    # Python→Node (responses)
    notif_r, notif_w = os.pipe()

    try:
        disp = Dispatcher()
        register_aligned_methods(disp)
        server = RpcServer(req_r, notif_w, disp)
        serve_task = asyncio.create_task(server.serve_forever())
        await server.started.wait()

        # Send a hello via the request pipe write end.
        frame = (
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "system.hello",
                    "params": {"client_version": "0.0.2"},
                }
            )
            + "\n"
        ).encode()
        os.write(req_w, frame)

        loop = asyncio.get_running_loop()

        # Read response from notif_r non-blockingly.
        os.set_blocking(notif_r, False)
        buf = bytearray()
        deadline = loop.time() + 2.0
        while not buf.endswith(b"\n") and loop.time() < deadline:
            try:
                chunk = os.read(notif_r, 65536)
            except BlockingIOError:
                await asyncio.sleep(0.02)
                continue
            if not chunk:
                break
            buf.extend(chunk)

        assert buf, "no response on legacy pipe path"
        obj = json.loads(buf.decode().strip())
        assert obj["id"] == 1
        assert obj["result"]["server_version"]

        serve_task.cancel()
        try:
            await serve_task
        except asyncio.CancelledError:
            pass
    finally:
        # serve_forever owns req_r + notif_w via _shutdown; we still close
        # the other ends.
        for fd in (req_w, notif_r):
            try:
                os.close(fd)
            except OSError:
                pass
