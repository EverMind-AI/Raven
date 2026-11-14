"""Tests for ``raven.input_history``: the composer history file both front ends share.

The format belongs to the TUI (``ui-tui/src/lib/history.ts``): one block per
entry, a ``# <timestamp>`` line before it, every content line prefixed with
``+``, and anything that is not a ``+`` line ends the current entry. This
module's job is to read what the TUI wrote and to write what the TUI will read,
so these cases pin the format's boundaries -- blank lines inside an entry,
content that itself starts with ``+`` or ``#``, the hand-off in both
directions -- rather than any private implementation detail.

Two properties are tested at the level they are actually guaranteed:

* Python writers (threads of one process, and separate processes pointed at
  one home) are serialised by ``.raven_history.lock``, so an append's
  read-dedupe-write is not interleaved with another Python writer's. Unique
  entries are all kept, and a burst of the same text appends exactly once.
* The TUI writer takes no lock, so across implementations only the filesystem
  guarantee of an ``O_APPEND`` write of one complete block remains. No test
  can prove a race absent; the lock tests below pin what the lock does *not*
  cover -- a writer that ignores it still lands, and a wedged lock falls
  through to an unlocked append after the timeout instead of blocking a turn.

``RAVEN_HOME`` is moved to ``tmp_path`` by ``history_home`` so no test touches
the real ``~/.raven/.raven_history``.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from raven import input_history
from raven.input_history import (
    MAX_ENTRIES,
    append,
    encode,
    history_path,
    parse,
    read_entries,
)

REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def history_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point ``RAVEN_HOME`` at a scratch directory (the conftest home is already isolated)."""
    home = tmp_path / "raven-home"
    monkeypatch.setenv("RAVEN_HOME", str(home))
    return home


def _block(text: str, i: int = 0) -> str:
    """One block laid out exactly as both writers lay it out."""
    return f"\n# 2026-11-05 13:15:15.{i % 1000:03d}\n{encode(text)}\n"


def _tui_load(text: str, max_entries: int = MAX_ENTRIES) -> list[str]:
    """A line-for-line port of ``load()`` in ``ui-tui/src/lib/history.ts``.

    The TUI is the other reader of this file, so the Python writer is checked
    against the TUI's actual rules -- a ``+`` line continues the entry, any
    other line ends it -- not against ``raven.input_history.parse`` (which
    would make the comparison circular).
    """
    entries: list[str] = []
    current: list[str] = []
    for line in re.split(r"\r\n|\n|\r", text):
        if line.startswith("+"):
            current.append(line[1:])
        elif current:
            entries.append("\n".join(current))
            current = []
    if current:
        entries.append("\n".join(current))
    return entries[-max_entries:]


def _lock_path() -> Path:
    path = history_path()
    return path.with_name(path.name + input_history._LOCK_SUFFIX)


# ---------------------------------------------------------------------------
# encode / parse: the format itself
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "hello world",
        "line1\nline2",
        "line1\n\nline3",  # a blank line inside the entry is a bare "+"
        "+starts with a plus",
        "++starts with two pluses",
        "+",
        "# 2026-11-05 13:15:15.244",  # content that looks like a timestamp
        "#not a comment\n#second line",
        "  leading and trailing spaces  ",
        # Non-ASCII text must round-trip byte for byte; built from code points
        # so this source file stays ASCII for the repository language gate.
        chr(0x4E2D) + chr(0x6587) + " with " + chr(0x00FC) + "n" + chr(0x00EF) + "code",
        "a\rb",  # a lone CR stays inside a line; only "\n" separates
    ],
)
def test_encode_and_parse_round_trip(text: str) -> None:
    assert parse(encode(text)) == [text]


def test_empty_text_encodes_as_one_empty_content_line() -> None:
    # Not reachable through append() (which refuses blanks), but encode must not
    # lose the shape: "" is one (empty) line, which is one bare "+".
    assert encode("") == "+"
    assert parse("+") == [""]


