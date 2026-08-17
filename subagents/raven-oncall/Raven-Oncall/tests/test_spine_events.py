import dataclasses
from typing import get_args

import pytest

from raven.spine import (
    ChatType,
    Deliverable,
    Media,
    MediaOut,
    Notice,
    NoticeKind,
    Reasoning,
    RunnerEvent,
    Source,
    StreamDelta,
    Text,
    ToolEvent,
    ToolPhase,
    TurnEnded,
    TurnEvent,
    TurnFailed,
    TurnStarted,
    Usage,
)


def test_usage_is_frozen_with_three_int_fields():
    u = Usage(prompt_tokens=1, completion_tokens=2, total_tokens=3)
    assert (u.prompt_tokens, u.completion_tokens, u.total_tokens) == (1, 2, 3)
    with pytest.raises(dataclasses.FrozenInstanceError):
        u.total_tokens = 9


def test_notice_kind_is_closed_enum_with_progress_and_tool_hint():
    # tool_hint is a distinct kind so an outlet can gate it on send_tool_hints
    # separately from progress (send_progress), as the bus path did.
    assert {k.value for k in NoticeKind} == {
        "progress",
        "tool_hint",
        "injected",
        "delivery_failed",
    }
    assert str(NoticeKind.PROGRESS) == "progress"
    assert str(NoticeKind.TOOL_HINT) == "tool_hint"
    with pytest.raises(ValueError):
        NoticeKind("hint")


def test_tool_phase_is_closed_two_value_enum():
    assert {p.value for p in ToolPhase} == {"start", "complete"}
    assert str(ToolPhase.START) == "start"
    with pytest.raises(ValueError):
        ToolPhase("error")  # tool failure is ToolEvent.ok=False, not a phase


def test_lifecycle_events_construct_and_are_frozen():
    TurnStarted()
    f = TurnFailed(error="boom", cancelled=False)
    e = TurnEnded(usage=Usage(1, 2, 3), latency_ms=12.5, explicit_reply=True)
    with pytest.raises(dataclasses.FrozenInstanceError):
        f.error = "other"
    with pytest.raises(dataclasses.FrozenInstanceError):
        e.explicit_reply = False


def test_turn_failed_has_no_usage():
    fields = {f.name for f in dataclasses.fields(TurnFailed)}
    assert fields == {"error", "cancelled", "conversation_id"}  # failure carries no usage


def test_turn_ended_carries_usage_latency_and_explicit_reply():
    fields = {f.name for f in dataclasses.fields(TurnEnded)}
    assert fields == {"usage", "latency_ms", "explicit_reply", "conversation_id"}


def test_every_deliverable_defaults_source_to_none():
    # All six deliverables carry source (default None) so the hub can route every
    # one statelessly by source.channel; emit stamps the turn's source on arrival.
    m = Media(path="/tmp/a.jpg", mime="image/jpeg", kind="image")
    assert Text(content="hi").source is None
    assert MediaOut(media=(m,)).source is None
    assert ToolEvent(phase=ToolPhase.START, tool_call_id="t1", name="t").source is None
    assert StreamDelta(delta="d").source is None
    assert Reasoning(content="r").source is None
    assert Notice(kind=NoticeKind.PROGRESS).source is None
    # the other defaulted stamp/payload slots
    assert Text(content="hi").reply_to is None
    assert StreamDelta(delta="d").stream_id is None
    assert Notice(kind=NoticeKind.PROGRESS).detail is None


def test_every_turn_event_defaults_conversation_id_to_none():
    # conversation_id is one axis across all nine events — six deliverables and
    # the three lifecycle events — so a stream-scoped consumer (the hub) can
    # correlate any of them to its lane; emit/worker stamp it on arrival.
    m = Media(path="/tmp/a.jpg", mime="image/jpeg", kind="image")
    deliverables = [
        Text(content="hi"),
        MediaOut(media=(m,)),
        ToolEvent(phase=ToolPhase.START, tool_call_id="t1", name="t"),
        StreamDelta(delta="d"),
        Reasoning(content="r"),
        Notice(kind=NoticeKind.PROGRESS),
    ]
    lifecycle = [
        TurnStarted(),
        TurnFailed(error="e", cancelled=False),
        TurnEnded(usage=Usage(0, 0, 0), latency_ms=1.0, explicit_reply=False),
    ]
    for event in deliverables + lifecycle:
        assert event.conversation_id is None
    # lifecycle carries the correlation key but no source — it is never routed
    assert not any(hasattr(e, "source") for e in lifecycle)


def test_media_out_carries_a_media_tuple():
    m = Media(path="/tmp/a.jpg", mime="image/jpeg", kind="image")
    out = MediaOut(media=(m, m))
    assert out.media == (m, m)


def test_tool_event_phase_is_typed():
    src = Source(channel="t", chat_id="c", sender_id="u", chat_type=ChatType.DM)
    ev = ToolEvent(phase=ToolPhase.START, tool_call_id="t1", name="grep")
    assert ev.phase is ToolPhase.START
    assert Text(content="x", source=src).source is src


def test_runner_event_is_the_six_deliverables_excluding_lifecycle():
    runner_members = set(get_args(RunnerEvent))
    assert runner_members == {ToolEvent, Text, MediaOut, StreamDelta, Reasoning, Notice}
    # lifecycle events are not deliverable: the basis for narrowing the runner's emit
    assert TurnStarted not in runner_members
    assert TurnFailed not in runner_members
    assert TurnEnded not in runner_members
    turn_members = set(get_args(TurnEvent))
    assert runner_members <= turn_members
    assert {TurnStarted, TurnFailed, TurnEnded} <= turn_members


def test_deliverable_is_the_runner_event_union_under_its_delivery_role_name():
    assert Deliverable is RunnerEvent  # one union, two role names; not a separate type


def test_notice_wraps_a_typed_kind_and_carries_source_and_detail():
    src = Source(channel="t", chat_id="c", sender_id="u", chat_type=ChatType.DM)
    n = Notice(kind=NoticeKind.DELIVERY_FAILED, source=src, detail="telegram send failed")
    assert n.kind is NoticeKind.DELIVERY_FAILED  # kind stays the closed-set enum
    assert n.source is src and n.detail == "telegram send failed"  # parallel fields, not in the enum


def test_reasoning_carries_content_and_is_frozen():
    r = Reasoning(content="thinking out loud")
    assert r.content == "thinking out loud"
    with pytest.raises(dataclasses.FrozenInstanceError):
        r.content = "other"


_ALL_EVENTS = [
    TurnStarted(),
    TurnFailed(error="e", cancelled=False),
    TurnEnded(usage=Usage(0, 0, 0), latency_ms=1.0, explicit_reply=False),
    ToolEvent(phase=ToolPhase.START, tool_call_id="t1", name="t"),
    Text(content="x"),
    MediaOut(media=()),
    StreamDelta(delta="d"),
    Reasoning(content="r"),
    Notice(kind=NoticeKind.PROGRESS),
]


@pytest.mark.parametrize("event", _ALL_EVENTS, ids=lambda e: type(e).__name__)
def test_every_event_type_is_frozen(event):
    with pytest.raises(dataclasses.FrozenInstanceError):
        setattr(event, "_probe", 1)
