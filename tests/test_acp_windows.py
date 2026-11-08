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
    for candidate in (r"C:\Program Files\Git\usr\bin\bash.exe", r"C:\Program Files\Git\bin\bash.exe"):
        if os.path.isfile(candidate):
            return candidate
    found = shutil.which("bash")
    if found and not found.lower().startswith(
        os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32").lower()
    ):
        return found
    return None


GIT_BASH = _git_bash()


def test_the_git_bash_probe_prefers_gits_install_over_a_wsl_bash(monkeypatch):
    import shutil

    git = r"C:\Program Files\Git\bin\bash.exe"
    monkeypatch.setattr(shutil, "which", lambda name: r"C:\Windows\System32\bash.exe")
    monkeypatch.setattr(os.path, "isfile", lambda path: path == git)
    assert _git_bash() == git


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


def _handoff_exchange(server, timeout=5.0):
    """One real request through the pipe; returns (finished, result)."""
    import json
    import threading
    from multiprocessing.connection import Client

    result = []

    def call():
        try:
            key = bytes.fromhex(server.env[windows.HANDOFF_KEY])
            connection = Client(server.address, family="AF_PIPE", authkey=key)
            try:
                connection.send_bytes(
                    json.dumps(
                        {
                            "argv": [sys.executable, "-c", "print('ok')"],
                            "cwd": os.getcwd(),
                            "env": dict(os.environ),
                        }
                    ).encode("utf-8")
                )
                result.append(json.loads(connection.recv_bytes().decode("utf-8")))
            finally:
                connection.close()
        except BaseException as exc:  # noqa: BLE001 - the probe reports everything
            result.append(exc)

    thread = threading.Thread(target=call, daemon=True)
    thread.start()
    thread.join(timeout)
    return not thread.is_alive(), result


@pytest.mark.skipif(sys.platform != "win32", reason="named pipes are Windows'")
def test_a_client_that_aborts_the_handshake_does_not_stop_the_server():
    server = windows._HandoffServer()
    try:
        time.sleep(0.2)
        os.close(os.open(server.address, os.O_RDWR))
        finished, result = _handoff_exchange(server)
        assert finished, "a client that aborted the handshake stopped the accept loop"
        assert result == [{"returncode": 0, "stdout": "ok\n", "stderr": ""}]
    finally:
        close = getattr(server, "close", None)
        if close is not None:
            close()


@pytest.mark.skipif(sys.platform != "win32", reason="named pipes are Windows'")
def test_a_client_that_stalls_the_handshake_does_not_stop_the_server():
    server = windows._HandoffServer()
    held = None
    try:
        time.sleep(0.2)
        held = os.open(server.address, os.O_RDWR)
        time.sleep(0.5)
        finished, result = _handoff_exchange(server)
        assert finished, "a stalled handshake stopped the accept loop"
        assert result == [{"returncode": 0, "stdout": "ok\n", "stderr": ""}]
    finally:
        if held is not None:
            os.close(held)
        close = getattr(server, "close", None)
        if close is not None:
            close()


def test_the_handoff_server_names_its_pipe_and_publishes_its_key(monkeypatch):
    import multiprocessing.connection

    listeners = []
    threads = []

    class FakePipeListener:
        def __init__(self, address, backlog=None):
            self.address = address
            listeners.append(self)

        def close(self):
            pass

    class FakeThread:
        def __init__(self, target, args=(), name=None, daemon=None):
            self.target, self.args, self.name, self.daemon = target, args, name, daemon
            threads.append(self)

        def start(self):
            pass

        def is_alive(self):
            return True

    monkeypatch.setattr(multiprocessing.connection, "PipeListener", FakePipeListener, raising=False)
    monkeypatch.setattr(windows.threading, "Thread", FakeThread)
    server = windows._HandoffServer()
    assert listeners[0].address.startswith("\\\\.\\pipe\\raven-acp-handoff-")
    assert len(server._authkey) == 32
    assert server.env[windows.HANDOFF_PIPE] == listeners[0].address
    assert server.env[windows.HANDOFF_KEY] == server._authkey.hex()
    assert threads[0].daemon is True
    assert threads[0].name == "raven-acp-handoff"
    assert server.alive() is True


