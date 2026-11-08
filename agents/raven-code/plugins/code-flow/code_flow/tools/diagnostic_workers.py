"""Bounded Raven-Code diagnostic work, isolated from the host's default executor."""

from __future__ import annotations

import asyncio
import contextvars
import threading
from concurrent.futures import Future
from typing import Callable, TypeVar

T = TypeVar("T")
MAX_WORKERS = 4


class WorkersBusyError(RuntimeError):
    """All diagnostic workers are still occupied; new work is not queued."""


class DiagnosticWorkers:
    """Keep capacity until real work finishes, even after its caller times out."""

    def __init__(self, limit: int = MAX_WORKERS) -> None:
        self._slots = threading.BoundedSemaphore(limit)

    async def run(self, call: Callable[[], T], *, timeout: float) -> T:
        if not self._slots.acquire(blocking=False):
            raise WorkersBusyError("Raven-Code diagnostic capacity is occupied")
        future: Future[T] = Future()
        context = contextvars.copy_context()

        def work() -> None:
            if not future.set_running_or_notify_cancel():
                self._slots.release()
                return
            try:
                result = context.run(call)
            except BaseException as exc:
                self._slots.release()
                future.set_exception(exc)
            else:
                self._slots.release()
                future.set_result(result)

        # A stalled filesystem read cannot be killed in a Python thread. It
        # keeps its slot, but must not hold process shutdown or the host pool.
        try:
            worker = threading.Thread(target=work, name="raven-code-diagnostics", daemon=True)
            worker.start()
        except BaseException:
            self._slots.release()
            raise
        return await asyncio.wait_for(asyncio.wrap_future(future), timeout)


WORKERS = DiagnosticWorkers()
