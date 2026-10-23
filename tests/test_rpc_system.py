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

import sys
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

_RAVEN_EXECUTABLE = "raven.exe" if sys.platform == "win32" else "raven"

# ---------------------------------------------------------------------------
# system.* handler tests (direct calls, no dispatcher)
# ---------------------------------------------------------------------------


@pytest.fixture
def trajectory_off():
    from raven.trajectory import policy

    policy.arm(False)
    yield
    policy._reset_for_tests()


async def test_hello_returns_versions():
    result = await system_hello({"client_version": "0.1.0"})
    assert "server_version" in result
    assert isinstance(result["server_version"], str)
    assert "server_capabilities" in result
    assert isinstance(result["server_capabilities"], list)
    assert "jsonrpc-2.0" in result["server_capabilities"]
    assert "trajectory-v1" in result["server_capabilities"]
    assert "model.options.selection_only" in result["server_capabilities"]
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
    from raven.updates import update_notice as un

    monkeypatch.setattr(un, "update_notice", lambda _cur: (True, "raven upgrade"))
    monkeypatch.setattr(un, "read_cache", lambda: {"latest_version": "9.9.9"})

    result = await system_version({})

    assert result["update_available"] is True
    assert result["latest_version"] == "9.9.9"


async def test_version_stays_quiet_without_a_cached_release(monkeypatch: pytest.MonkeyPatch):
    from raven.updates import update_notice as un

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


def _plan(tmp_path, monkeypatch):
    from raven.updates import upgrade as upgrade_commands

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    plan = upgrade_commands.UpgradePlan(
        current_version="0.1.3",
        release=upgrade_commands.ReleaseInfo(version="0.1.4", wheel_url="https://example.invalid/x.whl"),
        target=upgrade_commands.ToolInstallTarget(tool_dir=tmp_path / "tools", bin_dir=bin_dir),
    )
    monkeypatch.setattr(upgrade_commands, "plan_upgrade", lambda: plan)
    monkeypatch.setattr(upgrade_commands, "_is_editable_install", lambda: False)
    spawned: dict[str, object] = {}
    monkeypatch.setattr(
        upgrade_commands,
        "spawn_detached_upgrade",
        lambda p, **kw: spawned.update({"plan": p, **kw}),
    )
    return bin_dir, spawned


