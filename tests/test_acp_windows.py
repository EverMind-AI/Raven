"""Windows job ownership, launch ordering, and cleanup when setup cannot finish."""

from __future__ import annotations

import asyncio
import ctypes
import os
import shutil
import sys
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from raven.acp_client import client as acp_client
from raven.acp_client import windows


def _git_bash() -> str | None:
    found = shutil.which("bash")
    if found:
        return found
    for candidate in (r"C:\Program Files\Git\usr\bin\bash.exe", r"C:\Program Files\Git\bin\bash.exe"):
        if os.path.isfile(candidate):
            return candidate
    return None


GIT_BASH = _git_bash()


@pytest.fixture
def api(monkeypatch):
    kernel = SimpleNamespace(
        CreateJobObjectW=Mock(return_value=100),
        SetInformationJobObject=Mock(return_value=1),
        OpenProcess=Mock(return_value=200),
        AssignProcessToJobObject=Mock(return_value=1),
        CloseHandle=Mock(return_value=1),
    )
    monkeypatch.setattr(windows, "_kernel32", lambda: kernel)
    monkeypatch.setattr(windows, "_win_error", lambda: OSError("job operation failed"))
    return kernel


def test_the_host_is_the_only_owner_and_closing_its_handle_ends_the_job(api):
    flags = []

    def set_limits(handle, kind, pointer, size):
        limits = ctypes.cast(pointer, ctypes.POINTER(windows._ExtendedLimits)).contents
        flags.append(limits.BasicLimitInformation.LimitFlags)
        return 1

    api.SetInformationJobObject.side_effect = set_limits
    job = windows.JobObject()
    api.CreateJobObjectW.assert_called_once_with(None, None)
    # Kill-on-close, and nothing may leave: Git for Windows' runtime spends a
    # breakaway permission on every native child, not only on work asked to go.
    assert flags == [0x2000]
    job.assign(123)
    api.OpenProcess.assert_called_once_with(0x0101, False, 123)
    api.AssignProcessToJobObject.assert_called_once_with(100, 200)
    job.close()
    job.close()
    assert [call.args[0] for call in api.CloseHandle.call_args_list] == [200, 100]


@pytest.mark.parametrize(
    "failure", ["CreateJobObjectW", "SetInformationJobObject", "OpenProcess", "AssignProcessToJobObject"]
)
def test_a_failed_native_operation_releases_every_acquired_handle(api, failure):
    getattr(api, failure).return_value = 0
    job = None
    try:
        with pytest.raises(OSError, match="job operation failed"):
            job = windows.JobObject()
            job.assign(123)
    finally:
        if job is not None:
            job.close()
    expected = {
        "CreateJobObjectW": [],
        "SetInformationJobObject": [100],
        "OpenProcess": [100],
        "AssignProcessToJobObject": [200, 100],
    }
    assert [call.args[0] for call in api.CloseHandle.call_args_list] == expected[failure]


def test_a_failed_close_retains_the_handle_for_retry(api):
    job = windows.JobObject()
    api.CloseHandle.side_effect = [0, 1]
    with pytest.raises(OSError, match="job operation failed"):
        job.close()
    job.close()
    assert api.CloseHandle.call_count == 2


@pytest.fixture
def launcher(tmp_path, monkeypatch):
    program = tmp_path / "agent.exe"
    program.touch()
    events = []
    job = SimpleNamespace(
        assign=Mock(side_effect=lambda pid: events.append("assigned")),
        close=Mock(side_effect=lambda: events.append("closed")),
    )
    proc = SimpleNamespace(
        pid=123,
        stdin=SimpleNamespace(write=Mock(side_effect=lambda data: events.append("released")), drain=AsyncMock()),
        kill=Mock(),
        _transport=SimpleNamespace(close=Mock()),
    )
    spawn = AsyncMock(return_value=proc)
    monkeypatch.setattr(windows, "JobObject", lambda: job)
    monkeypatch.setattr(windows.subprocess, "CREATE_NO_WINDOW", 0x08000000, raising=False)
    monkeypatch.setattr(windows.asyncio, "create_subprocess_exec", spawn)
    return SimpleNamespace(argv=[str(program), "argument with spaces"], job=job, proc=proc, spawn=spawn, events=events)


