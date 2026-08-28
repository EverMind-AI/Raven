"""Provider that dispatches each call to the vendor adapter its model resolves to.

Per-session model selection means two concurrent turns can want two different
vendors, so a single baked-in adapter (whose api_key / api_base are fixed at
construction) is not enough. This resolves the vendor per call from the model
name via ``Config.get_provider_name``, so ``AgentLoop.provider`` never has to be
swapped -- which would tear an in-flight turn in another session.

Sub-providers are LazyProvider-wrapped: a vendor's litellm build is paid on its
first actual call, not up front for every configured vendor.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import TYPE_CHECKING, Any

from raven.providers.base import GenerationSettings, LLMProvider, LLMResponse, StreamDelta
from raven.providers.lazy import LazyProvider

if TYPE_CHECKING:
    from raven.config.schema import Config


class ResolvingProvider(LLMProvider):
    """Route provider calls to a per-vendor adapter, keyed by the model's vendor."""

    def __init__(self, config: "Config"):
        super().__init__()
        self._config = config
        self._by_vendor: dict[str, LLMProvider] = {}
        defaults = config.agents.defaults
        self._default_model = defaults.model
        self.generation = GenerationSettings(
            temperature=defaults.temperature,
            reasoning_effort=defaults.reasoning_effort,
            timeout=defaults.llm_call_timeout,
        )

    def _pick(self, model: str | None) -> LLMProvider:
        effective = model or self._default_model
        vendor = self._config.get_provider_name(effective)
        if vendor is None:
            effective = self._default_model
            vendor = self._config.get_provider_name(effective) or "_default"
        cached = self._by_vendor.get(vendor)
        if cached is not None:
            return cached
        built = self._build(effective)
        self._by_vendor[vendor] = built
        return built

    def _build(self, model: str) -> LLMProvider:
        from raven.core.helpers import make_provider

        sub = LazyProvider(
            factory=lambda: make_provider(self._config, model),
            default_model=model,
            generation=self.generation,
        )
        return sub

    def get_default_model(self) -> str:
        return self._default_model

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

    def wire_model_id(self, model: str) -> str:
        """Forwarded: the inner adapter is the one that decides the wire id.

        Answering identity here would let a caller size a request against the
        stored name while the inner sends a gateway spelling, which the
        catalogue files with different numbers.
        """
        return self._pick(model).wire_model_id(model)
