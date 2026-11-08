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
    monkeypatch.setattr("raven.cli._gateway_page._last_credentials", None)
    return tmp_path / "home"


async def test_a_swap_keeps_an_open_tab_signed_in(home: Path) -> None:
    """Seen live: a reload asked from the page signed the page out. The old
    generation's teardown removes serve.json before the next one mounts, so
    the cookie the tab holds has to cross the swap in memory."""
    from raven.cli._gateway_page import mount_page

    loop = _FakeLoop(_FakeCron())
    first = await mount_page(loop, 18937)
    assert first is not None
    before = json.loads((home / "serve.json").read_text(encoding="utf-8"))
    await first.teardown()
    assert not (home / "serve.json").exists()

    second = await mount_page(loop, 18937)
    assert second is not None
    try:
        after = json.loads((home / "serve.json").read_text(encoding="utf-8"))
        assert (after["token"], after["cookie"]) == (before["token"], before["cookie"])
    finally:
        await second.teardown()


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
        # And its spine's submit, so a relay into a page session queues on the
        # page's lane for that session rather than beside it.
        assert mount.submit is not None

        # serve.json carries this process, so `raven web` and the GUI shell
        # find the page, and `raven serve` can match the pid to the lock's.
        state = json.loads((home / "serve.json").read_text(encoding="utf-8"))
        assert state["port"] == mount.port
        assert state["pid"] == os.getpid()
        # The generation's own id, so a launch probe can tell this writer from a
        # later process the OS handed the same pid to.
        assert state["boot_id"]

        async with aiohttp.ClientSession() as session:
            async with session.get(f"{mount.url}/health") as resp:
                assert resp.status == 200
                health = await resp.json()
                assert health["service"] == "raven-serve"
                assert health["boot_id"] == state["boot_id"]
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


def _write_foreign_state(home: Path, *, pid: int, port: int, boot_id: str | None = None) -> Path:
    state: dict[str, object] = {"port": port, "token": "their-token", "pid": pid}
    if boot_id is not None:
        state["boot_id"] = boot_id
    path = home / "serve.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state), encoding="utf-8")
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


async def _standalone_owner_with_health(home: Path, *, boot_id: str | None, answer):
    """A live foreign pid plus a health server, read the way the mount reads it."""
    from aiohttp import web

    from raven.cli._gateway_page import _standalone_serve_owner
    from raven.rpc.transports.ws import pick_port

    app = web.Application()
    app.router.add_get("/health", answer)
    runner = web.AppRunner(app)
    await runner.setup()
    port = await pick_port(18960)
    site = web.TCPSite(runner, "127.0.0.1", port)
    await site.start()
    try:
        _write_foreign_state(home, pid=os.getppid(), port=port, boot_id=boot_id)
        return port, await _standalone_serve_owner()
    finally:
        await runner.cleanup()


async def test_the_standalone_owner_must_name_the_record_s_generation(home: Path) -> None:
    """A recycled pid is alive, and something else can answer the recorded
    port. The boot id is the part of the record that says whether the answer
    is the generation that wrote it."""
    from aiohttp import web

    async def health(_request):
        return web.json_response({"service": "raven-serve", "boot_id": "the-answering-generation"})

    _port, owner = await _standalone_owner_with_health(home, boot_id="the-generation-before", answer=health)
    assert owner is None


async def test_the_standalone_owner_accepts_the_generation_that_wrote_the_record(home: Path) -> None:
    """The match narrows the path, it does not disable it: the writer still
    owns the page."""
    from aiohttp import web

    async def health(_request):
        return web.json_response({"service": "raven-serve", "boot_id": "generation-7"})

    port, owner = await _standalone_owner_with_health(home, boot_id="generation-7", answer=health)
    assert owner == (os.getppid(), port)


async def test_a_health_answer_that_is_not_an_object_is_not_a_standalone_owner(home: Path) -> None:
    """A 200 carrying a JSON array names no service: reading a field off it is
    a traceback, and the mount is then wrongly abandoned."""
    from aiohttp import web

    async def health(_request):
        return web.json_response(["raven-serve"])

    _port, owner = await _standalone_owner_with_health(home, boot_id=None, answer=health)
    assert owner is None


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
        assert state["boot_id"]
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


async def test_a_relaunch_reclaims_the_exact_port_the_tab_is_on(home: Path, monkeypatch) -> None:
    """The page mount follows the same strict-port policy standalone serve does.

    A relaunch under an open browser tab -- the web supervisor's retry, or
    ``system.upgrade`` -- sets RAVEN_SERVE_PORT_STRICT because the tab is pointed
    at a port, and probing forward to the next free one strands it. Now that
    `raven web` supervises this mount rather than standalone serve, reading that
    flag in only one of the two places would strand the very page the restart was
    for.
    """
    import raven.rpc.transports.ws as ws_module
    from raven.cli import _gateway_page

    asked: list[bool] = []

    async def fake_pick(preferred: int, *, strict: bool = False, wait_s: float = 20.0) -> int:
        asked.append(strict)
        raise OSError("not binding anything in this test")

    monkeypatch.setattr(ws_module, "pick_port", fake_pick)
    monkeypatch.setenv("RAVEN_SERVE_PORT_STRICT", "1")
    loop = _FakeLoop(_FakeCron())
    with pytest.raises(OSError):
        await _gateway_page.mount_page(loop, 18931)
    assert asked == [True], "the mount probed forward under a tab it had to come back to"

    asked.clear()
    monkeypatch.delenv("RAVEN_SERVE_PORT_STRICT")
    with pytest.raises(OSError):
        await _gateway_page.mount_page(loop, 18931)
    assert asked == [False], "an ordinary start should still probe forward"


async def test_the_mounted_page_says_when_it_is_behind_its_sources(home: Path, tmp_path: Path, monkeypatch) -> None:
    """The gateway host hands the resolver's judgement to the app the way
    standalone serve does, so the page it mounts carries the header -- pinned
    here because the predicate and the header are each tested alone, and a
    host that forgot to wire them would keep both green."""
    import os

    import aiohttp

    from raven.cli import serve_commands
    from raven.cli._gateway_page import mount_page

    ui = tmp_path / "repo" / "ui-web"
    (ui / "dist").mkdir(parents=True)
    (ui / "src").mkdir()
    (ui / "dist" / "index.html").write_text("<html>", encoding="utf-8")
    (ui / "src" / "main.tsx").write_text("x", encoding="utf-8")
    os.utime(ui / "dist" / "index.html", (1_000, 1_000))
    os.utime(ui / "src" / "main.tsx", (2_000, 2_000))
    monkeypatch.setattr(serve_commands, "_PACKAGED_UI_DIST", tmp_path / "nowhere")
    monkeypatch.setattr(serve_commands, "_UI_DIR", ui)

    mount = await mount_page(_FakeLoop(_FakeCron()), 18931)
    assert mount is not None
    try:
        async with aiohttp.ClientSession() as session:
            async with session.head(f"{mount.url}/") as resp:
                assert resp.status == 200
                assert resp.headers["X-Raven-Page-Behind"] == "sources"
                assert resp.headers["Cache-Control"] == "no-cache"
    finally:
        await mount.teardown()
