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

from collections.abc import AsyncIterator, Callable
from typing import TYPE_CHECKING, Any

from raven.providers.base import ChatDelta, GenerationSettings, LLMProvider, LLMResponse
from raven.providers.lazy import LazyProvider

if TYPE_CHECKING:
    from raven.config.schema import Config


class ResolvingProvider(LLMProvider):
    """Route provider calls to a per-vendor adapter, keyed by the model's vendor."""

    def __init__(self, config: "Config", config_supplier: "Callable[[], Config] | None" = None):
        super().__init__()
        self._config = config
        # A supplier makes the credentials live, the way the pool's is: each
        # pick compares the providers fingerprint and drops the per-vendor
        # adapters when it moved, so a rotated apiKey serves the gateway's
        # default lane on the next call instead of the next restart. Without
        # one (tests, embedders) the adapters keep their construction-time
        # credentials, as before.
        self._config_supplier = config_supplier
        self._credentials_key: str | None = None
        self._by_vendor: dict[str, LLMProvider] = {}
        # The boot routing view, preserved across every credentials refresh.
        # ``_match_provider`` reads ``agents.defaults.provider`` (the forced
        # vendor) off the config, so importing a live ``agents`` section on a
        # credentials change would let a dormant routing edit take effect at an
        # unrelated key-rotation boundary. Routing is the default-binding lane's
        # concern (``set_default_binding`` / turn boundary); this provider only
        # makes credentials live.
        self._boot_agents = config.agents
        defaults = config.agents.defaults
        self._default_model = defaults.model
        self.generation = GenerationSettings(
            temperature=defaults.temperature,
            reasoning_effort=defaults.reasoning_effort,
            timeout=defaults.llm_call_timeout,
        )

    def _refresh_credentials(self) -> None:
        """Drop the adapters when the credentials they were built from changed.

        The same last-good posture as everything on the live lane: a config
        that cannot be loaded or fingerprinted right now (mid-rewrite) keeps
        the adapters we have. ``_default_model`` and ``generation`` stay the
        boot values on purpose -- they are turn/binding concerns with their own
        lanes; this refresh is only about whose key an adapter holds.

        The gate and the swap are kept to the same grain: the fingerprint moves
        only on a ``providers`` change, so the new config is adopted with its
        boot ``agents`` restored. Swapping the whole live object instead would
        import a dormant ``agents.defaults.provider`` edit on the next key
        rotation -- a routing change riding in on a credentials refresh, at a
        boundary neither lane owns.
        """
        if self._config_supplier is None:
            return
        from raven.providers.pool import credentials_fingerprint

        try:
            # The loader deliberately boots on DEFAULTS for a malformed file (a
            # mid-write race must not brick callers), which for this refresh
            # would read as "every credential vanished" and strip the adapters.
            # Gate on the raw file parsing first: unparseable means mid-write,
            # and mid-write means keep what we have. Assumes the supplier reads
            # the runtime config path, which the gateway's does.
            import json

            from raven.config.loader import get_config_path

            path = get_config_path()
            if path.exists():
                json.loads(path.read_text(encoding="utf-8"))
            current = self._config_supplier()
            key = credentials_fingerprint(current)
        except Exception:  # noqa: BLE001 - a torn config keeps the adapters we have
            return
        if key == self._credentials_key:
            return
        self._credentials_key = key
        self._config = current.model_copy(update={"agents": self._boot_agents})
        self._by_vendor.clear()

    def _pick(self, model: str | None) -> LLMProvider:
        self._refresh_credentials()
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
        from raven.providers.factory import make_provider

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
    ) -> AsyncIterator[ChatDelta]:
        async for delta in self._pick(model).chat_stream(messages, tools, model=model, **kwargs):
            yield delta

    def supports_prompt_caching(self, model: str) -> bool:
        """Forwarded for the same reason as ``wire_model_id``: only the adapter
        that sends the request knows whether its wire carries the field, and the
        base default (False) here would switch the cache optimizer off for every
        vendor behind this router."""
        return self._pick(model).supports_prompt_caching(model)

    def wire_model_id(self, model: str) -> str:
        """Forwarded: the inner adapter is the one that decides the wire id.

        Answering identity here would let a caller size a request against the
        stored name while the inner sends a gateway spelling, which the
        catalogue files with different numbers.
        """
        return self._pick(model).wire_model_id(model)
