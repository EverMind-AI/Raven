"""The exec tool's machine channel: registry-addressed, capped, kill-transitive.

The channel exists so the address never has to enter the model's context: a
field run measured every remote look typed as a raw ``ssh -p <port> root@<ip>``
because the task statement had to carry the address for anything to work at
all. Naming a connection id instead keeps host/port/key below the model, makes
the cap reach both ends of the wire, and gives the detach refusal one place to
stand.
"""

from __future__ import annotations

import time

import pytest

from raven.agent.tools import machine_exec
from raven.agent.tools.shell import ExecTool

ROW = {
    "id": "conn_gpu",
    "display_name": "GPU box",
    "host": "203.0.113.7",
    "port": 58717,
    "user": "root",
    "key": "~/.ssh/id_rsa",
}


@pytest.fixture
def registry(monkeypatch):
    monkeypatch.setattr("raven.ops.connections.load", lambda: [dict(ROW)])


def _capturing_runner(record: list[str], rc: int = 0, out: str = "ok"):
    def factory(row, *, cap_seconds=None):
        def run(cmd: str) -> tuple[int, str]:
            record.append(cmd)
            return rc, out

        return run

    return factory


# ---- the schema is honest about what this install can reach ----


def test_no_registry_means_no_machine_parameter(monkeypatch, tmp_path):
    monkeypatch.setattr("raven.ops.connections.load", lambda: [])
    tool = ExecTool(working_dir=str(tmp_path))

    assert "machine" not in tool.parameters["properties"]
    assert "name a machine" not in tool.description


def test_a_registered_machine_advertises_the_channel(registry, tmp_path):
    tool = ExecTool(working_dir=str(tmp_path))

    prop = tool.parameters["properties"]["machine"]
    assert "connection id" in prop["description"]
    assert "exactly as listed" in prop["description"], "the id form is taught before the first mistake"
    assert "name a machine" in tool.description


def test_a_broken_registry_leaves_the_plain_shell_working(monkeypatch, tmp_path):
    def boom():
        raise ValueError("mangled json")

    monkeypatch.setattr("raven.ops.connections.load", boom)
    tool = ExecTool(working_dir=str(tmp_path))

    assert "machine" not in tool.parameters["properties"]


# ---- routing: a named machine bypasses the local guards, not the rules ----


@pytest.mark.asyncio
async def test_a_named_machine_routes_to_the_channel(registry, monkeypatch, tmp_path):
    seen: dict = {}

    async def fake(command, *, connection, cwd=None):
        seen.update(command=command, connection=connection, cwd=cwd)
        return "routed"

    monkeypatch.setattr("raven.agent.tools.machine_exec.run_on_machine", fake)
    tool = ExecTool(working_dir=str(tmp_path))

    out = await tool.execute(command="nvidia-smi", machine="conn_gpu", working_dir="/root/ws")

    assert out == "routed"
    assert seen == {"command": "nvidia-smi", "connection": "conn_gpu", "cwd": "/root/ws"}


@pytest.mark.asyncio
async def test_an_unknown_id_answers_with_the_list(registry):
    out = await machine_exec.run_on_machine("ls", connection="conn-typo")

    assert "No connection with id 'conn-typo'" in out
    assert "conn_gpu" in out, "the refusal carries the registry, so the next call can be right"


# ---- the channel's own rules ----


@pytest.mark.asyncio
async def test_a_detaching_command_is_refused_before_anything_is_sent(registry, monkeypatch):
    def never(*a, **kw):
        raise AssertionError("a refused command must not build a runner")

    monkeypatch.setattr("raven.ops.transport.runner_from", never)

    out = await machine_exec.run_on_machine("nohup python train.py &", connection="conn_gpu")

    assert "Refusing this command" in out
    assert "ops_submit" in out
    assert "Nothing was run" in out
    assert "stopped when the call returns" in out, "the refusal says the fence behind it"


@pytest.mark.parametrize(
    "command",
    [
        "sleep 300 &",
        "sleep 300 & echo started",
        "python train.py > log 2>&1 & echo ok",
        "(sleep 300 &)",
        "bash -c 'sleep 300 &'",
        "sleep 300 &; echo",
    ],
)
def test_every_backgrounding_ampersand_is_refused_not_only_a_trailing_one(command):
    """Reviewer 2026-09-07: the guard caught a bare trailing & and let the other
    five shapes through to the machine."""
    assert machine_exec._detaching(command) == "a backgrounding &"


