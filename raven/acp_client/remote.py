"""An ACP sub-agent that lives on a registered machine, reached through that machine's Raven node.

The host keeps one Raven node per machine, started over ssh
(:mod:`raven.node.client`), and the node starts the agent there: in the
machine user's login environment, in a process group of its own, with its ACP
frames forwarded on a channel of the node's connection. Above the connection
nothing knows the agent is elsewhere. What does change is everything that
assumed the agent shares this disk: its working directory, the MCP servers
lent through a local socket, the files a turn is credited with, and the paths
handed to it.

This module owns what only a machine needs and the node cannot own:

- the registry lookup, refused up front for a row that cannot be used, so a
  dispatch fails before anything starts rather than as a network fault
  minutes in;
- the ssh argv that carries a node, built from the registry at launch and
  never stored, so the way in -- the key path, ssh's options, the address as
  ssh takes it -- reaches no config file, log line or model; the address does
  reach a model as part of a path on the machine, written the way scp writes
  it (:func:`shown`), because that is how a file there is named to the tools;
- one sentence for an ssh failure, naming the machine and never its address;
  ssh's own messages, which do name it, go to an owner-only log rather than to
  the stderr the connection journals and quotes;
- the one-shot commands the installer runs there (:func:`run`, :func:`push`);
- the tunnel an openai sub-agent's endpoint is reached through from the
  machine, held for one turn (:func:`tunnel`).
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import os
import re
import shlex
import socket
import subprocess
import time
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from raven.ops import connections, remote_path
from raven.ops.transport import TIMED_OUT_RC, TransportError, runner_from, ssh_argv, ssh_env, ssh_target

#: Where an agent works on its machine when its entry names nowhere else
#: (:data:`raven.ops.remote_path.DEFAULT_ROOT`, which the file tools read from
#: without asking).
DEFAULT_ROOT = remote_path.DEFAULT_ROOT

#: Options only a session that stays open needs. ``-T`` because a pseudo-tty
#: rewrites the JSON stream (CRLF line ends, echoed input); keepalives because
#: ``ConnectTimeout`` is over once the connection is up, and without them a
#: machine that drops off the network is noticed only by TCP, minutes later.
#: The node's own heartbeat is what notices first; these are the floor under it.
KEEPALIVE = ("-T", "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=3")

#: Options for a tunnel held for one turn: no remote command, the forward
#: refused rather than a session without it, and the keepalives a held session
#: has (``KEEPALIVE`` without ``-T``: there is no session to give a tty to).
TUNNEL = ("-N", "-o", "ExitOnForwardFailure=yes", "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=3")

#: How long past ssh's own connect timeout a tunnel's port may take to open.
TUNNEL_READY_S = 20.0

#: The session directory every check of an agent reuses (Test, Connect, the
#: capability record), so pressing Test leaves one directory on the machine
#: rather than one per press.
CHECK_HANDLE = "raven-check"

#: What ssh's own exit code is when ssh, rather than the remote command, failed.
SSH_FAILED_RC = 255

#: What a POSIX shell exits with when it cannot find the command it was given.
NOT_FOUND_RC = 127

# The fields of a registry row that are the way onto the machine: read to open
# the ssh, and kept off the launch line a model, a log or a record could see.
# The user, address and port do name a path there, written the way scp writes
# it (:func:`shown`); the key never shows anywhere.
_WAY_IN = frozenset({"host", "port", "user", "key"})
_LEAF_UNSAFE = re.compile(r"[^A-Za-z0-9._-]")
_LEAF_MAX = 64


class RemoteMachineError(RuntimeError):
    """A remote agent's machine could not be used. The message is safe to show.

    It names the machine by its id and display name only, never by its
    address: an error is read by a model, and what an address does in front
    of one is name a file there (:func:`shown`), not a fault.
    """


@dataclass(frozen=True)
class Machine:
    """A registered ssh machine.

    The row never prints whole: the key and the ssh line built from the row
    reach no model, log or record, while its user, address and port name a
    path on the machine the way scp writes it (:func:`shown`).
    """

    id: str
    display_name: str
    _row: dict[str, Any] = field(repr=False, compare=False)

    @property
    def label(self) -> str:
        """How a sentence names it: the owner's name for it, then the id."""
        if self.display_name and self.display_name != self.id:
            return f"{self.display_name!r} ({self.id})"
        return repr(self.id)

    def field(self, name: str) -> Any:
        """One field of the machine's row, other than the way onto it.

        For what a caller needs to know about the machine itself -- where a
        Raven node is kept on it (``node_dir``), the directories its owner
        listed (``paths``). The address, port, account and key open the way
        there behind :func:`ssh_command`, :func:`run` and :func:`push`; the
        first three also name a path there (:func:`shown`), the key never shows.
        """
        if name in _WAY_IN:
            raise KeyError(f"{name!r} is how raven reaches the machine, not a fact about it")
        return self._row.get(name)


