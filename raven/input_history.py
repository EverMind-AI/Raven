"""The composer's input history, shared with the TUI.

The file is the TUI's (``ui-tui/src/lib/history.ts``), under
``$RAVEN_HOME/.raven_history``: one block per entry, a ``# <timestamp>`` line
before it and every content line prefixed with ``+``. Dropping one leading
``+`` gives the line back, so a blank line inside an entry is a bare ``+`` and
a line that starts with ``+`` is encoded with one more. Everything that is not
a ``+`` line separates blocks, which is why the timestamp is never parsed.

The format is not re-invented here: the TUI both writes and reads it, and this
module has to read what the TUI wrote and write what the TUI will read.

Two writers share the file. Python writers take a lock file beside it, so an
append's read-dedupe-write is never interleaved with another Python writer's.
The TUI writer takes no lock -- it cannot be changed from here -- so the
guarantee across implementations is the one the filesystem gives an
``O_APPEND`` write of one complete block: the block lands at the end, and
blocks well under the platform's atomic-write size are not interleaved in
practice. What is not guaranteed: a TUI write landing during a Python write of
an unusually large prompt, and adjacent-duplicate detection racing a TUI write
(a duplicate may survive; nothing is corrupted). A stale lock older than
``_LOCK_STALE_S`` is broken by the next writer, and a writer that cannot get
the lock within ``_LOCK_TIMEOUT_S`` appends anyway rather than blocking a turn.

Sharing the file is eventual consistency, not a live feed: each front end
reads it once when it starts (the TUI at hook initialisation, the web page at
boot), so an entry the other side writes while a front end is already running
becomes visible there on its next reload, not on the next Up.
"""

from __future__ import annotations

import os
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from raven.home import raven_home

HISTORY_FILENAME = ".raven_history"
MAX_ENTRIES = 1000

_LOCK_SUFFIX = ".lock"
_LOCK_TIMEOUT_S = 2.0
_LOCK_STALE_S = 10.0
_LOCK_POLL_S = 0.02

# Windows opens a file descriptor in text mode unless ``O_BINARY`` is set, and
# the CRT then translates every ``\n`` written with ``os.write`` into ``\r\n``.
# The TUI writes LF and its reader does not normalise line endings, so a CRLF
# block would show a trailing ``\r`` there and defeat adjacent-duplicate
# detection. On POSIX the flag does not exist and this is 0.
_O_BINARY = getattr(os, "O_BINARY", 0)


def history_path() -> Path:
    """The file both front ends share.

    The two agree when ``RAVEN_HOME`` is unset or absolute. A ``~`` or a
    relative value does not: ``raven_home()`` expands it here while the TUI
    joins the literal string, so the two can name different files.
    ``RAVEN_HOME=""`` is the extreme case: ``raven_home()`` strips it and
    falls back to ``~/.raven``, while the TUI joins the empty string and reads
    the file from the current directory.
    """
    return raven_home() / HISTORY_FILENAME


def parse(text: str) -> list[str]:
    """The entries in ``text``, oldest first, exactly as the TUI's ``load()``.

    A run of ``+`` lines is one entry. Any other line ends the run; trailing
    and leading separators do not create empty entries. Line endings are left
    alone (a CRLF file yields ``\\r`` at the end of each line), matching the TUI
    reader rather than inventing a cleaner one.
    """
    entries: list[str] = []
    current: list[str] = []
    for line in text.split("\n"):
        if line.startswith("+"):
            current.append(line[1:])
        elif current:
            entries.append("\n".join(current))
            current = []
    if current:
        entries.append("\n".join(current))
    return entries


def encode(text: str) -> str:
    """One entry as the ``+`` lines the TUI's ``append()`` writes."""
    return "\n".join("+" + line for line in text.split("\n"))


