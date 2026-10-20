"""Reaching a machine that is this one.

Someone who writes their own solver runs it where they wrote it, and installs
raven on that box. There is then no host and no key, and the whole stack above
this -- a task statement, a declaration, a tool call -- has to read the same as
it does for a machine across the room.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
PLUGIN_DIR = REPO / "agents" / "raven-oncall" / "plugins" / "oncall-flow"
sys.path.insert(0, str(PLUGIN_DIR))

from oncall_flow.backend import JobBackendError  # noqa: E402
from oncall_flow.connections import resolve_into  # noqa: E402
from oncall_flow.transport import (
    LOCAL,
    SSH,
    TIMED_OUT_RC,
    make_local_runner,
    runner_from,
    transport_of,
)  # noqa: E402

from raven.ops.connections import password_path  # noqa: E402


def test_a_campaign_that_says_nothing_is_still_reached_over_ssh():
    # Every campaign written before this seam existed says nothing, and has to go
    # on meaning what it meant.
    assert transport_of({}) == SSH
    assert transport_of({"host": "h"}) == SSH
    assert transport_of({"transport": "local"}) == LOCAL
    assert transport_of({"transport": "LOCAL"}) == LOCAL


def test_a_local_runner_answers_in_the_same_shape_as_ssh():
    run = make_local_runner()

    rc, out = run("echo hi")
    assert (rc, out.strip()) == (0, "hi")

    rc, out = run("echo boom >&2; exit 3")
    assert rc == 3 and "boom" in out, "a failure has to carry what was said on stderr"

    rc, out = run("printf 'a\\nb\\n' | wc -l")
    assert rc == 0 and out.strip() == "2", "pipes and redirects are what remote commands use"


def test_the_local_cap_does_not_depend_on_the_timeout_binary():
    # `timeout` is GNU coreutils and macOS does not ship it, which is exactly the
    # machine a local connection is most likely to be. Measured 2026-08-19: the
    # first local look came back `exit 127, /bin/sh: timeout: command not found`.
    rc, _ = make_local_runner(cap_seconds=0.3)("sleep 5")

    assert rc == TIMED_OUT_RC, "the cap has to hold with no coreutils on the box"


def test_a_local_connection_needs_no_address():
    run = runner_from({"connection": "conn_here", "transport": "local"})

    rc, out = run("echo hi")
    assert (rc, out.strip()) == (0, "hi")


def test_a_named_connection_with_no_address_says_which_one():
    # Distinct from "this machine has no address by design": a connection the
    # registry no longer has leaves nothing to fill the address in, and the
    # failure used to reach the owner as their machine refusing the connection.
    with pytest.raises(JobBackendError) as exc:
        runner_from({"connection": "conn_gone"})

    assert "conn_gone" in str(exc.value) and "not in the connection registry" in str(exc.value)


def test_a_meta_with_neither_is_left_alone_rather_than_newly_refused():
    """The hand-written shape that predates connections still builds a runner.

    Refusing it here would be a new rule applied to campaigns that were set up
    before any of this existed. They fail where they always did -- at the staging
    step -- and narrowing this guard to a *named* connection is what keeps the
    change additive.
    """
    assert callable(runner_from({}, what="machine"))


# ---- a machine registered with a password: logged in to with the one kept for it ----

PASSWORD_ROW = {
    "id": "lab-box",
    "display_name": "the lab box",
    "transport": "ssh",
    "host": "203.0.113.7",
    "port": 2222,
    "user": "alice",
    "auth": "password",
}

# An ssh that logs what it was handed, one JSON line a call, and asks the
# askpass helper the way OpenSSH does under SSH_ASKPASS_REQUIRE=force. Its
# ``-G``, the config dump a password login reads first, names the host as
# given and is no call.
_STAND_IN_SSH = """#!{python}
import json, os, subprocess, sys
if "-G" in sys.argv[1:]:
    print("hostname " + sys.argv[-1].rpartition("@")[2])
    sys.exit(0)
saw = {{
    "argv": sys.argv[1:],
    "askpass": os.environ.get("SSH_ASKPASS", ""),
    "require": os.environ.get("SSH_ASKPASS_REQUIRE", ""),
    "file": os.environ.get("RAVEN_MACHINE_PASSWORD_FILE", ""),
}}
if saw["require"] == "force" and saw["askpass"]:
    prompt = os.environ.get("RAVEN_MACHINE_PASSWORD_FOR", "") + "'s password: "
    asked = subprocess.run([saw["askpass"], prompt], capture_output=True, text=True)
    saw["given"] = asked.stdout.rstrip("\\n")
with open({log!r}, "a", encoding="utf-8") as fh:
    fh.write(json.dumps(saw) + "\\n")
