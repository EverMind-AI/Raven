"""An ACP sub-agent that lives on a registered machine, reached over ssh.

The pool and :class:`~raven.acp_client.client.AcpClient` only ever start a local
process and talk to its stdio. For an agent on another machine that local
process is ``ssh``, and the far end of its stdio is the agent, so nothing above
the launch line has to change. What does change is everything that assumed the
agent shares this disk: its working directory, the MCP servers lent through a
local socket, the files a turn is credited with, and the paths handed to it.

This module owns the three things only a remote agent needs:

- the launch line, built from the machine registry at launch and never stored,
  so no address, port or key path reaches a config file, a log line or a model;
- the session directory on the machine, made and resolved to an absolute path
  before ``session/new``, because an agent refuses a cwd that does not exist
  where it runs (measured 2026-10-10 on claude-agent-acp: ``-32602``);
- one sentence for an ssh failure, naming the machine and never its address;
  ssh's own messages, which do name it, go to an owner-only log rather than to
  the stderr the connection journals and quotes.
"""

from __future__ import annotations

import hashlib
import os
import re
import shlex
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from raven.ops import connections
from raven.ops.transport import TIMED_OUT_RC, TransportError, runner_from, ssh_argv, ssh_target

#: Where an agent works on its machine when its entry names nowhere else.
#: Relative to nothing on this computer: ``~`` is the registered user's home
#: there, expanded by the machine's own shell.
DEFAULT_ROOT = "~/raven-work"

#: Options only a session that stays open needs. ``-T`` because a pseudo-tty
#: rewrites the JSON stream (CRLF line ends, echoed input); keepalives because
#: ``ConnectTimeout`` is over once the connection is up, and without them a
#: machine that drops off the network is noticed only by TCP, minutes later.
KEEPALIVE = ("-T", "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=3")

#: The session directory every check of an agent reuses (Test, Connect, the
#: capability record), so pressing Test leaves one directory on the machine
#: rather than one per press.
CHECK_HANDLE = "raven-check"

#: What ssh's own exit code is when ssh, rather than the remote command, failed.
SSH_FAILED_RC = 255

#: What a POSIX shell exits with when it cannot find the command it was given.
NOT_FOUND_RC = 127

_ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
# The fields of a registry row that are the way onto the machine, never shown.
_WAY_IN = frozenset({"host", "port", "user", "key"})
# The shell variable the launch line reads each stdin variable line into.
_ENV_LINE = "__raven_env"
# Login shells the launch starts with ``-lic``: the POSIX family, which reads
# the launch's script the way ``/bin/sh`` does. Wider than the local launch's
# rule (``backends.env._DRIVABLE_SHELLS``, bash and zsh), because that one
# parses the shell's own output to capture an environment, and this one only
# hands the shell a line to run. A csh or fish login shell reads neither, and
# the agent starts from ``/bin/sh`` with the environment that shell's own
# startup made (csh reads ``.cshrc`` for every ``-c``).
_LOGIN_SHELLS = ("ash", "bash", "dash", "ksh", "mksh", "sh", "yash", "zsh")
_LEAF_UNSAFE = re.compile(r"[^A-Za-z0-9._-]")
_LEAF_MAX = 64


class RemoteMachineError(RuntimeError):
    """A remote agent's machine could not be used. The message is safe to show.

    It names the machine by its id and display name only: the registry keeps
    the address out of anything a model reads (``connections.shown``), and an
    error is read by one.
    """


