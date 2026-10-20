"""raven.ops.transport: the one spelling of the ssh command line.

The one-shot runner and an ACP agent on a machine both build on ``ssh_argv``,
so these pin what the runner sent before the two shared it.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from raven.ops import transport
from raven.ops.transport import ISOLATE_IDENTITY, TransportError, make_ssh_runner, ssh_argv, ssh_target

pytestmark = pytest.mark.usefixtures("no_real_ssh_g")

BASE = [
    "ssh",
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
]


def test_the_runner_sends_what_it_always_sent(monkeypatch):
    sent: list[list[str]] = []

    def run(argv, **kwargs):
        sent.append(argv)
        # Its own stdin: under ``raven acp`` the caller's carries ACP frames,
        # and a one-shot ssh that inherited it swallowed them.
        assert kwargs["stdin"] is subprocess.DEVNULL
        return subprocess.CompletedProcess(argv, 0, "out", "")

    monkeypatch.setattr(transport.subprocess, "run", run)

    assert make_ssh_runner("h", 2222, "/k", user="u")("true") == (0, "out")
    assert make_ssh_runner("h", 2222, "/k", user="u", identities_only=True)("true") == (0, "out")

    assert sent[0] == [*BASE, "u@h", "true"]
    assert sent[1] == [*BASE, *ISOLATE_IDENTITY, "u@h", "true"]


def test_extra_options_go_before_the_destination_where_ssh_reads_them():
    argv = ssh_argv("h", 2222, "/k", user="u", extra=("-T", "-o", "ServerAliveInterval=15"))

    assert argv == [*BASE, "-T", "-o", "ServerAliveInterval=15", "u@h"]


def test_a_row_fills_in_the_registry_s_defaults():
    host, port, key, user = ssh_target({"id": "m", "host": " h "})

    assert (host, port, user) == ("h", 22, "root")
    assert key.endswith("/.ssh/id_rsa") and not key.startswith("~")


def test_a_row_with_no_host_is_refused_by_name():
    with pytest.raises(TransportError, match="'m' has no address"):
        ssh_target({"id": "m", "port": 22})


# ---- a machine registered with a password: askpass answers for ssh, every connection ----

KEY_ROW = {"id": "box", "transport": "ssh", "host": "203.0.113.7", "port": 2222, "user": "alice", "key": "/k"}
PASSWORD_ROW = {
    "id": "box",
    "transport": "ssh",
    "host": "203.0.113.7",
    "port": 2222,
    "user": "alice",
    "auth": "password",
}


def test_a_password_row_s_argv_offers_no_key_and_leaves_the_asking_to_askpass():
    argv = ssh_argv("203.0.113.7", 2222, "", user="alice", password=True)

    assert "-i" not in argv and "BatchMode=yes" not in argv
    assert argv[-1] == "alice@203.0.113.7"
    for option in (
        "PubkeyAuthentication=no",
        "PreferredAuthentications=password,keyboard-interactive",
        "NumberOfPasswordPrompts=1",
        "-o",
        "ControlMaster=no",
        "-o",
        "ControlPath=none",
        # A jump host's own ssh inherits the helper; it would be sent this machine's password.
        "-o",
        "ProxyJump=none",
        "-o",
        "ProxyCommand=none",
    ):
        assert option in argv, option
    assert "StrictHostKeyChecking=accept-new" in argv and "ConnectTimeout=15" in argv
    # A key row's line is what it always was.
    assert ssh_argv("203.0.113.7", 2222, "/k", user="alice") == BASE + ["alice@203.0.113.7"]


@pytest.mark.skipif(os.name == "nt", reason="POSIX file modes are not available on Windows")
def test_the_askpass_environment_names_the_helper_and_the_password_file_never_the_password(
    tmp_path, monkeypatch, supported_host
):
    from raven.ops.connection_add import store_password

    monkeypatch.setenv("RAVEN_HOME", str(tmp_path / "rhome"))
    monkeypatch.delenv("DISPLAY", raising=False)
    store_password("box", "hunter2")
    _owner_ssh_config(tmp_path, monkeypatch, "")

    env = transport.ssh_env(PASSWORD_ROW)

    helper = Path(env["SSH_ASKPASS"])
    assert helper == tmp_path / "rhome" / "keys" / "askpass" and oct(helper.stat().st_mode & 0o777) == "0o700"
    assert env["SSH_ASKPASS_REQUIRE"] == "force" and env["DISPLAY"]
    assert env["RAVEN_MACHINE_PASSWORD_FILE"] == str(tmp_path / "rhome" / "keys" / "box.password")
    assert env["RAVEN_MACHINE_PASSWORD_FOR"] == f"{PASSWORD_ROW['user']}@{PASSWORD_ROW['host']}"
    assert "hunter2" not in "".join(env.values())
    # The helper answers ssh with the file's line, at the registered account's own prompt.
    out = subprocess.run(
        [str(helper), f"{env['RAVEN_MACHINE_PASSWORD_FOR']}'s password: "],
        capture_output=True,
        text=True,
        env={**os.environ, **env},
    )
    assert out.stdout == "hunter2\n", out.stderr
    assert transport.ssh_env(KEY_ROW) == {}
    before = helper.read_text(encoding="utf-8")
    assert transport.ssh_env(PASSWORD_ROW)["SSH_ASKPASS"] == str(helper), "made once; found as it is after"
    assert helper.read_text(encoding="utf-8") == before


def test_a_password_row_with_no_password_kept_here_is_refused_by_name(tmp_path, monkeypatch, supported_host):
    """And told the way back that works: a registration under an id in use is refused, so the machine goes first."""
    monkeypatch.setenv("RAVEN_HOME", str(tmp_path / "rhome"))

    with pytest.raises(
        TransportError, match=r"no password is kept here for machine 'box' \(.*\); remove the machine and register it"
    ):
        transport.ssh_env(PASSWORD_ROW)
    with pytest.raises(TransportError, match="no password is kept here"):
        transport.runner_from(PASSWORD_ROW)


def test_a_password_row_is_refused_on_windows_before_ssh_is_started(tmp_path, monkeypatch):
    from raven.ops.connection_add import store_password

    monkeypatch.setenv("RAVEN_HOME", str(tmp_path / "rhome"))
    store_password("box", "hunter2")
    monkeypatch.setattr(sys, "platform", "win32")

    with pytest.raises(TransportError, match="Windows.*use a key"):
        transport.ssh_env(PASSWORD_ROW)


def test_the_runner_hands_ssh_the_askpass_environment_for_a_password_row_and_nothing_for_a_key_row(
    tmp_path, monkeypatch, supported_host
):
    from raven.ops.connection_add import store_password

    monkeypatch.setenv("RAVEN_HOME", str(tmp_path / "rhome"))
    store_password("box", "hunter2")
    sent: list[tuple[list[str], dict | None, bool]] = []

    def run(argv, **kwargs):
        if "-G" in argv:
            # How ssh resolves the host for the helper's prompt: a config dump, not a connection.
            return subprocess.CompletedProcess(argv, 0, "hostname 203.0.113.7\n", "")
        sent.append((argv, kwargs.get("env"), kwargs.get("start_new_session", False)))
        return subprocess.CompletedProcess(argv, 0, "out", "")

    monkeypatch.setattr(transport.subprocess, "run", run)

    assert transport.runner_from(PASSWORD_ROW)("true") == (0, "out")
    assert transport.runner_from(KEY_ROW)("true") == (0, "out")

    (argv_pw, env_pw, session_pw), (argv_key, env_key, session_key) = sent
    assert env_pw["SSH_ASKPASS_REQUIRE"] == "force" and env_pw["RAVEN_MACHINE_PASSWORD_FILE"].endswith("box.password")
    # No controlling terminal, so an OpenSSH older than 8.4 reaches the helper instead of prompting.
    assert session_pw is True and session_key is False, "a key connection keeps the caller's session"
    assert "PubkeyAuthentication=no" in argv_pw and "-i" not in argv_pw and "hunter2" not in " ".join(argv_pw)
    assert env_key is None and "-i" in argv_key


def test_a_password_row_whose_id_no_password_could_be_kept_under_is_refused_the_one_way(tmp_path, monkeypatch):
    """Hand-written registry, id the doctor blocks: a caller that reaches it anyway gets a TransportError, not a crash."""
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setenv("RAVEN_HOME", str(tmp_path / "rhome"))
    with pytest.raises(TransportError, match="not an id a password can be kept under"):
        transport.ssh_env({**PASSWORD_ROW, "id": "../escape"})


# ---- the askpass helper answers a prompt for the password, and no other ----

_posix_helper = pytest.mark.skipif(sys.platform == "win32", reason="the askpass helper is a POSIX shell script")


def _askpass(tmp_path: Path, prompt: str) -> subprocess.CompletedProcess[str]:
    """What the askpass helper hands ssh for ``prompt``, with ``hunter2`` kept for the machine."""
    keys = tmp_path / "keys"
    helper = transport.askpass_helper(keys)
    secret = keys / "box.password"
    secret.write_text("hunter2\n", encoding="utf-8")
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "RAVEN_MACHINE_PASSWORD_FILE": str(secret),
        "RAVEN_MACHINE_PASSWORD_FOR": "alice@203.0.113.7",
    }
    return subprocess.run(["sh", str(helper), prompt], capture_output=True, text=True, env=env, timeout=30)


@_posix_helper
@pytest.mark.parametrize(
    "prompt",
    ["alice@203.0.113.7's password: ", "(alice@203.0.113.7) Password: ", "ALICE@203.0.113.7's Password:"],
    ids=["password-method", "keyboard-interactive", "case-and-blank"],
)
def test_the_askpass_helper_answers_the_registered_account_s_own_password_prompt(tmp_path, prompt):
    done = _askpass(tmp_path, prompt)

    assert (done.returncode, done.stdout) == (0, "hunter2\n"), done.stderr


@_posix_helper
@pytest.mark.parametrize(
    "prompt",
    [
        "root@203.0.113.7's password: ",
        "other@198.51.100.7's password: ",
        "(root@203.0.113.7) Password: ",
        "Password: ",
        "(alice@box) Passwort: ",
        "(alice@203.0.113.7) One-time password: ",
        "One-time password (OATH) for alice: ",
        "Password 042: ",
        "Password & verification code: ",
        "(alice@203.0.113.7) Verification code: ",
        "(alice@203.0.113.7) Passcode or option (1-3): ",
        "Verification code: ",
        "Enter passphrase for key '/home/alice/.ssh/id_ed25519': ",
        "Are you sure you want to continue connecting (yes/no/[fingerprint])? ",
        "Enter PIN for authenticator: ",
    ],
    ids=[
        "other-user-same-host",
        "other-account",
        "other-user-kbd",
        "bare-password",
        "other-language",
        "otp-kbd",
        "otp-oath",
        "otp-numbered",
        "password-and-code",
        "code",
        "passcode",
        "code-bare",
        "key-passphrase",
        "host-key-question",
        "security-key-pin",
    ],
)
def test_the_askpass_helper_gives_every_other_prompt_nothing(tmp_path, prompt):
    """Another account's prompt (an ssh this one started inherits the helper), a second factor's, a one-time
    password's: the password is the machine's alone, and a PAM module may pass what it is given on to its vendor.
    A substring test for "passw" answered every one of these."""
    done = _askpass(tmp_path, prompt)

    assert (done.returncode, done.stdout) == (1, "")
    assert done.stderr.startswith("raven: ") and "hunter2" not in done.stderr


# ---- the prompt the helper waits for is the one ssh writes, for the host ssh resolves ----

_REAL_SSH = shutil.which("ssh")


def _owner_ssh_config(tmp_path: Path, monkeypatch, config: str) -> None:
    """The real ssh first on PATH, reading ``config`` as the owner's ``~/.ssh/config``."""
    conf = tmp_path / "ssh_config"
    conf.write_text(config, encoding="utf-8")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    wrapper = bin_dir / "ssh"
    wrapper.write_text(f'#!/bin/sh\nexec "{_REAL_SSH}" -F "{conf}" "$@"\n', encoding="utf-8")
    wrapper.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")


