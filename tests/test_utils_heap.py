"""Freed native memory goes back to the system, off the loop and only after growth."""

from __future__ import annotations

import asyncio
import contextlib
import ctypes
import io
import platform
import re
import sys
import threading
import types
from collections.abc import Callable

import pytest

from raven.utils import heap

_GROWTH = 32 * 1024


def test_trim_calls_malloc_trim_with_no_padding(monkeypatch: pytest.MonkeyPatch) -> None:
    pads: list[int] = []
    monkeypatch.setattr(heap, "_malloc_trim", lambda pad: pads.append(pad) or 0)
    assert heap.trim() is True
    assert pads == [0]


def test_trim_is_a_no_op_without_malloc_trim(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(heap, "_malloc_trim", None)
    assert heap.trim() is False


@pytest.mark.skipif(platform.libc_ver()[0] != "glibc", reason="malloc_trim is glibc's")
def test_malloc_trim_resolves_on_glibc(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(heap, "_malloc_trim", heap._UNRESOLVED)
    assert heap.trim() is True


_STATUS_PATH = "/proc/self/status"


def _fake_status(monkeypatch: pytest.MonkeyPatch, text: str) -> None:
    """Make the reader see ``text`` as the process's status file; every other file opens as it does."""
    real = open

    def fake(file, *args, **kwargs):
        if file == _STATUS_PATH:
            return io.StringIO(text)
        return real(file, *args, **kwargs)

    monkeypatch.setattr(heap, "open", fake, raising=False)


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="/proc/self/status is Linux's")
def test_the_reader_reads_rss_anon_from_proc() -> None:
    with open(_STATUS_PATH, encoding="ascii", errors="replace") as f:
        status = f.read()
    match = re.search(r"^RssAnon:\s+(\d+) kB$", status, re.MULTILINE)
    assert match is not None
    kib = heap._anon_kib()
    assert isinstance(kib, int)
    assert kib > 0
    # A second reading of a process that keeps allocating, so close rather than equal.
    assert abs(kib - int(match.group(1))) <= 64 * 1024


def test_the_reader_picks_rss_anon_by_name(monkeypatch: pytest.MonkeyPatch) -> None:
    # VmRSS and RssFile carry other numbers and RssAnon is not the first Rss line, so a
    # reader that takes the wrong line or the wrong token does not land on this value.
    _fake_status(
        monkeypatch,
        "Name:\tpython\nVmRSS:\t  900000 kB\nRssFile:\t    4000 kB\nRssAnon:\t  123456 kB\nRssShmem:\t       0 kB\n",
    )
    assert heap._anon_kib() == 123_456


@pytest.mark.parametrize(
    "status",
    [
        "Name:\tpython\nVmRSS:\t  900000 kB\nRssFile:\t    4000 kB\n",
        "Name:\tpython\nVmRSS:\t  900000 kB\nRssAnon:\t  many kB\n",
        "Name:\tpython\nRssAnon:\n",
    ],
    ids=["no-line", "not-a-number", "no-value"],
)
def test_the_reader_has_no_reading_without_a_usable_rss_anon(monkeypatch: pytest.MonkeyPatch, status: str) -> None:
    _fake_status(monkeypatch, status)
    assert heap._anon_kib() is None


def _readings(*values: int):
    it, last = iter(values), values[-1]
    return lambda: next(it, last)


def _counted(*values: int) -> tuple[list[int], Callable[[], int]]:
    """A scripted reader that also keeps what it has handed out, so a trim can say which reading set it off."""
    script = _readings(*values)
    reads: list[int] = []

    def read() -> int:
        reads.append(script())
        return reads[-1]

    return reads, read


async def _cancel_once(task: asyncio.Task, done, timeout: float = 5.0) -> None:
    """Cancel ``task`` once ``done()`` holds; one that ended on its own is awaited, not read."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not done():
        assert loop.time() < deadline, "the trimmer never got there"
        if task.done():
            # Awaited, not read: it re-raises the failure, and a bare exception()
            # answers with an empty string for one that was cancelled.
            await task
        await asyncio.sleep(0.005)
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


async def test_the_trimmer_trims_off_the_loop_once_memory_has_grown(monkeypatch: pytest.MonkeyPatch) -> None:
    threads: list[int] = []
    monkeypatch.setattr(heap, "trim", lambda: threads.append(threading.get_ident()) or True)
    read = _readings(100_000, 110_000, 140_000, 100_000)
    task = asyncio.create_task(heap._trim_when_grown(interval_s=0.001, growth_kib=_GROWTH, read_anon=read))
    await _cancel_once(task, lambda: threads)
    assert len(threads) == 1
    assert threads[0] != threading.get_ident()


async def test_a_drop_lowers_the_baseline_growth_is_measured_from(monkeypatch: pytest.MonkeyPatch) -> None:
    reads, read = _counted(100_000, 60_000, 95_000, 95_000)
    trims: list[int] = []
    monkeypatch.setattr(heap, "trim", lambda: trims.append(len(reads)) or True)
    task = asyncio.create_task(heap._trim_when_grown(interval_s=0.001, growth_kib=_GROWTH, read_anon=read))
    await _cancel_once(task, lambda: len(reads) >= 6)
    # Nothing at the drop itself; the trim comes at the 95,000 KiB reading, about 34 MiB
    # over the 60,000 KiB low the drop set.
    assert trims == [3]


async def test_after_a_trim_the_baseline_is_what_the_trim_left(monkeypatch: pytest.MonkeyPatch) -> None:
    reads, read = _counted(100_000, 150_000, 120_000, 140_000, 120_000, 120_000)
    trims: list[int] = []
    monkeypatch.setattr(heap, "trim", lambda: trims.append(len(reads)) or True)
    task = asyncio.create_task(heap._trim_when_grown(interval_s=0.001, growth_kib=_GROWTH, read_anon=read))
    await _cancel_once(task, lambda: len(reads) >= 8)
    # One trim. The 140 that follows is 20,000 KiB over the 120 the trim left, so a
    # baseline that outlived the trim would trim again there with nothing to give back.
    assert trims == [2]


async def test_after_a_trim_growth_is_measured_from_what_the_trim_left(monkeypatch: pytest.MonkeyPatch) -> None:
    reads, read = _counted(100_000, 140_000, 100_000, 110_000, 140_000, 100_000)
    trims: list[int] = []
    monkeypatch.setattr(heap, "trim", lambda: trims.append(len(reads)) or True)
    task = asyncio.create_task(heap._trim_when_grown(interval_s=0.001, growth_kib=_GROWTH, read_anon=read))
    await _cancel_once(task, lambda: len(reads) >= 8)
    # The second 140,000 KiB reading is about 39 MiB over the 100,000 the first trim left,
    # though no higher than the peak before it.
    assert trims == [2, 5]


async def test_growth_of_exactly_the_threshold_trims(monkeypatch: pytest.MonkeyPatch) -> None:
    reads, read = _counted(100_000, 100_000 + _GROWTH, 100_000)
    trims: list[int] = []
    monkeypatch.setattr(heap, "trim", lambda: trims.append(len(reads)) or True)
    task = asyncio.create_task(heap._trim_when_grown(interval_s=0.001, growth_kib=_GROWTH, read_anon=read))
    await _cancel_once(task, lambda: len(reads) >= 5)
    assert trims == [2]


@pytest.mark.parametrize("failure", [OSError("status unreadable"), ValueError("unexpected status")])
async def test_a_round_that_fails_does_not_end_the_trimmer(monkeypatch: pytest.MonkeyPatch, failure: Exception) -> None:
    trims: list[int] = []
    monkeypatch.setattr(heap, "trim", lambda: trims.append(1) or True)
    script = iter([100_000, failure, 140_000])

    def read() -> int:
        value = next(script, 100_000)
        if isinstance(value, Exception):
            raise value
        return value

    task = asyncio.create_task(heap._trim_when_grown(interval_s=0.001, growth_kib=_GROWTH, read_anon=read))
    await _cancel_once(task, lambda: trims)


async def test_growth_below_the_threshold_never_trims(monkeypatch: pytest.MonkeyPatch) -> None:
    trims: list[int] = []
    ticks: list[int] = []
    monkeypatch.setattr(heap, "trim", lambda: trims.append(1) or True)

    def creeping() -> int:
        ticks.append(1)
        return 100_000 + 200 * len(ticks)

    task = asyncio.create_task(heap._trim_when_grown(interval_s=0.001, growth_kib=_GROWTH, read_anon=creeping))
    await _cancel_once(task, lambda: len(ticks) >= 20)
    assert trims == []


async def test_cancelling_during_a_trim_ends_the_trimmer_cleanly(monkeypatch: pytest.MonkeyPatch) -> None:
    started, release, finished = threading.Event(), threading.Event(), threading.Event()

    def slow_trim() -> bool:
        started.set()
        release.wait(5)
        finished.set()
        return True

    monkeypatch.setattr(heap, "trim", slow_trim)
    task = asyncio.create_task(
        heap._trim_when_grown(interval_s=0.001, growth_kib=_GROWTH, read_anon=_readings(100_000, 200_000))
    )
    loop = asyncio.get_running_loop()
    deadline = loop.time() + 5
    while not started.is_set():
        assert loop.time() < deadline
        await asyncio.sleep(0.005)
    # The loop got here while the trim was still running, so the trim is not holding it.
    assert not finished.is_set()
    task.cancel()
    release.set()
    # Bounded, so a trimmer that swallowed the cancel fails here instead of hanging the suite.
    done, _ = await asyncio.wait({task}, timeout=5)
    assert task in done
    assert task.cancelled()


async def test_start_trimmer_starts_once_per_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(heap, "_malloc_trim", lambda pad: 0)
    monkeypatch.setattr(heap, "_anon_kib", lambda: 1000)
    monkeypatch.setattr(heap, "_task", None)
    first = heap.start_trimmer()
    try:
        assert first is not None
        assert heap.start_trimmer() is first
    finally:
        assert first is not None
        first.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await first


async def test_start_trimmer_starts_nothing_without_malloc_trim(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(heap, "_malloc_trim", None)
    monkeypatch.setattr(heap, "_task", None)
    assert heap.start_trimmer() is None


async def test_start_trimmer_starts_nothing_without_a_memory_reading(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(heap, "_malloc_trim", lambda pad: 0)
    monkeypatch.setattr(heap, "_anon_kib", lambda: None)
    monkeypatch.setattr(heap, "_task", None)
    assert heap.start_trimmer() is None


async def test_start_trimmer_checks_every_ten_minutes_for_32_mib_of_growth(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, object] = {}

    async def record(**kwargs: object) -> None:
        seen.update(kwargs)

    monkeypatch.setattr(heap, "_malloc_trim", lambda pad: 0)
    monkeypatch.setattr(heap, "_anon_kib", lambda: 1000)
    monkeypatch.setattr(heap, "_task", None)
    monkeypatch.setattr(heap, "_trim_when_grown", record)
    task = heap.start_trimmer()
    assert task is not None
    await task
    assert seen == {"interval_s": 600.0, "growth_kib": 32 * 1024, "read_anon": heap._anon_kib}


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="the lookup runs on Linux only")
def test_resolving_malloc_trim_publishes_one_answer(monkeypatch: pytest.MonkeyPatch) -> None:
    during: list[object] = []

    class _Libc:
        @property
        def malloc_trim(self):
            # What another thread would read while the lookup is still under way.
            during.append(heap._malloc_trim)
            return lambda pad: 0

    fake = types.SimpleNamespace(CDLL=lambda name: _Libc(), c_size_t=ctypes.c_size_t, c_int=ctypes.c_int)
    monkeypatch.setattr(heap, "ctypes", fake)
    monkeypatch.setattr(heap, "_malloc_trim", heap._UNRESOLVED)
    assert heap.trim() is True
    assert during == [heap._UNRESOLVED]
