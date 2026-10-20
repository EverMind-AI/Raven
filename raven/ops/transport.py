"""Command runners for registered machines, so nothing above this has to know.

A machine the owner registered is not always a different computer. Someone who
writes their own solver usually runs it where they wrote it and installs raven
on that same box: there is no host to reach and no key to present, and a
command is simply run. Measured need, 2026-08-18 -- the CFD group's in-house
Fortran code lives on the machine raven would be installed on.

Everything above this seam names a connection and nothing else. Which runner
answers is a property of the row (``connections.transport_of``), so a tool
call reads the same whether the work runs across the room or in this process's
own filesystem.
"""

from __future__ import annotations

import contextlib
import dataclasses
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from raven.ops.connections import LOCAL, password_auth, password_path, transport_of

CommandRunner = Callable[[str], tuple[int, str]]

# What ``timeout`` returns when it kills something, reused so a caller reads one
# code whichever way the cap was applied.
TIMED_OUT_RC = 124


class TransportError(RuntimeError):
    """A machine that cannot be reached from what its row says."""


def make_local_runner(cap_seconds: float = 600.0) -> CommandRunner:
    """Run the command here, answering in the shape an ssh runner answers in.

    ``shell=True`` because that is what the ssh runner effectively gives a
    caller: the remote sshd puts the string through a shell, so the commands
    written for it -- pipes, redirects, ``&&`` chains, heredocs -- keep working
    unchanged.

    The cap is enforced here rather than by wrapping the command in
    ``timeout``. That binary is GNU coreutils and macOS does not ship it,
    which is exactly the machine a local connection is most likely to be:
    measured 2026-08-19, the first local look came back
    ``exit 127, /bin/sh: timeout: command not found``.
    """

    def run(cmd: str) -> tuple[int, str]:
        # Its own session, so the cap can reach the whole tree. subprocess.run's
        # timeout kills the shell alone: a command that had forked (a python
        # that spawned a solver) kept running past the cap, past the return
        # code 124 that said it had been stopped, and outside anything this
        # channel tracks (reviewed 2026-09-04, reproduced with a sleep child).
        proc = subprocess.Popen(
            cmd,
            shell=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            **_own_group_kwargs(),
        )
        try:
            out, err = proc.communicate(timeout=cap_seconds)
        except subprocess.TimeoutExpired:
            _kill_process_tree(proc)
            try:
                out, _err = proc.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                # Something escaped the group and holds the pipe; the shell
                # itself is dead, and its output is whatever was collected.
                proc.kill()
                out = ""
            return TIMED_OUT_RC, out or ""
        tail = f"\n{err}" if err and proc.returncode != 0 else ""
        return proc.returncode, out + tail

    return run


def _own_group_kwargs() -> dict[str, Any]:
    """How this platform puts a child in a group of its own.

    POSIX: a new session, so the pid doubles as the group id ``killpg`` takes.
    Windows ignores ``start_new_session`` and has no ``killpg``; there the tree
    is killed by pid through ``taskkill``, which needs no group at all.
    """
    if hasattr(os, "killpg"):
        return {"start_new_session": True}
    return {}


def _kill_process_tree(proc: subprocess.Popen, grace_s: float = 2.0) -> None:
    """Stop the command and everything it started.

    POSIX: TERM the session, wait out a short grace, KILL what stayed.
    Windows: ``taskkill /T /F`` walks the child tree by pid -- the platform
    offers no group signal, and a bare ``Popen.kill`` would reach the shell
    alone, the very hole this exists to close (reviewed 2026-09-04: the first
    cut called ``os.killpg`` unconditionally and raised there instead of
    returning 124, leaving the command running).
    """
    if not hasattr(os, "killpg"):
        taskkill = shutil.which("taskkill")
        if taskkill:
            try:
                subprocess.run(
                    [taskkill, "/T", "/F", "/PID", str(proc.pid)],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=grace_s + 5,
                    check=False,
                )
            except (OSError, subprocess.SubprocessError):
                pass
        if proc.poll() is None:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
        return
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    deadline = time.monotonic() + grace_s
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            break
        time.sleep(0.05)
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


