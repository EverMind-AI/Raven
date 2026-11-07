"""Exercise actual Raven-Code workers, including work surviving caller cancellation."""

import asyncio
import contextvars
import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "agents/raven-code/plugins/code-flow"))
from code_flow.tools.diagnostic_workers import DiagnosticWorkers, WorkersBusyError  # noqa: E402


async def _until(predicate):
    async def poll():
        while not predicate():
            await asyncio.sleep(0.001)

    await asyncio.wait_for(poll(), 2)


@pytest.mark.asyncio
@pytest.mark.parametrize("stop", ["timeout", "cancel"])
async def test_abandoned_work_retains_capacity_until_it_really_finishes(stop):
    workers = DiagnosticWorkers(limit=1)
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()

    def stalled():
        entered.set()
        try:
            release.wait(3)
        finally:
            finished.set()

    task = asyncio.create_task(workers.run(stalled, timeout=0.1 if stop == "timeout" else 5))
    try:
        await _until(entered.is_set)
        if stop == "cancel":
            task.cancel()
        with pytest.raises(TimeoutError if stop == "timeout" else asyncio.CancelledError):
            await task
        calls = []
        for _ in range(12):
            with pytest.raises(WorkersBusyError):
                await workers.run(lambda: calls.append("unexpected queued work"), timeout=1)
        assert not calls and not finished.is_set()
    finally:
        release.set()
        await _until(finished.is_set)

    async def recover():
        while True:
            try:
                return await workers.run(lambda: "recovered", timeout=1)
            except WorkersBusyError:
                await asyncio.sleep(0.001)

    assert await asyncio.wait_for(recover(), 2) == "recovered"
    assert not calls


@pytest.mark.asyncio
async def test_workers_overlap_without_using_the_hosts_executor_and_keep_context(monkeypatch):
    workers = DiagnosticWorkers(limit=4)
    barrier = threading.Barrier(4, timeout=2)
    tag = contextvars.ContextVar("diagnostic_test_tag", default="missing")
    token = tag.set("caller")
    loop_thread = threading.get_ident()

    def forbidden(*args, **kwargs):
        raise AssertionError("diagnostics used the host's executor")

    def check():
        barrier.wait()
        return threading.get_ident(), threading.current_thread().daemon, tag.get()

    monkeypatch.setattr(asyncio.get_running_loop(), "run_in_executor", forbidden)
    try:
        results = await asyncio.gather(*(workers.run(check, timeout=3) for _ in range(4)))
    finally:
        tag.reset(token)
    assert len({ident for ident, _, _ in results}) == 4
    assert all(ident != loop_thread and daemon and value == "caller" for ident, daemon, value in results)


@pytest.mark.asyncio
async def test_worker_exception_and_thread_start_failure_release_capacity(monkeypatch):
    workers = DiagnosticWorkers(limit=1)

    def broken(*args):
        raise RuntimeError("worker failed")

    with pytest.raises(RuntimeError, match="worker failed"):
        await workers.run(broken, timeout=1)
    with monkeypatch.context() as patch:
        patch.setattr(threading.Thread, "start", broken)
        with pytest.raises(RuntimeError, match="worker failed"):
            await workers.run(lambda: 0, timeout=1)
    assert await workers.run(lambda: 1, timeout=1) == 1
