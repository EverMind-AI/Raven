"""Where the Mirasim host listens, and the one WebSocket the adapter keeps to it."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any

import pytest

from raven.acp.mirasim import host as host_mod
from raven.acp.mirasim.host import (
    DESKTOP_PORT,
    HOST_ENV,
    NO_HOST,
    HostAddress,
    HostConnection,
    HostUnavailableError,
    locate_host,
    mirasim_home,
    parse_address,
    port_token,
)
from tests.mirasim_fake_host import FakeHost

posix_only = pytest.mark.skipif(os.name == "nt", reason="a unix socket")


@pytest.fixture
def socket_dir() -> Any:
    """A directory short enough to bind a socket in: macOS caps a socket's path at 104 bytes, under its tmp_path."""
    import shutil
    import tempfile

    path = Path(tempfile.mkdtemp(prefix="mrs-", dir="/tmp" if os.path.isdir("/tmp") else None))
    yield path
    shutil.rmtree(path, ignore_errors=True)


@pytest.fixture
async def fake() -> Any:
    host = FakeHost(token="local-secret")
    await host.start()
    yield host
    await host.stop()


def write_token(home: Path, port: int, token: str) -> None:
    (home / "run").mkdir(parents=True, exist_ok=True)
    (home / "run" / f"local-{port}.token").write_text(f"{token}\n", encoding="utf-8")


# -- where the host is ----------------------------------------------------------


def test_the_named_address_wins_and_is_read_as_a_socket_or_a_loopback_port(tmp_path: Path) -> None:
    env = {HOST_ENV: f"unix:{tmp_path / 's.sock'}", "MIRASIM_HOME": str(tmp_path)}
    assert locate_host(env) == HostAddress(socket=str(tmp_path / "s.sock"))
    write_token(tmp_path, 4999, "abc")
    assert locate_host({**env, HOST_ENV: "4999"}) == HostAddress(port=4999, token="abc")
    assert parse_address("127.0.0.1:5001", env) == HostAddress(port=5001, token=None)
    assert parse_address(" localhost:5001 ", env).port == 5001


def test_a_loopback_host_named_in_the_address_is_the_one_connected_to() -> None:
    assert parse_address("[::1]:5001", {}).base_url == "http://[::1]:5001"
    assert parse_address("localhost:5001", {}).describe() == "localhost:5001"
    assert parse_address("5001", {}).base_url == "http://127.0.0.1:5001"
    assert parse_address("127.0.0.1:5001", {}) == HostAddress(port=5001)


@pytest.mark.parametrize("value", ["unix:", "example.com:4970", "4970x", "0", "70000", "192.168.1.2:4970"])
def test_an_address_that_is_not_one_is_refused(value: str) -> None:
    with pytest.raises(ValueError):
        parse_address(value, {})