def test_separators_do_not_become_empty_entries() -> None:
    assert parse("") == []
    assert parse("\n\n") == []
    assert parse("\n# timestamp\n+a\n\n# timestamp\n+b\n") == ["a", "b"]


def test_a_content_line_starting_with_plus_gets_one_more_plus() -> None:
    # Dropping one leading "+" when reading is what makes the extra one an
    # escape rather than a lossy encoding.
    assert encode("+quoted") == "++quoted"
    assert parse("++quoted") == ["+quoted"]


def test_a_hash_line_is_content_once_encoded(history_home: Path) -> None:
    append("# a heading\n+still content")
    raw = history_path().read_bytes().decode("utf-8")
    # Line endings are not the subject here; the escaping is.
    assert "+# a heading\n++still content\n" in raw.replace("\r\n", "\n")
    assert read_entries() == ["# a heading\n+still content"]


def test_any_line_that_does_not_start_with_plus_ends_the_entry() -> None:
    # The timestamp is not parsed: it is simply "not a + line", which is why a
    # garbage line would work the same way.
    assert parse("+a\n# 2026-11-05 13:15:15.244\n+b\n\n+garbage\n+c") == ["a", "b", "garbage\nc"]


# ---------------------------------------------------------------------------
# append: trimming, dedupe, error paths
# ---------------------------------------------------------------------------


def test_append_then_read_returns_the_entry(history_home: Path) -> None:
    assert append("hello world") is True
    assert read_entries() == ["hello world"]


def test_append_creates_the_home_directory(history_home: Path) -> None:
    assert not history_home.exists()
    assert append("first") is True
    assert history_path() == history_home / ".raven_history"
    assert read_entries() == ["first"]


def test_a_multiline_entry_survives_the_file_round_trip(history_home: Path) -> None:
    assert append("line1\n\nline3") is True
    assert read_entries() == ["line1\n\nline3"]


def test_append_trims_like_the_tui(history_home: Path) -> None:
    # history.ts calls line.trim(); trailing/leading whitespace never reaches
    # the file, and the stored entry is the trimmed text.
    assert append("  hello \n") is True
    assert read_entries() == ["hello"]


@pytest.mark.parametrize("text", ["", "   ", "\n\n", "\t \n"])
def test_append_refuses_blank_text(history_home: Path, text: str) -> None:
    assert append(text) is False
    assert read_entries() == []


def test_append_after_a_truncated_last_line_keeps_both_entries(history_home: Path) -> None:
    # The block starts with "\n", so a file whose last write was cut mid-line
    # still ends that line instead of gluing the new timestamp onto it.
    path = history_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"+old")
    assert append("new") is True
    assert read_entries() == ["old", "new"]


def test_a_write_failure_is_reported_not_raised(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # A fire-and-forget RPC must not fail a sent message because history moved:
    # a home path that is a file makes mkdir/open fail, and append says False.
    blocker = tmp_path / "blocked"
    blocker.write_text("not a directory")
    monkeypatch.setenv("RAVEN_HOME", str(blocker))
    assert append("nope") is False


# ---------------------------------------------------------------------------
# read_entries: the limit is a read boundary, not a file cap
# ---------------------------------------------------------------------------


def test_read_entries_returns_the_newest_limit_oldest_first(history_home: Path) -> None:
    for text in ("one", "two", "three"):
        append(text)
    assert read_entries(limit=2) == ["two", "three"]
    assert read_entries(limit=1) == ["three"]


@pytest.mark.parametrize("limit", [0, -1])
def test_a_non_positive_limit_reads_nothing(history_home: Path, limit: int) -> None:
    append("one")
    assert read_entries(limit=limit) == []


def test_a_limit_above_the_count_returns_everything(history_home: Path) -> None:
    for text in ("one", "two"):
        append(text)
    assert read_entries(limit=10_000) == ["one", "two"]


def test_read_entries_on_a_missing_file_is_empty(history_home: Path) -> None:
    assert read_entries() == []


def test_read_entries_replaces_undecodable_bytes(history_home: Path) -> None:
    path = history_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"+\xff\n")
    assert read_entries() == ["�"]