@_posix_helper
@pytest.mark.skipif(_REAL_SSH is None, reason="an OpenSSH client resolves the host")
@pytest.mark.parametrize(
    ("host", "prompt"),
    [
        ("devbox", "alice@10.0.0.5's password: "),
        ("devbox", "(alice@10.0.0.5) Password: "),
        ("aliasbox", "alice@labbox's password: "),
        ("0:0:0:0:0:0:0:1", "alice@::1's password: "),
    ],
    ids=["config-hostname", "config-hostname-keyboard-interactive", "host-key-alias", "non-canonical-address"],
)
def test_the_helper_answers_the_prompt_ssh_writes_for_the_host_it_resolves(
    tmp_path, monkeypatch, supported_host, host, prompt
):
    """ssh's prompt names the host ssh resolved, not the row's spelling of it: an alias's HostName, a
    HostKeyAlias in place of either, an address in its canonical form. Waiting for the row's spelling
    refused every such machine, and its registration then blamed the password (reviewed 2026-10-20)."""
    from raven.ops.connection_add import store_password

    _owner_ssh_config(
        tmp_path,
        monkeypatch,
        "Host devbox\n  HostName 10.0.0.5\nHost aliasbox\n  HostName 10.0.0.6\n  HostKeyAlias labbox\n",
    )
    monkeypatch.setenv("RAVEN_HOME", str(tmp_path / "rhome"))
    store_password("box", "hunter2")

    env = transport.ssh_env({**PASSWORD_ROW, "host": host})
    done = subprocess.run(
        [env["SSH_ASKPASS"], prompt], capture_output=True, text=True, env={**os.environ, **env}, timeout=30
    )

    assert (done.returncode, done.stdout) == (0, "hunter2\n"), done.stderr


