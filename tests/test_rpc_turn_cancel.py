"""Tests for ``turn.cancel`` real handler.

``turn.cancel`` cancels the in-flight turn handle, emits the one
``error(reason="cancelled_by_client")`` (the build_rpc_spine sink stays silent on a
cancelled TurnFailed to avoid a double error), then drains the handle. The
per-turn cancel must leave the session-scoped subscription open.

These tests drive the handler with a fake Scheduler/handle + a real
SubscriptionEmitter; the spine streaming path is covered in
``test_rpc_spine.py``.
"""

from __future__ import annotations

import asyncio
import contextlib
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from raven.rpc.dispatcher import Dispatcher
from raven.rpc.methods.turn import (
    register_session_interrupt_method,
    register_turn_methods,
    session_interrupt,
    turn_cancel,
    turn_send,
    turn_subscribe,
)
from raven.rpc.subscriptions import SubscriptionEmitter
from raven.spine import direct_lane


class FakeHandle:
    def __init__(self) -> None:
        self.cancelled = False

    async def cancel(self) -> None:
        self.cancelled = True

    async def result(self):
        return None


class FakeScheduler:
    def submit(self, req):
        return FakeHandle()


class _AtAcquire:
    """Wraps a pool semaphore to signal the moment a payload reaches its acquire.

    The payload parking on its pool is otherwise unobservable from outside, and
    the scheduler's own flag records having *reported*, not having entered. This
    fires synchronously before the acquire suspends, so a waiter that wakes on it
    is guaranteed to see the payload already parked.
    """

    def __init__(self, inner) -> None:
        self._inner = inner
        self.reached = asyncio.Event()

    async def __aenter__(self):
        self.reached.set()
        return await self._inner.__aenter__()

    async def __aexit__(self, *exc):
        return await self._inner.__aexit__(*exc)


@pytest.fixture(autouse=True)
def _clear_active_turns():
    from raven.rpc.methods import turn as _turn_mod

    _turn_mod._active_turns.clear()
    yield
    _turn_mod._active_turns.clear()


@pytest.fixture
def send_frame_capture() -> AsyncMock:
    return AsyncMock(return_value=None)


@pytest.fixture
def emitter(send_frame_capture: AsyncMock) -> SubscriptionEmitter:
    return SubscriptionEmitter(send_frame=send_frame_capture)


@pytest.fixture
def dispatcher(emitter: SubscriptionEmitter) -> Dispatcher:
    d = Dispatcher()
    register_turn_methods(d, emitter=emitter, scheduler=FakeScheduler(), turn_ids={})
    return d


def _collect_events(send_frame_capture: AsyncMock) -> list[dict]:
    events: list[dict] = []
    for call in send_frame_capture.call_args_list:
        frame = call.args[0] if call.args else call.kwargs.get("frame")
        if frame and frame.get("method") == "event":
            events.append(frame["params"]["event"])
    return events


# --- Cancel an active turn ---


async def test_turn_cancel_active_turn_returns_cancelled_true(
    emitter: SubscriptionEmitter,
) -> None:
    await turn_send(
        {"session_key": "tui:default", "content": "hello"}, emitter=emitter, scheduler=FakeScheduler(), turn_ids={}
    )
    result = await turn_cancel({"session_key": "tui:default"}, emitter=emitter)
    assert result == {"cancelled": True}


async def test_turn_cancel_no_active_turn_returns_cancelled_false(
    emitter: SubscriptionEmitter,
) -> None:
    result = await turn_cancel({"session_key": "tui:default"}, emitter=emitter)
    assert result == {"cancelled": False}


async def test_turn_cancel_cancels_the_handle(emitter: SubscriptionEmitter) -> None:
    from raven.rpc.methods import turn as turn_mod

    await turn_send(
        {"session_key": "tui:default", "content": "x"}, emitter=emitter, scheduler=FakeScheduler(), turn_ids={}
    )
    handle = turn_mod._active_turns["tui:default"]
    await turn_cancel({"session_key": "tui:default"}, emitter=emitter)
    assert handle.cancelled is True


async def test_turn_cancel_emits_error_event_with_cancelled_by_client_reason(
    emitter: SubscriptionEmitter,
    send_frame_capture: AsyncMock,
) -> None:
    await turn_subscribe({"session_key": "tui:default"}, emitter=emitter)
    await turn_send(
        {"session_key": "tui:default", "content": "hello"}, emitter=emitter, scheduler=FakeScheduler(), turn_ids={}
    )
    await turn_cancel({"session_key": "tui:default"}, emitter=emitter)
    await asyncio.sleep(0.05)  # let the coalescer flush

    cancelled_events = [
        e
        for e in _collect_events(send_frame_capture)
        if e.get("type") == "error" and e.get("payload", {}).get("reason") == "cancelled_by_client"
    ]
    assert len(cancelled_events) >= 1


