from __future__ import annotations

from raven.context_engine.history_trimmer import HistoryTrimmer


class _WeightedProvider:
    def estimate_prompt_tokens(self, messages, tools, model):
        del tools, model
        total = 0
        for message in messages:
            total += 100 if message.get("tool_calls") else 1
        return total, "test"


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


def test_budget_trim_drops_tool_call_and_result_as_one_group():
    messages = [
        {"role": "user", "content": "keep me"},
        {
            "role": "assistant",
            "content": "calling a tool",
            "tool_calls": [
                {
                    "id": "call_1|fc_1",
                    "type": "function",
                    "function": {"name": "message", "arguments": "{}"},
                }
            ],
        },
        {
            "role": "tool",
            "tool_call_id": "call_1|fc_1",
            "name": "message",
            "content": "sent",
        },
    ]
    trimmer = HistoryTrimmer(_WeightedProvider(), "test-model", lambda: [], 10)

    built, outcome = trimmer.trim(
        session_messages=messages,
        ids=[0, 1, 2],
        protected_ids={0},
        reserved_output=0,
        build_messages=lambda history: [
            {"role": "system", "content": "system"},
            *history,
            {"role": "user", "content": "current"},
        ],
    )

    assert outcome.included_ids == [0]
    assert HistoryTrimmer.structural_errors(built) == []