# The options that make ``-i key`` the ONLY identity ssh may offer. All three
# are needed: ``-i`` is a preference, not a restriction; ``IdentitiesOnly=yes``
# is defined as excluding what an agent or a provider adds, not what the
# owner's config names; and only reading no config at all (``-F /dev/null``)
# keeps the config's own ``IdentityFile`` lines out. Measured with OpenSSH
# 9.9p2 against a config carrying two IdentityFile lines: ``-i cand`` with
# IdentitiesOnly and no agent still resolved to three identities.
ISOLATE_IDENTITY = ("-F", "/dev/null", "-o", "IdentitiesOnly=yes", "-o", "IdentityAgent=none")

# How ssh is told to log in with the password kept for the machine instead of
# a key: no identity offered, the password methods first, and one try, since
# the answer comes from a program rather than a person who might retype it.
# And no connection sharing: the owner's ssh config may say ``ControlMaster
# auto``, and a connection that rides a master another one authenticated never
# authenticates itself -- a probe would then "prove" a password it never sent
# (one refused after the master expires), and a password rotated on the
# machine would go unnoticed until then. Each password connection logs in on
# its own; the command line wins over the config.
PASSWORD_AUTH = (
    "-o",
    "PubkeyAuthentication=no",
    "-o",
    "PreferredAuthentications=password,keyboard-interactive",
    "-o",
    "NumberOfPasswordPrompts=1",
    "-o",
    "ControlMaster=no",
    "-o",
    "ControlPath=none",
    # A jump host is another ssh, started by this one, that inherits the
    # askpass helper: it would be handed this machine's password for its own
    # prompt (reviewed 2026-10-19). A machine reached only through one is
    # registered with a key.
    "-o",
    "ProxyJump=none",
    "-o",
    "ProxyCommand=none",
    # ``ssh -G`` makes none of the DNS lookups canonicalization makes, and a
    # connection that canonicalizes re-reads the config under the new name --
    # where a ``Host <id>`` block Raven wrote for another machine can apply.
    # Off, the connection reaches the host ``ssh -G`` named, the one the
    # helper's prompt is for (reviewed 2026-10-20).
    "-o",
    "CanonicalizeHostname=no",
)

#: The variable the askpass helper reads the password file's path from.
ASKPASS_FILE_ENV = "RAVEN_MACHINE_PASSWORD_FILE"
#: The variable that tells the helper whose prompt it may answer: the registered account, at the host ssh resolves.
ASKPASS_FOR_ENV = "RAVEN_MACHINE_PASSWORD_FOR"
#: What the helper writes on ssh's stderr when the question is not that prompt, so it gave ssh nothing.
ASKPASS_REFUSED = (
    "raven: the password kept for this machine answers its own password prompt only, and this prompt is not one"
)
#: The variable that tells the helper why the prompt it waits for names the row's own spelling: ssh could not
#: say how it resolves the host. Empty when it could.
ASKPASS_UNRESOLVED_ENV = "RAVEN_MACHINE_PROMPT_UNRESOLVED"
#: What the helper adds to that refusal when the variable is set, with the reason and the prompt it waited for.
ASKPASS_UNRESOLVED = "raven: ssh could not say which host this machine's password prompt names"

