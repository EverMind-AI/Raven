"""raven.acp_client.remote: the launch line, session directory and failure sentences.

The quoting tests run the remote command through real shells on this computer
(``sh -c`` standing in for sshd, ``$SHELL -lic`` for the machine's login shell)
with ``HOME`` pointed at a temp dir, so a developer's own rc files take no part.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

from raven.acp_client import remote
from raven.acp_client.remote import Machine, RemoteMachineError

HOST = "203.0.113.7"
PORT = 58717
KEY = "/keys/id_raven_test"

ROW = {
    "id": "box",
    "display_name": "Lab box",
    "host": HOST,
    "port": PORT,
    "user": "worker",
    "key": KEY,
}


def _registry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, rows: list | str) -> None:
    path = tmp_path / "connections.json"
    path.write_text(rows if isinstance(rows, str) else json.dumps({"connections": rows}), encoding="utf-8")
    monkeypatch.setenv("RAVEN_CONNECTIONS", str(path))


def _box() -> Machine:
    return Machine(id="box", display_name="Lab box", _row=dict(ROW))


# Prints what the agent would see: its argv, its cwd and the variables asked for.
_PROBE = (
    "import json, os, sys; "
    "print(json.dumps({'argv': sys.argv[1:], 'cwd': os.getcwd(), "
    "'env': {k: os.environ.get(k) for k in ('RAVEN_SUBAGENT', 'ODD')}}))"
)


def _run_as_sshd(command: str, home: Path) -> subprocess.CompletedProcess[str]:
    """What sshd does with the one argv element it is sent: ``$SHELL -c`` it."""
    env = {"HOME": str(home), "SHELL": "/bin/sh", "PATH": os.environ.get("PATH", "/usr/bin:/bin")}
    return subprocess.run(["/bin/sh", "-c", command], capture_output=True, text=True, env=env, cwd=home, timeout=60)


# --- the launch line -------------------------------------------------------


def test_the_launch_line_reaches_the_machine_with_the_registry_s_options_and_stays_open():
    argv = shlex.split(remote.launch_command(_box(), "qwen --acp", root="~/raven-work"))

    assert argv[0] == "ssh"
    assert argv[argv.index("-i") + 1] == KEY
    assert argv[argv.index("-p") + 1] == str(PORT)
    assert "BatchMode=yes" in argv
    assert "StrictHostKeyChecking=accept-new" in argv
    # Kept open for the life of the agent: no tty, and keepalives so a machine
    # that drops off the network is noticed.
    assert "-T" in argv
    assert "ServerAliveInterval=15" in argv
    assert "ServerAliveCountMax=3" in argv
    dest = argv.index(f"worker@{HOST}")
    assert argv[dest + 1] == "--"
    assert len(argv) == dest + 3, "the remote command is one argv element"
    # Options come before the destination, where ssh reads them.
    assert argv.index("-T") < dest


def test_ssh_s_own_messages_go_to_their_log_not_to_stderr(tmp_path: Path):
    log = tmp_path / "box.log"
    argv = shlex.split(remote.launch_command(_box(), "agent", ssh_log=log))

    assert argv[argv.index("-E") + 1] == str(log)
    assert argv.index("-E") < argv.index(f"worker@{HOST}")
    assert "-E" not in shlex.split(remote.launch_command(_box(), "agent"))


def test_each_agent_gets_an_owner_only_ssh_log(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr("raven.config.paths.get_logs_dir", lambda: tmp_path / "logs")

    first = remote.ssh_log_path("claude_code@box")
    second = remote.ssh_log_path("qwen@box")

    assert first != second and first.parent == second.parent == tmp_path / "logs" / "ssh"
    assert first.parent.stat().st_mode & 0o777 == 0o700
    assert remote.read_ssh_log(first) == ""
    first.write_text("ssh: connect to host x port 1: Operation timed out\n", encoding="utf-8")
    assert "timed out" in remote.read_ssh_log(first)


def test_the_remote_command_runs_the_agent_with_its_arguments_intact(tmp_path: Path):
    # The trap measured 2026-10-10: `ssh host -- bash -lic 'qwen --acp'` loses
    # `--acp` to the far shell's re-parse and starts qwen interactive.
    agent = f"{shlex.quote(sys.executable)} -c {shlex.quote(_PROBE)} --acp 'two words'"
    done = _run_as_sshd(remote.remote_command(agent, root="~/raven-work", env={}), tmp_path)

    assert done.returncode == 0, done.stderr
    seen = json.loads(done.stdout.strip().splitlines()[-1])
    assert seen["argv"] == ["--acp", "two words"]


def test_the_agent_starts_in_the_root_which_the_machine_s_shell_expands_and_makes(tmp_path: Path):
    agent = f"{shlex.quote(sys.executable)} -c {shlex.quote(_PROBE)}"
    done = _run_as_sshd(remote.remote_command(agent, root="~/raven work/agents", env={}), tmp_path)

    assert done.returncode == 0, done.stderr
    seen = json.loads(done.stdout.strip().splitlines()[-1])
    assert Path(seen["cwd"]).resolve() == (tmp_path / "raven work" / "agents").resolve()


def test_variables_reach_the_agent_with_their_values_unchanged(tmp_path: Path):
    odd = "a b 'c' \"d\" $HOME `id` \\e"
    agent = f"{shlex.quote(sys.executable)} -c {shlex.quote(_PROBE)}"
    done = _run_as_sshd(
        remote.remote_command(agent, root=str(tmp_path / "work"), env={"RAVEN_SUBAGENT": "1", "ODD": odd}),
        tmp_path,
    )

    assert done.returncode == 0, done.stderr
    seen = json.loads(done.stdout.strip().splitlines()[-1])
    assert seen["env"] == {"RAVEN_SUBAGENT": "1", "ODD": odd}


def test_the_agent_is_found_on_the_login_shell_s_path(tmp_path: Path):
    # Measured 2026-10-10: agents installed through nvm or into ~/.local/bin
    # were on the PATH of a login shell and of nothing less.
    bin_dir = tmp_path / "agentbin"
    bin_dir.mkdir()
    agent = bin_dir / "fake-agent"
    agent.write_text("#!/bin/sh\necho found-by-login-path\n", encoding="utf-8")
    agent.chmod(0o755)
    (tmp_path / ".profile").write_text('PATH="$HOME/agentbin:$PATH"; export PATH\n', encoding="utf-8")

    done = _run_as_sshd(remote.remote_command("fake-agent", root="~/raven-work", env={}), tmp_path)

    assert done.returncode == 0, done.stderr
    assert "found-by-login-path" in done.stdout


def test_a_variable_name_the_shell_cannot_assign_is_refused():
    with pytest.raises(ValueError, match="not an environment variable name"):
        remote.remote_command("agent", root="~/raven-work", env={"A-B": "1"})


def test_an_absolute_root_is_quoted_and_a_bare_tilde_is_left_to_the_shell():
    assert remote._shell_path("/srv/raven work") == "'/srv/raven work'"
    assert remote._shell_path("~") == "~"
    assert remote._shell_path("~/") == "~"
    assert remote._shell_path("") == "~/raven-work"


# --- the machine ------------------------------------------------------------


def test_a_registered_machine_is_found_and_never_prints_its_address(tmp_path, monkeypatch):
    _registry(tmp_path, monkeypatch, [ROW])

    found = remote.machine("box")

    assert (found.id, found.display_name) == ("box", "Lab box")
    for text in (repr(found), str(found), found.label):
        assert HOST not in text and str(PORT) not in text and KEY not in text


@pytest.mark.parametrize(
    ("rows", "said"),
    [
        ([], "is not registered"),
        ([{**ROW, "id": "other"}], "is not registered"),
        ([ROW, ROW], "cannot be used"),
        ([{**ROW, "host": ""}], "cannot be used"),
        ([{**ROW, "port": "22"}], "cannot be used"),
        ([{"id": "box", "display_name": "Here", "transport": "local"}], "is this computer"),
        ("{not json", "cannot be read"),
    ],
)
def test_a_machine_that_cannot_be_used_is_refused_with_the_reason(tmp_path, monkeypatch, rows, said):
    _registry(tmp_path, monkeypatch, rows)

    with pytest.raises(RemoteMachineError, match=said) as caught:
        remote.machine("box")

    assert HOST not in str(caught.value)


# --- the session directory -------------------------------------------------


def _local_runner(home: Path, sent: list[str]):
    def runner_from(row, *, cap_seconds=None):
        def run(cmd: str) -> tuple[int, str]:
            sent.append(cmd)
            done = _run_as_sshd(cmd, home)
            return done.returncode, done.stdout + (f"\n{done.stderr}" if done.returncode else "")

        return run

    return runner_from


def test_the_session_directory_is_made_on_the_machine_and_comes_back_absolute(tmp_path, monkeypatch):
    sent: list[str] = []
    monkeypatch.setattr(remote, "runner_from", _local_runner(tmp_path, sent))

    got = remote.session_dir(_box(), root="~/raven-work", handle="reviewer-1")

    assert got.startswith("/")
    assert Path(got).resolve() == (tmp_path / "raven-work" / "reviewer-1").resolve()
    assert Path(got).is_dir()
    assert len(sent) == 1, "one round trip"


def test_a_relative_root_comes_back_absolute_from_where_the_login_lands(tmp_path, monkeypatch):
    # session/new takes an absolute cwd; a root written relative to the
    # registered user's home is resolved there, not on this computer.
    monkeypatch.setattr(remote, "runner_from", _local_runner(tmp_path, []))

    got = remote.session_dir(_box(), root="work", handle="t1")

    assert got.startswith("/")
    assert Path(got).resolve() == (tmp_path / "work" / "t1").resolve()


def test_two_handles_get_two_directories_even_when_made_safe_alike(tmp_path, monkeypatch):
    monkeypatch.setattr(remote, "runner_from", _local_runner(tmp_path, []))

    first = remote.session_dir(_box(), root="~/raven-work", handle="a b")
    second = remote.session_dir(_box(), root="~/raven-work", handle="a_b")

    assert first != second
    assert Path(first).parent == Path(second).parent


def test_a_session_directory_that_cannot_be_made_says_where_and_not_how(tmp_path, monkeypatch):
    def runner_from(row, *, cap_seconds=None):
        return lambda cmd: (
            255,
            f"\nssh: connect to host {HOST} port {PORT}: Operation timed out",
        )

    monkeypatch.setattr(remote, "runner_from", runner_from)

    with pytest.raises(RemoteMachineError) as caught:
        remote.session_dir(_box(), handle="t1")

    said = str(caught.value)
    assert "did not answer within the connection timeout" in said
    assert "'Lab box' (box)" in said
    assert HOST not in said and str(PORT) not in said


def test_a_directory_the_machine_will_not_make_is_named_without_ssh_s_words(tmp_path, monkeypatch):
    def runner_from(row, *, cap_seconds=None):
        return lambda cmd: (1, "\nmkdir: cannot create directory '/srv/x': Permission denied")

    monkeypatch.setattr(remote, "runner_from", runner_from)

    with pytest.raises(RemoteMachineError, match=r"could not make a working directory under '/srv/x'"):
        remote.session_dir(_box(), root="/srv/x", handle="t1")


# --- failures ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("rc", "output", "said"),
    [
        (255, f"ssh: connect to host {HOST} port {PORT}: Operation timed out", "did not answer within"),
        (255, f"ssh: connect to host {HOST} port {PORT}: Connection timed out", "did not answer within"),
        (255, f"ssh: connect to host {HOST} port {PORT}: Connection refused", "could not be reached"),
        (255, f"ssh: connect to host {HOST} port {PORT}: No route to host", "could not be reached"),
        (255, f"ssh: Could not resolve hostname {HOST}: nodename nor servname provided", "could not be reached"),
        (255, f"worker@{HOST}: Permission denied (publickey).", "refused the login"),
        (255, "@@@ WARNING: REMOTE HOST IDENTIFICATION HAS CHANGED! @@@", "host key that differs"),
        (255, "Host key verification failed.", "host key that differs"),
        (255, f"Connection to {HOST} closed by remote host.", "ended before the agent answered"),
        (127, "bash: line 1: qwen: command not found", "command was not found on machine"),
        (124, "", "did not answer in time"),
    ],
)
def test_an_ssh_failure_becomes_one_sentence_without_the_address(rc, output, said):
    sentence = remote.explain(_box(), rc, output)

    assert sentence is not None and said in sentence
    assert HOST not in sentence and str(PORT) not in sentence


def test_a_failure_past_ssh_is_not_ssh_s_to_explain():
    assert remote.explain(_box(), 1, "Error: something the agent said") is None
    assert remote.explain(_box(), 0, "") is None


# --- leaf ----------------------------------------------------------------------


@pytest.mark.parametrize("handle", ["reviewer-1", "t_0123abcd", "v2.final"])
def test_a_safe_handle_is_its_own_directory_name(handle):
    assert remote.leaf(handle) == handle


@pytest.mark.parametrize("handle", ["a b", "../up", "..", "", "x/y", "\u540d\u5b57"])
def test_an_unsafe_handle_becomes_one_component_that_cannot_climb(handle):
    got = remote.leaf(handle)

    assert "/" not in got and not got.startswith(".") and got
    assert remote.leaf(handle) == got, "the same handle always lands in the same place"


def test_a_machine_named_only_by_its_id_is_called_that():
    assert Machine(id="box", display_name="box", _row={}).label == "'box'"


def test_an_entry_that_lost_its_address_is_refused_by_name_not_as_a_network_fault():
    # machine() refuses such a row up front; this is the guard behind it, for a
    # Machine built from a row that changed after it was read.
    bare = Machine(id="box", display_name="Lab box", _row={"id": "box"})

    with pytest.raises(RemoteMachineError, match=r"'Lab box' \(box\): .*has no address"):
        remote.launch_command(bare, "agent")
    with pytest.raises(RemoteMachineError, match=r"'Lab box' \(box\): .*has no address"):
        remote.session_dir(bare, handle="t1")
