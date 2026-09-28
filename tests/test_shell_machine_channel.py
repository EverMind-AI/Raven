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


# ---- the plain shell refuses typed ssh to a machine the registry knows ----


@pytest.mark.asyncio
async def test_typed_ssh_to_a_registered_machine_is_refused(registry, tmp_path):
    tool = ExecTool(working_dir=str(tmp_path))
    marker = tmp_path / "ran"

    out = await tool.execute(command=f"ssh -p 58717 root@203.0.113.7 'nohup python train.py &'; touch {marker}")

    assert "Error:" in out
    assert "conn_gpu" in out and "ops_submit" in out
    assert not marker.exists()


@pytest.mark.asyncio
async def test_ssh_to_an_unregistered_host_still_runs(registry, tmp_path):
    tool = ExecTool(working_dir=str(tmp_path))

    out = await tool.execute(command="echo ssh 198.51.100.9 would be typed here")

    assert "would be typed here" in out


@pytest.mark.asyncio
async def test_naming_the_host_without_ssh_is_not_a_bypass(registry, tmp_path):
    tool = ExecTool(working_dir=str(tmp_path))

    out = await tool.execute(command="echo the box is 203.0.113.7")

    assert "203.0.113.7" in out and "Error:" not in out


@pytest.mark.asyncio
async def test_a_broken_registry_refuses_nothing_in_the_plain_shell(monkeypatch, tmp_path):
    def boom():
        raise ValueError("mangled json")

    monkeypatch.setattr("raven.ops.connections.load", boom)
    tool = ExecTool(working_dir=str(tmp_path))

    out = await tool.execute(command="echo ssh 203.0.113.7 with a broken registry")

    assert "with a broken registry" in out


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "written",
    ["ssh", "/usr/bin/ssh", "\\ssh"],
    ids=["bare", "path-qualified", "backslash-escaped"],
)
async def test_the_ssh_client_is_recognised_however_it_is_written(registry, tmp_path, written):
    """All three spellings run the client, so all three reach the far side.

    A leading backslash suppresses alias lookup and an absolute path skips
    PATH; neither changes what executes. Matching only the bare word left both
    on the plain path, past the cap, the process-group sweep and the ledger.
    """
    tool = ExecTool(working_dir=str(tmp_path))
    marker = tmp_path / f"ran-{written.strip('\\/').replace('usr', '')}"

    out = await tool.execute(command=f"{written} -o ConnectTimeout=1 -p 58717 root@203.0.113.7 true; touch {marker}")

    assert "Error:" in out and "conn_gpu" in out
    assert not marker.exists()


@pytest.mark.asyncio
async def test_a_different_port_at_the_same_address_is_a_different_machine(monkeypatch, tmp_path):
    """The registry holds several machines at one address on different ports.

    Matching the address alone returns whichever row is listed first, so the
    refusal names a machine the command was not reaching and the recovery it
    proposes would run the work somewhere else.
    """
    other = dict(ROW) | {"id": "conn_gpu_b", "display_name": "GPU box B", "port": 64101}
    monkeypatch.setattr("raven.ops.connections.load", lambda: [dict(ROW), other])
    tool = ExecTool(working_dir=str(tmp_path))

    out = await tool.execute(command="ssh -o ConnectTimeout=1 -p 64101 root@203.0.113.7 true")

    assert "conn_gpu_b" in out, "the refusal names the machine on the port that was typed"
    assert "conn_gpu'" not in out


@pytest.mark.asyncio
async def test_a_longer_address_that_starts_with_a_registered_one_still_runs(registry, tmp_path):
    """203.0.113.70 is not 203.0.113.7.

    A substring test made every address with a registered one as its prefix
    unreachable from here, which breaks the guarantee that ssh to a machine
    the registry does not hold keeps working.
    """
    tool = ExecTool(working_dir=str(tmp_path))

    out = await tool.execute(command="echo ssh root@203.0.113.70 would be typed here")

    assert "would be typed here" in out and "Error:" not in out