# The helper itself: OpenSSH runs it when ``SSH_ASKPASS_REQUIRE=force`` and
# reads the password from its stdout. It prints the one line of the file the
# environment names, so the password is in no process's environment and on no
# command line; the file is readable by the owner alone. It answers the
# registered account's own password prompt and no other: ssh's
# ``user@host's password: `` and the keyboard-interactive ``(user@host)
# Password: ``, with ``user@host`` from the environment. Another account's
# prompt (an ssh this one started inherits the helper), a second factor's (a
# verification code, a passcode) and a one-time password's -- "password" is
# in that wording too -- get nothing, since the password is the machine's
# alone and a PAM module may pass what it is given on to its vendor. The
# question is ssh's first argument; trailing blanks and case are ignored. A
# refusal says so on stderr, and also why the prompt it waited for is the
# row's own spelling when ssh could not resolve the host.
_ASKPASS = (
    "#!/bin/sh\n"
    "# Raven: hands ssh the password kept for a registered machine, at that account's own password prompt only.\n"
    "# Managed; rewritten when it differs.\n"
    f"who=$(printf '%s' \"${ASKPASS_FOR_ENV}\" | tr '[:upper:]' '[:lower:]')\n"
    "asked=$(printf '%s' \"$1\" | tr '[:upper:]' '[:lower:]' | sed 's/[[:space:]]*$//')\n"
    'case "$asked" in\n'
    f'  "$who\'s password:"|"($who) password:") exec cat "${ASKPASS_FILE_ENV}" ;;\n'
    "esac\n"
    f"echo '{ASKPASS_REFUSED}' >&2\n"
    f"[ -n \"${ASKPASS_UNRESOLVED_ENV}\" ] && printf '%s (%s), so it waited for the prompt of %s\\n' "
    f'"{ASKPASS_UNRESOLVED}" "${ASKPASS_UNRESOLVED_ENV}" "${ASKPASS_FOR_ENV}" >&2\n'
    "exit 1\n"
)


def ssh_argv(
    host: str,
    port: int,
    key: str,
    *,
    user: str = "root",
    connect_timeout: int = 15,
    identities_only: bool = False,
    password: bool = False,
    extra: tuple[str, ...] = (),
) -> list[str]:
    """The ssh command line up to, and not including, the remote command.

    One spelling of how raven reaches a registered machine, shared by the
    one-shot runner below and by an ACP sub-agent that lives on the machine
    (``raven.acp_client.remote``), so the two cannot drift on host-key policy or
    on never stopping to ask for a password. ``extra`` is placed before the
    destination, where ssh reads options; a caller that keeps the session open
    passes its keepalives there. ``identities_only`` narrows auth to ``key``
    (:data:`ISOLATE_IDENTITY`). With
    ``password`` no key is offered and ``BatchMode`` is off, so ssh asks --
    the askpass helper :func:`ssh_env` names, never a terminal
    (:data:`PASSWORD_AUTH`); ``key`` is then unread.
    """
    argv = ["ssh"]
    if not password:
        argv += ["-i", key]
    argv += ["-p", str(port)]
    if not password:
        argv += ["-o", "BatchMode=yes"]
    argv += ["-o", f"ConnectTimeout={connect_timeout}", "-o", "StrictHostKeyChecking=accept-new"]
    if password:
        argv += list(PASSWORD_AUTH)
    elif identities_only:
        argv += list(ISOLATE_IDENTITY)
    argv += list(extra)
    argv.append(f"{user}@{host}")
    return argv


# OpenSSH before 8.4 knows no ``SSH_ASKPASS_REQUIRE``: it reads a password from
# the terminal whenever it can open one, and falls back to askpass only when it
# cannot. A password connection is therefore started in a session of its own
# (``start_new_session``), with no controlling terminal to open, so every
# version reaches askpass. Key connections keep the caller's session.
def askpass_helper(keys_dir: Path) -> Path:
    """The askpass helper under ``keys_dir`` (:data:`_ASKPASS`), made or brought up to date; the owner's alone."""
    path = keys_dir / "askpass"
    keys_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        current = path.read_text(encoding="utf-8")
    except OSError:
        current = None
    if current != _ASKPASS:
        # Written beside it and renamed over it, never refilled in place: connections start together (the
        # machines list reaches them all at once), and one could run a helper another had just emptied. The
        # name is unique per call, since threads share a pid, and outside the ``*.password`` glob.
        fd, tmp = tempfile.mkstemp(prefix=".askpass.", dir=keys_dir)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(_ASKPASS)
            os.chmod(tmp, 0o700)
            os.replace(tmp, path)
        finally:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(tmp)
    os.chmod(path, 0o700)
    return path