async def test_turn_cancel_keeps_subscription_open_for_next_turn(
    emitter: SubscriptionEmitter,
    send_frame_capture: AsyncMock,
) -> None:
    """A per-turn cancel must NOT tear down the session-scoped subscription:
    a fresh emit on the same session still reaches the subscriber."""
    await turn_subscribe({"session_key": "tui:default"}, emitter=emitter)
    await turn_send(
        {"session_key": "tui:default", "content": "x"}, emitter=emitter, scheduler=FakeScheduler(), turn_ids={}
    )
    await turn_cancel({"session_key": "tui:default"}, emitter=emitter)
    await asyncio.sleep(0.05)

    pre_count = send_frame_capture.call_count
    await emitter.emit("tui:default", {"type": "message.start", "payload": {"turn_id": "turn-2"}})
    await asyncio.sleep(0.05)
    assert send_frame_capture.call_count > pre_count, (
        "per-turn cancel closed the session subscription; turn-2 emit was dropped"
    )


# --- Params validation ---


async def test_turn_cancel_rejects_missing_session_key(emitter: SubscriptionEmitter) -> None:
    with pytest.raises(ValidationError):
        await turn_cancel({}, emitter=emitter)


# --- End-to-end via Dispatcher ---


async def test_turn_cancel_dispatches_via_dispatcher_with_no_active_turn(
    dispatcher: Dispatcher,
) -> None:
    resp = await dispatcher.dispatch(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "turn.cancel",
            "params": {"session_key": "tui:default"},
        }
    )
    assert "error" not in resp
    assert resp["result"] == {"cancelled": False}


async def test_turn_cancel_dispatches_via_dispatcher_with_active_turn(
    dispatcher: Dispatcher,
) -> None:
    await dispatcher.dispatch(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "turn.send",
            "params": {"session_key": "tui:default", "content": "hello"},
        }
    )
    resp = await dispatcher.dispatch(
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "turn.cancel",
            "params": {"session_key": "tui:default"},
        }
    )

    assert "error" not in resp
    assert resp["result"] == {"cancelled": True}


# --- session.interrupt (the pre-typed-chat Ctrl+C path) ---


async def test_session_interrupt_cancels_the_active_turn(emitter: SubscriptionEmitter) -> None:
    await turn_send(
        {"session_key": "tui:default", "content": "hello"}, emitter=emitter, scheduler=FakeScheduler(), turn_ids={}
    )
    assert await session_interrupt({"session_id": "tui:default"}) == {"ok": True}


async def test_session_interrupt_with_no_turn_in_flight_is_not_an_error() -> None:
    assert await session_interrupt({"session_id": "tui:default"}) == {"ok": False}
    assert await session_interrupt({}) == {"ok": False}


async def test_session_interrupt_emits_nothing(emitter: SubscriptionEmitter, send_frame_capture: AsyncMock) -> None:
    # Unlike turn.cancel: the client draws the interrupted state itself before
    # firing this, so a second error frame would double-report the same event.
    await turn_subscribe({"session_key": "tui:default"}, emitter=emitter)
    await turn_send(
        {"session_key": "tui:default", "content": "hello"}, emitter=emitter, scheduler=FakeScheduler(), turn_ids={}
    )
    before = len(_collect_events(send_frame_capture))
    await session_interrupt({"session_id": "tui:default"})
    assert len(_collect_events(send_frame_capture)) == before


async def test_session_interrupt_is_registered_without_an_emitter() -> None:
    # The legacy path has no subscription channel; gating it on one the way
    # turn.* is gated would put it back on -32601 exactly where it is used.
    d = Dispatcher()
    register_session_interrupt_method(d)
    assert "session.interrupt" in d.methods()