def test_the_handoff_server_survives_a_bad_client_then_stops_when_closed(monkeypatch):
    import threading

    connection = SimpleNamespace()
    state = {"calls": 0}
    served = []

    def accept():
        state["calls"] += 1
        if state["calls"] == 1:
            raise EOFError("aborted handshake")
        if state["calls"] == 2:
            return connection
        server._closed.set()
        raise OSError("listener closed")

    class FakeThread:
        def __init__(self, target, args=(), name=None, daemon=None):
            self.target, self.args, self.daemon = target, args, daemon

        def start(self):
            served.append(self.args)

    server = object.__new__(windows._HandoffServer)
    server._listener = SimpleNamespace(accept=Mock(side_effect=accept))
    server._closed = threading.Event()
    server._handshake_slots = threading.BoundedSemaphore(2)
    monkeypatch.setattr(windows.threading, "Thread", FakeThread)
    assert server._accept() is None
    assert served == [(connection,)]
    assert state["calls"] == 3


def test_the_handoff_server_runs_the_relay_with_the_requested_environment(monkeypatch):
    # POSIX has no CREATE_NO_WINDOW; deleting it here is the shape the Ubuntu
    # shards run, and the call must still reach the mocked run().
    import json

    monkeypatch.delattr(windows.subprocess, "CREATE_NO_WINDOW", raising=False)
    completed = SimpleNamespace(returncode=0, stdout="4242\n", stderr="")
    run = Mock(return_value=completed)
    monkeypatch.setattr(windows.subprocess, "run", run)
    sent = []
    poll = Mock(return_value=True)
    request = {"argv": ["py", "-c", "relay"], "cwd": "job", "env": {"KEEP": "1"}}
    connection = SimpleNamespace(
        poll=poll,
        recv=Mock(side_effect=AssertionError("the request must not be unpickled")),
        recv_bytes=Mock(return_value=json.dumps(request).encode("utf-8")),
        send=Mock(side_effect=AssertionError("the answer must not be pickled")),
        send_bytes=Mock(side_effect=sent.append),
        close=Mock(),
    )
    server = object.__new__(windows._HandoffServer)
    server._serve(connection)
    assert poll.call_args.args[0] == windows._REQUEST_TIMEOUT_S
    assert run.call_args.args[0] == ["py", "-c", "relay"]
    assert run.call_args.kwargs["cwd"] == "job"
    assert run.call_args.kwargs["env"] == {"KEEP": "1"}
    assert run.call_args.kwargs["timeout"] == windows._RELAY_TIMEOUT_S
    assert json.loads(sent[0].decode("utf-8")) == {"returncode": 0, "stdout": "4242\n", "stderr": ""}
    connection.recv.assert_not_called()
    connection.send.assert_not_called()
    connection.close.assert_called_once()


def test_a_relay_the_host_cannot_start_is_answered_with_the_error(monkeypatch):
    import json

    monkeypatch.delattr(windows.subprocess, "CREATE_NO_WINDOW", raising=False)
    monkeypatch.setattr(windows.subprocess, "run", Mock(side_effect=OSError("no interpreter")))
    sent = []
    connection = SimpleNamespace(
        poll=Mock(return_value=True),
        recv_bytes=Mock(return_value=json.dumps({"argv": ["py"], "cwd": None, "env": {}}).encode("utf-8")),
        send_bytes=Mock(side_effect=sent.append),
        recv=Mock(side_effect=AssertionError("the request must not be unpickled")),
        send=Mock(side_effect=AssertionError("the answer must not be pickled")),
        close=Mock(),
    )
    server = object.__new__(windows._HandoffServer)
    server._serve(connection)
    assert connection.poll.called
    answer = json.loads(sent[0].decode("utf-8"))
    assert "error" in answer
    assert "no interpreter" in answer["error"]
    connection.close.assert_called_once()


def test_the_handoff_server_does_not_inherit_the_host_environment_for_an_empty_request(monkeypatch):
    import json

    monkeypatch.delattr(windows.subprocess, "CREATE_NO_WINDOW", raising=False)
    run = Mock(return_value=SimpleNamespace(returncode=0, stdout="", stderr=""))
    monkeypatch.setattr(windows.subprocess, "run", run)
    connection = SimpleNamespace(
        poll=Mock(return_value=True),
        recv_bytes=Mock(return_value=json.dumps({"argv": ["py"], "cwd": None, "env": {}}).encode("utf-8")),
        send_bytes=Mock(),
        recv=Mock(side_effect=AssertionError("the request must not be unpickled")),
        send=Mock(side_effect=AssertionError("the answer must not be pickled")),
        close=Mock(),
    )
    server = object.__new__(windows._HandoffServer)
    server._serve(connection)
    assert run.call_args.kwargs["env"] == {}