def ssh_env(row: dict[str, Any], *, password_file: Path | None = None) -> dict[str, str]:
    """What a password row's ssh needs in its environment; ``{}`` for a key row.

    The askpass helper, the order to use it (``SSH_ASKPASS_REQUIRE=force``,
    OpenSSH 8.4+), the password file it reads, and a ``DISPLAY``, which ssh
    requires to consult askpass at all and never looks at. The password itself
    is in none of it. Refused, by name, when no password is kept here for the
    machine -- removing it and registering it again puts one back, since a
    registration under an id in use is refused -- and on Windows, whose
    OpenSSH runs an askpass helper but not a POSIX one: the helper here is a
    shell script, and Windows cannot execute it (measured 2026-10-19,
    OpenSSH_for_Windows_9.5p2: a ``.cmd`` helper is run, a ``#!/bin/sh`` one
    fails with ``CreateProcessW failed error:193``).
    """
    if not password_auth(row):
        return {}
    label = str(row.get("id") or "?").strip()
    if sys.platform == "win32":
        raise TransportError(
            f"machine {label!r} was registered with a password, and this computer (Windows) cannot run the script "
            "that hands it to ssh; use a key instead"
        )
    try:
        secret = password_path(label)
    except ValueError as exc:
        # A hand-written row with an id the registry refuses: the doctor
        # blocks it, and a caller that reaches it anyway gets the one kind of
        # refusal this function gives rather than a crash.
        raise TransportError(str(exc)) from None
    if password_file is not None:
        secret = password_file
    if not secret.is_file():
        raise TransportError(
            f"no password is kept here for machine {label!r} ({secret}); remove the machine and register it again"
        )
    host, port, _key, user = ssh_target(row)
    if host.lower() in managed_aliases():
        # ssh would follow the block Raven wrote for that other machine, and
        # the helper would then wait for that machine's prompt: the password
        # goes only where the owner registered it, whatever ssh resolves.
        raise TransportError(
            f"machine {label!r} is registered at {host!r}, which is also the ssh alias Raven keeps in "
            f"~/.ssh/config for machine {host.lower()!r}: ssh would log in at that machine's address, so the "
            f"password kept for {label!r} is not sent; remove one of the two machines, or register {label!r} "
            "again at its own address"
        )
    target, unresolved = _prompt_target(host, port, user)
    return {
        "SSH_ASKPASS": str(askpass_helper(secret.parent)),
        "SSH_ASKPASS_REQUIRE": "force",
        ASKPASS_FILE_ENV: str(secret),
        ASKPASS_FOR_ENV: target,
        ASKPASS_UNRESOLVED_ENV: unresolved,
        "DISPLAY": os.environ.get("DISPLAY") or "raven-askpass",
    }


#: The line before and after each ``Host <id>`` block Raven keeps in ``~/.ssh/config`` for a key machine
#: (``connection_add.write_ssh_alias``).
ALIAS_MARK = "# raven connection {conn_id} (managed; rewritten on every add)"
_ALIAS_MARK_LINE = re.compile(
    "^" + re.escape(ALIAS_MARK).replace(re.escape("{conn_id}"), r"(\S+)") + r"[ \t]*\r?$", re.MULTILINE
)


def passwd_home() -> Path | None:
    """The home ssh reads ``~/.ssh/config`` under: the account's passwd entry, which ``$HOME`` does not move."""
    try:
        import pwd

        return Path(pwd.getpwuid(os.getuid()).pw_dir)
    except (ImportError, KeyError):
        return None


def managed_aliases() -> set[str]:
    """The ids, lowercased, of the ``Host`` blocks Raven keeps in the ssh config ssh reads or the one Raven writes.

    A mark counts on its own: a block the owner cut short is still not theirs.
    A file that cannot be read adds nothing: ssh, run as the same account,
    skips a user config it cannot open, so it follows none of its blocks.
    """
    homes = {Path(os.path.expanduser("~")), passwd_home()}
    found: set[str] = set()
    for home in homes - {None}:
        try:
            text = (home / ".ssh" / "config").read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        found.update(conn_id.lower() for conn_id in _ALIAS_MARK_LINE.findall(text))
    return found


#: How long ``ssh -G`` may take to say how it resolves a host (config, and DNS
#: when the owner's config canonicalizes) before the row's spelling stands.
_RESOLVE_TIMEOUT_S = 10.0


