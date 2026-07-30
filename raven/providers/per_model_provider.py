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
from raven.providers.litellm_provider import LiteLLMProvider, session_affinity_headers

if TYPE_CHECKING:
    from raven.config.schema import ModelEndpoint


def _endpoint_provider(endpoint: "ModelEndpoint") -> LiteLLMProvider:
    """Build a LiteLLM provider pinned to one endpoint.

    ``provider_name="custom"`` selects the generic OpenAI-compatible gateway
    spec, so the endpoint's own ``api_base`` / ``api_key`` are carried per call
    and several endpoints coexist in one process.
    """
    if not endpoint.api_base:
        # Without an explicit base LiteLLM falls back to OPENAI_BASE_URL (or
        # api.openai.com), silently shipping the prompt and this endpoint's key
        # to a third party. Routing config must name the endpoint it routes to.
        raise ValueError(f"routing.models entry for {endpoint.model!r} has no api_base")
    return LiteLLMProvider(
        api_key=endpoint.api_key,
        api_base=endpoint.api_base,
        default_model=endpoint.model,
        provider_name="custom",
        extra_headers=session_affinity_headers(),
    )


class PerModelProvider(LLMProvider):
    """Route provider calls to a per-model :class:`LiteLLMProvider` by model name."""

    def __init__(self, models: "Sequence[ModelEndpoint]", fallback: LLMProvider):
        super().__init__()
        self._fallback = fallback
        self._by_model: dict[str, LiteLLMProvider] = {m.model: _endpoint_provider(m) for m in models if m.model}
        self._default = next(iter(self._by_model), None) or fallback.get_default_model()
        # Per-model sub-providers are built here without generation settings;
        # inherit the fallback's (already configured from AgentDefaults) and push
        # them down so routed calls honor temperature / max_tokens / timeout.
        self.generation = fallback.generation
        # Same for the user's per-model overrides: a routed model must honor
        # them exactly as the default provider does.
        overrides = getattr(fallback, "model_overrides", None) or {}
        for sub in self._by_model.values():
            sub.generation = self.generation
            sub.model_overrides = overrides

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
