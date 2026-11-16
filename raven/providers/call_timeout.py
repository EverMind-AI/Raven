"""Bound a streamed model request by one deadline, even while chunks keep arriving."""

import asyncio
from collections.abc import AsyncIterator, Callable
from contextlib import aclosing, asynccontextmanager
from functools import wraps
from typing import Any

from raven.contracts.llm_provider import ChatDelta


class CallTimeoutError(TimeoutError):
    """The model request exhausted its whole-call budget."""

    def __init__(self, budget: float):
        super().__init__(f"the model request exceeded llm_call_timeout={budget:g}s")


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
        async with aclosing(method(self, *args, **kwargs)) as deltas:
            while True:
                try:
                    async with call_timeout(budget, deadline=deadline):
                        delta = await anext(deltas)
                except StopAsyncIteration:
                    return
                # A timeout context across yield would cancel the consumer while
                # it renders a delta, rather than cancelling the model request.
                yield delta

    return stream
