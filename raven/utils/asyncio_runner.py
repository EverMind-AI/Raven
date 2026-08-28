"""``asyncio.run`` with the two unbounded steps of its teardown capped.

``asyncio.Runner.close`` ends by cancelling every task still alive and gathering
them with no timeout, then joins the default executor's threads with a 300s
budget. Both run *after* the host's own shutdown has finished and returned, so
bounding the waits inside that shutdown moves the hang here rather than removing
it: one task that swallows its cancellation holds the process open forever.

The tasks that reach this point are the ones a bounded shutdown deliberately
gave up on -- an ACP answer handler parked on a broker, a sub-agent run inside a
provider call. Waiting on them again, unbounded, undoes the decision that let
the exit proceed.

Only those two steps change. ``Runner.run`` is the stdlib's, so Ctrl-C still
cancels the *main task* and lets its ``finally`` perform the host's whole
teardown inside the loop before anything here runs -- the semantics a raw
``loop.run_until_complete`` would lose, turning a shutdown into a
``KeyboardInterrupt`` raised straight out of the loop.
"""

from __future__ import annotations

import asyncio
import functools
from asyncio import constants, runners
from typing import Any, Coroutine, TypeVar

from loguru import logger

_T = TypeVar("_T")

# How long the final sweep waits on tasks it has just cancelled. Small because
# everything with a claim on this process has already had its own budget by the
# time the sweep runs; this only covers the unwind of what those budgets left.
_FINAL_SWEEP_S = 2.0

# How long the default executor is given to join its worker threads, against a
# stdlib default of 300s. A bound at the asyncio layer only: a worker wedged
# past it is still joined by the interpreter's own atexit hook, which no
# asyncio-level policy can reach.
_EXECUTOR_JOIN_S = 2.0

_DESTROYED_PENDING = "Task was destroyed but it is pending!"


def run(
    coro: Coroutine[Any, Any, _T],
    *,
    sweep_timeout: float = _FINAL_SWEEP_S,
    executor_timeout: float = _EXECUTOR_JOIN_S,
) -> _T:
    """Run ``coro`` to completion, then tear the loop down without hanging.

    A drop-in for ``asyncio.run`` at an entry point whose coroutine performs a
    shutdown of its own. Signal handling, async generator shutdown, executor
    shutdown and the loop close are all the stdlib's; only the two budgets are
    ours.
    """
    runner = asyncio.Runner()
    try:
        return runner.run(coro)
    finally:
        _close_bounded(runner, sweep_timeout, executor_timeout)


def _close_bounded(runner: Any, sweep_timeout: float, executor_timeout: float) -> None:
    """``Runner.close`` with its two open-ended steps capped.

    ``close`` is the only place the loop is closed and the runner's state is
    settled, and it exposes no seam for either step, so the two module
    attributes it reads are swapped for the duration of the call and restored
    after. Everything else the close does is left alone. Safe because this runs
    once, on the main thread, as the process is going away.
    """
    original_cancel = runners._cancel_all_tasks
    original_join = constants.THREAD_JOIN_TIMEOUT
    runners._cancel_all_tasks = functools.partial(_sweep, timeout=sweep_timeout)
    constants.THREAD_JOIN_TIMEOUT = executor_timeout
    try:
        runner.close()
    finally:
        runners._cancel_all_tasks = original_cancel
        constants.THREAD_JOIN_TIMEOUT = original_join


def _sweep(loop: asyncio.AbstractEventLoop, *, timeout: float) -> None:
    """Cancel every remaining task and wait out only ``timeout`` of the unwind.

    Stands in for ``asyncio.runners._cancel_all_tasks``, and keeps its contract:
    the loop is left usable for the asyncgen and executor shutdowns that follow,
    and a task that finished with an exception is still reported.
    """
    remaining = asyncio.all_tasks(loop)
    if not remaining:
        return
    for task in remaining:
        task.cancel()
    loop.run_until_complete(asyncio.wait(remaining, timeout=timeout))

    stuck = [task for task in remaining if not task.done()]
    for task in remaining:
        if task in stuck or task.cancelled():
            continue
        exc = task.exception()
        if exc is not None:
            loop.call_exception_handler(
                {
                    "message": "unhandled exception during shutdown",
                    "exception": exc,
                    "task": task,
                }
            )
    if stuck:
        # Named once, here, because the alternative report is the one asyncio
        # emits from `Task.__del__` at interpreter exit -- which arrives after
        # the terminal has the prompt back and says nothing about which
        # shutdown gave up on it. That one is suppressed just below.
        logger.warning(
            "shutdown: {} task(s) ignored their cancellation and are abandoned to the process exit",
            len(stuck),
        )
        loop.set_exception_handler(_drop_destroyed_pending)


def _drop_destroyed_pending(loop: asyncio.AbstractEventLoop, context: dict[str, Any]) -> None:
    """Swallow the garbage collector's complaint about a task we already reported."""
    if context.get("message") == _DESTROYED_PENDING:
        return
    loop.default_exception_handler(context)


__all__ = ["run"]