@pytest.mark.parametrize(
    "command", ["make -j8 && make test", "python x.py 2>&1 | tail", "cmd &> log", "a |& b", "x <&0"]
)
def test_a_chain_a_redirection_or_a_pipe_is_not_a_backgrounding_ampersand(command):
    assert machine_exec._detaching(command) is None


@pytest.mark.asyncio
async def test_a_remote_command_runs_as_one_capped_group_on_the_remote_end(registry, monkeypatch):
    sent: list[str] = []
    monkeypatch.setattr("raven.ops.transport.runner_from", _capturing_runner(sent))

    out = await machine_exec.run_on_machine("nvidia-smi", connection="conn_gpu", cwd="/root/ws")

    assert len(sent) == 1
    assert sent[0].startswith("cd /root/ws")
    assert "set -m" in sent[0] and "kill -TERM -- -$pid" in sent[0], "the group kill must live on the remote end"
    assert f"sleep {machine_exec._TIMEOUT_S};" in sent[0], "so must the cap"
    assert sent[0].rstrip().endswith("raven-look nvidia-smi")
    assert "on GPU box in /root/ws (exit 0)" in out


def _gone(pid: int) -> bool:
    """Whether ``pid`` is dead or a zombie nobody has reaped yet.

    No ``pgrep``/``ps``: the CI image ships neither. A signal probe answers
    "alive" for a zombie, and a swept child whose parent is gone sits as one
    until init gets to it, so on Linux the state is read from /proc.
    """
    import os
    from pathlib import Path

    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    status = Path(f"/proc/{pid}/status")
    if status.exists():
        return "State:\tZ" in status.read_text(errors="replace")
    return False


def _wait_gone(pid: int, seconds: float = 5.0) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if _gone(pid):
            return True
        time.sleep(0.05)
    return _gone(pid)


@pytest.mark.asyncio
async def test_work_left_running_in_the_background_is_stopped_when_the_look_returns(registry, monkeypatch, tmp_path):
    """The reviewer's reproduction, against a real shell: a child the guard
    cannot see (spawned from python, no & in the command) is reparented to
    init and runs on unbounded unless the group is swept. Local transport, so
    the real runner and the real wrapper run. The child's pid is written by
    the spawner before the foreground returns, so the check needs no process
    listing tool."""
    import sys

    monkeypatch.setattr("raven.ops.connections.load", lambda: [{**ROW, "transport": "local"}])
    pidfile = tmp_path / "child.pid"
    spawn = f"import subprocess; p = subprocess.Popen(['sleep', '2718']); open({str(pidfile)!r}, 'w').write(str(p.pid))"
    command = f"{sys.executable} -c {spawn!r}; echo ok"
    assert machine_exec._detaching(command) is None, "the shape the guard cannot see"

    started = time.monotonic()
    out = await machine_exec.run_on_machine(command, connection="conn_gpu")

    assert time.monotonic() - started < 10, "the sweep ends the call; nothing waits for the cap"
    assert "(exit 0)" in out and "ok" in out
    assert "left work running in the background; it was stopped" in out
    assert "ops_submit" in out
    child = int(pidfile.read_text())
    assert _wait_gone(child), "the child must not outlive the call"


@pytest.mark.asyncio
async def test_the_cap_fires_on_the_machine_and_takes_the_whole_group(registry, monkeypatch, tmp_path):
    monkeypatch.setattr("raven.ops.connections.load", lambda: [{**ROW, "transport": "local"}])
    monkeypatch.setattr(machine_exec, "_TIMEOUT_S", 1)
    pidfile = tmp_path / "sleeper.pid"
    # The sleeper writes its own pid, then becomes the sleep, so the pid on
    # file is the process the cap has to reach.
    sleeper = f"bash -c 'echo $$ > {pidfile}; exec sleep 2719'"

    started = time.monotonic()
    out = await machine_exec.run_on_machine(f"echo before; {sleeper}; echo after", connection="conn_gpu")

    assert time.monotonic() - started < 4, "a cap kill returns when the group is gone, not after the KILL grace"
    assert "killed at the 1s limit" in out and "before" in out and "after" not in out
    assert _wait_gone(int(pidfile.read_text()))


@pytest.mark.asyncio
async def test_a_local_transport_is_not_wrapped_in_timeout(registry, monkeypatch):
    sent: list[str] = []
    monkeypatch.setattr("raven.ops.transport.runner_from", _capturing_runner(sent))
    monkeypatch.setattr("raven.ops.connections.load", lambda: [{**ROW, "transport": "local"}])

    await machine_exec.run_on_machine("ls", connection="conn_gpu")

    assert "timeout " not in sent[0], "macOS ships no GNU timeout; the local runner caps the call itself"


