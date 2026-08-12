"""Provider that dispatches each call to a per-model endpoint by model name.

Used by the ``knn`` routing backend, where routable models live on different
OpenAI-compatible endpoints. Models listed in the routing config route to their
configured endpoints; any other model name (e.g. the agent default used by
background subsystems) is served by ``fallback``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from typing import TYPE_CHECKING, Any

from loguru import logger

from raven.providers.base import LLMProvider, LLMResponse, StreamDelta
from raven.providers.litellm_provider import LiteLLMProvider, session_affinity_headers

if TYPE_CHECKING:
    from raven.config.schema import ModelEndpoint


def _endpoint_provider(endpoint: "ModelEndpoint") -> LiteLLMProvider:
    """Build a LiteLLM provider pinned to one endpoint.

    ``provider_name="custom"`` selects the generic OpenAI-compatible gateway
    spec, so the endpoint's own ``api_base`` / ``api_key`` are carried per call
    and several endpoints coexist in one process. That name is borrowed for its
    api_base/api_key shape only, not as a claim about what is behind it -- a
    ``knn``-routed endpoint's backend is whatever the routing config points at,
    unknowable here, so ``unparsed_reasoning=False`` keeps this endpoint from
    being read as the self-hosted inference server ``custom`` also denotes: a
    front-loaded big vendor routed this way would otherwise have its ordinary
    content cut at a stray ``</think>``, and that cost is worse than the rare
    miss on a routing target that genuinely emits unparsed reasoning.
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
        unparsed_reasoning=False,
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
        fallback_models: list[str] | None = None,
        **kwargs: Any,
    ) -> LLMResponse:
        """Dispatch each hop of the fallback chain to its own routed endpoint.

        The base ``chat_with_retry`` (see ``LLMProvider``) runs the whole
        ``[model, *fallback_models]`` chain through a single provider
        instance, which is right when one instance can reach every hop --
        but here each hop may be a different ``knn``-routed endpoint (see
        ``_endpoint_provider``). Picking a sub-provider once, up front, and
        handing it the full chain would send every fallback model to the
        *primary* model's endpoint. Instead, ``_pick`` runs per hop, and each
        hop's own ``chat_with_retry`` is called with ``fallback_models=[]``
        so it keeps its own retry ladder without also retrying other hops'
        endpoints.

        Continuation between hops mirrors ``LLMProvider.chat_with_retry``:
        move to the next hop only on an error classified ``should_fallback``
        with a hop remaining; otherwise the response is returned as-is.

        The ``can_serve`` skip and the cache_control strip also mirror that
        loop (see ``LLMProvider.chat_with_retry``): a per-model sub-provider
        picked for a later hop can be just as unable to serve it, or just as
        unable to read a cache marker set for the primary model's vendor, as
        the single-instance case those guards were written for.
        """
        from raven.providers import prompt_cache

        model_chain = [model, *(fallback_models or [])]
        response: LLMResponse | None = None
        for idx, current_model in enumerate(model_chain):
            sub = self._pick(current_model)
            if idx and not sub.can_serve(current_model or ""):
                logger.warning(
                    "Skipping fallback model={} - this provider instance cannot serve it (wrong vendor)",
                    current_model,
                )
                continue
            if idx and not prompt_cache.accepts_cache_control(current_model or ""):
                messages, tools = prompt_cache.strip(messages, tools)
            response = await sub.chat_with_retry(messages, tools, model=current_model, fallback_models=[], **kwargs)
            if response.finish_reason != "error":
                return response

            classification = response.error_classification or self.classify_error(content=response.content)
            has_next = idx + 1 < len(model_chain)
            if has_next and classification.should_fallback:
                continue
            return response

        return response  # type: ignore[return-value]  # chain always non-empty

    async def chat_stream(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
        **kwargs: Any,
    ) -> AsyncIterator[StreamDelta]:
        async for delta in self._pick(model).chat_stream(messages, tools, model=model, **kwargs):
            yield delta
