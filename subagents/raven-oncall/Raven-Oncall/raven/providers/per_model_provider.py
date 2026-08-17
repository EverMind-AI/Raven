"""Provider that dispatches each call to a per-model endpoint by model name.

Used by the ``knn`` routing backend, where routable models live on different
OpenAI-compatible endpoints. Models listed in the routing config route to their
configured endpoints; any other model name (e.g. the agent default used by
background subsystems) is served by ``fallback``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from typing import TYPE_CHECKING, Any

from raven.providers.base import LLMProvider, LLMResponse, StreamDelta
from raven.providers.custom_provider import CustomProvider

if TYPE_CHECKING:
    from raven.config.schema import ModelEndpoint


class PerModelProvider(LLMProvider):
    """Route provider calls to a per-model :class:`CustomProvider` by model name."""

    def __init__(self, models: "Sequence[ModelEndpoint]", fallback: LLMProvider):
        super().__init__()
        self._fallback = fallback
        self._by_model: dict[str, CustomProvider] = {
            m.model: CustomProvider(api_key=m.api_key, api_base=m.api_base, default_model=m.model)
            for m in models
            if m.model
        }
        self._default = next(iter(self._by_model), None) or fallback.get_default_model()

    def _pick(self, model: str | None) -> LLMProvider:
        return self._by_model.get(model or "", self._fallback)

    def get_default_model(self) -> str:
        return self._default

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
        **kwargs: Any,
    ) -> LLMResponse:
        return await self._pick(model).chat(messages, tools, model=model, **kwargs)

    async def chat_with_retry(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
        **kwargs: Any,
    ) -> LLMResponse:
        return await self._pick(model).chat_with_retry(messages, tools, model=model, **kwargs)

    async def chat_stream(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
        **kwargs: Any,
    ) -> AsyncIterator[StreamDelta]:
        async for delta in self._pick(model).chat_stream(messages, tools, model=model, **kwargs):
            yield delta
