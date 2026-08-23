"""Tests for rpc confirm round-trip.

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

from raven.rpc import confirm_broker as cb
from raven.rpc._confirm_injection import confirm_injection
from raven.rpc.confirm_broker import ConfirmBroker
from raven.rpc.methods.cli_dispatch import cli_dispatch
from raven.rpc.methods.confirm import confirm_respond, register_confirm_methods


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
    from raven.rpc.dispatcher import Dispatcher

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
    test_rpc_cli_dispatch::test_abort_returns_confirmation_hint.)
    """
    orig = typer.confirm
    result = await cli_dispatch({"argv": ["needs-confirm"], "width": 80})
    assert typer.confirm is orig  # never patched without a broker
    assert "DID-IT" not in result["stdout"]  # never auto-accepted
    assert result["exit_code"] != 0  # native confirm failed; no phantom success


# ---------------------------------------------------------------------------
# Per-conversation scoping of the notification itself
# (connection.conversation_scoped, what the gateway's confirm broker is built on)
# ---------------------------------------------------------------------------


async def _hold_connection(sink, conversation_id: str, ready: asyncio.Event, release: asyncio.Event) -> None:
    """Stand in for one live socket: its own connection scope, held open in its
    own task, exactly as a transport holds one while it dispatches frames."""
    from raven.rpc import connection

    token = connection.bind_connection()
    connection.set_frame_sink(sink)
    connection.claim_conversation(conversation_id)
    ready.set()
    try:
        await release.wait()
    finally:
        connection.unbind_connection(token)


async def test_a_confirm_names_the_conversation_it_was_raised_in() -> None:
    """The field a frontend files the sheet under. Without it the yes/no docks
    over whichever conversation the reader happens to have open, which is not
    the one whose command is paused waiting for the answer.
    """
    frames, send_frame = _frame_collector()
    broker = ConfirmBroker(send_frame)

    task = asyncio.create_task(broker.await_confirm("Delete everything?", default=False, conversation_id="tui:a"))
    frame = await _wait_for_frame(frames)

    assert frame["params"]["conversation_id"] == "tui:a"
    broker.resolve(frame["params"]["request_id"], False)
    await task


async def _hold_terminal_connection(sink, ready: asyncio.Event, release: asyncio.Event) -> None:
    """A live socket that has NOT sent a turn: it binds a connection scope (so
    slash_exec can route its confirms to it) but claims NO conversation -- the
    persistent owner belongs to whoever actually sent the last turn."""
    from raven.rpc import connection

    token = connection.bind_connection()
    connection.set_frame_sink(sink)
    ready.set()
    try:
        await release.wait()
    finally:
        connection.unbind_connection(token)


async def _terminal_dispatch(dispatcher, sink, session_id: str, command: str, broker, task_out: dict) -> None:
    """Invoke slash.exec AS the terminal through the REGISTERED dispatcher,
    with the terminal's own connection scope current (so cli_dispatch's
    per-request sink is the terminal's)."""
    from raven.rpc import connection

    token = connection.bind_connection()
    connection.set_frame_sink(sink)
    try:
        frame = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "slash.exec",
            "params": {"command": command, "session_id": session_id},
        }
        resp = await dispatcher.dispatch(frame)
        task_out["result"] = resp.get("result", {})
    finally:
        connection.unbind_connection(token)


async def _terminal_invoke(send_frame, session_id: str, command: str, broker, task_out: dict) -> None:
    """Run slash.exec AS the terminal: its own connection scope is current, so
    the claim inside slash_exec lands on THIS connection -- exactly what a real
    terminal RPC call does. The browser holds a stale claim on the same
    session and must not receive the frame."""
    from raven.rpc import connection
    from raven.rpc.methods.slash_routing import slash_exec

    token = connection.bind_connection()
    connection.set_frame_sink(send_frame)
    # No claim here: the terminal has NOT sent a turn; the session's owner is
    # whoever claimed it last -- which the test arranges to be the browser.
    try:
        task_out["result"] = await slash_exec({"command": command, "session_id": session_id}, confirm_broker=broker)
    finally:
        connection.unbind_connection(token)