class TestUpgradingFromAGatewayHostedPage:
    """`raven web` runs `raven gateway`, so this is the page nearly every reader
    has. The old relaunch brought back a bare `raven serve`, which builds no
    ChannelManager: the IM channels would have vanished without a word. The
    restart now goes through the supervisor instead."""

    async def test_it_brings_back_the_supervised_gateway_not_a_bare_serve(self, monkeypatch, tmp_path):
        import asyncio

        from raven.cli.serve_commands import SERVE
        from raven.rpc.methods.system import system_upgrade

        bin_dir, spawned = _plan(tmp_path, monkeypatch)
        stopped: list[bool] = []
        SERVE.arm_hosted(18792, "tok", "cookie")
        SERVE.hand_over(lambda: stopped.append(True), lambda: None, 4242)
        try:
            result = await system_upgrade({})
            assert result["status"] == "started"
            # The reply must land before the gateway goes down.
            assert stopped == []
            await asyncio.sleep(0.9)
            assert stopped == [True]
        finally:
            SERVE.disarm()

        assert spawned["relaunch"] == [str(bin_dir / _RAVEN_EXECUTABLE), "web", "--supervise", "--port", "18792"]
        # The argv the entry point takes to the supervisor without the command table.
        from raven.cli import serve_commands

        taken: list[tuple] = []
        monkeypatch.setattr(serve_commands, "_web", lambda *a, **kw: taken.append((a, kw)))
        assert serve_commands.supervise_from_argv(spawned["relaunch"][1:]) is True
        assert taken == [((18792,), {"foreground": False, "stop": False, "supervise": True})]
        assert spawned["extra_env"] == {
            "RAVEN_SERVE_PORT_STRICT": "1",
            "RAVEN_SERVE_TOKEN": "tok",
            "RAVEN_SERVE_COOKIE": "cookie",
        }

    async def test_the_helper_waits_for_the_supervisor_not_the_gateway(self, monkeypatch, tmp_path):
        """The supervisor's own cleanup runs after the gateway exits and removes
        web.json. A new supervisor started before that finishes has its file
        removed from under it, and the next `raven web` starts a second one."""
        from raven.cli.serve_commands import SERVE
        from raven.rpc.methods.system import system_upgrade

        _bin_dir, spawned = _plan(tmp_path, monkeypatch)
        SERVE.arm_hosted(18792, "tok", "cookie")
        SERVE.hand_over(lambda: None, lambda: None, 4242)
        try:
            await system_upgrade({})
        finally:
            SERVE.disarm()

        assert spawned["parent_pid"] == 4242

    @pytest.mark.parametrize(
        ("windows", "command"),
        [
            (False, "raven web --stop\nraven upgrade\nraven web"),
            (True, "raven web --stop\nraven upgrade"),
        ],
        ids=["posix", "windows"],
    )
    async def test_it_refuses_a_gateway_nothing_would_bring_back(self, monkeypatch, tmp_path, windows, command):
        """Started by hand, or under `raven web --foreground`: no supervisor.
        Installing would end with nothing running at all."""
        from raven.cli.serve_commands import SERVE
        from raven.rpc.methods import system
        from raven.rpc.methods.system import system_upgrade

        _bin_dir, spawned = _plan(tmp_path, monkeypatch)
        monkeypatch.setattr(system, "_on_windows", lambda: windows)
        SERVE.arm_hosted(18792, "tok", "cookie")
        SERVE.hand_over(lambda: pytest.fail("stopped an unsupervised gateway"), lambda: None, None)
        try:
            with pytest.raises(ConfigValidationError) as excinfo:
                await system_upgrade({})
        finally:
            SERVE.disarm()

        assert excinfo.value.data["reason"] == "unsupervised"
        assert excinfo.value.data["command"] == command
        assert spawned == {}

    async def test_it_classifies_a_checkout_before_the_unsupervised_refusal(self, monkeypatch, tmp_path):
        """An editable install under a hand-started gateway is both at once, and
        the checkout answer has to win: `raven upgrade` refuses a checkout, so
        the unsupervised recovery's first command can never work there."""
        from raven.cli.serve_commands import SERVE
        from raven.rpc.methods.system import system_upgrade
        from raven.updates import upgrade as upgrade_commands

        def plan() -> None:
            raise AssertionError("a checkout should be refused before the release lookup")

        monkeypatch.setattr(upgrade_commands, "_is_editable_install", lambda: True)
        monkeypatch.setattr(upgrade_commands, "editable_checkout", lambda: tmp_path)
        monkeypatch.setattr(upgrade_commands, "plan_upgrade", plan)
        SERVE.arm_hosted(18792, "tok", "cookie")
        SERVE.hand_over(lambda: pytest.fail("stopped an unsupervised gateway"), lambda: None, None)
        try:
            with pytest.raises(ConfigValidationError) as excinfo:
                await system_upgrade({})
        finally:
            SERVE.disarm()

        assert excinfo.value.data["reason"] == "editable", excinfo.value.data
        assert "git pull" in excinfo.value.data["command"].splitlines()

    async def _probe_answers(self, monkeypatch, distribution, reason, detail=None):
        """Drive system_upgrade with the real editable probe: only the metadata
        boundary is faked, so _is_editable_install and its reads run for real
        and every refusal has to come from the actual failure."""
        import asyncio

        from raven.cli.serve_commands import SERVE
        from raven.rpc.methods.system import system_upgrade
        from raven.updates import upgrade as upgrade_commands

        monkeypatch.setattr(upgrade_commands.metadata, "distribution", distribution)

        def plan() -> None:
            raise AssertionError("the install cannot be classified before the release lookup runs")

        monkeypatch.setattr(upgrade_commands, "plan_upgrade", plan)
        SERVE.arm(18792, "tok", "cookie", asyncio.Event())
        try:
            with pytest.raises(ConfigValidationError) as excinfo:
                await system_upgrade({})
        finally:
            SERVE.disarm()

        assert excinfo.value.data["reason"] == reason, excinfo.value.data
        if detail is not None:
            assert detail in excinfo.value.data["detail"], excinfo.value.data["detail"]
        return excinfo.value.data

    async def test_an_install_replacement_window_answers_in_progress_not_an_error(self, monkeypatch, tmp_path):
        """An upgrade already running has uv mid-replacement of the environment,
        and metadata.distribution itself reports the package gone. Letting that
        escape answers internal_error, whose card falls back to the default
        command this branch exists to withhold."""
        from importlib.metadata import PackageNotFoundError

        def gone(_name):
            raise PackageNotFoundError("raven")

        await self._probe_answers(monkeypatch, gone, "in_progress")

    async def test_malformed_install_metadata_answers_not_upgradable(self, monkeypatch, tmp_path):
        """direct_url.json that is not valid JSON is refused by the probe's own
        validation; the detail proves the refusal came from the real reads, not
        from a stub raising on the probe's behalf."""

        class _Dist:
            def read_text(self, _name):
                return "{ not json"

        data = await self._probe_answers(
            monkeypatch, lambda _n: _Dist(), "not_upgradable", "Malformed Raven installation metadata"
        )
        # Explicitly none, not merely absent: no command can work on this
        # install, and the card drops the row instead of offering the default.
        assert "command" in data and data["command"] is None, data

    @pytest.mark.parametrize(
        "failure",
        [
            OSError("disk gone"),
            UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte"),
        ],
        ids=["unreadable", "undecodable"],
    )
    async def test_an_unreadable_probe_classifies_as_not_a_checkout(self, monkeypatch, tmp_path, failure):
        """Reading direct_url.json can fail below JSON entirely -- an IO error,
        or bytes that are not the utf-8 the file promises. The probe only
        classifies a checkout, so an unreadable probe is not one: no refusal
        comes from the guard, and the flow continues to the release lookup
        (whose stub is the marker this pins)."""
        import asyncio

        from raven.cli.serve_commands import SERVE
        from raven.rpc.methods.system import system_upgrade
        from raven.updates import upgrade as upgrade_commands

        class _Dist:
            def read_text(self, _name):
                raise failure

        monkeypatch.setattr(upgrade_commands.metadata, "distribution", lambda _n: _Dist())

        def plan() -> None:
            raise AssertionError("the flow continued past the probe to the release lookup")

        monkeypatch.setattr(upgrade_commands, "plan_upgrade", plan)
        SERVE.arm(18792, "tok", "cookie", asyncio.Event())
        try:
            with pytest.raises(ConfigValidationError) as excinfo:
                await system_upgrade({})
        finally:
            SERVE.disarm()

        # No refusal came from the guard: the flow continued to the release
        # lookup, whose own handler answered -- carrying the marker through.
        assert excinfo.value.data["reason"] == "check_failed", excinfo.value.data
        assert "continued past the probe" in excinfo.value.data["detail"], excinfo.value.data["detail"]

    async def test_it_refuses_while_work_the_page_cannot_see_is_running(self, monkeypatch, tmp_path):
        """The page checks its own turn. An IM turn or a sub-agent on the same
        engine is invisible to it, and the restart would cut it off."""
        from raven.cli.serve_commands import SERVE
        from raven.rpc.methods.system import system_upgrade

        _bin_dir, spawned = _plan(tmp_path, monkeypatch)
        SERVE.arm_hosted(18792, "tok", "cookie")
        SERVE.hand_over(
            lambda: pytest.fail("stopped a busy gateway"),
            lambda: {"subagents": 1, "questions": 0},
            4242,
        )
        try:
            with pytest.raises(ConfigValidationError) as excinfo:
                await system_upgrade({})
        finally:
            SERVE.disarm()

        assert excinfo.value.data["reason"] == "busy"
        assert spawned == {}

    async def test_it_refuses_when_there_is_nothing_to_relaunch_from(self, monkeypatch, tmp_path):
        """Under the gateway there is no degraded path: without a relaunch the
        helper would install and leave nothing running."""
        from raven.cli.serve_commands import SERVE
        from raven.rpc.methods.system import system_upgrade

        bin_dir, spawned = _plan(tmp_path, monkeypatch)
        bin_dir.rmdir()
        SERVE.arm_hosted(18792, "tok", "cookie")
        SERVE.hand_over(lambda: pytest.fail("stopped with nothing to relaunch"), lambda: None, 4242)
        try:
            with pytest.raises(ConfigValidationError) as excinfo:
                await system_upgrade({})
        finally:
            SERVE.disarm()

        assert excinfo.value.data["reason"] == "not_upgradable"
        assert spawned == {}

    async def test_a_page_mounted_before_the_hand_over_does_not_upgrade(self):
        """The mount happens before the gateway has built its stop. A request in
        that gap must not be treated as upgradable."""
        from raven.cli.serve_commands import SERVE
        from raven.rpc.methods.system import system_upgrade

        SERVE.arm_hosted(18792, "tok", "cookie")
        try:
            with pytest.raises(ConfigValidationError) as excinfo:
                await system_upgrade({})
        finally:
            SERVE.disarm()

        assert excinfo.value.data["reason"] in {"unsupervised", "not_serving"}