@_posix_helper
@pytest.mark.parametrize(
    "stand_in",
    [
        "echo 'hostname 10.9.9.9'; echo 'ssh: Could not resolve hostname: Name or service not known' >&2; exit 255",
        "echo 'usage: ssh [-46AaCfGgKkMNnqsTtVvXxYy] destination [command]'",
        None,
        "/bin/sleep 3; echo 'hostname 10.9.9.9'",
    ],
    ids=["ssh-fails", "nothing-resolved", "no-ssh-here", "ssh-hangs"],
)
def test_when_ssh_cannot_say_how_it_resolves_the_host_the_helper_waits_for_the_row_s_own_address(
    tmp_path, monkeypatch, supported_host, stand_in
):
    from raven.ops.connection_add import store_password

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    if stand_in is not None:
        (bin_dir / "ssh").write_text(f"#!/bin/sh\n{stand_in}\n", encoding="utf-8")
        (bin_dir / "ssh").chmod(0o755)
    monkeypatch.setenv("PATH", str(bin_dir))
    monkeypatch.setattr(transport, "_RESOLVE_TIMEOUT_S", 0.3)
    monkeypatch.setenv("RAVEN_HOME", str(tmp_path / "rhome"))
    store_password("box", "hunter2")

    env = transport.ssh_env(PASSWORD_ROW)
    done = subprocess.run(
        [env["SSH_ASKPASS"], "alice@203.0.113.7's password: "],
        capture_output=True,
        text=True,
        env={**os.environ, **env, "PATH": "/usr/bin:/bin"},
        timeout=30,
    )

    assert (done.returncode, done.stdout) == (0, "hunter2\n"), done.stderr


