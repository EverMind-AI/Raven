"""Tests for rpc system.* handlers, dispatcher, and error mapping.

Covers:
- system.hello / system.ping / system.version handler contracts
- Dispatcher JSON-RPC 2.0 framing & error mapping (-32600, -32601, -32700, -32603)
- ConfigValidationError (-32011) raised for non-semver client_version

These tests treat handlers as `async def handler(params: dict) -> dict` and the
dispatcher as `async def dispatch(frame: dict) -> dict`. Pydantic validation
runs inside handlers (validation errors bubble up as RpcError subclasses).
"""

from __future__ import annotations

import time

import pytest

from raven.rpc.dispatcher import Dispatcher
from raven.rpc.errors import ConfigValidationError
from raven.rpc.methods.system import (
    register_system_methods,
    system_hello,
    system_ping,
    system_version,
)

# ---------------------------------------------------------------------------
# system.* handler tests (direct calls, no dispatcher)
# ---------------------------------------------------------------------------


async def test_hello_returns_versions():
    result = await system_hello({"client_version": "0.1.0"})
    assert "server_version" in result
    assert isinstance(result["server_version"], str)
    assert "server_capabilities" in result
    assert isinstance(result["server_capabilities"], list)
    assert "jsonrpc-2.0" in result["server_capabilities"]
    assert "session" in result
    assert result["session"]["default_channel"] == "tui"
    assert result["session"]["default_session_key"].startswith("tui:")


async def test_hello_rejects_invalid_semver():
    with pytest.raises(ConfigValidationError):
        await system_hello({"client_version": "not-a-semver"})


async def test_hello_rejects_missing_client_version():
    with pytest.raises(ConfigValidationError):
        await system_hello({})


async def test_ping_returns_server_time():
    before = int(time.time() * 1000)
    result = await system_ping({})
    after = int(time.time() * 1000)
    assert result["pong"] is True
    assert isinstance(result["server_time_ms"], int)
    # Should be within the window we measured
    assert before - 1000 <= result["server_time_ms"] <= after + 1000


async def test_version_returns_three_fields():
    result = await system_version({})
    assert "server_version" in result
    assert "schema_version" in result
    assert "raven_version" in result
    assert all(isinstance(result[k], str) for k in ("server_version", "schema_version", "raven_version"))


# ---------------------------------------------------------------------------
# Dispatcher framing / error mapping
# ---------------------------------------------------------------------------


def _build_dispatcher() -> Dispatcher:
    d = Dispatcher()
    register_system_methods(d)
    return d


async def test_dispatcher_hello_happy_path():
    d = _build_dispatcher()
    frame = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "system.hello",
        "params": {"client_version": "0.1.0"},
    }
    resp = await d.dispatch(frame)
    assert resp["jsonrpc"] == "2.0"
    assert resp["id"] == 1
    assert "result" in resp
    assert "error" not in resp
    assert resp["result"]["server_capabilities"]


async def test_dispatcher_unknown_method():
    d = _build_dispatcher()
    frame = {
        "jsonrpc": "2.0",
        "id": 2,
        "method": "no.such.method",
        "params": {},
    }
    resp = await d.dispatch(frame)
    assert "error" in resp
    assert resp["error"]["code"] == -32601  # method_not_found
    assert resp["id"] == 2


async def test_dispatcher_invalid_jsonrpc_version():
    d = _build_dispatcher()
    frame = {
        "jsonrpc": "1.0",
        "id": 3,
        "method": "system.ping",
        "params": {},
    }
    resp = await d.dispatch(frame)
    assert "error" in resp
    assert resp["error"]["code"] == -32600  # invalid_request


async def test_dispatcher_missing_method_field():
    d = _build_dispatcher()
    frame = {"jsonrpc": "2.0", "id": 4}
    resp = await d.dispatch(frame)
    assert "error" in resp
    assert resp["error"]["code"] == -32600


async def test_dispatcher_validation_error_maps_to_32011():
    d = _build_dispatcher()
    frame = {
        "jsonrpc": "2.0",
        "id": 5,
        "method": "system.hello",
        "params": {"client_version": "not-semver"},
    }
    resp = await d.dispatch(frame)
    assert "error" in resp
    assert resp["error"]["code"] == -32011  # config_validation_error
    assert resp["error"]["message"] == "config_validation_error"


async def test_dispatcher_internal_error_maps_to_32603():
    d = Dispatcher()

    async def boom(params: dict) -> dict:
        raise RuntimeError("kaboom")

    d.register("test.boom", boom)
    frame = {"jsonrpc": "2.0", "id": 6, "method": "test.boom", "params": {}}
    resp = await d.dispatch(frame)
    assert "error" in resp
    assert resp["error"]["code"] == -32603  # internal_error
    # Traceback tail should be included for debuggability
    assert "data" in resp["error"]
    assert "traceback_tail" in resp["error"]["data"]