async def test_upgrade_refuses_when_the_install_cannot_self_upgrade(monkeypatch: pytest.MonkeyPatch):
    import asyncio

    from raven.cli.serve_commands import SERVE
    from raven.rpc.methods.system import system_upgrade
    from raven.updates import upgrade as upgrade_commands

    def refuse() -> None:
        raise upgrade_commands.UpgradeError("Editable Raven installations cannot be upgraded automatically")

    monkeypatch.setattr(upgrade_commands, "plan_upgrade", refuse)
    monkeypatch.setattr(upgrade_commands, "_is_editable_install", lambda: False)
    SERVE.arm(18792, "tok", "cookie", asyncio.Event())
    try:
        with pytest.raises(ConfigValidationError) as excinfo:
            await system_upgrade({})
    finally:
        SERVE.disarm()
    assert excinfo.value.data["reason"] == "not_upgradable"


@pytest.mark.parametrize(
    ("windows", "checkout", "command"),
    [
        (False, "/srv/raven dev", "cd '/srv/raven dev'\ngit pull\n./install.sh"),
        (
            True,
            "C:\\Users\\o'neil\\Raven",
            "Set-Location -LiteralPath 'C:\\Users\\o''neil\\Raven'\ngit pull\n.\\install.ps1",
        ),
    ],
    ids=["posix", "windows"],
)
async def test_upgrade_names_a_checkouts_own_recovery_and_stops_there(
    monkeypatch: pytest.MonkeyPatch, windows, checkout, command
):
    """A source checkout is never upgradable through releases, and the page's
    default `raven upgrade` fallback refuses it too, so the card has to name
    the steps that do work instead -- in the checkout itself, since a bare
    `git pull` updates whichever repository the terminal happens to be in."""
    import asyncio

    from raven.cli.serve_commands import SERVE
    from raven.rpc.methods import system
    from raven.rpc.methods.system import system_upgrade
    from raven.updates import upgrade as upgrade_commands

    def plan() -> None:
        raise AssertionError("a checkout should be refused before the release lookup")

    monkeypatch.setattr(system, "_on_windows", lambda: windows)
    monkeypatch.setattr(upgrade_commands, "_is_editable_install", lambda: True)
    monkeypatch.setattr(upgrade_commands, "editable_checkout", lambda: checkout)
    monkeypatch.setattr(upgrade_commands, "plan_upgrade", plan)
    SERVE.arm(18792, "tok", "cookie", asyncio.Event())
    try:
        with pytest.raises(ConfigValidationError) as excinfo:
            await system_upgrade({})
    finally:
        SERVE.disarm()

    assert excinfo.value.data["reason"] == "editable"
    assert excinfo.value.data["command"] == command