"""


@pytest.fixture
def password_machine(tmp_path, monkeypatch):
    """A machine registered by password the way trunk registers one, in a scratch registry and home."""
    from raven.ops import connection_add

    monkeypatch.setenv("RAVEN_CONNECTIONS", str(tmp_path / "connections.json"))
    monkeypatch.setenv("RAVEN_HOME", str(tmp_path / "rhome"))
    connection_add.write(dict(PASSWORD_ROW))
    return connection_add


def _stand_in_ssh(tmp_path, monkeypatch) -> Path:
    """Put the logging ssh first on PATH; returns where it logs."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    log = tmp_path / "ssh-saw.jsonl"
    script = bin_dir / "ssh"
    script.write_text(_STAND_IN_SSH.format(python=sys.executable, log=str(log)), encoding="utf-8")
    script.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    for name in ("SSH_ASKPASS", "SSH_ASKPASS_REQUIRE", "RAVEN_MACHINE_PASSWORD_FILE"):
        monkeypatch.delenv(name, raising=False)
    return log


def _calls(log: Path) -> list[dict]:
    return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]


@pytest.mark.skipif(
    sys.platform == "win32", reason="password machines are refused on Windows; the stand-in ssh is a POSIX script"
)
def test_a_password_machine_is_logged_in_to_through_askpass_and_offered_no_key(password_machine, tmp_path, monkeypatch):
    """Reviewed 2026-10-19: a campaign on a machine registered with a password
    resolved to a meta like any key machine's, so its first ssh offered
    ``~/.ssh/id_rsa`` under ``BatchMode=yes`` with no askpass in reach, and
    the machine refused it. The meta now says how the machine is logged in
    to, and trunk's login -- the one every other way onto it uses -- does it."""
    password_machine.store_password("lab-box", "hunter2")
    log = _stand_in_ssh(tmp_path, monkeypatch)

    rc, _ = runner_from(resolve_into({"connection": "lab-box"}))("hostname")

    assert rc == 0
    (saw,) = _calls(log)
    argv = saw["argv"]
    assert "-i" not in argv and "BatchMode=yes" not in argv
    for option in (
        "PubkeyAuthentication=no",
        "PreferredAuthentications=password,keyboard-interactive",
        "NumberOfPasswordPrompts=1",
    ):
        assert option in argv, option
    assert argv[-2:] == ["alice@203.0.113.7", "hostname"] and argv[argv.index("-p") + 1] == "2222"
    assert saw["require"] == "force" and saw["askpass"]
    assert saw["file"] == str(password_path("lab-box"))
    assert saw["given"] == "hunter2" and "hunter2" not in " ".join(argv)


@pytest.mark.skipif(
    sys.platform == "win32", reason="Windows refuses a password machine before looking for its password"
)
def test_a_password_machine_with_no_password_kept_here_is_refused_by_name_before_ssh_starts(password_machine):
    with pytest.raises(JobBackendError, match="no password is kept here for machine 'lab-box'.*register it again"):
        runner_from(resolve_into({"connection": "lab-box"}))


@pytest.mark.skipif(sys.platform == "win32", reason="the stand-in ssh is a POSIX script")
@pytest.mark.parametrize(
    "meta",
    [
        {"connection": "lab-box", "host": "198.51.100.99"},
        {"connection": "lab-box", "host": "203.0.113.7", "port": 22},
        {"connection": "lab-box", "host": "203.0.113.7", "port": 2222, "user": "root"},
    ],
    ids=["other-host", "other-port", "other-user"],
)
def test_a_password_meta_that_carries_another_address_or_account_is_refused_before_ssh_starts(
    password_machine, tmp_path, monkeypatch, meta
):
    """The login row took host, port and user from the meta, which the model's own tool calls write, so a
    campaign could send a machine's kept password to a host of its choosing. The address is the registry's
    alone, and a meta that says otherwise for a password connection is refused by name."""
    password_machine.store_password("lab-box", "hunter2")
    log = _stand_in_ssh(tmp_path, monkeypatch)

    with pytest.raises(JobBackendError, match="goes only to the machine it was registered for") as exc:
        runner_from(resolve_into(dict(meta)))

    assert "198.51.100.99" not in str(exc.value)
    assert not log.exists(), "ssh never ran"


def test_a_password_meta_naming_a_connection_the_registry_lost_is_refused_by_name(password_machine):
    with pytest.raises(JobBackendError, match="not in the connection registry"):
        runner_from({"connection": "gone-box", "auth": "password", "host": "203.0.113.7"})


def test_a_password_meta_that_names_no_connection_is_refused_by_name():
    """The password is kept for a registered machine, under its id, so a meta
    that says password and names no machine has nothing to log in with."""
    with pytest.raises(JobBackendError, match="names no connection"):
        runner_from({"host": "203.0.113.7", "auth": "password"})


@pytest.mark.skipif(sys.platform == "win32", reason="the stand-in ssh is a POSIX script")
def test_a_key_machine_is_logged_in_to_exactly_as_before(tmp_path, monkeypatch):
    log = _stand_in_ssh(tmp_path, monkeypatch)

    runner_from({"host": "203.0.113.7", "port": 2222, "user": "alice", "key": "/k"})("hostname")

    (saw,) = _calls(log)
    assert saw["argv"] == [
        "-i",
        "/k",
        "-p",
        "2222",
        "-o",
        "BatchMode=yes",
        "-o",
        "ConnectTimeout=15",
        "-o",
        "StrictHostKeyChecking=accept-new",
        "alice@203.0.113.7",
        "hostname",
    ]
    assert (saw["askpass"], saw["require"], saw["file"]) == ("", "", "")