async def test_dispatcher_parse_response_id_echoed():
    d = _build_dispatcher()
    frame = {
        "jsonrpc": "2.0",
        "id": "string-id-abc",
        "method": "system.ping",
        "params": {},
    }
    resp = await d.dispatch(frame)
    assert resp["id"] == "string-id-abc"
    assert resp["result"]["pong"] is True


# ---------------------------------------------------------------------------
# system.version update hint / system.upgrade
# ---------------------------------------------------------------------------


async def test_version_reports_a_pending_upgrade(monkeypatch: pytest.MonkeyPatch):
    from raven.cli import update_notice as un

    monkeypatch.setattr(un, "update_notice", lambda _cur: (True, "raven upgrade"))
    monkeypatch.setattr(un, "read_cache", lambda: {"latest_version": "9.9.9"})

    result = await system_version({})

    assert result["update_available"] is True
    assert result["latest_version"] == "9.9.9"


async def test_version_stays_quiet_without_a_cached_release(monkeypatch: pytest.MonkeyPatch):
    from raven.cli import update_notice as un

    monkeypatch.setattr(un, "update_notice", lambda _cur: None)

    result = await system_version({})

    assert "update_available" not in result
    assert "latest_version" not in result


async def test_upgrade_refuses_outside_serve():
    from raven.cli.serve_commands import SERVE
    from raven.rpc.methods.system import system_upgrade

    assert not SERVE.running
    with pytest.raises(ConfigValidationError) as excinfo:
        await system_upgrade({})
    assert excinfo.value.data["reason"] == "not_serving"


async def test_upgrade_refuses_on_a_gateway_hosted_page():
    """The relaunch flow replaces this process with a bare `raven serve`. On a
    page the gateway hosts that would silently drop the IM channels, so until
    the gateway can restart in place the page gets a structured refusal the
    front end already renders (reason + human sentence)."""
    from raven.cli.serve_commands import SERVE
    from raven.rpc.methods.system import system_upgrade

    SERVE.arm_hosted(18792, "tok", "cookie")
    try:
        with pytest.raises(ConfigValidationError) as excinfo:
            await system_upgrade({})
    finally:
        SERVE.disarm()
    assert excinfo.value.data["reason"] == "gateway_hosted"
    assert "raven gateway" in excinfo.value.data["detail"]


async def test_upgrade_refuses_when_the_install_cannot_self_upgrade(monkeypatch: pytest.MonkeyPatch):
    import asyncio

    from raven.cli import upgrade_commands
    from raven.cli.serve_commands import SERVE
    from raven.rpc.methods.system import system_upgrade

    def refuse() -> None:
        raise upgrade_commands.UpgradeError("Editable Raven installations cannot be upgraded automatically")

    monkeypatch.setattr(upgrade_commands, "plan_upgrade", refuse)
    SERVE.arm(18792, "tok", "cookie", asyncio.Event())
    try:
        with pytest.raises(ConfigValidationError) as excinfo:
            await system_upgrade({})
    finally:
        SERVE.disarm()
    assert excinfo.value.data["reason"] == "not_upgradable"


async def test_upgrade_names_an_install_already_in_flight(monkeypatch: pytest.MonkeyPatch):
    """uv removes the old environment before writing the new one, so a check
    landing in that window finds no metadata for `raven`. Reported as a generic
    check failure it reads as a broken install, and the reader starts a second
    upgrade into the same gap."""
    import asyncio
    import importlib.metadata as md

    from raven.cli import upgrade_commands
    from raven.cli.serve_commands import SERVE
    from raven.rpc.methods.system import system_upgrade

    def mid_replacement() -> None:
        raise md.PackageNotFoundError("raven")

    monkeypatch.setattr(upgrade_commands, "plan_upgrade", mid_replacement)
    SERVE.arm(18792, "tok", "cookie", asyncio.Event())
    try:
        with pytest.raises(ConfigValidationError) as excinfo:
            await system_upgrade({})
    finally:
        SERVE.disarm()
    assert excinfo.value.data["reason"] == "in_progress"


