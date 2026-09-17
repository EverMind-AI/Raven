"""The idle ceiling's clocks: what it reads per thread, and how two readings become idle."""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

import pytest

from tests import conftest
from tests.conftest import _Clocks, _spent


def _task(tasks: Path, tid: str, content: str) -> None:
    (tasks / tid).mkdir(parents=True)
    (tasks / tid / "schedstat").write_text(content, encoding="utf-8")


def test_the_queue_time_is_the_second_schedstat_field(tmp_path: Path) -> None:
    stat = tmp_path / "schedstat"
    stat.write_text("1230000000 456000000 7\n", encoding="utf-8")

    assert conftest._queued_ns(stat) == 456000000


@pytest.mark.parametrize("content", ["", "1230000000\n", "not numbers at all\n"], ids=["empty", "one-field", "garbage"])
def test_an_unreadable_schedstat_is_no_answer(tmp_path: Path, content: str) -> None:
    stat = tmp_path / "schedstat"
    stat.write_text(content, encoding="utf-8")

    assert conftest._queued_ns(stat) is None
    assert conftest._queued_ns(tmp_path / "absent") is None


def test_thread_queues_are_read_for_every_readable_thread(tmp_path: Path, monkeypatch) -> None:
    tasks = tmp_path / "task"
    _task(tasks, "100", "1230000000 456000000 7\n")
    _task(tasks, "101", "50000000 44000000 2\n")
    _task(tasks, "102", "garbage\n")
    monkeypatch.setattr(conftest, "_TASKS", tasks)

    assert conftest._thread_queues() == {"100": 456000000, "101": 44000000}


def test_no_task_directory_reads_as_no_threads(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(conftest, "_TASKS", tmp_path / "absent")

    assert conftest._thread_queues() == {}


def _reading(wall: float, cpu: float = 0.0, ended: int = 0, **queues: int) -> _Clocks:
    return _Clocks(wall, cpu, dict(queues), ended)


def test_idle_is_wall_minus_cpu_and_the_live_threads_queue_time(monkeypatch) -> None:
    monkeypatch.setattr(conftest, "_ended", [])
    started = _reading(10.0, cpu=1.0, t1=0, t2=0)
    now = _reading(14.0, cpu=3.0, t1=500_000_000, t2=500_000_000)

    wall, idle, queued = _spent(started, now)

    assert (wall, queued) == (4.0, 1.0)
    assert idle == pytest.approx(1.0)


def test_a_thread_that_ended_inside_the_window_is_charged_from_what_it_wrote_on_the_way_out(monkeypatch) -> None:
    monkeypatch.setattr(conftest, "_ended", [("gone", 900_000_000)])
    started = _reading(0.0, ended=0, main=0, gone=100_000_000)
    now = _reading(2.0, cpu=1.0, ended=1, main=0)

    wall, idle, queued = _spent(started, now)

    assert (wall, queued) == (2.0, 0.8)
    assert idle == pytest.approx(0.2)


def test_threads_that_ended_outside_the_window_are_not_the_windows_and_are_dropped_once_read(monkeypatch) -> None:
    ended = [("before", 5_000_000_000), ("inside", 1_000_000_000), ("after", 7_000_000_000)]
    monkeypatch.setattr(conftest, "_ended", ended)
    started = _reading(0.0, ended=1)
    now = _reading(3.0, ended=2)

    _, _, queued = _spent(started, now)

    assert queued == 1.0
    assert ended == [("after", 7_000_000_000)]


def test_a_thread_missing_from_the_end_reading_cannot_make_the_queue_time_negative(monkeypatch) -> None:
    monkeypatch.setattr(conftest, "_ended", [])
    started = _reading(0.0, lost=4_000_000_000)
    now = _reading(2.0, cpu=1.0)

    _, idle, queued = _spent(started, now)

    assert queued == 0.0
    assert idle == pytest.approx(1.0)


def test_without_scheduler_clocks_idle_is_wall_minus_os_times(monkeypatch) -> None:
    monkeypatch.setattr(conftest, "_ended", [])

    wall, idle, queued = _spent(_reading(0.0, cpu=1.0), _reading(3.0, cpu=2.5))

    assert (wall, queued) == (3.0, 0.0)
    assert idle == pytest.approx(1.5)


def test_every_thread_is_wrapped_once_to_write_its_last_line() -> None:
    """A second module object for this file would wrap twice and count twice."""
    same_file = [m for m in sys.modules.values() if getattr(m, "__file__", None) == conftest.__file__]

    assert same_file == [conftest]
    assert threading.Thread._bootstrap_inner is conftest._bootstrap_inner_then_remember


@pytest.mark.skipif(sys.platform != "linux", reason="only Linux keeps per-thread scheduler statistics")
def test_linux_reports_the_thread_queues_and_an_ending_thread_writes_its_line() -> None:
    """The gate runs strict on Linux only, and falling back to wall clock minus
    CPU there would bring the contention failures back without a word, so the
    kernel has to answer rather than be excused."""
    assert conftest._thread_queues()

    tid: list[int] = []

    def spin() -> None:
        tid.append(threading.get_native_id())
        end = time.perf_counter() + 0.05
        while time.perf_counter() < end:
            pass

    worker = threading.Thread(target=spin)
    worker.start()
    worker.join()

    assert str(tid[0]) in {ended for ended, _ in conftest._ended}