async def test_an_untargeted_cancel_leaves_a_direct_turn_running(
    emitter: SubscriptionEmitter, send_frame_capture: AsyncMock
) -> None:
    """A cancel with no target means the main agent's turn and nothing else.

    A direct turn runs on its instance's own lane, so the session's slot does not
    hold it: the cancel finds nothing rather than reaching across and tearing
    down a sub-agent the user was not looking at.
    """
    targets: dict[str, dict[str, str]] = {}
    await turn_subscribe({"session_key": "tui:default"}, emitter=emitter)
    await turn_send(
        {
            "session_key": "tui:default",
            "content": "fix it",
            "target": {"agent": "Raven-Code", "handle": "refactor-auth"},
        },
        emitter=emitter,
        scheduler=FakeScheduler(),
        turn_ids={},
        direct_targets=targets,
    )

    result = await turn_cancel({"session_key": "tui:default"}, emitter=emitter, direct_targets=targets)
    await asyncio.sleep(0.05)  # let the coalescer flush

    assert result == {"cancelled": False}
    assert [e for e in _collect_events(send_frame_capture) if e["type"] == "error"] == []


async def test_a_direct_turn_is_cancelled_through_its_target(
    emitter: SubscriptionEmitter, send_frame_capture: AsyncMock
) -> None:
    """Naming the instance cancels its turn: the lane is resolved the same way
    ``turn.send`` resolved the one it bound, and the error is tagged so the
    client clears the direct view rather than the main transcript."""
    from raven.rpc.methods import turn as turn_mod

    targets: dict[str, dict[str, str]] = {}
    turn_ids: dict[str, str] = {}
    target = {"agent": "Raven-Code", "handle": "refactor-auth"}
    await turn_subscribe({"session_key": "tui:default"}, emitter=emitter)
    sent = await turn_send(
        {"session_key": "tui:default", "content": "fix it", "target": target},
        emitter=emitter,
        scheduler=FakeScheduler(),
        turn_ids=turn_ids,
        direct_targets=targets,
    )
    handle = turn_mod._active_turns[direct_lane("tui:default", "Raven-Code", "refactor-auth")]

    result = await turn_cancel(
        {"session_key": "tui:default", "target": target},
        emitter=emitter,
        turn_ids=turn_ids,
        direct_targets=targets,
    )
    await asyncio.sleep(0.05)  # let the coalescer flush

    assert result == {"cancelled": True}
    assert handle.cancelled is True
    errors = [e for e in _collect_events(send_frame_capture) if e["type"] == "error"]
    assert errors[-1]["payload"]["reason"] == "cancelled_by_client"
    assert errors[-1]["payload"]["target"] == target
    # The lane's own turn id, not the session's: a client correlating this error
    # to the turn it was streaming has nothing else to match on.
    assert errors[-1]["payload"]["turn_id"] == sent["turn_id"]


async def test_cancelling_one_instance_leaves_another_instance_running(
    emitter: SubscriptionEmitter,
) -> None:
    """Each instance is its own lane, so a targeted cancel reaches exactly one."""
    from raven.rpc.methods import turn as turn_mod

    for handle_name in ("refactor-auth", "write-docs"):
        await turn_send(
            {
                "session_key": "tui:default",
                "content": "go",
                "target": {"agent": "Raven-Code", "handle": handle_name},
            },
            emitter=emitter,
            scheduler=FakeScheduler(),
            turn_ids={},
        )
    other = turn_mod._active_turns[direct_lane("tui:default", "Raven-Code", "write-docs")]

    result = await turn_cancel(
        {"session_key": "tui:default", "target": {"agent": "Raven-Code", "handle": "refactor-auth"}},
        emitter=emitter,
    )

    assert result == {"cancelled": True}
    assert other.cancelled is False


async def test_turn_cancel_for_an_idle_instance_is_not_an_error(emitter: SubscriptionEmitter) -> None:
    """The routine case: Ctrl+C in a direct view whose turn already landed."""
    result = await turn_cancel(
        {"session_key": "tui:default", "target": {"agent": "Raven-Code", "handle": "refactor-auth"}},
        emitter=emitter,
    )
    assert result == {"cancelled": False}


async def test_cancelling_a_main_turn_leaves_the_error_untagged(
    emitter: SubscriptionEmitter, send_frame_capture: AsyncMock
) -> None:
    targets: dict[str, dict[str, str]] = {}
    await turn_subscribe({"session_key": "tui:default"}, emitter=emitter)
    await turn_send(
        {"session_key": "tui:default", "content": "hi"},
        emitter=emitter,
        scheduler=FakeScheduler(),
        turn_ids={},
        direct_targets=targets,
    )

    await turn_cancel({"session_key": "tui:default"}, emitter=emitter, direct_targets=targets)
    await asyncio.sleep(0.05)  # let the coalescer flush

    errors = [e for e in _collect_events(send_frame_capture) if e["type"] == "error"]
    assert "target" not in errors[-1]["payload"]


