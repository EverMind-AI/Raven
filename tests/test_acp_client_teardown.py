"""AcpClient.close ends the server wherever raven runs.

On POSIX the whole process group goes, as it always did. Windows has neither
process groups ``killpg`` can reach nor SIGKILL, and a teardown that reached
for either raised before any of the cleanup after it ran -- the child stayed
alive, the pipes stayed open -- on every path that closes a client, the node
installer's handshake included.
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from raven.acp_client import client as acp_client


class _Proc:
    def __init__(self, *, gone: bool = False) -> None:
        self.gone = gone
        self.killed = 0
        self.pid = 4242
        self.returncode = None

    def kill(self) -> None:
        if self.gone:
            raise ProcessLookupError
        self.killed += 1


def _as_posix(monkeypatch: pytest.MonkeyPatch) -> None:
    """The POSIX branch, wherever the test runs: Windows has no SIGKILL to name, so one is lent for the read."""
    monkeypatch.setattr(acp_client.signal, "SIGKILL", getattr(acp_client.signal, "SIGKILL", 9), raising=False)


async def test_on_posix_the_process_group_is_killed(monkeypatch):
    swept: list[tuple[int, int]] = []
    _as_posix(monkeypatch)
    monkeypatch.setattr(os, "killpg", lambda pgid, sig: swept.append((pgid, sig)), raising=False)
    proc = _Proc()

    await acp_client._end(4242, proc)

    assert swept == [(4242, acp_client.signal.SIGKILL)]
    assert proc.killed == 0


async def test_a_group_already_gone_is_not_an_error(monkeypatch):
    def gone(pgid, sig):
        raise ProcessLookupError

    _as_posix(monkeypatch)
    monkeypatch.setattr(os, "killpg", gone, raising=False)

    await acp_client._end(4242, _Proc())


async def test_without_process_groups_the_child_itself_is_killed(monkeypatch):
    """No ``killpg`` and no ``taskkill`` on the PATH: the child is ended by handle, and one already gone is no error."""
    monkeypatch.delattr(os, "killpg", raising=False)
    monkeypatch.setattr(acp_client.shutil, "which", lambda name, *a, **k: None)
    proc = _Proc()

    await acp_client._end(4242, proc)

    assert proc.killed == 1
    await acp_client._end(4242, _Proc(gone=True))


async def test_without_process_groups_taskkill_walks_the_tree_first(monkeypatch):
    """Where ``taskkill`` exists it ends the launcher's children by pid, bounded, before the launcher itself."""
    monkeypatch.delattr(os, "killpg", raising=False)
    monkeypatch.setattr(acp_client.shutil, "which", lambda name, *a, **k: "taskkill" if name == "taskkill" else None)
    calls: list[tuple[list[str], float]] = []

    def run(argv, **kwargs):
        calls.append((list(argv), kwargs["timeout"]))
        return acp_client.subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(acp_client.subprocess, "run", run)
    proc = _Proc()

    await acp_client._end(4242, proc)

    assert calls == [(["taskkill", "/T", "/F", "/PID", "4242"], acp_client._TEARDOWN_TASKKILL_S)]
    assert proc.killed == 1
    exited = _Proc()
    exited.returncode = 0
    await acp_client._end(4242, exited)
    assert len(calls) == 1, "a launcher that has exited is not walked: its pid may name another process by now"


async def test_a_pid_only_endpoint_keeps_the_launcher_alive_for_taskkill(monkeypatch):
    proc = SimpleNamespace(pid=4242, returncode=None, kill=Mock(), wait=AsyncMock(return_value=0))
    proc.stdin = SimpleNamespace(close=Mock(side_effect=lambda: setattr(proc, "returncode", 0)))
    walked = []

    def run(argv, **kwargs):
        walked.append((argv, proc.returncode))
        return acp_client.subprocess.CompletedProcess(argv, 0)

    monkeypatch.delattr(os, "killpg", raising=False)
    monkeypatch.setattr(acp_client.shutil, "which", lambda name: "taskkill")
    monkeypatch.setattr(acp_client.subprocess, "run", run)

    await acp_client._ProcessEndpoint(proc, proc.pid).end()

    assert walked == [(["taskkill", "/T", "/F", "/PID", "4242"], None)]
    proc.stdin.close.assert_not_called()
    proc.wait.assert_not_awaited()
    proc.kill.assert_called_once()


