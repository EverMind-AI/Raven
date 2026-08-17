"""reasoning_content passback contract for DeepSeek's official thinking mode.

Live incident (2026-08-11, WorkBuddy code full run, deepseek-v4-flash on the
official endpoint): the model occasionally returns a tool_call response
WITHOUT reasoning_content on fast consecutive turns; once that assistant
message enters the history, the next request is rejected with 400:
    "The `reasoning_content` in the thinking mode must be passed back to the API."
218 of 240 attempts (91%) were cut short by it.

Contract probe against the official API (2026-08-11):
    assistant message with non-empty reasoning_content -> 200
    assistant message with reasoning_content=""        -> 200
    assistant message missing the reasoning_content key -> 400

Fix: one backfill action with two arming conditions. Behavioral: once any
assistant message in the list carries reasoning_content (the conversation is
visibly in thinking mode), assistant messages missing the key get "".
Pinned: coerce_reasoning=True (model-name match at the call site) backfills
unconditionally, covering histories where no reasoning survived pruning or
replay. Non-thinking conversations without the pin gain no new keys. This
also covers requests after prune_old_reasoning (emergency shrink) stripped
older reasoning.
"""

from __future__ import annotations

from raven.providers.litellm_provider import LiteLLMProvider


def _sanitize(messages):
    return LiteLLMProvider._sanitize_messages(messages)


def test_assistant_missing_reasoning_is_backfilled_with_empty_string() -> None:
    messages = [
        {"role": "user", "content": "fix the bug"},
        {
            "role": "assistant",
            "content": "",
            "reasoning_content": "let me look around",
            "tool_calls": [
                {"id": "call00001", "type": "function", "function": {"name": "list_dir", "arguments": "{}"}}
            ],
        },
        {"role": "tool", "tool_call_id": "call00001", "content": "src/"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {"id": "call00002", "type": "function", "function": {"name": "read_file", "arguments": "{}"}}
            ],
        },
        {"role": "tool", "tool_call_id": "call00002", "content": "..."},
    ]
    out = _sanitize(messages)
    assert out[1]["reasoning_content"] == "let me look around"
    assert out[3]["reasoning_content"] == "", (
        "keyless assistant msgs in thinking replays must get '' or the official endpoint 400s"
    )


def test_pruned_reasoning_history_is_backfilled() -> None:
    """Emergency shrink pops reasoning_content off old assistant messages; replay must re-add the key."""
    messages = [
        {"role": "user", "content": "task"},
        {"role": "assistant", "content": "old turn, reasoning pruned", "tool_calls": []},
        {"role": "assistant", "content": "", "reasoning_content": "fresh reasoning"},
    ]
    out = _sanitize(messages)
    assert out[1]["reasoning_content"] == ""
    assert out[2]["reasoning_content"] == "fresh reasoning"


def test_non_thinking_conversations_gain_no_new_keys() -> None:
    """A list with no reasoning_content anywhere stays untouched: no new fields for non-thinking endpoints."""
    messages = [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "hello"},
        {"role": "assistant", "content": "", "tool_calls": []},
    ]
    out = _sanitize(messages)
    for m in out:
        assert "reasoning_content" not in m


def test_reasoning_none_counts_as_missing_and_is_backfilled() -> None:
    """A present key holding None counts as missing on the server side; it gets the same empty-string backfill."""
    messages = [
        {"role": "assistant", "content": "", "reasoning_content": "thought"},
        {"role": "assistant", "content": "", "reasoning_content": None},
    ]
    out = _sanitize(messages)
    assert out[1]["reasoning_content"] == ""


def test_coerce_reasoning_backfills_even_without_prior_reasoning() -> None:
    """A pinned endpoint (coerce_reasoning=True) backfills even when no reasoning survives anywhere
    in the history - the pruned/replayed states that the behavioral arming condition cannot see."""
    messages = [
        {"role": "user", "content": "task"},
        {"role": "assistant", "content": "", "tool_calls": []},
    ]
    out = LiteLLMProvider._sanitize_messages(messages, coerce_reasoning=True)
    assert out[1]["reasoning_content"] == ""


def test_no_coercion_and_no_prior_reasoning_stays_untouched() -> None:
    """With neither arming condition met, not a single new key is added."""
    messages = [
        {"role": "user", "content": "task"},
        {"role": "assistant", "content": "", "tool_calls": []},
    ]
    out = LiteLLMProvider._sanitize_messages(messages, coerce_reasoning=False)
    assert "reasoning_content" not in out[1]