# ---------------------------------------------------------------------------
# Cancelling a QUEUED turn: driven through the real spine, because the defect is
# in how the lane, the sink and the active-turn slot interact -- a fake scheduler
# has no queue for a turn to sit in.
# ---------------------------------------------------------------------------


async def test_cancelling_a_queued_turn_does_not_wedge_the_lane(emitter: SubscriptionEmitter) -> None:
    """A queued turn is dropped from the lane with no lifecycle event, so nothing
    releases the active-turn slot it bound -- and because that slot is exactly what
    -32003 rejects on, no later turn can become the owner that would release it.
    The lane would stay closed until the process restarts.
    """
    from raven.rpc.methods import turn as _turn_mod
    from raven.rpc.spine import build_rpc_spine
    from raven.spine import Origin, Source, Text, TurnOutcome, TurnRequest, Usage
    from raven.spine.message import ChatType

    gate = asyncio.Event()

    class _GatedLoop:
        tools: dict = {}

        async def run_turn(self, req, emit, drain, **_kwargs):
            await gate.wait()
            await emit(Text(content="announced"))
            return TurnOutcome(usage=Usage(0, 0, 0), explicit_reply=True)

    scheduler, _hub, turn_ids, teardown = build_rpc_spine(_GatedLoop(), emitter, on_turn_end=_turn_mod.clear_active)
    try:
        # An announce the runtime submitted itself takes the lane first.
        announce = scheduler.submit(
            TurnRequest(
                origin=Origin.SUBAGENT,
                source=Source(channel="tui", chat_id="default", sender_id="user", chat_type=ChatType.DM),
                text="done",
                conversation="tui:default",
            )
        )
        await asyncio.sleep(0)  # let the worker pick it up so the next turn queues

        # The user's turn is accepted and queues behind it.
        await turn_send(
            {"session_key": "tui:default", "content": "hi"}, emitter=emitter, scheduler=scheduler, turn_ids=turn_ids
        )
        assert _turn_mod.is_turn_active("tui:default")

        # The user hits stop while their turn is still queued.
        assert await turn_cancel({"session_key": "tui:default"}, emitter=emitter) == {"cancelled": True}

        gate.set()
        await announce.result()
        await asyncio.sleep(0.05)

        assert not _turn_mod.is_turn_active("tui:default"), (
            "the cancelled queued turn left the active-turn slot bound; the lane is wedged"
        )
        # The proof that matters to a user: the next turn is accepted.
        again = await turn_send(
            {"session_key": "tui:default", "content": "again"},
            emitter=emitter,
            scheduler=scheduler,
            turn_ids=turn_ids,
        )
        assert again["accepted"] is True
        gate.set()
    finally:
        await teardown()