@_posix_helper
@pytest.mark.parametrize(
    ("stand_in", "why"),
    [
        ("echo 'ssh: Could not resolve hostname: Name or service not known' >&2; exit 255", "ssh -G exited 255"),
        ("echo 'usage: ssh [-46AaCfGgKkMNnqsTtVvXxYy] destination [command]'", "ssh -G named no host"),
        (None, "ssh -G could not be started"),
        ("/bin/sleep 3; echo 'hostname 10.9.9.9'", "ssh -G took over 0.3 s"),
    ],
    ids=["ssh-fails", "nothing-resolved", "no-ssh-here", "ssh-hangs"],
)
def test_when_ssh_cannot_say_how_it_resolves_the_host_the_helper_s_refusal_says_so(
    tmp_path, monkeypatch, supported_host, stand_in, why
):
    """The fallback to the row's spelling is safe and was silent: a machine whose prompt names the host ssh did
    resolve was then declined, and its registration read as a machine asking another question. The helper's
    refusal names the failed resolution, which is no secret, and still gives the prompt nothing."""
    from raven.ops.connection_add import store_password

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    if stand_in is not None:
        (bin_dir / "ssh").write_text(f"#!/bin/sh\n{stand_in}\n", encoding="utf-8")
        (bin_dir / "ssh").chmod(0o755)
    monkeypatch.setenv("PATH", str(bin_dir))
    monkeypatch.setattr(transport, "_RESOLVE_TIMEOUT_S", 0.3)
    monkeypatch.setenv("RAVEN_HOME", str(tmp_path / "rhome"))
    store_password("box", "hunter2")

    env = transport.ssh_env({**PASSWORD_ROW, "host": "ownerbox"})
    done = subprocess.run(
        [env["SSH_ASKPASS"], "alice@192.0.2.8's password: "],
        capture_output=True,
        text=True,
        env={**os.environ, **env, "PATH": "/usr/bin:/bin"},
        timeout=30,
    )

    assert (done.returncode, done.stdout) == (1, "")
    assert done.stderr.splitlines() == [
        transport.ASKPASS_REFUSED,
        f"{transport.ASKPASS_UNRESOLVED} ({why}), so it waited for the prompt of alice@ownerbox",
    ]


