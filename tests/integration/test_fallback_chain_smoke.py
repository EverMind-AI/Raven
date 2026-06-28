"""Cross-module smoke for the routed fallback model chain.

Exercises the real code paths wired by the fallback-chain change, end to end
across two subsystems:

- ``routing.selector.select_model`` — composite quality/cost scoring
- ``ModelRouter.select_model_chain`` — chain assembly + last-resort tail
- ``LLMProvider.chat_with_retry`` — transient-retry ladder, ``_should_fallback``
  classification, and the model switch

Only the two genuine externals are stubbed: the embedding classifier (would
need an API call) and the network ``chat()`` (raised as a real
``ConnectionError`` so the transient path is driven by a real exception, not
a pre-baked error response). Everything between is the real implementation.
"""

from __future__ import annotations

import pytest

from raven.providers.base import LLMProvider, LLMResponse
from raven.routing.router import ModelRouter
from raven.routing.types import ClassificationResult, ModelBenchmark, ModelTaskScore


class _FlakyProvider(LLMProvider):
    """Real provider whose ``chat()`` raises a connection error for chosen models.

    A raised ``ConnectionError`` flows through ``chat_with_retry``'s real
    exception handling -> transient classification ("connection") -> retry ->
    fallback, so the integration covers the live exception path.
    """

    def __init__(self, failing_models: set[str]):
        super().__init__(api_key="test")
        self._failing = failing_models
        self.calls: list[str | None] = []

    async def chat(self, messages, tools=None, model=None, max_tokens=4096,
                   temperature=0.7, reasoning_effort=None, tool_choice=None):
        self.calls.append(model)
        if model in self._failing:
            raise ConnectionError("Connection refused by upstream")
        return LLMResponse(content=f"answer from {model}", finish_reason="stop")

    def get_default_model(self) -> str:
        return "anthropic/model-a"


def _router_with_models(fallback_model: str | None = None) -> ModelRouter:
    """Router pre-loaded with 3 ranked models; classifier stubbed to tool_use.

    Scores are monotone on both axes (A best quality + cheapest, C worst +
    priciest), so the real selector ranks A > B > C under any non-negative
    profile weights — deterministic primary/fallback order.
    """
    def _bench(model: str, provider: str, score: float, cost: float) -> ModelBenchmark:
        return ModelBenchmark(
            model=model,
            provider=provider,
            overall_score=score,
            speed=1.0,
            cost=cost,
            task_scores=[ModelTaskScore(task_id="task_04_weather", score=score, max_score=100.0)],
            submission_id=f"sub-{model}",
        )

    data = {
        "anthropic/model-a": _bench("anthropic/model-a", "anthropic", 90.0, 1.0),
        "openai/model-b": _bench("openai/model-b", "openai", 80.0, 2.0),
        "google/model-c": _bench("google/model-c", "google", 70.0, 3.0),
    }

    router = ModelRouter(api_key="test", profile="balanced", fallback_model=fallback_model)
    router._data = data  # skip cache.load (network)

    async def _fake_classify(_prompt: str) -> ClassificationResult:
        return ClassificationResult(category="tool_use", similarity=1.0)

    router._classifier.classify = _fake_classify  # skip embedding API
    return router


@pytest.mark.asyncio
async def test_routed_chain_recovers_on_first_fallback():
    router = _router_with_models()
    primary, fallbacks = await router.select_model_chain("what's the weather?")
    # Real selector ranking, not hand-fed.
    assert primary == "anthropic/model-a"
    assert fallbacks == ["openai/model-b", "google/model-c"]

    provider = _FlakyProvider(failing_models={"anthropic/model-a"})
    provider._CHAT_RETRY_DELAYS = (0, 0, 0)
    resp = await provider.chat_with_retry(
        messages=[{"role": "user", "content": "hi"}],
        model=primary,
        fallback_models=fallbacks,
    )

    assert resp.finish_reason == "stop"
    assert resp.content == "answer from openai/model-b"
    # primary exhausts its ladder (3 + 1), then the first fallback succeeds.
    assert provider.calls == ["anthropic/model-a"] * 4 + ["openai/model-b"]


@pytest.mark.asyncio
async def test_routed_chain_walks_to_second_fallback_with_configured_tail():
    router = _router_with_models(fallback_model="meta/default-fallback")
    primary, fallbacks = await router.select_model_chain("weather please")
    # Configured fallback_model appended as the last-resort tail.
    assert primary == "anthropic/model-a"
    assert fallbacks == ["openai/model-b", "google/model-c", "meta/default-fallback"]

    provider = _FlakyProvider(failing_models={"anthropic/model-a", "openai/model-b"})
    provider._CHAT_RETRY_DELAYS = (0, 0, 0)
    resp = await provider.chat_with_retry(
        messages=[{"role": "user", "content": "hi"}],
        model=primary,
        fallback_models=fallbacks,
    )

    assert resp.finish_reason == "stop"
    assert resp.content == "answer from google/model-c"
    assert provider.calls == (
        ["anthropic/model-a"] * 4 + ["openai/model-b"] * 4 + ["google/model-c"]
    )