@pytest.mark.asyncio
async def test_a_line_that_cannot_be_tokenised_is_not_judged(registry, tmp_path):
    """An unbalanced quote is not a shell line this can read.

    The shell rejects it too, so nothing reaches a machine either way, and
    guessing at a half-parsed line would refuse commands that never ran.
    """
    tool = ExecTool(working_dir=str(tmp_path))

    out = await tool.execute(command="ssh root@203.0.113.7 'unterminated")

    assert "conn_gpu" not in out, "no refusal is issued for a line nobody can parse"


@pytest.mark.asyncio
async def test_a_flag_that_takes_no_value_does_not_hide_the_destination(registry, tmp_path):
    """-v takes no argument, so the destination is the next word, not the one after it."""
    tool = ExecTool(working_dir=str(tmp_path))
    marker = tmp_path / "ran"

    out = await tool.execute(command=f"ssh -v -o ConnectTimeout=1 -p 58717 root@203.0.113.7 true; touch {marker}")

    assert "Error:" in out and "conn_gpu" in out
    assert not marker.exists()


@pytest.mark.asyncio
async def test_ssh_with_no_destination_at_all_is_left_alone(registry, tmp_path):
    """Flags and nothing else reaches no machine; ssh prints its usage and stops."""
    tool = ExecTool(working_dir=str(tmp_path))

    out = await tool.execute(command="ssh -V")

    assert "conn_gpu" not in out


@pytest.mark.asyncio
async def test_a_row_whose_port_is_not_a_number_is_read_as_the_default(monkeypatch, tmp_path):
    """A hand-edited registry must not make the guard give up on the row.

    22 is what ssh itself would use, so a row with no usable port is compared
    against the port a command that names none would reach.
    """
    monkeypatch.setattr("raven.ops.connections.load", lambda: [dict(ROW) | {"port": "not-a-number"}])
    tool = ExecTool(working_dir=str(tmp_path))

    out = await tool.execute(command="ssh -o ConnectTimeout=1 root@203.0.113.7 true")

    assert "Error:" in out and "conn_gpu" in out


@pytest.mark.asyncio
async def test_an_unspaced_operator_still_separates_the_commands(registry, tmp_path):
    """A shell reads "true&&ssh" as two commands; splitting on whitespace reads one word.

    That word is neither "true" nor "ssh", so the guard saw no ssh at all and
    the registered machine was reached from the plain shell.
    """
    tool = ExecTool(working_dir=str(tmp_path))
    marker = tmp_path / "ran"

    out = await tool.execute(command=f"true&&ssh -o ConnectTimeout=1 -p 58717 root@203.0.113.7 true; touch {marker}")

    assert "Error:" in out and "conn_gpu" in out
    assert not marker.exists()


@pytest.mark.asyncio
async def test_every_ssh_in_the_line_is_read_not_just_the_first(registry, tmp_path):
    """A compound line reaches each of its commands, so each destination counts.

    Stopping at the first let a line through on the strength of the half that
    was allowed, and the registered machine in the second half still ran.
    """
    tool = ExecTool(working_dir=str(tmp_path))
    marker = tmp_path / "ran"

    out = await tool.execute(
        command=(
            "ssh -o ConnectTimeout=1 root@198.51.100.9 true; "
            f"ssh -o ConnectTimeout=1 -p 58717 root@203.0.113.7 true; touch {marker}"
        )
    )

    assert "Error:" in out and "conn_gpu" in out
    assert not marker.exists()