async def test_slash_exec_confirm_reaches_the_caller_and_only_the_caller(fake_confirm_app) -> None:
    """The reviewer's first shape: a browser already OWNS the shared session (it
    sent the last turn), and the terminal now runs a destructive slash on the
    same session. The confirm must reach the TERMINAL -- the surface that asked
    -- not the owner, and not broadcast.

    slash_exec routes the confirm on the CALLING connection's own sink, so the
    owner registry is neither consulted nor mutated."""

    broadcast_frames, broadcast = _frame_collector()
    tab_frames, tab_send = _frame_collector()
    terminal_frames: list[dict] = []

    release = asyncio.Event()
    tab_ready = asyncio.Event()
    tab = asyncio.create_task(_hold_connection(tab_send, "tui:shared", tab_ready, release))
    await asyncio.wait_for(tab_ready.wait(), 1)

    try:
        # The terminal runs the slash with a broker handed in explicitly (the
        # caller's own sink is what production wires, and the test's
        # auto-answering sink stands in for the frontend answering it). The
        # frame must reach THIS sink -- the caller -- not the stale owner.
        holder: dict = {}

        async def answering_terminal_send(frame: dict) -> None:
            terminal_frames.append(frame)
            holder["broker"].resolve(frame["params"]["request_id"], True)

        broker = ConfirmBroker(answering_terminal_send)
        holder["broker"] = broker
        task_out: dict = {}
        invoke = asyncio.create_task(
            _terminal_invoke(answering_terminal_send, "tui:shared", "needs-confirm", broker, task_out)
        )
        frame = await _wait_for_frame(terminal_frames)
        assert frame["params"]["conversation_id"] == "tui:shared"
        assert tab_frames == []
        assert broadcast_frames == []
        await asyncio.wait_for(invoke, 2)
        assert "DID-IT" in task_out["result"]["output"]
    finally:
        release.set()
        await asyncio.gather(tab)


async def test_slash_exec_with_no_prior_owner_still_reaches_only_the_caller(fake_confirm_app) -> None:
    """The reviewer's second shape: a freshly created or resumed session with
    no earlier turn means _owners has nobody for it. The caller's own sink is
    still the destination, so the frame never broadcasts."""

    broadcast_frames, broadcast = _frame_collector()
    terminal_frames: list[dict] = []

    holder: dict = {}

    async def answering_terminal_send(frame: dict) -> None:
        terminal_frames.append(frame)
        holder["broker"].resolve(frame["params"]["request_id"], False)

    broker = ConfirmBroker(answering_terminal_send)
    holder["broker"] = broker
    task_out: dict = {}
    invoke = asyncio.create_task(
        _terminal_invoke(answering_terminal_send, "tui:new", "needs-confirm", broker, task_out)
    )
    frame = await _wait_for_frame(terminal_frames)
    assert frame["params"]["conversation_id"] == "tui:new"
    assert broadcast_frames == []
    await asyncio.wait_for(invoke, 2)
    assert "ABORTED" in task_out["result"]["output"]


async def test_a_confirm_reaches_only_the_surface_that_owns_the_conversation() -> None:
    """One gateway, two live surfaces. A destructive command run in the
    terminal's session used to open the sheet in the browser tab as well, and
    either one could answer it; naming the conversation is what lets
    ``conversation_scoped`` narrow the frame to the surface that raised it.
    """
    from raven.rpc import connection

    broadcast_frames, broadcast = _frame_collector()
    terminal_frames, terminal_send = _frame_collector()
    tab_frames, tab_send = _frame_collector()

    release = asyncio.Event()
    tab_ready, term_ready = asyncio.Event(), asyncio.Event()
    tab = asyncio.create_task(_hold_connection(tab_send, "tui:tab", tab_ready, release))
    term = asyncio.create_task(_hold_connection(terminal_send, "tui:term", term_ready, release))
    await asyncio.wait_for(tab_ready.wait(), 1)
    await asyncio.wait_for(term_ready.wait(), 1)

    try:
        broker = ConfirmBroker(connection.conversation_scoped(broadcast))
        task = asyncio.create_task(
            broker.await_confirm("Delete everything?", default=False, conversation_id="tui:term")
        )
        frame = await _wait_for_frame(terminal_frames)
        assert frame["params"]["conversation_id"] == "tui:term"
        assert tab_frames == []
        assert broadcast_frames == []
        broker.resolve(frame["params"]["request_id"], True)
        assert await task is True
    finally:
        release.set()
        await asyncio.gather(tab, term)


async def test_a_confirm_with_no_conversation_still_broadcasts() -> None:
    """A bare ``cli.dispatch`` names no conversation, so the frame carries no
    field to scope by and every attached surface sees it -- what the confirm
    round-trip did before it could name one. The key is absent rather than null:
    the frame claims nothing it cannot back up.
    """
    from raven.rpc import connection

    frames, send_frame = _frame_collector()
    broker = ConfirmBroker(connection.conversation_scoped(send_frame))

    task = asyncio.create_task(broker.await_confirm("Delete everything?", default=False))
    frame = await _wait_for_frame(frames)

    assert set(frame["params"]) == {"request_id", "prompt", "default"}
    broker.resolve(frame["params"]["request_id"], False)
    await task