@_posix_helper
def test_a_host_ssh_resolves_adds_nothing_to_the_helper_s_refusal(tmp_path, monkeypatch, supported_host):
    from raven.ops.connection_add import store_password

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "ssh").write_text("#!/bin/sh\necho 'hostname 203.0.113.7'\n", encoding="utf-8")
    (bin_dir / "ssh").chmod(0o755)
    monkeypatch.setenv("PATH", str(bin_dir))
    monkeypatch.setenv("RAVEN_HOME", str(tmp_path / "rhome"))
    store_password("box", "hunter2")

    env = transport.ssh_env(PASSWORD_ROW)
    done = subprocess.run(
        [env["SSH_ASKPASS"], "(alice@203.0.113.7) Verification code: "],
        capture_output=True,
        text=True,
        env={**os.environ, **env, "PATH": "/usr/bin:/bin"},
        timeout=30,
    )

    assert (done.returncode, done.stdout, done.stderr) == (1, "", transport.ASKPASS_REFUSED + "\n")


@_posix_helper
def test_a_config_dump_carrying_bytes_that_are_not_utf_8_still_names_the_host(tmp_path, monkeypatch, supported_host):
    """ssh prints the owner's config values as they are on disk. One that is not UTF-8 must not take the
    resolution down, and with it every connection to the machine."""
    from raven.ops.connection_add import store_password

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "ssh").write_text(
        "#!/bin/sh\nprintf 'remotecommand caf\\351\\nhostname 10.9.9.9\\n'\n", encoding="utf-8"
    )
    (bin_dir / "ssh").chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    monkeypatch.setenv("RAVEN_HOME", str(tmp_path / "rhome"))
    store_password("box", "hunter2")

    env = transport.ssh_env(PASSWORD_ROW)
    done = subprocess.run(
        [env["SSH_ASKPASS"], "alice@10.9.9.9's password: "],
        capture_output=True,
        text=True,
        env={**os.environ, **env},
        timeout=30,
    )

    assert (done.returncode, done.stdout) == (0, "hunter2\n"), done.stderr


