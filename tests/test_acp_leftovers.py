"""The leftover-agent guard: the parent-death wrapper, the launch records and the sweep."""

from __future__ import annotations

import asyncio
import contextlib
import errno
import io
import json
import os
import re
import select
import shlex
import shutil
import signal
import subprocess
import sys
import threading
import time
import types
from collections.abc import Iterator
from pathlib import Path

import pytest
from loguru import logger

from raven.acp_client import leftovers
from raven.acp_client.client import AcpClient
from raven.acp_client.protocol import AcpConnectionError
from raven.agent.subagent.backends import env as backend_env

linux_only = pytest.mark.skipif(not sys.platform.startswith("linux"), reason="a Linux-only mechanism")


def _program(tmp_path: Path, name: str = "agent", mode: int = 0o755) -> Path:
    path = tmp_path / "bin" / name
    path.parent.mkdir(exist_ok=True)
    path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    path.chmod(mode)
    return path


@linux_only
def test_a_program_on_the_childs_path_starts_through_the_wrapper(tmp_path: Path) -> None:
    program = _program(tmp_path)
    argv = leftovers.bind_to_host(["agent", "--acp"], {"PATH": str(program.parent)}, None)
    assert argv[:4] == [sys.executable, "-I", "-S", "-c"]
    assert argv[5:] == [str(os.getpid()), str(program), "agent", "--acp"]


@linux_only
def test_a_relative_program_resolves_from_the_childs_cwd(tmp_path: Path) -> None:
    program = _program(tmp_path)
    assert leftovers.bind_to_host(["bin/agent"], {"PATH": ""}, str(tmp_path))[6] == str(program)


@linux_only
def test_an_absolute_program_is_wrapped_as_given(tmp_path: Path) -> None:
    program = _program(tmp_path)
    assert leftovers.bind_to_host([str(program)], {}, None)[6:] == [str(program), str(program)]


def test_a_missing_program_is_left_for_the_spawn_to_refuse(tmp_path: Path) -> None:
    argv = ["no-such-agent", "acp"]
    assert leftovers.bind_to_host(argv, {"PATH": str(tmp_path)}, None) == argv


def test_a_file_that_is_not_executable_is_not_wrapped(tmp_path: Path) -> None:
    program = _program(tmp_path, mode=0o644)
    assert leftovers.bind_to_host([str(program)], {}, None) == [str(program)]


