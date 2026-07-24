"""LiteLLM-backed MiniMax Token Plan OAuth provider."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

from raven.providers.base import LLMResponse, StreamDelta
from raven.providers.litellm_provider import LiteLLMProvider
from raven.providers.minimax_oauth import get_token, oauth_config


class MiniMaxOAuthProvider(LiteLLMProvider):
    def __init__(self, region: str, default_model: str):
        self.region = region
        config = oauth_config(region)
        super().__init__(
            api_base=config.default_resource_url,
            default_model=default_model,
            provider_name=config.provider,
        )

    async def _prepare_token(self) -> None:
        token = await asyncio.to_thread(get_token, self.region)
        self.api_key = token.access
        self.api_base = token.resource_url
        self.extra_headers = {
            "x-api-key": token.access,
            "Authorization": f"Bearer {token.access}",
        }

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
        max_tokens: int = 4096,
        temperature: float = 0.7,
        reasoning_effort: str | None = None,
        tool_choice: str | dict[str, Any] | None = None,
    ) -> LLMResponse:
        await self._prepare_token()
        return await super().chat(messages, tools, model, max_tokens, temperature, reasoning_effort, tool_choice)

    async def chat_stream(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
        max_tokens: int = 4096,
        temperature: float = 0.7,
        reasoning_effort: str | None = None,
        tool_choice: str | dict[str, Any] | None = None,
    ) -> AsyncIterator[StreamDelta]:
        await self._prepare_token()
        async for delta in super().chat_stream(
            messages, tools, model, max_tokens, temperature, reasoning_effort, tool_choice
        ):
            yield delta
