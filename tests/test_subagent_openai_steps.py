"""How an OpenAI-compatible endpoint's reasoning_steps become turn events."""

from __future__ import annotations

import json
from pathlib import Path

from raven.agent.subagent.backends import turn_rows
from raven.agent.subagent.openai_steps import OpenAIStepReader

_FIXTURES = Path(__file__).parent / "fixtures" / "mirothinker"


def _buffered_steps() -> list[dict]:
    body = json.loads((_FIXTURES / "buffered_research.json").read_text(encoding="utf-8"))
    return body["choices"][0]["message"]["reasoning_steps"]


def test_a_thinking_step_becomes_a_thought() -> None:
    reader = OpenAIStepReader()
    reader.feed_steps([{"type": "thinking", "thought": "I should look this up"}])
    events = reader.events()
    assert [e["text"] for e in events] == ["I should look this up"]
    assert events[0]["at"], "the endpoint stamps nothing, so the reader's arrival time is the row's clock"


def test_consecutive_thinking_steps_join_into_one_thought() -> None:
    reader = OpenAIStepReader()
    reader.feed_steps([{"type": "thinking", "thought": "a"}, {"type": "thinking", "thought": "b"}])
    assert [e["text"] for e in reader.events()] == ["ab"]


def test_a_web_search_step_splits_into_a_call_and_its_result() -> None:
    """One step's payload holds both halves, so the halves get their own rows.

    The request key is the endpoint's own (`search_keywords`, not `query`): the
    record says what was sent.
    """
    reader = OpenAIStepReader()
    reader.feed_steps(
        [
            {
                "type": "web_search",
                "web_search": {
                    "search_keywords": ["aiohttp latest stable version"],
                    "search_results": [{"title": "t", "url": "u", "snippet": "s"}],
                },
            }
        ]
    )
    events = reader.events()
    assert [e["t"] for e in events] == ["call", "result"]
    assert events[0]["name"] == "web_search"
    assert json.loads(events[0]["arguments_json"]) == {"search_keywords": ["aiohttp latest stable version"]}
    assert json.loads(events[1]["text"]) == [{"title": "t", "url": "u", "snippet": "s"}]
    assert events[1]["ok"] is True
    assert events[0]["id"] == events[1]["id"], "the two rows pair through this id"


def test_a_fetch_step_is_unwrapped_to_its_extracted_info() -> None:
    """`snippet` is a JSON string inside the payload - the transport's wrapping,
    removed here rather than left for a reader to peel."""
    reader = OpenAIStepReader()
    reader.feed_steps(
        [
            {
                "type": "fetch_url_content",
                "fetch_url_content": {
                    "url": "https://pypi.org/project/aiohttp/",
                    "snippet": json.dumps(
                        {
                            "error": "",
                            "extracted_info": "latest is 3.14.3",
                            "success": True,
                            "tokens_used": 127696,
                        }
                    ),
                },
            }
        ]
    )
    events = reader.events()
    assert json.loads(events[0]["arguments_json"]) == {"url": "https://pypi.org/project/aiohttp/"}
    assert events[1]["text"] == "latest is 3.14.3"
    assert events[1]["ok"] is True


def test_a_failed_fetch_reports_not_ok() -> None:
    reader = OpenAIStepReader()
    reader.feed_steps(
        [
            {
                "type": "fetch_url_content",
                "fetch_url_content": {
                    "url": "https://example.com",
                    "snippet": json.dumps({"error": "403", "extracted_info": "", "success": False}),
                },
            }
        ]
    )
    assert reader.events()[1]["ok"] is False


def test_an_unmeasured_type_yields_a_call_and_no_result() -> None:
    """Which key holds the result is not knowable without seeing one, and a
    guessed split would put a guess in an audit record."""
    reader = OpenAIStepReader()
    reader.feed_steps([{"type": "execute_python", "execute_python": {"code": "print(1)"}}])
    events = reader.events()
    assert [e["t"] for e in events] == ["call"]
    assert events[0]["name"] == "execute_python"
    assert json.loads(events[0]["arguments_json"]) == {"code": "print(1)"}


def test_a_malformed_step_is_skipped_rather_than_raised_on() -> None:
    reader = OpenAIStepReader()
    reader.feed_steps([{"no_type": True}, "not a dict", None, {"type": ""}])
    assert reader.events() == []


def test_the_captured_response_produces_the_expected_row_run() -> None:
    """The real 9-step payload: 5 thoughts and 4 actions, each one answered."""
    reader = OpenAIStepReader()
    reader.feed_steps(_buffered_steps())
    events = reader.events()
    assert [e["t"] for e in events].count("call") == 4
    assert [e["t"] for e in events].count("result") == 4
    rows = turn_rows.rows(events)
    assert all(r["role"] in ("assistant", "tool") for r in rows)
    assert any(r.get("reasoning_content") for r in rows), "the thoughts ride on the calls"