@pytest.mark.asyncio
async def test_a_cap_kill_names_the_job_door(registry, monkeypatch):
    from raven.ops.transport import TIMED_OUT_RC

    monkeypatch.setattr("raven.ops.transport.runner_from", _capturing_runner([], rc=TIMED_OUT_RC, out="partial"))

    out = await machine_exec.run_on_machine("python train.py", connection="conn_gpu")

    assert f"killed at the {machine_exec._TIMEOUT_S}s limit" in out
    assert "ops_submit" in out, "a cap kill is a routing signal, not a transient failure"


@pytest.mark.asyncio
async def test_a_missing_directory_is_named_not_guessed(registry, monkeypatch):
    monkeypatch.setattr("raven.ops.transport.runner_from", _capturing_runner([], rc=66, out=""))

    out = await machine_exec.run_on_machine("ls", connection="conn_gpu", cwd="/no/such/dir")

    assert "no such directory" in out
    assert "Nothing was run" in out


# ---- the runner half ----


def test_runner_from_refuses_a_row_with_no_address():
    from raven.ops.transport import TransportError, runner_from

    with pytest.raises(TransportError, match="no address"):
        runner_from({"id": "conn_lost"})


def test_a_local_row_runs_here_in_the_ssh_answer_shape():
    from raven.ops.transport import runner_from

    run = runner_from({"id": "here", "transport": "local"}, cap_seconds=30)
    rc, out = run("echo hello && echo oops 1>&2; exit 3")

    assert rc == 3
    assert "hello" in out
    assert "oops" in out, "stderr rides along on failure, the way the ssh runner answers"


def test_a_local_row_cap_kills_the_command():
    from raven.ops.transport import TIMED_OUT_RC, runner_from

    run = runner_from({"id": "here", "transport": "local"}, cap_seconds=0.2)
    rc, _ = run("sleep 5")

    assert rc == TIMED_OUT_RC


def test_a_local_row_cap_kills_the_commands_children_too(tmp_path):
    """subprocess.run's timeout reached the shell alone: a python that had
    forked a sleep kept both alive past the 124 that said they were stopped
    (reviewed 2026-09-04). The command runs in its own session and the cap
    kills that session."""
    import os
    import sys
    import time

    from raven.ops.transport import TIMED_OUT_RC, runner_from

    pid_file = tmp_path / "child.pid"
    script = (
        "import subprocess, sys, time, pathlib; "
        "p = subprocess.Popen(['sleep', '30']); "
        f"pathlib.Path({str(pid_file)!r}).write_text(str(p.pid)); "
        "time.sleep(30)"
    )
    run = runner_from({"id": "here", "transport": "local"}, cap_seconds=0.5)
    rc, _ = run(f"{sys.executable} -c {script!r}")

    assert rc == TIMED_OUT_RC
    child = int(pid_file.read_text())
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        try:
            os.kill(child, 0)
        except ProcessLookupError:
            break
        time.sleep(0.05)
    else:
        os.kill(child, 9)
        raise AssertionError(f"the command's child (pid {child}) outlived the cap")


def test_without_killpg_the_cap_falls_back_to_a_tree_kill_by_pid(monkeypatch):
    """Native Windows has neither ``killpg`` nor a working ``start_new_session``.
    The runner must still return 124 and still reach the command's children:
    the platform's tree kill is ``taskkill /T /F`` by pid."""
    import os
    from types import SimpleNamespace

    from raven.ops import transport

    monkeypatch.delattr(os, "killpg", raising=False)
    calls: list[list[str]] = []

    def fake_run(argv, **kwargs):
        calls.append(list(argv))
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(transport.subprocess, "run", fake_run)
    monkeypatch.setattr(transport.shutil, "which", lambda name: "C:/Windows/System32/taskkill.exe")
    killed: list[str] = []
    proc = SimpleNamespace(pid=4242, poll=lambda: None, kill=lambda: killed.append("kill"))

    assert transport._own_group_kwargs() == {}
    transport._kill_process_tree(proc)

    assert calls == [["C:/Windows/System32/taskkill.exe", "/T", "/F", "/PID", "4242"]]
    assert killed == ["kill"], "and the shell itself is still killed when the tree kill left it"
