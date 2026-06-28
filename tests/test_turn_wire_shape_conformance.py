"""Wire-shape conformance tests — every TurnEvent emitted by the turn.* flow
must round-trip through the Pydantic ``TurnEvent`` discriminated union.

Background: the previous
``message.complete`` payload (`{turn_id, content}`) passed unit tests because
emissions are raw dicts that never round-trip Pydantic. Real TS clients with
``additionalProperties: false`` validators would reject. This test asserts the
contract by routing every emitted event back through
``pydantic.TypeAdapter(TurnEvent)``.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest
from pydantic import TypeAdapter

from raven.spine import ChatType, Origin, Source, TurnRequest
from raven.tui_rpc.methods.turn import turn_cancel, turn_send, turn_subscribe
from raven.tui_rpc.models import TurnEvent
from raven.tui_rpc.spine import build_tui
from raven.tui_rpc.subscriptions import SubscriptionEmitter

_turn_event_adapter: TypeAdapter[TurnEvent] = TypeAdapter(TurnEvent)


class FakeHandle:
    def cancel(self) -> None:
        pass

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


def _collect_events(send_frame: AsyncMock) -> list[dict]:
    events = []
    for call in send_frame.call_args_list:
        frame = call.args[0] if call.args else call.kwargs.get("frame")
        if frame and frame.get("method") == "event":
            events.append(frame["params"]["event"])
    return events


def _assert_event_validates(event: dict) -> None:
    _turn_event_adapter.validate_python(event)


def _req() -> TurnRequest:
    return TurnRequest(
        origin=Origin.USER,
        source=Source(channel="tui", chat_id="default", sender_id="user", chat_type=ChatType.DM),
        text="hi",
        conversation="tui:default",
    )


# --- message.complete payload shape (B1 regression guard) ---


async def test_message_complete_payload_has_turn_id_and_usage_only() -> None:
    """B1 regression: message.complete payload must be ``{turn_id, usage}``.

    Driven through the real spine (build_tui): the sink emits message.complete
    after the render barrier.
    """
    send_frame = AsyncMock(return_value=None)
    emitter = SubscriptionEmitter(send_frame=send_frame)

    class _StubAgent:
        tools: dict = {}

        async def run_turn(self, req, emit, drain, *, stream, usage_sink=None, text_sink=None):
            from raven.spine import Text, TurnOutcome, Usage

            await emit(Text(content="stub content (must not appear on wire)", source=req.source))
            if usage_sink is not None:
                usage_sink.update({"prompt_tokens": 5, "completion_tokens": 10, "total_tokens": 15})
            return TurnOutcome(usage=Usage(0, 0, 0), explicit_reply=True)

    scheduler, _hub, turn_ids, teardown = build_tui(_StubAgent(), emitter)
    try:
        await emitter.register("tui:default")
        turn_ids["tui:default"] = "t1"
        await scheduler.submit(_req()).result()
        await asyncio.sleep(0.1)  # let the coalescer flush
    finally:
        await teardown()

    events = _collect_events(send_frame)
    completions = [e for e in events if e.get("type") == "message.complete"]
    assert len(completions) == 1, f"expected 1 message.complete; got {events}"

    payload = completions[0]["payload"]
    assert set(payload) == {"turn_id", "usage"}, (
        f"payload keys must be exactly {{turn_id, usage}}; got {set(payload)}"
    )
    assert "content" not in payload  # B1: must not leak
    _assert_event_validates(completions[0])  # Pydantic accepts


# --- error event shape — no ``detail`` field (B2 regression guard) ---


async def test_overflow_error_event_payload_shape() -> None:
    """B2 regression: overflow error must be ``{code: -32016, message, reason}``;
    no ``detail`` field per ``ErrorEventPayload`` additionalProperties: false."""
    send_frame = AsyncMock(return_value=None)
    emitter = SubscriptionEmitter(send_frame=send_frame)

    sub_id = await emitter.register("tui:default")
    # Force overflow by pushing beyond queue capacity without yielding.
    from raven.tui_rpc.subscriptions import QUEUE_CAPACITY
    for i in range(QUEUE_CAPACITY + 50):
        await emitter.emit(
            "tui:default",
            {"type": "token.delta", "payload": {"text": str(i)}},
        )
    await asyncio.sleep(0.1)

    events = _collect_events(send_frame)
    errors = [
        e for e in events
        if e.get("type") == "error"
        and e.get("payload", {}).get("code") == -32016
    ]
    assert len(errors) >= 1, f"expected ≥1 -32016 overflow event; got {events}"
    err = errors[0]
    assert set(err["payload"]) <= {"code", "message", "reason"}, (
        f"error payload must only contain {{code, message, reason?}}; "
        f"got {set(err['payload'])}"
    )
    assert "detail" not in err["payload"]  # B2: must not leak
    _assert_event_validates(err)  # Pydantic accepts

    # Smoke: sub got closed after overflow.
    assert sub_id not in emitter._by_id


async def test_cancel_error_event_payload_shape() -> None:
    """B2 regression: turn.cancel cancelled_by_client error must validate."""
    send_frame = AsyncMock(return_value=None)
    emitter = SubscriptionEmitter(send_frame=send_frame)

    await turn_subscribe({"session_key": "tui:default"}, emitter=emitter)
    await turn_send(
        {"session_key": "tui:default", "content": "hi"},
        emitter=emitter,
        scheduler=FakeScheduler(),
        turn_ids={},
    )
    await turn_cancel({"session_key": "tui:default"}, emitter=emitter)
    await asyncio.sleep(0.1)

    events = _collect_events(send_frame)
    cancels = [
        e for e in events
        if e.get("type") == "error"
        and e.get("payload", {}).get("reason") == "cancelled_by_client"
    ]
    assert len(cancels) >= 1
    payload = cancels[0]["payload"]
    assert set(payload) <= {"code", "message", "reason"}
    assert "detail" not in payload
    _assert_event_validates(cancels[0])
