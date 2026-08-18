"""The TUI-to-gateway relay (``raven/cli/_tui_relay.py``).

``plan_attach`` decides whether a launch relays to a running page-hosting
gateway or builds the embedded engine; ``_relay_until_done`` is the frame
pump between the Node client's TCP transport and the gateway's ``/rpc``
WebSocket. Both are exercised here against real sockets: a real aiohttp
WebSocket server plays the gateway, and a socketpair plays the accepted
client connection, so what is asserted is bytes on wires, not mocks.
"""

from __future__ import annotations

import asyncio
import json
import socket
import sys
from pathlib import Path

import pytest

from raven.cli._tui_relay import (
    AttachPlan,
    _healthy,
    _relay_until_done,
    plan_attach,
    run_subprocess_attached,
)


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """An agent home of our own, so nothing here reads the developer's."""
    monkeypatch.setenv("RAVEN_HOME", str(tmp_path / "home"))
    return tmp_path / "home"


# ---------------------------------------------------------------------------
# plan_attach: when a launch attaches, and when it stays embedded
# ---------------------------------------------------------------------------


def test_plan_attach_returns_none_without_a_recorded_gateway(home: Path) -> None:
    assert plan_attach() is None


def test_plan_attach_respects_the_config_kill_switch(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``tui.attach_gateway=false`` must short-circuit before any discovery."""
    from raven.cli import serve_commands
    from raven.config.loader import load_config

    cfg = load_config()
    cfg.tui.attach_gateway = False
    monkeypatch.setattr("raven.config.loader.load_config", lambda *a, **kw: cfg)

    def _must_not_be_called():
        raise AssertionError("discovery ran despite the kill switch")

    monkeypatch.setattr(serve_commands, "_gateway_hosted_page", _must_not_be_called)
    assert plan_attach() is None


def test_plan_attach_carries_the_launch_directory(home: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from raven.cli import _tui_relay, serve_commands

    monkeypatch.setattr(serve_commands, "_gateway_hosted_page", lambda: (18999, "tok"))

    async def _always_healthy(port: int) -> bool:
        return port == 18999

    monkeypatch.setattr(_tui_relay, "_healthy", _always_healthy)
    project = tmp_path / "proj"
    project.mkdir()
    monkeypatch.chdir(project)

    plan = plan_attach()
    assert plan == AttachPlan(port=18999, token="tok", workdir=project.resolve())


def test_plan_attach_declines_a_dead_gateway(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from raven.cli import _tui_relay, serve_commands

    monkeypatch.setattr(serve_commands, "_gateway_hosted_page", lambda: (18999, "tok"))

    async def _never_healthy(port: int) -> bool:
        return False

    monkeypatch.setattr(_tui_relay, "_healthy", _never_healthy)
    assert plan_attach() is None


def test_plan_attach_declines_a_workdir_the_contract_cannot_express(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A launch directory ``validate_override`` rejects (here: the agent home
    itself) cannot be pinned on the attached engine — run embedded instead."""
    from raven.cli import serve_commands

    monkeypatch.setattr(serve_commands, "_gateway_hosted_page", lambda: (18999, "tok"))
    from raven.config.loader import load_config

    agent_home = load_config().workspace_path
    agent_home.mkdir(parents=True, exist_ok=True)
    assert plan_attach(workspace=str(agent_home)) is None


async def test_healthy_requires_the_raven_serve_service(home: Path) -> None:
    """/health must both answer 200 and name raven-serve; a stranger on the
    recorded port must read as not-a-gateway."""
    from aiohttp import web

    async def health(_request: web.Request) -> web.Response:
        return web.json_response({"ok": True, "service": "someone-else"})

    app = web.Application()
    app.router.add_get("/health", health)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    try:
        assert await _healthy(port) is False
    finally:
        await runner.cleanup()
    # And with the listener gone the same port reads as dead, not as an error.
    assert await _healthy(port) is False


# ---------------------------------------------------------------------------
# _relay_until_done: the frame pump against a real WS gateway
# ---------------------------------------------------------------------------


async def _start_fake_gateway() -> tuple[int, list[dict], object]:
    """A dialect-A speaking stand-in: answers hello / session.create /
    turn.subscribe, pushes one event after a subscribe, and closes the socket
    on a ``die.now`` frame. Records every request it sees."""
    from aiohttp import WSMsgType, web

    seen: list[dict] = []

    async def rpc(request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        async for msg in ws:
            if msg.type != WSMsgType.TEXT:
                continue
            frame = json.loads(msg.data)
            seen.append(frame)
            method = frame.get("method")
            if method == "system.hello":
                await ws.send_json({"jsonrpc": "2.0", "id": frame["id"], "result": {"server_version": "0"}})
            elif method == "session.create":
                await ws.send_json({"jsonrpc": "2.0", "id": frame["id"], "result": {"session_id": "tui:x"}})
            elif method == "turn.subscribe":
                await ws.send_json({"jsonrpc": "2.0", "id": frame["id"], "result": {"subscription_id": "sub-1"}})
                await ws.send_json(
                    {
                        "jsonrpc": "2.0",
                        "method": "event",
                        "params": {
                            "subscription_id": "sub-1",
                            "event": {"type": "token.delta", "payload": {"text": "hi"}},
                        },
                    }
                )
            elif method == "die.now":
                await ws.close()
                break
        return ws

    app = web.Application()
    app.router.add_get("/rpc", rpc)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    return port, seen, runner


async def _open_relay(port: int, workdir: Path, deadline: float = 5.0):
    """Wire ws + socketpair + relay task; return the client-side stream pair,
    the relay task, its proc_done event, and the aiohttp session to close."""
    import aiohttp

    session = aiohttp.ClientSession()
    ws = await session.ws_connect(f"http://127.0.0.1:{port}/rpc")
    client_sock, server_sock = socket.socketpair()
    proc_done = asyncio.Event()

    async def _run() -> tuple[bool, bool]:
        try:
            return await _relay_until_done(server_sock, "secret", ws, workdir, deadline, proc_done)
        finally:
            await ws.close()
            await session.close()

    relay_task = asyncio.create_task(_run())
    client_sock.setblocking(False)
    reader, writer = await asyncio.open_connection(sock=client_sock)
    return reader, writer, relay_task, proc_done


def _frame(rid: int, method: str, params: dict) -> bytes:
    return (json.dumps({"jsonrpc": "2.0", "id": rid, "method": method, "params": params}) + "\n").encode()


async def test_relay_round_trips_and_injects_the_workdir(home: Path, tmp_path: Path) -> None:
    port, seen, runner = await _start_fake_gateway()
    try:
        reader, writer, relay_task, proc_done = await _open_relay(port, tmp_path / "launch")

        writer.write(b"secret\n")
        writer.write(_frame(1, "system.hello", {"client_version": "0.1.0"}))
        hello = json.loads(await asyncio.wait_for(reader.readline(), 5))
        assert hello["id"] == 1 and "result" in hello

        # The stock client predates the surface param; the relay declares the
        # terminal so the gateway's traces can tell this connection apart.
        hello_seen = next(f for f in seen if f.get("method") == "system.hello")
        assert hello_seen["params"]["surface"] == "tui"

        # The stock client sends no workdir; the relay says where it launched.
        writer.write(_frame(2, "session.create", {"cols": 80}))
        created = json.loads(await asyncio.wait_for(reader.readline(), 5))
        assert created["id"] == 2
        create_seen = next(f for f in seen if f.get("method") == "session.create")
        assert create_seen["params"]["workdir"] == str(tmp_path / "launch")
        assert create_seen["params"]["cols"] == 80

        # Notifications pass through verbatim, after their subscribe response.
        writer.write(_frame(3, "turn.subscribe", {"session_key": "tui:x"}))
        subscribed = json.loads(await asyncio.wait_for(reader.readline(), 5))
        assert subscribed["result"]["subscription_id"] == "sub-1"
        event = json.loads(await asyncio.wait_for(reader.readline(), 5))
        assert event["method"] == "event"
        assert event["params"]["event"]["payload"]["text"] == "hi"

        # The TUI closing only closes the relay; the gateway is never touched.
        proc_done.set()
        ok, dropped = await asyncio.wait_for(relay_task, 5)
        assert (ok, dropped) == (True, False)
    finally:
        await runner.cleanup()


async def test_relay_surfaces_a_dead_gateway_and_never_hangs(home: Path, tmp_path: Path) -> None:
    """The gateway dying mid-session must land on every live stream as the
    error event shape the client already renders, then EOF — not silence."""
    port, _seen, runner = await _start_fake_gateway()
    try:
        reader, writer, relay_task, proc_done = await _open_relay(port, tmp_path)

        writer.write(b"secret\n")
        writer.write(_frame(1, "system.hello", {"client_version": "0.1.0"}))
        await asyncio.wait_for(reader.readline(), 5)
        writer.write(_frame(2, "turn.subscribe", {"session_key": "tui:x"}))
        await asyncio.wait_for(reader.readline(), 5)
        await asyncio.wait_for(reader.readline(), 5)

        writer.write(_frame(3, "die.now", {}))
        surfaced = json.loads(await asyncio.wait_for(reader.readline(), 5))
        assert surfaced["method"] == "event"
        assert surfaced["params"]["subscription_id"] == "sub-1"
        assert surfaced["params"]["event"]["type"] == "error"
        assert surfaced["params"]["event"]["payload"]["reason"] == "gateway_disconnected"
        assert await asyncio.wait_for(reader.readline(), 5) == b""

        proc_done.set()
        ok, dropped = await asyncio.wait_for(relay_task, 5)
        assert (ok, dropped) == (True, True)
    finally:
        await runner.cleanup()


async def test_relay_reports_a_client_frame_over_the_frame_limit(home: Path, tmp_path: Path) -> None:
    """The client-to-gateway pump can die on its own: a frame past
    MAX_FRAME_BYTES overruns the reader, and the overrun bytes stay in the
    buffer, so the stream is finished. The child and the WebSocket are both
    still alive, so nothing else notices -- the relay has to, or the terminal
    silently stops talking to the engine."""
    from raven.rpc.server import MAX_FRAME_BYTES

    port, _seen, runner = await _start_fake_gateway()
    try:
        reader, writer, relay_task, proc_done = await _open_relay(port, tmp_path)

        writer.write(b"secret\n")
        writer.write(_frame(1, "system.hello", {"client_version": "0.1.0"}))
        await asyncio.wait_for(reader.readline(), 5)
        writer.write(_frame(2, "turn.subscribe", {"session_key": "tui:x"}))
        await asyncio.wait_for(reader.readline(), 5)
        await asyncio.wait_for(reader.readline(), 5)

        writer.write(b'{"jsonrpc":"2.0","id":3,"method":"turn.send","params":{"content":"' + b"x" * MAX_FRAME_BYTES)

        surfaced = json.loads(await asyncio.wait_for(reader.readline(), 5))
        assert surfaced["method"] == "event"
        assert surfaced["params"]["subscription_id"] == "sub-1"
        assert surfaced["params"]["event"]["type"] == "error"
        assert surfaced["params"]["event"]["payload"]["reason"] == "client_frame_too_large"
        assert await asyncio.wait_for(reader.readline(), 5) == b""

        proc_done.set()
        ok, failed = await asyncio.wait_for(relay_task, 5)
        assert (ok, failed) == (True, True)
    finally:
        await runner.cleanup()


async def test_relay_reads_a_client_eof_as_a_clean_end(home: Path, tmp_path: Path) -> None:
    """The same pump ends benignly when the child closes its socket on its way
    out, and the reaper thread has not set proc_done yet. That must not be
    reported as a lost engine, or every normal quit would exit nonzero."""
    port, _seen, runner = await _start_fake_gateway()
    try:
        reader, writer, relay_task, proc_done = await _open_relay(port, tmp_path)

        writer.write(b"secret\n")
        writer.write(_frame(1, "system.hello", {"client_version": "0.1.0"}))
        await asyncio.wait_for(reader.readline(), 5)

        writer.close()
        await asyncio.sleep(0.05)
        proc_done.set()
        ok, failed = await asyncio.wait_for(relay_task, 5)
        assert (ok, failed) == (True, False)
    finally:
        await runner.cleanup()


async def test_relay_rejects_a_wrong_token(home: Path, tmp_path: Path) -> None:
    port, seen, runner = await _start_fake_gateway()
    try:
        reader, writer, relay_task, _proc_done = await _open_relay(port, tmp_path)
        writer.write(b"not-the-secret\n")
        writer.write(_frame(1, "system.hello", {"client_version": "0.1.0"}))
        ok, dropped = await asyncio.wait_for(relay_task, 5)
        assert (ok, dropped) == (False, False)
        assert seen == []
    finally:
        await runner.cleanup()


async def test_relay_enforces_the_handshake_deadline(home: Path, tmp_path: Path) -> None:
    port, _seen, runner = await _start_fake_gateway()
    try:
        _reader, writer, relay_task, _proc_done = await _open_relay(port, tmp_path, deadline=0.3)
        writer.write(b"secret\n")
        ok, dropped = await asyncio.wait_for(relay_task, 5)
        assert (ok, dropped) == (False, False)
    finally:
        await runner.cleanup()


# ---------------------------------------------------------------------------
# run_subprocess_attached: the gateway vanishing between discovery and attach
# ---------------------------------------------------------------------------


def test_attached_launch_falls_back_to_embedded_when_the_gateway_is_gone(
    home: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """/health answered at discovery, and the gateway died before the WS
    connect: the accepted socket is untouched, so the embedded server (stubbed
    here) must take over the very same connection."""
    from raven.cli import tui_commands

    calls: dict[str, object] = {}

    async def fake_embedded(conn, auth_token, deadline, proc_done, workspace=None, home=None):
        calls["conn"] = conn
        calls["workspace"] = workspace
        await proc_done.wait()
        return True

    monkeypatch.setattr(tui_commands, "_run_rpc_server_until_done", fake_embedded)

    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.bind(("127.0.0.1", 0))
    dead_port = probe.getsockname()[1]
    probe.close()

    child = (
        "import os, socket;"
        "host, port = os.environ['RAVEN_RPC_SOCKET'].rsplit(':', 1);"
        "s = socket.create_connection((host, int(port)));"
        "s.sendall((os.environ['RAVEN_RPC_TOKEN'] + '\\n').encode());"
        "s.close()"
    )
    plan = AttachPlan(port=dead_port, token="tok", workdir=tmp_path)
    code = run_subprocess_attached(
        sys.executable, ["-c", child], cwd=tmp_path, plan=plan, forward_signals=False, workspace=None
    )
    assert code == 0
    assert "conn" in calls


# ---------------------------------------------------------------------------
# _rewrite_outbound: the handshake declares what this connection fronts
# ---------------------------------------------------------------------------


def test_relay_keeps_a_surface_the_client_already_declared(home: Path) -> None:
    """setdefault, not overwrite: a future client that names itself wins."""
    from raven.cli._tui_relay import _RelayState, _rewrite_outbound

    raw = json.dumps(
        {"jsonrpc": "2.0", "id": 1, "method": "system.hello", "params": {"client_version": "0.1.0", "surface": "shell"}}
    )
    out = json.loads(_rewrite_outbound(raw, Path("/w"), _RelayState()))
    assert out["params"]["surface"] == "shell"


def test_relay_declares_the_surface_even_on_a_hello_notification(home: Path) -> None:
    """The injection must not depend on the frame carrying an id."""
    from raven.cli._tui_relay import _RelayState, _rewrite_outbound

    state = _RelayState()
    raw = json.dumps({"jsonrpc": "2.0", "method": "system.hello", "params": {"client_version": "0.1.0"}})
    out = json.loads(_rewrite_outbound(raw, Path("/w"), state))
    assert out["params"]["surface"] == "tui"
    assert state.hello_id is None, "a notification must not latch the handshake"
