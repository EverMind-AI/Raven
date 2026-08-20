"""Tests for tui_rpc confirm round-trip.

Covers the ConfirmBroker (notification emit + request_id→Future registry +
fail-safe), the confirm.respond handler + umbrella registration, and the
typer.confirm injection layer. Destructive TUI confirms become answerable
via a generic RPC round-trip.
"""

from __future__ import annotations

import asyncio
import threading

import click
import pytest
import typer

from raven.tui_rpc import confirm_broker as cb
from raven.tui_rpc._confirm_injection import confirm_injection
from raven.tui_rpc.confirm_broker import ConfirmBroker
from raven.tui_rpc.methods.cli_dispatch import cli_dispatch
from raven.tui_rpc.methods.confirm import confirm_respond, register_confirm_methods


def _frame_collector() -> tuple[list[dict], object]:
    frames: list[dict] = []

    async def send_frame(frame: dict) -> None:
        frames.append(frame)

    return frames, send_frame


async def _wait_for_frame(frames: list[dict], timeout: float = 1.0) -> dict:
    """Poll until the broker has emitted its confirm.request frame."""
    deadline = asyncio.get_running_loop().time() + timeout
    while not frames:
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("confirm.request frame never emitted")
        await asyncio.sleep(0.005)
    return frames[0]


# ---------------------------------------------------------------------------
# ConfirmBroker (CAP-CONF-1 / CAP-CONF-3)
# ---------------------------------------------------------------------------


async def test_confirm_request_notification_emitted() -> None:
    frames, send_frame = _frame_collector()
    broker = ConfirmBroker(send_frame)

    task = asyncio.create_task(broker.await_confirm("Continue?", default=False))
    frame = await _wait_for_frame(frames)

    assert "id" not in frame
    assert frame["jsonrpc"] == "2.0"
    assert frame["method"] == "confirm.request"
    params = frame["params"]
    assert isinstance(params["request_id"], str) and params["request_id"]
    assert params["prompt"] == "Continue?"
    assert params["default"] is False

    broker.resolve(params["request_id"], True)
    await task


async def test_broker_await_returns_answer() -> None:
    frames, send_frame = _frame_collector()
    broker = ConfirmBroker(send_frame)

    task = asyncio.create_task(broker.await_confirm("Reset?", default=False))
    frame = await _wait_for_frame(frames)
    broker.resolve(frame["params"]["request_id"], True)

    assert await task is True


async def test_confirm_respond_resolves_future() -> None:
    frames, send_frame = _frame_collector()
    broker = ConfirmBroker(send_frame)

    task = asyncio.create_task(broker.await_confirm("Reset?", default=False))
    frame = await _wait_for_frame(frames)
    rid = frame["params"]["request_id"]

    assert broker.resolve(rid, False) is True
    assert await task is False
    # registry cleaned up — a second resolve is a no-op
    assert broker.resolve(rid, True) is False


async def test_confirm_respond_unknown_id_idempotent() -> None:
    _frames, send_frame = _frame_collector()
    broker = ConfirmBroker(send_frame)

    assert broker.resolve("does-not-exist", True) is False


async def test_confirm_hard_limit_returns_false(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cb, "_CONFIRM_HARD_LIMIT_S", 0.05)
    frames, send_frame = _frame_collector()
    broker = ConfirmBroker(send_frame)

    # No resolve ever arrives → hard limit fires → fail-safe to default.
    result = await broker.await_confirm("Continue?", default=False)
    assert result is False
    await _wait_for_frame(frames)  # it did emit the request first


async def test_broker_cancel_all_failsafe() -> None:
    frames, send_frame = _frame_collector()
    broker = ConfirmBroker(send_frame)

    task = asyncio.create_task(broker.await_confirm("Continue?", default=False))
    await _wait_for_frame(frames)

    broker.cancel_all()
    assert await task is False


# ---------------------------------------------------------------------------
# Confirm injection layer (CAP-CONF-4)
# ---------------------------------------------------------------------------


def test_injection_restores_typer_confirm() -> None:
    orig_typer = typer.confirm
    orig_click = click.confirm
    broker = ConfirmBroker(lambda _frame: None)  # send_frame unused here
    loop = asyncio.new_event_loop()
    try:
        with confirm_injection(broker, loop):
            assert typer.confirm is not orig_typer
            assert click.confirm is not orig_click
        assert typer.confirm is orig_typer
        assert click.confirm is orig_click
    finally:
        loop.close()


def test_injection_restores_on_exception() -> None:
    orig_typer = typer.confirm
    broker = ConfirmBroker(lambda _frame: None)
    loop = asyncio.new_event_loop()
    try:
        with pytest.raises(RuntimeError):
            with confirm_injection(broker, loop):
                raise RuntimeError("boom")
        assert typer.confirm is orig_typer
    finally:
        loop.close()


def test_injected_confirm_routes_to_broker() -> None:
    """The patched typer.confirm bridges the worker thread to the loop's broker.

    Topology mirrors production: the event loop runs in one thread; the
    (test) caller thread invokes the patched confirm, which blocks on
    run_coroutine_threadsafe(...).result() until the broker is resolved.
    """
    loop = asyncio.new_event_loop()
    loop_thread = threading.Thread(target=loop.run_forever, daemon=True)
    loop_thread.start()

    holder: dict = {}

    async def send_frame(frame: dict) -> None:
        # Simulate an instant confirm.respond from the frontend.
        holder["broker"].resolve(frame["params"]["request_id"], True)

    broker = ConfirmBroker(send_frame)
    holder["broker"] = broker

    try:
        with confirm_injection(broker, loop):
            result = typer.confirm("Continue?", default=False)
        assert result is True
    finally:
        loop.call_soon_threadsafe(loop.stop)
        loop_thread.join(timeout=2.0)
        loop.close()


