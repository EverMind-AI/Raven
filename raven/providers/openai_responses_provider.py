"""OpenAI Responses transport for direct and compatible API endpoints."""

from __future__ import annotations

import hashlib
import re
from typing import Any

import httpx

from raven.providers.base import (
    ErrorClassification,
    LLMProvider,
    LLMResponse,
    format_llm_error,
)
from raven.providers.openai_codex_provider import (
    _consume_sse,
    _convert_messages,
    _convert_tools,
)

_GPT_VERSION = re.compile(r"(?:^|/)gpt-(\d+)\.(\d+)(?:$|[-/])", re.IGNORECASE)
_DEFAULT_BASE = "https://api.openai.com/v1"


def _convert_tool_choice(
    tool_choice: str | dict[str, Any] | None,
) -> str | dict[str, Any] | None:
    if not isinstance(tool_choice, dict):
        return tool_choice
    function = tool_choice.get("function")
    if tool_choice.get("type") == "function" and isinstance(function, dict) and function.get("name"):
        return {"type": "function", "name": function["name"]}
    return tool_choice


class OpenAIResponsesProvider(LLMProvider):
    """Call an OpenAI-compatible Responses endpoint and consume its SSE body."""

    api_protocol = "responses"

    def __init__(
        self,
        api_key: str = "no-key",
        api_base: str | None = None,
        default_model: str = "default",
        extra_headers: dict[str, str] | None = None,
        provider_name: str | None = None,
        model_overrides: dict[str, dict[str, Any]] | None = None,
    ) -> None:
        super().__init__(api_key, api_base)
        self.default_model = default_model
        self.extra_headers = extra_headers or {}
        self.model_overrides = model_overrides or {}
        self._provider_name = provider_name or "openai"

    @property
    def provider_name(self) -> str:
        return self._provider_name

    def get_default_model(self) -> str:
        return self.default_model

    def wire_model_id(self, model: str) -> str:
        from raven.providers.wire import wire_model

        return wire_model(model, client_provider=self._provider_name)

    def supports_native_tool_result_images(self, model: str | None = None) -> bool:
        del model
        return True

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
        instructions, input_items = _convert_messages(messages)
        body: dict[str, Any] = {
            "model": self.wire_model_id(stored_model),
            "input": input_items,
            "stream": True,
        }
        if instructions:
            body["instructions"] = instructions
        if not _uses_gpt56_profile(stored_model):
            body["temperature"] = temperature
            if max_tokens is not None:
                body["max_output_tokens"] = max(1, max_tokens)
        if reasoning_effort:
            body["reasoning"] = {"effort": reasoning_effort}
        for pattern, overrides in self.model_overrides.items():
            if isinstance(overrides, dict) and pattern.lower() in stored_model.lower():
                body.update(overrides)
        if instructions:
            body["prompt_cache_key"] = _prompt_cache_key(instructions, stored_model)
        if tools:
            body["tools"] = _convert_tools(tools)
            body["tool_choice"] = _convert_tool_choice(tool_choice) or "auto"
            body["parallel_tool_calls"] = True

        url = f"{(self.api_base or _DEFAULT_BASE).rstrip('/')}/responses"
        headers = {"Accept": "text/event-stream", **self.extra_headers}
        try:
            async with httpx.AsyncClient(timeout=self.generation.timeout) as client:
                async with client.stream(
                    "POST",
                    url,
                    headers={"Authorization": f"Bearer {self.api_key}", **headers},
                    json=body,
                ) as response:
                    if response.status_code >= 400:
                        detail = (await response.aread()).decode("utf-8", "replace")
                        raise RuntimeError(f"HTTP {response.status_code}: {detail[:2000]}")
                    # The per-event watchdog runs on the stream-idle budget, not
                    # the call budget: with llmCallTimeout 600 and
                    # streamIdleTimeout 7 this adapter still waited 600 s on a
                    # silent stream and the setting did nothing here (reviewed
                    # 2026-09-07). getattr: a GenerationSettings from before the
                    # field falls back to the call budget, as it always did.
                    idle_timeout = getattr(self.generation, "stream_idle_timeout", None) or self.generation.timeout
                    content, tool_calls, finish_reason = await _consume_sse(response, idle_timeout)
            return LLMResponse(
                content=content or None,
                tool_calls=tool_calls,
                finish_reason=finish_reason,
            )
        except Exception as exc:
            classification = self.classify_error(exc)
            if any(
                marker in str(exc).lower()
                for marker in (
                    "upstream_error",
                    "responses response incomplete",
                    "responses body ended before completion",
                )
            ):
                classification = ErrorClassification(
                    "server",
                    retryable=True,
                    should_fallback=True,
                )
            if classification.category == "unknown" and not str(exc).strip():
                classification = ErrorClassification("network", retryable=True)
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