def test_a_crlf_file_reads_as_its_lf_twin(history_home: Path) -> None:
    """Both readers drop the \r an external CRLF conversion introduces (#1193).

    The Python reader goes through ``Path.read_text``, whose universal-newline
    translation folds CRLF; the TUI's ``load()`` splits on ``/\r?\n/``. A file
    converted by an external editor must therefore yield the same entries in
    both, which this pins with the TUI's own rules as the second reader.
    """
    path = history_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    lf = "".join(_block(text) for text in ("first", "second", "line a\nline b"))
    path.write_bytes(lf.replace("\n", "\r\n").encode("utf-8"))

    assert read_entries() == ["first", "second", "line a\nline b"]
    assert _tui_load(path.read_bytes().decode("utf-8")) == ["first", "second", "line a\nline b"]


def test_the_default_cap_is_the_tui_in_memory_cap_and_the_file_is_not_trimmed(history_home: Path) -> None:
    # The TUI keeps only the newest 1000 entries in memory but never truncates
    # the file, and so does this module: the cap belongs at the read boundary.
    path = history_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(_block(f"e{i:04d}", i) for i in range(1005)), encoding="utf-8")

    before = path.stat().st_size
    assert append("newest") is True
    assert path.stat().st_size > before
    assert len(parse(path.read_text(encoding="utf-8"))) == 1006

    entries = read_entries()
    assert len(entries) == 1000
    assert entries[0] == "e0006"
    assert entries[-1] == "newest"


def test_an_explicit_limit_is_not_clamped_by_read_entries(history_home: Path) -> None:
    # Only the RPC handler clamps to MAX_ENTRIES (input.history.list); the
    # lower-level read honours what its caller asked for.
    path = history_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(_block(f"e{i:04d}", i) for i in range(1005)), encoding="utf-8")
    assert len(read_entries(limit=10_000)) == 1005


# ---------------------------------------------------------------------------
# Dedupe: only the immediately previous entry, exactly like the TUI
# ---------------------------------------------------------------------------


def test_only_the_immediately_previous_entry_is_deduplicated(history_home: Path) -> None:
    # history.ts:61-63 compares against items.at(-1) only; an older duplicate
    # is a different entry and is kept.
    assert append("a") is True
    assert append("a") is False
    assert append("b") is True
    assert append("a") is True
    assert read_entries() == ["a", "b", "a"]


def test_dedupe_compares_the_trimmed_text(history_home: Path) -> None:
    assert append("  hello  ") is True
    assert append("hello") is False
    assert read_entries() == ["hello"]


def test_a_multiline_duplicate_is_deduplicated(history_home: Path) -> None:
    assert append("x\ny") is True
    assert append("x\ny") is False
    assert read_entries() == ["x\ny"]


def test_a_duplicate_the_tui_wrote_is_detected(history_home: Path) -> None:
    # Python re-reads the file instead of caching, so an entry another writer
    # just appended is still recognised as the newest one (the TUI's own cache
    # is the side that can miss this).
    path = history_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_block("same"), encoding="utf-8")
    assert append("same") is False
    assert read_entries() == ["same"]


def test_an_old_crlf_file_still_reads_and_new_blocks_are_lf(history_home: Path) -> None:
    """Before ``O_BINARY`` this writer produced CRLF; those files need no rewrite.

    The read path normalises line endings, so an old CRLF entry is still the
    newest entry for dedupe -- and the block appended after it is LF, which
    proves the write path no longer depends on that normalisation.
    """
    path = history_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\r\n# 2026-11-05 13:15:15.244\r\n+same\r\n")

    assert append("same") is False
    assert append("new") is True

    raw = path.read_bytes()
    tail = raw.split(b"+same\r\n", 1)[1]
    assert b"\r" not in tail
    assert b"+new\n" in tail
    assert read_entries() == ["same", "new"]


