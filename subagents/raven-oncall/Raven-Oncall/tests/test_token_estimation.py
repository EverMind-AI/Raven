from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from raven.memory_engine.consolidate.consolidator import MemoryConsolidator
from raven.session.manager import Session
from raven.utils import helpers


class _FakeEncoding:
    def encode(self, payload: str) -> list[str]:
        return list(payload)


def test_estimate_prompt_tokens_counts_assistant_tool_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        helpers.tiktoken,
        "get_encoding",
        lambda _name: _FakeEncoding(),
    )
    msg = {
        "role": "assistant",
        "content": "",
        "tool_calls": [
            {
                "id": "call_1",
                "type": "function",
                "function": {
                    "name": "write_file",
                    "arguments": "x" * 10_000,
                },
            },
        ],
    }

    prompt_tokens = helpers.estimate_prompt_tokens([msg])
    message_tokens = helpers.estimate_message_tokens(msg)

    assert prompt_tokens >= message_tokens
    assert prompt_tokens > 10_000


def test_estimate_counts_reasoning_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        helpers.tiktoken,
        "get_encoding",
        lambda _name: _FakeEncoding(),
    )
    base = {"role": "assistant", "content": "answer"}
    with_reasoning = {
        "role": "assistant",
        "content": "answer",
        "reasoning_content": "r" * 5_000,
        "thinking_blocks": [{"thinking": "t" * 5_000}],
    }

    assert helpers.estimate_message_tokens(with_reasoning) > helpers.estimate_message_tokens(base)
    assert helpers.estimate_prompt_tokens([with_reasoning]) > helpers.estimate_prompt_tokens([base])
    assert helpers.estimate_message_tokens(with_reasoning) > 10_000


def test_estimate_prompt_tokens_fallback_counts_tool_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _raise(_name: str) -> object:
        raise RuntimeError("encoding unavailable")

    monkeypatch.setattr(helpers.tiktoken, "get_encoding", _raise)
    msg = {
        "role": "assistant",
        "content": "",
        "tool_calls": [
            {
                "id": "call_1",
                "type": "function",
                "function": {
                    "name": "edit_file",
                    "arguments": "x" * 40_000,
                },
            },
        ],
    }

    assert helpers.estimate_prompt_tokens([msg]) > 10_000


def test_consolidator_prompt_estimate_counts_tool_calls_for_trigger(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    monkeypatch.setattr(
        helpers.tiktoken,
        "get_encoding",
        lambda _name: _FakeEncoding(),
    )
    session = Session(
        key="cli:default",
        messages=[
            {"role": "user", "content": "please write a large file"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {
                            "name": "write_file",
                            "arguments": "x" * 70_000,
                        },
                    },
                ],
            },
        ],
    )

    consolidator = MemoryConsolidator(
        workspace=tmp_path,
        provider=object(),
        model="test-model",
        sessions=object(),
        context_window_tokens=65_536,
        build_messages=lambda **kwargs: [
            {"role": "system", "content": "system"},
            *kwargs["history"],
            {"role": "user", "content": kwargs["current_message"]},
        ],
        get_tool_definitions=lambda: [],
    )

    estimated, source = consolidator.estimate_session_prompt_tokens(session)

    assert source == "tiktoken"
    assert estimated > consolidator.context_window_tokens


def test_token_consolidation_triggers_on_tool_call_payload(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    monkeypatch.setattr(
        helpers.tiktoken,
        "get_encoding",
        lambda _name: _FakeEncoding(),
    )
    session = Session(
        key="cli:default",
        messages=[
            {"role": "user", "content": "please write a large file"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {
                            "name": "write_file",
                            "arguments": "x" * 2_000,
                        },
                    },
                ],
            },
            {"role": "user", "content": "continue"},
        ],
    )
    sessions = MagicMock()
    consolidator = MemoryConsolidator(
        workspace=tmp_path,
        provider=object(),
        model="test-model",
        sessions=sessions,
        context_window_tokens=1_000,
        build_messages=lambda **kwargs: [
            {"role": "system", "content": "system"},
            *kwargs["history"],
            {"role": "user", "content": kwargs["current_message"]},
        ],
        get_tool_definitions=lambda: [],
    )
    consolidator.consolidate_messages = AsyncMock(return_value=True)
    consolidator.maybe_refresh_hot_tags = AsyncMock(return_value=0)

    asyncio.run(consolidator.maybe_consolidate_by_tokens(session))

    assert session.last_consolidated == 2
    consolidator.consolidate_messages.assert_awaited_once()
    sessions.save.assert_called_once_with(session)
