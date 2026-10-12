"""The relay: the host's ssh runs it on the machine, and it bridges the session to the daemon, starting one when none answers."""

from __future__ import annotations

import asyncio
import contextlib
import io
import json
import os
import signal
import sys
import time
from pathlib import Path

import pytest

if sys.platform == "win32":
    pytest.skip("the daemon is POSIX: it locks with fcntl and listens on a unix socket", allow_module_level=True)

from raven.node import __main__ as node_main
from raven.node import protocol, relay, server


async def _until(check, *, timeout: float = 15.0) -> None:
    deadline = time.monotonic() + timeout
    while not check():
        assert time.monotonic() < deadline, "did not happen in time"
        await asyncio.sleep(0.05)


def _alive(pid: int) -> bool:
    """Whether the daemon still runs: reaped here when it was this process's child, so a zombie reads as gone."""
    try:
        done, _ = os.waitpid(pid, os.WNOHANG)
        if done == pid:
            return False
    except ChildProcessError:
        pass
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


@pytest.mark.slow
async def test_the_relay_starts_a_daemon_when_none_answers_and_bridges_bytes_both_ways(tmp_path, monkeypatch):
    monkeypatch.setenv("RAVEN_NODE_IDLE_S", "1")
    root = tmp_path / "n"

    reader, writer = await relay.connect(root, timeout_s=60)

    pid = int(server.pid_path(root).read_text(encoding="utf-8"))
    try:
        stdin = asyncio.StreamReader()
        out = io.BytesIO()
        hello = {"jsonrpc": "2.0", "id": 1, "method": protocol.HELLO, "params": {"protocol": protocol.PROTOCOL}}
        stdin.feed_data(json.dumps(hello).encode() + b"\n")
        bridged = asyncio.create_task(relay.bridge(reader, writer, stdin, out))
        await _until(lambda: b"\n" in out.getvalue())
        stdin.feed_eof()
        await asyncio.wait_for(bridged, timeout=10)
        answer = json.loads(out.getvalue().splitlines()[0])
        assert answer["result"]["mode"] == "daemon" and answer["result"]["pid"] == pid
        assert (root / server.LOG_DIR / "serve.log").exists()

        started: list[int] = []
        monkeypatch.setattr(relay, "start_daemon", lambda *a, **k: started.append(1))
        again_reader, again_writer = await relay.connect(root)
        again_writer.close()
        with contextlib.suppress(OSError):
            await again_writer.wait_closed()
        assert started == [], "a daemon that answers is not started again"
        await _until(lambda: not _alive(pid), timeout=30)
    finally:
        if _alive(pid):
            os.kill(pid, signal.SIGTERM)


async def test_the_relay_forwards_to_nothing_that_is_not_this_user_s_own_and_starts_no_daemon_beside_it(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(server.tempfile, "gettempdir", lambda: str(tmp_path / "tmp"))
    monkeypatch.setattr(server, "owned", lambda path: False)
    started: list[int] = []
    monkeypatch.setattr(relay, "start_daemon", lambda *a, **k: started.append(1))

    with pytest.raises(
        ConnectionError, match="is not this user's own; the relay forwards to this user's Raven daemon only"
    ):
        await relay.connect(tmp_path / "n", timeout_s=0.5)

    assert started == []
    assert (server.socket_path(tmp_path / "n").parent.stat().st_mode & 0o777) == 0o700, (
        "the socket's directory, made first"
    )


async def test_the_relay_gives_up_when_no_daemon_comes_up(tmp_path, monkeypatch):
    monkeypatch.setattr(relay, "start_daemon", lambda *a, **k: None)

    with pytest.raises(ConnectionError, match="did not answer within"):
        await relay.connect(tmp_path / "n", timeout_s=0.5)


def test_attach_and_serve_are_run_with_the_directory_named(tmp_path, monkeypatch):
    seen: list[tuple[str, Path, float | None]] = []
    monkeypatch.setattr("raven.node.relay.run_attach", lambda root: seen.append(("attach", root, None)) or 7)
    monkeypatch.setattr("raven.node.server.run_daemon", lambda root, idle_s: seen.append(("serve", root, idle_s)) or 0)

    assert node_main.main(["--attach", "--dir", str(tmp_path / "n")]) == 7
    assert node_main.main(["--serve", "--dir", str(tmp_path / "n"), "--idle", "3"]) == 0
    assert node_main.main(["--serve", "--dir", "~/elsewhere"]) == 0

    assert seen[0] == ("attach", tmp_path / "n", None)
    assert seen[1] == ("serve", tmp_path / "n", 3.0)
    assert seen[2] == ("serve", Path("~/elsewhere").expanduser(), server.IDLE_EXIT_S)
