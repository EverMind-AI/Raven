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


def _run_as_sshd(command: str, home: Path, stdin: bytes = b"\n") -> subprocess.CompletedProcess[str]:
    """What sshd does with the one argv element it is sent: ``$SHELL -c`` it.

    ``stdin`` is what the client writes first; the launch line's shell reads
    the variables from it up to an empty line, which by default is all it is.
    """
    env = {"HOME": str(home), "SHELL": "/bin/sh", "PATH": os.environ.get("PATH", "/usr/bin:/bin")}
    return subprocess.run(
        ["/bin/sh", "-c", command],
        capture_output=True,
        text=True,
        input=stdin.decode("utf-8"),
        env=env,
        cwd=home,
        timeout=60,
    )


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
    done = _run_as_sshd(remote.remote_command(agent, root="~/raven-work"), tmp_path)

    assert done.returncode == 0, done.stderr
    seen = json.loads(done.stdout.strip().splitlines()[-1])
    assert seen["argv"] == ["--acp", "two words"]


def test_the_agent_starts_in_the_root_which_the_machine_s_shell_expands_and_makes(tmp_path: Path):
    agent = f"{shlex.quote(sys.executable)} -c {shlex.quote(_PROBE)}"
    done = _run_as_sshd(remote.remote_command(agent, root="~/raven work/agents"), tmp_path)

    assert done.returncode == 0, done.stderr
    seen = json.loads(done.stdout.strip().splitlines()[-1])
    assert Path(seen["cwd"]).resolve() == (tmp_path / "raven work" / "agents").resolve()


# Reads like an ACP agent: the variables it was started with, then all of stdin.
_READER = (
    "import json, os, sys; rest = sys.stdin.read(); "
    "print(json.dumps({'env': {k: os.environ.get(k) for k in ('RAVEN_SUBAGENT', 'ODD', 'OPENAI_API_KEY')}, "
    "'rest': rest}))"
)
_FRAMES = '{"jsonrpc":"2.0","id":1,"method":"initialize"}\n{"jsonrpc":"2.0","id":2,"method":"session/new"}\n'


def test_variables_arrive_on_stdin_unchanged_and_the_protocol_after_them_untouched(tmp_path: Path):
    # Review of #897: an entry's env holds credentials, and an argument is in
    # the process list at both ends, so the values come on stdin ahead of the
    # first frame -- and the shell must not read a byte past its empty line.
    odd = "a b 'c' \"d\" $HOME `id` \\e"
    env = {"RAVEN_SUBAGENT": "1", "ODD": odd, "OPENAI_API_KEY": "sk-raven-test-not-real"}
    agent = f"{shlex.quote(sys.executable)} -c {shlex.quote(_READER)}"
    command = remote.remote_command(agent, root=str(tmp_path / "work"))

    done = _run_as_sshd(command, tmp_path, stdin=remote.env_preamble(env) + _FRAMES.encode())

    assert done.returncode == 0, done.stderr
    seen = json.loads(done.stdout.strip().splitlines()[-1])
    assert seen == {"env": env, "rest": _FRAMES}
    assert "sk-raven-test-not-real" not in command and odd not in command


def test_with_no_variables_the_agent_still_starts_and_reads_the_protocol(tmp_path: Path):
    agent = f"{shlex.quote(sys.executable)} -c {shlex.quote(_READER)}"

    done = _run_as_sshd(
        remote.remote_command(agent, root="~/raven-work"), tmp_path, stdin=remote.env_preamble({}) + _FRAMES.encode()
    )

    assert done.returncode == 0, done.stderr
    assert json.loads(done.stdout.strip().splitlines()[-1])["rest"] == _FRAMES
    assert remote.env_preamble({}) == b"\n"


def test_a_value_with_a_line_break_is_refused_by_name_not_by_value():
    with pytest.raises(ValueError, match=r"\['TOKEN'\] holds a line break") as caught:
        remote.env_preamble({"TOKEN": "first\nsecond-secret"})
    assert "second-secret" not in str(caught.value)


def test_a_launch_keeps_the_entry_s_secret_off_its_command_line(tmp_path, monkeypatch):
    _registry(tmp_path, monkeypatch, [ROW])
    monkeypatch.setattr("raven.config.paths.get_logs_dir", lambda: tmp_path / "logs")

    launched = remote.prepare_launch(
        "a", "box", "agent --acp", remote_cwd=None, env={"OPENAI_API_KEY": "sk-raven-test-not-real"}
    )

    assert "sk-raven-test-not-real" not in launched.command
    assert b"OPENAI_API_KEY=sk-raven-test-not-real\n" in launched.preamble
    assert launched.preamble.startswith(b"RAVEN_SUBAGENT=1\n") and launched.preamble.endswith(b"\n\n")
    assert "sk-raven-test-not-real" not in repr(launched)


