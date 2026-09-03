"""Splitting a reply for a channel's length limit, and the assistant message a
tool round appends. Three adapters (discord, telegram, weixin) send through the
splitter, and none of their tests reaches a message long enough to split.
"""

from __future__ import annotations

from raven.utils.messages import build_assistant_message, split_message


def test_a_message_within_the_limit_is_one_chunk() -> None:
    assert split_message("short", max_len=10) == ["short"]
    assert split_message("") == []


def test_a_split_prefers_the_last_newline_in_the_window() -> None:
    chunks = split_message("aaa\nbbb ccc dddddd", max_len=8)
    assert chunks[0] == "aaa"
    assert "".join(c.replace(" ", "") for c in chunks) == "aaabbbcccdddddd"
    assert all(len(c) <= 8 for c in chunks)


def test_a_split_falls_back_to_a_space_when_there_is_no_newline() -> None:
    chunks = split_message("aaaa bbbb cccc", max_len=9)
    assert chunks[0] == "aaaa"
    assert all(len(c) <= 9 for c in chunks)


def test_a_window_with_no_separator_is_cut_hard() -> None:
    chunks = split_message("a" * 25, max_len=10)
    assert chunks == ["a" * 10, "a" * 10, "a" * 5]


def test_the_remainder_loses_the_whitespace_it_was_split_on() -> None:
    chunks = split_message("aaaa     bbbb", max_len=6)
    assert chunks[1].startswith("b")


def test_the_assistant_message_carries_only_the_fields_it_was_given() -> None:
    plain = build_assistant_message("hi")
    assert plain == {"role": "assistant", "content": "hi"}

    full = build_assistant_message(
        None,
        tool_calls=[{"id": "1"}],
        reasoning_content="because",
        thinking_blocks=[{"type": "thinking"}],
    )
    assert full["content"] is None
    assert full["tool_calls"] == [{"id": "1"}]
    assert full["reasoning_content"] == "because"
    assert full["thinking_blocks"] == [{"type": "thinking"}]

    assert "tool_calls" not in build_assistant_message("x", tool_calls=[])
    assert "thinking_blocks" not in build_assistant_message("x", thinking_blocks=[])
    assert build_assistant_message("x", reasoning_content="")["reasoning_content"] == ""
