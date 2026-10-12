"""raven.acp_client.remote: the machine, the ssh line that carries its node, and the failure sentences.

The quoting tests run what is sent through real shells on this computer
(``sh -c`` standing in for sshd, each login shell for a machine user's) with
``HOME`` pointed at a temp dir, so a developer's own rc files take no part.
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


@pytest.fixture(autouse=True)
def _lane_on(monkeypatch: pytest.MonkeyPatch) -> None:
    """These tests are about reaching a machine; the switch that allows it is tested on its own."""
    monkeypatch.setenv("RAVEN_DEBUG_MACHINES", "1")


def _registry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, rows: list | str) -> None:
    path = tmp_path / "connections.json"
    path.write_text(rows if isinstance(rows, str) else json.dumps({"connections": rows}), encoding="utf-8")
    monkeypatch.setenv("RAVEN_CONNECTIONS", str(path))


def _box() -> Machine:
    return Machine(id="box", display_name="Lab box", _row=dict(ROW))


# Prints what the far side would see: its argv and its cwd.
_PROBE = "import json, os, sys; print(json.dumps({'argv': sys.argv[1:], 'cwd': os.getcwd()}))"


# What is sent is run the way sshd runs it, under a POSIX shell; a host
# without one (Windows) keeps the tests of what is sent and what is said.
_posix_shell = pytest.mark.skipif(not Path("/bin/sh").exists(), reason="runs what is sent under /bin/sh")


def _run_as_sshd(command: str, home: Path) -> subprocess.CompletedProcess[str]:
    """What sshd does with the one argv element it is sent: ``$SHELL -c`` it, in the user's home."""
    env = {"HOME": str(home), "SHELL": "/bin/sh", "PATH": os.environ.get("PATH", "/usr/bin:/bin")}
    return subprocess.run(
        ["/bin/sh", "-c", command],
        capture_output=True,
        text=True,
        input="",
        env=env,
        cwd=home,
        timeout=60,
    )


# --- the ssh line -------------------------------------------------------------


def test_the_ssh_command_reaches_the_machine_with_the_registry_s_options_and_stays_open(tmp_path: Path):
    log = tmp_path / "box.log"
    argv = remote.ssh_command(_box(), "node --stdio", ssh_log=log)

    assert isinstance(argv, list) and argv[0] == "ssh"
    assert argv[argv.index("-i") + 1] == KEY
    assert argv[argv.index("-p") + 1] == str(PORT)
    assert "BatchMode=yes" in argv
    assert "StrictHostKeyChecking=accept-new" in argv
    # Kept open for the life of the node: no tty, and keepalives under the
    # node's own heartbeat so a machine that drops off the network is noticed.
    assert "-T" in argv
    assert "ServerAliveInterval=15" in argv
    assert "ServerAliveCountMax=3" in argv
    # ssh's own messages go to their log, not to the stderr the connection quotes.
    assert argv[argv.index("-E") + 1] == str(log)
    dest = argv.index(f"worker@{HOST}")
    assert argv[dest + 1] == "--"
    assert len(argv) == dest + 3, "the remote command is one argv element"
    # Options come before the destination, where ssh reads them.
    assert argv.index("-T") < dest and argv.index("-E") < dest


def test_the_ssh_command_carries_no_variable_and_reads_no_profile(tmp_path: Path):
    # The node captures the login environment itself, for the agents it
    # starts; what carries it is one plain line any login shell reads.
    argv = remote.ssh_command(_box(), "~/.raven-node/x/bin/python -I -m raven.node --stdio", ssh_log=tmp_path / "l")

    sent = argv[-1]
    line = shlex.join(argv)
    assert sent == "exec /bin/sh -c 'echo; ~/.raven-node/x/bin/python -I -m raven.node --stdio'"
    assert "-lic" not in line and "mkdir" not in line and "read -r" not in line


@_posix_shell
def test_the_ssh_command_s_command_runs_with_its_arguments_intact_from_the_login_directory(tmp_path: Path):
    # The trap measured 2026-10-10: `ssh host -- bash -lic 'qwen --acp'` loses
    # `--acp` to the far shell's re-parse and starts qwen interactive.
    command = f"{shlex.quote(sys.executable)} -c {shlex.quote(_PROBE)} --stdio 'two words'"
    sent = remote.ssh_command(_box(), command, ssh_log=tmp_path / "box.log")[-1]

    done = _run_as_sshd(sent, tmp_path)

    assert done.returncode == 0, done.stderr
    lines = done.stdout.splitlines()
    assert lines[0] == "", "the far side's output starts on a line of its own"
    seen = json.loads(lines[-1])
    assert seen["argv"] == ["--stdio", "two words"]
    assert Path(seen["cwd"]).resolve() == tmp_path.resolve(), "where the login lands; nothing is made"