def test_a_launch_with_an_unsendable_value_is_refused_naming_the_agent(tmp_path, monkeypatch):
    _registry(tmp_path, monkeypatch, [ROW])
    monkeypatch.setattr("raven.config.paths.get_logs_dir", lambda: tmp_path / "logs")

    with pytest.raises(RemoteMachineError, match=r"agent 'a' on machine 'Lab box' \(box\): .*line break"):
        remote.prepare_launch("a", "box", "agent", remote_cwd=None, env={"TOKEN": "x\ny"})


def test_the_agent_is_found_on_the_login_shell_s_path(tmp_path: Path):
    # Measured 2026-10-10: agents installed through nvm or into ~/.local/bin
    # were on the PATH of a login shell and of nothing less.
    bin_dir = tmp_path / "agentbin"
    bin_dir.mkdir()
    agent = bin_dir / "fake-agent"
    agent.write_text("#!/bin/sh\necho found-by-login-path\n", encoding="utf-8")
    agent.chmod(0o755)
    (tmp_path / ".profile").write_text('PATH="$HOME/agentbin:$PATH"; export PATH\n', encoding="utf-8")

    done = _run_as_sshd(remote.remote_command("fake-agent", root="~/raven-work"), tmp_path)

    assert done.returncode == 0, done.stderr
    assert "found-by-login-path" in done.stdout


def test_a_variable_name_the_shell_cannot_assign_is_refused():
    with pytest.raises(ValueError, match="not an environment variable name"):
        remote.env_preamble({"A-B": "1"})


def test_an_absolute_root_is_quoted_and_a_bare_tilde_is_left_to_the_shell():
    assert remote.shell_path("/srv/raven work") == "'/srv/raven work'"
    assert remote.shell_path("~") == "~"
    assert remote.shell_path("~/") == "~"
    assert remote.shell_path("") == "~/raven-work"


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


def test_a_directory_the_machine_will_not_make_gives_its_reason_without_ssh_s_words(tmp_path, monkeypatch):
    # Measured 2026-10-10: a registered machine whose disk had filled. On a
    # first connect the runner's output also carries ssh's own warning, which
    # names the address.
    def runner_from(row, *, cap_seconds=None):
        return lambda cmd: (
            1,
            f"\nWarning: Permanently added '[{HOST}]:{PORT}' (ED25519) to the list of known hosts.\n"
            "mkdir: cannot create directory '/srv/x': No space left on device",
        )

    monkeypatch.setattr(remote, "runner_from", runner_from)

    with pytest.raises(RemoteMachineError) as caught:
        remote.session_dir(_box(), root="/srv/x", handle="t1")

    said = str(caught.value)
    assert said.startswith("could not make a working directory under '/srv/x' on machine 'Lab box' (box): ")
    assert said.endswith("No space left on device")
    assert HOST not in said and str(PORT) not in said


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


@pytest.mark.parametrize(
    "said",
    [
        "mkdir: cannot create directory '/root/raven-work': No space left on device",
        "mkdir: cannot create directory '/dev/raven-work': Read-only file system",
    ],
)
def test_a_host_that_is_a_plain_word_does_not_hide_the_machine_s_reason(tmp_path, monkeypatch, said):
    # Review of #893: filtering on the host as a substring made a host called
    # "dev" swallow "No space left on device". ssh's own lines go by their shape.
    dev = Machine(id="dev", display_name="Dev box", _row={**ROW, "id": "dev", "host": "dev"})

    def runner_from(row, *, cap_seconds=None):
        return lambda cmd: (1, f"\nWarning: Permanently added '[dev]:22' (ED25519) to the list of known hosts.\n{said}")

    monkeypatch.setattr(remote, "runner_from", runner_from)

    with pytest.raises(RemoteMachineError) as caught:
        remote.session_dir(dev, root="/srv/x", handle="t1")

    assert str(caught.value).endswith(said)