def machine(conn_id: str) -> Machine:
    """The registered machine ``conn_id`` names, or :class:`RemoteMachineError`.

    Looked up here rather than through a registry-side ``get``, the way the
    machine channel looks one up (``machine_exec._runner_for_connection``):
    main keeps the registry's readers to what its callers need. Unlike that
    lane this refuses a row with a blocking problem up front, because an agent
    launched on half a row fails as a network fault minutes into a dispatch.
    """
    wanted = str(conn_id or "").strip()
    found = connections.read()
    if found.state == connections.UNREADABLE:
        raise RemoteMachineError(
            f"machine {wanted!r}: the machine registry cannot be read; run `raven ops connection doctor`"
        )
    rows = [r for r in found.rows if str(r.get("id") or "").strip() == wanted]
    if not rows:
        raise RemoteMachineError(f"machine {wanted!r} is not registered; add it with `raven ops connection add`")
    row = rows[0]
    if len(rows) > 1 or not connections.usable([row]):
        raise RemoteMachineError(
            f"machine {wanted!r} is registered, but its entry cannot be used; run `raven ops connection doctor`"
        )
    if connections.transport_of(row) == connections.LOCAL:
        raise RemoteMachineError(
            f"machine {wanted!r} is this computer; remove `machine` from the agent's entry to run it here"
        )
    return Machine(id=wanted, display_name=str(row.get("display_name") or wanted), _row=dict(row))


def posix(command: str) -> str:
    """``command`` as sshd should hand it to the machine user's shell, whatever shell that is.

    sshd runs a remote command with the user's login shell, and a tcsh, csh or
    fish one cannot read a POSIX line -- ``${SHELL:-/bin/sh}`` alone stops csh
    with "Bad : modifier". ``exec /bin/sh -c '...'`` reads
    the same in all of them, with two adjustments for csh: a ``!`` is written
    ``'\\!'``, because csh substitutes history inside single quotes even for
    ``-c`` (measured 2026-10-10: ``a!b`` gave "Event not found"), and a line
    break is refused, because csh cannot quote one.

    The command's output starts on a line of its own. The user's shell may
    print while it starts -- bash reads ``.bashrc`` for a command sshd hands
    it, csh reads ``.cshrc`` for every ``-c`` -- and a greeting with no line
    break would otherwise glue itself to the first line the caller reads.
    """
    if "\n" in command or "\r" in command:
        raise ValueError("a command for a registered machine must be one line")
    return "exec /bin/sh -c " + shlex.quote(f"echo; {command}").replace("!", "'\\!'")


