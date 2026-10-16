"""Export jobs: phases, slots held by live threads, tokens, expiry, and cleanup."""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path
from typing import Any, Callable

import pytest

from raven.trajectory import export_jobs as jobs_mod
from raven.trajectory import selection as sel
from raven.trajectory.export_jobs import ExportJobs, JobLimitError, JobNotFoundError

SESSION = "tui:s1"


def _plan() -> sel.SelectionPlan:
    turn = sel.TurnSummary(
        trace_id="trace-a",
        span_id="span-a",
        number=1,
        start="2026-10-01T10:01:00+00:00",
        end="2026-10-01T10:02:00+00:00",
        input_preview="hello",
        in_progress=False,
        input_artifact=None,
        main_line=True,
    )
    return sel.SelectionPlan(
        session_key=SESSION, selected=("trace-a",), dispatched=(), turns=(turn,), session_turns=(turn,)
    )


class _Gate:
    """Stands in for collect_selection; holds every caller until opened."""

    def __init__(self) -> None:
        self.open = threading.Event()
        self.entered = threading.Semaphore(0)
        self.fail: BaseException | None = None

    def __call__(self, plan: sel.SelectionPlan, *, state_dir: Path | None = None) -> sel.Selection:
        self.entered.release()
        assert self.open.wait(5), "the test never opened the gate"
        if self.fail is not None:
            raise self.fail
        return sel.Selection(plan=plan, spans=({"traceId": "trace-a", "spanId": "span-a", "attributes": {}},))


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def gate(monkeypatch) -> _Gate:
    gate = _Gate()
    monkeypatch.setattr(jobs_mod, "collect_selection", gate)
    return gate


@pytest.fixture
def clock() -> _Clock:
    return _Clock()


@pytest.fixture
def jobs(tmp_path: Path, clock: _Clock, monkeypatch) -> ExportJobs:
    monkeypatch.setenv("RAVEN_TRACING_DIR", str(tmp_path / "traces"))
    return ExportJobs(lambda: tmp_path / "exports", ttl=60.0, keep_finished=30.0, clock=clock)


def _start(jobs: ExportJobs, tmp_path: Path, name: str = "trip.tar.gz") -> str:
    return jobs.start(_plan(), file_name=name, session_path=tmp_path / "missing.jsonl")


