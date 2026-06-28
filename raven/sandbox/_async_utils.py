"""Internal async helpers shared between the sandbox debug server and CLI."""
from __future__ import annotations

import asyncio


async def cancel_and_collect(task: asyncio.Task) -> None:
    """Cancel a single task and absorb its result.

    Convention: use this for a single task in a teardown / race path. For
    multiple tasks at end-of-handler, prefer ``cancel()`` + ``asyncio.gather(
    *tasks, return_exceptions=True)`` — functionally equivalent for one task,
    just more convenient when you already have a list to fan out.

    Without the await, a cancelled task that raises something other than
    CancelledError surfaces a "Task exception was never retrieved" warning
    when garbage-collected. Using this helper everywhere we cancel keeps
    teardown paths quiet regardless of what the underlying coroutine does.

    Works for tasks in any state — cancel() on a done task is a no-op and
    ``await task`` on a done task returns/raises immediately, so the
    absorption below covers all of {pending, completed, failed, cancelled}.

    A CancelledError from ``await task`` could mean either (a) ``task``
    finished due to our cancel — swallow it, or (b) the *parent* (the caller)
    was itself cancelled and the await is propagating that — re-raise so
    shutdown still flows. We distinguish via task.cancelled(): True only
    when (a).
    """
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        if not task.cancelled():
            raise
    except Exception:
        pass