@pytest.mark.parametrize("with_job", [False, True])
@pytest.mark.parametrize("outcome", ["exit", "timeout", "pipe_error", "missing_stdin"])
async def test_endpoint_teardown_keeps_the_force_fallback_after_grace(monkeypatch, with_job, outcome):
    closed = []
    proc = SimpleNamespace(pid=4242, returncode=None, wait=AsyncMock())

    def close_stdin():
        if outcome == "pipe_error":
            raise BrokenPipeError
        if outcome == "exit":
            proc.returncode = 0

    proc.stdin = None if outcome == "missing_stdin" else SimpleNamespace(close=close_stdin)
    job = SimpleNamespace(close=Mock(side_effect=lambda: closed.append(proc.returncode))) if with_job else None
    fallback = AsyncMock(side_effect=lambda *args: closed.append(proc.returncode))
    monkeypatch.setattr(acp_client, "_end", fallback)
    monkeypatch.setattr(acp_client, "_TEARDOWN_GRACE_S", 0.05)
    if not with_job:
        monkeypatch.setattr(os, "killpg", Mock(), raising=False)

    started = asyncio.get_running_loop().time()
    await asyncio.wait_for(acp_client._ProcessEndpoint(proc, proc.pid, job).end(), 0.5)
    waited = asyncio.get_running_loop().time() - started

    assert closed == [0 if outcome == "exit" else None]
    assert (waited >= 0.05) == (outcome == "timeout")
    proc.wait.assert_not_awaited()
    assert fallback.await_count == (0 if with_job else 1)


@pytest.mark.parametrize("with_job", [False, True])
async def test_cancelling_the_grace_wait_still_ends_the_process_tree(monkeypatch, with_job):
    waiting = asyncio.Event()
    proc = SimpleNamespace(pid=4242, returncode=None, stdin=SimpleNamespace(close=Mock(side_effect=waiting.set)))
    job = SimpleNamespace(close=Mock()) if with_job else None
    fallback = AsyncMock()
    monkeypatch.setattr(acp_client, "_end", fallback)
    if not with_job:
        monkeypatch.setattr(os, "killpg", Mock(), raising=False)
    ending = asyncio.create_task(acp_client._ProcessEndpoint(proc, proc.pid, job).end())
    await asyncio.wait_for(waiting.wait(), 1)
    ending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await ending
    if job is not None:
        job.close.assert_called_once()
        fallback.assert_not_awaited()
    else:
        fallback.assert_awaited_once_with(proc.pid, proc)


@pytest.mark.slow
@pytest.mark.parametrize("linger", [False, True], ids=["cooperative", "ignores_eof"])
async def test_a_real_child_receives_eof_before_the_force_fallback(tmp_path: Path, linger):
    ready = tmp_path / "ready"
    ended = tmp_path / "eof"
    script = (
        "import pathlib,sys,threading;"
        f"pathlib.Path({str(ready)!r}).touch();"
        "sys.stdin.buffer.read();"
        f"pathlib.Path({str(ended)!r}).touch();" + ("threading.Event().wait()" if linger else "")
    )
    client = await acp_client.AcpClient.launch(name="eof-fixture", command=[sys.executable, "-c", script])
    try:

        async def started():
            while not ready.exists():
                await asyncio.sleep(0.01)

        await asyncio.wait_for(started(), 15)
        await asyncio.wait_for(client.close(), 5)
        assert ended.exists(), "the child must observe EOF before its process tree is ended"
        assert client._proc.returncode is not None
    finally:
        await client.close()


@pytest.mark.skipif(os.name == "nt", reason="a process group to end")
async def test_the_grace_ends_when_the_agent_does_though_a_descendant_still_holds_its_stdout(monkeypatch):
    monkeypatch.setattr(acp_client, "_TEARDOWN_GRACE_S", 5.0)
    child = (
        "import subprocess, sys\n"
        "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])\n"
        "sys.stdin.buffer.read()\n"
    )
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        child,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
    )
    endpoint = acp_client._ProcessEndpoint(proc, proc.pid)
    try:
        started = asyncio.get_running_loop().time()
        await endpoint.end()
        assert proc.returncode == 0
        assert asyncio.get_running_loop().time() - started < 2.0
    finally:
        endpoint.release()


@pytest.mark.skipif(os.name == "nt", reason="a process group to end")
async def test_a_close_cancelled_during_the_grace_still_lets_go_of_what_it_held():
    child = (
        "import json, sys, threading\n"
        "ask = {'jsonrpc': '2.0', 'id': 1, 'method': 'session/request_permission', 'params': {'sessionId': 's'}}\n"
        "sys.stdout.write(json.dumps(ask) + '\\n')\n"
        "sys.stdout.flush()\n"
        "sys.stdin.buffer.read()\n"
        "threading.Event().wait()\n"
    )
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        child,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
    )
    asked = asyncio.Event()

    async def answer(method, params):
        asked.set()
        await asyncio.Event().wait()

    endpoint = acp_client._ProcessEndpoint(proc, proc.pid)
    released = []
    release = endpoint.release
    endpoint.release = lambda: (released.append(True), release())  # type: ignore[method-assign]
    client = acp_client.AcpClient(name="cancelled-close", endpoint=endpoint, on_request=answer)
    client._start_loops()
    await asyncio.wait_for(asked.wait(), 10)
    answering = list(client._answer_tasks)
    closing = asyncio.create_task(client.close())
    await asyncio.sleep(0.3)
    closing.cancel()
    with pytest.raises(asyncio.CancelledError):
        await closing
    await asyncio.sleep(0)
    assert released and answering and all(task.done() for task in answering)
    assert client.closed