# ---------------------------------------------------------------------------
# confirm.respond handler + registration (CAP-CONF-2)
# ---------------------------------------------------------------------------


async def test_confirm_respond_handler_resolves() -> None:
    frames, send_frame = _frame_collector()
    broker = ConfirmBroker(send_frame)
    task = asyncio.create_task(broker.await_confirm("Reset?", default=False))
    frame = await _wait_for_frame(frames)
    rid = frame["params"]["request_id"]

    result = await confirm_respond({"request_id": rid, "answer": True}, confirm_broker=broker)

    assert result == {"ok": True}
    assert await task is True


async def test_confirm_respond_handler_unknown_id_returns_not_ok() -> None:
    _frames, send_frame = _frame_collector()
    broker = ConfirmBroker(send_frame)

    result = await confirm_respond({"request_id": "nope", "answer": True}, confirm_broker=broker)

    assert result == {"ok": False}


async def test_register_confirm_methods_adds_respond() -> None:
    from raven.tui_rpc.dispatcher import Dispatcher

    _frames, send_frame = _frame_collector()
    broker = ConfirmBroker(send_frame)
    dispatcher = Dispatcher()
    register_confirm_methods(dispatcher, confirm_broker=broker)

    assert "confirm.respond" in dispatcher.methods()


# ---------------------------------------------------------------------------
# End-to-end through cli.dispatch (CAP-CONF-3 sync bridge, REQ-1/2/3)
# ---------------------------------------------------------------------------


def _make_confirm_app() -> typer.Typer:
    """Fake Typer app whose `needs-confirm` command pivots on typer.confirm."""
    fake = typer.Typer(no_args_is_help=False)

    @fake.command("needs-confirm")
    def needs_confirm() -> None:
        import raven.cli.commands as ec_commands

        if typer.confirm("Continue?", default=False):
            ec_commands.console.print("DID-IT")
        else:
            ec_commands.console.print("ABORTED")
            raise typer.Exit(0)

    # A second command forces Typer into multi-command (subcommand) mode, so
    # argv[0] is parsed as the command name rather than a positional arg.
    @fake.command("noop")
    def noop() -> None:
        pass

    return fake


@pytest.fixture
def fake_confirm_app(monkeypatch: pytest.MonkeyPatch) -> typer.Typer:
    import raven.cli.commands as ec_commands

    fake = _make_confirm_app()
    monkeypatch.setattr(ec_commands, "app", fake)
    return fake


def _auto_answer_broker(*, answer: bool | None = None, drop: bool = False) -> ConfirmBroker:
    """Broker whose send_frame simulates an instant frontend response."""
    holder: dict = {}

    async def send_frame(frame: dict) -> None:
        broker = holder["broker"]
        if drop:
            broker.cancel_all()
        else:
            broker.resolve(frame["params"]["request_id"], answer)

    broker = ConfirmBroker(send_frame)
    holder["broker"] = broker
    return broker


async def test_confirm_accept_runs_command(fake_confirm_app) -> None:
    broker = _auto_answer_broker(answer=True)
    result = await cli_dispatch({"argv": ["needs-confirm"], "width": 80}, confirm_broker=broker)
    assert result["exit_code"] == 0
    assert "DID-IT" in result["stdout"]


async def test_confirm_reject_aborts_command(fake_confirm_app) -> None:
    broker = _auto_answer_broker(answer=False)
    result = await cli_dispatch({"argv": ["needs-confirm"], "width": 80}, confirm_broker=broker)
    assert result["exit_code"] == 0
    assert "ABORTED" in result["stdout"]
    assert "DID-IT" not in result["stdout"]


async def test_confirm_connection_drop_cancels(fake_confirm_app) -> None:
    broker = _auto_answer_broker(drop=True)
    result = await cli_dispatch({"argv": ["needs-confirm"], "width": 80}, confirm_broker=broker)
    # cancel_all → await_confirm returns default False → command aborts
    assert "ABORTED" in result["stdout"]
    assert "DID-IT" not in result["stdout"]


async def test_non_tui_confirm_unchanged(fake_confirm_app) -> None:
    """No broker → the round-trip never activates: typer.confirm stays the
    native callable (not the bridge) and is never auto-answered.

    (The native confirm's exact failure mode is environment-dependent — a real
    TUI's EOF pipe raises click.Abort, while pytest's captured stdin raises
    OSError — so we assert the invariant, not the specific error. The C1 Abort
    path is covered deterministically by
    test_tui_rpc_cli_dispatch::test_abort_returns_confirmation_hint.)
    """
    orig = typer.confirm
    result = await cli_dispatch({"argv": ["needs-confirm"], "width": 80})
    assert typer.confirm is orig  # never patched without a broker
    assert "DID-IT" not in result["stdout"]  # never auto-accepted
    assert result["exit_code"] != 0  # native confirm failed; no phantom success
