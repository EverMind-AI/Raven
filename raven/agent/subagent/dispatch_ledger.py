"""The rolling-hour dispatch budget, on disk instead of in one process's memory.

The budget exists to stop a loop nobody asked for: a sub-agent's result comes
back as a new turn, that turn can dispatch more, and nothing in that circle
needs a person. A cap per session per hour is what bounds it.

**Why a file.** In memory the cap is per process, which means it is not a cap
at all in the two situations it matters most. A gateway that restarts -- or is
restarted *by* the loop it is meant to bound -- comes back with an empty
counter, so "thirty an hour" becomes thirty per restart. And two processes
sharing one agent home (a gateway and a `raven agent` run, two gateways) each
count to thirty. The file is the only place both of them can see.

**Wall clock, not monotonic.** A monotonic clock is the right one for a window
inside a process and the only wrong one for a window that outlives it: it is
measured from an arbitrary zero that changes every time the process starts, so
two processes cannot compare readings and a restart cannot read its own. The
price is a clock that steps: forwards ages entries out early, backwards would
hold them for as long as the step, so a stamp in the future is pulled back to
now and the worst a step costs is one more window. A rate limit is a guard
rail, not an accounting record.

**Held while it is read and written.** The whole point is that two processes
charge the same budget, so the read-modify-write between them has to be one
step. Where `flock` is unavailable the ledger degrades to last-writer-wins,
which loses a charge under a race and never invents one -- the direction a
rate limit should fail in when it fails.
"""

from __future__ import annotations

import contextlib
import json
import os
import time
from collections.abc import Iterator
from pathlib import Path
from typing import IO

from loguru import logger

__all__ = ["DispatchLedger"]

LEDGER_FILENAME = "dispatch-ledger.json"


def _take(path: Path) -> "IO[str] | None":
    """An exclusive hold on the ledger, or None where one cannot be had.

    Separate from the block it guards so that only *taking* the lock is
    forgiven. Wrapping the body in the same ``except OSError`` swallowed the
    caller's own failures and re-entered the block -- and a context manager that
    yields twice raises where it is used, not where it is written.
    """
    try:
        import fcntl
    except ImportError:  # pragma: no cover - Windows
        return None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        handle = path.with_suffix(".lock").open("a+")
    except OSError as exc:
        logger.warning("dispatch ledger could not be locked ({}); charging without one", exc)
        return None
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
    except OSError as exc:
        logger.warning("dispatch ledger could not be locked ({}); charging without one", exc)
        handle.close()
        return None
    return handle


@contextlib.contextmanager
def _held(path: Path) -> Iterator[None]:
    """An exclusive hold on the ledger for the length of one charge."""
    handle = _take(path)
    try:
        yield
    finally:
        if handle is not None:
            with contextlib.suppress(ImportError, OSError):
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            with contextlib.suppress(OSError):
                handle.close()


class DispatchLedger:
    """Who dispatched what, recently, across every process sharing one home."""

    def __init__(self, path: Path | str, *, window_seconds: float) -> None:
        self.path = Path(path)
        self.window_seconds = window_seconds

    def _read(self) -> dict[str, list[float]]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return {}
        if not isinstance(data, dict):
            return {}
        return {
            str(key): [float(stamp) for stamp in value if isinstance(stamp, (int, float))]
            for key, value in data.items()
            if isinstance(value, list)
        }

    def _write(self, entries: dict[str, list[float]]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".json.writing")
        temporary.write_text(json.dumps(entries), encoding="utf-8")
        os.replace(temporary, self.path)

    def _pruned(self, entries: dict[str, list[float]], now: float) -> dict[str, list[float]]:
        """Only what is still inside the window, and only keys that still have any.

        Pruned on every charge rather than swept: the file is read and written
        whole anyway, and a session that stopped dispatching an hour ago should
        not still be a line in it.

        A stamp in the future is pulled back to now. That is a clock that was
        stepped backwards, and left alone it would hold a session out for as
        long as the step -- an NTP correction of a day would be a day's
        lock-out. Clamped, the worst a step can cost is one more window, which
        is the bound the budget already promises.
        """
        cutoff = now - self.window_seconds
        kept = {key: [min(stamp, now) for stamp in stamps if stamp >= cutoff] for key, stamps in entries.items()}
        return {key: stamps for key, stamps in kept.items() if stamps}

    def charge(self, key: str, *, limit: int) -> bool:
        """Record one dispatch against ``key``, or refuse when the window is full."""
        now = time.time()
        with _held(self.path):
            entries = self._pruned(self._read(), now)
            spent = entries.setdefault(key, [])
            if len(spent) >= limit:
                self._write(entries)
                return False
            spent.append(now)
            try:
                self._write(entries)
            except OSError as exc:
                # A charge that cannot be written is a charge nobody will see,
                # and refusing the dispatch over it would make an unwritable
                # home look like an exhausted budget. Allowed, and said once.
                logger.error("dispatch ledger at {} is not writable ({}); this charge is not recorded", self.path, exc)
            return True

    def spent(self, key: str) -> int:
        """How many dispatches ``key`` has inside the window, for a HUD to read."""
        return len(self._pruned(self._read(), time.time()).get(key, []))

    def forget(self, key: str) -> None:
        """Drop a session's window, for a session that was cancelled outright."""
        with _held(self.path):
            entries = self._pruned(self._read(), time.time())
            if entries.pop(key, None) is not None:
                self._write(entries)
