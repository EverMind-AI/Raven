"""The spine turn contracts: Origin, BusyPolicy, and TurnRequest."""

import dataclasses

import pytest

from raven.spine import PROACTIVE_ORIGINS, BusyPolicy, ChatType, Origin, Source, TurnRequest


def _src():
    return Source(channel="t", chat_id="c", sender_id="u", chat_type=ChatType.DM)


@pytest.mark.parametrize("parent", ["tui:plain", "tui:issue#42", "email:name#tag@example.com"])
def test_session_key_retains_the_complete_direct_parent(parent):
    from raven.spine import direct_lane, session_key_for

    target = ("Raven", "handle#1")
    request = TurnRequest(
        origin=Origin.USER, source=_src(), text="hi", conversation=direct_lane(parent, *target), direct_target=target
    )
    assert session_key_for(request) == parent
    assert session_key_for(dataclasses.replace(request, parent_session="tui:declared#parent")) == "tui:declared#parent"
    assert session_key_for(dataclasses.replace(request, direct_target=None)) == request.conversation


def test_origin_is_closed_five_value_enum_without_system():
    assert {o.value for o in Origin} == {"user", "sentinel", "cron", "heartbeat", "subagent"}
    assert not hasattr(Origin, "SYSTEM")  # a zero-producer origin, intentionally absent
    with pytest.raises(ValueError):
        Origin("system")


def test_the_proactive_origins_are_every_origin_a_person_does_not_start():
    assert PROACTIVE_ORIGINS == frozenset({Origin.SENTINEL, Origin.CRON, Origin.HEARTBEAT, Origin.SUBAGENT})
    assert PROACTIVE_ORIGINS == frozenset(Origin) - {Origin.USER}, "a new origin has to be put on one side of the line"


@pytest.mark.parametrize("origin", [*Origin, None])
def test_the_proactive_origins_agree_with_the_spelling_the_loop_and_the_pools_used(origin):
    """The stream hold used to read "not a person's turn" and the pools a tuple of four: one set stands for both."""
    assert (origin in PROACTIVE_ORIGINS) == (origin not in (Origin.USER, None))


def test_busy_policy_is_closed_three_value_enum():
    assert {b.value for b in BusyPolicy} == {"append", "inject", "interrupt"}
    with pytest.raises(ValueError):
        BusyPolicy("drop")


def test_enum_str_renders_as_value():
    # StrEnum: str() yields the value. Reverting to (str, Enum) turns this red.
    assert str(Origin.USER) == "user"
    assert str(BusyPolicy.APPEND) == "append"


def test_turn_request_defaults():
    r = TurnRequest(origin=Origin.USER, source=_src(), text="hi")
    assert r.media == ()
    assert r.conversation is None
    assert r.busy is BusyPolicy.APPEND


def test_turn_id_defaults_to_none():
    # None, not "": absent means "the lane mints one", which is what keeps every
    # submit path other than turn.send unchanged.
    r = TurnRequest(origin=Origin.USER, source=_src(), text="hi")
    assert r.turn_id is None


def test_turn_request_carries_no_dead_reply_threading_fields():
    # message_id / reply_to were a designed-but-never-built reply-threading
    # pipe (no setter, no reader anywhere). Removed 2026-08-29; the live design
    # for outbound reply anchoring rides Source, not sibling fields here.
    fields = {f.name for f in dataclasses.fields(TurnRequest)}
    assert "message_id" not in fields
    assert "reply_to" not in fields


def test_turn_request_is_frozen():
    r = TurnRequest(origin=Origin.CRON, source=_src(), text="x")
    with pytest.raises(dataclasses.FrozenInstanceError):
        r.text = "y"


def test_turn_request_is_not_hashable_via_source_extras():
    # Same precedent as Source: frozen advertises __hash__, but the nested
    # Source.extras mapping makes TurnRequest unhashable. Lanes key by the
    # conversation_id string, never by a request object.
    r = TurnRequest(origin=Origin.USER, source=_src(), text="x")
    with pytest.raises(TypeError):
        hash(r)


def test_turn_request_has_no_conversation_id_derivation():
    # conversation_id derivation needs the channel SPEC registry and is therefore
    # a scheduler-step behaviour, not pure data: turn.py carries only the
    # `conversation` override field and never derives.
    assert not hasattr(TurnRequest, "conversation_id")
    fields = {f.name for f in dataclasses.fields(TurnRequest)}
    assert "conversation" in fields
    assert "conversation_id" not in fields


def test_turn_request_deliver_text_defaults_none():
    # New optional field must default None so existing keyword constructors
    # (~15 call sites) are unaffected.
    req = TurnRequest(origin=Origin.USER, source=_src(), text="hi")
    assert req.deliver_text is None


def test_direct_target_defaults_to_none():
    req = TurnRequest(
        origin=Origin.USER,
        source=Source(channel="tui", chat_id="c", sender_id="u", chat_type=ChatType.DM),
        text="hi",
    )
    assert req.direct_target is None


def test_direct_target_carries_agent_and_handle():
    req = TurnRequest(
        origin=Origin.USER,
        source=Source(channel="tui", chat_id="c", sender_id="u", chat_type=ChatType.DM),
        text="hi",
        direct_target=("Raven-Code", "refactor-auth"),
    )
    assert req.direct_target == ("Raven-Code", "refactor-auth")


def test_dispatch_link_defaults_to_none():
    req = TurnRequest(origin=Origin.USER, source=_src(), text="hi")
    assert req.dispatch_link is None


def test_dispatch_link_carries_trace_and_span():
    req = TurnRequest(origin=Origin.SUBAGENT, source=_src(), text="hi", dispatch_link=("trace-1", "span-1"))
    assert req.dispatch_link == ("trace-1", "span-1")