async def test_a_checkout_whose_path_cannot_be_read_gets_no_command(monkeypatch: pytest.MonkeyPatch):
    """Without the checkout's path the only command left is a `git pull` in
    whatever directory the terminal is in, so the card drops the row and keeps
    the sentence."""
    import asyncio

    from raven.cli.serve_commands import SERVE
    from raven.rpc.methods.system import system_upgrade
    from raven.updates import upgrade as upgrade_commands

    def unreadable():
        raise upgrade_commands.UpgradeError("Editable source checkout must be a local directory")

    def plan() -> None:
        raise AssertionError("a checkout should be refused before the release lookup")

    monkeypatch.setattr(upgrade_commands, "_is_editable_install", lambda: True)
    monkeypatch.setattr(upgrade_commands, "editable_checkout", unreadable)
    monkeypatch.setattr(upgrade_commands, "plan_upgrade", plan)
    SERVE.arm(18792, "tok", "cookie", asyncio.Event())
    try:
        with pytest.raises(ConfigValidationError) as excinfo:
            await system_upgrade({})
    finally:
        SERVE.disarm()

    assert excinfo.value.data["reason"] == "editable"
    assert "command" in excinfo.value.data and excinfo.value.data["command"] is None


@pytest.mark.parametrize("quote", ["'", "\u2018", "\u2019", "\u201a", "\u201b"])
def test_a_windows_checkout_path_cannot_close_its_quotes(quote):
    """PowerShell reads the curly single quotes as quotes too, so a profile
    folder spelled with one would end the literal early and run the rest of
    the path as code unless every one of them is doubled."""
    from raven.rpc.methods.system import _checkout_recovery

    command = _checkout_recovery(f"C:\\Users\\O{quote}Neil\\Raven", True)

    assert command.splitlines()[0] == f"Set-Location -LiteralPath 'C:\\Users\\O{quote}{quote}Neil\\Raven'"


