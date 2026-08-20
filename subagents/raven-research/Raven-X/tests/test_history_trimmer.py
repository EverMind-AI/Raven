from __future__ import annotations

from raven.context_engine.history_trimmer import HistoryTrimmer


def test_history_from_ids_preserves_reasoning_fields():
    messages = [
        {"role": "user", "content": "hi"},
        {
            "role": "assistant",
            "content": "answer",
            "reasoning_content": "chain of thought",
            "thinking_blocks": [{"thinking": "block"}],
        },
    ]

    history = HistoryTrimmer.history_from_ids(messages, [0, 1])

    assert history[1]["reasoning_content"] == "chain of thought"
    assert history[1]["thinking_blocks"] == [{"thinking": "block"}]


def test_history_from_ids_drops_non_provider_keys():
    messages = [
        {"role": "user", "content": "hi", "timestamp": "2026-07-08T00:00:00"},
    ]

    history = HistoryTrimmer.history_from_ids(messages, [0])

    assert history == [{"role": "user", "content": "hi"}]


class _CountingProvider:
    """100 'tokens' per message, so the budget maps to a message count."""

    def estimate_prompt_tokens(self, messages, tools, model):
        return (sum(1 for m in messages if m.get("role")) * 100, "stub")


def _trimmer(window: int) -> HistoryTrimmer:
    return HistoryTrimmer(
        provider=_CountingProvider(),
        model="fake/model",
        get_tool_definitions=lambda: [],
        context_window_tokens=window,
    )


def _build(history):
    return [{"role": "system", "content": "s"}, *history, {"role": "user", "content": "q"}]


_EXCHANGE = [
    {"role": "user", "content": "q1"},
    {"role": "assistant", "content": None, "tool_calls": [
        {"id": "c1", "type": "function", "function": {"name": "web_search", "arguments": "{}"}}]},
    {"role": "tool", "tool_call_id": "c1", "name": "web_search", "content": "results"},
    {"role": "assistant", "content": "a1"},
    {"role": "user", "content": "q2"},
]


def test_trim_drops_tool_call_groups_whole():
    """dr@3.4. A parent assistant and its tool results drop as one unit.

    Dropping them singly left orphan tool results, and the provider rejects
    that request outright - the turn that most needed trimming died on a 400.
    """
    trimmer = _trimmer(600)
    messages, outcome = trimmer.trim(
        session_messages=_EXCHANGE,
        ids=[0, 1, 2, 3, 4],
        protected_ids={0, 4},
        reserved_output=0,
        build_messages=_build,
    )
    assert outcome.ok
    assert outcome.included_ids == [0, 3, 4]
    assert HistoryTrimmer.structural_errors(messages) == []


def test_trim_never_silently_drops_a_protected_message():
    trimmer = _trimmer(300)
    messages, outcome = trimmer.trim(
        session_messages=_EXCHANGE,
        ids=[0, 1, 2, 3, 4],
        protected_ids={0, 1, 2, 3, 4},
        reserved_output=0,
        build_messages=_build,
    )
    assert not outcome.ok
    assert outcome.included_ids == [0, 1, 2, 3, 4]
    assert any("protected" in w for w in outcome.warnings)
    assert HistoryTrimmer.structural_errors(messages) == []


def test_reanchor_keeps_a_protected_message_ahead_of_the_first_user():
    """dr@3.4. The start-at-user invariant yields to the protected contract.

    Dropping the leading user message re-anchors history to the next user
    message; a protected assistant message sitting between the two used to be
    cut by that slice with only a generic warning.
    """
    session = [
        {"role": "user", "content": "q1"},
        {"role": "assistant", "content": "pinned summary"},
        {"role": "user", "content": "q2"},
    ]
    trimmer = _trimmer(400)
    _, outcome = trimmer.trim(
        session_messages=session,
        ids=[0, 1, 2],
        protected_ids={1, 2},
        reserved_output=0,
        build_messages=_build,
    )
    assert 1 in outcome.included_ids, "protected message cut by the re-anchor slice"
    assert any("protected" in w for w in outcome.warnings)


def test_no_user_left_does_not_silently_clear_protected_messages():
    """The for-else path cleared everything, protected included, warning-free."""
    session = [
        {"role": "user", "content": "q1"},
        {"role": "assistant", "content": "pinned"},
    ]
    trimmer = _trimmer(200)
    _, outcome = trimmer.trim(
        session_messages=session,
        ids=[0, 1],
        protected_ids={1},
        reserved_output=0,
        build_messages=_build,
    )
    assert outcome.included_ids == [1]
    assert any("protected" in w for w in outcome.warnings)