def _wait(condition: Callable[[], bool], timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while not condition():
        assert time.monotonic() < deadline, "condition not reached"
        time.sleep(0.01)


def _threads() -> list[threading.Thread]:
    return [t for t in threading.enumerate() if t.name.startswith(jobs_mod.THREAD_PREFIX)]


def _files(tmp_path: Path) -> list[str]:
    root = tmp_path / "exports"
    return sorted(p.name for p in root.iterdir()) if root.is_dir() else []


def _quiet(jobs: ExportJobs) -> bool:
    return jobs._workers == 0 and not jobs._cleaner_alive


def test_a_job_moves_from_collecting_to_done(jobs, gate, tmp_path):
    job = _start(jobs, tmp_path)
    assert gate.entered.acquire(timeout=5)
    status = jobs.status(job)
    assert (status["state"], status["phase"], status["download_url"]) == ("running", "collecting", None)
    gate.open.set()
    _wait(lambda: jobs.status(job)["state"] == "done")
    status = jobs.status(job)
    assert status["phase"] == "done" and status["file_name"] == "trip.tar.gz"
    assert status["download_url"].startswith(f"{jobs_mod.DOWNLOAD_PATH}?token=")
    assert status["size_bytes"] > 0 and status["counts"] == {"spans": 1, "artifacts": 0, "messages": 0}
    token = status["download_url"].split("token=", 1)[1]
    path, name = jobs.download(token)
    assert path.is_file() and name == "trip.tar.gz"
    _wait(lambda: _quiet(jobs))


def test_a_failed_collection_is_reported_and_leaves_no_file(jobs, gate, tmp_path):
    gate.fail = LookupError("no spans left for selected turn(s): trace-a")
    gate.open.set()
    job = _start(jobs, tmp_path)
    _wait(lambda: jobs.status(job)["state"] == "failed")
    assert jobs.status(job)["error"] == "LookupError: no spans left for selected turn(s): trace-a"
    _wait(lambda: _quiet(jobs))
    assert _files(tmp_path) == []


def test_cancelled_jobs_keep_their_slots_until_their_threads_exit(jobs, gate, tmp_path):
    first, second = _start(jobs, tmp_path), _start(jobs, tmp_path)
    assert gate.entered.acquire(timeout=5) and gate.entered.acquire(timeout=5)
    before = len(_threads())
    jobs.cancel(first)
    jobs.cancel(second)
    assert jobs.status(first)["state"] == "cancelled"
    with pytest.raises(JobLimitError) as excinfo:
        _start(jobs, tmp_path)
    assert excinfo.value.limit == 2
    assert len(_threads()) == before
    gate.open.set()
    _wait(lambda: _quiet(jobs))
    assert _files(tmp_path) == []
    third = _start(jobs, tmp_path)
    _wait(lambda: jobs.status(third)["state"] == "done")
    _wait(lambda: _quiet(jobs))


def test_cancelling_a_finished_job_kills_its_token_and_removes_its_file(jobs, gate, tmp_path):
    gate.open.set()
    job = _start(jobs, tmp_path)
    _wait(lambda: jobs.status(job)["state"] == "done")
    _wait(lambda: _quiet(jobs))
    token = jobs.status(job)["download_url"].split("token=", 1)[1]
    jobs.cancel(job)
    assert jobs.download(token) is None
    _wait(lambda: _quiet(jobs) and _files(tmp_path) == [])
    jobs.cancel(job)
    assert jobs.status(job)["state"] == "cancelled"


def test_unknown_jobs_are_reported(jobs):
    with pytest.raises(JobNotFoundError):
        jobs.status("exj-nope")
    with pytest.raises(JobNotFoundError):
        jobs.cancel("exj-nope")
    assert jobs.download("nope") is None
    assert jobs.download("") is None


def test_only_a_plan_starts_a_job(jobs, tmp_path):
    with pytest.raises(TypeError):
        jobs.start(["trace-a"], file_name="x.tar.gz", session_path=tmp_path / "s.jsonl")  # type: ignore[arg-type]


def test_files_and_records_expire(jobs, gate, clock, tmp_path):
    gate.open.set()
    job = _start(jobs, tmp_path)
    _wait(lambda: jobs.status(job)["state"] == "done")
    _wait(lambda: _quiet(jobs))
    token = jobs.status(job)["download_url"].split("token=", 1)[1]
    clock.now += 61
    assert jobs.download(token) is None
    assert jobs.status(job)["state"] == "cancelled"
    _wait(lambda: _quiet(jobs) and _files(tmp_path) == [])
    clock.now += 31
    with pytest.raises(JobNotFoundError):
        jobs.status(job)


def test_the_startup_sweep_removes_only_stale_packages(jobs, tmp_path):
    root = tmp_path / "exports"
    root.mkdir()
    old = time.time() - 3600
    for name in ("old.tar.gz", ".old.tar.gz.part", "fresh.tar.gz", "notes.txt"):
        (root / name).write_text("x", encoding="utf-8")
    os.utime(root / "old.tar.gz", (old, old))
    os.utime(root / ".old.tar.gz.part", (old, old))
    os.utime(root / "notes.txt", (old, old))
    assert jobs.sweep_stale_files() == 2
    assert _files(tmp_path) == ["fresh.tar.gz", "notes.txt"]


def test_the_startup_sweep_tolerates_a_missing_directory(jobs):
    assert jobs.sweep_stale_files() == 0


def test_shutdown_cancels_everything_and_the_registry_still_works(jobs, gate, tmp_path):
    gate.open.set()
    job = _start(jobs, tmp_path)
    _wait(lambda: jobs.status(job)["state"] == "done")
    _wait(lambda: _quiet(jobs))
    token = jobs.status(job)["download_url"].split("token=", 1)[1]
    assert jobs.shutdown() is True
    assert jobs.download(token) is None and _files(tmp_path) == []
    again = _start(jobs, tmp_path)
    _wait(lambda: jobs.status(again)["state"] == "done")
    _wait(lambda: _quiet(jobs))


def test_a_cancel_racing_the_worker_exit_still_removes_the_file(jobs, gate, tmp_path, monkeypatch):
    """The worker finished and marked the job done; a cancel then lands while
    its thread is still on the way out. Whichever side looks last deletes."""
    gate.open.set()
    real_write = jobs_mod.write_export
    hold = threading.Event()
    done_writing = threading.Event()

    def slow_tail(*args: Any, **kwargs: Any):
        summary = real_write(*args, **kwargs)
        done_writing.set()
        assert hold.wait(5)
        return summary

    monkeypatch.setattr(jobs_mod, "write_export", slow_tail)
    job = _start(jobs, tmp_path)
    assert done_writing.wait(5)
    jobs.cancel(job)
    hold.set()
    _wait(lambda: _quiet(jobs) and _files(tmp_path) == [])
    assert jobs.status(job)["state"] == "cancelled"


def _done_jobs(jobs: ExportJobs, tmp_path: Path, count: int) -> list[str]:
    ids = []
    for _ in range(count):
        ids.append(_start(jobs, tmp_path))
        _wait(lambda: jobs.status(ids[-1])["state"] == "done")
        _wait(lambda: _quiet(jobs))
    return ids


def test_a_finished_job_outlives_the_record_window_until_its_ttl(tmp_path, gate, clock):
    jobs = ExportJobs(lambda: tmp_path / "exports", ttl=3600.0, keep_finished=600.0, clock=clock)
    gate.open.set()
    [job] = _done_jobs(jobs, tmp_path, 1)
    token = jobs.status(job)["download_url"].split("token=", 1)[1]
    clock.now += 601
    assert jobs.status(job)["state"] == "done" and jobs.download(token) is not None
    clock.now += 3000
    assert jobs.download(token) is None
    _wait(lambda: _quiet(jobs) and _files(tmp_path) == [])


def test_a_blocked_cancelled_worker_keeps_its_slot_past_the_record_window(jobs, gate, clock, tmp_path):
    first, second = _start(jobs, tmp_path), _start(jobs, tmp_path)
    assert gate.entered.acquire(timeout=5) and gate.entered.acquire(timeout=5)
    jobs.cancel(first)
    jobs.cancel(second)
    clock.now += 31
    with pytest.raises(JobNotFoundError):
        jobs.status("exj-unknown")
    assert jobs.status(first)["state"] == "cancelled", "a record with a live worker is not swept"
    with pytest.raises(JobLimitError):
        _start(jobs, tmp_path)
    gate.open.set()
    _wait(lambda: _quiet(jobs))
    assert _files(tmp_path) == []
    third = _start(jobs, tmp_path)
    _wait(lambda: jobs.status(third)["state"] == "done")
    _wait(lambda: _quiet(jobs))


def test_retiring_many_packages_at_once_uses_one_cleaning_thread(tmp_path, gate, clock, monkeypatch):
    jobs = ExportJobs(lambda: tmp_path / "exports", limit=2, ttl=60.0, clock=clock)
    gate.open.set()
    ids = _done_jobs(jobs, tmp_path, 6)
    assert len(_files(tmp_path)) == 6
    real_remove = jobs_mod._remove
    release = threading.Event()

    def slow_remove(path: Path) -> None:
        assert release.wait(5)
        real_remove(path)

    monkeypatch.setattr(jobs_mod, "_remove", slow_remove)
    started = time.monotonic()
    for job in ids[:3]:
        jobs.cancel(job)
    clock.now += 61
    assert all(jobs.status(job)["state"] == "cancelled" for job in ids)
    assert time.monotonic() - started < 1.0, "cancel and status must not wait on the disk"
    cleaners = [t for t in _threads() if t.name.endswith("-clean")]
    assert len(cleaners) == 1 and len(_threads()) == 1
    release.set()
    _wait(lambda: _quiet(jobs) and _files(tmp_path) == [])


def test_shutdown_returns_once_the_files_are_really_gone(tmp_path, gate, clock, monkeypatch):
    jobs = ExportJobs(lambda: tmp_path / "exports", ttl=60.0, clock=clock)
    gate.open.set()
    _done_jobs(jobs, tmp_path, 2)
    real_remove = jobs_mod._remove

    def slow_remove(path: Path) -> None:
        time.sleep(0.05)
        real_remove(path)

    monkeypatch.setattr(jobs_mod, "_remove", slow_remove)
    assert jobs.shutdown(timeout=5) is True
    assert _files(tmp_path) == [] and _quiet(jobs)


def test_shutdown_gives_up_on_a_worker_that_does_not_stop_in_time(jobs, gate, tmp_path):
    job = _start(jobs, tmp_path)
    assert gate.entered.acquire(timeout=5)
    assert jobs.shutdown(timeout=0.05) is False
    assert jobs.status(job)["state"] == "cancelled"
    gate.open.set()
    _wait(lambda: _quiet(jobs))
    assert _files(tmp_path) == []


def test_failed_jobs_give_up_their_tokens_at_once(jobs, gate, clock, tmp_path):
    gate.fail = LookupError("no spans left")
    gate.open.set()
    for _ in range(3):
        job = _start(jobs, tmp_path)
        _wait(lambda job=job: jobs.status(job)["state"] == "failed")
        _wait(lambda: _quiet(jobs))
    assert jobs._tokens == {}
    clock.now += 31
    assert jobs.download("anything") is None
    assert jobs._jobs == {} and jobs._tokens == {}


def test_a_finished_job_keeps_its_token_until_its_ttl_beside_failed_ones(jobs, gate, clock, tmp_path):
    gate.open.set()
    good = _start(jobs, tmp_path)
    _wait(lambda: jobs.status(good)["state"] == "done")
    _wait(lambda: _quiet(jobs))
    gate.fail = LookupError("no spans left")
    bad = _start(jobs, tmp_path)
    _wait(lambda: jobs.status(bad)["state"] == "failed")
    _wait(lambda: _quiet(jobs))
    token = jobs.status(good)["download_url"].split("token=", 1)[1]
    assert list(jobs._tokens) == [token]
    clock.now += 31
    assert jobs.download(token) is not None, "the record window does not touch a finished job"
    clock.now += 30
    assert jobs.download(token) is None and jobs._tokens == {}


def test_shutdown_leaves_no_token_behind(jobs, gate, tmp_path):
    gate.open.set()
    _start(jobs, tmp_path)
    gate.fail = LookupError("no spans left")
    _start(jobs, tmp_path)
    _wait(lambda: _quiet(jobs) and len(jobs._jobs) == 2 and all(j.state != "running" for j in jobs._jobs.values()))
    assert jobs.shutdown() is True
    assert jobs._tokens == {}