@posix_only
def test_the_remote_hosts_socket_is_found_before_the_desktop_port(
    tmp_path: Path, socket_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import socket as socket_mod

    path = socket_dir / "server.sock"
    listener = socket_mod.socket(socket_mod.AF_UNIX)
    listener.bind(str(path))
    try:
        monkeypatch.setattr(host_mod, "remote_socket", lambda: path)
        write_token(tmp_path / "home", DESKTOP_PORT, "desk")
        assert locate_host({"MIRASIM_HOME": str(tmp_path / "home")}) == HostAddress(socket=str(path))
        # Where asyncio opens no unix socket, the socket is passed over for the port.
        monkeypatch.setattr(host_mod, "UNIX_SOCKETS", False)
        assert locate_host({"MIRASIM_HOME": str(tmp_path / "home")}) == HostAddress(port=DESKTOP_PORT, token="desk")
    finally:
        listener.close()


async def test_a_named_socket_where_asyncio_opens_none_is_unavailable_and_says_what_to_name_instead(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(host_mod, "UNIX_SOCKETS", False)
    conn = HostConnection(on_frame=lambda f: None, locate=lambda: HostAddress(socket="/run/server.sock"))
    with pytest.raises(HostUnavailableError, match=f"opens no unix socket.*port in {HOST_ENV}"):
        await conn.connect()
    assert not conn.connected


def test_without_a_socket_the_desktop_port_is_found_by_its_token_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(host_mod, "remote_socket", lambda: tmp_path / "absent.sock")
    env = {"MIRASIM_HOME": str(tmp_path)}
    assert locate_host(env) is None
    write_token(tmp_path, DESKTOP_PORT, "desk")
    assert locate_host(env) == HostAddress(port=DESKTOP_PORT, token="desk")


def test_the_state_directory_and_token_follow_mirasim_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    assert mirasim_home({}) == Path.home() / ".mirasim"
    assert mirasim_home({"MIRASIM_HOME": str(tmp_path / "m")}) == tmp_path / "m"
    write_token(tmp_path / "m", 1234, "")
    assert port_token(1234, {"MIRASIM_HOME": str(tmp_path / "m")}) is None
    monkeypatch.setenv("MIRASIM_HOME", str(tmp_path / "m"))
    assert mirasim_home() == tmp_path / "m"


def test_an_address_describes_itself_without_its_token() -> None:
    assert HostAddress(socket="/s").describe() == "unix socket /s"
    assert HostAddress(port=4970, token="t").describe() == "127.0.0.1:4970"
    assert "s3cr3t-token" not in repr(HostAddress(port=1, token="s3cr3t-token"))


# -- the connection -------------------------------------------------------------


async def test_a_request_is_answered_by_the_frame_its_predicate_takes(fake: FakeHost) -> None:
    seen: list[dict[str, Any]] = []
    conn = HostConnection(on_frame=seen.append, locate=lambda: fake.address)
    try:
        catalog = await conn.request(
            {"type": "getCatalog", "agent": "claude"}, lambda f: f.get("type") == "catalog", timeout=5
        )
        assert catalog["defaultModel"] == "sonnet"
        assert conn.version == "0.0.537"
        assert conn.address == fake.address
        # The host was asked to merge patches before anything else, and the token travelled.
        assert [frame["type"] for frame in fake.received] == ["setStreamCadence", "getCatalog"]
        assert any(frame.get("type") == "catalog" for frame in seen)
    finally:
        await conn.close()


async def test_an_address_nothing_listens_on_is_unavailable_and_says_where() -> None:
    import socket as socket_mod

    with socket_mod.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    conn = HostConnection(on_frame=lambda f: None, locate=lambda: HostAddress(port=port, token="t"))
    with pytest.raises(HostUnavailableError, match=f"cannot reach the Mirasim host at 127.0.0.1:{port}"):
        await conn.connect()
    assert not conn.connected


async def test_a_host_that_answers_its_health_but_holds_the_websocket_is_given_up_in_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from aiohttp import web

    monkeypatch.setattr(host_mod, "CONNECT_TIMEOUT_S", 0.3)
    opened: list[Any] = []
    real = host_mod.aiohttp.ClientSession

    def recording(*args: Any, **kwargs: Any) -> Any:
        opened.append(real(*args, **kwargs))
        return opened[-1]

    monkeypatch.setattr(host_mod.aiohttp, "ClientSession", recording)
    held = asyncio.Event()

    async def health(request: web.Request) -> web.Response:
        return web.json_response({"ok": True, "version": "0.0.537"})

    async def upgrade(request: web.Request) -> web.Response:
        held.set()
        await asyncio.sleep(3600)
        return web.Response()

    app = web.Application()
    app.router.add_get("/api/health", health)
    app.router.add_get("/ws", upgrade)
    runner = web.AppRunner(app, shutdown_timeout=0.1)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]  # type: ignore[union-attr]
    try:
        conn = HostConnection(on_frame=lambda f: None, locate=lambda: HostAddress(port=port))
        with pytest.raises(HostUnavailableError, match=f"cannot reach the Mirasim host at 127.0.0.1:{port}"):
            await asyncio.wait_for(conn.connect(), 5)
        assert held.is_set() and opened and all(session.closed for session in opened)
        # A connect given up from outside while the upgrade hangs closes what it opened as well.
        monkeypatch.setattr(host_mod, "CONNECT_TIMEOUT_S", 30)
        held.clear()
        attempt = asyncio.ensure_future(conn.connect())
        await asyncio.wait_for(held.wait(), 5)
        attempt.cancel()
        with pytest.raises(asyncio.CancelledError):
            await attempt
        assert len(opened) == 2 and all(session.closed for session in opened)
    finally:
        await runner.cleanup()


@posix_only
async def test_a_socket_its_host_left_behind_is_unavailable(socket_dir: Path) -> None:
    import socket as socket_mod

    path = socket_dir / "server.sock"
    with socket_mod.socket(socket_mod.AF_UNIX) as gone:
        gone.bind(str(path))
    conn = HostConnection(on_frame=lambda f: None, locate=lambda: HostAddress(socket=str(path)))
    with pytest.raises(HostUnavailableError, match=f"cannot reach the Mirasim host at unix socket {path}"):
        await conn.connect()


async def test_a_wrong_token_or_no_host_is_unavailable(fake: FakeHost, tmp_path: Path) -> None:
    assert fake.address is not None
    conn = HostConnection(on_frame=lambda f: None, locate=lambda: HostAddress(port=fake.address.port, token="wrong"))
    with pytest.raises(HostUnavailableError, match="cannot reach the Mirasim host at 127.0.0.1"):
        await conn.connect()
    nowhere = HostConnection(on_frame=lambda f: None, locate=lambda: None)
    with pytest.raises(HostUnavailableError) as raised:
        await nowhere.send({"type": "ping"})
    assert str(raised.value) == NO_HOST
    malformed = HostConnection(on_frame=lambda f: None, locate=lambda: parse_address("nope", {}))
    with pytest.raises(HostUnavailableError, match="names no port"):
        await malformed.connect()


@pytest.mark.parametrize("health", [False, "not json"])
async def test_a_host_that_cannot_say_its_version_is_still_one_to_talk_to(fake: FakeHost, health: bool | str) -> None:
    fake.health = health
    conn = HostConnection(on_frame=lambda f: None, locate=lambda: fake.address)
    try:
        await conn.connect()
        assert conn.connected and conn.version is None
    finally:
        await conn.close()


async def test_frames_that_are_not_json_objects_and_a_failing_handler_do_not_end_the_connection(fake: FakeHost) -> None:
    calls: list[str] = []

    def handler(frame: dict[str, Any]) -> None:
        calls.append(frame.get("type"))
        raise RuntimeError("handler broke")

    conn = HostConnection(on_frame=handler, locate=lambda: fake.address)
    try:
        await conn.connect()
        client = fake.clients[0]
        await client.ws.send_str("not json")
        await client.ws.send_str("[1, 2]")
        await client.ws.send_bytes(b"binary")
        with conn.listen(lambda f: f.get("type") == "catalog") as frames:
            await conn.send({"type": "getCatalog", "agent": "codex"})
            got = await asyncio.wait_for(frames.get(), 5)
        assert isinstance(got, dict) and got["agent"] == "codex"
        assert calls == ["catalog"]
        assert conn.connected
    finally:
        await conn.close()


async def test_a_predicate_that_raises_takes_nothing_and_the_other_listeners_still_get_the_frame(
    fake: FakeHost,
) -> None:
    conn = HostConnection(on_frame=lambda f: None, locate=lambda: fake.address)

    def broken(frame: dict[str, Any]) -> bool:
        raise KeyError("nope")

    try:
        with conn.listen(lambda f: f.get("type") == "catalog") as frames:
            with pytest.raises(TimeoutError):
                await conn.request({"type": "getCatalog", "agent": "claude"}, broken, timeout=0.3)
            got = await asyncio.wait_for(frames.get(), 5)
        assert isinstance(got, dict) and got["agent"] == "claude"
    finally:
        await conn.close()


async def test_a_dropped_connection_fails_what_waits_and_comes_back_while_it_is_wanted(fake: FakeHost) -> None:
    lost: list[bool] = []
    connected: list[bool] = []

    async def on_connect() -> None:
        connected.append(True)

    conn = HostConnection(
        on_frame=lambda f: None,
        locate=lambda: fake.address,
        on_connect=on_connect,
        on_lost=lambda: lost.append(True),
        wanted=lambda: True,
    )
    try:
        await conn.connect()
        fake.silent.add("getCatalog")
        waiting = asyncio.ensure_future(
            conn.request({"type": "getCatalog", "agent": "claude"}, lambda f: f.get("type") == "catalog", timeout=10)
        )
        await fake.wait_for(lambda: fake.frames("getCatalog"))
        await fake.drop()
        with pytest.raises(HostUnavailableError, match="dropped"):
            await waiting
        assert lost == [True]
        assert conn.lost_at is not None
        await fake.wait_for(lambda: conn.connected and len(connected) == 2)
        assert conn.lost_at is None
    finally:
        await conn.close()


async def test_a_connection_nothing_depends_on_is_made_again_only_when_used(fake: FakeHost) -> None:
    conn = HostConnection(on_frame=lambda f: None, locate=lambda: fake.address)
    try:
        await conn.connect()
        await fake.drop()
        await fake.wait_for(lambda: not conn.connected)
        await asyncio.sleep(0.3)
        assert not conn.connected
        await conn.send({"type": "ping"})
        assert conn.connected
    finally:
        await conn.close()


async def test_a_closed_connection_stays_closed(fake: FakeHost) -> None:
    conn = HostConnection(on_frame=lambda f: None, locate=lambda: fake.address)
    await conn.connect()
    await conn.close()
    with pytest.raises(HostUnavailableError, match="closed"):
        await conn.connect()
    await conn.close()


async def test_a_frame_cannot_be_sent_on_a_connection_that_is_gone(fake: FakeHost) -> None:
    conn = HostConnection(on_frame=lambda f: None, locate=lambda: fake.address)
    try:
        with pytest.raises(HostUnavailableError, match="is closed"):
            await conn._send({"type": "ping"})
        await conn.connect()

        class Broken:
            closed = False

            async def send_str(self, data: str) -> None:
                raise ConnectionResetError("reset")

        real = conn._ws
        conn._ws = Broken()  # type: ignore[assignment]
        with pytest.raises(HostUnavailableError, match="could not be sent"):
            await conn._send({"type": "ping"})
        conn._ws = real
    finally:
        await conn.close()


@posix_only
async def test_the_host_is_reached_over_a_unix_socket_with_no_token(socket_dir: Path) -> None:
    host = FakeHost()
    address = await host.start(socket=socket_dir / "server.sock")
    conn = HostConnection(on_frame=lambda f: None, locate=lambda: address)
    try:
        answer = await conn.request(
            {"type": "listSessions", "reqId": "r1", "workdir": "/w"},
            lambda f: f.get("type") == "sessions" and f.get("reqId") == "r1",
            timeout=5,
        )
        assert answer["sessions"] == []
        assert conn.address == HostAddress(socket=str(socket_dir / "server.sock"))
    finally:
        await conn.close()
        await host.stop()
