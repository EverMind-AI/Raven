"""The real ``raven acp`` binary: nothing but frames on stdout.

The unit tests prove the descriptor is moved. This proves it holds for the
process an editor actually spawns, with the whole import graph loaded and loguru
initialised -- which is where a stray writer would come from in the first place.

Marked ``integration`` because it spawns the binary; deselected by default, run
with ``-m integration`` or by pointing pytest at ``tests/integration``.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration

_TIMEOUT = 60.0


def _raven_bin() -> Path:
    """The console script next to the interpreter running the tests.

    Same derivation ``tui_commands`` uses for its own child, so a test and a
    real launch resolve the same binary rather than whatever PATH happens to
    hold.
    """
    return Path(sys.executable).with_name("raven.exe" if sys.platform == "win32" else "raven")


def _run(stdin_bytes: bytes) -> subprocess.CompletedProcess[bytes]:
    binary = _raven_bin()
    if not binary.exists():
        pytest.skip(f"raven console script not installed at {binary}")
    return subprocess.run(
        [str(binary), "acp"],
        input=stdin_bytes,
        capture_output=True,
        timeout=_TIMEOUT,
        check=False,
    )


def _frames(stdout: bytes) -> list[dict]:
    """Every stdout line, parsed. A line that is not a frame fails here.

    This is the assertion the whole module exists for, so it is deliberately
    strict: no skipping blanks, no tolerating a banner.
    """
    lines = stdout.decode("utf-8").splitlines()
    frames = []
    for i, line in enumerate(lines):
        try:
            frame = json.loads(line)
        except json.JSONDecodeError as exc:
            pytest.fail(f"stdout line {i} is not a frame: {line[:200]!r} ({exc})")
        assert isinstance(frame, dict), f"stdout line {i} is JSON but not an object: {line[:200]!r}"
        assert frame.get("jsonrpc") == "2.0", f"stdout line {i} is not JSON-RPC: {line[:200]!r}"
        frames.append(frame)
    return frames


def test_a_request_is_answered_and_stdout_holds_only_frames():
    """One request in, one frame out, and nothing else on the channel.

    The agent logs an INFO record at startup naming its log file. That record is
    the test's own noise source: if the fd claim or the log redirection were
    wrong it would arrive here as an unparseable line.
    """
    result = _run(b'{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}\n')

    frames = _frames(result.stdout)
    assert len(frames) == 1, f"expected exactly one frame, got {frames}"
    assert frames[0]["id"] == 1
    assert frames[0]["error"]["code"] == -32601
    assert result.returncode == 0, f"stderr:\n{result.stderr.decode('utf-8', 'replace')[:2000]}"


def test_the_startup_log_record_does_not_reach_stdout():
    """Named explicitly rather than left implied by the parse check.

    ``acp: serving on stdio`` is written through loguru at startup. Finding it on
    stdout would mean the channel is shared with the logger, which is the exact
    failure this command exists to prevent.
    """
    result = _run(b"")

    assert b"acp: serving on stdio" not in result.stdout
    assert _frames(result.stdout) == []


def test_malformed_input_is_answered_and_the_next_frame_still_lands():
    """A client that sends garbage must get an error and keep its session.

    Asserting on the frame *after* the garbage is what distinguishes recovery
    from merely not crashing.
    """
    result = _run(
        b'this is not json\n{"jsonrpc":"2.0","id":2,"method":"session/new","params":{}}\n',
    )

    frames = _frames(result.stdout)
    assert len(frames) == 2, f"expected a parse error then an answer, got {frames}"
    assert frames[0]["id"] is None, "the id lived in the line that could not be read"
    assert frames[0]["error"]["code"] == -32700
    assert frames[1]["id"] == 2
    assert result.returncode == 0


def test_closing_stdin_exits_cleanly():
    """The editor closing the pipe is how an ACP session ends. It is not an
    error, and a non-zero exit would be reported to the user as one."""
    result = _run(b"")

    assert result.returncode == 0, f"stderr:\n{result.stderr.decode('utf-8', 'replace')[:2000]}"