async def test_no_agent_can_start_until_its_job_is_assigned(launcher):
    proc, job = await windows.launch(launcher.argv, stdin=-1, stdout=-1, stderr=-1, limit=8388608)
    assert (proc, job) == (launcher.proc, launcher.job)
    assert launcher.events == ["assigned", "released"]
    assert launcher.spawn.call_args.args[5:] == tuple(launcher.argv)
    assert launcher.spawn.call_args.kwargs["limit"] == 8388608
    launcher.job.close.assert_not_called()


@pytest.mark.parametrize("failure", [OSError, asyncio.CancelledError])
async def test_a_spawn_that_fails_or_is_cancelled_closes_its_job(launcher, failure):
    launcher.spawn.side_effect = failure
    with pytest.raises(failure):
        await windows.launch(launcher.argv)
    assert launcher.events == ["closed"]


@pytest.mark.parametrize("stage", ["assign", "write", "drain", "missing_stdin"])
@pytest.mark.parametrize("failure", [OSError, asyncio.CancelledError])
async def test_an_unfinished_launch_ends_its_job_and_releases_its_transport(launcher, stage, failure):
    if stage == "assign":
        launcher.job.assign.side_effect = failure
    elif stage == "missing_stdin":
        launcher.proc.stdin = None
        failure = ValueError
    else:
        getattr(launcher.proc.stdin, stage).side_effect = failure
    with pytest.raises(failure):
        await windows.launch(launcher.argv)
    launcher.job.close.assert_called_once()
    launcher.proc.kill.assert_called_once()
    launcher.proc._transport.close.assert_called_once()
    if stage == "assign":
        launcher.proc.stdin.write.assert_not_called()


async def test_launch_cleanup_still_closes_the_transport_after_the_process_exits(launcher):
    launcher.job.assign.side_effect = OSError
    launcher.proc.kill.side_effect = ProcessLookupError
    with pytest.raises(OSError):
        await windows.launch(launcher.argv)
    launcher.proc._transport.close.assert_called_once()


async def test_an_exited_launcher_does_not_prevent_ending_its_job(monkeypatch):
    proc = SimpleNamespace(pid=123, returncode=0)
    job = SimpleNamespace(close=Mock())
    fallback = AsyncMock()
    monkeypatch.setattr(acp_client, "_end", fallback)
    endpoint = acp_client._ProcessEndpoint(proc, 123, job)
    await endpoint.end()
    endpoint.release()
    assert job.close.call_count == 2
    fallback.assert_not_awaited()


def test_a_missing_program_is_rejected_before_starting_a_launcher(tmp_path):
    with pytest.raises(FileNotFoundError):
        windows._program([str(tmp_path / "absent.exe")], None)


def test_a_relative_program_uses_the_agents_working_directory(tmp_path):
    program = tmp_path / "agent.exe"
    program.touch()
    assert windows._program(["./agent", "arg"], str(tmp_path)) == [str(program), "arg"]


_CHILD = """\
import os, sys, time

started, escaped = sys.argv[1], sys.argv[2]
with open(started, "w") as fh:
    fh.write(str(os.getpid()))
time.sleep(3)
with open(escaped, "w") as fh:
    fh.write("escaped")
"""


def _is_in_a_job(pid: int) -> bool:
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = ctypes.c_void_p
    kernel32.IsProcessInJob.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(wintypes.BOOL)]
    kernel32.IsProcessInJob.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel32.CloseHandle.restype = wintypes.BOOL

    handle = kernel32.OpenProcess(0x1000, False, pid)
    if not handle:
        return False
    try:
        inside = wintypes.BOOL(False)
        answered = kernel32.IsProcessInJob(handle, None, ctypes.byref(inside))
        return bool(answered) and bool(inside.value)
    finally:
        kernel32.CloseHandle(handle)


