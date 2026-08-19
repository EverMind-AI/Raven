"""A job that has stopped running has not necessarily finished.

The pid file does not name the job. A job script writes its solver's pid there
so the backend's ``cancel`` can reach the solver, and the script keeps running
its own wrap-up after that solver exits -- a final progress line, a log tail,
several greps over the solver log.

Measured 2026-08-14 on the FEA contact trial:

    11:41:18  solver exits
    11:41:22  probe reads "gone", records "ended from outside"
    11:41:31  script writes result.json: succeeded, rc=0

The ledger said failed while the job said succeeded. The loop spent the rest of
that campaign unsure which was true and ended it with 84% of the budget unspent.
A first "gone" therefore only starts a wait; a job truly ended from outside stays
gone and is reported that way once the wait is over.
"""

from __future__ import annotations

import pytest

from raven.ops.backend import JobStatus
from raven.ops.process_backend import ProcessExecutor


def _backend(monkeypatch, replies: list[str]) -> ProcessExecutor:
    """A backend whose probe returns the given texts in order, without sleeping."""
    b = ProcessExecutor.__new__(ProcessExecutor)
    b._job_dir = lambda idem: f"/jobs/{idem}"  # type: ignore[method-assign]
    seen = iter(replies)

    async def _arun(cmd: str):
        return 0, next(seen)

    b._arun = _arun  # type: ignore[method-assign]

    async def _no_sleep(_seconds):
        return None

    monkeypatch.setattr("raven.ops.process_backend.asyncio.sleep", _no_sleep)
    return b


async def test_a_result_written_during_wrap_up_is_read_not_called_a_kill(monkeypatch):
    """The measured case: gone, gone, then the job's own succeeded."""
    b = _backend(monkeypatch, ["gone", "gone", "result succeeded"])
    assert await b._status("t1") == JobStatus.SUCCEEDED


async def test_a_job_that_stays_gone_is_still_reported_failed(monkeypatch):
    """The wait must not turn a real kill into a job that never terminates."""
    b = _backend(monkeypatch, ["gone"] * 40)
    assert await b._status("t2") == JobStatus.FAILED


async def test_a_failed_result_written_during_wrap_up_is_still_failed(monkeypatch):
    b = _backend(monkeypatch, ["gone", "result failed"])
    assert await b._status("t3") == JobStatus.FAILED


async def test_a_running_job_is_not_delayed(monkeypatch):
    """The common path must cost exactly one probe."""
    b = _backend(monkeypatch, ["alive"])
    assert await b._status("t4") == JobStatus.RUNNING


async def test_a_job_that_never_started_is_pending(monkeypatch):
    b = _backend(monkeypatch, ["absent"])
    assert await b._status("t5") == JobStatus.PENDING


async def test_a_host_that_stops_answering_mid_wait_is_not_an_outcome(monkeypatch):
    b = ProcessExecutor.__new__(ProcessExecutor)
    b._job_dir = lambda idem: f"/jobs/{idem}"  # type: ignore[method-assign]
    seen = iter([(0, "gone"), (255, "")])

    async def _arun(cmd: str):
        return next(seen)

    b._arun = _arun  # type: ignore[method-assign]

    async def _no_sleep(_seconds):
        return None

    monkeypatch.setattr("raven.ops.process_backend.asyncio.sleep", _no_sleep)
    assert await b._status("t6") == JobStatus.RUNNING


# --- a running job has no result either ----------------------------------------

async def test_a_running_job_is_not_reported_as_ended_from_outside(monkeypatch):
    """Measured 2026-08-17 mid-run: ops_outputs answered "FAILED (ended from
    outside)" for a contact trial whose heartbeat was 15 seconds old, and the arm
    had to argue the harness down from its own verdict using the progress file.
    Absence of a result file had one explanation, and "still running" was not it.
    """
    from raven.ops.backend import JobHandle

    b = ProcessExecutor.__new__(ProcessExecutor)
    b._job_dir = lambda idem: f"/jobs/{idem}"  # type: ignore[method-assign]
    b._prefix = "ops-"
    b._objective = {}

    async def _arun(cmd: str):
        if cmd.startswith("cat "):
            return 1, "No such file"
        return 0, "alive\n"

    b._arun = _arun  # type: ignore[method-assign]
    res = await b.fetch_result(JobHandle("process", "ops-t1"))
    assert res.status is JobStatus.RUNNING
    assert "still running" in (res.error or "")
    assert "ended from outside" not in (res.error or "")


async def test_a_job_whose_process_is_gone_still_gets_the_outside_verdict(monkeypatch):
    """The verdict is right when it is right: nothing wrote a result and nothing is
    running, so the reason is not in what the job left behind."""
    from raven.ops.backend import JobHandle

    b = ProcessExecutor.__new__(ProcessExecutor)
    b._job_dir = lambda idem: f"/jobs/{idem}"  # type: ignore[method-assign]
    b._prefix = "ops-"
    b._objective = {}

    async def _arun(cmd: str):
        if cmd.startswith("cat ") and "result.json" in cmd:
            return 1, "No such file"
        if "kill -0" in cmd:
            return 0, "gone\n"
        return 0, "progress bar 99%\n"

    b._arun = _arun  # type: ignore[method-assign]
    res = await b.fetch_result(JobHandle("process", "ops-t1"))
    assert res.status is JobStatus.FAILED
    assert "ended from outside" in (res.error or "")
