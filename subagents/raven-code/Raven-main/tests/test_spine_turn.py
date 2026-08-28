import dataclasses

import pytest

from raven.spine import BusyPolicy, ChatType, Origin, Source, TurnRequest


def _src():
    return Source(channel="t", chat_id="c", sender_id="u", chat_type=ChatType.DM)


def test_origin_is_closed_five_value_enum_without_system():
    assert {o.value for o in Origin} == {"user", "sentinel", "cron", "heartbeat", "subagent"}
    assert not hasattr(Origin, "SYSTEM")  # a zero-producer origin, intentionally absent
    with pytest.raises(ValueError):
        Origin("system")


def test_busy_policy_is_closed_three_value_enum():
    assert {b.value for b in BusyPolicy} == {"append", "inject", "interrupt"}
    with pytest.raises(ValueError):
        BusyPolicy("drop")


def test_enum_str_renders_as_value():
    # StrEnum: str() yields the value. Reverting to (str, Enum) turns this red.
    assert str(Origin.USER) == "user"
    assert str(BusyPolicy.APPEND) == "append"


def test_message_id_and_conversation_are_independent_axes():
    # Two orthogonal axes: conversation = coarse session/lane ownership (stable
    # across a conversation), message_id = this single inbound message's id
    # (varies per message). Same source + different message_id must not couple.
    base = dict(origin=Origin.USER, source=_src(), text="x")
    a = TurnRequest(**base, message_id="557")
    b = TurnRequest(**base, message_id="558")
    assert a.message_id != b.message_id
    assert a.conversation is None and b.conversation is None


def test_turn_request_defaults():
    r = TurnRequest(origin=Origin.USER, source=_src(), text="hi")
    assert r.media == ()
    assert r.message_id is None
    assert r.conversation is None
    assert r.busy is BusyPolicy.APPEND


def test_turn_request_carries_message_id_not_reply_to():
    fields = {f.name for f in dataclasses.fields(TurnRequest)}
    assert "message_id" in fields
    assert "reply_to" not in fields  # reply_to is the outbound Text field, not here


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