def read_entries(limit: int = MAX_ENTRIES) -> list[str]:
    """The newest ``limit`` entries, oldest first.

    The file itself is never trimmed -- the TUI only caps its in-memory copy --
    so the cap belongs at this read boundary, before the answer reaches a page.

    ``Path.read_text`` applies universal-newline translation, so a CRLF file
    converted by an external editor yields clean entries here while the TUI
    reader keeps the trailing ``\\r``. A lone ``\\r`` is folded to ``\\n`` here
    as well; the TUI's ``readFileSync`` keeps it. Both writers emit LF.
    """
    if limit <= 0:
        return []
    try:
        text = history_path().read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    return parse(text)[-limit:]


def append(text: str) -> bool:
    """Append ``text`` unless it is blank or repeats the newest entry.

    Returns whether anything was written. Failures are reported in the return
    value only: the caller is a fire-and-forget RPC whose page keeps its own
    in-memory copy, and a history write must not fail a sent message.
    """
    trimmed = text.strip()
    if not trimmed:
        return False

    path = history_path()
    block = f"\n# {_timestamp()}\n{encode(trimmed)}\n"

    with _write_lock(path):
        try:
            existing = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            existing = ""
        entries = parse(existing)
        if entries and entries[-1] == trimmed:
            return False

        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | _O_BINARY, 0o600)
        except OSError:
            return False

        try:
            data = block.encode("utf-8")
            while data:
                written = os.write(fd, data)
                if written <= 0:
                    return False
                data = data[written:]
        except OSError:
            return False
        finally:
            try:
                os.close(fd)
            except OSError:
                pass
    return True


def _timestamp() -> str:
    """``2026-11-05 12:34:56.789``: what the TUI's ISO string reduces to."""
    now = datetime.now(timezone.utc)
    return f"{now:%Y-%m-%d %H:%M:%S}.{now.microsecond // 1000:03d}"


@contextmanager
def _write_lock(path: Path) -> Iterator[None]:
    """Serialise Python writers through ``.raven_history.lock``.

    The lock is advisory and only this module takes it: the TUI appends without
    looking, so this is a lock among Python writers (the gateway's own request
    handling and any second process pointed at the same home), not a lock on
    the file. Breaking a stale lock is best-effort -- two waiters can both
    decide one is stale -- and the timeout falls through to an unlocked append
    rather than holding a turn behind a wedged lock.
    """
    lock = path.with_name(path.name + _LOCK_SUFFIX)
    try:
        lock.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass

    fd: int | None = None
    deadline = time.monotonic() + _LOCK_TIMEOUT_S
    while True:
        # Checked before every attempt: a retry from ``_break_stale`` must not
        # be able to skip the deadline the way it could when the check sat
        # after it.
        if time.monotonic() >= deadline:
            break
        try:
            fd = os.open(lock, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _O_BINARY, 0o600)
            break
        except FileExistsError:
            if _break_stale(lock):
                continue
            time.sleep(_LOCK_POLL_S)
        except OSError:
            break

    try:
        if fd is not None:
            try:
                os.write(fd, f"{os.getpid()}\n".encode("ascii"))
            except OSError:
                pass
        yield
    finally:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
            try:
                lock.unlink()
            except OSError:
                pass


def _break_stale(lock: Path) -> bool:
    """Remove a lock old enough to be a dead writer's; say whether to retry.

    ``True`` means the lock is gone, so the caller may retry the open at once.
    A failed ``unlink`` is not that: the lock is still there -- on Windows the
    other process may still hold the handle -- and telling the caller to retry
    would spin it between ``open`` and ``unlink`` without ever reaching its
    deadline. ``False`` sends it through the caller's poll and deadline
    instead.
    """
    try:
        before = lock.stat()
    except FileNotFoundError:
        return True
    except OSError:
        return False
    if time.time() - before.st_mtime <= _LOCK_STALE_S:
        return False
    try:
        after = lock.stat()
        if (after.st_ino, after.st_mtime_ns) != (before.st_ino, before.st_mtime_ns):
            return False
    except FileNotFoundError:
        return True
    except OSError:
        return False
    try:
        lock.unlink()
    except FileNotFoundError:
        return True
    except OSError:
        return False
    return True


__all__ = [
    "HISTORY_FILENAME",
    "MAX_ENTRIES",
    "append",
    "encode",
    "history_path",
    "parse",
    "read_entries",
]