def ssh_command(
    target: Machine, command: str, *, ssh_log: str | os.PathLike[str], connect_timeout: int = 15
) -> list[str]:
    """The ssh that keeps ``command`` running on ``target`` for as long as it is read: its argv.

    What a Raven node is started with. An argv, handed to the connection as
    it is, so no host's own grammar reads it again: the one word that is the
    remote command keeps the POSIX quoting the far shell reads, whatever this
    host is (a Windows host's parser made twelve words of it while it went as
    a line). Built at launch and held in memory only: it
    carries the address. ``command`` is sent through :func:`posix`,
    so the machine user's login shell may be any shell; it carries no
    variable and reads no login profile -- the node captures the login
    environment itself, for the agents it starts.

    ``ssh_log`` (see :func:`ssh_log_path`) is where ssh writes its own
    messages instead of stderr, so stderr carries only what the far side
    writes: the connection journals stderr and quotes its tail in errors, and
    ssh's own lines name the address.
    """
    host, port, key, user = _target(target)
    password = connections.password_auth(target._row)
    if password:
        # Refused here, by name, when the password is not kept here any more:
        # the environment is what carries it (:func:`ssh_environment`).
        ssh_environment(target)
    extra = KEEPALIVE + ("-E", os.fspath(ssh_log))
    argv = ssh_argv(host, port, key, user=user, connect_timeout=connect_timeout, password=password, extra=extra)
    # ``--`` so nothing in the remote command is read as an option to ssh,
    # which also parses options that follow the destination.
    argv += ["--", posix(command)]
    return argv


# Whether this process is stopping and leaves the agents on registered machines to their daemons.
_LEAVING = False


def leave_agents_running() -> None:
    """From here on, closing a link leaves the agents on the machines running: this process is stopping.

    A machine's daemon owns the agent (:mod:`raven.node.server`); the link is
    only the way to it. Said once by the host's shutdown, before the sub-agents
    are cancelled and the pools closed, so neither a turn's cancel nor a link's
    close reaches an agent there, and the next process picks the run up
    (:mod:`raven.acp_client.remote_runs`).
    """
    global _LEAVING
    _LEAVING = True


def leaving() -> bool:
    """Whether this process is stopping and leaves the agents on the machines running."""
    return _LEAVING


def staying() -> None:
    """The opposite of :func:`leave_agents_running`: for a process that goes on (tests)."""
    global _LEAVING
    _LEAVING = False


def failure(target: Machine, log: str | os.PathLike[str], exc: BaseException) -> str | None:
    """The sentence for a connection that ended, when ssh or the far shell ended it.

    The launch ``exec``s the node, so once ssh got through the status ssh
    reports is the node's own, and ssh's exits are told apart by what ssh
    said: 255 with ssh's words in its log is ssh's; 255 with none is the far
    side exiting 255 itself, and its stderr is the better account. 127 is the
    far shell not finding the command, which it says on stderr; a process
    exiting 127 on its own is left to its own words. 124 is the one-shot
    runner's cap, which a launch does not have.
    """
    rc = getattr(exc, "returncode", None)
    if rc is None:
        return None
    said = read_ssh_log(log)
    if rc == SSH_FAILED_RC and not _ssh_spoke(said):
        return None
    if rc == NOT_FOUND_RC and not _NOT_FOUND.search(str(getattr(exc, "stderr", None) or "")):
        return None
    if rc == TIMED_OUT_RC:
        return None
    return explain(target, rc, said)


def _ssh_spoke(log: str) -> bool:
    """Whether ssh's log holds a word of its own beyond the first-connect host-key note."""
    return any(line.strip() and not line.startswith("Warning: Permanently added ") for line in log.splitlines())


# How a POSIX shell says the command it was told to exec is not there.
_NOT_FOUND = re.compile(r"not found|No such file or directory|cannot execute", re.IGNORECASE)


def root_of(remote_cwd: str | None) -> str:
    """Where an entry's agent works on its machine, as written: its ``remoteCwd`` or the default."""
    return (str(remote_cwd or "").strip() or DEFAULT_ROOT).rstrip("/") or "/"