# ---------------------------------------------------------------------------
# Concurrency: what the lock guarantees, and what it does not
# ---------------------------------------------------------------------------


def test_threads_appending_the_same_text_append_it_once(history_home: Path) -> None:
    # This is the property the lock actually adds over O_APPEND: two writers
    # that both read "not the newest" would otherwise both append a duplicate.
    threads = 8
    barrier = threading.Barrier(threads)
    results: list[bool] = []
    results_lock = threading.Lock()

    def worker() -> None:
        barrier.wait()
        wrote = append("same")
        with results_lock:
            results.append(wrote)

    running = [threading.Thread(target=worker) for _ in range(threads)]
    for thread in running:
        thread.start()
    for thread in running:
        thread.join()

    assert results.count(True) == 1
    assert read_entries() == ["same"]


def test_threads_appending_unique_texts_lose_nothing(history_home: Path) -> None:
    threads, per_thread = 8, 10
    barrier = threading.Barrier(threads)
    failures: list[str] = []
    failures_lock = threading.Lock()

    def worker(w: int) -> None:
        barrier.wait()
        for i in range(per_thread):
            text = f"t{w}-{i}"
            if not append(text):
                with failures_lock:
                    failures.append(text)

    running = [threading.Thread(target=worker, args=(w,)) for w in range(threads)]
    for thread in running:
        thread.start()
    for thread in running:
        thread.join()

    assert failures == []
    entries = read_entries(limit=10_000)
    assert len(entries) == threads * per_thread
    assert sorted(entries) == sorted(f"t{w}-{i}" for w in range(threads) for i in range(per_thread))


_WORKER = (
    "import sys\n"
    "from raven.input_history import append\n"
    "for i in range(int(sys.argv[2])):\n"
    "    append(f'{sys.argv[1]}-{i}')\n"
)


