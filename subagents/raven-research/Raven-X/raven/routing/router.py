"""EcoClaw-style model router for raven.

Usage:
    router = ModelRouter(api_key="sk-or-...", profile="balanced")
    await router.initialize()
    model_id = await router.select_model(user_message)
"""

from __future__ import annotations

from loguru import logger

from raven.routing.cache import BenchmarkCache
from raven.routing.classifier import PromptClassifier
from raven.routing.fetcher import BenchmarkData
from raven.routing.selector import select_model
from raven.routing.types import RoutingProfileName, SelectionResult


class ModelRouter:
    """Routes user messages to the best-value model via PinchBench benchmark data.

    Flow (per message):
      1. classify(prompt) — embedding cosine similarity → TaskCategory
      2. select_model(data, category, profile) — composite score → ModelScore
      3. return primary model ID (OpenRouter format "provider/model")
    """

    def __init__(
        self,
        api_key: str,
        profile: RoutingProfileName = "balanced",
        fallback_model: str | None = None,
    ):
        self._api_key = api_key
        self._profile = profile
        self._fallback_model = fallback_model
        self._cache = BenchmarkCache()
        self._classifier = PromptClassifier(api_key=api_key)
        self._data: BenchmarkData | None = None

    async def initialize(self) -> None:
        """Pre-load benchmark data (call once at startup)."""
        self._data = await self._cache.load()
        logger.info(
            "ModelRouter initialized: {} models, profile={}",
            len(self._data),
            self._profile,
        )

    async def route(self, prompt: str) -> SelectionResult | None:
        """Classify prompt and select models. Returns None on failure."""
        if self._data is None:
            try:
                self._data = await self._cache.load()
            except Exception:
                return None

        try:
            classification = await self._classifier.classify(prompt)
        except Exception as e:
            logger.warning("Classification failed: {}", e)
            return None

        try:
            result = select_model(self._data, classification.category, self._profile)
            logger.info(
                "Routed to {} (category={}, score={:.3f})",
                result.primary.model,
                result.primary.task_score,
                result.primary.composite_score,
            )
            return result
        except Exception as e:
            logger.warning("Model selection failed: {}", e)
            return None

    async def select_model_id(self, prompt: str) -> str | None:
        """Return the best model ID for this prompt, or None to use default.

        None means: use the configured default model (no routing override).
        """
        result = await self.route(prompt)
        if result is None:
            return None
        return result.primary.model

    async def select_model_chain(self, prompt: str) -> tuple[str | None, list[str]]:
        """Return ``(primary_id, [fallback_ids])`` for this prompt.

        ``primary`` is None when routing yields nothing — use the configured
        default model and an empty chain. Otherwise the fallbacks are the
        selector's runner-up models, with the configured ``fallback_model``
        (typically the agent's default) appended as a last resort.
        """
        result = await self.route(prompt)
        if result is None:
            return None, []
        fallbacks = [f.model for f in result.fallbacks]
        if self._fallback_model and self._fallback_model not in fallbacks:
            if self._fallback_model != result.primary.model:
                fallbacks.append(self._fallback_model)
        return result.primary.model, fallbacks

    @property
    def profile(self) -> RoutingProfileName:
        return self._profile

    @profile.setter
    def profile(self, value: RoutingProfileName) -> None:
        self._profile = value
        logger.info("ModelRouter profile changed to '{}'", value)
