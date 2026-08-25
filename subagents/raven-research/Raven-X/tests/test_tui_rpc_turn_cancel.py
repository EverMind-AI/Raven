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