async def test_cancelling_a_turn_that_has_not_started_does_not_wedge_the_lane(
    emitter: SubscriptionEmitter,
) -> None:
    """The second door to the same lockout: a turn past the queue but not yet
    started. It is blocked on its origin semaphore -- the user pool is one slot by
    default, so any two user turns on different lanes overlap here -- and a cancel
    landing there unwinds with no lifecycle event, so nothing releases the
    active-turn slot and the lane never reopens.

    This is the half where the payload coroutine has not taken its first step, so
    none of its own handlers can run and only the worker can name the end.

    What makes it deterministic is NOT the pool hold -- the payload never reaches the
    acquire here, and ``cancel_turn`` runs from the assert to ``_run_task.cancel()``
    with no await in between. It is asyncio's FIFO ready queue (the worker is
    scheduled before the payload it creates) plus the premise asserts below.
    ``_payload_reported`` and ``loop.started`` are both also false in the sibling
    test's parked-on-acquire window, so neither fails loudly if the window ever slides
    onto that path instead; ``at_acquire.reached`` is the one that does, since only
    this window never lets the payload's coroutine take a single step. The pool is
    held only so the turn cannot complete and mask it.
    """
    from raven.rpc.methods import turn as _turn_mod
    from raven.rpc.spine import build_rpc_spine
    from raven.spine import Origin, Source, Text, TurnOutcome, TurnRequest, Usage
    from raven.spine.message import ChatType

    holding = asyncio.Event()
    release = asyncio.Event()

    class _PoolHoggingLoop:
        tools: dict = {}

        def __init__(self) -> None:
            self.started: list[str] = []

        async def run_turn(self, req, emit, drain, **_kwargs):
            self.started.append(req.conversation or "")
            holding.set()
            await release.wait()
            await emit(Text(content="done"))
            return TurnOutcome(usage=Usage(0, 0, 0), explicit_reply=True)

    loop = _PoolHoggingLoop()
    scheduler, _hub, turn_ids, teardown = build_rpc_spine(loop, emitter, on_turn_end=_turn_mod.clear_active)
    try:
        # A user turn on another lane takes the single user-pool slot and holds it.
        blocker = scheduler.submit(
            TurnRequest(
                origin=Origin.USER,
                source=Source(channel="tui", chat_id="other", sender_id="user", chat_type=ChatType.DM),
                text="hold",
                conversation="tui:other",
            )
        )
        await holding.wait()
        # Wrapped only now, so the blocker's own acquire does not trip it.
        at_acquire = _AtAcquire(scheduler._pools._user)
        scheduler._pools._user = at_acquire

        # The victim is accepted, leaves its own lane's queue, and blocks on the pool.
        await turn_send(
            {"session_key": "tui:default", "content": "hi"}, emitter=emitter, scheduler=scheduler, turn_ids=turn_ids
        )
        lane = scheduler._lanes["tui:default"]
        for _ in range(1000):
            if lane._running_fut is not None and not lane._pending:
                break
            await asyncio.sleep(0)
        else:  # pragma: no cover - the worker always dequeues an only turn
            raise AssertionError("the turn never left the queue; the premise does not hold")
        assert not lane._payload_reported, "the payload already reported; that is the other half's case"
        assert "tui:default" not in loop.started, "the turn started; this is not the pre-start window"
        assert not at_acquire.reached.is_set(), "the payload reached the pool; that is the other half's case"

        # The user hits stop while it is still waiting for its pool slot.
        assert await turn_cancel({"session_key": "tui:default"}, emitter=emitter) == {"cancelled": True}

        assert not _turn_mod.is_turn_active("tui:default"), (
            "the pre-start cancel left the active-turn slot bound; the lane is wedged"
        )
        again = await turn_send(
            {"session_key": "tui:default", "content": "again"},
            emitter=emitter,
            scheduler=scheduler,
            turn_ids=turn_ids,
        )
        assert again["accepted"] is True
    finally:
        release.set()
        with contextlib.suppress(Exception):
            await blocker.result()
        await teardown()


async def test_cancelling_a_turn_blocked_on_its_pool_does_not_wedge_the_lane(
    emitter: SubscriptionEmitter,
) -> None:
    """The other half of the pre-start window, and the wider one: the payload has
    started and is parked on its origin semaphore, so it can report its own end --
    but only if it does so without a paired TurnStarted, which it used to refuse.

    Here the pool hold IS load-bearing: it is what keeps the payload parked on the
    acquire, so the window this asserts in cannot close under it.
    """
    from raven.rpc.methods import turn as _turn_mod
    from raven.rpc.spine import build_rpc_spine
    from raven.spine import Origin, Source, Text, TurnOutcome, TurnRequest, Usage
    from raven.spine.message import ChatType

    holding = asyncio.Event()
    release = asyncio.Event()

    class _PoolHoggingLoop:
        tools: dict = {}

        def __init__(self) -> None:
            self.started: list[str] = []

        async def run_turn(self, req, emit, drain, **_kwargs):
            self.started.append(req.conversation or "")
            holding.set()
            await release.wait()
            await emit(Text(content="done"))
            return TurnOutcome(usage=Usage(0, 0, 0), explicit_reply=True)

    loop = _PoolHoggingLoop()
    scheduler, _hub, turn_ids, teardown = build_rpc_spine(loop, emitter, on_turn_end=_turn_mod.clear_active)
    try:
        blocker = scheduler.submit(
            TurnRequest(
                origin=Origin.USER,
                source=Source(channel="tui", chat_id="other", sender_id="user", chat_type=ChatType.DM),
                text="hold",
                conversation="tui:other",
            )
        )
        await holding.wait()
        # Wrapped only now, so the blocker's own acquire does not trip it.
        at_acquire = _AtAcquire(scheduler._pools._user)
        scheduler._pools._user = at_acquire

        await turn_send(
            {"session_key": "tui:default", "content": "hi"}, emitter=emitter, scheduler=scheduler, turn_ids=turn_ids
        )
        await at_acquire.reached.wait()  # the payload is parked on the pool
        assert "tui:default" not in loop.started, "it got past the pool; the premise does not hold"

        assert await turn_cancel({"session_key": "tui:default"}, emitter=emitter) == {"cancelled": True}

        assert not _turn_mod.is_turn_active("tui:default"), (
            "the cancel left the active-turn slot bound; the lane is wedged"
        )
        again = await turn_send(
            {"session_key": "tui:default", "content": "again"},
            emitter=emitter,
            scheduler=scheduler,
            turn_ids=turn_ids,
        )
        assert again["accepted"] is True
    finally:
        release.set()
        with contextlib.suppress(Exception):
            await blocker.result()
        await teardown()