@pytest.mark.asyncio
async def test_the_port_given_as_an_option_counts_like_the_flag(registry, tmp_path):
    """OpenSSH honours "-o Port=N" exactly as it honours "-p N" (checked with ssh -G).

    Consuming -o as a value-taking flag and dropping its value left the guard
    on port 22, so a machine registered elsewhere read as unregistered.
    """
    tool = ExecTool(working_dir=str(tmp_path))
    marker = tmp_path / "ran"

    out = await tool.execute(command=f"ssh -o ConnectTimeout=1 -o Port=58717 root@203.0.113.7 true; touch {marker}")

    assert "Error:" in out and "conn_gpu" in out
    assert not marker.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "option",
    ["-o 'Port 58717'", '-o "Port 58717"', "-o 'Port = 58717'"],
    ids=["single-quoted", "double-quoted", "spaced-equals"],
)
async def test_the_port_option_is_read_when_a_space_separates_it(registry, tmp_path, option):
    """OpenSSH takes the option written either way, so the guard has to read both.

    `ssh -G -o "Port 58717" root@203.0.113.7` prints `port 58717`, the same as
    the equals spelling. Splitting the value on the equals sign alone discarded
    the spaced one, left the guard on port 22, and the line ran on the plain
    shell path -- past the cap, the process-group sweep and the ledger.
    """
    tool = ExecTool(working_dir=str(tmp_path))
    marker = tmp_path / "ran"

    out = await tool.execute(command=f"ssh -o ConnectTimeout=1 {option} root@203.0.113.7 true; touch {marker}")

    assert "Error:" in out and "conn_gpu" in out
    assert not marker.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "redirection",
    ["2>&1", "&>/dev/null", "2>/dev/null"],
    ids=["descriptor-dup", "both-streams", "descriptor-to-file"],
)
async def test_a_redirection_before_the_destination_does_not_hide_it(registry, tmp_path, redirection):
    """A redirection is plumbing; the ssh's own words continue after it.

    `2>&1` arrives as the three tokens 2, >& and 1, so the bare descriptor was
    taken for the destination, and `&>` -- matching no operator the reader knew
    -- became one itself. Either way the registered host further along the line
    was never read and the command ran on the plain shell path.
    """
    tool = ExecTool(working_dir=str(tmp_path))
    marker = tmp_path / "ran"

    out = await tool.execute(
        command=f"ssh {redirection} -o ConnectTimeout=1 -p 58717 root@203.0.113.7 true; touch {marker}"
    )

    assert "Error:" in out and "conn_gpu" in out
    assert not marker.exists()


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        ("ssh -p 58717 root@h true 2>&1 | tee log", [("h", 58717)]),
        ("ssh -p 58717 root@h >", [("h", 58717)]),
        ("ssh root@h >; ssh -p 9 root@k", [("h", 22), ("k", 9)]),
        ("ssh root@h <<EOF\nfoo\nEOF", [("h", 22)]),
        ("ssh -o Port=58717 -o 'Port 22' root@h", [("h", 58717)]),
        ("ssh 2>&1", []),
    ],
    ids=["after-host", "dangling", "dangling-then-separator", "heredoc", "first-port-wins", "no-host"],
)
def test_redirections_are_stepped_over_wherever_they_fall(command, expected):
    """A redirection is neither a destination nor the end of the command.

    Before the destination it is skipped so the host after it is read; after
    the destination it changes nothing; dangling before a separator it does not
    swallow the separator, so the next ssh in the line is still found.
    """
    assert machine_exec._ssh_destinations(command) == expected


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        ("ssh -o Port=58717 -o 'Port 22' root@h", 58717),
        ("ssh -o 'Port 22' -o Port=58717 root@h", 22),
        ("ssh -p 2222 -o Port=58717 root@h", 2222),
        ("ssh -o Port=58717 -p 2222 root@h", 58717),
        ("ssh -p 58717 -p 22 root@h", 58717),
        ("ssh -p 22 -p 58717 root@h", 22),
    ],
    ids=["option-then-option", "reversed", "flag-then-option", "option-then-flag", "flag-twice", "flag-twice-reversed"],
)
def test_a_repeated_port_keeps_the_first_value_as_ssh_does(command, expected):
    """ssh takes the first obtained value for every option, `-p` and `-o Port`
    queueing together: `ssh -G -p 2222 -o Port=58717 host` prints 2222 and the
    two reversed prints 58717 (measured with OpenSSH 9.9p2).

    Overwriting instead read `-p 58717 -p 22` as port 22, so a command that
    really reaches the registered machine on 58717 read as unregistered and
    ran on the plain shell path -- the bypass this guard exists to close.
    """
    assert machine_exec._ssh_destinations(command) == [("h", expected)]


