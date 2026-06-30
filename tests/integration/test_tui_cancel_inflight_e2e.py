"""Real-RPC regression for the in-flight-cancel freeze.

Repro (prod / real-RPC dist mode): cancel a streaming turn
with Ctrl+C, then send a second turn → the whole TUI freezes (only Ctrl+D
recovers). Root cause: ``turn.cancel`` tore down the SESSION-scoped
subscription on a PER-TURN cancel, so every turn-2 event was emitted to an
empty subscriber list and silently dropped.

This drives the real ``RpcServer`` over a unix socket + a real
``SubscriptionEmitter`` (NOT an AsyncMock stub), with the turn path on the
spine (``build_tui`` scheduler/hub/sink) and a fake streaming agent loop whose
``run_turn`` parks for the cancelled turn and streams a token for the
next. Pre-fix the client never receives turn-2's ``message.complete``; post-fix
it does — proving the per-turn cancel leaves the session subscription intact
over real transport.
"""

from __future__ import annotations

import asyncio
import json
import os
import socket
import tempfile
from pathlib import Path

import pytest

from raven.spine import StreamDelta, TurnOutcome, Usage
from raven.tui_rpc.dispatcher import Dispatcher
from raven.tui_rpc.methods.turn import clear_active, register_turn_methods
from raven.tui_rpc.server import RpcServer
from raven.tui_rpc.spine import build_tui
from raven.tui_rpc.subscriptions import SubscriptionEmitter

SESSION_KEY = "tui:default"


class FakeStreamingAgent:
    """Agent loop stand-in for the spine runner: parks the ``hang`` turn so it
    is genuinely in-flight and cancellable; streams one token otherwise. Token
    content flows run_turn → StreamDelta → hub → token.delta; message.start is
    turn.send's, message.complete is the sink's."""

    tools: dict = {}  # mirrors AgentLoop.tools (the TUI runner reads .get('message'))

    async def run_turn(self, req, emit, drain, *, stream, usage_sink=None) -> TurnOutcome:
        if req.text == "hang":
            await asyncio.Event().wait()  # never set — cancelled by turn.cancel
        await emit(StreamDelta(delta="second-turn-token"))
        return TurnOutcome(usage=Usage(0, 0, 0), explicit_reply=True)


async def _wire_paired_socket() -> tuple[socket.socket, socket.socket, Path]:
    """Mirror the production fd topology built by ``run_subprocess_with_rpc``."""
    tmp = Path(tempfile.mkdtemp(prefix="eve-test-cancel-"))
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
    # The RpcServer dups + owns ``conn``'s fd; keep listening + conn handles
    # alive for the duration via the returned tuple, close them in teardown.
    server_sock_keep = server_sock
    return client, conn, tmp, server_sock_keep  # type: ignore[return-value]


async def _send(client: socket.socket, method: str, params: dict, rid: int) -> None:
    loop = asyncio.get_running_loop()
    frame = json.dumps({"jsonrpc": "2.0", "id": rid, "method": method, "params": params}) + "\n"
    await loop.sock_sendall(client, frame.encode())


async def _drain_events(client: socket.socket, *, duration: float = 0.6) -> list[dict]:
    """Read all frames available within ``duration`` and return event payloads."""
    loop = asyncio.get_running_loop()
    buf = bytearray()
    events: list[dict] = []
    deadline = loop.time() + duration
    while loop.time() < deadline:
        remaining = deadline - loop.time()
        try:
            chunk = await asyncio.wait_for(loop.sock_recv(client, 65536), timeout=remaining)
        except asyncio.TimeoutError:
            break
        if not chunk:
            break
        buf.extend(chunk)
        while b"\n" in buf:
            line, _, rest = buf.partition(b"\n")
            buf = bytearray(rest)
            if not line.strip():
                continue
            frame = json.loads(line.decode())
            if frame.get("method") == "event":
                events.append(frame["params"]["event"])
    return events


@pytest.fixture(autouse=True)
def _clear_active_turns():
    from raven.tui_rpc.methods import turn as _turn_mod

    _turn_mod._active_turns.clear()
    yield
    for task in list(_turn_mod._active_turns.values()):
        task.cancel()
    _turn_mod._active_turns.clear()


@pytest.mark.asyncio
async def test_turn2_streams_after_turn1_cancel_over_real_rpc() -> None:
    """Cancel turn 1 mid-stream, then turn 2 must still reach the client.

    End-to-end through the real RpcServer + SubscriptionEmitter, with the turn
    path submitting onto the spine (build_tui). The single per-session
    subscription (subscribe-once) must survive the turn-1 cancel so turn 2's
    ``message.complete`` is delivered.
    """
    client, conn, tmp, server_sock = await _wire_paired_socket()
    serve_task = None
    teardown = None
    try:
        req_fd = os.dup(conn.fileno())
        notif_fd = os.dup(conn.fileno())

        disp = Dispatcher()
        server = RpcServer(req_fd, notif_fd, disp)
        emitter = SubscriptionEmitter(send_frame=server.send_frame)
        scheduler, _hub, turn_ids, teardown = build_tui(FakeStreamingAgent(), emitter, on_turn_end=clear_active)
        register_turn_methods(disp, emitter=emitter, scheduler=scheduler, turn_ids=turn_ids)

        serve_task = asyncio.create_task(server.serve_forever())
        await server.started.wait()

        # Subscribe once per session (mirrors useMainApp's subscribe-per-session).
        await _send(client, "turn.subscribe", {"session_key": SESSION_KEY}, 1)
        await asyncio.sleep(0.05)

        # Turn 1 — starts streaming, user hits Ctrl+C mid-stream.
        await _send(client, "turn.send", {"session_key": SESSION_KEY, "content": "hang"}, 2)
        await asyncio.sleep(0.08)
        await _send(client, "turn.cancel", {"session_key": SESSION_KEY}, 3)
        await asyncio.sleep(0.08)

        # Turn 2 — must run and stream to completion on the SAME subscription.
        await _send(client, "turn.send", {"session_key": SESSION_KEY, "content": "second"}, 4)

        events = await _drain_events(client, duration=0.6)

        completes = [e for e in events if e.get("type") == "message.complete"]
        assert completes, (
            "turn 2 produced no message.complete over real RPC — the per-turn "
            f"cancel wedged the session subscription. events seen: {events}"
        )
    finally:
        if teardown is not None:
            await teardown()
        if serve_task is not None:
            serve_task.cancel()
            try:
                await serve_task
            except asyncio.CancelledError:
                pass
        client.close()
        try:
            conn.close()
        except OSError:
            pass
        server_sock.close()