def _stream_steps() -> list[dict]:
    """Every `reasoning_steps` entry from the captured SSE, in frame order."""
    steps: list[dict] = []
    for line in (_FIXTURES / "stream_research.sse").read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line.startswith("data:"):
            continue
        body = line[len("data:") :].strip()
        if not body or body == "[DONE]":
            continue
        try:
            frame = json.loads(body)
        except ValueError:
            continue
        delta = (frame.get("choices") or [{}])[0].get("delta") or {}
        steps.extend(s for s in (delta.get("reasoning_steps") or []) if isinstance(s, dict))
    return steps


def test_the_capture_really_is_fragmented() -> None:
    """Guards the fixture, not the code: if a future capture stops fragmenting
    `thinking`, the accumulator below stops being tested by it."""
    steps = _stream_steps()
    thinking = [s for s in steps if s.get("type") == "thinking"]
    assert len(thinking) > 50
    assert min(len(s.get("thought") or "") for s in thinking) < 20


def test_a_fragmented_thought_accumulates_into_one_row() -> None:
    reader = OpenAIStepReader()
    for step in _stream_steps():
        reader.feed_delta(step)
    events = reader.events()
    thoughts = [e for e in events if e["t"] == "thought"]
    assert thoughts, "the stream's thinking fragments produced no thought"
    assert len(thoughts) < 20, "fragments were not joined -- one row per frame"
    assert any(len(t["text"]) > 100 for t in thoughts)


def test_both_response_shapes_produce_the_same_event_kinds() -> None:
    """The core guarantee: a buffered call and a streamed call of the same
    endpoint leave the same *kind* of account, so a live view and the settled
    record cannot disagree in shape.

    Sequences, not counts: the two captures answered different prompts, so the
    number of searches differs. What must match is that each maps to the same
    grammar of thought / call / result.
    """
    buffered = OpenAIStepReader()
    buffered.feed_steps(_buffered_steps())
    streamed = OpenAIStepReader()
    for step in _stream_steps():
        streamed.feed_delta(step)

    for reader in (buffered, streamed):
        events = reader.events()
        kinds = [e["t"] for e in events]
        assert set(kinds) <= {"thought", "call", "result"}
        for index, kind in enumerate(kinds):
            if kind == "result":
                assert kinds[index - 1] == "call"
                assert events[index]["id"] == events[index - 1]["id"]
        rows = turn_rows.rows(events)
        assert rows and all(r["role"] in ("assistant", "tool") for r in rows)


def test_feeding_the_same_reader_twice_does_not_duplicate_an_open_thought() -> None:
    """A streamed run publishes on every frame, so `events()` is called many
    times mid-thought."""
    reader = OpenAIStepReader()
    reader.feed_delta({"type": "thinking", "thought": "ab"})
    first = reader.events()
    second = reader.events()
    assert first == second
    reader.feed_delta({"type": "thinking", "thought": "cd"})
    joined = reader.events()
    assert [e["text"] for e in joined] == ["abcd"]
    assert joined[0]["at"] == first[0]["at"], "a fragment extends the open thought, it does not restamp it"


def test_a_step_whose_result_key_never_arrived_gets_no_result_row() -> None:
    """The endpoint reported a call and no result for it. Emitting one anyway
    records the call as answered, which is the audit trail asserting something
    that did not happen. Both measured types, because both carry their result in
    a payload key that can simply be absent."""
    for step, name in (
        ({"type": "web_search", "web_search": {"search_keywords": ["x"]}}, "web_search"),
        ({"type": "fetch_url_content", "fetch_url_content": {"url": "https://x"}}, "fetch_url_content"),
    ):
        reader = OpenAIStepReader()
        reader.feed_delta(step)
        events = reader.events()
        assert [e["t"] for e in events] == ["call"], f"{name} invented a result row: {events}"
        assert events[0]["name"] == name


def test_a_null_result_leaves_the_call_unpaired_like_an_absent_one() -> None:
    """Null carries no status. Reading it as a failure would invent a provider
    failure exactly as recording it as success invented a provider answer -- a
    failed row needs a failure the endpoint reported. So null is unreadable, and
    an unreadable result is no result."""
    for kind, key in (("web_search", "search_results"), ("fetch_url_content", "snippet")):
        reader = OpenAIStepReader()
        reader.feed_delta({"type": kind, kind: {"url": "u", key: None}})
        assert [e["t"] for e in reader.events()] == ["call"], f"{kind} paired a null result"


def test_an_empty_result_list_is_a_successful_search_that_found_nothing() -> None:
    """The control that keeps the null rule from swallowing a real result: an
    empty list is what both measured types send for "no matches"."""
    reader = OpenAIStepReader()
    reader.feed_delta({"type": "web_search", "web_search": {"search_keywords": ["x"], "search_results": []}})
    result = next(e for e in reader.events() if e["t"] == "result")
    assert result["ok"] is True
    assert result["text"] == "[]"