def test_an_oversized_request_is_refused_by_the_stdlib_length_check():
    import json

    sent = []
    connection = SimpleNamespace(
        poll=Mock(return_value=True),
        # Connection.recv_bytes(maxlength) raises this when the message is too
        # large; it does not return None (CPython _bad_message_length).
        recv_bytes=Mock(side_effect=OSError("bad message length")),
        send_bytes=Mock(side_effect=sent.append),
        recv=Mock(side_effect=AssertionError("the request must not be unpickled")),
        send=Mock(side_effect=AssertionError("the answer must not be pickled")),
        close=Mock(),
    )
    server = object.__new__(windows._HandoffServer)
    server._serve(connection)
    assert "bad message length" in json.loads(sent[0].decode("utf-8"))["error"]


def test_a_connection_that_cannot_be_answered_is_closed_quietly():
    connection = SimpleNamespace(
        poll=Mock(return_value=True),
        recv_bytes=Mock(side_effect=EOFError("gone")),
        send_bytes=Mock(side_effect=OSError("gone")),
        recv=Mock(side_effect=AssertionError("the request must not be unpickled")),
        send=Mock(side_effect=AssertionError("the answer must not be pickled")),
        close=Mock(side_effect=OSError("gone")),
    )
    server = object.__new__(windows._HandoffServer)
    server._serve(connection)
    assert connection.poll.called
    connection.send_bytes.assert_called_once()
    connection.recv.assert_not_called()
    connection.close.assert_called_once()


def test_the_host_starts_one_handoff_server_and_reuses_it(monkeypatch):
    monkeypatch.setattr(windows, "_IS_WINDOWS", True)
    monkeypatch.setattr(windows, "_HANDOFF", None)
    server = SimpleNamespace(env={windows.HANDOFF_PIPE: "pipe", windows.HANDOFF_KEY: "key"}, alive=lambda: True)
    made = []
    monkeypatch.setattr(windows, "_HandoffServer", lambda: made.append(server) or server)
    assert windows._handoff() is server
    assert windows._handoff() is server
    assert made == [server]


def test_a_host_that_cannot_open_a_handoff_channel_retries(monkeypatch):
    monkeypatch.setattr(windows, "_IS_WINDOWS", True)
    monkeypatch.setattr(windows, "_HANDOFF", None)
    server = SimpleNamespace(alive=lambda: True)
    attempts = Mock(side_effect=[OSError("no pipe"), server])
    monkeypatch.setattr(windows, "_HandoffServer", attempts)
    assert windows._handoff() is None
    assert windows._handoff() is server
    assert attempts.call_count == 2


def test_a_dead_handoff_server_is_replaced(monkeypatch):
    monkeypatch.setattr(windows, "_IS_WINDOWS", True)
    old = SimpleNamespace(alive=lambda: False, close=Mock())
    new = SimpleNamespace(alive=lambda: True)
    monkeypatch.setattr(windows, "_HANDOFF", old)
    monkeypatch.setattr(windows, "_HandoffServer", lambda: new)
    assert windows._handoff() is new
    old.close.assert_called_once()


def test_the_handoff_server_continues_after_an_unknown_accept_error(monkeypatch):
    import threading

    connection = SimpleNamespace()
    state = {"calls": 0}
    served = []

    def accept():
        state["calls"] += 1
        if state["calls"] == 1:
            raise ValueError("bad client")
        if state["calls"] == 2:
            return connection
        server._closed.set()
        raise OSError("closed")

    class FakeThread:
        def __init__(self, target, args=(), name=None, daemon=None):
            self.target, self.args, self.daemon = target, args, daemon

        def start(self):
            served.append(self.args)

    server = object.__new__(windows._HandoffServer)
    server._listener = SimpleNamespace(accept=Mock(side_effect=accept))
    server._closed = threading.Event()
    server._handshake_slots = threading.BoundedSemaphore(2)
    monkeypatch.setattr(windows.threading, "Thread", FakeThread)
    assert server._accept() is None
    assert served == [(connection,)]
    assert state["calls"] == 3


def test_the_handoff_server_closes_a_connection_when_its_slots_are_taken():
    import threading

    connection = SimpleNamespace(close=Mock())
    state = {"calls": 0}

    def accept():
        state["calls"] += 1
        if state["calls"] == 1:
            return connection
        server._closed.set()
        raise OSError("closed")

    server = object.__new__(windows._HandoffServer)
    server._listener = SimpleNamespace(accept=Mock(side_effect=accept))
    server._closed = threading.Event()
    server._handshake_slots = threading.BoundedSemaphore(1)
    server._handshake_slots.acquire()
    assert server._accept() is None
    connection.close.assert_called_once()


