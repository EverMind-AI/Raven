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

from loguru import logger

from raven.providers.base import GenerationSettings, LLMProvider, LLMResponse, StreamDelta


class LazyProvider(LLMProvider):
    """Proxy that builds the real provider on first chat call (memoized)."""

    def __init__(
        self,
        factory: Callable[[], LLMProvider],
        default_model: str,
        generation: GenerationSettings,
        *,
        initial_endpoint_label: str | None = None,
    ):
        super().__init__()
        self._factory = factory
        self._default_model = default_model
        self.generation = generation
        self._initial_endpoint_label = initial_endpoint_label
        self._provider: LLMProvider | None = None
        self._lock = threading.Lock()
        self._on_built: Callable[[], None] | None = None

    def _built(self) -> LLMProvider:
        if self._provider is None:
            with self._lock:
                if self._provider is None:
                    self._provider = self._factory()
                    callback = self._on_built
                    if callback is not None:
                        self._invoke_on_built(callback)
        return self._provider

    @property
    def on_built(self) -> "Callable[[], None] | None":
        """Fired once, right after the real provider finishes building.

        Lets a caller that skipped the real provider's import at construction
        (see ``rates._try_litellm_context_window``'s ``allow_import``) correct
        a value it answered cheaply once the real thing is on hand.

        No production setter today. ``AgentLoop`` used this to fix up a window
        it had resolved eagerly; the window now belongs to the binding and is
        resolved on first read, by which time a turn is in flight and the
        import has happened -- so there is nothing left to correct after the
        fact. Kept for a caller with the same shape of problem.
        """
        return self._on_built

    @on_built.setter
    def on_built(self, callback: "Callable[[], None] | None") -> None:
        """Setting this after the build already happened (prewarm can finish
        before the constructor gets here) still fires the callback once,
        rather than silently missing the one build event there is."""
        with self._lock:
            self._on_built = callback
            already_built = self._provider is not None
        if already_built and callback is not None:
            self._invoke_on_built(callback)

    @staticmethod
    def _invoke_on_built(callback: Callable[[], None]) -> None:
        try:
            callback()
        except Exception:
            logger.debug("LazyProvider.on_built callback raised", exc_info=True)

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

    def emits_unparsed_reasoning(self) -> bool:
        """Forwarded post-materialization: the stream collation that asks this
        only runs after a call, and the first call is what builds the inner
        provider -- before that there is nothing to normalize anyway."""
        return False if self._provider is None else self._provider.emits_unparsed_reasoning()

    def wire_model_id(self, model: str) -> str:
        """Forwarded, because the inner provider is the one with a wire.

        Answering identity here rather than forwarding is not a missing method
        but a wrong answer: the base class supplies one, so the caller sizes a
        request against the stored id while the inner sends the gateway
        spelling, and the two are separate catalogue rows.

        Post-materialization like ``emits_unparsed_reasoning``, and for the same
        reason: the truncation check that asks this runs after a call, and the
        call is what builds the inner. Before that, no request has been sized.
        """
        return model if self._provider is None else self._provider.wire_model_id(model)

    @property
    def active_endpoint_label(self) -> str | None:
        """Which endpoint is answering, for the session footer.

        Before materialization, the rotor behind the real provider has not
        rotated yet, so the first endpoint it would pick (``initial_endpoint_label``)
        is exactly what a sticky rotor would answer -- no build needed just to
        display a label. Once built, defer to the inner provider so the footer
        reflects any rotation that happened since.
        """
        if self._provider is None:
            return self._initial_endpoint_label
        return getattr(self._provider, "active_endpoint_label", None)

    @property
    def unwrapped(self) -> LLMProvider | None:
        """The materialized inner provider, or None before the first build.

        For callers that need the real class rather than this proxy -- an
        ``isinstance`` probe against the proxy answers about the proxy
        (``capabilities`` type-tests for the Azure transport this way).
        Deliberately not a building accessor: a capability question must not
        pay the multi-second import that materialization costs.
        """
        return self._provider

    async def chat(self, *args: Any, **kwargs: Any) -> LLMResponse:
        return await self._built().chat(*args, **kwargs)

    async def chat_stream(self, *args: Any, **kwargs: Any) -> AsyncIterator[StreamDelta]:
        async for delta in self._built().chat_stream(*args, **kwargs):
            yield delta

    async def chat_with_retry(self, *args: Any, **kwargs: Any) -> LLMResponse:
        return await self._built().chat_with_retry(*args, **kwargs)