@_posix_helper
def test_resolving_the_host_hands_ssh_no_askpass_so_nothing_it_starts_reaches_the_helper(
    tmp_path, monkeypatch, supported_host
):
    """A helper that ran ``ssh -G`` itself, against a stand-in ssh that called askpass, recursed into
    thousands of processes (2026-10-19). The resolution now runs before ssh is started, and whatever it
    starts finds no askpass to call."""
    from raven.ops.connection_add import store_password

    seen = tmp_path / "seen"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "ssh").write_text(
        f'#!/bin/sh\nprintf "%s|%s|%s\\n" "$1" "$SSH_ASKPASS" "$SSH_ASKPASS_REQUIRE" >> "{seen}"\n'
        'echo "hostname 203.0.113.7"\n',
        encoding="utf-8",
    )
    (bin_dir / "ssh").chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    monkeypatch.setenv("SSH_ASKPASS", str(tmp_path / "an-askpass-already-set"))
    monkeypatch.setenv("SSH_ASKPASS_REQUIRE", "force")
    monkeypatch.setenv("RAVEN_HOME", str(tmp_path / "rhome"))
    store_password("box", "hunter2")

    transport.ssh_env(PASSWORD_ROW)

    assert seen.read_text(encoding="utf-8").splitlines() == ["-G||never"]


@_posix_helper
@pytest.mark.skipif(_REAL_SSH is None, reason="an OpenSSH client resolves the host")
@pytest.mark.parametrize("setting", ["yes", "always"])
def test_a_password_connection_never_canonicalizes_so_it_logs_in_where_ssh_g_said(tmp_path, monkeypatch, setting):
    """``ssh -G`` prints the configuration without the DNS lookups ``CanonicalizeHostname`` makes, and a real
    connection then re-reads the config under the canonical name -- where a ``Host <id>`` block Raven wrote
    for another machine can apply (measured with OpenSSH 8.9: ``-G -v`` reports applying it in that pass).
    So the host the helper is told to answer for could differ from the one the connection reaches. A password
    connection turns canonicalization off on its command line, which outranks the owner's config, and the
    resolution, asked with the same options, says so."""
    _owner_ssh_config(tmp_path, monkeypatch, f"CanonicalizeHostname {setting}\nHost devbox\n  HostName 10.0.0.5\n")
    argv = ssh_argv("devbox", 22, "", user="alice", password=True)

    resolved = transport.resolve_with_ssh(argv)

    assert resolved.failure == "" and resolved.first("canonicalizehostname") == "false"
    assert resolved.first("hostname") == "10.0.0.5"
    assert "CanonicalizeHostname=no" in argv


@_posix_helper
@pytest.mark.skipif(_REAL_SSH is None, reason="an OpenSSH client resolves the host")
def test_a_managed_block_a_final_pass_applies_never_supplies_the_address(tmp_path, monkeypatch, supported_host):
    """With canonicalization off, the one later pass left is ``Match final``, which re-reads the config under
    the name the owner's ``HostName`` gave: a ``Host <id>`` block Raven wrote then applies (OpenSSH 8.9 says
    so under ``-v``), but ssh keeps the first ``HostName`` it read, the owner's. A Raven block can only
    supply the address when it matches the row's own host, and that row is refused by name."""
    from raven.ops.connection_add import ALIAS_MARK, store_password

    mark = ALIAS_MARK.format(conn_id="labbox")
    _owner_ssh_config(
        tmp_path,
        monkeypatch,
        f"Match final\nHost lab\n  HostName labbox\n\n{mark}\nHost labbox\n  HostName 198.51.100.66\n{mark}\n",
    )
    monkeypatch.setattr(transport, "passwd_home", lambda: tmp_path / "nobody")
    monkeypatch.setenv("RAVEN_HOME", str(tmp_path / "rhome"))
    store_password("lab", "hunter2")

    env = transport.ssh_env({"id": "lab", "transport": "ssh", "host": "lab", "user": "alice", "auth": "password"})

    assert env[transport.ASKPASS_FOR_ENV] == "alice@labbox"