@pytest.mark.parametrize(
    "line",
    [
        f"Warning: Permanently added '[{HOST}]:{PORT}' (ED25519) to the list of known hosts.",
        f"ssh: connect to host {HOST} port {PORT}: Connection refused",
        f"worker@{HOST}: Permission denied (publickey).",
        f"Connection to {HOST} closed by remote host.",
        f"ssh: Could not resolve hostname {HOST}: Name or service not known",
    ],
)
def test_ssh_s_own_lines_are_never_quoted_as_the_machine_s_reason(line):
    assert remote._far_side_words(_box(), ["mkdir: failed", line]) == "mkdir: failed"


def test_a_failure_whose_only_words_are_ssh_s_gives_no_reason_rather_than_the_address(tmp_path, monkeypatch):
    def runner_from(row, *, cap_seconds=None):
        return lambda cmd: (1, f"\nWarning: Permanently added '[{HOST}]:{PORT}' (ED25519) to the list of known hosts.")

    monkeypatch.setattr(remote, "runner_from", runner_from)

    with pytest.raises(RemoteMachineError) as caught:
        remote.session_dir(_box(), root="/srv/x", handle="t1")

    assert str(caught.value) == "could not make a working directory under '/srv/x' on machine 'Lab box' (box)"


# --- any login shell ---------------------------------------------------------
#
# Review of #893: a machine user's login shell may be tcsh or csh (HPC
# clusters still hand them out), which cannot read ``${SHELL:-/bin/sh}``; and a
# profile that prints had its words glued to the agent's first frame. Each test
# runs what is sent the way sshd does for a user whose login shell that is. A
# shell this computer does not have is skipped: the Linux CI has no tcsh.

_SHELLS = ["/bin/sh", "/bin/dash", "/bin/bash", "/bin/zsh", "/bin/tcsh", "/bin/csh"]
_ARGS_PROBE = (
    "import json, os, sys; print(json.dumps({'argv': sys.argv[1:], 'cwd': os.getcwd(), 'odd': os.environ.get('ODD')}))"
)


def _shell(path: str) -> str:
    if not os.access(path, os.X_OK):
        pytest.skip(f"{path} is not on this computer")
    return path


def _as_user_of(shell: str, command: str, home: Path, stdin: bytes = b"\n") -> subprocess.CompletedProcess[bytes]:
    """What sshd does for a user whose login shell is ``shell``: ``shell -c`` the command, in their home."""
    return subprocess.run(
        [shell, "-c", command],
        capture_output=True,
        input=stdin,
        env={"HOME": str(home), "SHELL": shell, "PATH": "/usr/bin:/bin"},
        cwd=home,
        timeout=60,
    )


def _first_frame(stdout: bytes) -> object:
    """The first line that parses, read the way the client reads stdout: it skips any other line."""
    for line in stdout.decode("utf-8", "replace").splitlines():
        try:
            return json.loads(line)
        except ValueError:
            continue
    return None


@pytest.mark.parametrize("shell", _SHELLS)
def test_the_agent_starts_with_its_arguments_and_variables_whatever_the_login_shell(tmp_path, shell):
    shell = _shell(shell)
    agent = f"{shlex.quote(sys.executable)} -c {shlex.quote(_ARGS_PROBE)} 'bang!x' '$HOME' \"it's\""
    stdin = remote.env_preamble({"ODD": "a b'$c!d"}) + _FRAMES.encode()

    done = _as_user_of(shell, remote.remote_command(agent, root="~/raven work"), tmp_path, stdin)

    assert done.returncode == 0, done.stderr
    seen = _first_frame(done.stdout)
    assert isinstance(seen, dict), done.stdout
    assert seen["argv"] == ["bang!x", "$HOME", "it's"]
    assert seen["odd"] == "a b'$c!d"
    assert Path(seen["cwd"]).resolve() == (tmp_path / "raven work").resolve()


@pytest.mark.parametrize("shell", ["/bin/sh", "/bin/bash", "/bin/zsh", "/bin/tcsh", "/bin/csh"])
def test_what_a_profile_prints_never_joins_the_first_frame(tmp_path, shell):
    shell = _shell(shell)
    for rc in (".profile", ".bash_profile", ".bashrc", ".zprofile", ".zshrc"):
        (tmp_path / rc).write_text("printf 'Welcome to the cluster'\n", encoding="utf-8")
    (tmp_path / ".cshrc").write_text("echo -n 'Welcome to the cluster'\n", encoding="utf-8")
    frame = '{"jsonrpc":"2.0","id":1,"result":{}}'

    done = _as_user_of(shell, remote.remote_command(f"printf '%s\\n' {shlex.quote(frame)}", root="~"), tmp_path)

    assert done.returncode == 0, done.stderr
    lines = done.stdout.decode().splitlines()
    assert frame in lines, lines
    assert any("Welcome to the cluster" in line for line in lines), "the greeting is a line of its own, skipped"


