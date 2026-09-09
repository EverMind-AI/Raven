"""Tests for the hard-exit helper in ``raven.cli._exit``.

Its only caller is the pytest session hook in ``tests/conftest.py``, which needs
it because a fully green run was observed exiting 139 on Linux when the
interpreter finalized with native state live. These pin the helper's behaviour
in-process, so the coverage does not depend on provoking a real native crash.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap


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