@pytest.mark.skipif(sys.platform != "win32" or not GIT_BASH, reason="job objects and Git Bash are Windows'")
def test_a_git_bash_descendant_stays_in_the_agents_job(tmp_path):
    # Git for Windows' MSYS2 runtime adds CREATE_BREAKAWAY_FROM_JOB to every
    # native program it starts when the job it is in allows breakaway, so a
    # breakaway-capable job leaks ordinary Bash tool commands. Nothing the
    # agent's job holds may leave it; a handed-off job starts outside it.
    child = tmp_path / "child.py"
    child.write_text(_CHILD, encoding="utf-8")
    started, escaped = tmp_path / "started", tmp_path / "escaped"
    argv = [GIT_BASH, "-c", f"'{sys.executable}' '{child}' '{started}' '{escaped}'"]

    async def run() -> bool:
        proc, job = await windows.launch(argv, stdin=-1, stdout=-1, stderr=-1)
        try:
            deadline = time.time() + 30
            while not started.exists() and time.time() < deadline:
                await asyncio.sleep(0.1)
            assert started.exists(), "the Git Bash child never started"
            inside = _is_in_a_job(int(started.read_text()))
            job.close()
            await asyncio.wait_for(proc.wait(), 15)
            return inside
        finally:
            job.close()
            if proc.returncode is None:
                proc.kill()
                await proc.wait()

    assert asyncio.run(run()) is True
    deadline = time.time() + 6
    while not escaped.exists() and time.time() < deadline:
        time.sleep(0.1)
    assert not escaped.exists(), "a Git Bash child left the agent's job and outlived it"


def test_the_handoff_server_names_its_pipe_and_publishes_its_key(monkeypatch):
    import multiprocessing.connection

    listeners = []
    threads = []

    class FakeListener:
        def __init__(self, address, family=None, authkey=None):
            self.address, self.family, self.authkey = address, family, authkey
            listeners.append(self)

    class FakeThread:
        def __init__(self, target, name=None, daemon=None):
            self.target, self.name, self.daemon = target, name, daemon
            threads.append(self)

        def start(self):
            pass

    monkeypatch.setattr(multiprocessing.connection, "Listener", FakeListener)
    monkeypatch.setattr(windows.threading, "Thread", FakeThread)
    server = windows._HandoffServer()
    assert listeners[0].address.startswith("\\\\.\\pipe\\raven-acp-handoff-")
    assert listeners[0].family == "AF_PIPE"
    assert len(listeners[0].authkey) == 32
    assert server.env[windows.HANDOFF_PIPE] == listeners[0].address
    assert server.env[windows.HANDOFF_KEY] == listeners[0].authkey.hex()
    assert threads[0].daemon is True
    assert threads[0].name == "raven-acp-handoff"


def test_the_handoff_server_serves_each_connection_then_stops(monkeypatch):
    connection = SimpleNamespace()
    listener = SimpleNamespace(accept=Mock(side_effect=[ValueError("bad key"), connection, OSError("closed")]))
    served = []

    class FakeThread:
        def __init__(self, target, args=(), name=None, daemon=None):
            self.target, self.args, self.daemon = target, args, daemon

        def start(self):
            served.append(self.args)

    monkeypatch.setattr(windows.threading, "Thread", FakeThread)
    server = object.__new__(windows._HandoffServer)
    server._listener = listener
    assert server._accept() is None
    assert served == [(connection,)]
    assert listener.accept.call_count == 3


