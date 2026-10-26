"""Windows job ownership, launch ordering, and cleanup when setup cannot finish."""

from __future__ import annotations

import asyncio
import ctypes
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from raven.acp_client import client as acp_client
from raven.acp_client import windows


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
    # Kill-on-close, and leaving allowed to a process that asks to.
    assert flags == [0x2000 | 0x0800]
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
