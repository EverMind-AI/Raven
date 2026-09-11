"""The loop's own bound on getting as far as issuing the LLM request.

Every timeout a provider holds can only fire once the call has been entered.
The deck stall of 2026-09-10 never got that far: ``Iteration 1/600`` opened, the
pre-request awaits went quiet, and no request was ever issued -- no ``llm.call``
span, no ``llm.input`` artefact, no established outbound socket, and fifteen
minutes later a retry of the identical turn was healthy. Nothing held by the
provider could have caught that, because nothing of the provider had run.

So the loop bounds its own pre-request awaits, with the same first-byte budget
(``llmFirstByteTimeout``) on the reasoning that they are the other half of the
same question: has this turn got started? None of them is a place a healthy turn
spends minutes -- an episode-boundary emit is a queue put, the hook and strategy
chains are local work plus, at worst, their own already-bounded gates.

A stall here abandons the one stage and carries on to ask the model anyway,
rather than failing the turn: the stages are advisory (an outlet grouping hint,
a hook's tool edit, a cache-breakpoint placement) and an error that ends a run
is worse than the stall. Only a stall: an error the stage raised, a
``TimeoutError`` of its own included, is left to propagate as it did before this
guard existed. If the request that follows also cannot get started,
that is the provider's first-byte bound to catch, with a retryable verdict and
the loop's existing ladder behind it.

What this cannot interrupt: synchronous work. ``LazyProvider._built`` builds
litellm under a ``threading.Lock`` inside a coroutine, so a slow build blocks the
whole event loop and reaches no cancellation point -- a guard can bound an await,
never a frozen loop.
"""

from __future__ import annotations

import asyncio
from typing import Any, TypeVar

from loguru import logger

from raven.providers.first_byte import BOUND_NAME
from raven.tracing import trace

T = TypeVar("T")


class FirstCallGuard:
    """Bound the loop's pre-request awaits and report the one that went quiet."""

    def __init__(self, budget: float, *, model: str = "") -> None:
        self._budget = float(budget) if budget and budget > 0 else 0.0
        self._model = model

    @property
    def budget(self) -> float:
        """The per-stage budget, or 0.0 when the guard is switched off."""
        return self._budget

    async def stage(self, name: str, awaitable: Any, *, iteration: int, fallback: T) -> T:
        """Await ``awaitable``, or give up on it and answer ``fallback``.

        ``fallback`` is evaluated by the caller, so each site states in its own
        terms what "this stage did not happen" means there.

        Only *this* guard's deadline answers ``fallback``. A ``TimeoutError`` the
        awaited work raised itself is somebody else's bound expiring and
        propagates unchanged -- ``asyncio.timeout`` says which happened
        (``expired()``), where an ``except TimeoutError`` around ``wait_for``
        cannot: the two arrive as the same exception type. Read as this guard's,
        a strategy's own timeout was reported as a 0.0-second first-call stall
        and the un-preprocessed messages, tools and model were sent, which the
        TokenStrategy contract has ``before_llm_call`` errors propagate
        specifically to prevent.
        """
        if self._budget <= 0:
            return await awaitable
        loop = asyncio.get_running_loop()
        started = loop.time()
        try:
            async with asyncio.timeout(self._budget) as deadline:
                return await awaitable
        except TimeoutError:
            if not deadline.expired():
                raise
            waited = loop.time() - started
            self._record(name, iteration=iteration, waited=waited)
            return fallback

    def _record(self, name: str, *, iteration: int, waited: float) -> None:
        logger.error(
            "First call guard: iteration {} never got past {} -- quiet for {:.1f}s "
            "(bound {}={:g}s, model {}); abandoning that stage and asking the model anyway",
            iteration,
            name,
            waited,
            BOUND_NAME,
            self._budget,
            self._model or "unknown",
        )
        span = trace.current_span()
        if span is None:
            return
        # Stamped on the enclosing span rather than opened as one of its own:
        # the reader needs this beside the turn that stalled, and a span that
        # only ever exists on failure is a span nobody thinks to look for.
        span.set(
            {
                "first_call.stalled_stage": name,
                "first_call.stalled_iteration": iteration,
                "first_call.waited_s": round(waited, 1),
                "first_call.bound": BOUND_NAME,
                "first_call.budget_s": self._budget,
            }
        )
