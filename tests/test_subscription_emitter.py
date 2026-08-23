"""Tests for SubscriptionEmitter — the turn-streaming subsystem."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest

from raven.rpc.subscriptions import (
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


# ---------------------------------------------------------------------------
# replay of a turn in flight
# ---------------------------------------------------------------------------


def _events_for(send_frame_mock: AsyncMock, sub_id: str) -> list[dict]:
    """Only the events that went to one subscription, in order."""
    out = []
    for call in send_frame_mock.call_args_list:
        frame = call.args[0] if call.args else call.kwargs.get("frame")
        if frame and frame.get("method") == "event" and frame["params"]["subscription_id"] == sub_id:
            out.append(frame["params"]["event"])
    return out


def _start(turn_id: str = "t1", content: str = "render this log"):
    return {"type": "message.start", "payload": {"turn_id": turn_id, "content": content}}


def _tok(text: str):
    return {"type": "token.delta", "payload": {"text": text}}


class TestReplayOfATurnInFlight:
    """A window opened mid-turn used to show nothing until the turn ended.

    ``session.resume`` reads the transcript, and a turn is written to it only
    once it finishes -- so a second window, or a shared link, sat on an empty
    stage while the answer was streaming into the first one.
    """

    @pytest.mark.asyncio
    async def test_a_late_subscriber_is_handed_what_already_streamed(
        self, emitter: SubscriptionEmitter, send_frame: AsyncMock
    ) -> None:
        await emitter.emit("s1", _start())
        await emitter.emit("s1", _tok("Hel"))
        await emitter.emit("s1", _tok("lo"))

        late = await emitter.register("s1")
        await asyncio.sleep(COALESCE_WINDOW_S * 3)

        got = _events_for(send_frame, late)
        assert got[0] == _start()
        # Merged on the way into the buffer, so the reader gets the text whole.
        assert "".join(e["payload"]["text"] for e in got if e["type"] == "token.delta") == "Hello"

    @pytest.mark.asyncio
    async def test_the_seam_neither_repeats_nor_drops(
        self, emitter: SubscriptionEmitter, send_frame: AsyncMock
    ) -> None:
        """The whole point of the buffer is this join.

        Replay it twice and the answer stutters; miss the join and a word
        vanishes from the middle of a sentence.
        """
        await emitter.emit("s1", _start())
        await emitter.emit("s1", _tok("before "))
        late = await emitter.register("s1")
        await emitter.emit("s1", _tok("after"))
        await asyncio.sleep(COALESCE_WINDOW_S * 3)

        got = _events_for(send_frame, late)
        text = "".join(e["payload"]["text"] for e in got if e["type"] == "token.delta")
        assert text == "before after"
        assert [e["type"] for e in got].count("message.start") == 1

    @pytest.mark.asyncio
    async def test_a_runtime_turn_is_replayable_for_a_late_subscriber(
        self, emitter: SubscriptionEmitter, send_frame: AsyncMock
    ) -> None:
        """The reviewer's shape: a runtime turn opens with turn.started (not
        message.start -- turn.send owns that), its deltas stream, and a
        subscriber joins mid-turn. The boundary must seed the in-flight buffer
        the way message.start does, or the joiner sees deltas with no opening
        and the wrong workspace counter."""
        await emitter.emit(
            "s1",
            {
                "type": "turn.started",
                "payload": {
                    "turn_id": "t9",
                    "delegated": {
                        "kind": "dag",
                        "label": "run-7",
                        "status": "ok",
                        "run_id": "run-7",
                        "content": "result text",
                    },
                },
            },
        )
        await emitter.emit("s1", _tok("res"))
        await emitter.emit("s1", _tok("ult"))

        late = await emitter.register("s1")
        await asyncio.sleep(COALESCE_WINDOW_S * 3)

        got = _events_for(send_frame, late)
        assert got[0]["type"] == "turn.started"
        assert got[0]["payload"]["delegated"]["kind"] == "dag"
        # The delivery identity survived, and the streamed text came whole.
        assert "".join(e["payload"]["text"] for e in got if e["type"] == "token.delta") == "result"

    @pytest.mark.asyncio
    async def test_a_runtime_turn_ending_clears_the_buffer(
        self, emitter: SubscriptionEmitter, send_frame: AsyncMock
    ) -> None:
        """Same contract as message.start: once the turn is over the transcript
        on disk is the record, so nothing is replayed on top of it."""
        await emitter.emit("s1", {"type": "turn.started", "payload": {"turn_id": "t9"}})
        await emitter.emit("s1", _tok("res"))
        await emitter.emit("s1", {"type": "message.complete", "payload": {"turn_id": "t9", "usage": {}}})

        late = await emitter.register("s1")
        await asyncio.sleep(COALESCE_WINDOW_S * 3)
        assert _events_for(send_frame, late) == []

    @pytest.mark.asyncio
    async def test_a_subscriber_present_all_along_sees_each_event_once(
        self, emitter: SubscriptionEmitter, send_frame: AsyncMock
    ) -> None:
        """Recording must not double-deliver to the subscriptions already open."""
        early = await emitter.register("s1")
        await emitter.emit("s1", _start())
        await emitter.emit("s1", _tok("one"))
        await asyncio.sleep(COALESCE_WINDOW_S * 3)

        got = _events_for(send_frame, early)
        assert [e["type"] for e in got] == ["message.start", "token.delta"]

    @pytest.mark.asyncio
    async def test_nothing_is_replayed_once_the_turn_is_over(
        self, emitter: SubscriptionEmitter, send_frame: AsyncMock
    ) -> None:
        """After the turn lands, the transcript on disk is the record. Replaying
        on top of what ``session.resume`` returns would draw it twice."""
        await emitter.emit("s1", _start())
        await emitter.emit("s1", _tok("done"))
        await emitter.emit("s1", {"type": "message.complete", "payload": {"turn_id": "t1", "usage": {}}})

        late = await emitter.register("s1")
        await asyncio.sleep(COALESCE_WINDOW_S * 3)

        assert _events_for(send_frame, late) == []

    @pytest.mark.asyncio
    async def test_a_failed_turn_also_stops_the_replay(
        self, emitter: SubscriptionEmitter, send_frame: AsyncMock
    ) -> None:
        await emitter.emit("s1", _start())
        await emitter.emit("s1", _tok("half"))
        await emitter.emit("s1", {"type": "error", "payload": {"code": -32000, "message": "turn_failed"}})

        late = await emitter.register("s1")
        await asyncio.sleep(COALESCE_WINDOW_S * 3)

        assert _events_for(send_frame, late) == []

    @pytest.mark.asyncio
    async def test_a_new_turn_replaces_the_previous_buffer(
        self, emitter: SubscriptionEmitter, send_frame: AsyncMock
    ) -> None:
        await emitter.emit("s1", _start("t1", "first"))
        await emitter.emit("s1", _tok("old"))
        await emitter.emit("s1", _start("t2", "second"))
        await emitter.emit("s1", _tok("new"))

        late = await emitter.register("s1")
        await asyncio.sleep(COALESCE_WINDOW_S * 3)

        got = _events_for(send_frame, late)
        assert got[0]["payload"]["turn_id"] == "t2"
        assert "".join(e["payload"]["text"] for e in got if e["type"] == "token.delta") == "new"

    @pytest.mark.asyncio
    async def test_the_buffer_is_bounded_and_keeps_the_head_and_the_tail(
        self, emitter: SubscriptionEmitter, send_frame: AsyncMock
    ) -> None:
        """A long turn must not grow without limit, and the replay must stay
        smaller than one queue -- overflowing it would kill the very
        subscription the replay exists to serve."""
        from raven.rpc.subscriptions import REPLAY_CAPACITY

        await emitter.emit("s1", _start())
        for i in range(REPLAY_CAPACITY * 2):
            # Non-mergeable, so each one is its own buffered event.
            await emitter.emit("s1", {"type": "tool.start", "payload": {"tool_call_id": str(i), "name": "exec"}})

        late = await emitter.register("s1")
        await asyncio.sleep(COALESCE_WINDOW_S * 4)

        got = _events_for(send_frame, late)
        assert len(got) <= REPLAY_CAPACITY < QUEUE_CAPACITY
        # The turn opener survives, so the client has a turn to render into.
        assert got[0]["type"] == "message.start"
        # And the most recent call survives, because that is what is on screen.
        assert got[-1]["payload"]["tool_call_id"] == str(REPLAY_CAPACITY * 2 - 1)

    @pytest.mark.asyncio
    async def test_closing_the_session_drops_the_buffer(
        self, emitter: SubscriptionEmitter, send_frame: AsyncMock
    ) -> None:
        await emitter.emit("s1", _start())
        await emitter.emit("s1", _tok("x"))
        await emitter.close_session("s1")

        late = await emitter.register("s1")
        await asyncio.sleep(COALESCE_WINDOW_S * 3)

        assert _events_for(send_frame, late) == []

    @pytest.mark.asyncio
    async def test_another_session_is_not_replayed_into_this_one(
        self, emitter: SubscriptionEmitter, send_frame: AsyncMock
    ) -> None:
        await emitter.emit("s1", _start())
        await emitter.emit("s1", _tok("mine"))

        late = await emitter.register("s2")
        await asyncio.sleep(COALESCE_WINDOW_S * 3)

        assert _events_for(send_frame, late) == []


# Coalescing must not lose a direct-chat tag
# ---------------------------------------------------------------------------


def test_merging_carries_the_direct_chat_target() -> None:
    """The merged frame is rebuilt, not mutated, so a tag not carried across is
    a tag dropped -- and an untagged delta reads as the main agent's, which is
    exactly what this field exists to prevent."""
    from raven.rpc.subscriptions import _merge_consecutive_token_deltas

    target = {"agent": "Raven-Code", "handle": "refactor-auth"}
    merged = _merge_consecutive_token_deltas(
        [
            {"type": "token.delta", "payload": {"text": "a", "target": target}},
            {"type": "token.delta", "payload": {"text": "b", "target": target}},
        ]
    )

    assert merged == [{"type": "token.delta", "payload": {"text": "ab", "target": target}}]


def test_merging_leaves_an_untagged_run_exactly_as_it_was() -> None:
    from raven.rpc.subscriptions import _merge_consecutive_token_deltas

    merged = _merge_consecutive_token_deltas(
        [
            {"type": "token.delta", "payload": {"text": "a"}},
            {"type": "token.delta", "payload": {"text": "b"}},
        ]
    )

    assert merged == [{"type": "token.delta", "payload": {"text": "ab"}}]


def test_a_run_breaks_where_the_target_changes() -> None:
    """Merging across a change would relabel one conversation's text as another's."""
    from raven.rpc.subscriptions import _merge_consecutive_token_deltas

    target = {"agent": "Raven-Code", "handle": "refactor-auth"}
    merged = _merge_consecutive_token_deltas(
        [
            {"type": "token.delta", "payload": {"text": "a", "target": target}},
            {"type": "token.delta", "payload": {"text": "b"}},
        ]
    )

    assert merged == [
        {"type": "token.delta", "payload": {"text": "a", "target": target}},
        {"type": "token.delta", "payload": {"text": "b"}},
    ]


def test_a_new_run_starts_after_the_target_changes() -> None:
    """Breaking a run must open the next one, not pass the breaking frame through
    and leave the rest unmerged."""
    from raven.rpc.subscriptions import _merge_consecutive_token_deltas

    target = {"agent": "Raven-Code", "handle": "refactor-auth"}
    merged = _merge_consecutive_token_deltas(
        [
            {"type": "token.delta", "payload": {"text": "a", "target": target}},
            {"type": "token.delta", "payload": {"text": "b"}},
            {"type": "token.delta", "payload": {"text": "c"}},
        ]
    )

    assert merged == [
        {"type": "token.delta", "payload": {"text": "a", "target": target}},
        {"type": "token.delta", "payload": {"text": "bc"}},
    ]


# startup-event buffer (queue_startup_event → flushed on first register)
# ---------------------------------------------------------------------------


async def test_startup_event_flushed_to_first_registration(
    emitter: SubscriptionEmitter,
    send_frame: AsyncMock,
) -> None:
    """An event queued before any subscription exists SHALL be delivered to
    the first subscription that registers."""
    event = {"type": "cron.missed", "payload": {"count": 1, "items": []}}
    emitter.queue_startup_event(event)
    await emitter.register("tui:default")
    await asyncio.sleep(COALESCE_WINDOW_S * 3)

    events = _collect_emitted_events(send_frame)
    assert events == [event]


async def test_startup_event_not_redelivered_to_later_registrations(
    emitter: SubscriptionEmitter,
    send_frame: AsyncMock,
) -> None:
    """The buffer is one-shot: a second registration (re-attach, session
    switch) SHALL NOT receive the startup events again."""
    emitter.queue_startup_event({"type": "cron.missed", "payload": {"count": 1, "items": []}})
    sub_id = await emitter.register("tui:default")
    await asyncio.sleep(COALESCE_WINDOW_S * 3)
    await emitter.unregister(sub_id)
    first_count = len(_collect_emitted_events(send_frame))

    await emitter.register("tui:other")
    await asyncio.sleep(COALESCE_WINDOW_S * 3)

    assert len(_collect_emitted_events(send_frame)) == first_count == 1


async def test_register_without_startup_events_emits_nothing(
    emitter: SubscriptionEmitter,
    send_frame: AsyncMock,
) -> None:
    await emitter.register("tui:default")
    await asyncio.sleep(COALESCE_WINDOW_S * 3)
    assert _collect_emitted_events(send_frame) == []
