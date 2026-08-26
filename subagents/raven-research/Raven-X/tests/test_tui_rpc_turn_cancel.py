"""Tests for ``turn.cancel`` real handler.

``turn.cancel`` cancels the in-flight turn handle, emits the one
``error(reason="cancelled_by_client")`` (the build_tui sink stays silent on a
cancelled TurnFailed to avoid a double error), then drains the handle. The
per-turn cancel must leave the session-scoped subscription open.

These tests drive the handler with a fake Scheduler/handle + a real
SubscriptionEmitter; the spine streaming path is covered in
``test_tui_rpc_spine.py``.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest

from raven.tui_rpc.dispatcher import Dispatcher
from raven.tui_rpc.methods.turn import (
    register_turn_methods,
    turn_cancel,
    turn_send,
    turn_subscribe,
)
from raven.tui_rpc.subscriptions import SubscriptionEmitter


class FakeHandle:
    def __init__(self) -> None:
        self.cancelled = False

    def cancel(self) -> None:
        self.cancelled = True

    async def result(self):
        return None


class FakeScheduler:
    def submit(self, req):
        return FakeHandle()


@pytest.fixture(autouse=True)
def _clear_active_turns():
    from raven.tui_rpc.methods import turn as _turn_mod

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
    from raven.tui_rpc.methods import turn as turn_mod

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


# --- The never-started turn's slot ---


async def test_turn_cancel_of_a_never_started_turn_frees_the_slot(
    emitter: SubscriptionEmitter,
) -> None:
    """A turn cancelled before it starts (still queued on its lane, or waiting
    on its origin pool) emits no terminating event, so the sink never fires
    clear_active -- turn_cancel itself must free the slot, or every later
    turn.send on this session answers -32003 until process restart. The
    FakeHandle here is exactly that shape: result() resolves with no sink
    event ever firing."""
    from raven.tui_rpc.methods import turn as turn_mod

    await turn_send(
        {"session_key": "tui:default", "content": "x"}, emitter=emitter, scheduler=FakeScheduler(), turn_ids={}
    )
    await turn_cancel({"session_key": "tui:default"}, emitter=emitter)

    assert not turn_mod.is_turn_active("tui:default")
    result = await turn_send(
        {"session_key": "tui:default", "content": "y"}, emitter=emitter, scheduler=FakeScheduler(), turn_ids={}
    )
    assert result["accepted"] is True


async def test_turn_cancel_drop_is_identity_guarded(
    emitter: SubscriptionEmitter,
) -> None:
    """While cancel drains the unwind, the sink may drop the slot and a next
    turn may claim it; cancel's own drop must not evict the successor."""
    from raven.tui_rpc.methods import turn as turn_mod

    unwind: asyncio.Future = asyncio.get_running_loop().create_future()

    class GatedHandle(FakeHandle):
        async def result(self):
            await unwind
            return None

    class GatedScheduler:
        def submit(self, req):
            return GatedHandle()

    await turn_send(
        {"session_key": "tui:default", "content": "x"}, emitter=emitter, scheduler=GatedScheduler(), turn_ids={}
    )
    cancel = asyncio.ensure_future(turn_cancel({"session_key": "tui:default"}, emitter=emitter))
    await asyncio.sleep(0.01)
    assert not cancel.done(), "cancel must wait for the turn's unwind"

    # Play the sink at the old turn's end, then let the next turn claim the slot.
    turn_mod.clear_active("tui:default")
    await turn_send(
        {"session_key": "tui:default", "content": "y"}, emitter=emitter, scheduler=FakeScheduler(), turn_ids={}
    )
    successor = turn_mod._active_turns["tui:default"]

    unwind.set_result(None)
    assert (await cancel) == {"cancelled": True}
    assert turn_mod._active_turns.get("tui:default") is successor


# --- Params validation ---


async def test_turn_cancel_rejects_missing_session_key(emitter: SubscriptionEmitter) -> None:
    with pytest.raises(Exception):  # noqa: BLE001
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