def test_the_handoff_server_releases_a_slot_when_it_cannot_start_a_worker(monkeypatch):
    import threading

    connection = SimpleNamespace(close=Mock())
    state = {"calls": 0}

    def accept():
        state["calls"] += 1
        if state["calls"] == 1:
            return connection
        server._closed.set()
        raise OSError("closed")

    class FailingThread:
        def __init__(self, *args, **kwargs):
            pass

        def start(self):
            raise RuntimeError("no thread")

    server = object.__new__(windows._HandoffServer)
    server._listener = SimpleNamespace(accept=Mock(side_effect=accept))
    server._closed = threading.Event()
    server._handshake_slots = threading.BoundedSemaphore(1)
    monkeypatch.setattr(windows.threading, "Thread", FailingThread)
    assert server._accept() is None
    connection.close.assert_called_once()
    assert server._handshake_slots.acquire(blocking=False)
    server._handshake_slots.release()


def test_the_handoff_server_authenticates_the_client_then_serves_it(monkeypatch):
    import multiprocessing.connection
    import threading

    calls = []
    connection = SimpleNamespace(close=Mock())
    server = object.__new__(windows._HandoffServer)
    server._authkey = b"key"
    server._handshake_slots = threading.BoundedSemaphore(1)
    server._handshake_slots.acquire()
    monkeypatch.setattr(
        multiprocessing.connection, "deliver_challenge", lambda conn, key: calls.append(("deliver", conn, key))
    )
    monkeypatch.setattr(
        multiprocessing.connection, "answer_challenge", lambda conn, key: calls.append(("answer", conn, key))
    )
    monkeypatch.setattr(server, "_serve", lambda conn: calls.append(("serve", conn)))
    server._handshake(connection)
    assert calls == [("deliver", connection, b"key"), ("answer", connection, b"key"), ("serve", connection)]
    connection.close.assert_not_called()
    assert server._handshake_slots.acquire(blocking=False)
    server._handshake_slots.release()


def test_a_failed_handshake_closes_its_connection_and_releases_the_slot(monkeypatch):
    import multiprocessing.connection
    import threading

    connection = SimpleNamespace(close=Mock())
    server = object.__new__(windows._HandoffServer)
    server._authkey = b"key"
    server._handshake_slots = threading.BoundedSemaphore(1)
    server._handshake_slots.acquire()
    monkeypatch.setattr(multiprocessing.connection, "deliver_challenge", Mock(side_effect=OSError("bad key")))
    monkeypatch.setattr(multiprocessing.connection, "answer_challenge", Mock())
    monkeypatch.setattr(server, "_serve", Mock())
    server._handshake(connection)
    connection.close.assert_called_once()
    assert server._handshake_slots.acquire(blocking=False)
    server._handshake_slots.release()


def test_closing_the_handoff_server_stops_accepting():
    import threading

    closing = []
    server = object.__new__(windows._HandoffServer)
    server._closed = threading.Event()
    server._listener = SimpleNamespace(close=lambda: closing.append(True))
    server.close()
    assert server._closed.is_set()
    assert closing == [True]

    server = object.__new__(windows._HandoffServer)
    server._closed = threading.Event()
    server._listener = SimpleNamespace(close=Mock(side_effect=OSError("gone")))
    server.close()
    assert server._closed.is_set()


def test_a_handoff_request_that_does_not_arrive_is_answered_with_the_error():
    import json

    sent = []
    connection = SimpleNamespace(
        poll=Mock(return_value=False),
        recv_bytes=Mock(),
        send_bytes=Mock(side_effect=sent.append),
        close=Mock(),
    )
    server = object.__new__(windows._HandoffServer)
    server._serve(connection)
    assert "did not arrive" in json.loads(sent[0].decode("utf-8"))["error"]
    connection.recv_bytes.assert_not_called()


def test_a_handoff_request_that_is_not_an_object_is_refused(monkeypatch):
    import json

    monkeypatch.setattr(windows.subprocess, "run", Mock())
    sent = []
    connection = SimpleNamespace(
        poll=Mock(return_value=True),
        recv_bytes=Mock(return_value=b"[]"),
        send_bytes=Mock(side_effect=sent.append),
        close=Mock(),
    )
    server = object.__new__(windows._HandoffServer)
    server._serve(connection)
    assert "must be an object" in json.loads(sent[0].decode("utf-8"))["error"]
    windows.subprocess.run.assert_not_called()