def test_a_csh_user_s_agent_is_found_on_the_path_their_cshrc_sets(tmp_path):
    shell = _shell("/bin/tcsh")
    bin_dir = tmp_path / "agentbin"
    bin_dir.mkdir()
    agent = bin_dir / "fake-agent"
    agent.write_text("#!/bin/sh\necho found-by-cshrc-path\n", encoding="utf-8")
    agent.chmod(0o755)
    (tmp_path / ".cshrc").write_text("setenv PATH ${HOME}/agentbin:${PATH}\n", encoding="utf-8")

    done = _as_user_of(shell, remote.remote_command("fake-agent", root="~"), tmp_path)

    assert done.returncode == 0, done.stderr
    assert "found-by-cshrc-path" in done.stdout.decode()


def test_every_login_shell_the_local_launch_drives_is_driven_on_a_machine_too():
    from raven.agent.subagent.backends.env import _DRIVABLE_SHELLS

    assert set(_DRIVABLE_SHELLS) <= set(remote._LOGIN_SHELLS)
    assert not {"csh", "tcsh", "fish"} & set(remote._LOGIN_SHELLS), "they cannot read the launch's line"


@pytest.mark.parametrize("shell", _SHELLS)
def test_a_one_shot_command_reads_the_same_under_every_login_shell(tmp_path, shell):
    shell = _shell(shell)
    command = "printf '%s\\n' 'a!b' \"it's\" '$HOME' && [ ! -d /nonexistent ] && echo done"

    done = _as_user_of(shell, remote.posix(command), tmp_path)

    assert done.returncode == 0, done.stderr
    assert done.stdout.decode().splitlines() == ["", "a!b", "it's", "$HOME", "done"]


def test_the_session_directory_is_read_past_a_greeting_from_the_user_s_shell(tmp_path, monkeypatch):
    # bash reads .bashrc for a command sshd hands it and csh reads .cshrc for
    # every -c, before anything raven sent runs; a greeting with no line break
    # would turn the path into "greeting/root/raven-work/t1".
    def runner_from(row, *, cap_seconds=None):
        def run(cmd: str) -> tuple[int, str]:
            done = _run_as_sshd(f"printf 'greeting with no line break'; {cmd}", tmp_path)
            return done.returncode, done.stdout

        return run

    monkeypatch.setattr(remote, "runner_from", runner_from)

    got = remote.session_dir(_box(), root="~/raven-work", handle="t1")

    assert Path(got).resolve() == (tmp_path / "raven-work" / "t1").resolve()


def test_a_line_break_in_what_would_be_sent_is_refused_before_any_ssh(tmp_path, monkeypatch):
    with pytest.raises(ValueError, match="must be one line"):
        remote.posix("echo a\necho b")
    _registry(tmp_path, monkeypatch, [ROW])
    monkeypatch.setattr("raven.config.paths.get_logs_dir", lambda: tmp_path / "logs")
    monkeypatch.setattr(remote, "runner_from", lambda row, *, cap_seconds=None: pytest.fail("no ssh for this"))

    with pytest.raises(
        RemoteMachineError,
        match=r"^agent 'a' on machine 'Lab box' \(box\): a command for a registered machine must be one line$",
    ):
        remote.prepare_launch("a", "box", "agent\n--flag", remote_cwd=None, env={})
    with pytest.raises(RemoteMachineError, match=r"^machine 'Lab box' \(box\): .*must be one line$"):
        remote.session_dir(_box(), root="~/a\nb", handle="t1")


def test_what_is_sent_is_one_quoted_sh_line_with_csh_s_history_character_escaped():
    # The shape, pinned where no csh is installed to run it (the Linux CI).
    assert remote.posix("echo a!b") == "exec /bin/sh -c 'echo; echo a'\\!'b'"
    assert remote.remote_command("qwen --acp", root="~").startswith(
        "exec /bin/sh -c 'echo; mkdir -p ~ && cd ~ || exit; "
    )