def test_each_agent_gets_an_owner_only_ssh_log(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr("raven.config.paths.get_logs_dir", lambda: tmp_path / "logs")

    first = remote.ssh_log_path("raven-node@box")
    second = remote.ssh_log_path("raven-node@other")

    assert first != second and first.parent == second.parent == tmp_path / "logs" / "ssh"
    assert first.parent.stat().st_mode & 0o777 == 0o700
    assert remote.read_ssh_log(first) == ""
    first.write_text("ssh: connect to host x port 1: Operation timed out\n", encoding="utf-8")
    assert "timed out" in remote.read_ssh_log(first)


def test_an_entry_that_lost_its_address_is_refused_by_name_not_as_a_network_fault(tmp_path: Path):
    # machine() refuses such a row up front; this is the guard behind it, for a
    # Machine built from a row that changed after it was read.
    bare = Machine(id="box", display_name="Lab box", _row={"id": "box"})

    with pytest.raises(RemoteMachineError, match=r"'Lab box' \(box\): .*has no address"):
        remote.ssh_command(bare, "node", ssh_log=tmp_path / "l")
    with pytest.raises(RemoteMachineError, match=r"'Lab box' \(box\): .*has no address"):
        remote.push(bare, "true", b"")


def test_a_line_break_in_what_would_be_sent_is_refused_before_any_ssh(tmp_path: Path):
    with pytest.raises(ValueError, match="must be one line"):
        remote.posix("echo a\necho b")
    with pytest.raises(ValueError, match="must be one line"):
        remote.ssh_command(_box(), "node\n--stdio", ssh_log=tmp_path / "l")


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


def test_a_machine_named_only_by_its_id_is_called_that():
    assert Machine(id="box", display_name="box", _row={}).label == "'box'"


def test_a_machine_answers_for_itself_but_never_for_the_way_onto_it():
    box = Machine(id="box", display_name="Lab box", _row={**ROW, "node_dir": "/srv/raven-node", "paths": ["/data"]})

    assert box.field("node_dir") == "/srv/raven-node"
    assert box.field("paths") == ["/data"]
    assert box.field("missing") is None
    for way_in in ("host", "port", "user", "key"):
        with pytest.raises(KeyError, match="how raven reaches the machine"):
            box.field(way_in)


# --- where an agent works on its machine, as written ----------------------------------------


@pytest.mark.parametrize(
    ("remote_cwd", "want"),
    [
        (None, "~/raven-work"),
        ("", "~/raven-work"),
        ("  ", "~/raven-work"),
        ("~/raven-work/", "~/raven-work"),
        ("/srv/agents/", "/srv/agents"),
        ("~", "~"),
        ("/", "/"),
    ],
)
def test_the_root_is_the_entry_s_or_the_default(remote_cwd, want):
    assert remote.root_of(remote_cwd) == want


@pytest.mark.parametrize(
    ("root", "handle", "want"),
    [
        ("~/raven-work", "t1", "~/raven-work/t1"),
        (None, "reviewer", "~/raven-work/reviewer"),
        ("", "t1", "~/raven-work/t1"),
        ("/srv/agents/", "t1", "/srv/agents/t1"),
        ("~", "t1", "~/t1"),
        ("/", "t1", "/t1"),
    ],
)
def test_the_session_root_is_the_root_and_the_handle_as_written(root, handle, want):
    assert remote.session_root(root, handle) == want


def test_the_session_root_uses_the_safe_leaf_of_a_handle():
    assert remote.session_root("~/raven-work", "a b") == f"~/raven-work/{remote.leaf('a b')}"


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


def test_a_missing_agent_is_one_sentence_that_points_at_the_machine():
    assert remote.agent_missing(_box()) == (
        "the agent's command was not found on machine 'Lab box' (box); "
        "install it there, for the user the machine is registered with"
    )


def _ended(rc: int | None, stderr: str = "") -> Exception:
    from raven.acp_client.protocol import AcpConnectionError

    return AcpConnectionError("gone", stderr=stderr, returncode=rc)


@pytest.mark.parametrize(
    ("rc", "log", "stderr", "said"),
    [
        # ssh's own failures, in its own words.
        (255, f"ssh: connect to host {HOST} port {PORT}: Connection refused\n", "", "could not be reached"),
        (255, f"worker@{HOST}: Permission denied (publickey).\n", "", "refused the login"),
        (255, "client_loop: send disconnect: Broken pipe\n", "", "ended before the agent answered"),
        # ssh got through and said nothing: 255 is the far side's own exit.
        (255, "", "FATAL: config file is unreadable\n", None),
        (255, f"Warning: Permanently added '[{HOST}]:{PORT}' (ED25519) to the list of known hosts.\n", "", None),
        # The far shell could not find the command, in its own words; a
        # process exiting 127 for a reason of its own is left to its words.
        (127, "", "bash: line 1: exec: qwen: not found\n", "was not found on machine"),
        (127, "", "sh: 1: exec: /opt/agent: cannot execute\n", "was not found on machine"),
        (127, "", "module loader exited 127\n", None),
        # 124 is the one-shot runner's cap, which a launch does not have.
        (124, "", "", None),
        (1, "", "boom\n", None),
        (None, "", "", None),
    ],
)
def test_a_failure_is_ssh_s_only_when_ssh_said_so(tmp_path, rc, log, stderr, said):
    path = tmp_path / "box.log"
    path.write_text(log, encoding="utf-8")

    got = remote.failure(_box(), path, _ended(rc, stderr))

    if said is None:
        assert got is None
    else:
        assert got is not None and said in got
        assert HOST not in got


def test_a_failure_with_no_log_to_read_is_judged_on_the_exit_alone(tmp_path):
    assert remote.failure(_box(), tmp_path / "never-written.log", _ended(255)) is None
    assert remote.failure(_box(), tmp_path / "never-written.log", _ended(124)) is None


# --- leaf ----------------------------------------------------------------------


@pytest.mark.parametrize("handle", ["reviewer-1", "t_0123abcd", "v2.final"])
def test_a_safe_handle_is_its_own_directory_name(handle):
    assert remote.leaf(handle) == handle


@pytest.mark.parametrize("handle", ["a b", "../up", "..", "", "x/y", "\u540d\u5b57"])
def test_an_unsafe_handle_becomes_one_component_that_cannot_climb(handle):
    got = remote.leaf(handle)

    assert "/" not in got and not got.startswith(".") and got
    assert remote.leaf(handle) == got, "the same handle always lands in the same place"


# --- any login shell ---------------------------------------------------------
#
# A machine user's login shell may be tcsh or csh (HPC clusters still hand
# them out), which cannot read ``${SHELL:-/bin/sh}``; and a profile that
# prints had its words glued to the first line read. Each test runs what is
# sent the way sshd does for a user whose login shell that is. A shell this
# computer does not have is skipped: the Linux CI has no tcsh.

_SHELLS = ["/bin/sh", "/bin/dash", "/bin/bash", "/bin/zsh", "/bin/tcsh", "/bin/csh"]


def _shell(path: str) -> str:
    if not os.access(path, os.X_OK):
        pytest.skip(f"{path} is not on this computer")
    return path


def _as_user_of(shell: str, command: str, home: Path) -> subprocess.CompletedProcess[bytes]:
    """What sshd does for a user whose login shell is ``shell``: ``shell -c`` the command, in their home."""
    return subprocess.run(
        [shell, "-c", command],
        capture_output=True,
        input=b"",
        env={"HOME": str(home), "SHELL": shell, "PATH": "/usr/bin:/bin"},
        cwd=home,
        timeout=60,
    )


@_posix_shell
@pytest.mark.parametrize("shell", _SHELLS)
def test_a_command_reads_the_same_under_every_login_shell(tmp_path, shell):
    shell = _shell(shell)
    # A directory the test knows is absent: /nonexistent exists on some machines (nobody's home).
    absent = shlex.quote(str(tmp_path / "absent"))
    command = f"printf '%s\\n' 'a!b' \"it's\" '$HOME' && [ ! -d {absent} ] && echo done"

    done = _as_user_of(shell, remote.posix(command), tmp_path)

    assert done.returncode == 0, done.stderr
    assert done.stdout.decode().splitlines() == ["", "a!b", "it's", "$HOME", "done"]


@_posix_shell
@pytest.mark.parametrize("shell", ["/bin/sh", "/bin/bash", "/bin/zsh", "/bin/tcsh", "/bin/csh"])
def test_what_a_profile_prints_never_joins_the_first_frame(tmp_path, shell):
    shell = _shell(shell)
    for rc in (".profile", ".bash_profile", ".bashrc", ".zprofile", ".zshrc"):
        (tmp_path / rc).write_text("printf 'Welcome to the cluster'\n", encoding="utf-8")
    (tmp_path / ".cshrc").write_text("echo -n 'Welcome to the cluster'\n", encoding="utf-8")
    frame = '{"jsonrpc":"2.0","id":1,"result":{}}'

    done = _as_user_of(shell, remote.posix(f"printf '%s\\n' {shlex.quote(frame)}"), tmp_path)

    assert done.returncode == 0, done.stderr
    lines = done.stdout.decode().splitlines()
    # bash and zsh read their rc files only for a command sshd itself hands
    # them, csh for every -c; whichever printed, the frame is a line of its own.
    assert frame in lines, lines
    assert lines[0] in ("", "Welcome to the cluster"), lines


def test_what_is_sent_is_one_quoted_sh_line_with_csh_s_history_character_escaped():
    # The shape, pinned where no csh is installed to run it (the Linux CI).
    assert remote.posix("echo a!b") == "exec /bin/sh -c 'echo; echo a'\\!'b'"


# --- the lane is debug-only ------------------------------------------------------------


def test_with_the_lane_off_a_machine_is_refused_before_the_registry_is_read(tmp_path, monkeypatch):
    _registry(tmp_path, monkeypatch, [ROW])
    monkeypatch.delenv("RAVEN_DEBUG_MACHINES")
    monkeypatch.setattr(remote.connections, "read", lambda: pytest.fail("the registry was read"))

    with pytest.raises(RemoteMachineError) as caught:
        remote.machine("box")

    assert str(caught.value) == (
        "machine 'box': the machine lane is debug-only and off; start raven with RAVEN_DEBUG_MACHINES=1 "
        "to use agents and files on registered machines"
    )


@pytest.mark.parametrize("value", ["1", "true", "YES", " on "])
def test_the_lane_switch_reads_the_usual_spellings(monkeypatch, value):
    from raven.home import machine_lane_enabled

    monkeypatch.setenv("RAVEN_DEBUG_MACHINES", value)
    assert machine_lane_enabled()
    monkeypatch.setenv("RAVEN_DEBUG_MACHINES", "0")
    assert not machine_lane_enabled()


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


@_posix_shell
def test_push_hands_the_data_to_the_command_on_the_machine(tmp_path, monkeypatch):
    argv = _ssh_here(tmp_path, monkeypatch, 'sh -c "$cmd"\n')

    rc, out = remote.push(_box(), "wc -c && echo done", b"0123456789", timeout=30.0)

    assert (rc, out.split()) == (0, ["10", "done"])
    sent = argv.read_text(encoding="utf-8").splitlines()
    from raven.ops.transport import ssh_argv

    assert sent == ssh_argv(HOST, PORT, KEY, user="worker")[1:] + [remote.posix("wc -c && echo done")]


@_posix_shell
def test_a_push_that_failed_keeps_what_the_machine_said(tmp_path, monkeypatch):
    _ssh_here(tmp_path, monkeypatch, 'echo partial; echo "tar: Unexpected EOF in archive" >&2; exit 2\n')

    assert remote.push(_box(), "tar -xzf -", b"x") == (2, "partial\n\ntar: Unexpected EOF in archive\n")


@_posix_shell
def test_a_push_that_succeeded_drops_ssh_s_chatter(tmp_path, monkeypatch):
    _ssh_here(tmp_path, monkeypatch, f"echo \"Warning: Permanently added '[{HOST}]:{PORT}'\" >&2; echo ok\n")

    assert remote.push(_box(), "true", b"") == (0, "ok\n")


@_posix_shell
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


# --- what the file tools read back ------------------------------------------------------


def test_a_path_on_the_machine_is_shown_the_way_scp_writes_it():
    assert (
        remote.shown(_box(), "/home/worker/raven-work/t1") == f"scp://worker@{HOST}:{PORT}//home/worker/raven-work/t1"
    )


def test_the_files_note_names_what_came_with_the_task_by_address_and_nothing_else():
    from types import SimpleNamespace

    target = SimpleNamespace(label="'Lab box' (box)")
    media = [
        SimpleNamespace(path="/Users/me/sales.csv"),
        SimpleNamespace(path=""),
        SimpleNamespace(path="/tmp/a b.png"),
    ]

    note = remote.files_note(media, target)

    assert note.startswith("Files that came with this task, by their address on the computer that sent it.")
    assert "Nothing was copied here" in note and "say so in your reply and go on" in note
    assert note.endswith("- /Users/me/sales.csv\n- /tmp/a b.png")
    assert "machine 'Lab box' (box)" in note
    assert remote.files_note([], target) == "" and remote.files_note([SimpleNamespace(path=None)], target) == ""