def _prompt_target(host: str, port: int, user: str) -> tuple[str, str]:
    """The ``user@host`` ssh's password prompt names for this destination, and why not when ssh could not say.

    The first is whose prompt the helper answers.

    ssh names the host it resolved, not the one on its command line: the
    ``HostName`` the owner's ssh config gives an alias, the canonical form of
    a numeric address (``::1`` for ``0:0:0:0:0:0:0:1``), or the
    ``HostKeyAlias`` in place of either. A helper waiting for the row's own
    spelling refused every such machine, and its registration then blamed the
    password (reviewed 2026-10-20). ``ssh -G`` prints that resolution without
    connecting, and is asked with the password connection's own options. The
    row's spelling when ssh cannot say, which sends the password nowhere else;
    the reason then goes to the helper (:data:`ASKPASS_UNRESOLVED_ENV`), whose
    refusal would otherwise read as a machine asking another question.

    Asked here, before the connection, never by the helper: a helper that ran
    ``ssh -G`` itself recursed into thousands of processes against a stand-in
    ssh that called askpass (2026-10-19). Nothing this call starts finds an
    askpass to call either (:func:`resolve_with_ssh`).
    """
    resolved = resolve_with_ssh(ssh_argv(host, port, "", user=user, password=True))
    if resolved.failure:
        return f"{user}@{host}", resolved.failure
    named = resolved.first("hostkeyalias") or resolved.first("hostname")
    return (f"{user}@{named}", "") if named else (f"{user}@{host}", "ssh -G named no host")


@dataclasses.dataclass(frozen=True)
class SshResolution:
    """What ``ssh -G`` printed for one command line: each key's values in ssh's order, or why there are none."""

    values: dict[str, tuple[str, ...]]
    #: Empty when ssh answered; otherwise what went wrong, in words a refusal can quote.
    failure: str

    def first(self, key: str) -> str:
        return next(iter(self.values.get(key, ())), "")


def resolve_with_ssh(argv: list[str]) -> SshResolution:
    """The configuration ssh resolves for ``argv`` -- an ssh command line up to its destination -- without connecting.

    The one reader of ``ssh -G``: a password machine's prompt
    (:func:`_prompt_target`) and a key machine's registration defaults
    (``connection_add.ssh_defaults``) both resolve here, each with its own
    command line. Two readers disagreed (reviewed 2026-10-20): the key one
    raised on a byte that is not UTF-8 and on an ssh it could not start, and
    handed its ``ssh -G`` the caller's terminal and askpass. Here nothing the
    call starts finds an askpass or a terminal to read, a value keeps every
    occurrence (``identityfile`` repeats, in the order ssh offers them), and
    a failure is a reason rather than an exception.
    """
    probe = [argv[0], "-G", *argv[1:]]
    env = {**os.environ, "SSH_ASKPASS": "", "SSH_ASKPASS_REQUIRE": "never"}
    try:
        # ``errors``: ssh prints config values as they are on disk, in no
        # promised encoding, and one undecodable byte must not cost the resolution.
        done = subprocess.run(
            probe,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=_RESOLVE_TIMEOUT_S,
            env=env,
        )
    except OSError:
        return SshResolution({}, "ssh -G could not be started")
    except subprocess.TimeoutExpired:
        return SshResolution({}, f"ssh -G took over {_RESOLVE_TIMEOUT_S:g} s")
    if done.returncode != 0:
        return SshResolution({}, f"ssh -G exited {done.returncode}")
    values: dict[str, list[str]] = {}
    for line in done.stdout.splitlines():
        key, _, value = line.partition(" ")
        if key:
            values.setdefault(key, []).append(value.strip())
    return SshResolution({key: tuple(found) for key, found in values.items()}, "")