async def test_two_concurrent_cancels_still_leave_exactly_one_reported_end(
    emitter: SubscriptionEmitter,
) -> None:
    """Two cancels for one turn, the second landing while the first is still being
    reported. The server dispatches every frame in its own task, so two
    ``turn.cancel`` frames -- or a cancel plus a ``session.interrupt`` -- overlap;
    and the second one fires because the slot it is waiting on is only released at
    the far end of the first report, so it still looks like a live turn.

    The report is interruptible up to the render barrier, so the second cancel can
    destroy it. If the payload were to count as having reported merely by starting
    to, nobody would name this turn's end and the lane would never reopen.
    """
    from raven.rpc.methods import turn as _turn_mod
    from raven.rpc.spine import build_rpc_spine
    from raven.spine import Origin, Source, Text, TurnOutcome, TurnRequest, Usage
    from raven.spine.events import TurnFailed
    from raven.spine.message import ChatType

    holding = asyncio.Event()
    release = asyncio.Event()
    in_report = asyncio.Event()
    let_report_finish = asyncio.Event()
    delivered: list = []

    class _PoolHoggingLoop:
        tools: dict = {}

        def __init__(self) -> None:
            self.started: list[str] = []

        async def run_turn(self, req, emit, drain, **_kwargs):
            self.started.append(req.conversation or "")
            holding.set()
            await release.wait()
            await emit(Text(content="done"))
            return TurnOutcome(usage=Usage(0, 0, 0), explicit_reply=True)

    loop = _PoolHoggingLoop()
    scheduler, _hub, turn_ids, teardown = build_rpc_spine(loop, emitter, on_turn_end=_turn_mod.clear_active)
    inner_sink = scheduler._sink
    parked_once = False

    async def parking_sink(event):
        # Park inside the FIRST terminal report for the victim, standing in for the
        # render barrier's await -- deterministic where racing the hub would not be.
        nonlocal parked_once
        victim_end = isinstance(event, TurnFailed) and event.conversation_id == "tui:default"
        if victim_end and not parked_once:
            parked_once = True
            in_report.set()
            await let_report_finish.wait()
        await inner_sink(event)
        if victim_end:
            delivered.append(event)

    scheduler._sink = parking_sink
    try:
        blocker = scheduler.submit(
            TurnRequest(
                origin=Origin.USER,
                source=Source(channel="tui", chat_id="other", sender_id="user", chat_type=ChatType.DM),
                text="hold",
                conversation="tui:other",
            )
        )
        await holding.wait()
        at_acquire = _AtAcquire(scheduler._pools._user)
        scheduler._pools._user = at_acquire

        await turn_send(
            {"session_key": "tui:default", "content": "hi"}, emitter=emitter, scheduler=scheduler, turn_ids=turn_ids
        )
        await at_acquire.reached.wait()

        # Cancel #1 cannot be awaited: it blocks until the turn unwinds, and the
        # turn is about to park inside its own report.
        first = asyncio.create_task(turn_cancel({"session_key": "tui:default"}, emitter=emitter))
        await in_report.wait()

        # Cancel #2, exactly as the server would deliver it: the slot is still bound,
        # so this looks like a live turn and cancels it again -- inside its report.
        assert _turn_mod.is_turn_active("tui:default"), "premise: the slot is still bound"
        second = asyncio.create_task(turn_cancel({"session_key": "tui:default"}, emitter=emitter))
        await asyncio.sleep(0)
        let_report_finish.set()
        await asyncio.gather(first, second)

        assert len(delivered) == 1, f"expected exactly one reported end; got {len(delivered)}"
        assert not _turn_mod.is_turn_active("tui:default"), (
            "the interrupted report left the active-turn slot bound; the lane is wedged"
        )
        again = await turn_send(
            {"session_key": "tui:default", "content": "again"},
            emitter=emitter,
            scheduler=scheduler,
            turn_ids=turn_ids,
        )
        assert again["accepted"] is True
    finally:
        let_report_finish.set()
        release.set()
        with contextlib.suppress(Exception):
            await blocker.result()
        await teardown()