def test_a_handoff_request_whose_env_is_not_an_object_is_refused(monkeypatch):
    import json

    monkeypatch.setattr(windows.subprocess, "run", Mock())
    sent = []
    connection = SimpleNamespace(
        poll=Mock(return_value=True),
        recv_bytes=Mock(return_value=json.dumps({"argv": ["py"], "cwd": None, "env": []}).encode("utf-8")),
        send_bytes=Mock(side_effect=sent.append),
        close=Mock(),
    )
    server = object.__new__(windows._HandoffServer)
    server._serve(connection)
    assert "env must be an object" in json.loads(sent[0].decode("utf-8"))["error"]
    windows.subprocess.run.assert_not_called()


async def test_launch_gives_the_agent_the_hosts_handoff_channel(launcher, monkeypatch):
    channel = {windows.HANDOFF_PIPE: "pipe", windows.HANDOFF_KEY: "key"}
    monkeypatch.setattr(windows, "_handoff", lambda: SimpleNamespace(env=channel))
    await windows.launch(launcher.argv, env={"KEEP": "1"})
    assert launcher.spawn.call_args.kwargs["env"] == {"KEEP": "1", **channel}


def _pipe_dacl_sddl(handle):
    from ctypes import wintypes

    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    advapi32.GetSecurityInfo.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        wintypes.DWORD,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_void_p),
    ]
    advapi32.GetSecurityInfo.restype = wintypes.DWORD
    advapi32.ConvertSecurityDescriptorToStringSecurityDescriptorW.argtypes = [
        ctypes.c_void_p,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.LPWSTR),
        ctypes.POINTER(wintypes.ULONG),
    ]
    advapi32.ConvertSecurityDescriptorToStringSecurityDescriptorW.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p

    descriptor = ctypes.c_void_p()
    dacl = ctypes.c_void_p()
    result = advapi32.GetSecurityInfo(
        wintypes.HANDLE(handle),
        6,  # SE_KERNEL_OBJECT
        0x00000004,  # DACL_SECURITY_INFORMATION
        None,
        None,
        ctypes.byref(dacl),
        None,
        ctypes.byref(descriptor),
    )
    assert result == 0, ctypes.WinError(result)
    text = wintypes.LPWSTR()
    length = wintypes.ULONG()
    try:
        assert advapi32.ConvertSecurityDescriptorToStringSecurityDescriptorW(
            descriptor, 1, 0x00000004, ctypes.byref(text), ctypes.byref(length)
        )
        return text.value
    finally:
        kernel32.LocalFree(descriptor)
        if text:
            kernel32.LocalFree(text)


@pytest.mark.skipif(sys.platform != "win32", reason="named pipes are Windows'")
def test_the_handoff_pipe_is_not_readable_by_other_users():
    server = windows._HandoffServer()
    try:
        time.sleep(0.2)
        sddl = _pipe_dacl_sddl(server._listener._handle_queue[0])
        assert ";;;WD)" not in sddl, sddl
        assert ";;;AN)" not in sddl, sddl
        assert ";;;SY)" in sddl, sddl
        assert ";;;OW)" in sddl, sddl
    finally:
        server.close()


def test_the_handoff_server_stops_after_repeated_accept_errors(monkeypatch):
    import threading

    calls = []
    sleeps = []

    def accept():
        calls.append(1)
        if len(calls) >= 50:
            server._closed.set()
        raise OSError("listener broken")

    server = object.__new__(windows._HandoffServer)
    server._listener = SimpleNamespace(accept=accept)
    server._closed = threading.Event()
    server._handshake_slots = threading.BoundedSemaphore(1)
    monkeypatch.setattr(windows.time, "sleep", sleeps.append)
    assert server._accept() is None
    assert len(calls) == windows._ACCEPT_FAILURE_LIMIT
    assert len(sleeps) == windows._ACCEPT_FAILURE_LIMIT - 1


@pytest.mark.skipif(sys.platform != "win32", reason="named pipes are Windows'")
def test_a_closed_handoff_server_serves_nothing_more():
    from multiprocessing.connection import PipeClient

    server = windows._HandoffServer()
    try:
        time.sleep(0.3)
        server.close()
        server._accept_thread.join(3)
        assert not server._accept_thread.is_alive()
        with pytest.raises(OSError):
            PipeClient(server.address)
    finally:
        server.close()
