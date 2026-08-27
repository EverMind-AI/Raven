"""API-key transport for OpenAI and OpenAI-compatible Responses endpoints."""

from __future__ import annotations

import asyncio
import hashlib
import re
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any

from openai import AsyncOpenAI

from raven.providers._responses import consume_response_stream, convert_tool_choice
from raven.providers.base import ErrorClassification, LLMProvider, LLMResponse, format_llm_error
from raven.providers.openai_codex_provider import (
    _convert_messages as convert_messages,
)
from raven.providers.openai_codex_provider import (
    _convert_tools as convert_tools,
)
from raven.providers.openai_codex_provider import (
    _split_tool_call_id as split_tool_call_id,
)

_GPT_VERSION = re.compile(r"(?:^|/)gpt-(\d+)\.(\d+)(?:$|[-/])", re.IGNORECASE)


@dataclass
class _ResponseContinuation:
    enabled: bool = True
    response_id: str | None = None
    logical_input: list[dict[str, Any]] = field(default_factory=list)
    tool_call_ids: tuple[str, ...] = ()
    model: str | None = None
    instructions: str = ""


class OpenAIResponsesProvider(LLMProvider):
    """Call a configured ``/responses`` endpoint with an API key."""

    def __init__(
        self,
        api_key: str = "no-key",
        api_base: str | None = None,
        default_model: str = "default",
        extra_headers: dict[str, str] | None = None,
        provider_name: str | None = None,
    ) -> None:
        super().__init__(api_key, api_base)
        self.default_model = default_model
        self.extra_headers = extra_headers or {}
        self._provider_name = provider_name or "openai"
        self._client = AsyncOpenAI(
            api_key=api_key or "no-key",
            base_url=api_base,
            default_headers=extra_headers or None,
        )

    @property
    def provider_name(self) -> str:
        return self._provider_name

    def get_default_model(self) -> str:
        return self.default_model

    def wire_model_id(self, model: str) -> str:
        from raven.providers.wire import wire_model

        return wire_model(model, client_provider=self._provider_name)

    def can_serve(self, model: str) -> bool:
        from raven.providers.registry import find_by_model, find_by_name

        mine = find_by_name(self._provider_name)
        if mine is None or mine.is_gateway:
            return True
        theirs = find_by_model(model)
        return theirs is None or theirs.name == mine.name

    def supports_native_tool_result_images(self, model: str | None = None) -> bool:
        del model
        return True

    def create_response_continuation(self, model: str | None = None) -> _ResponseContinuation:
        del model
        return _ResponseContinuation()

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
        max_tokens: int | None = None,
        temperature: float = 0.7,
        reasoning_effort: str | None = None,
        tool_choice: str | dict[str, Any] | None = None,
    ) -> LLMResponse:
        stored_model = model or self.default_model
        wire_model = self.wire_model_id(stored_model)
        instructions, input_items = convert_messages(messages)
        body: dict[str, Any] = {
            "model": wire_model,
            "input": input_items,
            "stream": True,
        }
        if not _uses_gpt56_profile(wire_model):
            body["temperature"] = temperature
            if max_tokens is not None:
                body["max_output_tokens"] = max(1, max_tokens)
        if instructions:
            body["instructions"] = instructions
            body["prompt_cache_key"] = _prompt_cache_key(instructions, wire_model)
        if reasoning_effort:
            body["reasoning"] = {"effort": reasoning_effort}
        if tools:
            body["tools"] = convert_tools(tools)
            body["tool_choice"] = convert_tool_choice(tool_choice) or "auto"
            body["parallel_tool_calls"] = True

        continuation = self.response_continuation_state
        continuation_input = _build_continuation_input(
            continuation,
            input_items,
            wire_model,
            instructions,
        )
        used_continuation = continuation_input is not None
        if used_continuation:
            body["input"] = continuation_input
            body["previous_response_id"] = continuation.response_id

        try:
            response = await self._request_response(body)
        except Exception as exc:
            if not used_continuation or not _continuation_rejected(exc):
                return self._error_response(exc)
            continuation.enabled = False
            body.pop("previous_response_id", None)
            body["input"] = input_items
            try:
                response = await self._request_response(body)
            except Exception as fallback_exc:
                return self._error_response(fallback_exc)

        _record_response_continuation(
            continuation,
            response,
            input_items,
            wire_model,
            instructions,
        )
        return response

    async def _request_response(self, body: dict[str, Any]) -> LLMResponse:
        stream = await asyncio.wait_for(
            self._client.responses.create(**body),
            timeout=self.generation.timeout,
        )
        return await consume_response_stream(stream)

    def _error_response(self, exc: Exception) -> LLMResponse:
        classification = self.classify_error(exc)
        if classification.category == "unknown" and _is_bare_stream_failure(exc):
            classification = ErrorClassification("network", retryable=True, should_fallback=True)
        return LLMResponse(
            content=format_llm_error(exc, classification, provider=self._provider_name),
            finish_reason="error",
            error_classification=classification,
        )


def _uses_gpt56_profile(model: str) -> bool:
    match = _GPT_VERSION.search(model)
    return bool(match and (int(match.group(1)), int(match.group(2))) >= (5, 6))


def _prompt_cache_key(instructions: str, model: str) -> str:
    return hashlib.sha256(f"openai-responses\0{model}\0{instructions}".encode()).hexdigest()


def _build_continuation_input(
    continuation: Any,
    input_items: list[dict[str, Any]],
    model: str,
    instructions: str,
) -> list[dict[str, Any]] | None:
    if (
        not isinstance(continuation, _ResponseContinuation)
        or not continuation.enabled
        or not continuation.response_id
        or not continuation.tool_call_ids
        or continuation.model != model
        or continuation.instructions != instructions
    ):
        return None
    prefix_length = len(continuation.logical_input)
    if input_items[:prefix_length] != continuation.logical_input:
        return None

    appended = input_items[prefix_length:]
    cursor = 0
    for call_id in continuation.tool_call_ids:
        match = next(
            (
                index
                for index in range(cursor, len(appended))
                if appended[index].get("type") == "function_call" and appended[index].get("call_id") == call_id
            ),
            None,
        )
        if match is None:
            return None
        cursor = match + 1

    delta = appended[cursor:]
    output_ids = {item.get("call_id") for item in delta if item.get("type") == "function_call_output"}
    if not set(continuation.tool_call_ids).issubset(output_ids):
        return None
    return delta


def _record_response_continuation(
    continuation: Any,
    response: LLMResponse,
    logical_input: list[dict[str, Any]],
    model: str,
    instructions: str,
) -> None:
    if not isinstance(continuation, _ResponseContinuation):
        return
    continuation.response_id = response.response_id
    continuation.logical_input = deepcopy(logical_input)
    continuation.tool_call_ids = tuple(split_tool_call_id(call.id)[0] for call in response.tool_calls)
    continuation.model = model
    continuation.instructions = instructions


def _continuation_rejected(exc: Exception) -> bool:
    message = str(exc).lower()
    return "previous_response_id" in message or "previous response" in message


def _is_bare_stream_failure(exc: BaseException) -> bool:
    return str(exc).strip().lower() in {
        "responses request failed",
        "responses body ended before completion",
    }
