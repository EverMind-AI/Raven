"""Hand freed native memory back to the system in long-running hosts.

glibc keeps the memory C extensions free -- PyMuPDF's rasters, tokenizer
tables, pydantic-core schemas -- in per-thread arenas and seldom returns it, so
a gateway that renders a deck in a worker thread keeps that peak for the rest of
its life although nothing uses it any more. ``malloc_trim(0)`` walks every arena
and gives the free pages back. On a fragmented 1.9 GiB heap it was measured at
about 170 ms, stalling other threads' allocations for up to 20 ms, so it runs in
a worker thread, at most every ten minutes, and only once anonymous memory
stands 32 MiB above the lowest it has been since the trimmer started or last
trimmed.

Linux with glibc only; elsewhere there is nothing to call and nothing is scheduled.
"""

from __future__ import annotations

import asyncio
import ctypes
import sys
from collections.abc import Callable
from typing import Any

from loguru import logger

_INTERVAL_S = 600.0
_GROWTH_KIB = 32 * 1024

_UNRESOLVED: Any = object()
_malloc_trim: Any = _UNRESOLVED
_task: asyncio.Task[None] | None = None


def _resolve_malloc_trim() -> Any:
    global _malloc_trim
    if _malloc_trim is _UNRESOLVED:
        found = None
        if sys.platform.startswith("linux"):
            try:
                found = getattr(ctypes.CDLL(None), "malloc_trim", None)
            except OSError:
                found = None
            if found is not None:
                found.argtypes = [ctypes.c_size_t]
                found.restype = ctypes.c_int
        # One assignment: a caller in another thread sees the lookup's answer or
        # none at all, never a None it would read as "no malloc_trim here".
        _malloc_trim = found
    return _malloc_trim


def trim() -> bool:
    """Give the free pages of every malloc arena back to the system; False where there is no glibc."""
    malloc_trim = _resolve_malloc_trim()
    if malloc_trim is None:
        return False
    malloc_trim(0)
    return True


def _anon_kib() -> int | None:
    try:
        with open("/proc/self/status", encoding="ascii", errors="replace") as f:
            for line in f:
                if line.startswith("RssAnon:"):
                    return int(line.split()[1])
    except (OSError, ValueError, IndexError):
        return None
    return None


async def _trim_when_grown(*, interval_s: float, growth_kib: int, read_anon: Callable[[], int | None]) -> None:
    baseline = read_anon()
    while True:
        await asyncio.sleep(interval_s)
        try:
            now = read_anon()
            if now is None:
                continue
            if baseline is None or now - baseline < growth_kib:
                baseline = now if baseline is None else min(baseline, now)
                continue
            await asyncio.to_thread(trim)
            after = read_anon()
            if after is not None:
                logger.debug(
                    "heap: returned {} MiB to the system ({} -> {} MiB)",
                    (now - after) // 1024,
                    now // 1024,
                    after // 1024,
                )
            baseline = now if after is None else after
        except Exception as exc:  # noqa: BLE001 - a failed round must not end the trimmer
            logger.debug("heap: trim round failed: {}", exc)


def start_trimmer() -> asyncio.Task[None] | None:
    """Start trimming in the background on the running loop, once; None where it cannot help."""
    global _task
    if _resolve_malloc_trim() is None or _anon_kib() is None:
        return None
    loop = asyncio.get_running_loop()
    if _task is not None and not _task.done() and _task.get_loop() is loop:
        return _task
    _task = loop.create_task(
        _trim_when_grown(interval_s=_INTERVAL_S, growth_kib=_GROWTH_KIB, read_anon=_anon_kib),
        name="heap-trimmer",
    )
    return _task
