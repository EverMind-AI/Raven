"""Rotate and fail over across a provider section's several endpoints.

``provider_endpoints`` (see ``raven.providers.endpoints``) resolves a section
into one or more ``ResolvedEndpoint``s -- several accounts on the same
vendor, several regions, several keys. This module is the behavior asked
of that list: spread requests across them (round-robin) or stick
to one until it misbehaves (sticky), and route around an endpoint that just
failed instead of sending the next request into the same wall.

State (which endpoint is cooling, how many times it has failed, the rotation
cursor) lives in process memory only -- see ``RotorState`` for why, which is
the same reasoning as ``prompt_cache._SUPPRESSED``.

A stream in progress is never switched mid-flight. Rotation only ever
happens before the caller has seen a token: an exception or an error-shaped
terminal delta arriving before the first real delta means nothing has been
said yet, so trying the next endpoint costs nothing. Once a normal first
delta has been handed to the caller, part of an answer already exists;
resuming it from a different endpoint would either duplicate or contradict
what was already sent, so a failure past that point is raised as-is and the
stream ends there, same as any single-endpoint provider's stream would.
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator, Callable
from contextlib import aclosing
from dataclasses import dataclass, field
from typing import Any

from loguru import logger

from raven.providers.base import ErrorClassification, GenerationSettings, LLMProvider, LLMResponse, StreamDelta
from raven.providers.endpoints import ResolvedEndpoint

#: Seconds a failed endpoint sits out before it is tried again, doubling per
#: consecutive failure (30, 60, 120, 240, 300-capped). Doubling lets a
#: momentary blip cost one skipped rotation while an endpoint that keeps
#: failing backs off further each time, rather than being retried on every
#: single request forever.
_COOLDOWN_INITIAL_SECONDS = 30.0
_COOLDOWN_CAP_SECONDS = 300.0


def _rotates(classification: ErrorClassification) -> bool:
    """Whether this failure is worth trying the next endpoint for.

    Broader than ``should_fallback`` in exactly one place: an ``auth`` failure
    never falls back across *models* -- the same key fails for every model --
    but each endpoint here is its own account with its own key, and a revoked
    or exhausted key on one account says nothing about the next. Measured on
    a live wire: a dead OpenRouter key classifies ``auth``, and judging it by
    ``should_fallback`` alone left the rotor returning the 401 with a healthy
    endpoint sitting right behind it. Everything genuinely endpoint-agnostic
    (invalid_request, context overflow, unknown) still returns immediately.
    """
    return classification.should_fallback or classification.category == "auth"


@dataclass
class RotorState:
    """Per-instance rotation/failover bookkeeping -- process memory only.

    Not persisted, for the same reason as ``prompt_cache._SUPPRESSED``: which
    endpoint is down right now is a live fact about this process's recent
    calls, not a decision worth outliving it. The cost of forgetting on
    restart is at most one bad request per endpoint before the next failure
    re-learns it; the cost of a written file would be a healthy endpoint left
    cooling because of a fact that stopped being true after the process that
    wrote it exited.

    Concurrency: several ``chat``/``chat_stream`` calls can be in flight on
    one instance, all on the same event loop. Every read and write here
    happens between ``await`` points with no lock, which is safe under that
    single-loop assumption -- there is no point where two of these methods run
    interleaved. What *can* happen is two overlapping calls each hitting the
    same endpoint and each independently calling ``mark_failure`` for it after
    their own ``await`` returns; the second call just re-extends a cooldown
    that was already in effect. That is redundant, not incorrect -- it never
    leaves ``failure_count`` or ``cooldown_until`` in a state neither caller
    intended.
    """

    index: int = 0
    cooldown_until: dict[int, float] = field(default_factory=dict)
    failure_count: dict[int, int] = field(default_factory=dict)

    def is_cooling(self, i: int, now: float) -> bool:
        return self.cooldown_until.get(i, 0.0) > now

    def mark_failure(self, i: int, now: float) -> None:
        count = self.failure_count.get(i, 0) + 1
        self.failure_count[i] = count
        cooldown = min(_COOLDOWN_CAP_SECONDS, _COOLDOWN_INITIAL_SECONDS * (2 ** (count - 1)))
        self.cooldown_until[i] = now + cooldown

    def mark_success(self, i: int) -> None:
        self.failure_count[i] = 0
        self.cooldown_until[i] = 0.0


class EndpointRotorProvider(LLMProvider):
    """Fan one provider section's several endpoints out behind one instance.

    ``make_inner`` builds the real per-endpoint provider (a real
    ``LiteLLMProvider`` in production, a stub in tests) -- constructed eagerly
    here, once, for every endpoint. Endpoints are already-resolved static
    config (typically one to a handful), and building the inner provider does
    no network I/O, so there is nothing to gain from deferring it and it keeps
    ``_healthy_order()``'s index arithmetic pointed at a fixed, pre-built
    list rather than re-invoking a factory per lookup.
    """

    def __init__(
        self,
        endpoints: list[ResolvedEndpoint],
        make_inner: Callable[[ResolvedEndpoint], LLMProvider],
        default_model: str,
        strategy: str = "sticky",
    ) -> None:
        if not endpoints:
            raise ValueError("EndpointRotorProvider requires at least one endpoint")
        if strategy not in ("sticky", "round_robin"):
            raise ValueError(f"unknown rotation strategy: {strategy!r}")
        super().__init__()
        self._endpoints = endpoints
        self._inners = [make_inner(endpoint) for endpoint in endpoints]
        self._default_model = default_model
        self.strategy = strategy
        self._state = RotorState()
        self.generation = self.generation  # push the base class's default down now that inners exist

    @property
    def generation(self) -> GenerationSettings:
        return self._generation

    @generation.setter
    def generation(self, value: GenerationSettings) -> None:
        """Push generation settings down to every inner.

        ``make_provider`` builds this instance and only then assigns
        ``provider.generation = GenerationSettings(...)`` from config (see
        ``per_model_provider.py``'s ``PerModelProvider.__init__`` for the same
        push-down at construction time) -- without this setter that assignment
        would land on the rotor alone and every inner would keep answering
        temperature/max_tokens/timeout from its own untouched default.

        ``getattr(self, "_inners", [])`` covers the one call that happens
        before ``self._inners`` exists: the base class's own ``__init__``
        assigns a default ``self.generation`` before this subclass's
        constructor has built the endpoint list.
        """
        self._generation = value
        for inner in getattr(self, "_inners", []):
            inner.generation = value

    def _mark_failure(self, i: int) -> None:
        self._state.mark_failure(i, time.monotonic())

    def _mark_success(self, i: int) -> None:
        self._state.mark_success(i)

    def _healthy_order(self) -> list[int]:
        """Endpoint indices to try, in the order to try them.

        ``sticky`` always starts at index 0 and skips cooling entries -- one
        endpoint stays "the" endpoint until it fails, matching the common
        case of a single working account with spares for failover only.
        ``round_robin`` starts at the rotor's cursor and advances the cursor
        by one on every call, win or lose, so load spreads evenly across
        several accounts rather than favoring whichever is first.

        When every endpoint is cooling, cooldown is ignored and the full
        order is returned regardless of strategy -- a request has to go
        somewhere, and cooldown is a preference between healthy endpoints,
        not a breaker that can leave nothing to try.
        """
        if self.strategy == "round_robin":
            start = self._state.index
            self._state.index = (start + 1) % len(self._inners)
        else:
            start = 0
        return self._order_from(start)

    def _order_from(self, start: int) -> list[int]:
        """The order ``_healthy_order`` returns for a given starting index.

        Split out so the cursor advance stays in ``_healthy_order`` alone and
        ``active_endpoint_label`` can ask the same question without answering
        it differently or moving the rotation on.
        """
        n = len(self._inners)
        now = time.monotonic()
        order = [(start + i) % n for i in range(n)]
        healthy = [i for i in order if not self._state.is_cooling(i, now)]
        return healthy or order

    @property
    def active_endpoint_label(self) -> str:
        """Label of the endpoint the next request would go to.

        Read-only: unlike ``_healthy_order`` it never advances the round-robin
        cursor, so asking is not a rotation.
        """
        start = self._state.index if self.strategy == "round_robin" else 0
        return self._endpoints[self._order_from(start)[0]].label

    async def _chat_attempt_with_retry(
        self,
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        model: str | None,
        max_tokens: object,
        temperature: object,
        reasoning_effort: object,
        tool_choice: str | dict[str, Any] | None,
    ) -> LLMResponse:
        """Run the retry ladder against each healthy endpoint in turn.

        Delegates to each endpoint's own inner ``_chat_attempt_with_retry`` --
        the retry ladder itself (backoff, prompt-cache-refusal downgrade)
        stays that endpoint's job; this only decides which endpoint gets the
        next attempt. A fallback-worthy exhaustion moves to the next endpoint
        from ``_healthy_order()``; a non-fallback error (auth,
        invalid_request, context overflow, ...) returns immediately, since a
        different endpoint on the same vendor will not fix a malformed
        request. Exhausting every endpoint returns the last response, letting
        the caller's own model-chain fallback (``LLMProvider.chat_with_retry``)
        take over from there.
        """
        order = self._healthy_order()
        last_response: LLMResponse | None = None
        tried: list[tuple[str, str]] = []
        for i in order:
            response = await self._inners[i]._chat_attempt_with_retry(
                messages=messages,
                tools=tools,
                model=model,
                max_tokens=max_tokens,
                temperature=temperature,
                reasoning_effort=reasoning_effort,
                tool_choice=tool_choice,
            )
            if response.finish_reason != "error":
                self._mark_success(i)
                return response

            classification = response.error_classification or self.classify_error(content=response.content)
            response.error_classification = classification
            tried.append((self._endpoints[i].label, classification.category))
            last_response = response
            if not _rotates(classification):
                return response
            self._mark_failure(i)

        # Every endpoint was tried and every one failed -- the caller only sees
        # the last error otherwise, with no way to tell that the others were
        # tried too rather than skipped.
        logger.warning(
            "All endpoints exhausted, returning the last error. Tried: {}",
            ", ".join(f"{label} [{category}]" for label, category in tried),
        )
        return last_response  # type: ignore[return-value]  # order always non-empty

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
        **kwargs: Any,
    ) -> LLMResponse:
        """Single-shot call to the first healthy endpoint -- see ``_healthy_order``.

        No in-call rotation: a caller reaching ``chat`` directly (bypassing
        ``chat_with_retry``) gets exactly one endpoint's answer, same as any
        other provider's ``chat``. Rotating on failure is the retry layer's
        job, handled by ``_chat_attempt_with_retry``.
        """
        idx = self._healthy_order()[0]
        return await self._inners[idx].chat(messages, tools, model=model, **kwargs)

    async def chat_stream(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
        **kwargs: Any,
    ) -> AsyncIterator[StreamDelta]:
        """Open-stream endpoint rotation -- see the module docstring for why
        rotation stops the moment a real delta has been yielded.

        ``aclosing`` guarantees a stream abandoned mid-rotation (an endpoint
        that failed before its first delta) is closed before the next one is
        opened, mirroring ``LiteLLMProvider.chat_stream``'s own close-on-every-
        exit discipline for the underlying HTTP stream.
        """
        order = self._healthy_order()
        last_failure: Exception | StreamDelta | None = None
        for i in order:
            inner = self._inners[i]
            async with aclosing(inner.chat_stream(messages, tools, model=model, **kwargs)) as agen:
                try:
                    first = await agen.__anext__()
                except StopAsyncIteration:
                    self._mark_success(i)
                    return
                except Exception as exc:
                    classification = self.classify_error(exc)
                    if not _rotates(classification):
                        raise
                    self._mark_failure(i)
                    last_failure = exc
                    continue

                if first.finish_reason == "error":
                    # The fallback path 34099d8 added surfaces a failed open as
                    # a terminal error delta rather than an exception; judged
                    # the same way as one.
                    classification = first.error_classification or self.classify_error(content=first.content)
                    if _rotates(classification):
                        self._mark_failure(i)
                        last_failure = first
                        continue
                    yield first
                    return

                self._mark_success(i)
                yield first
                async for delta in agen:
                    yield delta
                return

        if isinstance(last_failure, Exception):
            raise last_failure
        if last_failure is not None:
            yield last_failure

    def can_serve(self, model: str) -> bool:
        """Delegates to the first endpoint's inner -- every endpoint under one
        rotor is the same vendor/section, so their identity for routing
        purposes is one answer, not one per endpoint."""
        return self._inners[0].can_serve(model)

    def emits_unparsed_reasoning(self) -> bool:
        """Delegates to the first endpoint's inner, same reasoning as ``can_serve``:
        every endpoint under one rotor is the same vendor/section, so the shape
        of its wire is one answer, not one per endpoint."""
        return self._inners[0].emits_unparsed_reasoning()

    def get_default_model(self) -> str:
        return self._default_model
