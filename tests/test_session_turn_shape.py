"""What a stored transcript says about the turn it holds: did the message tool answer it?"""

from raven.session.turn_shape import calls_message_tool, turn_called_message_tool


def _stored_call(name: str) -> dict:
    """A call as a stored row keeps it: the provider's own shape."""
    return {"id": "c1", "type": "function", "function": {"name": name, "arguments": "{}"}}


def _wire_call(name: str) -> dict:
    """A call as the wire carries it: flattened."""
    return {"id": "c1", "name": name, "arguments": "{}"}


def test_a_stored_row_that_called_the_message_tool_says_so():
    assert calls_message_tool({"role": "assistant", "tool_calls": [_stored_call("message")]})


def test_a_wire_row_that_called_the_message_tool_says_so():
    assert calls_message_tool({"role": "assistant", "tool_calls": [_wire_call("message")]})


def test_a_row_that_called_another_tool_did_not_answer():
    assert not calls_message_tool({"role": "assistant", "tool_calls": [_stored_call("exec")]})


def test_a_row_that_called_nothing_did_not_answer():
    assert not calls_message_tool({"role": "assistant", "tool_calls": []})
    assert not calls_message_tool({"role": "assistant", "content": "[SILENT]"})


def test_only_an_assistant_row_calls_tools():
    assert not calls_message_tool({"role": "user", "tool_calls": [_stored_call("message")]})


def test_a_turn_is_answered_when_any_of_its_rows_called_the_message_tool():
    rows = [
        {"role": "user", "content": "[Unprompted report from 'ci']\n\n3 tests failed", "origin": "subagent"},
        {"role": "assistant", "content": "", "tool_calls": [_stored_call("message")]},
        {"role": "tool", "content": "Message sent", "tool_call_id": "c1", "name": "message"},
        {"role": "assistant", "content": "[SILENT]", "silent": True},
    ]
    assert turn_called_message_tool(rows)


def test_a_turn_that_called_another_tool_only_is_not_answered():
    rows = [
        {"role": "user", "content": "[Unprompted report from 'ci']\n\n3 tests failed", "origin": "subagent"},
        {"role": "assistant", "content": "", "tool_calls": [_stored_call("list_dir")]},
        {"role": "tool", "content": "inbox", "tool_call_id": "c1", "name": "list_dir"},
        {"role": "assistant", "content": "[SILENT]", "silent": True},
    ]
    assert not turn_called_message_tool(rows)