def session_root(remote_cwd: str | None, handle: str) -> str:
    """``<remoteCwd>/<handle>`` as written, where an agent's session works on its machine.

    The directory the node makes (``session/dir``), before the machine has
    resolved it -- what a record and an announce can name on every path, a
    failed and a cancelled run included.
    """
    return f"{root_of(remote_cwd).rstrip('/')}/{leaf(handle)}"


def files_note(media: Sequence[Any], target: Machine) -> str:
    """What the agent on ``target`` is told about the files that came with its task: their addresses, nothing more.

    Nothing is carried to the machine. A file is named as the sender has it,
    and the agent uses it where the machine can read it -- the same path on
    a shared disk, a copy already there -- or says that it could not be found
    on the machine, and goes on.
    """
    paths = [str(getattr(item, "path", "") or "") for item in media]
    paths = [path for path in paths if path]
    if not paths:
        return ""
    listed = "\n".join(f"- {path}" for path in paths)
    return (
        "Files that came with this task, by their address on the computer that sent it. Nothing was copied here: "
        "use a file where this machine can read it (the same path on a shared disk, a copy already here), and "
        f"where it cannot be found on machine {target.label}, say so in your reply and go on.\n{listed}"
    )


def agent_missing(target: Machine) -> str:
    """The sentence for an agent's command that is not on its machine."""
    return (
        f"the agent's command was not found on machine {target.label}; "
        "install it there, for the user the machine is registered with"
    )


def ssh_log_path(agent: str) -> Path:
    """The owner-only file a remote agent's ssh writes its own messages to.

    One per agent entry, so a connection's failure is read from its own file.
    ssh appends, which is why a caller about to launch empties it first.
    Measured 2026-10-10 with OpenSSH: under ``-E`` both "connect to host <ip>
    port <n>: Operation timed out" and "Permanently added '[<ip>]:<n>'" land
    here, and stderr holds only the remote side's own output.
    """
    from raven.config.paths import get_logs_dir

    base = get_logs_dir() / "ssh"
    base.mkdir(parents=True, exist_ok=True)
    try:
        base.chmod(0o700)
    except OSError:
        pass
    return base / f"{leaf(agent)}.log"


def read_ssh_log(path: str | os.PathLike[str], limit: int = 4000) -> str:
    """The tail of an ssh log, for :func:`explain`; empty when there is none."""
    try:
        return Path(path).read_text(encoding="utf-8", errors="replace")[-limit:]
    except OSError:
        return ""


def run(target: Machine, command: str, *, timeout: float = 60.0) -> tuple[int, str]:
    """``command``, a POSIX shell line, run once on ``target``: ``(exit code, output)``.

    The registry's own one-shot runner (:func:`raven.ops.transport.runner_from`),
    so a command here and a look through the machine channel reach the machine
    the same way; sent through :func:`posix`, so the user's login shell may be
    any shell. Blocking: call it off the event loop.
    """
    try:
        wrapped = posix(command)
        runner = runner_from(target._row, cap_seconds=timeout)
    except (TransportError, ValueError) as exc:
        raise RemoteMachineError(f"machine {target.label}: {exc}") from None
    return runner(wrapped)


def push(target: Machine, command: str, data: bytes, *, timeout: float = 300.0) -> tuple[int, str]:
    """``command`` on ``target`` with ``data`` on its stdin: how a file is sent there.

    The same ssh options as :func:`run`, and the same :func:`posix` wrapping.
    Output is ``(exit code, stdout, then stderr when it failed)``, the shape
    :func:`run` answers in, so a failure is read by :func:`explain` either way.
    Blocking: call it off the event loop.
    """
    host, port, key, user = _target(target)
    try:
        wrapped = posix(command)
    except ValueError as exc:
        raise RemoteMachineError(f"machine {target.label}: {exc}") from None
    password = connections.password_auth(target._row)
    env = {**os.environ, **ssh_environment(target)} if password else None
    argv = ssh_argv(host, port, key, user=user, password=password) + [wrapped]
    try:
        # `start_new_session` for a password: no controlling terminal for ssh to
        # ask on, so OpenSSH before 8.4 reaches the helper too (transport.askpass_helper).
        done = subprocess.run(
            argv, input=data, capture_output=True, timeout=timeout, env=env, start_new_session=password
        )
    except subprocess.TimeoutExpired:
        return TIMED_OUT_RC, ""
    out = done.stdout.decode("utf-8", "replace")
    if done.returncode != 0 and done.stderr:
        out += "\n" + done.stderr.decode("utf-8", "replace")
    return done.returncode, out