def test_the_handoff_server_runs_the_relay_with_the_requested_environment(monkeypatch):
    # POSIX has no CREATE_NO_WINDOW; deleting it here is the shape the Ubuntu
    # shards run, and the call must still reach the mocked run().
    monkeypatch.delattr(windows.subprocess, "CREATE_NO_WINDOW", raising=False)
    completed = SimpleNamespace(returncode=0, stdout="4242\n", stderr="")
    run = Mock(return_value=completed)
    monkeypatch.setattr(windows.subprocess, "run", run)
    sent = []
    connection = SimpleNamespace(
        recv=Mock(return_value={"argv": ["py", "-c", "relay"], "cwd": "job", "env": {"KEEP": "1"}}),
        send=Mock(side_effect=sent.append),
        close=Mock(),
    )
    server = object.__new__(windows._HandoffServer)
    server._serve(connection)
    assert run.call_args.args[0] == ["py", "-c", "relay"]
    assert run.call_args.kwargs["cwd"] == "job"
    assert run.call_args.kwargs["env"] == {"KEEP": "1"}
    assert sent == [{"returncode": 0, "stdout": "4242\n", "stderr": ""}]
    connection.close.assert_called_once()


def test_a_relay_the_host_cannot_start_is_answered_with_the_error(monkeypatch):
    monkeypatch.delattr(windows.subprocess, "CREATE_NO_WINDOW", raising=False)
    monkeypatch.setattr(windows.subprocess, "run", Mock(side_effect=OSError("no interpreter")))
    sent = []
    connection = SimpleNamespace(
        recv=Mock(return_value={"argv": ["py"], "cwd": None, "env": None}),
        send=Mock(side_effect=sent.append),
        close=Mock(),
    )
    server = object.__new__(windows._HandoffServer)
    server._serve(connection)
    assert "error" in sent[0]
    assert "no interpreter" in sent[0]["error"]
    connection.close.assert_called_once()


def test_a_connection_that_cannot_be_answered_is_closed_quietly():
    connection = SimpleNamespace(
        recv=Mock(side_effect=EOFError("gone")),
        send=Mock(side_effect=OSError("gone")),
        close=Mock(side_effect=OSError("gone")),
    )
    server = object.__new__(windows._HandoffServer)
    server._serve(connection)
    connection.send.assert_called_once()
    connection.close.assert_called_once()


def test_the_host_starts_one_handoff_server_and_reuses_it(monkeypatch):
    monkeypatch.setattr(windows, "_IS_WINDOWS", True)
    monkeypatch.setattr(windows, "_HANDOFF", None)
    monkeypatch.setattr(windows, "_HANDOFF_TRIED", False)
    server = SimpleNamespace(env={windows.HANDOFF_PIPE: "pipe", windows.HANDOFF_KEY: "key"})
    made = []
    monkeypatch.setattr(windows, "_HandoffServer", lambda: made.append(server) or server)
    assert windows._handoff() is server
    assert windows._handoff() is server
    assert made == [server]


def test_a_host_that_cannot_open_a_handoff_channel_returns_none(monkeypatch):
    monkeypatch.setattr(windows, "_IS_WINDOWS", True)
    monkeypatch.setattr(windows, "_HANDOFF", None)
    monkeypatch.setattr(windows, "_HANDOFF_TRIED", False)
    monkeypatch.setattr(windows, "_HandoffServer", Mock(side_effect=OSError("no pipe")))
    assert windows._handoff() is None
    assert windows._handoff() is None
    windows._HandoffServer.assert_called_once()


async def test_launch_gives_the_agent_the_hosts_handoff_channel(launcher, monkeypatch):
    channel = {windows.HANDOFF_PIPE: "pipe", windows.HANDOFF_KEY: "key"}
    monkeypatch.setattr(windows, "_handoff", lambda: SimpleNamespace(env=channel))
    await windows.launch(launcher.argv, env={"KEEP": "1"})
    assert launcher.spawn.call_args.kwargs["env"] == {"KEEP": "1", **channel}
