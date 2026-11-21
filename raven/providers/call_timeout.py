"""Bound a streamed model request by one deadline, even while chunks keep arriving.

The deadline covers the model request itself, not preparation that runs before
the request is sent: MiniMax OAuth token acquisition or refresh, Codex
credential refresh, and the Codex client-version lookup are all outside it.
Those steps keep their own bounds (a 30 s HTTP client for a token refresh, a
3 s lookup backed by a cache), and the common path only reads a still-valid
credential from disk. Charging them to the call budget would report an
authentication delay as a model timeout.
"""

import asyncio
from collections.abc import AsyncIterator, Callable
from contextlib import aclosing, asynccontextmanager
from functools import wraps
from typing import Any

from raven.contracts.llm_provider import ChatDelta


class CallTimeoutError(TimeoutError):
    """The model request exhausted its whole-call budget."""

    def __init__(self, budget: float):
        super().__init__(f"the model request exceeded llmCallTimeout={budget:g}s")
        # The relay rebuilds this failure in another process, where the message
        # is the host's own sentence and the budget has to travel as a field.
        self.budget = float(budget)


@asynccontextmanager
async def call_timeout(budget: float, *, deadline: float | None = None) -> AsyncIterator[None]:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + budget if deadline is None else deadline
    if loop.time() >= deadline:
        raise CallTimeoutError(budget)
    timer = asyncio.timeout_at(deadline)
    try:
        async with timer:
            yield
    except TimeoutError as exc:
        if timer.expired():
            raise CallTimeoutError(budget) from exc
        raise


def _tail_budget(generation: Any, budget: float) -> float:
    """How long the pulls after a delivered ``finish_reason`` may still take.

    One idle window, capped at the whole-call budget: the number every adapter
    already uses to give up on a stream that went silent, and the one an
    operator tunes for how long a stream may dawdle.
    """
    idle = getattr(generation, "stream_idle_timeout", None)
    return min(float(idle), budget) if idle else budget


def bounded_stream(method: Callable[..., AsyncIterator[ChatDelta]]) -> Callable[..., AsyncIterator[ChatDelta]]:
    """Give every pull the remaining budget without arming cancellation across a yield."""

    @wraps(method)
    async def stream(self: Any, *args: Any, **kwargs: Any) -> AsyncIterator[ChatDelta]:
        budget = self.generation.timeout
        loop = asyncio.get_running_loop()
        deadline = loop.time() + budget
        # A delta that carries a finish_reason settles the reply. The pulls after
        # it only finish the iterator (a final usage snapshot, the stop event),
        # and the consumer may already have spent the rest of the budget
        # rendering the answer, so they are not judged against the call
        # deadline. They get a window of their own instead, opened at the first
        # pull *after* the settled delta: without one, an upstream that keeps
        # trickling past its terminal reason outlives the budget again, which is
        # the failure this decorator exists to prevent.
        settled = False
        tail_deadline: float | None = None
        async with aclosing(method(self, *args, **kwargs)) as deltas:
            while True:
                if settled and tail_deadline is None:
                    tail_deadline = loop.time() + _tail_budget(self.generation, budget)
                try:
                    if tail_deadline is None:
                        async with call_timeout(budget, deadline=deadline):
                            delta = await anext(deltas)
                    elif loop.time() >= tail_deadline:
                        # The reply is delivered in full; the tail is not worth
                        # a timeout the turn would report as a cut answer.
                        return
                    else:
                        try:
                            async with asyncio.timeout_at(tail_deadline) as tail:
                                delta = await anext(deltas)
                        except TimeoutError:
                            if tail.expired():
                                return
                            raise
                except StopAsyncIteration:
                    return
                if delta.finish_reason is not None:
                    settled = True
                # A timeout context across yield would cancel the consumer while
                # it renders a delta, rather than cancelling the model request.
                yield delta

    return stream