@dataclass(frozen=True)
class Forwarded:
    """``url`` as it is reached through a tunnel: the same path, on this computer's loopback."""

    url: str
    host: str
    """The name the far server knows itself by, the netloc as written: for the ``Host`` header, since the
    request reaches the server from its own machine and not under that name."""


@contextlib.asynccontextmanager
async def tunnel(
    target: Machine, url: str, *, ssh_log: str | os.PathLike[str], connect_timeout: int = 15
) -> AsyncIterator[Forwarded]:
    """``url``'s host and port, as ``target`` resolves them, reached from a port here for as long as the block runs.

    How an openai sub-agent on a machine is called: ssh forwards a port on
    this computer's loopback to the endpoint as the machine sees it, the
    request goes through it, and the ssh ends with the block, so nothing of
    the machine is held between turns. ``http`` only: the tunnel ends at this
    computer's loopback, which an ``https`` certificate would not name, and
    that refusal is one sentence given before any ssh starts. ssh's own words,
    which name the address, go to ``ssh_log`` (:func:`ssh_log_path`), and a
    tunnel that does not open is explained from them (:func:`explain`). The
    port here is chosen free and checked again before ssh starts; one taken in
    between, or one ssh loses the race for, is given up for another
    (:data:`_TUNNEL_TRIES`), never used with whoever took it.
    """
    parts = urlsplit(url)
    if parts.scheme != "http":
        raise RemoteMachineError(
            f"machine {target.label}: an endpoint reached through the machine has to be http, and this one is "
            f"{parts.scheme or 'of no scheme'}: the tunnel ends at this computer's loopback, which a certificate "
            "would not name"
        )
    if not parts.hostname:
        raise RemoteMachineError(f"machine {target.label}: the endpoint's URL names no host to reach from it")
    host, port, key, user = _target(target)
    password = connections.password_auth(target._row)
    # Off the loop: the environment asks a local ``ssh -G`` which host the
    # password prompt will name (``transport.ssh_env``).
    env = {**os.environ, **(await asyncio.to_thread(ssh_environment, target))} if password else None
    # An IPv6 address is bracketed in the forward as it is in the URL, which
    # ``hostname`` gives bare; ssh refuses the bare one.
    far = f"[{parts.hostname}]" if ":" in parts.hostname else parts.hostname
    # The port here is chosen free and checked free again just before ssh is
    # started; one taken in between -- by anything on this computer -- is
    # given up for another, as is one ssh then loses the race for. Without
    # the check a listener that took the port would answer the readiness
    # probe below in ssh's place, and the turn's request would go to it.
    for _ in range(_TUNNEL_TRIES):
        local = _free_port()
        if not _still_free(local):
            continue
        forward = f"127.0.0.1:{local}:{far}:{parts.port or 80}"
        extra = TUNNEL + ("-L", forward, "-E", os.fspath(ssh_log))
        argv = ssh_argv(host, port, key, user=user, connect_timeout=connect_timeout, password=password, extra=extra)
        since = _log_size(ssh_log)
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
                env=env,
                # A password tunnel gets no controlling terminal either.
                start_new_session=password,
            )
        except OSError as exc:
            raise RemoteMachineError(f"machine {target.label}: ssh could not be started: {exc}") from None
        try:
            await _tunnel_up(target, proc, local, ssh_log, since, within=connect_timeout + TUNNEL_READY_S)
        except _PortTakenError:
            await _end_ssh(proc)
            continue
        except BaseException:
            await _end_ssh(proc)
            raise
        try:
            yield Forwarded(url=urlunsplit(parts._replace(netloc=f"127.0.0.1:{local}")), host=parts.netloc)
        finally:
            await _end_ssh(proc)
        return
    raise RemoteMachineError(
        f"machine {target.label}: a port on this computer for the tunnel was taken each of the {_TUNNEL_TRIES} times "
        "one was chosen; something here takes ports as soon as they are free"
    )