@pytest.mark.asyncio
async def test_an_option_that_is_not_the_port_leaves_the_port_alone(registry, tmp_path):
    """-o carries many settings; only Port changes where the command lands."""
    tool = ExecTool(working_dir=str(tmp_path))

    out = await tool.execute(command="echo ssh -o StrictHostKeyChecking=no root@203.0.113.7 true")

    assert "conn_gpu" not in out, "port 22 is not the registered 58717"


@pytest.mark.asyncio
async def test_a_value_taking_flag_does_not_stand_in_for_the_destination(registry, tmp_path):
    """-l takes the login name, so the bare host after it is still the destination.

    Also the form with no "user@": the destination is the whole word, not
    whatever follows an at sign that is not there.
    """
    tool = ExecTool(working_dir=str(tmp_path))
    marker = tmp_path / "ran"

    out = await tool.execute(command=f"ssh -l root -o ConnectTimeout=1 -p 58717 203.0.113.7 true; touch {marker}")

    assert "Error:" in out and "conn_gpu" in out
    assert not marker.exists()


# Each spelling beside what OpenSSH itself resolves it to. The expectations were
# measured with `ssh -G` (OpenSSH_9.9p2 here, and OpenSSH_8.9p1 in review for
# the ones that apply to both), and the test below re-measures them wherever an
# ssh client is installed, so a wrong expectation cannot hide in this table.
GETOPT_SPELLINGS = [
    ("ssh -vp 58717 root@h true", ("h", 58717)),
    ("ssh -4p 58717 h", ("h", 58717)),
    ("ssh -vvvp58717 h", ("h", 58717)),
    ("ssh -vo Port=58717 h", ("h", 58717)),
    ("ssh -o Port=58717 -vp 22 h", ("h", 58717)),
    ("ssh root@h -p 58717 true", ("h", 58717)),
    ("ssh root@h -o Port=58717 true", ("h", 58717)),
    ("ssh root@h -vp 58717 ls -la", ("h", 58717)),
    ("ssh -p 58717 root@h -p 22", ("h", 58717)),
    ("ssh root@h true -p 58717", ("h", 22)),
    ("ssh root@h -- -p 58717", ("h", 22)),
    ("ssh -- root@h -p 58717", ("h", 22)),
    ("ssh -B lo -p 58717 root@h", ("h", 58717)),
    ("ssh -P tag -p 58717 root@h", ("h", 58717)),
]
GETOPT_IDS = [
    "bundled",
    "bundled-after-a-number-flag",
    "bundled-attached",
    "bundled-option",
    "bundled-after-a-port",
    "port-after-host",
    "option-after-host",
    "bundled-after-host",
    "first-port-wins-across-host",
    "after-the-remote-command",
    "after-double-dash",
    "double-dash-before-host",
    "bind-interface-value",
    "tag-value",
]


@pytest.mark.parametrize(("command", "expected"), GETOPT_SPELLINGS, ids=GETOPT_IDS)
def test_options_are_read_the_way_getopt_and_ssh_read_them(command, expected):
    """Two readings the reviewer showed let a real connection past the guard
    (2026-09-21): a bundled group (`-vp 58717`) was skipped as if it took no
    argument, so the port became the destination; and the scan stopped at the
    host, while OpenSSH goes back to reading options after it until the first
    word that is not one. `-B` takes a value too; the set that said which flags
    do was kept by hand and had lost it, so it is read from ssh's own getopt
    string now."""
    assert machine_exec._ssh_destinations(command) == [expected]


