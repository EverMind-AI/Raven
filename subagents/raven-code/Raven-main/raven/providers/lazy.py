"""Lazy LLM provider: defer building the real provider until the first model call.

Building the real provider imports litellm (~2-7s), which — when done eagerly at
``AgentLoop`` construction — stalls startup even though tools/skills/memory do not
need it. ``LazyProvider`` answers the two things read before the first call
(``get_default_model`` and ``generation``) from config, and builds the real
provider (memoized, thread-safe) only when a chat method is actually invoked.
"""

from __future__ import annotations

import threading
from collections.abc import AsyncIterator, Callable
from typing import Any

from raven.providers.base import GenerationSettings, LLMProvider, LLMResponse, StreamDelta


class LazyProvider(LLMProvider):
    """Proxy that builds the real provider on first chat call (memoized)."""

    def __init__(
        self,
        factory: Callable[[], LLMProvider],
        default_model: str,
        generation: GenerationSettings,
    ):
        super().__init__()
        self._factory = factory
        self._default_model = default_model
        self.generation = generation
        self._provider: LLMProvider | None = None
        self._lock = threading.Lock()

    def _built(self) -> LLMProvider:
        if self._provider is None:
            with self._lock:
                if self._provider is None:
                    self._provider = self._factory()
        return self._provider

    def prewarm(self) -> None:
        """Build the real provider in a daemon thread so the ~2-7s litellm import
        is hidden behind render + user think-time. Safe to race with the first
        real call (``_built`` is lock-guarded); build errors are left for the
        first call to surface."""

        def _run() -> None:
            try:
                self._built()
            except Exception:
                pass

        threading.Thread(target=_run, name="litellm-prewarm", daemon=True).start()

    def get_default_model(self) -> str:
        return self._default_model

    async def chat(self, *args: Any, **kwargs: Any) -> LLMResponse:
        return await self._built().chat(*args, **kwargs)

    async def chat_stream(self, *args: Any, **kwargs: Any) -> AsyncIterator[StreamDelta]:
        async for delta in self._built().chat_stream(*args, **kwargs):
            yield delta

    async def chat_with_retry(self, *args: Any, **kwargs: Any) -> LLMResponse:
        return await self._built().chat_with_retry(*args, **kwargs)
