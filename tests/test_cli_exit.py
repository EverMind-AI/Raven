"""Tests for the hard-exit guard in ``raven.cli._exit``.

The guard exists because native runtimes loaded by the agent loop segfault
during interpreter finalization; ``raven.cli.commands.run`` and the pytest
session both route their exit through it. These pin the guard's own behaviour
in-process, so the coverage does not depend on provoking a real native crash.

Scope note, so these tests are not mistaken for proof the guard is load-bearing:
``flush_and_hard_exit`` is live (the pytest session calls it on CI), but the
``lancedb_finalization_hazard`` gate in ``commands.py`` is currently dormant --
the memory plugin talks to everos over HTTP and opens no local lancedb
connection, so the probe returns False in every configuration today.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap
import threading

import pytest

from raven.cli._exit import lancedb_finalization_hazard

_LANCEDB_THREAD = "LanceDBBackgroundEventLoop"


def test_hazard_false_without_the_lancedb_thread() -> None:
    if any(t.name == _LANCEDB_THREAD for t in threading.enumerate()):
        pytest.skip("a lancedb connection is already open in this process")
    assert lancedb_finalization_hazard() is False


def test_hazard_true_while_a_thread_of_that_name_is_alive() -> None:
    """The probe keys on the thread name, not on lancedb being imported."""
    stop = threading.Event()
    worker = threading.Thread(target=stop.wait, name=_LANCEDB_THREAD, daemon=True)
    worker.start()
    try:
        assert lancedb_finalization_hazard() is True
    finally:
        stop.set()
        worker.join(timeout=5)
    assert lancedb_finalization_hazard() is False


def _hard_exit_child(code: int, *, prints: str = "") -> subprocess.CompletedProcess:
    src = f"""
    import sys
    from raven.cli._exit import flush_and_hard_exit

    sys.stdout.write({prints!r})
    flush_and_hard_exit({code})
    sys.stdout.write("UNREACHABLE")
    """
    return subprocess.run(
        [sys.executable, "-c", textwrap.dedent(src)],
        capture_output=True,
        text=True,
        timeout=60,
    )


def test_hard_exit_propagates_the_status_code() -> None:
    assert _hard_exit_child(0).returncode == 0
    assert _hard_exit_child(3).returncode == 3


def test_hard_exit_flushes_buffered_stdout_and_stops_the_interpreter() -> None:
    """Buffered output survives the bypass, and nothing after the call runs."""
    result = _hard_exit_child(0, prints="buffered-before-exit")
    assert result.stdout == "buffered-before-exit"
    assert "UNREACHABLE" not in result.stdout
