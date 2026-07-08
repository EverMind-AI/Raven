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