# ---- an alias Raven keeps for one machine never picks who gets another machine's password ----


def _key_machine_alias(conn_id: str, host: str) -> Path:
    """The ``Host <conn_id>`` block Raven writes into ``~/.ssh/config`` when a key machine is registered."""
    from raven.ops.connection_add import write_ssh_alias

    write_ssh_alias({"id": conn_id, "transport": "ssh", "host": host, "port": 22, "user": "root", "key": "~/k"})
    return Path(os.path.expanduser("~/.ssh/config"))


@_posix_helper
@pytest.mark.skipif(_REAL_SSH is None, reason="an OpenSSH client resolves the host")
@pytest.mark.parametrize("spelled", ["labbox", "LabBox"], ids=["as-written", "another-case"])
def test_a_password_machine_whose_host_is_another_machine_s_managed_alias_is_refused_and_its_password_stays(
    tmp_path, monkeypatch, supported_host, spelled
):
    """A key machine registered under the id ``labbox`` makes Raven write ``Host labbox`` / ``HostName <its
    address>``. Following ssh's resolution through that block handed the password kept for ``lab`` -- a password
    machine at ``labbox`` -- to the key machine's address, and the model can register such a machine (reviewed
    2026-10-20). ssh matches ``Host`` case-blind, so the refusal does too."""
    from raven.ops.connection_add import store_password

    config = _key_machine_alias("labbox", "198.51.100.66")
    wrapper_dir = tmp_path / "bin"
    wrapper_dir.mkdir()
    (wrapper_dir / "ssh").write_text(f'#!/bin/sh\nexec "{_REAL_SSH}" -F "{config}" "$@"\n', encoding="utf-8")
    (wrapper_dir / "ssh").chmod(0o755)
    monkeypatch.setenv("PATH", f"{wrapper_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    monkeypatch.setenv("RAVEN_HOME", str(tmp_path / "rhome"))
    store_password("lab", "hunter2")

    with pytest.raises(TransportError) as refused:
        transport.ssh_env({"id": "lab", "transport": "ssh", "host": spelled, "user": "alice", "auth": "password"})

    said = str(refused.value)
    assert "'lab'" in said and "'labbox'" in said and "not sent" in said
    assert "198.51.100.66" not in said and "hunter2" not in said


@_posix_helper
@pytest.mark.parametrize("where", ["passwd-home", "raven-home"])
@pytest.mark.parametrize("whole", [True, False], ids=["block", "one-mark-left"])
def test_a_managed_alias_counts_in_the_config_ssh_reads_and_in_the_one_raven_writes_even_cut_short(
    tmp_path, monkeypatch, supported_host, where, whole
):
    """ssh reads ``~/.ssh/config`` from the account's passwd entry, Raven writes the one under ``$HOME``; a block
    in either may be the one ssh follows. A block with one of its marks gone is still Raven's, not the owner's.
    The ssh stand-in names the managed address, as the real one would, and is never what decides."""
    from raven.ops.connection_add import ALIAS_MARK, store_password

    passwd_home = tmp_path / "passwd-home"
    monkeypatch.setattr(transport, "passwd_home", lambda: passwd_home, raising=False)
    home = passwd_home if where == "passwd-home" else Path(os.path.expanduser("~"))
    mark = ALIAS_MARK.format(conn_id="labbox")
    block = [mark, "Host labbox", "  HostName 198.51.100.66"] + ([mark] if whole else [])
    (home / ".ssh").mkdir(parents=True, exist_ok=True)
    (home / ".ssh" / "config").write_text("Host ownerbox\n  HostName 192.0.2.8\n\n" + "\n".join(block) + "\n")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "ssh").write_text("#!/bin/sh\necho 'hostname 198.51.100.66'\n", encoding="utf-8")
    (bin_dir / "ssh").chmod(0o755)
    monkeypatch.setenv("PATH", str(bin_dir))
    monkeypatch.setenv("RAVEN_HOME", str(tmp_path / "rhome"))
    store_password("lab", "hunter2")

    with pytest.raises(TransportError, match="'labbox'"):
        transport.ssh_env({"id": "lab", "transport": "ssh", "host": "labbox", "user": "alice", "auth": "password"})


