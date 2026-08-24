"""The real ``raven acp`` binary: nothing but frames on stdout.

The unit tests prove the descriptor is moved. This proves it holds for the
process an editor actually spawns, with the whole import graph loaded and loguru
initialised -- which is where a stray writer would come from in the first place.

Marked ``integration`` because it spawns the binary; deselected by default, run
with ``-m integration`` or by pointing pytest at ``tests/integration``.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
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


def _run(stdin_bytes: bytes, *, home: Path | None = None) -> subprocess.CompletedProcess[bytes]:
    """Run the binary against a throwaway agent home.

    The home matters. ``raven acp`` builds a full engine, which starts the cron
    service and the memory backend against whatever ``RAVEN_HOME`` names -- so a
    test that inherited the developer's would run their schedules and write into
    their sessions. Pointed at a temporary directory instead, which also makes the
    run reproducible: no provider is configured there, so the engine reports a
    build error rather than depending on local credentials.
    """
    binary = _raven_bin()
    if not binary.exists():
        pytest.skip(f"raven console script not installed at {binary}")
    env = dict(os.environ)
    env["RAVEN_HOME"] = str(home or Path(tempfile.mkdtemp(prefix="acp-smoke-")) / "raven")
    return subprocess.run(
        [str(binary), "acp"],
        input=stdin_bytes,
        capture_output=True,
        timeout=_TIMEOUT,
        check=False,
        env=env,
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

    The agent logs an INFO record at startup naming its log file, and builds a
    whole engine before answering. That startup is the test's own noise source:
    if the fd claim or the log redirection were wrong, one of its records would
    arrive here as an unparseable line.
    """
    result = _run(b'{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":1}}\n')

    frames = _frames(result.stdout)
    assert len(frames) == 1, f"expected exactly one frame, got {frames}"
    assert frames[0]["id"] == 1
    assert frames[0]["result"]["protocolVersion"] == 1
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
    assert frames[1]["error"]["code"] == -32600, "session/new before initialize is refused, not method-not-found"
    assert result.returncode == 0


def test_a_whole_handshake_runs_in_one_process(tmp_path):
    """Three exchanges against the real binary: negotiate, mint, then a session
    the agent does not have.

    The last one is the point. An agent that answered an unknown session by
    minting a fresh one would look identical here until a user reopened a
    conversation and found it empty.
    """
    project = tmp_path / "project"
    project.mkdir()
    payload = (
        b'{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":1,"clientCapabilities":{}}}\n'
        + json.dumps(
            {"jsonrpc": "2.0", "id": 2, "method": "session/new", "params": {"cwd": str(project), "mcpServers": []}}
        ).encode()
        + b"\n"
        + b'{"jsonrpc":"2.0","id":3,"method":"session/prompt","params":{"sessionId":"acp:nope","prompt":[]}}\n'
    )

    result = _run(payload, home=tmp_path / "home")

    # By id, not by position. Every inbound frame is handled in its own task --
    # that is what makes ``session/cancel`` readable while a prompt is suspended
    # -- so a fast answer overtakes a slow one and the arrival order is not the
    # send order. Here ``session/prompt`` for a session nobody has is a lookup
    # miss while ``session/new`` mints a session and binds a working directory,
    # so 3 lands before 2 whenever the machine is quick enough. Asserting the
    # order was asserting a timing coincidence; JSON-RPC pairs answers by id for
    # exactly this reason, and a client that needed the order could not pipeline
    # at all.
    answers = {f["id"]: f for f in _frames(result.stdout)}
    assert sorted(answers) == [1, 2, 3]
    assert answers[1]["result"]["authMethods"] == []
    assert answers[2]["result"]["sessionId"].startswith("acp:")
    assert answers[3]["error"]["code"] == -32002
    assert result.returncode == 0, f"stderr:\n{result.stderr.decode('utf-8', 'replace')[:2000]}"


def test_a_batch_of_requests_is_all_answered(tmp_path):
    """Piped input reaches EOF before the handlers have run at all. An agent that
    cancelled its in-flight work on EOF would answer none of these."""
    payload = b"".join(
        json.dumps({"jsonrpc": "2.0", "id": n, "method": "initialize", "params": {"protocolVersion": 1}}).encode()
        + b"\n"
        for n in range(1, 6)
    )

    result = _run(payload, home=tmp_path / "home")

    # The set, for the same reason as the handshake above: five concurrent tasks
    # answer in whatever order they finish. What this test is about is that all
    # five are answered at all.
    assert sorted(f["id"] for f in _frames(result.stdout)) == [1, 2, 3, 4, 5]


def test_closing_stdin_exits_cleanly():
    """The editor closing the pipe is how an ACP session ends. It is not an
    error, and a non-zero exit would be reported to the user as one."""
    result = _run(b"")

    assert result.returncode == 0, f"stderr:\n{result.stderr.decode('utf-8', 'replace')[:2000]}"