def make_ssh_runner(
    host: str,
    port: int,
    key: str,
    *,
    user: str = "root",
    connect_timeout: int = 15,
    identities_only: bool = False,
    cap_seconds: float | None = None,
    password: bool = False,
    env: dict[str, str] | None = None,
) -> CommandRunner:
    """Run a command over ssh. ``identities_only`` narrows auth to ``key`` alone.

    ``cap_seconds`` bounds the whole call, not only the connect: a machine that
    accepts the session and then stops answering held the caller forever,
    because ``ConnectTimeout`` is over once the connection is up (reviewed
    2026-09-23 -- the registry probe lost the 30 s the old CLI enforced when
    it moved onto this runner). A cap that fires returns ``TIMED_OUT_RC`` with
    whatever arrived, the code the local runner uses, so a caller reads one
    code however the machine is reached. The local ssh client is what gets
    killed; bounding the far side is the caller's to do (the machine channel
    wraps its command for that).

    Off by default, which is how work reaches its machine: whatever the owner's
    ssh would use gets to work, an agent included. Turned on only where the
    session has to prove WHICH key opened it -- the registry probe trying the
    keys ssh named when the owner gave no path -- and it takes every option in
    :data:`ISOLATE_IDENTITY` to get there. The cost is that an alias or a jump
    host in the owner's config is not in play for that one call.

    ``password`` logs in with the password kept for the machine, through the
    askpass helper ``env`` carries (:func:`ssh_env`); ``key`` is then unread.
    """

    def run(cmd: str) -> tuple[int, str]:
        argv = ssh_argv(
            host,
            port,
            key,
            user=user,
            connect_timeout=connect_timeout,
            identities_only=identities_only,
            password=password,
        ) + [cmd]
        try:
            # Its own stdin, never the caller's: ssh reads stdin for the far
            # side, and under ``raven acp`` the caller's stdin carries ACP
            # frames, which a one-shot ssh would otherwise swallow.
            proc = subprocess.run(
                argv,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=cap_seconds,
                env=env,
                # See ``askpass_helper``: no controlling terminal, so no OpenSSH
                # version can ask it and every one uses the helper.
                start_new_session=password,
            )
        except subprocess.TimeoutExpired as exc:
            got = exc.stdout.decode(errors="replace") if isinstance(exc.stdout, bytes) else (exc.stdout or "")
            return TIMED_OUT_RC, got
        tail = f"\n{proc.stderr}" if proc.stderr and proc.returncode != 0 else ""
        return proc.returncode, proc.stdout + tail

    return run


def runner_from(
    row: dict[str, Any], *, cap_seconds: float | None = None, password_file: Path | None = None
) -> CommandRunner:
    """The runner this connection row calls for.

    Raises rather than handing back something half-connected: a runner built on
    an empty address fails later with a message that reads as a network fault
    rather than as a row that was never told where to run. Measured 2026-08-17,
    and again 2026-08-18 when the failure reached the owner as "your machine
    refused the connection" while that machine was answering in under 200ms.
    """
    if transport_of(row) == LOCAL:
        # The caller's cap, not this module's default. Looking at a machine is
        # capped the same whichever way it is reached; without this the local
        # transport quietly allowed ten minutes, because the cap for a remote
        # look rides on the ``timeout`` wrapper and a local one has only the
        # runner.
        return make_local_runner(cap_seconds) if cap_seconds else make_local_runner()

    host, port, key, user = ssh_target(row)
    if password_auth(row):
        # Judged here, before ssh is started: a password that is not kept here
        # any more, or a Windows host, is a refusal with a name, not a login
        # that fails minutes into a dispatch.
        extra = ssh_env(row, password_file=password_file)
        return make_ssh_runner(
            host, port, "", user=user, cap_seconds=cap_seconds, password=True, env={**os.environ, **extra}
        )
    return make_ssh_runner(host, port, key, user=user, cap_seconds=cap_seconds)


def ssh_target(row: dict[str, Any]) -> tuple[str, int, str, str]:
    """``(host, port, key, user)`` for an ssh row, with the registry's defaults.

    Raises :class:`TransportError` on a row that names no host, for the reason
    :func:`runner_from` gives; every way onto a machine reads the row here.
    """
    host = str(row.get("host") or "").strip()
    if not host:
        raise TransportError(
            f"connection {str(row.get('id') or '?')!r} has no address to run on: its row names no host"
        )
    return (
        host,
        int(row.get("port") or 22),
        os.path.expanduser(str(row.get("key") or "~/.ssh/id_rsa")),
        # A connection names the account to log in as. Dropping it would make
        # that field one more setting that is written, accepted and does nothing.
        str(row.get("user") or "root"),
    )
