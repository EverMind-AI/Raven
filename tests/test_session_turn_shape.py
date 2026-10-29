"""What a stored transcript says about the turn it holds: did the message tool answer it?"""

from raven.rpc.methods.session import _map_to_wire
from raven.session.turn_shape import replied_via_message_tool


def _message_call() -> dict:
    arguments = '{"content": "3 tests failed"}'
    return {"id": "m1", "type": "function", "function": {"name": "message", "arguments": arguments}}


def _turn(*, answered: bool, sent: str = "Message sent to cron:job") -> list[dict]:
    closing = {"role": "assistant", "content": "[SILENT]", "silent": True}
    if answered:
        closing["replied_via_tool"] = True
    return [
        {"role": "user", "content": "[Scheduled Task] check the build", "origin": "cron"},
        {"role": "assistant", "content": "", "tool_calls": [_message_call()]},
        {"role": "tool", "content": sent, "tool_call_id": "m1", "name": "message"},
        closing,
    ]


def test_a_stored_turn_is_answered_by_its_closing_rows_verdict():
    assert replied_via_message_tool(_turn(answered=True)[-1])


def test_a_wire_turn_is_answered_by_the_same_verdict():
    assert replied_via_message_tool(_map_to_wire(_turn(answered=True), "cron:job")[-1])


def test_a_message_call_that_failed_did_not_answer_in_either_shape():
    stored = _turn(answered=False, sent="Error sending message: the channel is down")
    assert not any(replied_via_message_tool(row) for row in stored)
    assert not any(replied_via_message_tool(row) for row in _map_to_wire(stored, "cron:job"))


def test_only_an_assistant_row_carries_the_verdict():
    assert not replied_via_message_tool({"role": "user", "content": "hi", "replied_via_tool": True})
    assert not replied_via_message_tool({"role": "assistant", "content": "[SILENT]", "replied_via_tool": "yes"})