@pytest.mark.parametrize("windows", [False, True], ids=["posix", "windows"])
def test_the_unsupervised_sentence_names_every_step_its_command_runs(windows):
    """The sentence tells the reader to stop Raven first; a command that leaves
    the stop out sends them into an upgrade under a Raven still running."""
    from raven.rpc.methods.system import _unsupervised_recovery

    detail, command = _unsupervised_recovery(windows)

    assert command.splitlines()[0] == "raven web --stop"
    for line in command.splitlines():
        assert f"`{line}`" in detail, (line, detail)


def test_a_windows_unsupervised_command_never_starts_raven_beside_the_install():
    """On Windows `raven upgrade` returns once its helper is started, before the
    install has run; a `raven web` right behind it lands in the middle of that
    install. The start belongs to the reader, after the upgrade reports done."""
    from raven.rpc.methods.system import _unsupervised_recovery

    detail, command = _unsupervised_recovery(True)
    after_upgrade = command.splitlines()[command.splitlines().index("raven upgrade") + 1 :]

    assert after_upgrade == []
    assert "`raven web`" in detail


def test_a_posix_unsupervised_command_starts_raven_after_the_install():
    """POSIX `raven upgrade` runs the install in its own process and returns only
    once it is done, so the start can follow it on the next line."""
    from raven.rpc.methods.system import _unsupervised_recovery

    _detail, command = _unsupervised_recovery(False)

    assert command.splitlines() == ["raven web --stop", "raven upgrade", "raven web"]


@pytest.mark.parametrize(("name", "windows"), [("nt", True), ("posix", False)])
def test_the_platform_switch_reads_os_name(monkeypatch: pytest.MonkeyPatch, name, windows):
    import os

    from raven.rpc.methods.system import _on_windows

    with monkeypatch.context() as patched:
        patched.setattr(os, "name", name)
        answer = _on_windows()

    assert answer is windows


async def test_upgrade_names_an_install_already_in_flight(monkeypatch: pytest.MonkeyPatch):
    """uv removes the old environment before writing the new one, so a check
    landing in that window finds no metadata for `raven`. Reported as a generic
    check failure it reads as a broken install, and the reader starts a second
    upgrade into the same gap."""
    import asyncio
    import importlib.metadata as md

    from raven.cli.serve_commands import SERVE
    from raven.rpc.methods.system import system_upgrade
    from raven.updates import upgrade as upgrade_commands

    def mid_replacement() -> None:
        raise md.PackageNotFoundError("raven")

    monkeypatch.setattr(upgrade_commands, "plan_upgrade", mid_replacement)
    monkeypatch.setattr(upgrade_commands, "_is_editable_install", lambda: False)
    SERVE.arm(18792, "tok", "cookie", asyncio.Event())
    try:
        with pytest.raises(ConfigValidationError) as excinfo:
            await system_upgrade({})
    finally:
        SERVE.disarm()
    assert excinfo.value.data["reason"] == "in_progress"