@dataclass(frozen=True)
class Machine:
    """A registered ssh machine. The row, with the address in it, never prints."""

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
        listed (``paths``). The address, port, account and key stay behind
        :func:`launch_command` and :func:`session_dir`.
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
    with "Bad : modifier" (review of #893). ``exec /bin/sh -c '...'`` reads
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


def remote_command(agent_command: str, *, root: str) -> str:
    """The one argv element sshd hands to the machine user's shell.

    Three shells read it, each once: the user's shell reads only
    :func:`posix`'s wrapper, ``/bin/sh`` reads the script that makes the
    root and starts the login shell, and the login shell reads ``inner``. The
    login shell is what finds the agent: measured 2026-10-10, claude, codex
    and qwen installed through nvm or into ``~/.local/bin`` were on the PATH
    of ``bash -lic`` and of nothing less. ``-i`` because that is where nvm's
    lines live (``.bashrc`` returns early for a non-interactive shell). Which
    login shells are started that way, and why, is :data:`_LOGIN_SHELLS`.

    stdout carries the protocol, and a profile or rc file may print on its way
    in: a banner with no line break ended up glued to the first frame (review
    of #893). The client skips a line that is not a frame, so the launch ends
    whatever was printed with a line break the moment before the agent starts,
    and the agent's first frame arrives on a line of its own. Keeping the
    startup off stdout instead does not hold: measured 2026-10-10, macOS's
    bash login startup closed the descriptor the real stdout was kept on.

    No variable is on this line. An entry's ``env`` is where credentials live
    (``OPENAI_API_KEY`` and the like), and an argument is visible in the
    process list at both ends, so the variables arrive on stdin instead, ahead
    of the protocol (:func:`env_preamble`): the login shell reads them up to an
    empty line, one line at a time, and execs the agent on the rest.
    """
    inner = (
        f'while IFS= read -r {_ENV_LINE} && [ -n "${_ENV_LINE}" ]; do eval "export ${_ENV_LINE}"; done; '
        f"echo; exec {agent_command}"
    )
    where = shell_path(root)
    script = (
        f"mkdir -p {where} && cd {where} || exit; "
        f'case "${{SHELL##*/}}" in {"|".join(_LOGIN_SHELLS)}) exec "$SHELL" -lic {shlex.quote(inner)};; esac; '
        f"exec /bin/sh -c {shlex.quote(inner)}"
    )
    return posix(script)


def launch_command(
    target: Machine,
    agent_command: str,
    *,
    root: str = DEFAULT_ROOT,
    connect_timeout: int = 15,
    ssh_log: str | os.PathLike[str] | None = None,
) -> str:
    """The launch line for ``agent_command`` on ``target``, as one shell-quoted string.

    A string because that is what the pool and the client take (they
    ``shlex.split`` it); ``shlex.join`` makes the split give back exactly this
    argv. Built at launch and held in memory only: it carries the address. It
    carries no variable: those go on stdin (:func:`env_preamble`).

    ``ssh_log`` (see :func:`ssh_log_path`) is where ssh writes its own
    messages instead of stderr, so stderr carries only what the far side
    writes: the connection journals stderr and quotes its tail in errors, and
    ssh's own lines name the address.
    """
    host, port, key, user = _target(target)
    extra = KEEPALIVE + (("-E", os.fspath(ssh_log)) if ssh_log is not None else ())
    argv = ssh_argv(host, port, key, user=user, connect_timeout=connect_timeout, extra=extra)
    # ``--`` so nothing in the remote command is read as an option to ssh,
    # which also parses options that follow the destination.
    argv += ["--", remote_command(agent_command, root=root)]
    return shlex.join(argv)


def env_preamble(env: Mapping[str, str]) -> bytes:
    """``env`` as the lines the launch line's shell reads from stdin, then an empty line.

    Written to the agent's stdin before the first protocol frame, so the values
    never appear in an argument list, a log or the frame journal. Each line is
    ``NAME=<value quoted for the shell>``, exported by the login shell; the
    empty line ends them and is always sent, so an entry with no variables
    still lets the agent start. A value holding a newline would end its line
    early, so it is refused, by name and never by value.
    """
    bad = sorted(k for k in env if not _ENV_NAME.fullmatch(k))
    if bad:
        raise ValueError(f"not an environment variable name: {bad}")
    broken = sorted(k for k, v in env.items() if "\n" in v or "\r" in v or "\0" in v)
    if broken:
        raise ValueError(f"environment variable {broken} holds a line break, which cannot be sent to another machine")
    lines = "".join(f"{k}={shlex.quote(v)}\n" for k, v in env.items())
    return (lines + "\n").encode("utf-8")


def session_dir(target: Machine, *, root: str = DEFAULT_ROOT, handle: str, timeout: float = 30.0) -> str:
    """Make ``<root>/<handle>`` on ``target`` and return its absolute path there.

    One ssh round trip (about 0.3 s measured). Absolute because ``session/new``
    requires it, and resolved on the machine because ``~`` and symlinks mean
    something only there.
    """
    where = f"{shell_path(root)}/{shlex.quote(leaf(handle))}"
    try:
        command = posix(f"mkdir -p {where} && cd {where} && pwd -P")
        run = runner_from(target._row, cap_seconds=timeout)
    except (TransportError, ValueError) as exc:
        raise RemoteMachineError(f"machine {target.label}: {exc}") from None
    rc, out = run(command)
    lines = [line.strip() for line in out.splitlines() if line.strip()]
    if rc == 0 and lines and lines[-1].startswith("/"):
        return lines[-1]
    said = explain(target, rc, out)
    if said is None:
        said = f"could not make a working directory under {root!r} on machine {target.label}"
        if reason := _far_side_words(target, lines):
            said += f": {reason}"
    raise RemoteMachineError(said)


@dataclass(frozen=True)
class RemoteLaunch:
    """One launch of an agent on a registered machine, resolved from its entry."""

    target: Machine
    root: str
    log: Path
    command: str = field(repr=False)
    """The launch line. It carries the address, so it is never printed."""
    preamble: bytes = field(default=b"\n", repr=False)
    """The variables for the agent's stdin (:func:`env_preamble`); they may be secrets."""

    @property
    def cwd(self) -> str:
        """Where the local ssh starts.

        Fixed rather than the caller's workspace: the pool keys a connection on
        its launch arguments, so a cwd that followed the workspace would start
        a second ssh, and drop the first one's sessions, for every new one.
        """
        return str(Path.home())


def prepare_launch(
    agent: str, machine_id: str, agent_command: str, *, remote_cwd: str | None, env: Mapping[str, str]
) -> RemoteLaunch:
    """Resolve the machine and build the launch line, with an emptied ssh log.

    Blocking (it reads the registry): call it off the event loop. ``env`` is the
    entry's own; the role variable every sub-agent is started with goes first,
    as the local launch puts it. Raises :class:`RemoteMachineError`.
    """
    from raven.home import subagent_role_env

    target = machine(machine_id)
    log = ssh_log_path(agent)
    try:
        # Emptied per launch, so a failure is read from this launch's lines.
        log.write_text("", encoding="utf-8")
    except OSError:
        pass
    root = (remote_cwd or "").strip() or DEFAULT_ROOT
    try:
        line = launch_command(target, agent_command, root=root, ssh_log=log)
        preamble = env_preamble({**subagent_role_env(), **env})
    except ValueError as exc:
        raise RemoteMachineError(f"agent {agent!r} on machine {target.label}: {exc}") from None
    return RemoteLaunch(target=target, root=root, log=log, command=line, preamble=preamble)


def failure(launch: RemoteLaunch, exc: BaseException) -> str | None:
    """The sentence for a connection that ended, when ssh or the far shell ended it."""
    rc = getattr(exc, "returncode", None)
    if rc is None:
        return None
    return explain(launch.target, rc, read_ssh_log(launch.log))


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
    argv = ssh_argv(host, port, key, user=user) + [wrapped]
    try:
        done = subprocess.run(argv, input=data, capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return TIMED_OUT_RC, ""
    out = done.stdout.decode("utf-8", "replace")
    if done.returncode != 0 and done.stderr:
        out += "\n" + done.stderr.decode("utf-8", "replace")
    return done.returncode, out


def explain(target: Machine, rc: int, output: str) -> str | None:
    """One sentence for a failure ssh itself caused, or None when ssh got through.

    ssh's own words name the address ("connect to host <ip> port <n>"), so
    they are classified here and never passed on.
    """
    if rc == TIMED_OUT_RC:
        return f"machine {target.label} did not answer in time"
    if rc == NOT_FOUND_RC:
        return (
            f"the agent's command was not found on machine {target.label}; "
            "install it there, for the user the machine is registered with"
        )
    if rc != SSH_FAILED_RC:
        return None
    for pattern, said in _SSH_FAILURES:
        if pattern.search(output):
            return f"machine {target.label} {said}"
    return f"the connection to machine {target.label} ended before the agent answered"


# Checked in order; the first that matches names the failure. Wording is ssh's
# own (OpenSSH 9), which is stable across the platforms raven runs on.
_SSH_FAILURES = (
    (
        re.compile(r"REMOTE HOST IDENTIFICATION HAS CHANGED|Host key verification failed", re.IGNORECASE),
        "presented a host key that differs from the one on record; check the machine before trusting it again",
    ),
    (
        re.compile(r"Permission denied|Too many authentication failures", re.IGNORECASE),
        "refused the login; check the key it is registered with",
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


def _far_side_words(target: Machine, lines: list[str]) -> str:
    """The machine's own last word on a command that failed past ssh, or ``""``.

    The machine's reason is the useful part ("No space left on device",
    measured 2026-10-10 on a registered machine whose disk had filled), and it
    names nothing of how raven got there. The one-shot runner shares stderr
    with ssh, though, and ssh's lines name the address (on a first connect,
    "Permanently added '[<ip>]:<n>'"), so ssh's own lines are skipped.

    Recognised by ssh's own shapes, not by the host appearing anywhere: a host
    is often a plain word, and "dev" would otherwise take "No space left on
    device" with it (review of #893).
    """
    host = str(target._row.get("host") or "").strip()
    forms = (f"[{host}]", f"host {host} ", f"@{host}:", f"to {host} ") if host else ()
    words = [line for line in lines if not _SSH_OWN_LINE.match(line) and not any(form in line for form in forms)]
    return words[-1][:200] if words else ""


# How ssh's own diagnostics begin, address or not. Matched at the start of a
# line, so a machine's message that merely mentions one of these words stays.
_SSH_OWN_LINE = re.compile(
    r"(ssh: |Warning: Permanently added |@@@|Host key verification failed|Connection (to|closed by) |"
    r"kex_exchange_identification|Load key )"
)


def shell_path(path: str) -> str:
    """``path`` quoted for the machine's shell, leaving a leading ``~`` to it."""
    path = str(path or "").strip() or DEFAULT_ROOT
    if path == "~":
        return "~"
    if path.startswith("~/"):
        rest = path[2:].strip("/")
        return f"~/{shlex.quote(rest)}" if rest else "~"
    return shlex.quote(path)


def _target(target: Machine) -> tuple[str, int, str, str]:
    try:
        return ssh_target(target._row)
    except TransportError as exc:
        raise RemoteMachineError(f"machine {target.label}: {exc}") from None


__all__ = [
    "CHECK_HANDLE",
    "DEFAULT_ROOT",
    "KEEPALIVE",
    "Machine",
    "RemoteLaunch",
    "RemoteMachineError",
    "env_preamble",
    "explain",
    "failure",
    "launch_command",
    "leaf",
    "machine",
    "posix",
    "prepare_launch",
    "push",
    "read_ssh_log",
    "remote_command",
    "run",
    "session_dir",
    "shell_path",
    "ssh_log_path",
]
