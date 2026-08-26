"""The rows one delegated turn contributes, independent of transport."""

from __future__ import annotations

from raven.agent.subagent.backends import turn_rows as tr


def test_a_call_wears_the_thought_that_preceded_it() -> None:
    rows = tr.rows(
        [
            tr.thought("first I look", at="2026-08-21T10:00:00"),
            tr.call(id="c1", name="Bash", arguments_json='{"command": "ls"}', at="2026-08-21T10:00:01"),
        ]
    )
    assert len(rows) == 1
    assert rows[0]["role"] == "assistant"
    assert rows[0]["reasoning_content"] == "first I look"
    assert rows[0]["tool_calls"][0]["function"] == {"name": "Bash", "arguments": '{"command": "ls"}'}
    assert rows[0]["timestamp"] == "2026-08-21T10:00:00", "the thought's clock opens the row"


def test_narration_lands_on_the_call_it_preceded() -> None:
    rows = tr.rows([tr.say("checking the tree"), tr.call(id="c1", name="Bash", arguments_json="{}")])
    assert rows[0]["content"] == "checking the tree"


def test_a_failed_result_is_prefixed() -> None:
    rows = tr.rows([tr.result(id="c1", text="no such file", ok=False)])
    assert rows[0] == {"role": "tool", "tool_call_id": "c1", "content": "[failed] no such file"}


def test_a_trailing_thought_gets_its_own_row() -> None:
    """A turn that thought after its last call would otherwise lose it."""
    rows = tr.rows([tr.call(id="c1", name="Bash", arguments_json="{}"), tr.thought("now I answer")])
    assert rows[-1] == {"role": "assistant", "content": "", "reasoning_content": "now I answer"}


def test_the_answer_is_not_a_row() -> None:
    """The record keeps the answer and the reader appends it as the closing
    message; a transcript that also carried it would say it twice."""
    rows = tr.rows([tr.call(id="c1", name="Bash", arguments_json="{}")])
    assert all(r.get("content") != "the answer" for r in rows)


def test_an_in_flight_read_appends_the_partial_answer() -> None:
    """A live view has no record to append the answer from."""
    rows = tr.rows(
        [tr.call(id="c1", name="Bash", arguments_json="{}")],
        in_flight_answer="half an ans",
        in_flight_answer_at="2026-08-21T10:00:09",
    )
    assert rows[-1] == {
        "role": "assistant",
        "content": "half an ans",
        "timestamp": "2026-08-21T10:00:09",
    }


def test_a_steer_flushes_what_was_said_before_it_and_lands_as_a_user_row() -> None:
    """The person's words go where they were said. What the agent had said and
    thought up to then becomes a row of its own first, or it would ride the next
    call and be drawn after the words it was answering."""
    rows = tr.rows(
        [
            tr.thought("first plan", at="2026-08-26T10:00:00"),
            tr.say("starting with the tests"),
            tr.user("no, the docs first", at="2026-08-26T10:00:05"),
            tr.call(id="c1", name="Read", arguments_json="{}", at="2026-08-26T10:00:06"),
        ]
    )
    assert [r["role"] for r in rows] == ["assistant", "user", "assistant"]
    assert rows[0] == {
        "role": "assistant",
        "content": "starting with the tests",
        "reasoning_content": "first plan",
        "timestamp": "2026-08-26T10:00:00",
    }
    assert rows[1] == {
        "role": "user",
        "content": "no, the docs first",
        "steer": True,
        "timestamp": "2026-08-26T10:00:05",
    }
    assert "reasoning_content" not in rows[2], "the thought was flushed, not carried onto the call"


def test_a_steer_with_nothing_said_before_it_adds_no_empty_row() -> None:
    rows = tr.rows([tr.user("go faster")])
    assert rows == [{"role": "user", "content": "go faster", "steer": True}]


def test_a_steer_row_is_marked_so_a_reader_can_tell_it_from_a_prompt() -> None:
    msgs = tr.rows([tr.say("on it"), tr.user("the docs first", at="t1")])
    steer = next(m for m in msgs if m["role"] == "user")
    assert steer == {"role": "user", "content": "the docs first", "steer": True, "timestamp": "t1"}