#: How many ports are tried for one tunnel before the turn gives up on this computer.
_TUNNEL_TRIES = 3
#: ssh's words when the port chosen here was taken before it could listen (``ExitOnForwardFailure`` then ends it).
_PORT_TAKEN = re.compile(
    r"Address already in use|cannot listen to port|Could not request local forwarding", re.IGNORECASE
)


class _PortTakenError(RemoteMachineError):
    """ssh could not listen on the port chosen here: something on this computer took it first."""


def _still_free(port: int) -> bool:
    """Whether nothing has bound ``port`` on this computer's loopback since it was chosen."""
    with socket.socket() as sock:
        try:
            sock.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


def _log_size(path: str | os.PathLike[str]) -> int:
    try:
        return os.path.getsize(path)
    except OSError:
        return 0


def _log_since(path: str | os.PathLike[str], offset: int, limit: int = 64_000) -> str:
    """What ssh appended to its log from ``offset`` on: this attempt's words, not an earlier attempt's."""
    try:
        with open(path, "rb") as handle:
            handle.seek(offset)
            return handle.read(limit).decode("utf-8", "replace")
    except OSError:
        return ""


async def _end_ssh(proc: Any) -> None:
    if proc.returncode is None:
        with contextlib.suppress(ProcessLookupError):
            proc.terminate()
        try:
            await asyncio.wait_for(proc.wait(), timeout=5.0)
        except asyncio.TimeoutError:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            await proc.wait()


async def _tunnel_up(
    target: Machine, proc: Any, local: int, log: str | os.PathLike[str], since: int, *, within: float
) -> None:
    """Wait for ssh to open the port here, or to end, whichever comes first; an end is explained.

    Raises :class:`_PortTakenError` when ssh ended because the port here was taken
    (its words name only this computer's port), so the caller tries another.
    """
    deadline = time.monotonic() + within
    while True:
        if proc.returncode is not None:
            output = _log_since(log, since)
            if _PORT_TAKEN.search(output):
                raise _PortTakenError(f"port {local} on this computer was taken before ssh could listen on it")
            said = explain(target, proc.returncode, output)
            raise RemoteMachineError(said or f"the tunnel to machine {target.label} ended before it was open")
        try:
            _reader, writer = await asyncio.wait_for(asyncio.open_connection("127.0.0.1", local), timeout=0.5)
        except (OSError, asyncio.TimeoutError):
            pass
        else:
            writer.close()
            with contextlib.suppress(OSError):
                await writer.wait_closed()
            return
        if time.monotonic() >= deadline:
            raise RemoteMachineError(f"machine {target.label} did not open the tunnel within {within:.0f}s")
        await asyncio.sleep(0.1)


