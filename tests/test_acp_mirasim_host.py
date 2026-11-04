"""Where the Mirasim host listens, and the one WebSocket the adapter keeps to it."""

from __future__ import annotations

import asyncio
import os
import traceback
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
    upgrade_url,
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


def dead_port() -> int:
    """A loopback port nothing listens on: bound, then let go."""
    import socket as socket_mod

    with socket_mod.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


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


def test_every_port_a_token_file_names_is_a_place_to_try_the_desktop_app_and_mirasim_serve_first(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A host writes `run/local-<port>.token` for the port it bound, and `mirasim serve` binds the next one
    when 4939 is taken; a killed host leaves its file behind. So each file is a place to try, not a host."""
    monkeypatch.setattr(host_mod, "remote_socket", lambda: tmp_path / "absent.sock")
    env = {"MIRASIM_HOME": str(tmp_path)}
    assert host_mod.locate_hosts(env) == []
    for port, token in (
        (36925, "old"),
        (4939, "serve"),
        (4940, "moved"),
        (DESKTOP_PORT, "desk"),
        (3000, "dev"),
        (4100, " "),
    ):
        write_token(tmp_path, port, token)
    for other in ("local-x.token", "local-0.token", "local-70000.token", "space-api.env"):
        (tmp_path / "run" / other).write_text("x\n", encoding="utf-8")
    assert host_mod.SERVE_PORT == 4939
    assert host_mod.token_ports(env) == [DESKTOP_PORT, 4939, 3000, 4940, 36925]
    assert host_mod.locate_hosts(env) == [
        HostAddress(port=DESKTOP_PORT, token="desk"),
        HostAddress(port=4939, token="serve"),
        HostAddress(port=3000, token="dev"),
        HostAddress(port=4940, token="moved"),
        HostAddress(port=36925, token="old"),
    ]
    assert locate_host(env) == HostAddress(port=DESKTOP_PORT, token="desk")
    (tmp_path / "run" / f"local-{DESKTOP_PORT}.token").unlink()
    assert locate_host(env) == HostAddress(port=4939, token="serve")


def test_a_place_a_token_file_names_is_marked_as_found_and_a_named_one_is_not(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(host_mod, "remote_socket", lambda: tmp_path / "absent.sock")
    write_token(tmp_path, 4939, "serve")
    env = {"MIRASIM_HOME": str(tmp_path)}
    assert [(place.port, place.discovered) for place in host_mod.locate_hosts(env)] == [(4939, True)]
    assert parse_address("4939", env).discovered is False


@posix_only
def test_a_named_host_or_the_remote_hosts_socket_is_the_only_place_to_try(
    tmp_path: Path, socket_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import socket as socket_mod

    env = {"MIRASIM_HOME": str(tmp_path)}
    write_token(tmp_path, 4939, "serve")
    assert host_mod.locate_hosts({**env, HOST_ENV: "5001"}) == [HostAddress(port=5001)]
    path = socket_dir / "server.sock"
    listener = socket_mod.socket(socket_mod.AF_UNIX)
    listener.bind(str(path))
    try:
        monkeypatch.setattr(host_mod, "remote_socket", lambda: path)
        assert host_mod.locate_hosts(env) == [HostAddress(socket=str(path))]
    finally:
        listener.close()


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
    port = dead_port()
    conn = HostConnection(on_frame=lambda f: None, locate=lambda: HostAddress(port=port, token="t"))
    with pytest.raises(HostUnavailableError) as raised:
        await conn.connect()
    assert str(raised.value) == f"cannot reach the Mirasim host at 127.0.0.1:{port}: nothing listens there"
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


@posix_only
@pytest.mark.parametrize("left", ["socket file", "nothing"])
async def test_a_socket_nothing_listens_on_says_so(socket_dir: Path, left: str) -> None:
    import socket as socket_mod

    path = socket_dir / "server.sock"
    if left == "socket file":
        with socket_mod.socket(socket_mod.AF_UNIX) as gone:
            gone.bind(str(path))
    conn = HostConnection(on_frame=lambda f: None, locate=lambda: HostAddress(socket=str(path)))
    with pytest.raises(HostUnavailableError) as raised:
        await conn.connect()
    assert str(raised.value) == f"cannot reach the Mirasim host at unix socket {path}: nothing listens there"


@pytest.mark.parametrize("token_in", ["query", "header"])
async def test_the_token_goes_in_the_upgrades_query_as_the_host_reads_it_and_in_the_header_earlier_hosts_read(
    token_in: str,
) -> None:
    """A host from 0.0.571 on reads the token off the upgrade's query and answers a header alone with 401
    (measured against 0.0.571, 0.0.576 and 0.0.585, and how Mirasim's own clients send it); the hosts this
    adapter was written against read the header. One request satisfies both."""
    host = FakeHost(token="local-secret", token_in=token_in)
    address = await host.start()
    try:
        conn = HostConnection(on_frame=lambda f: None, locate=lambda: address)
        await conn.connect()
        assert conn.connected
        assert host.upgrades == [{"header": "local-secret", "query": "local-secret"}]
        await conn.close()
    finally:
        await host.stop()


def test_the_upgrade_url_carries_the_token_url_encoded_and_nothing_without_one() -> None:
    assert upgrade_url(HostAddress(port=4970, token="a b/c&d=e")) == "http://127.0.0.1:4970/ws?token=a%20b%2Fc%26d%3De"
    assert upgrade_url(HostAddress(port=4970)) == "http://127.0.0.1:4970/ws"
    assert upgrade_url(HostAddress(socket="/run/s.sock")).endswith("/ws")


async def test_a_refused_upgrade_names_the_token_file_and_never_the_token_or_the_url(fake: FakeHost) -> None:
    """aiohttp's own words for a refused upgrade quote the request's URL, which now carries the token."""
    from raven.acp_client.capabilities import looks_like_auth

    assert fake.address is not None
    conn = HostConnection(on_frame=lambda f: None, locate=lambda: HostAddress(port=fake.address.port, token="wrong"))
    with pytest.raises(HostUnavailableError) as raised:
        await conn.connect()
    said = str(raised.value)
    assert said.startswith(
        f"cannot reach the Mirasim host at 127.0.0.1:{fake.address.port}: it turned this connection away"
    )
    assert f"run/local-{fake.address.port}.token" in said and "Start Mirasim again" in said
    assert "wrong" not in said and "url=" not in said and "/ws" not in said
    # Not in the chain either: the refused upgrade's own exception holds the request, URL and token
    # included, and a formatted traceback prints a cause and a context.
    assert raised.value.__cause__ is None and raised.value.__context__ is None
    chain = "".join(traceback.format_exception(raised.value))
    assert "wrong" not in chain and "token=" not in chain and "/ws" not in chain
    # The token is Raven's way in to the host, not the agent's sign-in: the roster must not send its reader
    # to sign in to Claude Code over it. Judged on the wording alone, with the port masked: the fake host's
    # port is random, and `_AUTH_HINTS` matches the bare digits `401`, which a port such as 40123 contains.
    assert not looks_like_auth(said.replace(str(fake.address.port), "<port>"))


async def test_a_host_that_is_not_there_keeps_its_cause_which_names_no_url() -> None:
    """Only a refused upgrade is cut off; a refused connection is kept for whoever reads the chain."""
    import socket

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    conn = HostConnection(on_frame=lambda f: None, locate=lambda: HostAddress(port=port, token="s3cret"))
    with pytest.raises(HostUnavailableError) as raised:
        await conn.connect()
    assert isinstance(raised.value.__cause__, OSError)
    chain = "".join(traceback.format_exception(raised.value))
    assert "s3cret" not in chain and "token=" not in chain


async def test_another_refusal_of_the_upgrade_is_worded_from_its_status_without_the_token(fake: FakeHost) -> None:
    import aiohttp

    assert fake.address is not None
    address = HostAddress(port=fake.address.port, token="s3cret")
    gone = aiohttp.ClientResponseError(
        request_info=None,  # type: ignore[arg-type]
        history=(),
        status=503,
        message="busy, see http://127.0.0.1/ws?token=s3cret",
    )
    assert (
        host_mod._reason(gone, address) == "its upgrade answered HTTP 503: busy, see http://127.0.0.1/ws?token=<token>"
    )
    assert host_mod._reason(OSError("no route to s3cret"), address) == "no route to <token>"
    assert host_mod._reason(TimeoutError(), address) == "it did not answer in time"


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


async def test_the_first_place_that_answers_is_the_host_connected_to(
    fake: FakeHost, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A desktop app that was killed leaves its token file, which no longer hides a host running elsewhere."""
    assert fake.address is not None and fake.address.port is not None
    monkeypatch.setattr(host_mod, "remote_socket", lambda: tmp_path / "absent.sock")
    monkeypatch.setattr(host_mod, "DESKTOP_PORT", dead_port())
    env = {"MIRASIM_HOME": str(tmp_path)}
    write_token(tmp_path, host_mod.DESKTOP_PORT, "stale")
    write_token(tmp_path, fake.address.port, "local-secret")
    assert host_mod.token_ports(env)[0] == host_mod.DESKTOP_PORT
    conn = HostConnection(on_frame=lambda f: None, locate=lambda: host_mod.locate_hosts(env))
    try:
        await conn.connect()
        assert conn.connected and conn.address == fake.address and conn.version == "0.0.537"
    finally:
        await conn.close()


async def test_a_connection_told_nowhere_tries_every_place_its_environment_names(
    fake: FakeHost, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert fake.address is not None and fake.address.port is not None
    monkeypatch.delenv(HOST_ENV, raising=False)
    monkeypatch.setenv("MIRASIM_HOME", str(tmp_path))
    monkeypatch.setattr(host_mod, "remote_socket", lambda: tmp_path / "absent.sock")
    monkeypatch.setattr(host_mod, "DESKTOP_PORT", dead_port())
    write_token(tmp_path, host_mod.DESKTOP_PORT, "stale")
    write_token(tmp_path, fake.address.port, "local-secret")
    conn = HostConnection(on_frame=lambda f: None)
    try:
        await conn.connect()
        assert conn.address == fake.address
    finally:
        await conn.close()


async def test_a_host_that_comes_back_on_another_port_is_found_there(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every connect looks again, so the reconnect after a restart follows the token file to the new port."""
    monkeypatch.setattr(host_mod, "remote_socket", lambda: tmp_path / "absent.sock")
    env = {"MIRASIM_HOME": str(tmp_path)}
    before, after = FakeHost(token="one"), FakeHost(token="two")
    first = await before.start()
    assert first.port is not None
    write_token(tmp_path, first.port, "one")
    conn = HostConnection(on_frame=lambda f: None, locate=lambda: host_mod.locate_hosts(env), wanted=lambda: True)
    try:
        await conn.connect()
        assert conn.address == first
        second = await after.start()
        assert second.port is not None
        (tmp_path / "run" / f"local-{first.port}.token").unlink()
        write_token(tmp_path, second.port, "two")
        await before.stop()
        await after.wait_for(lambda: conn.connected and after.clients)
        assert conn.address == second
    finally:
        await conn.close()
        await after.stop()


async def test_a_place_a_token_file_names_is_used_once_its_health_names_mirasim(fake: FakeHost) -> None:
    assert fake.address is not None
    found = HostAddress(port=fake.address.port, token=fake.address.token, discovered=True)
    conn = HostConnection(on_frame=lambda f: None, locate=lambda: found)
    try:
        await conn.connect()
        assert conn.connected
    finally:
        await conn.close()


@pytest.mark.parametrize("answer", ["not-there", "not-json", "no-name", "unavailable"])
async def test_a_place_a_token_file_names_is_left_alone_unless_its_health_names_mirasim(answer: str) -> None:
    """A killed host's port may be any program's by now; only a host that says it is one gets the token."""
    from aiohttp import web

    seen: list[str] = []

    async def health(request: web.Request) -> web.Response:
        seen.append(request.path_qs)
        if answer == "not-there":
            return web.Response(status=404)
        if answer == "unavailable":
            return web.Response(status=503)
        if answer == "not-json":
            return web.Response(text="not json at all")
        return web.json_response({"ok": True})

    async def anything_else(request: web.Request) -> web.Response:
        seen.append(request.path_qs)
        return web.Response(status=404)

    app = web.Application()
    app.router.add_get("/api/health", health)
    app.router.add_route("*", "/{rest:.*}", anything_else)
    runner = web.AppRunner(app, shutdown_timeout=0.1)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]  # type: ignore[union-attr]
    found = HostAddress(port=port, token="s3cret-stale-token", discovered=True)
    try:
        conn = HostConnection(on_frame=lambda f: None, locate=lambda: found)
        with pytest.raises(HostUnavailableError) as raised:
            await conn.connect()
        assert str(raised.value) == (
            f"cannot reach the Mirasim host at 127.0.0.1:{port}: it did not identify itself as a Mirasim host"
        )
        assert seen == ["/api/health"]
        assert not conn.connected
    finally:
        await runner.cleanup()


async def test_a_place_whose_health_names_another_service_is_not_a_mirasim_host_and_never_sees_the_token(
    fake: FakeHost,
) -> None:
    """A killed host's port may since be any service's. `/api/health` needs no token, so the probe sends none."""
    from aiohttp import web

    seen: list[str] = []

    async def health(request: web.Request) -> web.Response:
        seen.append(f"{request.path_qs} {sorted(request.headers.items())}")
        return web.json_response({"ok": True, "name": "grafana", "version": "11.2.0"})

    async def anything_else(request: web.Request) -> web.Response:
        seen.append(f"{request.path_qs} {sorted(request.headers.items())}")
        return web.Response(status=404)

    app = web.Application()
    app.router.add_get("/api/health", health)
    app.router.add_route("*", "/{rest:.*}", anything_else)
    runner = web.AppRunner(app, shutdown_timeout=0.1)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]  # type: ignore[union-attr]
    other = HostAddress(port=port, token="s3cret-stale-token", discovered=True)
    try:
        alone = HostConnection(on_frame=lambda f: None, locate=lambda: other)
        with pytest.raises(HostUnavailableError) as raised:
            await alone.connect()
        assert str(raised.value) == (
            f"cannot reach the Mirasim host at 127.0.0.1:{port}: it is not a Mirasim host: "
            "its /api/health names 'grafana'"
        )
        conn = HostConnection(on_frame=lambda f: None, locate=lambda: [other, fake.address])
        try:
            await conn.connect()
            assert conn.address == fake.address
        finally:
            await conn.close()
        assert len(seen) == 2 and not any("s3cret" in request for request in seen)
    finally:
        await runner.cleanup()


async def test_when_no_place_answers_each_is_named_with_why_and_never_with_its_token(fake: FakeHost) -> None:
    assert fake.address is not None
    gone = HostAddress(port=dead_port(), token="s3cret-one")
    refused = HostAddress(port=fake.address.port, token="s3cret-two")
    conn = HostConnection(on_frame=lambda f: None, locate=lambda: [gone, refused])
    with pytest.raises(HostUnavailableError) as raised:
        await conn.connect()
    said = str(raised.value)
    assert said.startswith("cannot reach a Mirasim host at any of the 2 places it may listen: ")
    assert f"127.0.0.1:{gone.port} (nothing listens there); 127.0.0.1:{refused.port} (it turned this connection" in said
    assert said.endswith(f"or name where the host listens in {HOST_ENV}")
    assert raised.value.__cause__ is None and raised.value.__context__ is None
    chain = "".join(traceback.format_exception(raised.value))
    assert "s3cret" not in chain and "token=" not in chain and "/ws" not in chain
    assert not conn.connected


@pytest.mark.parametrize("health", [False, "not json"])
async def test_an_explicitly_selected_host_that_cannot_say_its_version_is_still_one_to_talk_to(
    fake: FakeHost, health: bool | str
) -> None:
    """Compatibility: a host selected outright is tried as before, its health notwithstanding."""
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
