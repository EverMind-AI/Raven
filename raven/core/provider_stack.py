"""Provider assembly for the runtime: routing composition and the connectivity probe.

The providers shelf builds a provider (raven.providers.factory); this module
is the assembly root's use of it -- wrapping it in model routing when the
config asks for it, and exchanging one message to prove the configured
provider answers. No rendering here: the surfaces print.
"""

from __future__ import annotations

import asyncio
import time

from loguru import logger

DEFAULT_PROBE_MESSAGE = "Hi! Say hello in one sentence."


def build_model_routing(config, provider):
    """Return ``(router, provider)`` for the configured routing backend.

    - ``knn``: build a :class:`KNNModelRouter` and wrap ``provider`` in a
      :class:`PerModelProvider` so routed model names reach their endpoints
      (other models fall back to ``provider`` unchanged).
    - ``ecoclaw``: build the PinchBench :class:`ModelRouter`.
    - routing disabled, or ecoclaw with no API key: return ``(None, provider)``.
    """
    if not config.routing.enabled:
        return None, provider

    if config.routing.backend == "knn":
        from raven.providers.per_model_provider import PerModelProvider
        from raven.routing.knn_router import KNNModelRouter

        router = KNNModelRouter(config.routing, default_model=config.agents.defaults.model)
        return router, PerModelProvider(config.routing.models, fallback=provider)

    from raven.routing.router import ModelRouter

    openrouter = config.providers.get("openrouter")
    api_key = config.routing.api_key or getattr(openrouter, "api_key", "") or ""
    if not api_key:
        logger.warning("routing enabled but no OpenRouter API key found; routing disabled")
        return None, provider

    from raven.routing.types import RoutingProfileName

    profile: RoutingProfileName = config.routing.profile  # type: ignore[assignment]
    router = ModelRouter(api_key=api_key, profile=profile, fallback_model=config.agents.defaults.model)
    return router, provider


def send_probe(
    *,
    message: str = DEFAULT_PROBE_MESSAGE,
    timeout_s: int = 15,
    max_tokens: int = 200,
) -> tuple[str, int | None, float]:
    """Build a provider from the current config and exchange one chat message.

    Shared by ``onboard`` Step 3 and ``doctor --probe``. Bypasses the full
    ``AgentLoop`` so the probe only proves the provider answers, not that
    the agent runtime is healthy.

    Returns ``(response_text, tokens_used, elapsed_s)``. Raises ``RuntimeError``
    on provider error, ``asyncio.TimeoutError`` on timeout, or whatever
    ``load_config`` / ``make_provider`` raise on config failure.
    """
    from raven.config.loader import load_config
    from raven.providers.factory import make_provider

    config = load_config()
    provider = make_provider(config)

    start = time.monotonic()
    response = asyncio.run(
        asyncio.wait_for(
            provider.chat_with_retry(
                messages=[{"role": "user", "content": message}],
                max_tokens=max_tokens,
                temperature=0.3,
            ),
            timeout=timeout_s,
        )
    )
    elapsed = time.monotonic() - start

    if response.finish_reason == "error":
        raise RuntimeError(response.content or "provider returned an error")

    usage = response.usage or {}
    tokens = usage.get("total_tokens") or usage.get("completion_tokens")
    return (response.content or "").strip(), tokens, elapsed


__all__ = ["DEFAULT_PROBE_MESSAGE", "build_model_routing", "send_probe"]