async def test_a_terminal_slash_does_not_hijack_the_browsers_turn_questions(fake_confirm_app) -> None:
    """The reviewer's regression: the browser OWNS the session and its turn is
    running; the terminal runs a slash that NEVER confirms (a noop). The old
    claim made that slash steal the owner, so a question the browser's turn
    later emitted -- a clarify/approval resolving through the persistent owner
    -- went to the terminal. It must still reach the BROWSER."""
    from raven.rpc import connection

    terminal_frames, terminal_send = _frame_collector()
    tab_frames, tab_send = _frame_collector()

    release = asyncio.Event()
    tab_ready, term_ready = asyncio.Event(), asyncio.Event()
    # The BROWSER owns the session (it sent the last turn). The terminal is a
    # live socket that has NOT sent a turn: it must not claim the conversation.
    tab = asyncio.create_task(_hold_connection(tab_send, "tui:shared", tab_ready, release))
    term = asyncio.create_task(_hold_terminal_connection(terminal_send, term_ready, release))
    await asyncio.wait_for(tab_ready.wait(), 1)
    await asyncio.wait_for(term_ready.wait(), 1)

    try:
        # The terminal runs a slash that needs no confirmation. It must not
        # change who owns the conversation.
        task_out: dict = {}
        invoke = asyncio.create_task(_terminal_invoke(terminal_send, "tui:shared", "noop", None, task_out))
        await asyncio.wait_for(invoke, 2)

        # The browser's running turn emits a question through the engine's own
        # task: no connection is bound, so it goes through conversation_scoped,
        # which resolves the PERSISTENT owner -- still the browser.
        engine_frames, engine_send = _frame_collector()
        broker = ConfirmBroker(connection.conversation_scoped(engine_send))
        task = asyncio.create_task(broker.await_confirm("Question?", default=False, conversation_id="tui:shared"))
        frame = await _wait_for_frame(tab_frames)
        assert frame["params"]["conversation_id"] == "tui:shared"
        assert terminal_frames == []
        broker.resolve(frame["params"]["request_id"], True)
        await task
    finally:
        release.set()
        await asyncio.gather(tab, term)


async def test_the_registered_slash_path_routes_the_confirm_to_the_caller(fake_confirm_app) -> None:
    """The reviewer's exact wiring: the production gateway registers
    slash.exec with the SHARED broker (build_rpc_stack -> register_aligned_
    methods_except_system -> register_slash_routing_methods), and the browser
    owns the session. A terminal slash's confirm must reach the terminal -- on
    the caller's own sink -- while the pending stays in the shared broker, so
    confirm.respond (registered against that same broker) can resolve it."""
    from raven.rpc import connection
    from raven.rpc.confirm_broker import ConfirmBroker
    from raven.rpc.dispatcher import Dispatcher
    from raven.rpc.methods.slash_routing import register_slash_routing_methods

    tab_frames, tab_send = _frame_collector()
    terminal_frames: list[dict] = []
    broadcast_frames, broadcast = _frame_collector()

    async def terminal_sink(frame: dict) -> None:
        terminal_frames.append(frame)

    shared = ConfirmBroker(connection.conversation_scoped(broadcast))
    dispatcher = Dispatcher()
    register_slash_routing_methods(dispatcher, confirm_broker=shared)

    release = asyncio.Event()
    tab_ready, term_ready = asyncio.Event(), asyncio.Event()
    tab = asyncio.create_task(_hold_connection(tab_send, "tui:shared", tab_ready, release))
    term = asyncio.create_task(_hold_terminal_connection(terminal_sink, term_ready, release))
    await asyncio.wait_for(tab_ready.wait(), 1)
    await asyncio.wait_for(term_ready.wait(), 1)

    try:
        # The terminal invokes slash.exec THROUGH the registered dispatcher.
        invoke = asyncio.create_task(
            _terminal_dispatch(dispatcher, terminal_sink, "tui:shared", "needs-confirm", shared, {})
        )
        frame = await _wait_for_frame(terminal_frames)
        assert frame["params"]["conversation_id"] == "tui:shared"
        assert tab_frames == []
        # The pending is in the SHARED broker, so confirm.respond resolves it.
        assert shared.resolve(frame["params"]["request_id"], True) is True
        await asyncio.wait_for(invoke, 2)
    finally:
        release.set()
        await asyncio.gather(tab, term)


async def test_slash_exec_files_the_confirm_under_the_session_it_was_typed_in(fake_confirm_app) -> None:
    """End to end over the seam that actually knows the conversation: the
    ``session_id`` the client sends with ``slash.exec`` is what comes back out on
    the ``confirm.request`` frame.
    """
    from raven.rpc.methods.slash_routing import slash_exec

    frames, send_frame = _frame_collector()
    broker = ConfirmBroker(send_frame)

    task = asyncio.create_task(slash_exec({"command": "needs-confirm", "session_id": "tui:a"}, confirm_broker=broker))
    frame = await _wait_for_frame(frames)

    assert frame["params"]["conversation_id"] == "tui:a"
    broker.resolve(frame["params"]["request_id"], True)
    assert "DID-IT" in (await task)["output"]


async def test_slash_exec_without_a_session_names_no_conversation(fake_confirm_app) -> None:
    """An older client sends no ``session_id``; the frame then names nothing
    rather than inventing a conversation for the sheet to dock in."""
    from raven.rpc.methods.slash_routing import slash_exec

    frames, send_frame = _frame_collector()
    broker = ConfirmBroker(send_frame)

    task = asyncio.create_task(slash_exec({"command": "needs-confirm"}, confirm_broker=broker))
    frame = await _wait_for_frame(frames)

    assert "conversation_id" not in frame["params"]
    broker.resolve(frame["params"]["request_id"], True)
    await task
