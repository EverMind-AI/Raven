"""A tool call whose arguments are not a JSON object reaches the loop as raw text, not as a crash.

MiroThinker-1.7-mini behind vLLM sent ``web_fetch`` with the arguments ``"bad"``:
valid JSON, but a string. It passed both parse exits as a string, and the first
reader that took the arguments as a mapping ended the turn with an AttributeError.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest

from raven.agent.tools.registry import ToolRegistry
from raven.contracts.llm_provider import ToolCallRequest
from raven.contracts.tool import RAW_ARGUMENTS_KEY, Tool
from raven.providers.azure_openai_provider import AzureOpenAIProvider
from raven.providers.litellm_provider import LiteLLMProvider
from raven.providers.openai_codex_provider import _consume_sse
from raven.providers.streaming import _finalize_tool_calls


@dataclass
class _Msg:
    content: str | None = None
    tool_calls: list[Any] | None = None
    reasoning_content: str | None = None


@dataclass
class _Choice:
    message: _Msg
    finish_reason: str = "tool_calls"


@dataclass
class _Usage:
    prompt_tokens: int = 100
    completion_tokens: int = 10
    total_tokens: int = 110


@dataclass
class _Response:
    choices: list[_Choice]
    usage: _Usage = field(default_factory=_Usage)


def _parsed_call(arguments: Any) -> ToolCallRequest:
    call = SimpleNamespace(
        function=SimpleNamespace(name="web_fetch", arguments=arguments, provider_specific_fields=None),
        provider_specific_fields=None,
    )
    with (
        patch("raven.providers.litellm_provider.litellm"),
        patch("raven.providers.litellm_provider.LiteLLMProvider._setup_env"),
    ):
        provider = LiteLLMProvider(api_key="sk-test", provider_name="openrouter")
    response = _Response(choices=[_Choice(message=_Msg(tool_calls=[call]))])
    return provider._parse_response(response, sent_chars=12000).tool_calls[0]


def _parsed(arguments: Any) -> dict[str, Any]:
    return _parsed_call(arguments).arguments


def _streamed_call(arguments: str) -> ToolCallRequest:
    slot = {"id": "call_1", "function": {"name": "web_fetch", "arguments_buf": [arguments]}}
    return _finalize_tool_calls([slot])[0]


def _streamed(arguments: str) -> dict[str, Any]:
    return _streamed_call(arguments).arguments


def _azure_call(arguments: Any) -> ToolCallRequest:
    provider = AzureOpenAIProvider(api_key="k", api_base="https://x.openai.azure.com", default_model="gpt-4o")
    message = {"tool_calls": [{"id": "c1", "function": {"name": "web_fetch", "arguments": arguments}}]}
    response = {"choices": [{"message": message, "finish_reason": "tool_calls"}]}
    return provider._parse_response(response).tool_calls[0]


def _azure(arguments: Any) -> dict[str, Any]:
    return _azure_call(arguments).arguments


class _Events:
    def __init__(self, events: list[dict[str, Any]]) -> None:
        self._lines = [line for event in events for line in (f"data: {json.dumps(event)}", "")]

    async def aiter_lines(self):
        for line in self._lines:
            yield line


def _codex_call(arguments: str) -> ToolCallRequest:
    item = {"type": "function_call", "call_id": "c1", "id": "fc_1", "name": "web_fetch", "arguments": ""}
    events = [
        {"type": "response.output_item.added", "item": item},
        {"type": "response.output_item.done", "item": {**item, "arguments": arguments}},
        {"type": "response.completed", "response": {"status": "completed"}},
    ]
    return asyncio.run(_consume_sse(_Events(events), 5))[1][0]


def _codex(arguments: str) -> dict[str, Any]:
    return _codex_call(arguments).arguments


@pytest.mark.parametrize("arguments", ['"bad"', "[1, 2]", "7", "null"])
def test_json_that_is_not_an_object_is_parked_as_text(arguments):
    assert _parsed(arguments) == {RAW_ARGUMENTS_KEY: arguments}
    assert _streamed(arguments) == {RAW_ARGUMENTS_KEY: arguments}
    assert _azure(arguments) == {RAW_ARGUMENTS_KEY: arguments}
    assert _codex(arguments) == {RAW_ARGUMENTS_KEY: arguments}


def test_an_object_is_still_the_arguments():
    assert _parsed('{"url": "https://a.example"}') == {"url": "https://a.example"}
    assert _azure('{"url": "https://a.example"}') == {"url": "https://a.example"}
    assert _codex('{"url": "https://a.example"}') == {"url": "https://a.example"}
    assert _streamed('{"url": "https://a.example"}') == {"url": "https://a.example"}
    assert _streamed("") == {}


def test_arguments_an_upstream_already_decoded_into_a_non_object_are_parked_too():
    assert _parsed(["a"]) == {RAW_ARGUMENTS_KEY: '["a"]'}


_EXITS = [_parsed_call, _streamed_call, _azure_call, _codex_call]


class _NoArgs(Tool):
    """A tool with no required parameters: schema validation alone would let a parked call through."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    @property
    def name(self) -> str:
        return "web_fetch"

    @property
    def description(self) -> str:
        return "records its calls"

    @property
    def parameters(self) -> dict:
        return {"type": "object", "properties": {"url": {"type": "string"}}, "required": []}

    async def execute(self, **kwargs) -> str:
        self.calls.append(kwargs)
        return "ran"


@pytest.mark.parametrize("exit_", _EXITS, ids=lambda f: f.__name__)
@pytest.mark.parametrize("arguments", ['"bad"', "[1, 2]", "7", "null"])
def test_a_parked_call_is_refused_by_the_registry_not_run(exit_, arguments):
    call = exit_(arguments)
    assert call.run_meta is not None and call.run_meta.arguments_repaired is True
    tool = _NoArgs()
    registry = ToolRegistry()
    registry.register(tool)
    result = asyncio.run(registry.execute(call.name, call.arguments, run_meta=call.run_meta))
    assert tool.calls == []
    assert "did not parse" in result or "not valid JSON" in result
    assert arguments in result


@pytest.mark.parametrize("exit_", _EXITS, ids=lambda f: f.__name__)
def test_an_object_call_carries_no_repair_flag(exit_):
    call = exit_('{"url": "https://a.example"}')
    assert call.run_meta is None or call.run_meta.arguments_repaired is False
