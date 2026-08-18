"""End-to-end round-trip for the web-app WebSocket RPC channel (ui-webui P1).

Starts the real :class:`WebSocketRpcServer`, connects an aiohttp WS client, and
drives the full path that ``build_web`` + the gateway wiring stand up:

    system.hello  -> handshake result
    turn.subscribe -> {subscription_id}
    turn.send      -> {turn_id, accepted}; message.start streams back
    (simulated turn events) -> token.delta / message.complete reach the client

The spine turn execution itself is covered elsewhere (test_rpc_spine); here a
FakeScheduler stands in for the agent loop, and streamed events are injected
through the real SubscriptionEmitter so the WS transport + coalesce loop +
broadcast are all exercised together.
"""

from __future__ import annotations

import asyncio
import json
import socket
from contextlib import closing

import aiohttp
import pytest

from raven.rpc.dispatcher import Dispatcher
from raven.rpc.methods.system import register_system_methods
from raven.rpc.methods.turn import register_turn_methods
from raven.rpc.subscriptions import SubscriptionEmitter
from raven.web_rpc.server import WebSocketRpcServer


class _FakeHandle:
    async def result(self):
        return None

    async def cancel(self) -> None:
        pass


class _FakeScheduler:
    """Records submitted TurnRequests; the real turn is not run here."""

    def __init__(self) -> None:
        self.submitted: list = []

    def submit(self, req):
        self.submitted.append(req)
        return _FakeHandle()


@pytest.fixture(autouse=True)
def _clear_active_turns():
    from raven.rpc.methods import turn as _turn_mod

    _turn_mod._active_turns.clear()
    yield
    _turn_mod._active_turns.clear()


def _free_port() -> int:
    with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def _rpc(ws, method, params, req_id):
    await ws.send_str(json.dumps({"jsonrpc": "2.0", "id": req_id, "method": method, "params": params}))
    while True:
        msg = await ws.receive(timeout=5.0)
        assert msg.type == aiohttp.WSMsgType.TEXT, f"unexpected ws msg: {msg.type}"
        frame = json.loads(msg.data)
        # Skip async event notifications until the matching response arrives.
        if frame.get("id") == req_id:
            return frame


async def _next_event(ws):
    """Return the next JSON-RPC ``event`` notification's inner event dict."""
    while True:
        msg = await ws.receive(timeout=5.0)
        assert msg.type == aiohttp.WSMsgType.TEXT
        frame = json.loads(msg.data)
        if frame.get("method") == "event":
            return frame["params"]["event"]


@pytest.fixture
async def server_url():
    port = _free_port()
    scheduler = _FakeScheduler()
    server = WebSocketRpcServer(host="127.0.0.1", port=port, auth_token=None)
    emitter = SubscriptionEmitter(send_frame=server.broadcast)
    dispatcher = Dispatcher()
    # Both halves of the same wiring, as the gateway builds it: a handshake that
    # names one channel while turns run on another is the bug this harness was
    # otherwise reproducing.
    register_system_methods(dispatcher, channel="web")
    register_turn_methods(dispatcher, emitter=emitter, scheduler=scheduler, turn_ids={}, default_channel="web")
    server.bind(dispatcher)

    serve_task = asyncio.create_task(server.serve_forever())
    # Wait until the port accepts connections.
    for _ in range(50):
        try:
            with closing(socket.create_connection(("127.0.0.1", port), timeout=0.1)):
                break
        except OSError:
            await asyncio.sleep(0.05)

    yield f"ws://127.0.0.1:{port}/ws", emitter, scheduler

    await server.stop()
    serve_task.cancel()
    try:
        await serve_task
    except asyncio.CancelledError:
        pass


async def test_hello_subscribe_send_and_stream(server_url) -> None:
    url, emitter, scheduler = server_url
    async with aiohttp.ClientSession() as session:
        async with session.ws_connect(url) as ws:
            # 1. handshake
            hello = await _rpc(ws, "system.hello", {"client_version": "0.1.0"}, 1)
            assert "error" not in hello, hello
            assert "server_version" in hello["result"]
            # The handshake's own answer, not just that it answered: a client
            # takes the session key from here and subscribes to it, so a
            # terminal key handed to a web client sends its turns to a pool it
            # is not watching.
            assert hello["result"]["session"]["default_channel"] == "web"
            assert hello["result"]["session"]["default_session_key"] == "web:default"

            # 2. subscribe
            sub = await _rpc(ws, "turn.subscribe", {"session_key": "web:default"}, 2)
            assert "error" not in sub, sub
            assert isinstance(sub["result"]["subscription_id"], str)

            # 3. send -> accepted, and message.start streams back
            sent = await _rpc(ws, "turn.send", {"session_key": "web:default", "content": "你好"}, 3)
            assert sent["result"]["accepted"] is True
            assert len(scheduler.submitted) == 1
            req = scheduler.submitted[0]
            assert req.conversation == "web:default"
            assert req.source.channel == "web"

            start = await _next_event(ws)
            assert start["type"] == "message.start"

            # 4. simulate the turn's streamed output through the real emitter
            await emitter.emit("web:default", {"type": "token.delta", "payload": {"text": "hi "}})
            await emitter.emit("web:default", {"type": "token.delta", "payload": {"text": "there"}})
            await emitter.emit(
                "web:default",
                {"type": "message.complete", "payload": {"turn_id": sent["result"]["turn_id"], "usage": {}}},
            )

            got = []
            while True:
                ev = await _next_event(ws)
                got.append(ev)
                if ev["type"] == "message.complete":
                    break
            types = [e["type"] for e in got]
            assert "token.delta" in types
            assert types[-1] == "message.complete"
            # coalesce loop merges the two consecutive token.delta frames
            merged = "".join(e["payload"]["text"] for e in got if e["type"] == "token.delta")
            assert merged == "hi there"


async def test_auth_token_gate_rejects_wrong_token() -> None:
    port = _free_port()
    server = WebSocketRpcServer(host="127.0.0.1", port=port, auth_token="s3cret")
    emitter = SubscriptionEmitter(send_frame=server.broadcast)
    dispatcher = Dispatcher()
    register_system_methods(dispatcher, channel="web")
    server.bind(dispatcher)
    serve_task = asyncio.create_task(server.serve_forever())
    for _ in range(50):
        try:
            with closing(socket.create_connection(("127.0.0.1", port), timeout=0.1)):
                break
        except OSError:
            await asyncio.sleep(0.05)
    try:
        async with aiohttp.ClientSession() as session:
            async with session.ws_connect(url := f"ws://127.0.0.1:{port}/ws") as ws:
                await ws.send_str("wrong-token")
                # Server closes the connection on token mismatch.
                msg = await ws.receive(timeout=5.0)
                assert msg.type in (
                    aiohttp.WSMsgType.CLOSE,
                    aiohttp.WSMsgType.CLOSED,
                    aiohttp.WSMsgType.CLOSING,
                )
    finally:
        await server.stop()
        serve_task.cancel()
        try:
            await serve_task
        except asyncio.CancelledError:
            pass
