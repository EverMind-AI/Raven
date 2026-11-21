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


def bounded_stream(method: Callable[..., AsyncIterator[ChatDelta]]) -> Callable[..., AsyncIterator[ChatDelta]]:
    """Give every pull the remaining budget without arming cancellation across a yield."""

    @wraps(method)
    async def stream(self: Any, *args: Any, **kwargs: Any) -> AsyncIterator[ChatDelta]:
        budget = self.generation.timeout
        deadline = asyncio.get_running_loop().time() + budget
        # A delta that carries a finish_reason settles the reply. Pulls after it
        # only finish the iterator (a final usage snapshot, the stop event), and
        # the consumer may already have spent the rest of the budget rendering
        # the answer. Keep the inner idle watchdogs, drop the whole-call
        # precheck: a reply delivered in full must not end as a timeout.
        finished = False
        async with aclosing(method(self, *args, **kwargs)) as deltas:
            while True:
                try:
                    if finished:
                        delta = await anext(deltas)
                    else:
                        async with call_timeout(budget, deadline=deadline):
                            delta = await anext(deltas)
                except StopAsyncIteration:
                    return
                if delta.finish_reason is not None:
                    finished = True
                # A timeout context across yield would cancel the consumer while
                # it renders a delta, rather than cancelling the model request.
                yield delta

    return stream
