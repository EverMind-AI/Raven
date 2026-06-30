"""Tests for SubscriptionEmitter — the turn-streaming subsystem."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest

from raven.tui_rpc.subscriptions import (
    COALESCE_WINDOW_S,
    QUEUE_CAPACITY,
    SubscriptionEmitter,
)


@pytest.fixture
def send_frame() -> AsyncMock:
    return AsyncMock(return_value=None)


@pytest.fixture
def emitter(send_frame: AsyncMock) -> SubscriptionEmitter:
    return SubscriptionEmitter(send_frame=send_frame)


def _collect_emitted_events(send_frame_mock: AsyncMock) -> list[dict]:
    """Extract the `event` field from every recorded `send_frame` call."""
    events = []
    for call in send_frame_mock.call_args_list:
        frame = call.args[0] if call.args else call.kwargs.get("frame")
        if frame and frame.get("method") == "event":
            events.append(frame["params"]["event"])
    return events


# ---------------------------------------------------------------------------
# register / unregister
# ---------------------------------------------------------------------------


async def test_register_returns_sub_id_and_indexes(
    emitter: SubscriptionEmitter,
) -> None:
    sub_id = await emitter.register("tui:default")
    assert isinstance(sub_id, str)
    assert len(sub_id) >= 16  # uuid hex
    assert sub_id in emitter._by_id
    assert any(s.sub_id == sub_id for s in emitter._by_session["tui:default"])


async def test_unregister_existing_returns_true(
    emitter: SubscriptionEmitter,
) -> None:
    sub_id = await emitter.register("tui:default")
    assert await emitter.unregister(sub_id) is True
    assert sub_id not in emitter._by_id


async def test_unregister_unknown_returns_false_idempotent(
    emitter: SubscriptionEmitter,
) -> None:
    assert await emitter.unregister("nonexistent-sub-id-xxxxx") is False


async def test_unregister_twice_second_call_returns_false(
    emitter: SubscriptionEmitter,
) -> None:
    sub_id = await emitter.register("tui:default")
    assert await emitter.unregister(sub_id) is True
    assert await emitter.unregister(sub_id) is False


# ---------------------------------------------------------------------------
# emit + coalesce
# ---------------------------------------------------------------------------


async def test_emit_delivers_to_single_subscriber(
    emitter: SubscriptionEmitter,
    send_frame: AsyncMock,
) -> None:
    """Single event → 1 frame written to the wire (after coalesce window)."""
    await emitter.register("tui:default")
    await emitter.emit(
        "tui:default",
        {"type": "message.start", "payload": {"turn_id": "t1"}},
    )
    await asyncio.sleep(COALESCE_WINDOW_S * 3)  # let coalesce loop flush
    events = _collect_emitted_events(send_frame)
    assert len(events) == 1
    assert events[0]["type"] == "message.start"


async def test_16ms_coalesce_merges_consecutive_token_deltas(
    emitter: SubscriptionEmitter,
    send_frame: AsyncMock,
) -> None:
    """5 consecutive token.delta within 16ms → 1 frame with merged text."""
    await emitter.register("tui:default")
    for piece in ["He", "llo", " ", "wo", "rld"]:
        await emitter.emit(
            "tui:default",
            {"type": "token.delta", "payload": {"text": piece}},
        )
    await asyncio.sleep(COALESCE_WINDOW_S * 4)
    events = _collect_emitted_events(send_frame)
    delta_events = [e for e in events if e["type"] == "token.delta"]
    # All 5 pieces should coalesce into 1 frame (or at most very few).
    assert len(delta_events) <= 2, f"expected coalesced ≤2 delta frames; got {len(delta_events)}: {delta_events}"
    merged_text = "".join(e["payload"]["text"] for e in delta_events)
    assert merged_text == "Hello world"


async def test_mixed_events_preserve_order(
    emitter: SubscriptionEmitter,
    send_frame: AsyncMock,
) -> None:
    """token.delta / tool.start / token.delta / message.complete preserves order;
    only consecutive deltas merge.
    """
    await emitter.register("tui:default")
    sequence = [
        {"type": "token.delta", "payload": {"text": "A"}},
        {"type": "token.delta", "payload": {"text": "B"}},
        {
            "type": "tool.start",
            "payload": {
                "tool_call_id": "tc1",
                "name": "fs.read",
                "arguments": {"path": "/tmp"},
            },
        },
        {"type": "token.delta", "payload": {"text": "C"}},
        {
            "type": "message.complete",
            "payload": {
                "turn_id": "t1",
                "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
            },
        },
    ]
    for ev in sequence:
        await emitter.emit("tui:default", ev)
    await asyncio.sleep(COALESCE_WINDOW_S * 5)

    events = _collect_emitted_events(send_frame)
    types = [e["type"] for e in events]
    # Expected: ["token.delta" (merged "AB"), "tool.start", "token.delta" ("C"), "message.complete"]
    assert types == ["token.delta", "tool.start", "token.delta", "message.complete"], f"order not preserved: {types}"
    assert events[0]["payload"]["text"] == "AB"
    assert events[2]["payload"]["text"] == "C"


# ---------------------------------------------------------------------------
# Overflow
# ---------------------------------------------------------------------------


async def test_queue_overflow_emits_error_and_closes(
    send_frame: AsyncMock,
) -> None:
    """When queue exceeds capacity → emit error(code=-32016) + close sub."""
    emitter = SubscriptionEmitter(send_frame=send_frame)
    sub_id = await emitter.register("tui:default")

    # Fill the queue beyond capacity. We use a delta event (size matters less
    # than count; queue cap is item count).
    # We do NOT let the coalesce loop drain — to stress the put_nowait path,
    # we push faster than 16ms window can drain. Simplest: push immediately
    # without awaiting between emits; the coalesce loop yields after 1 get
    # then sleeps 16ms — during that 16ms we push QUEUE_CAPACITY+ items.
    # But after the first await get(), the queue contains 0 items briefly.
    # To deterministically trigger overflow, push QUEUE_CAPACITY + 100 events
    # with no awaits in between (the loop has not had a chance to wake).
    for i in range(QUEUE_CAPACITY + 100):
        await emitter.emit(
            "tui:default",
            {"type": "token.delta", "payload": {"text": str(i)}},
        )

    await asyncio.sleep(COALESCE_WINDOW_S * 5)

    events = _collect_emitted_events(send_frame)
    overflow_errors = [e for e in events if e.get("type") == "error" and e.get("payload", {}).get("code") == -32016]
    assert len(overflow_errors) >= 1, f"expected ≥1 overflow error; got events: {[e['type'] for e in events]}"

    # Subscription should be closed
    assert sub_id not in emitter._by_id


# ---------------------------------------------------------------------------
# Multi-subscriber on same session
# ---------------------------------------------------------------------------


async def test_multiple_subs_same_session_both_receive(
    send_frame: AsyncMock,
) -> None:
    """Two subs on same session → emit reaches both."""
    emitter = SubscriptionEmitter(send_frame=send_frame)
    sub_a = await emitter.register("tui:default")
    sub_b = await emitter.register("tui:default")
    assert sub_a != sub_b

    await emitter.emit(
        "tui:default",
        {"type": "message.start", "payload": {"turn_id": "t1"}},
    )
    await asyncio.sleep(COALESCE_WINDOW_S * 3)

    # Count frames per subscription_id
    sub_a_frames = 0
    sub_b_frames = 0
    for call in send_frame.call_args_list:
        frame = call.args[0] if call.args else call.kwargs.get("frame")
        if frame and frame.get("method") == "event":
            sid = frame["params"]["subscription_id"]
            if sid == sub_a:
                sub_a_frames += 1
            elif sid == sub_b:
                sub_b_frames += 1
    assert sub_a_frames >= 1, f"sub_a got {sub_a_frames} frames"
    assert sub_b_frames >= 1, f"sub_b got {sub_b_frames} frames"


async def test_close_session_closes_all_subscriptions_for_session(
    emitter: SubscriptionEmitter,
) -> None:
    """close_session('sk') unregisters every sub for that session."""
    sub_a = await emitter.register("tui:default")
    sub_b = await emitter.register("tui:default")
    sub_c = await emitter.register("tui:other")  # different session

    await emitter.close_session("tui:default")

    assert sub_a not in emitter._by_id
    assert sub_b not in emitter._by_id
    assert sub_c in emitter._by_id  # other session untouched


# ---------------------------------------------------------------------------
# Closed subscription does not receive new events
# ---------------------------------------------------------------------------


async def test_closed_subscription_does_not_receive_new_events(
    emitter: SubscriptionEmitter,
    send_frame: AsyncMock,
) -> None:
    """After unregister, subsequent emit on same session does NOT reach the closed sub."""
    sub_id = await emitter.register("tui:default")
    await emitter.emit(
        "tui:default",
        {"type": "message.start", "payload": {"turn_id": "t1"}},
    )
    await asyncio.sleep(COALESCE_WINDOW_S * 3)
    pre_count = send_frame.call_count

    await emitter.unregister(sub_id)

    await emitter.emit(
        "tui:default",
        {"type": "message.start", "payload": {"turn_id": "t2"}},
    )
    await asyncio.sleep(COALESCE_WINDOW_S * 3)
    post_count = send_frame.call_count
    assert post_count == pre_count, f"closed sub received events after unregister: pre={pre_count} post={post_count}"