def _free_port() -> int:
    """A port on this computer's loopback that nothing listens on now."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def explain(target: Machine, rc: int, output: str) -> str | None:
    """One sentence for a failure ssh itself caused, or None when ssh got through.

    ssh's own words name the address ("connect to host <ip> port <n>"), so
    they are classified here and never passed on.
    """
    if rc == TIMED_OUT_RC:
        return f"machine {target.label} did not answer in time"
    if rc == NOT_FOUND_RC:
        return agent_missing(target)
    if rc != SSH_FAILED_RC:
        return None
    for pattern, said in _SSH_FAILURES:
        if pattern.search(output):
            if said is _LOGIN_REFUSED and connections.password_auth(target._row):
                said = "refused the login; check the password kept for it, and that the machine accepts password logins"
            return f"machine {target.label} {said}"
    return f"the connection to machine {target.label} ended before the agent answered"


# Checked in order; the first that matches names the failure. Wording is ssh's
# own (OpenSSH 9), which is stable across the platforms raven runs on.
_LOGIN_REFUSED = "refused the login; check the key it is registered with"
_SSH_FAILURES = (
    (
        re.compile(r"REMOTE HOST IDENTIFICATION HAS CHANGED|Host key verification failed", re.IGNORECASE),
        "presented a host key that differs from the one on record; check the machine before trusting it again",
    ),
    (
        re.compile(r"Permission denied|Too many authentication failures", re.IGNORECASE),
        _LOGIN_REFUSED,
    ),
    (re.compile(r"timed out", re.IGNORECASE), "did not answer within the connection timeout"),
    (
        re.compile(
            r"Connection refused|No route to host|Network is unreachable|Could not resolve hostname"
            r"|nodename nor servname|Name or service not known",
            re.IGNORECASE,
        ),
        "could not be reached",
    ),
)


def leaf(handle: str) -> str:
    """``handle`` as one safe path component; distinct handles stay distinct.

    A handle is an instance name or a task id. Most are already safe and are
    used as they are; one that had to be changed gets a short digest of the
    original, so ``a b`` and ``a_b`` do not end up sharing a directory.
    """
    raw = str(handle or "")
    safe = _LEAF_UNSAFE.sub("_", raw).lstrip(".")[:_LEAF_MAX] or "session"
    if safe == raw:
        return safe
    return f"{safe}-{hashlib.sha256(raw.encode('utf-8')).hexdigest()[:8]}"


def shell_path(path: str) -> str:
    """``path`` quoted for the machine's shell, leaving a leading ``~`` to it."""
    path = str(path or "").strip() or DEFAULT_ROOT
    if path == "~":
        return "~"
    if path.startswith("~/"):
        rest = path[2:].strip("/")
        return f"~/{shlex.quote(rest)}" if rest else "~"
    return shlex.quote(path)


def shown(target: Machine, path: str) -> str:
    """``path`` on ``target`` the way scp writes it, with the registry's user, address and port.

    The one spelling a path on a machine has in a reply, a record or a note:
    what names a file there, and what :mod:`raven.ops.remote_path` reads back
    as one.
    """
    return remote_path.RemotePath(machine=target.id, path=path, _row=target._row).display()


def _target(target: Machine) -> tuple[str, int, str, str]:
    try:
        return ssh_target(target._row)
    except TransportError as exc:
        raise RemoteMachineError(f"machine {target.label}: {exc}") from None


def ssh_environment(target: Machine) -> dict[str, str]:
    """What ssh to ``target`` needs in its environment: the askpass helper for a password machine, nothing for a key one.

    The one place a password machine's ssh is refused by name -- no password
    kept here any more, or a Windows host -- before anything is started
    (:func:`raven.ops.transport.ssh_env`).
    """
    try:
        return ssh_env(target._row)
    except TransportError as exc:
        raise RemoteMachineError(f"machine {target.label}: {exc}") from None


__all__ = [
    "CHECK_HANDLE",
    "DEFAULT_ROOT",
    "Forwarded",
    "KEEPALIVE",
    "Machine",
    "RemoteMachineError",
    "TUNNEL",
    "agent_missing",
    "explain",
    "failure",
    "leaf",
    "machine",
    "posix",
    "push",
    "read_ssh_log",
    "root_of",
    "run",
    "session_root",
    "shell_path",
    "shown",
    "ssh_command",
    "ssh_environment",
    "leave_agents_running",
    "leaving",
    "staying",
    "ssh_log_path",
    "tunnel",
]
