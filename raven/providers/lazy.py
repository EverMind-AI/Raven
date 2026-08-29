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

from raven.providers.base import ChatDelta, GenerationSettings, LLMProvider, LLMResponse


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
        self._disable_auto_cache_control = False
        self._lock = threading.Lock()
        # Deliberately not ``_lock``: that one is held for the whole build, and
        # the build is the litellm import. Guarding the callback with it makes
        # wiring the callback wait out the very import prewarm exists to hide.
        self._callback_lock = threading.Lock()
        self._on_built: Callable[[], None] | None = None
        self._on_built_fired = False

    def _built(self) -> LLMProvider:
        if self._provider is None:
            with self._lock:
                if self._provider is None:
                    self._provider = self._factory()
                    self._provider.disable_auto_cache_control = self._disable_auto_cache_control
                    self._fire_on_built()
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
        rather than silently missing the one build event there is.

        Never waits on a build in flight: the callback is recorded, and
        whichever of the two finishes second is the one that fires it (see
        ``_fire_on_built``). This setter runs on ``AgentLoop``'s construction
        path, which is the startup path -- blocking here would hand back the
        seconds ``prewarm`` just moved off it.
        """
        with self._callback_lock:
            self._on_built = callback
        if self._provider is not None:
            self._fire_on_built()

    def _fire_on_built(self) -> None:
        """Invoke the callback at most once per instance.

        Called from both ends of the race the split lock opens: the build
        finishing and the callback being wired. Either can be second, and
        without the latch a setter landing between the build's assignment and
        its own call here would have both of them fire.
        """
        with self._callback_lock:
            callback = self._on_built
            if callback is None or self._on_built_fired:
                return
            self._on_built_fired = True
        self._invoke_on_built(callback)

    @staticmethod
    def _invoke_on_built(callback: Callable[[], None]) -> None:
        try:
            callback()
        except Exception:
            # `opt(exception=True)`, not `exc_info=True`: that is a stdlib kwarg
            # and loguru files unknown ones under `record["extra"]`, which the
            # sinks do not format -- so the traceback this line exists to keep
            # was never reaching the log.
            logger.opt(exception=True).debug("LazyProvider.on_built callback raised")

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
        request against the Model Ref while the inner sends the gateway
        spelling, and the two are separate catalogue rows.

        Post-materialization like ``emits_unparsed_reasoning``, and for the same
        reason: the truncation check that asks this runs after a call, and the
        call is what builds the inner. Before that, no request has been sized.
        """
        return model if self._provider is None else self._provider.wire_model_id(model)

    def supports_prompt_caching(self, model: str) -> bool:
        """Forwarded through a build, unlike the two probes above.

        Those are asked after a call and can answer cheaply until one happens.
        This one is asked by a token strategy in ``before_llm_call`` -- so
        "not built yet" is never a moment when the answer does not matter, it
        is the moment before every first call. Answering the base default there
        would switch caching off for the first turn of every session and turn it
        on afterwards, silently.

        The build it forces is the one the call on the next line forces anyway,
        so nothing is paid that laziness was protecting: what it protects is the
        gap between construction and the first turn, and no strategy runs there.
        """
        return self._built().supports_prompt_caching(model)

    @property
    def disable_auto_cache_control(self) -> bool:
        return self._disable_auto_cache_control

    @disable_auto_cache_control.setter
    def disable_auto_cache_control(self, value: bool) -> None:
        """Recorded now, applied to the inner whenever it is built.

        Unlike the rotor's push-down there is nothing to push to yet: this is
        assigned while the loop is being constructed and the inner is built on
        the first call. Storing it and replaying it in ``_built`` is what keeps
        the flag from being dropped on the one surface that defers its provider.
        """
        self._disable_auto_cache_control = value
        if self._provider is not None:
            self._provider.disable_auto_cache_control = value

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

    async def chat_stream(self, *args: Any, **kwargs: Any) -> AsyncIterator[ChatDelta]:
        async for delta in self._built().chat_stream(*args, **kwargs):
            yield delta

    async def chat_with_retry(self, *args: Any, **kwargs: Any) -> LLMResponse:
        return await self._built().chat_with_retry(*args, **kwargs)