async def test_upgrade_hands_off_then_stops_the_gateway(monkeypatch: pytest.MonkeyPatch, tmp_path):
    import asyncio

    from raven.cli import upgrade_commands
    from raven.cli.serve_commands import SERVE
    from raven.rpc.methods.system import system_upgrade

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    plan = upgrade_commands.UpgradePlan(
        current_version="0.1.3",
        release=upgrade_commands.ReleaseInfo(version="0.1.4", wheel_url="https://example.invalid/x.whl"),
        target=upgrade_commands.ToolInstallTarget(tool_dir=tmp_path / "tools", bin_dir=bin_dir),
    )
    monkeypatch.setattr(upgrade_commands, "plan_upgrade", lambda: plan)
    spawned: dict[str, object] = {}
    monkeypatch.setattr(
        upgrade_commands,
        "spawn_detached_upgrade",
        lambda p, **kw: spawned.update({"plan": p, **kw}),
    )

    stop = asyncio.Event()
    SERVE.arm(18792, "tok", "cookie", stop)
    try:
        result = await system_upgrade({})
        assert result == {
            "status": "started",
            "from_version": "0.1.3",
            "to_version": "0.1.4",
            "relaunch": True,
        }
        # The reply must land before the gateway goes down.
        assert not stop.is_set()
        await asyncio.sleep(0.9)
        assert stop.is_set()
    finally:
        SERVE.disarm()

    assert spawned["relaunch"] == [str(bin_dir / "raven"), "serve", "--port", "18792"]
    # Both credentials travel, and that is the point: the browser presents the
    # cookie, a relauncher needs the shared secret, and since they are no longer
    # the same string, carrying only one of them would sign the open page out.
    assert spawned["extra_env"] == {
        "RAVEN_SERVE_PORT_STRICT": "1",
        "RAVEN_SERVE_TOKEN": "tok",
        "RAVEN_SERVE_COOKIE": "cookie",
    }
    assert spawned["parent_pid"] > 0


# ---------------------------------------------------------------------------
# The handshake reports the channel the dispatcher was actually built for
# ---------------------------------------------------------------------------


async def test_hello_defaults_to_the_shared_local_channel() -> None:
    """The terminal and the served page share one session pool, so they share
    one channel: a browser that opened `raven serve` finds the conversations it
    started in the terminal."""
    from raven.rpc import LOCAL_CHANNEL

    result = await system_hello({"client_version": "0.1.0"})

    assert result["session"]["default_channel"] == LOCAL_CHANNEL
    assert result["session"]["default_session_key"] == f"{LOCAL_CHANNEL}:default"


async def test_hello_reports_the_channel_it_was_registered_with() -> None:
    """The web gateway registers these same handlers and runs its turns on
    ``channel="web"`` (gateway_commands passes it to register_turn_methods). The
    handshake used to answer the literal "tui" regardless, so a web client was
    handed the terminal's channel and the terminal's default session key -- for a
    pool it does not share."""
    dispatcher = Dispatcher()
    register_system_methods(dispatcher, channel="web")

    response = await dispatcher.dispatch(
        {"jsonrpc": "2.0", "id": 1, "method": "system.hello", "params": {"client_version": "0.1.0"}}
    )

    assert response["result"]["session"] == {
        "default_channel": "web",
        "default_session_key": "web:default",
    }


async def test_version_check_true_fetches_before_answering(monkeypatch) -> None:
    """The button labelled "check for updates" must ask about now, not about the
    last daily poll -- a tester clicks it seconds after being told a build was
    published, and a cached "up to date" there reads as a broken channel."""
    from raven.cli import update_notice
    from raven.rpc.methods import system as system_mod

    fetched = {"n": 0}
    monkeypatch.setattr(update_notice, "check_for_update", lambda current: fetched.__setitem__("n", fetched["n"] + 1))

    await system_mod.system_version({})
    assert fetched["n"] == 0, "the plain call must stay off the network"

    await system_mod.system_version({"check": True})
    assert fetched["n"] == 1


# ---------------------------------------------------------------------------
# The handshake may name the front end behind this connection
# ---------------------------------------------------------------------------


async def test_hello_declares_the_surface_on_the_connection() -> None:
    """One gateway serves the page, the shell, and relayed terminals at once,
    so the declaration lands on the connection's own state, never process-wide."""
    from raven.rpc import connection

    token = connection.bind_connection()
    try:
        await system_hello({"client_version": "0.1.0", "surface": "shell"})
        assert connection.declared_surface() == "shell"
    finally:
        connection.unbind_connection(token)


async def test_hello_without_a_surface_declares_nothing() -> None:
    from raven.rpc import connection

    token = connection.bind_connection()
    try:
        await system_hello({"client_version": "0.1.0"})
        assert connection.declared_surface() is None
    finally:
        connection.unbind_connection(token)