@pytest.mark.slow
def test_processes_appending_unique_texts_lose_nothing(history_home: Path) -> None:
    # The lock is a file next to the history, so it serialises separate
    # processes pointed at one RAVEN_HOME, not just threads. Each process is a
    # real interpreter: nothing is shared through memory that could fake it.
    workers, per_worker = 4, 10
    env = os.environ.copy()
    env["RAVEN_HOME"] = str(history_home)
    running = [
        subprocess.Popen(
            [sys.executable, "-c", _WORKER, f"p{w}", str(per_worker)],
            cwd=str(REPO_ROOT),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        for w in range(workers)
    ]
    for proc in running:
        _, err = proc.communicate(timeout=120)
        assert proc.returncode == 0, err.decode("utf-8", "replace")

    entries = read_entries(limit=10_000)
    assert len(entries) == workers * per_worker
    assert sorted(entries) == sorted(f"p{w}-{i}" for w in range(workers) for i in range(per_worker))


def test_a_wedged_lock_falls_through_to_an_unlocked_append(history_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Not a guarantee this module claims: after the timeout it appends anyway
    # rather than holding a turn behind a lock nobody released. The foreign
    # lock is left alone, because the writer never owned it.
    monkeypatch.setattr(input_history, "_LOCK_TIMEOUT_S", 0.05)
    lock = _lock_path()
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text(f"{os.getpid()}\n", encoding="ascii")

    assert append("while locked") is True
    assert read_entries() == ["while locked"]
    assert lock.exists(), "an append must not delete a lock it never acquired"


def test_a_stale_lock_is_broken(history_home: Path) -> None:
    lock = _lock_path()
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text("999999\n", encoding="ascii")
    old = time.time() - (input_history._LOCK_STALE_S + 1)
    os.utime(lock, (old, old))

    assert append("after a dead writer") is True
    assert read_entries() == ["after a dead writer"]
    assert not lock.exists()


def test_append_leaves_no_lock_behind(history_home: Path) -> None:
    assert append("x") is True
    assert not _lock_path().exists()


# The probe runs in a child interpreter so the pre-fix hang is a timeout in the
# assertion below, not a wedged test process. ``unlink`` keeps raising: on
# Windows another process still holds the lock handle, on POSIX the directory
# may have become read-only.
_LOCK_PROBE = (
    "import os, time\n"
    "from pathlib import Path\n"
    "from raven import input_history as ih\n"
    "ih._LOCK_TIMEOUT_S = 0.05\n"
    "lock = ih.history_path().with_name(ih.history_path().name + ih._LOCK_SUFFIX)\n"
    "lock.parent.mkdir(parents=True, exist_ok=True)\n"
    "lock.write_text('999999\\n', encoding='ascii')\n"
    "old = time.time() - (ih._LOCK_STALE_S + 5)\n"
    "os.utime(lock, (old, old))\n"
    "def cannot_unlink(self, *args, **kwargs):\n"
    "    raise OSError('another handle holds it')\n"
    "Path.unlink = cannot_unlink\n"
    "print('appended=%r' % ih.append('probe'))\n"
)


def test_a_stale_lock_that_cannot_be_unlinked_does_not_spin(history_home: Path) -> None:
    """A stale lock that cannot be removed must not retry without a bound.

    ``unlink`` failing means the lock is still there, so a writer that treats
    it as "gone, retry now" spins between ``open`` and ``unlink`` and never
    reaches its deadline. The append must fall back to its timeout instead.
    """
    env = os.environ.copy()
    env["RAVEN_HOME"] = str(history_home)
    proc = subprocess.run(
        [sys.executable, "-c", _LOCK_PROBE],
        cwd=str(REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert proc.returncode == 0, proc.stderr
    assert "appended=True" in proc.stdout


# ---------------------------------------------------------------------------
# The failure branches: append reports, never raises, and a lock it cannot
# manage must not take the append down with it.
# ---------------------------------------------------------------------------


def test_append_reports_a_zero_length_write(history_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # os.write returning 0 means the same buffer would be retried forever.
    monkeypatch.setattr(os, "write", lambda fd, data: 0)
    assert append("short") is False


def test_append_reports_a_write_error(history_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def failing_write(fd: int, data: bytes) -> int:
        raise OSError("disk full")

    monkeypatch.setattr(os, "write", failing_write)
    assert append("nope") is False


def test_append_survives_a_close_error(history_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def failing_close(fd: int) -> None:
        raise OSError("already closed")

    monkeypatch.setattr(os, "close", failing_close)
    assert append("still lands") is True
    assert read_entries() == ["still lands"]


def test_append_survives_a_lock_unlink_error(history_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def failing_unlink(self: Path, *args: object, **kwargs: object) -> None:
        raise OSError("another handle holds it")

    monkeypatch.setattr(Path, "unlink", failing_unlink)
    assert append("lock stays") is True
    assert read_entries() == ["lock stays"]


class _StubLock:
    """A lock path whose ``stat``/``unlink`` outcomes the test scripts."""

    def __init__(self, stats: list[SimpleNamespace | OSError], unlink_error: OSError | None = None) -> None:
        self._stats = list(stats)
        self._unlink_error = unlink_error

    def stat(self) -> SimpleNamespace:
        outcome = self._stats.pop(0)
        if isinstance(outcome, OSError):
            raise outcome
        return outcome

    def unlink(self) -> None:
        if self._unlink_error is not None:
            raise self._unlink_error


def _old_stat(*, ino: int = 1) -> SimpleNamespace:
    old = time.time() - (input_history._LOCK_STALE_S + 1)
    return SimpleNamespace(st_ino=ino, st_mtime=old, st_mtime_ns=int(old * 1_000_000_000))


def test_break_stale_retries_when_the_lock_vanished() -> None:
    # ``stat`` raising ENOENT is the one stat failure that proves the lock is
    # gone; retrying the open immediately is right.
    assert input_history._break_stale(_StubLock([FileNotFoundError("vanished mid-check")])) is True


def test_break_stale_waits_when_the_lock_cannot_be_stat_ed() -> None:
    # Any other stat failure leaves the lock's existence unknown. Waiting for
    # the caller's deadline is bounded; retrying at once is not.
    assert input_history._break_stale(_StubLock([OSError("stale handle")])) is False


def test_break_stale_retries_when_the_lock_vanished_before_the_second_stat() -> None:
    assert input_history._break_stale(_StubLock([_old_stat(), FileNotFoundError("gone")])) is True


def test_break_stale_returns_false_when_the_lock_changed() -> None:
    # Two waiters can both decide an old lock is stale; the second stat is the
    # check that the waiter is still looking at the lock it decided about.
    assert input_history._break_stale(_StubLock([_old_stat(ino=1), _old_stat(ino=2)])) is False


def test_break_stale_retries_when_the_lock_vanished_before_unlink() -> None:
    stat = _old_stat()
    lock = _StubLock([stat, stat], unlink_error=FileNotFoundError("already gone"))
    assert input_history._break_stale(lock) is True


def test_break_stale_waits_when_unlink_fails() -> None:
    # The lock is still there (on Windows another process holds the handle);
    # this must not become "retry now" or ``_write_lock`` spins.
    stat = _old_stat()
    lock = _StubLock([stat, stat], unlink_error=OSError("another writer holds it"))
    assert input_history._break_stale(lock) is False


def test_parse_does_not_normalise_line_endings_but_read_entries_does(history_home: Path) -> None:
    """Pin how the two Python readers treat ``\\r`` in an externally converted file.

    ``parse`` leaves ``\\r`` alone, as its docstring says. But ``read_entries``
    (and ``append``'s dedupe read) go through ``Path.read_text``, whose text
    mode translates ``\\r\\n`` and lone ``\\r`` to ``\\n``. The TUI's ``load()``
    splits on ``/\\r\\n|\\n|\\r/``, so it folds the same endings and agrees with
    ``read_entries`` for a CRLF file and for a file of lone CRs. Both writers
    emit LF, so this only shows for a history converted by something else or
    for content pasted with CRLF.
    """
    raw = "+line one\r\n+line two\r\n"
    assert parse(raw) == ["line one\r\nline two\r"]

    path = history_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw.encode("utf-8"))
    assert read_entries() == ["line one\nline two"]


def test_read_entries_reads_a_cr_only_file_like_an_lf_one(history_home: Path) -> None:
    """A CR-only file yields the entries its LF form yields.

    ``Path.read_text`` applies universal-newline translation, so the host
    folds lone CRs the way it folds CRLF; this pins the other half of the
    line-ending parity the TUI test covers.
    """
    lf = "# 2026-11-09 00:00:00\n+first command\n# 2026-11-09 00:00:01\n+second command\n"
    path = history_path()
    path.parent.mkdir(parents=True, exist_ok=True)

    path.write_bytes(lf.replace("\n", "\r").encode("utf-8"))
    cr_entries = read_entries()

    path.write_bytes(lf.encode("utf-8"))
    assert cr_entries == read_entries() == ["first command", "second command"]


# ---------------------------------------------------------------------------
# Cross-language: the TUI's writer and reader, in both directions
# ---------------------------------------------------------------------------

# Exactly what history.ts append() writes for the two entries in the task's
# sample: a leading newline, "# <UTC ISO without T/Z>", then the + lines.
_TUI_WRITTEN = "\n# 2026-11-05 13:15:15.244\n+hello world\n\n# 2026-11-05 13:15:15.245\n+line1\n+line2\n"


def test_reads_what_the_tui_writer_wrote(history_home: Path) -> None:
    path = history_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(_TUI_WRITTEN.encode("utf-8"))
    assert read_entries() == ["hello world", "line1\nline2"]


def test_reads_tui_escaped_content(history_home: Path) -> None:
    # The TUI escapes a leading "+" the same way this module does, so the two
    # readers agree on where an entry starts.
    path = history_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\n# 2026-11-05 13:15:15.244\n++literal plus\n+#literal hash\n")
    assert read_entries() == ["+literal plus\n#literal hash"]


def test_python_output_parses_the_way_the_tui_reader_does(history_home: Path) -> None:
    written = ["plain", "line1\nline2", "line1\n\nline3", "+plus", "#hash"]
    for text in written:
        assert append(text) is True

    # Bytes as the TUI's readFileSync('utf8') sees them: no normalisation.
    raw = history_path().read_bytes().decode("utf-8")
    assert _tui_load(raw) == written


def test_python_output_reads_back_the_same_way(history_home: Path) -> None:
    # With the writer emitting LF the parsed entries agree with the bytes
    # without relying on read_text's newline normalisation.
    written = ["plain", "line1\nline2", "line1\n\nline3", "+plus", "#hash"]
    for text in written:
        assert append(text) is True
    assert read_entries() == written


def test_written_blocks_use_lf_like_the_tui(history_home: Path) -> None:
    assert append("hello") is True
    assert b"\r" not in history_path().read_bytes()


def test_append_writes_the_tui_bytes_exactly(history_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The byte-level contract, on every platform: no CR, LF block separators.

    ``os.open`` without ``O_BINARY`` is text mode on Windows, and the CRT turns
    every LF written through the descriptor into CRLF. That made the Python
    writer produce a file whose entries the TUI read with a trailing ``\\r`` on
    each one, which broke its adjacent-duplicate check. The writer now passes
    this flag, so the bytes are LF, and the TUI's ``load()`` also drops a
    CRLF's ``\\r``. Pinned as exact bytes rather than a "no \\r" search so a
    future encoding change cannot slip through.
    """

    class _FixedDatetime:
        @classmethod
        def now(cls, tz: timezone) -> datetime:
            return datetime(2026, 11, 5, 13, 15, 15, 244123, tzinfo=timezone.utc)

    monkeypatch.setattr(input_history, "datetime", _FixedDatetime)
    assert append("hello") is True
    assert history_path().read_bytes() == b"\n# 2026-11-05 13:15:15.244\n+hello\n"


def test_the_timestamp_matches_the_tui_iso_shape(history_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # history.ts writes `new Date().toISOString().replace('T',' ').replace('Z','')`:
    # UTC, millisecond precision (truncated, since the format ends at .mmm).
    class _FixedDatetime:
        @classmethod
        def now(cls, tz: timezone) -> datetime:
            assert tz is timezone.utc
            return datetime(2026, 11, 5, 13, 15, 15, 244123, tzinfo=timezone.utc)

    monkeypatch.setattr(input_history, "datetime", _FixedDatetime)
    assert append("hello") is True

    # Line endings are pinned separately (test_written_blocks_use_lf_like_the_tui).
    raw = history_path().read_bytes().decode("utf-8").replace("\r\n", "\n")
    assert re.fullmatch(r"\n# 2026-11-05 13:15:15\.244\n\+hello\n", raw), raw


# ---------------------------------------------------------------------------
# Path resolution
# ---------------------------------------------------------------------------


def test_history_path_follows_raven_home(history_home: Path) -> None:
    assert history_path() == history_home / ".raven_history"


def test_history_path_defaults_to_the_tui_location(monkeypatch: pytest.MonkeyPatch) -> None:
    # The TUI resolves RAVEN_HOME in ravenHomeDir() (ui-tui/src/lib/ravenHome.ts).
    monkeypatch.delenv("RAVEN_HOME", raising=False)
    assert history_path() == Path.home() / ".raven" / ".raven_history"