async def test_upgrade_hands_off_then_stops_the_gateway(monkeypatch: pytest.MonkeyPatch, tmp_path):
    import asyncio

    from raven.cli.serve_commands import SERVE
    from raven.rpc.methods.system import system_upgrade
    from raven.updates import upgrade as upgrade_commands

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    plan = upgrade_commands.UpgradePlan(
        current_version="0.1.3",
        release=upgrade_commands.ReleaseInfo(version="0.1.4", wheel_url="https://example.invalid/x.whl"),
        target=upgrade_commands.ToolInstallTarget(tool_dir=tmp_path / "tools", bin_dir=bin_dir),
    )
    monkeypatch.setattr(upgrade_commands, "plan_upgrade", lambda: plan)
    monkeypatch.setattr(upgrade_commands, "_is_editable_install", lambda: False)
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

    assert spawned["relaunch"] == [str(bin_dir / _RAVEN_EXECUTABLE), "serve", "--port", "18792"]
    # Both credentials travel, and that is the point: the browser presents the
    # cookie, a relauncher needs the shared secret, and since they are no longer
    # the same string, carrying only one of them would sign the open page out.
    assert spawned["extra_env"] == {
        "RAVEN_SERVE_PORT_STRICT": "1",
        "RAVEN_SERVE_TOKEN": "tok",
        "RAVEN_SERVE_COOKIE": "cookie",
    }
    assert spawned["parent_pid"] > 0


async def test_an_upgrade_relaunch_never_passes_dev(monkeypatch, tmp_path):
    import asyncio

    from raven.cli.serve_commands import SERVE
    from raven.rpc.methods.system import system_upgrade

    bin_dir, spawned = _plan(tmp_path, monkeypatch)
    SERVE.arm(18792, "tok", "cookie", asyncio.Event())
    try:
        await system_upgrade({})
    finally:
        SERVE.disarm()
    assert spawned["relaunch"] == [str(bin_dir / _RAVEN_EXECUTABLE), "serve", "--port", "18792"]

    _bin_dir, spawned = _plan(tmp_path, monkeypatch)
    SERVE.arm_hosted(18792, "tok", "cookie")
    SERVE.hand_over(lambda: None, lambda: None, 4242)
    try:
        await system_upgrade({})
    finally:
        SERVE.disarm()
    assert spawned["relaunch"] == [str(bin_dir / _RAVEN_EXECUTABLE), "web", "--supervise", "--port", "18792"]


# ---------------------------------------------------------------------------
# The handshake reports the channel the dispatcher was actually built for
# ---------------------------------------------------------------------------


async def test_hello_announces_the_trajectory_surface_while_the_policy_is_on() -> None:
    result = await system_hello({"client_version": "0.1.0"})
    assert "trajectory-v1" in result["server_capabilities"]


async def test_hello_leaves_the_trajectory_surface_out_once_the_policy_is_off(trajectory_off) -> None:
    result = await system_hello({"client_version": "0.1.0"})
    assert "trajectory-v1" not in result["server_capabilities"]


async def test_hello_announces_a_folder_dialog_only_where_the_host_has_one(monkeypatch) -> None:
    """The page offers the host's own folder dialog on this announcement alone:
    a gateway in a container, or on a headless box reached through a forwarded
    port, is served at a loopback address all the same and has none to open."""
    from raven.rpc import folder_dialog

    monkeypatch.setattr(folder_dialog, "pick_dir_argv", lambda: ["dialog"])
    result = await system_hello({"client_version": "0.1.0"})
    assert "folder-dialog-v1" in result["server_capabilities"]
    monkeypatch.setattr(folder_dialog, "pick_dir_argv", lambda: None)
    result = await system_hello({"client_version": "0.1.0"})
    assert "folder-dialog-v1" not in result["server_capabilities"]


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
    from raven.rpc.methods import system as system_mod
    from raven.updates import update_notice

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
    from raven.rpc.methods import system as system_mod
    from raven.updates import update_notice as un

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
    from raven.rpc.methods import system as system_mod
    from raven.updates import update_notice as un

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
    from raven.rpc.methods import system as system_mod
    from raven.updates import update_notice as un

    monkeypatch.setattr(un, "update_notice", lambda _cur: (True, "raven upgrade"))
    monkeypatch.setattr(un, "read_cache", lambda: {"latest_version": "9.9.9"})

    frames: list = []

    async def sink(frame):
        frames.append(frame)

    result = await system_mod.system_version({}, send_frame=sink)

    assert result["latest_version"] == "9.9.9"
    assert frames == []