@pytest.mark.parametrize("bad", ["Not A Surface", "", "x" * 33, 5, {"name": "page"}])
async def test_hello_rejects_a_malformed_surface(bad) -> None:
    """The value is stamped onto every span of every turn this connection
    sends; an arbitrary client does not get to write paragraphs there."""
    with pytest.raises(ConfigValidationError):
        await system_hello({"client_version": "0.1.0", "surface": bad})


async def test_hello_with_a_surface_needs_no_connection_scope() -> None:
    """A transport that never binds (bare pipe, in-process tests) still
    handshakes fine -- the declaration simply has nowhere to live."""
    result = await system_hello({"client_version": "0.1.0", "surface": "page"})
    assert result["server_version"]


async def test_two_connections_keep_their_surfaces_apart() -> None:
    """The state is per context, so two sockets saying hello concurrently on
    one dispatcher never see each other's declaration."""
    import asyncio

    from raven.rpc import connection

    async def one_connection(surface: str | None) -> str | None:
        token = connection.bind_connection()
        try:
            params: dict = {"client_version": "0.1.0"}
            if surface is not None:
                params["surface"] = surface
            await system_hello(params)
            await asyncio.sleep(0)
            return connection.declared_surface()
        finally:
            connection.unbind_connection(token)

    seen = await asyncio.gather(one_connection("page"), one_connection("tui"), one_connection(None))
    assert seen == ["page", "tui", None]


async def _check_with(monkeypatch, *, latest: str | None, sink=None) -> tuple[dict, list]:
    """Run an explicit check against a cache holding ``latest``, collecting frames."""
    from raven.cli import update_notice as un
    from raven.rpc.methods import system as system_mod

    monkeypatch.setattr(un, "check_for_update", lambda _cur: latest)
    monkeypatch.setattr(un, "update_notice", lambda _cur: (True, "raven upgrade") if latest else None)
    monkeypatch.setattr(un, "read_cache", lambda: {"latest_version": latest} if latest else {})

    frames: list = []
    if sink is None:

        async def sink(frame):
            frames.append(frame)

    result = await system_mod.system_version({"check": True}, send_frame=sink)
    return result, frames


async def test_an_explicit_check_announces_a_newer_build_to_every_client(monkeypatch) -> None:
    """``system.version`` answers the socket that asked. A check that nobody in a
    tab asked for -- the one `make beta` triggers after moving the pointer -- has
    no such socket to be useful on, so the newer build goes out as the same
    notification the periodic announcer sends."""
    result, frames = await _check_with(monkeypatch, latest="9.9.9")

    assert result["update_available"] is True
    assert frames == [{"method": "system.update_available", "params": {"latest_version": "9.9.9"}}]


async def test_an_explicit_check_announces_nothing_when_up_to_date(monkeypatch) -> None:
    result, frames = await _check_with(monkeypatch, latest=None)

    assert "update_available" not in result
    assert frames == []


async def test_a_transport_with_no_sink_announces_nothing(monkeypatch) -> None:
    """The TUI's single-socket transport and the web gateway register these
    handlers without a broadcast sink; the check must behave as it did before."""
    from raven.cli import update_notice as un
    from raven.rpc.methods import system as system_mod

    monkeypatch.setattr(un, "check_for_update", lambda _cur: "9.9.9")
    monkeypatch.setattr(un, "update_notice", lambda _cur: (True, "raven upgrade"))
    monkeypatch.setattr(un, "read_cache", lambda: {"latest_version": "9.9.9"})

    async def _forbidden(*_args, **_kwargs):
        raise AssertionError("announced with no sink wired")

    monkeypatch.setattr(system_mod, "_announce_update", _forbidden)

    dispatcher = Dispatcher()
    register_system_methods(dispatcher)
    response = await dispatcher.dispatch(
        {"jsonrpc": "2.0", "id": 1, "method": "system.version", "params": {"check": True}}
    )

    assert response["result"]["latest_version"] == "9.9.9"


async def test_a_plain_version_call_never_announces(monkeypatch) -> None:
    """Only an explicit check announces. Every open page asks for its versions at
    boot, and a broadcast per boot would banner every other tab as well."""
    from raven.cli import update_notice as un
    from raven.rpc.methods import system as system_mod

    monkeypatch.setattr(un, "update_notice", lambda _cur: (True, "raven upgrade"))
    monkeypatch.setattr(un, "read_cache", lambda: {"latest_version": "9.9.9"})

    frames: list = []

    async def sink(frame):
        frames.append(frame)

    result = await system_mod.system_version({}, send_frame=sink)

    assert result["latest_version"] == "9.9.9"
    assert frames == []