@_posix_helper
@pytest.mark.skipif(_REAL_SSH is None, reason="an OpenSSH client resolves the host")
def test_the_owner_s_own_alias_beside_raven_s_blocks_still_names_the_prompt_it_resolves(
    tmp_path, monkeypatch, supported_host
):
    """What the resolution exists for keeps working with Raven's blocks in the same file: only a host that is a
    managed id is refused."""
    from raven.ops.connection_add import store_password

    config = _key_machine_alias("labbox", "198.51.100.66")
    with config.open("a", encoding="utf-8") as fh:
        fh.write("\nHost ownerbox\n  HostName 192.0.2.8\n")
    monkeypatch.setattr(transport, "passwd_home", lambda: config.parent.parent, raising=False)
    wrapper_dir = tmp_path / "bin"
    wrapper_dir.mkdir()
    (wrapper_dir / "ssh").write_text(f'#!/bin/sh\nexec "{_REAL_SSH}" -F "{config}" "$@"\n', encoding="utf-8")
    (wrapper_dir / "ssh").chmod(0o755)
    monkeypatch.setenv("PATH", f"{wrapper_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    monkeypatch.setenv("RAVEN_HOME", str(tmp_path / "rhome"))
    store_password("own", "hunter2")

    env = transport.ssh_env({"id": "own", "transport": "ssh", "host": "ownerbox", "user": "alice", "auth": "password"})

    assert env[transport.ASKPASS_FOR_ENV] == "alice@192.0.2.8"


@pytest.mark.skipif(os.name == "nt", reason="POSIX file modes are not available on Windows")
def test_a_stale_askpass_helper_is_replaced_whole_and_never_rewritten_in_place(tmp_path):
    keys = tmp_path / "keys"
    keys.mkdir(mode=0o700)
    stale = '#!/bin/sh\nexec cat "$RAVEN_MACHINE_PASSWORD_FILE"\n'
    (keys / "askpass").write_text(stale, encoding="utf-8")

    with (keys / "askpass").open(encoding="utf-8") as running:
        helper = transport.askpass_helper(keys)
        # A connection that started the stale helper while another brought it up to date reads
        # it whole: never a file emptied under it.
        assert running.read() == stale

    assert helper.read_text(encoding="utf-8") == transport._ASKPASS
    assert oct(helper.stat().st_mode & 0o777) == "0o700"
    assert sorted(path.name for path in keys.iterdir()) == ["askpass"], "nothing is left beside it"


def test_one_function_builds_the_environment_that_hands_ssh_a_kept_password():
    """Every way a kept password reaches ssh -- registration probes, the CLI, ``machines.add``, exec's machine
    channel, the ACP tunnel and node link, on-call's runners and rsync -- takes it from :func:`transport.ssh_env`,
    so the refusals there (no password kept, Windows, a host that is one of Raven's own aliases) stand on every
    one of them. A second module naming the helper's password-file variable, or ordering askpass by force, would
    be a path around them."""
    import re

    repo = Path(__file__).resolve().parent.parent
    builds = re.compile(r"RAVEN_MACHINE_PASSWORD_FILE|ASKPASS_FILE_ENV|SSH_ASKPASS_REQUIRE[\"']?\s*[:=,]\s*[\"']force")
    found = sorted(
        str(path.relative_to(repo))
        for root in ("raven", "agents", "plugins-dist")
        for path in (repo / root).rglob("*.py")
        if builds.search(path.read_text(encoding="utf-8", errors="replace"))
    )

    assert found == ["raven/ops/transport.py"]