@pytest.mark.parametrize(("command", "expected"), GETOPT_SPELLINGS, ids=GETOPT_IDS)
def test_the_table_agrees_with_the_ssh_on_this_computer(command, expected):
    import shutil
    import subprocess

    if not shutil.which("ssh"):
        pytest.skip("no ssh on this computer")
    words = command.split()[1:]

    def resolved(args: list[str]) -> tuple[str, int]:
        out = subprocess.run(["ssh", "-G", *args], capture_output=True, text=True, check=False, timeout=10).stdout
        seen = dict(line.split(" ", 1) for line in out.splitlines() if line.startswith(("hostname ", "port ")))
        return seen.get("hostname", ""), int(seen.get("port", 0) or 0)

    if "tag" in words and resolved(["-P", "tag", "host.invalid"])[0] != "host.invalid":
        # ``-P`` takes no value before OpenSSH 9.2, where ``tag`` would BE the
        # host: this row's expectation is the newer client's reading, so it is
        # skipped rather than failed. Asked as a question rather than read off a
        # version string, and asked as its own invocation so it cannot perturb
        # the row's own arguments (reviewed 2026-09-24: the earlier check looked
        # for the literal "tag" in the output, which an older ssh prints as the
        # hostname -- so it never skipped).
        pytest.skip("this ssh predates `-P tag` (OpenSSH < 9.2), where P takes no value")
    assert resolved(words) == expected


def test_the_value_taking_flags_are_ssh_s_own():
    """The set is derived from the getopt string, not listed beside it: the
    hand-kept list this replaces had lost `B`."""
    assert machine_exec._SSH_VALUE_FLAGS == frozenset("bceilmopBDEFIJLOPQRSWw")


@pytest.mark.asyncio
async def test_a_port_written_after_the_host_is_still_refused(registry, tmp_path):
    """Through ExecTool, the shape the reviewer ran: the whole line used to run
    on the plain shell path and reached 203.0.113.7:58717."""
    tool = ExecTool(working_dir=str(tmp_path))
    marker = tmp_path / "ran"

    for command in (
        f"ssh -o ConnectTimeout=1 root@203.0.113.7 -p 58717 true; touch {marker}",
        f"ssh -o ConnectTimeout=1 -vp 58717 root@203.0.113.7 true; touch {marker}",
    ):
        out = await tool.execute(command=command)
        assert "Error:" in out and "conn_gpu" in out, command
        assert not marker.exists()


def test_the_machine_lane_appears_once_a_first_machine_is_added(tmp_path, monkeypatch):
    """The reviewed shape (2026-09-24): a process that starts with no machine,
    then gets its first one from ``ops_connection_add``. The registry used to
    serve exec from the copy it took at registration, so ``machine`` stayed
    missing until a restart -- while every text after the add sent the model
    to ``exec(machine=...)``. Written through the real writer and read back
    through the real registry, not a patched ``load``."""
    from raven.agent.tools.registry import ToolRegistry
    from raven.ops import connection_add, connections

    store = tmp_path / "connections.json"
    monkeypatch.setattr(connections, "store_path", lambda: store)
    registry = ToolRegistry()
    registry.register(ExecTool(working_dir=str(tmp_path)))

    def served() -> dict:
        (exec_def,) = [d for d in registry.get_definitions() if d["function"]["name"] == "exec"]
        return exec_def["function"]

    before = served()
    assert "machine" not in before["parameters"]["properties"]
    assert "machine" not in before["description"]

    connection_add.write(dict(ROW, transport="ssh"))

    after = served()
    assert "machine" in after["parameters"]["properties"], "the lane the reply names is in the served schema"
    assert "pass 'machine'" in after["description"]
