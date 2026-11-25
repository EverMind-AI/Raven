"""A tool call whose arguments are not a JSON object reaches the loop as raw text, not as a crash.

MiroThinker-1.7-mini behind vLLM sent ``web_fetch`` with the arguments ``"bad"``:
valid JSON, but a string. It passed both parse exits as a string, and the first
reader that took the arguments as a mapping ended the turn with an AttributeError.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest

from raven.contracts.tool import RAW_ARGUMENTS_KEY
from raven.providers.litellm_provider import LiteLLMProvider
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


def _parsed(arguments: Any) -> dict[str, Any]:
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
    return provider._parse_response(response, sent_chars=12000).tool_calls[0].arguments


def _streamed(arguments: str) -> dict[str, Any]:
    slot = {"id": "call_1", "function": {"name": "web_fetch", "arguments_buf": [arguments]}}
    return _finalize_tool_calls([slot])[0].arguments


@pytest.mark.parametrize("arguments", ['"bad"', "[1, 2]", "7", "null"])
def test_json_that_is_not_an_object_is_parked_as_text(arguments):
    assert _parsed(arguments) == {RAW_ARGUMENTS_KEY: arguments}
    assert _streamed(arguments) == {RAW_ARGUMENTS_KEY: arguments}


def test_an_object_is_still_the_arguments():
    assert _parsed('{"url": "https://a.example"}') == {"url": "https://a.example"}
    assert _streamed('{"url": "https://a.example"}') == {"url": "https://a.example"}
    assert _streamed("") == {}


def test_arguments_an_upstream_already_decoded_into_a_non_object_are_parked_too():
    assert _parsed(["a"]) == {RAW_ARGUMENTS_KEY: '["a"]'}