def test_a_machine_answers_for_itself_but_never_for_the_way_onto_it():
    box = Machine(id="box", display_name="Lab box", _row={**ROW, "node_dir": "/srv/raven-node", "paths": ["/data"]})

    assert box.field("node_dir") == "/srv/raven-node"
    assert box.field("paths") == ["/data"]
    assert box.field("missing") is None
    for way_in in ("host", "port", "user", "key"):
        with pytest.raises(KeyError, match="how raven reaches the machine"):
            box.field(way_in)


# --- one command, and a file sent ----------------------------------------------------


def test_run_is_the_registry_s_own_runner_capped_at_the_timeout(monkeypatch):
    seen: dict = {}

    def runner_from(row, *, cap_seconds):
        seen.update(row=row, cap=cap_seconds)
        return lambda command: (0, f"ran {command}")

    monkeypatch.setattr(remote, "runner_from", runner_from)

    assert remote.run(_box(), "echo hi", timeout=12.0) == (0, f"ran {remote.posix('echo hi')}")
    assert seen == {"row": ROW, "cap": 12.0}


def test_run_on_a_row_the_runner_cannot_use_is_one_sentence(monkeypatch):
    from raven.ops.transport import TransportError

    def runner_from(row, *, cap_seconds):
        raise TransportError("no key to log in with")

    monkeypatch.setattr(remote, "runner_from", runner_from)

    with pytest.raises(RemoteMachineError, match=r"^machine 'Lab box' \(box\): no key to log in with$"):
        remote.run(_box(), "true")


def _ssh_here(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, body: str) -> Path:
    """An ``ssh`` on PATH that runs ``body`` with the remote command as ``$cmd``."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    ssh = bin_dir / "ssh"
    ssh.write_text(
        f'#!/bin/sh\nprintf "%s\\n" "$@" > "{tmp_path}/argv"\nfor cmd; do :; done\n' + body,
        encoding="utf-8",
    )
    ssh.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '/usr/bin:/bin')}")
    return tmp_path / "argv"


def test_push_hands_the_data_to_the_command_on_the_machine(tmp_path, monkeypatch):
    argv = _ssh_here(tmp_path, monkeypatch, 'sh -c "$cmd"\n')

    rc, out = remote.push(_box(), "wc -c && echo done", b"0123456789", timeout=30.0)

    assert (rc, out.split()) == (0, ["10", "done"])
    sent = argv.read_text(encoding="utf-8").splitlines()
    from raven.ops.transport import ssh_argv

    assert sent == ssh_argv(HOST, PORT, KEY, user="worker")[1:] + [remote.posix("wc -c && echo done")]


def test_a_push_that_failed_keeps_what_the_machine_said(tmp_path, monkeypatch):
    _ssh_here(tmp_path, monkeypatch, 'echo partial; echo "tar: Unexpected EOF in archive" >&2; exit 2\n')

    assert remote.push(_box(), "tar -xzf -", b"x") == (2, "partial\n\ntar: Unexpected EOF in archive\n")


def test_a_push_that_succeeded_drops_ssh_s_chatter(tmp_path, monkeypatch):
    _ssh_here(tmp_path, monkeypatch, f"echo \"Warning: Permanently added '[{HOST}]:{PORT}'\" >&2; echo ok\n")

    assert remote.push(_box(), "true", b"") == (0, "ok\n")


def test_a_push_that_runs_out_of_time_reads_as_timed_out(tmp_path, monkeypatch):
    from raven.ops.transport import TIMED_OUT_RC

    _ssh_here(tmp_path, monkeypatch, "sleep 5\n")

    assert remote.push(_box(), "true", b"", timeout=0.3) == (TIMED_OUT_RC, "")


def test_a_push_to_a_row_without_an_address_is_one_sentence():
    box = Machine(id="box", display_name="Lab box", _row={"id": "box"})

    with pytest.raises(RemoteMachineError, match=r"^machine 'Lab box' \(box\): "):
        remote.push(box, "true", b"")


def test_run_and_push_refuse_a_command_of_two_lines_before_any_ssh(monkeypatch):
    monkeypatch.setattr(remote, "runner_from", lambda row, *, cap_seconds=None: pytest.fail("no ssh for this"))
    monkeypatch.setattr(remote.subprocess, "run", lambda *a, **k: pytest.fail("no ssh for this"))

    for call in (lambda: remote.run(_box(), "echo a\necho b"), lambda: remote.push(_box(), "cat\ncat", b"")):
        with pytest.raises(RemoteMachineError, match=r"^machine 'Lab box' \(box\): .*must be one line$"):
            call()