def test_off_linux_nothing_is_wrapped(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    program = _program(tmp_path)
    monkeypatch.setattr(leftovers, "_on_linux", lambda: False)
    assert leftovers.bind_to_host([str(program)], {}, None) == [str(program)]


def test_without_an_interpreter_nothing_is_wrapped(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    program = _program(tmp_path)
    monkeypatch.setattr(leftovers.sys, "executable", "")
    assert leftovers.bind_to_host([str(program)], {}, None) == [str(program)]


_REPORT = """\
import ctypes, json, sys
sig = ctypes.c_int(-1)
ctypes.CDLL(None).prctl(2, ctypes.byref(sig), 0, 0, 0)
argv = open("/proc/self/cmdline", "rb").read().split(b"\\0")[:-1]
json.dump({"pdeathsig": sig.value, "argv": [a.decode() for a in argv]}, open(sys.argv[1], "w"))
"""


@linux_only
def test_the_wrapper_arms_the_signal_and_execs_the_agent_with_its_own_argv(tmp_path: Path) -> None:
    report, out = tmp_path / "report.py", tmp_path / "out.json"
    report.write_text(_REPORT, encoding="utf-8")
    argv = leftovers.bind_to_host([sys.executable, str(report), str(out)], {}, None)
    assert subprocess.run(argv, timeout=30).returncode == 0
    seen = json.loads(out.read_text(encoding="utf-8"))
    assert seen == {"pdeathsig": 9, "argv": [sys.executable, str(report), str(out)]}


@linux_only
def test_the_wrapper_starts_nothing_once_its_host_is_gone(tmp_path: Path) -> None:
    marker = tmp_path / "started"
    argv = leftovers.bind_to_host([sys.executable, "-c", f"open({str(marker)!r}, 'w').close()"], {}, None)
    # A host pid that is not this test's process, as if the host had died before the prctl.
    argv[5] = str(os.getpid() + 1)
    result = subprocess.run(argv, timeout=30, capture_output=True)
    assert (result.returncode, result.stderr) == (1, b"")
    assert not marker.exists()


def test_an_empty_argv_is_returned_as_it_is() -> None:
    assert leftovers.bind_to_host([], {}, None) == []


@linux_only
def test_without_a_path_the_lookup_falls_back_to_the_default() -> None:
    expected = [os.path.join(d, "sh") for d in os.defpath.split(os.pathsep) if d]
    assert leftovers.bind_to_host(["sh"], {}, None)[6] in expected


@linux_only
def test_a_relative_cwd_resolves_from_the_hosts_own_directory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    program = _program(tmp_path)
    monkeypatch.chdir(tmp_path.parent)
    assert leftovers.bind_to_host(["bin/agent"], {}, tmp_path.name)[6] == str(program)


@linux_only
def test_a_deleted_working_directory_leaves_a_relative_program_to_the_spawn(monkeypatch: pytest.MonkeyPatch) -> None:
    def gone() -> str:
        raise FileNotFoundError(2, "No such file or directory")

    monkeypatch.setattr(leftovers.os, "getcwd", gone)
    assert leftovers.bind_to_host(["bin/agent"], {}, None) == ["bin/agent"]


@linux_only
def test_an_absolute_program_is_wrapped_with_a_deleted_working_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    program = _program(tmp_path)

    def gone() -> str:
        raise FileNotFoundError(2, "No such file or directory")

    monkeypatch.setattr(leftovers.os, "getcwd", gone)
    assert leftovers.bind_to_host([str(program)], {}, None)[6] == str(program)


@linux_only
def test_a_bare_name_keeps_its_own_argv0_through_the_wrapper(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "agent-alias").symlink_to(shutil.which("sh"))
    out = tmp_path / "argv0"
    script = f'printf %s "$0" > {shlex.quote(str(out))}'
    argv = leftovers.bind_to_host(["agent-alias", "-c", script], {"PATH": str(bin_dir)}, None)
    assert subprocess.run(argv, timeout=30).returncode == 0
    assert out.read_text(encoding="utf-8") == "agent-alias"


@linux_only
def test_the_agent_gets_the_signal_defaults_a_plain_spawn_gives(tmp_path: Path) -> None:
    out = tmp_path / "status"
    script = f"grep '^SigIgn' /proc/self/status > {shlex.quote(str(out))}"
    argv = leftovers.bind_to_host([shutil.which("sh"), "-c", script], {}, None)
    assert subprocess.run(argv, timeout=30).returncode == 0
    ignored = int(out.read_text(encoding="utf-8").split()[1], 16)
    for signo in (signal.SIGPIPE, signal.SIGXFSZ):
        assert not ignored & (1 << (signo - 1)), f"{signo!r} reached the agent ignored"


@linux_only
def test_an_agent_that_cannot_be_executed_says_which_path_failed(tmp_path: Path) -> None:
    broken = tmp_path / "bin" / "broken"
    broken.parent.mkdir()
    broken.write_text("#!/nonexistent-interpreter-for-a-raven-test\n", encoding="utf-8")
    broken.chmod(0o755)
    result = subprocess.run(leftovers.bind_to_host([str(broken)], {}, None), timeout=30, capture_output=True, text=True)
    assert result.returncode == 127
    assert f"raven: cannot start {broken}" in result.stderr


@pytest.fixture
def records_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    directory = tmp_path / "acp-launches"
    directory.mkdir()
    monkeypatch.setattr(leftovers, "get_runtime_subdir", lambda name: directory)
    monkeypatch.setattr(leftovers, "_records", {})
    monkeypatch.setattr(leftovers, "_swept", False)
    monkeypatch.setattr(leftovers, "_sweep_done", threading.Event())
    monkeypatch.setattr(leftovers, "_warned", False)
    monkeypatch.setattr(leftovers, "_sweep_abandoned", False)
    return directory


def _own_start() -> int:
    start = leftovers._start_ticks(os.getpid())
    assert start is not None
    return start


def _dead_host() -> tuple[int, int]:
    # This process's pid under a start time it never had: a host that has died.
    return os.getpid(), _own_start() - 1


def _marked_env(host_pid: int, host_start: int) -> dict[str, str]:
    return {**os.environ, leftovers._MARK: f"{host_pid}-{host_start}"}


def _launch_group(host_pid: int, host_start: int) -> subprocess.Popen:
    """A real process group of its own, carrying the mark of the host named."""
    return subprocess.Popen(["sleep", "120"], start_new_session=True, env=_marked_env(host_pid, host_start))


def _end(proc: subprocess.Popen) -> None:
    # os.kill on the negative pgid rather than os.killpg, which some tests replace.
    with contextlib.suppress(ProcessLookupError):
        os.kill(-proc.pid, signal.SIGKILL)
    proc.wait(timeout=10)


@pytest.fixture
def group():
    """A group the dead host launched, ended whatever the test does."""
    proc = _launch_group(*_dead_host())
    yield proc
    _end(proc)


@pytest.fixture
def signalled(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """Every group the sweep would signal; none of them is."""
    calls: list[int] = []
    monkeypatch.setattr(leftovers.os, "killpg", lambda pgid, sig: calls.append(pgid))
    return calls


def _host_file(
    directory: Path,
    *,
    host_pid: int,
    host_start: int,
    groups: dict | list,
    boot_id=None,
    pid_ns=None,
    version: int | None = 1,
) -> Path:
    """A host's record; a ``version`` of None leaves that key out of it."""
    path = directory / f"{host_pid}-{host_start}.json"
    payload = {
        "version": version,
        "boot_id": leftovers._boot_id() if boot_id is None else boot_id,
        "pid_ns": leftovers._pid_namespace() if pid_ns is None else pid_ns,
        "host_pid": host_pid,
        "host_start": host_start,
        "groups": groups,
    }
    if version is None:
        del payload["version"]
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _entry(pid: int, *, start: int | None = None) -> dict:
    leader_start = leftovers._start_ticks(pid) if start is None else start
    return {str(pid): {"leader_start": leader_start, "agent": "stand-in", "launched_at": "2026-10-12T00:00:00+00:00"}}


def _dead_host_file(directory: Path, groups: dict, **overrides) -> Path:
    host_pid, host_start = _dead_host()
    return _host_file(directory, host_pid=host_pid, host_start=host_start, groups=groups, **overrides)


def _unreadable(monkeypatch: pytest.MonkeyPatch, pid: int) -> None:
    """Make reading ``pid``'s start time fail as it does once the host is out of file descriptors."""
    real = leftovers._start_ticks

    def out_of_fds(target: int) -> int | None:
        if target == pid:
            raise OSError(errno.EMFILE, "Too many open files")
        return real(target)

    monkeypatch.setattr(leftovers, "_start_ticks", out_of_fds)


def _failing_read(monkeypatch: pytest.MonkeyPatch, path: Path) -> None:
    real = Path.read_bytes

    def read_bytes(self: Path) -> bytes:
        if self == path:
            raise OSError(errno.EIO, "Input/output error")
        return real(self)

    monkeypatch.setattr(Path, "read_bytes", read_bytes)


def _environ_fails(monkeypatch: pytest.MonkeyPatch, pid: int, exc: OSError) -> None:
    real = open

    def failing(file, *args, **kwargs):
        if file == f"/proc/{pid}/environ":
            raise exc
        return real(file, *args, **kwargs)

    monkeypatch.setattr(leftovers, "open", failing, raising=False)


@contextlib.contextmanager
def _loguru_capture(level: str = "DEBUG") -> Iterator[list[str]]:
    """Capture loguru output for the block, because caplog does not see it."""
    captured: list[str] = []
    sink_id = logger.add(lambda msg: captured.append(msg), level=level)
    try:
        yield captured
    finally:
        logger.remove(sink_id)


def _alive(pid: int) -> bool:
    try:
        with open(f"/proc/{pid}/stat", encoding="utf-8") as f:
            state = f.read().rsplit(")", 1)[1].split()[0]
    except (OSError, IndexError):
        return False
    return state != "Z"


def _gone_within(pid: int, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _alive(pid):
            return True
        time.sleep(0.05)
    return not _alive(pid)


@linux_only
def test_record_and_forget_keep_one_file_per_host(records_dir: Path, group) -> None:
    leftovers.record(group.pid, "stand-in")
    path = records_dir / f"{os.getpid()}-{_own_start()}.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["host_pid"] == os.getpid()
    assert data["host_start"] == _own_start()
    assert data["boot_id"] == leftovers._boot_id()
    assert data["pid_ns"] == leftovers._pid_namespace()
    assert data["groups"][str(group.pid)]["agent"] == "stand-in"
    assert data["groups"][str(group.pid)]["leader_start"] == leftovers._start_ticks(group.pid)
    leftovers.forget(group.pid)
    assert not path.exists()


@linux_only
def test_a_host_file_keeps_the_groups_that_are_still_open(records_dir: Path) -> None:
    first, second = _launch_group(*_dead_host()), _launch_group(*_dead_host())
    try:
        leftovers.record(first.pid, "first")
        leftovers.record(second.pid, "second")
        leftovers.forget(first.pid)
        path = records_dir / f"{os.getpid()}-{_own_start()}.json"
        assert list(json.loads(path.read_text(encoding="utf-8"))["groups"]) == [str(second.pid)]
        leftovers.forget(second.pid)
        assert not path.exists()
    finally:
        _end(first)
        _end(second)


@linux_only
def test_a_rewrite_keeps_the_identity_the_file_was_written_under(
    records_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first, second = _launch_group(*_dead_host()), _launch_group(*_dead_host())
    try:
        leftovers.record(first.pid, "first")
        leftovers.record(second.pid, "second")
        path = records_dir / f"{os.getpid()}-{_own_start()}.json"
        boot_id = leftovers._boot_id()
        # Whatever this process would read about itself now, the file it already
        # wrote goes on naming the host it was written for.
        monkeypatch.setattr(leftovers, "_boot_id", lambda: "another-boot")
        leftovers.forget(first.pid)
        assert json.loads(path.read_text(encoding="utf-8"))["boot_id"] == boot_id
    finally:
        _end(first)
        _end(second)


def test_a_process_that_exits_while_it_is_read_is_gone(monkeypatch: pytest.MonkeyPatch) -> None:
    def exiting(*_args, **_kwargs):
        raise ProcessLookupError(errno.ESRCH, "No such process")

    monkeypatch.setattr(leftovers, "open", exiting, raising=False)
    assert leftovers._start_ticks(os.getpid()) is None


@linux_only
def test_a_process_that_already_exited_is_not_recorded(records_dir: Path) -> None:
    gone = subprocess.Popen(["true"])
    gone.wait(timeout=10)
    leftovers.record(gone.pid, "stand-in")
    assert list(records_dir.glob("*.json")) == []


@linux_only
def test_a_host_that_cannot_name_its_boot_records_nothing(records_dir: Path, group, monkeypatch) -> None:
    monkeypatch.setattr(leftovers, "_boot_id", lambda: None)
    leftovers.record(group.pid, "stand-in")
    assert list(records_dir.glob("*.json")) == []
    assert leftovers._warned is False


@linux_only
def test_records_that_cannot_be_written_never_raise(records_dir: Path, group, monkeypatch) -> None:
    def refuse(*_args, **_kwargs):
        raise OSError(30, "Read-only file system")

    monkeypatch.setattr(leftovers, "atomic_replace", refuse)
    leftovers.record(group.pid, "stand-in")
    leftovers.forget(group.pid)
    assert leftovers._warned is True


@linux_only
def test_an_agent_is_launched_with_this_hosts_mark() -> None:
    assert leftovers.launch_env() == {leftovers._MARK: f"{os.getpid()}-{_own_start()}"}


@linux_only
def test_a_group_left_by_a_dead_host_is_ended(records_dir: Path, group) -> None:
    path = _dead_host_file(records_dir, _entry(group.pid))
    leftovers._sweep()
    assert group.wait(timeout=10) == -signal.SIGKILL
    assert not path.exists()


@linux_only
def test_what_a_dead_hosts_agent_left_in_its_group_is_ended(records_dir: Path) -> None:
    host_pid, host_start = _dead_host()
    # A launcher whose server runs on in its group after the launcher is gone, as
    # an npx launcher's does once the parent-death signal has ended the launcher.
    launcher = subprocess.Popen(
        ["sh", "-c", "sleep 120 >/dev/null 2>&1 & echo $!; wait"],
        start_new_session=True,
        stdout=subprocess.PIPE,
        text=True,
        env=_marked_env(host_pid, host_start),
    )
    assert launcher.stdout is not None
    try:
        server = int(launcher.stdout.readline())
        groups = _entry(launcher.pid)
        launcher.kill()
        launcher.wait(timeout=10)
        _host_file(records_dir, host_pid=host_pid, host_start=host_start, groups=groups)
        leftovers._sweep()
        assert _gone_within(server, 5.0), "the server the launcher left behind is still running"
    finally:
        with contextlib.suppress(ProcessLookupError):
            os.kill(-launcher.pid, signal.SIGKILL)
        launcher.stdout.close()


@linux_only
def test_a_host_that_has_exited_is_taken_for_dead(records_dir: Path, signalled: list[int]) -> None:
    exited = subprocess.Popen(["true"])
    exited.wait(timeout=10)
    launched = _launch_group(exited.pid, 1)
    try:
        _host_file(records_dir, host_pid=exited.pid, host_start=1, groups=_entry(launched.pid))
        leftovers._sweep()
        assert signalled == [launched.pid]
    finally:
        _end(launched)


@linux_only
def test_a_group_without_the_dead_hosts_mark_is_left_alone(records_dir: Path, signalled: list[int]) -> None:
    # The recorded number now names a group somebody else started.
    stranger = subprocess.Popen(["sleep", "120"], start_new_session=True)
    try:
        path = _dead_host_file(records_dir, _entry(stranger.pid))
        leftovers._sweep()
        assert signalled == []
        assert not path.exists()
    finally:
        _end(stranger)


@linux_only
def test_only_the_dead_hosts_exact_mark_counts(records_dir: Path, signalled: list[int]) -> None:
    host_pid, host_start = _dead_host()
    # Another host's mark that merely starts with the dead host's.
    lookalike = _launch_group(host_pid, int(f"{host_start}0"))
    try:
        path = _dead_host_file(records_dir, _entry(lookalike.pid))
        leftovers._sweep()
        assert signalled == []
        assert not path.exists()
    finally:
        _end(lookalike)


@linux_only
def test_a_member_whose_environment_cannot_be_read_now_is_left_for_later(
    records_dir: Path, group, signalled: list[int], monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _dead_host_file(records_dir, _entry(group.pid))
    _environ_fails(monkeypatch, group.pid, OSError(errno.EMFILE, "Too many open files"))
    leftovers._sweep()
    assert signalled == []
    assert path.exists()


@linux_only
def test_a_member_whose_environment_is_closed_to_this_user_is_not_taken_for_ours(
    records_dir: Path, group, signalled: list[int], monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _dead_host_file(records_dir, _entry(group.pid))
    _environ_fails(monkeypatch, group.pid, PermissionError(errno.EACCES, "Permission denied"))
    leftovers._sweep()
    assert signalled == []
    assert not path.exists()


@linux_only
def test_processes_closed_to_this_user_do_not_stop_the_search_for_members(
    records_dir: Path, group, signalled: list[int], monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _dead_host_file(records_dir, _entry(group.pid))
    real = leftovers._stat_fields

    def hidden(pid: int) -> list[str] | None:
        # Every process but the group's and this one, as /proc shows them under hidepid=1.
        if pid not in (group.pid, os.getpid()):
            raise PermissionError(errno.EACCES, "Permission denied")
        return real(pid)

    monkeypatch.setattr(leftovers, "_stat_fields", hidden)
    leftovers._sweep()
    assert signalled == [group.pid]
    assert not path.exists()


@linux_only
def test_a_live_hosts_groups_are_left_alone(records_dir: Path, group, signalled: list[int]) -> None:
    # The stand-in group is its own "host": alive, with the start time on record.
    path = _host_file(
        records_dir, host_pid=group.pid, host_start=leftovers._start_ticks(group.pid), groups=_entry(group.pid)
    )
    leftovers._sweep()
    assert signalled == []
    assert path.exists()


@linux_only
def test_a_reused_leader_pid_is_left_alone(records_dir: Path, group, signalled: list[int]) -> None:
    path = _dead_host_file(records_dir, _entry(group.pid, start=leftovers._start_ticks(group.pid) + 1))
    leftovers._sweep()
    assert signalled == []
    assert not path.exists()


def _booted() -> float:
    booted = leftovers._boot_time()
    assert booted is not None
    return booted


def _written_at(path: Path, when: float) -> None:
    os.utime(path, (when, when))


def _fake_file(monkeypatch: pytest.MonkeyPatch, name: str, text: str) -> None:
    """Make ``leftovers`` read ``text`` wherever it opens ``name``; every other file opens as it does."""
    real = open

    def fake(file, *args, **kwargs):
        if file == name:
            return io.StringIO(text)
        return real(file, *args, **kwargs)

    monkeypatch.setattr(leftovers, "open", fake, raising=False)


@linux_only
def test_a_record_from_before_this_boot_is_dropped_without_ending_anything(
    records_dir: Path, group, signalled: list[int]
) -> None:
    path = _dead_host_file(records_dir, _entry(group.pid), boot_id="another-boot")
    _written_at(path, _booted() - 3600)
    leftovers._sweep()
    assert signalled == []
    assert not path.exists()


@linux_only
def test_a_record_from_another_boot_written_since_this_boot_is_kept(
    records_dir: Path, group, signalled: list[int]
) -> None:
    path = _dead_host_file(records_dir, _entry(group.pid), boot_id="another-boot")
    _written_at(path, time.time())
    leftovers._sweep()
    assert signalled == []
    assert path.exists()


@linux_only
def test_another_boots_record_is_kept_when_the_boot_time_cannot_be_read(
    records_dir: Path, group, signalled: list[int], monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _dead_host_file(records_dir, _entry(group.pid), boot_id="another-boot")
    _written_at(path, _booted() - 3600)
    monkeypatch.setattr(leftovers, "_boot_time", lambda: None)
    leftovers._sweep()
    assert signalled == []
    assert path.exists()


@linux_only
def test_a_record_that_vanishes_before_its_age_is_read_is_not_taken_for_old(tmp_path: Path) -> None:
    assert leftovers._written_before_boot(tmp_path / "gone.json") is False


@linux_only
def test_the_boot_time_is_read_from_proc_stat() -> None:
    # Not compared with /proc/uptime, which lxcfs virtualises on some hosts while /proc/stat
    # stays the host's.
    with open("/proc/stat", encoding="ascii", errors="replace") as f:
        match = re.search(r"^btime (\d+)$", f.read(), re.MULTILINE)
    assert match is not None
    booted = leftovers._boot_time()
    assert booted == int(match.group(1))
    assert booted < time.time()


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("cpu  1 2 3 4\nintr 99\nbtime 1700000000\nprocesses 7\n", 1700000000.0),
        ("cpu  1 2 3 4\nintr 99\nprocesses 7\n", None),
        ("cpu  1 2 3 4\nbtime soon\nprocesses 7\n", None),
    ],
    ids=["picked-by-name", "no-btime-line", "not-a-number"],
)
def test_the_boot_time_is_picked_by_name(monkeypatch: pytest.MonkeyPatch, text: str, expected: float | None) -> None:
    _fake_file(monkeypatch, "/proc/stat", text)
    assert leftovers._boot_time() == expected


@linux_only
def test_a_record_from_another_pid_namespace_is_kept_and_not_acted_on(
    records_dir: Path, group, signalled: list[int]
) -> None:
    path = _dead_host_file(records_dir, _entry(group.pid), pid_ns="pid:[1]")
    leftovers._sweep()
    assert signalled == []
    assert path.exists()


@linux_only
def test_a_host_whose_state_cannot_be_read_is_not_taken_for_dead(
    records_dir: Path, signalled: list[int], monkeypatch: pytest.MonkeyPatch
) -> None:
    host = subprocess.Popen(["sleep", "120"])
    host_start = leftovers._start_ticks(host.pid)
    assert host_start is not None
    launched = _launch_group(host.pid, host_start)
    try:
        path = _host_file(records_dir, host_pid=host.pid, host_start=host_start, groups=_entry(launched.pid))
        _unreadable(monkeypatch, host.pid)
        leftovers._sweep()
        assert signalled == []
        assert path.exists()
    finally:
        _end(launched)
        host.kill()
        host.wait(timeout=10)


@linux_only
def test_a_leader_whose_state_cannot_be_read_is_left_alone(
    records_dir: Path, group, signalled: list[int], monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _dead_host_file(records_dir, _entry(group.pid))
    _unreadable(monkeypatch, group.pid)
    leftovers._sweep()
    assert signalled == []
    assert path.exists()


@linux_only
def test_a_record_that_cannot_be_read_is_kept(records_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = _dead_host_file(records_dir, {})
    _failing_read(monkeypatch, path)
    leftovers._sweep()
    assert path.exists()


@linux_only
@pytest.mark.parametrize(
    "content",
    [b"not json", b"\x80", b"[]", b'{"version": 1, "host_pid": "x", "host_start": 1, "groups": {}}'],
)
def test_a_corrupt_record_is_dropped(records_dir: Path, content: bytes) -> None:
    path = records_dir / "123-456.json"
    path.write_bytes(content)
    leftovers._sweep()
    assert not path.exists()


@linux_only
@pytest.mark.parametrize(
    ("version", "groups_as_list"),
    [(2, False), (2, True), (None, False)],
    ids=["newer-same-shape", "newer-changed-shape", "no-version"],
)
def test_a_record_from_another_version_is_neither_acted_on_nor_removed(
    records_dir: Path, group, signalled: list[int], version: int | None, groups_as_list: bool
) -> None:
    entry = _entry(group.pid)
    groups = [{"pgid": group.pid, **entry[str(group.pid)]}] if groups_as_list else entry
    path = _dead_host_file(records_dir, groups, version=version)
    leftovers._sweep()
    assert signalled == []
    assert path.exists()


@linux_only
def test_a_record_that_fails_to_sweep_does_not_stop_the_rest(
    records_dir: Path, group, signalled: list[int], monkeypatch: pytest.MonkeyPatch
) -> None:
    # "0-0.json" sorts ahead of the dead host's file, so it is reached first.
    first = _host_file(records_dir, host_pid=0, host_start=0, groups={})
    _failing_read(monkeypatch, first)
    _dead_host_file(records_dir, _entry(group.pid))
    leftovers._sweep()
    assert signalled == [group.pid]
    assert first.exists()


@linux_only
def test_a_record_never_signals_group_zero_one_or_its_own(
    records_dir: Path, signalled: list[int], monkeypatch: pytest.MonkeyPatch
) -> None:
    # Every other gate held open, so only this one stands between the record and a signal.
    monkeypatch.setattr(leftovers, "_members", lambda pgid: [pgid])
    monkeypatch.setattr(leftovers, "_carries", lambda pid, mark: True)
    groups = {}
    for pgid in (0, 1, os.getpgrp()):
        groups.update(_entry(pgid, start=leftovers._start_ticks(pgid) or 0))
    _dead_host_file(records_dir, groups)
    leftovers._sweep()
    assert signalled == []


@linux_only
def test_a_process_that_cannot_be_read_is_not_taken_for_gone(monkeypatch: pytest.MonkeyPatch) -> None:
    exited = subprocess.Popen(["true"])
    exited.wait(timeout=10)
    assert leftovers._start_ticks(exited.pid) is None

    def out_of_fds(*_args, **_kwargs):
        raise OSError(errno.EMFILE, "Too many open files")

    monkeypatch.setattr(leftovers, "open", out_of_fds, raising=False)
    with pytest.raises(OSError):
        leftovers._start_ticks(os.getpid())


# PR_SET_NAME is 15. A process chooses its own name, so one can hold ") " and end the
# name field early for a reader that stops at the first closing parenthesis.
_RENAMED_CHILD = """\
import ctypes, sys
ctypes.CDLL(None).prctl(15, b"a) b) c", 0, 0, 0)
print("ready", flush=True)
sys.stdin.read()
"""


@linux_only
def test_a_process_whose_name_holds_a_parenthesis_is_read_right() -> None:
    child = subprocess.Popen(
        [sys.executable, "-c", _RENAMED_CHILD], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True
    )
    try:
        assert child.stdout is not None
        readable, _, _ = select.select([child.stdout], [], [], 10)
        assert readable, "the child never reported ready"
        assert child.stdout.readline().strip() == "ready"
        with open(f"/proc/{child.pid}/comm", encoding="utf-8") as f:
            assert f.read().strip() == "a) b) c"
        fields = leftovers._stat_fields(child.pid)
        assert fields is not None
        # State, parent, group: the child's parent is this process and it shares its group.
        assert (int(fields[1]), int(fields[2])) == (os.getpid(), os.getpgrp())
        started = leftovers._start_ticks(child.pid)
        assert isinstance(started, int)
        assert started > 0
    finally:
        if child.stdin is not None:
            child.stdin.close()
        if child.stdout is not None:
            child.stdout.close()
        child.wait(timeout=10)


@linux_only
def test_this_hosts_own_file_is_not_swept(records_dir: Path, group, signalled: list[int]) -> None:
    leftovers.record(group.pid, "stand-in")
    leftovers._sweep()
    assert signalled == []
    assert (records_dir / f"{os.getpid()}-{_own_start()}.json").exists()
    leftovers.forget(group.pid)


@linux_only
async def test_the_sweep_runs_once_per_process(records_dir: Path, monkeypatch) -> None:
    runs: list[int] = []
    monkeypatch.setattr(leftovers, "_sweep", lambda: runs.append(1))
    await leftovers.sweep_before_first_launch()
    await leftovers.sweep_before_first_launch()
    assert runs == [1]


@linux_only
async def test_a_sweep_that_cannot_reach_its_records_never_fails_a_launch(
    records_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def no_home(_name: str) -> Path:
        raise RuntimeError("Could not determine home directory.")

    monkeypatch.setattr(leftovers, "get_runtime_subdir", no_home)
    await leftovers.sweep_before_first_launch()
    assert leftovers._swept is True


@linux_only
async def test_a_launch_that_arrives_while_the_sweep_runs_waits_for_it(records_dir: Path, monkeypatch) -> None:
    release = threading.Event()
    runs: list[int] = []

    def slow_sweep() -> None:
        release.wait(10)
        runs.append(1)

    monkeypatch.setattr(leftovers, "_sweep", slow_sweep)
    launches: list[asyncio.Task[None]] = []
    try:
        launches.append(asyncio.create_task(leftovers.sweep_before_first_launch()))
        await asyncio.sleep(0.05)
        launches.append(asyncio.create_task(leftovers.sweep_before_first_launch()))
        await asyncio.sleep(0.1)
        assert not any(launch.done() for launch in launches)
        release.set()
        await asyncio.wait_for(asyncio.gather(*launches), 5)
        assert runs == [1]
    finally:
        release.set()
        for launch in launches:
            launch.cancel()
        await asyncio.gather(*launches, return_exceptions=True)


@linux_only
async def test_a_stuck_sweep_holds_a_launch_no_longer_than_the_bound(records_dir: Path, monkeypatch) -> None:
    release = threading.Event()
    monkeypatch.setattr(leftovers, "_SWEEP_WAIT_S", 0.2)
    monkeypatch.setattr(leftovers, "_sweep", lambda: release.wait(10))
    try:
        with _loguru_capture("WARNING") as captured:
            started = time.monotonic()
            await leftovers.sweep_before_first_launch()
            await leftovers.sweep_before_first_launch()
            elapsed = time.monotonic() - started
    finally:
        release.set()
    assert elapsed < 2.0
    assert len([message for message in captured if "still being swept" in message]) == 1


@linux_only
async def test_once_a_launch_has_waited_out_the_bound_the_next_ones_do_not_wait(records_dir: Path, monkeypatch) -> None:
    release = threading.Event()
    monkeypatch.setattr(leftovers, "_SWEEP_WAIT_S", 0.5)
    monkeypatch.setattr(leftovers, "_sweep", lambda: release.wait(10))
    try:
        await leftovers.sweep_before_first_launch()
        started = time.monotonic()
        await leftovers.sweep_before_first_launch()
        await leftovers.sweep_before_first_launch()
        later = time.monotonic() - started
    finally:
        release.set()
    assert later < 0.25


@linux_only
async def test_a_sweep_thread_that_cannot_start_never_stalls_a_launch(records_dir: Path, monkeypatch) -> None:
    class CannotStart:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        def start(self) -> None:
            raise RuntimeError("can't start new thread")

    # Only this module's view of threading, not the Thread class every thread in the process shares.
    monkeypatch.setattr(leftovers, "threading", types.SimpleNamespace(Thread=CannotStart))
    started = time.monotonic()
    await leftovers.sweep_before_first_launch()
    assert time.monotonic() - started < 1.0
    assert leftovers._swept is True


async def test_off_linux_there_is_nothing_to_record_or_sweep(records_dir: Path, monkeypatch) -> None:
    runs: list[int] = []
    monkeypatch.setattr(leftovers, "_on_linux", lambda: False)
    monkeypatch.setattr(leftovers, "_sweep", lambda: runs.append(1))
    leftovers.record(os.getpid(), "stand-in")
    await leftovers.sweep_before_first_launch()
    assert runs == []
    assert list(records_dir.glob("*.json")) == []
    assert leftovers.launch_env() == {}


_REPO = Path(__file__).resolve().parents[1]
_QUIET_AGENT = shlex.join([sys.executable, "-c", "import sys; sys.stdin.read()"])


@linux_only
async def test_a_launch_records_its_group_and_close_forgets_it(records_dir: Path, monkeypatch) -> None:
    monkeypatch.setattr(backend_env, "login_shell_env", lambda: dict(os.environ))
    client = await AcpClient.launch(name="stand-in", command=_QUIET_AGENT)
    try:
        [path] = records_dir.glob("*.json")
        assert list(json.loads(path.read_text(encoding="utf-8"))["groups"]) == [str(client._proc.pid)]
    finally:
        await client.close()
    assert list(records_dir.glob("*.json")) == []


async def test_an_agent_that_is_not_installed_still_fails_at_launch(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(backend_env, "login_shell_env", lambda: {"PATH": str(tmp_path)})
    with pytest.raises(AcpConnectionError, match="cannot start 'no-such-agent'"):
        await AcpClient.launch(name="absent", command="no-such-agent acp")


@linux_only
async def test_a_launch_survives_records_that_cannot_be_written(records_dir: Path, monkeypatch) -> None:
    def refuse(_name: str) -> Path:
        raise OSError(30, "Read-only file system")

    monkeypatch.setattr(backend_env, "login_shell_env", lambda: dict(os.environ))
    monkeypatch.setattr(leftovers, "get_runtime_subdir", refuse)
    client = await AcpClient.launch(name="stand-in", command=_QUIET_AGENT)
    try:
        assert client.alive
    finally:
        await client.close()
    assert leftovers._warned is True


@linux_only
async def test_a_close_survives_records_that_cannot_be_written(records_dir: Path, monkeypatch) -> None:
    """``forget`` runs inside the pool's teardown, where an exception would abandon the
    rest of it. The write refused here rewrites a file the second agent still has a
    group in."""

    def refuse(*_args, **_kwargs):
        raise OSError(30, "Read-only file system")

    monkeypatch.setattr(backend_env, "login_shell_env", lambda: dict(os.environ))
    first = await AcpClient.launch(name="stand-in", command=_QUIET_AGENT)
    second = await AcpClient.launch(name="stand-in", command=_QUIET_AGENT)
    try:
        # Patched after the launches, so the records are on disk and closing the first
        # rewrites a file the second still has a group in.
        monkeypatch.setattr(leftovers, "atomic_replace", refuse)
        await first.close()
        assert not first.alive
        assert second.alive
    finally:
        await second.close()


@linux_only
async def test_a_launch_survives_a_mark_that_cannot_be_read(records_dir: Path, monkeypatch) -> None:
    """The mark is read from /proc inside ``launch``; a failure there may cost the mark,
    never the agent."""

    def unreadable(_pid: int) -> int | None:
        raise OSError(errno.EMFILE, "Too many open files")

    # A runner started under a Raven host carries that host's mark, and the child would
    # inherit it from the environment this test hands down.
    monkeypatch.delenv(leftovers._MARK, raising=False)
    monkeypatch.setattr(backend_env, "login_shell_env", lambda: dict(os.environ))
    monkeypatch.setattr(leftovers, "_start_ticks", unreadable)
    client = await AcpClient.launch(name="stand-in", command=_QUIET_AGENT)
    try:
        assert client.alive
        with open(f"/proc/{client._proc.pid}/environ", "rb") as f:
            entries = f.read().split(b"\0")
        assert not [e for e in entries if e.startswith(f"{leftovers._MARK}=".encode())]
    finally:
        await client.close()


@linux_only
async def test_a_program_on_the_configs_own_path_starts_through_the_wrapper(
    records_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A program only the config's ``env`` PATH can find is wrapped like any other.

    The merged map is what the spawn gets, so the two PATHs are not separable here; the
    child's own PATH is pinned by
    test_a_program_found_through_the_childs_path_or_cwd_starts_through_the_wrapper."""
    (tmp_path / "bin").mkdir()
    (tmp_path / "bin" / "config-path-agent").symlink_to(sys.executable)
    reported = tmp_path / "pdeathsig"
    monkeypatch.setattr(backend_env, "login_shell_env", lambda: {"PATH": str(tmp_path / "nowhere")})
    command = shlex.join(["config-path-agent", "-c", _PDEATHSIG_AGENT, str(reported)])
    client = await AcpClient.launch(
        name="stand-in", command=command, env={"PATH": str(tmp_path / "bin")}, cwd=str(tmp_path)
    )
    try:
        for _ in range(500):
            if reported.exists():
                break
            await asyncio.sleep(0.01)
        assert reported.read_text(encoding="utf-8") == str(int(signal.SIGKILL))
    finally:
        await client.close()


@linux_only
async def test_the_start_log_names_the_agent_not_the_wrapper(
    records_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    program = _program(tmp_path)
    monkeypatch.setattr(backend_env, "login_shell_env", lambda: dict(os.environ))
    with _loguru_capture() as captured:
        client = await AcpClient.launch(name="stand-in", command=shlex.join([str(program)]))
        try:
            assert any(f"started ['{program}']" in message for message in captured)
        finally:
            await client.close()


@linux_only
async def test_the_first_launch_ends_what_a_dead_host_left(records_dir: Path, group, monkeypatch) -> None:
    monkeypatch.setattr(backend_env, "login_shell_env", lambda: dict(os.environ))
    _dead_host_file(records_dir, _entry(group.pid))
    client = await AcpClient.launch(name="stand-in", command=_QUIET_AGENT)
    await client.close()
    assert group.wait(timeout=10) == -signal.SIGKILL


@linux_only
async def test_a_launched_agent_carries_this_hosts_mark_whatever_its_config_says(
    records_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(backend_env, "login_shell_env", lambda: dict(os.environ))
    client = await AcpClient.launch(name="stand-in", command=_QUIET_AGENT, env={leftovers._MARK: "1-1"})
    try:
        with open(f"/proc/{client._proc.pid}/environ", "rb") as f:
            entries = f.read().split(b"\0")
        # Only the mark entries are compared, so a failure never prints the rest of
        # the agent's environment, which carries whatever secrets the host had.
        marks = [e for e in entries if e.startswith(f"{leftovers._MARK}=".encode())]
        assert marks == [f"{leftovers._MARK}={os.getpid()}-{_own_start()}".encode()]
    finally:
        await client.close()


# Staged and renamed, like the report further down, so the file existing means the pid in it
# is whole.
_READY_AGENT = """\
import os, sys, time
open(sys.argv[1] + ".partial", "w").write(str(os.getpid()))
os.replace(sys.argv[1] + ".partial", sys.argv[1])
sys.stdin.read()
time.sleep(120)
"""


_HOST = """\
import asyncio, os, sys
from raven.agent.subagent.backends import env as backend_env
backend_env.login_shell_env = lambda: dict(os.environ)
from raven.acp_client.client import AcpClient

async def main():
    client = await AcpClient.launch(name="stand-in", command=sys.argv[1])
    print(client._proc.pid, flush=True)
    await asyncio.sleep(120)

asyncio.run(main())
"""


@linux_only
@pytest.mark.slow
def test_an_agent_ends_with_a_host_that_is_killed(tmp_path: Path) -> None:
    host = tmp_path / "host.py"
    host.write_text(_HOST, encoding="utf-8")
    ready = tmp_path / "ready"
    agent = shlex.join([sys.executable, "-c", _READY_AGENT, str(ready)])
    env = {**os.environ, "PYTHONPATH": str(_REPO)}
    proc = subprocess.Popen(
        [sys.executable, str(host), agent], stdout=subprocess.PIPE, text=True, env=env, cwd=tmp_path
    )
    agent_pid = None
    try:
        readable, _, _ = select.select([proc.stdout], [], [], 10)
        assert readable, "the host never named its agent"
        agent_pid = int(proc.stdout.readline())
        deadline = time.monotonic() + 10
        while not ready.exists():
            assert time.monotonic() < deadline, "the agent never started"
            time.sleep(0.01)
        # Killing the host now lands after the exec, so what ends the agent is the armed
        # signal and not the wrapper's parent check.
        assert int(ready.read_text(encoding="utf-8")) == agent_pid
        proc.kill()
        proc.wait(timeout=10)
        assert _gone_within(agent_pid, 5.0), "the agent outlived the host that launched it"
    finally:
        proc.kill()
        if proc.stdout is not None:
            proc.stdout.close()
        if agent_pid is not None and _alive(agent_pid):
            with contextlib.suppress(ProcessLookupError):
                os.killpg(agent_pid, signal.SIGKILL)


_STUCK_SWEEP_HOST = """\
import asyncio, time
from raven.acp_client import leftovers as lv

lv._sweep = lambda: time.sleep(60)
lv._SWEEP_WAIT_S = 0.2
asyncio.run(lv.sweep_before_first_launch())
"""


@linux_only
@pytest.mark.slow
def test_a_stuck_sweep_does_not_hold_the_hosts_exit(tmp_path: Path) -> None:
    env = {**os.environ, "PYTHONPATH": str(_REPO)}
    started = time.monotonic()
    result = subprocess.run(
        [sys.executable, "-c", _STUCK_SWEEP_HOST], env=env, cwd=tmp_path, timeout=30, capture_output=True
    )
    assert result.returncode == 0, result.stderr
    assert time.monotonic() - started < 20


# Staged and renamed, so the report existing means its content is whole.
_PDEATHSIG_AGENT = """\
import ctypes, os, sys
sig = ctypes.c_int(-1)
ctypes.CDLL(None).prctl(2, ctypes.byref(sig), 0, 0, 0)
open(sys.argv[1] + ".partial", "w").write(str(sig.value))
os.replace(sys.argv[1] + ".partial", sys.argv[1])
sys.stdin.read()
"""


@linux_only
@pytest.mark.parametrize("program", ["path-only-agent", "bin/path-only-agent"])
async def test_a_program_found_through_the_childs_path_or_cwd_starts_through_the_wrapper(
    program: str, records_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "bin").mkdir()
    (tmp_path / "bin" / "path-only-agent").symlink_to(sys.executable)
    reported = tmp_path / "pdeathsig"
    monkeypatch.setattr(backend_env, "login_shell_env", lambda: {"PATH": str(tmp_path / "bin")})
    command = shlex.join([program, "-c", _PDEATHSIG_AGENT, str(reported)])
    client = await AcpClient.launch(name="stand-in", command=command, cwd=str(tmp_path))
    try:
        for _ in range(500):
            if reported.exists():
                break
            await asyncio.sleep(0.01)
        assert reported.read_text(encoding="utf-8") == str(int(signal.SIGKILL))
    finally:
        await client.close()


@linux_only
async def test_a_launch_that_cannot_start_names_the_agent_not_the_wrapper(tmp_path: Path, monkeypatch) -> None:
    program = _program(tmp_path)
    monkeypatch.setattr(backend_env, "login_shell_env", lambda: dict(os.environ))
    with pytest.raises(AcpConnectionError) as raised:
        await AcpClient.launch(name="stuck", command=str(program), cwd=str(tmp_path / "missing"))
    assert f"cannot start {str(program)!r}" in str(raised.value)
