"""The served page mounted inside the gateway process (``_gateway_page``).

What `raven serve` assembles around an engine it builds, ``mount_page``
assembles around the engine the gateway already has: the same auth model, the
same routes, the same serve.json ``raven web`` reads -- and the same answers
over the wire, asserted here against a real socket.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from tests.test_rpc_bootstrap import _FakeCron, _FakeLoop


@pytest.fixture
def home(tmp_path: Path, monkeypatch) -> Path:
    """An agent home of our own, so nothing here touches the developer's."""
    monkeypatch.setenv("RAVEN_HOME", str(tmp_path / "home"))
    return tmp_path / "home"


async def test_the_mounted_page_answers_like_raven_serve(home: Path) -> None:
    import aiohttp

    from raven.cli._gateway_page import mount_page
    from raven.cli.serve_commands import SERVE

    loop = _FakeLoop(_FakeCron())
    mount = await mount_page(loop, 18930)
    assert mount is not None
    try:
        # The engine is the one it was handed, not a second build.
        assert mount.outlet.name == "tui"
        assert SERVE.hosted_by_gateway is True
        # The page's own broker is exposed for the host's routing shim.
        assert mount.question_broker is not None

        # serve.json carries this process, so `raven web` and the GUI shell
        # find the page, and `raven serve` can match the pid to the lock's.
        state = json.loads((home / "serve.json").read_text(encoding="utf-8"))
        assert state["port"] == mount.port
        assert state["pid"] == os.getpid()

        async with aiohttp.ClientSession() as session:
            async with session.get(f"{mount.url}/health") as resp:
                assert resp.status == 200
                assert (await resp.json())["service"] == "raven-serve"
            # The dialect-A socket, opened with the recorded token the way
            # local tooling does.
            async with session.ws_connect(f"{mount.url}/rpc", headers={"X-Raven-Token": state["token"]}) as ws:
                await ws.send_json(
                    {"jsonrpc": "2.0", "id": 1, "method": "system.hello", "params": {"client_version": "0.1.0"}}
                )
                reply = json.loads((await ws.receive()).data)
                assert reply["id"] == 1
                assert "result" in reply
    finally:
        await mount.teardown()

    # Teardown leaves nothing claiming a page is served.
    assert not (home / "serve.json").exists()
    assert SERVE.hosted_by_gateway is False
    assert SERVE.port is None


def _write_foreign_state(home: Path, *, pid: int, port: int) -> Path:
    path = home / "serve.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"port": port, "token": "their-token", "pid": pid}), encoding="utf-8")
    return path


async def test_mount_yields_to_a_live_standalone_serve(home: Path) -> None:
    """A live standalone `raven serve` owning serve.json must not be clobbered:
    the mount is skipped and the file is left exactly as it was."""
    from aiohttp import web

    from raven.cli._gateway_page import mount_page
    from raven.rpc.transports.ws import pick_port

    async def health(_request):
        return web.json_response({"service": "raven-serve"})

    app = web.Application()
    app.router.add_get("/health", health)
    runner = web.AppRunner(app)
    await runner.setup()
    other_port = await pick_port(18940)
    site = web.TCPSite(runner, "127.0.0.1", other_port)
    await site.start()
    try:
        # The parent process stands in for the standalone serve: alive, not us.
        path = _write_foreign_state(home, pid=os.getppid(), port=other_port)
        before = path.read_text(encoding="utf-8")

        mount = await mount_page(_FakeLoop(_FakeCron()), 18941)

        assert mount is None
        assert path.read_text(encoding="utf-8") == before
    finally:
        await runner.cleanup()


async def test_a_stale_serve_json_does_not_block_the_mount(home: Path) -> None:
    """serve.json naming a live pid whose port no longer answers /health is
    stale (the serve died and the pid is a stranger's); mounting over it is
    correct, and the file is rewritten as ours."""
    from raven.cli._gateway_page import mount_page
    from raven.rpc.transports.ws import pick_port

    dead_port = await pick_port(18950)
    _write_foreign_state(home, pid=os.getppid(), port=dead_port)

    mount = await mount_page(_FakeLoop(_FakeCron()), 18951)
    assert mount is not None
    try:
        state = json.loads((home / "serve.json").read_text(encoding="utf-8"))
        assert state["pid"] == os.getpid()
        assert state["port"] == mount.port
    finally:
        await mount.teardown()


async def test_teardown_keeps_a_state_file_another_process_rewrote(home: Path) -> None:
    """Only our own serve.json is unlinked on teardown: a standalone serve that
    (re)wrote the file after us keeps its state file and stays discoverable."""
    from raven.cli._gateway_page import mount_page

    mount = await mount_page(_FakeLoop(_FakeCron()), 18960)
    assert mount is not None

    path = _write_foreign_state(home, pid=os.getppid(), port=18961)
    await mount.teardown()

    assert path.exists()
    assert json.loads(path.read_text(encoding="utf-8"))["pid"] == os.getppid()
    path.unlink()


async def test_a_question_opens_only_on_the_socket_that_sent_the_turn(home: Path) -> None:
    """Two surfaces on one page: with G2 the socket set on ``/rpc`` includes a
    relayed ``raven tui``, so a broadcast ``clarify.request`` would open the
    sheet in a browser tab for a conversation it is not in. The question follows
    the socket that sent the turn."""
    import asyncio
    import contextlib

    import aiohttp

    from raven.cli._gateway_page import mount_page

    mount = await mount_page(_FakeLoop(_FakeCron()), 18970)
    assert mount is not None
    try:
        token = json.loads((home / "serve.json").read_text(encoding="utf-8"))["token"]
        headers = {"X-Raven-Token": token}
        async with aiohttp.ClientSession() as session:
            async with (
                session.ws_connect(f"{mount.url}/rpc", headers=headers) as terminal,
                session.ws_connect(f"{mount.url}/rpc", headers=headers) as tab,
            ):
                await terminal.send_json(
                    {
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "turn.send",
                        "params": {"session_key": "tui:t", "content": "hi"},
                    }
                )
                await _read_until(terminal, lambda f: f.get("id") == 1)

                asked = asyncio.create_task(mount.question_broker.await_question("tui:t", prompt="which?"))
                try:
                    frame = await _read_until(terminal, lambda f: f.get("method") == "clarify.request")
                    assert frame["params"]["conversation_id"] == "tui:t"
                    with pytest.raises(asyncio.TimeoutError):
                        await _read_until(tab, lambda f: f.get("method") == "clarify.request", timeout=0.5)
                finally:
                    mount.question_broker.reply("tui:t", "answered here")
                    with contextlib.suppress(asyncio.TimeoutError):
                        await asyncio.wait_for(asked, 2)
    finally:
        await mount.teardown()


async def _read_until(ws, matches, timeout: float = 5.0) -> dict:
    """The next frame on this socket that ``matches``; TimeoutError if none does.

    A shared page streams other traffic (a turn against the test's fake engine
    fails and says so), so a test that wants one frame has to skip the rest.
    """
    import asyncio

    import aiohttp

    async def _read() -> dict:
        while True:
            msg = await ws.receive()
            if msg.type is not aiohttp.WSMsgType.TEXT:
                continue
            frame = json.loads(msg.data)
            if matches(frame):
                return frame

    return await asyncio.wait_for(_read(), timeout)
