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

#: What ssh's own exit code is when ssh, rather than the remote command, failed.
SSH_FAILED_RC = 255

#: What a POSIX shell exits with when it cannot find the command it was given.
NOT_FOUND_RC = 127

_ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
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


def remote_command(agent_command: str, *, root: str, env: Mapping[str, str]) -> str:
    """The one argv element sshd hands to the machine user's shell.

    Two shells read it, each once: sshd's ``$SHELL -c`` reads this string, and
    the login shell it execs reads ``inner``. The login shell is what finds the
    agent: measured 2026-10-10, claude, codex and qwen installed through nvm or
    into ``~/.local/bin`` were on the PATH of ``bash -lic`` and of nothing
    less. ``-i`` because that is where nvm's lines live (``.bashrc`` returns
    early for a non-interactive shell); it costs two "no job control" lines on
    stderr and nothing on stdout, which carries the protocol.

    ``env`` travels as ``env K=V`` on the command line because ssh forwards no
    environment without ``AcceptEnv`` on every machine; the values are
    therefore visible in the process list at both ends, and the caller passes
    nothing secret.
    """
    bad = sorted(k for k in env if not _ENV_NAME.fullmatch(k))
    if bad:
        raise ValueError(f"not an environment variable name: {bad}")
    assigns = " ".join(shlex.quote(f"{k}={v}") for k, v in env.items())
    inner = f"exec env {assigns} {agent_command}" if assigns else f"exec {agent_command}"
    where = _shell_path(root)
    return f'mkdir -p {where} && cd {where} && exec "${{SHELL:-/bin/sh}}" -lic {shlex.quote(inner)}'


def launch_command(
    target: Machine,
    agent_command: str,
    *,
    root: str = DEFAULT_ROOT,
    env: Mapping[str, str] | None = None,
    connect_timeout: int = 15,
    ssh_log: str | os.PathLike[str] | None = None,
) -> str:
    """The launch line for ``agent_command`` on ``target``, as one shell-quoted string.

    A string because that is what the pool and the client take (they
    ``shlex.split`` it); ``shlex.join`` makes the split give back exactly this
    argv. Built at launch and held in memory only: it carries the address.

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
    argv += ["--", remote_command(agent_command, root=root, env=env or {})]
    return shlex.join(argv)


def session_dir(target: Machine, *, root: str = DEFAULT_ROOT, handle: str, timeout: float = 30.0) -> str:
    """Make ``<root>/<handle>`` on ``target`` and return its absolute path there.

    One ssh round trip (about 0.3 s measured). Absolute because ``session/new``
    requires it, and resolved on the machine because ``~`` and symlinks mean
    something only there.
    """
    where = f"{_shell_path(root)}/{shlex.quote(leaf(handle))}"
    try:
        run = runner_from(target._row, cap_seconds=timeout)
    except TransportError as exc:
        raise RemoteMachineError(f"machine {target.label}: {exc}") from None
    rc, out = run(f"mkdir -p {where} && cd {where} && pwd -P")
    lines = [line.strip() for line in out.splitlines() if line.strip()]
    if rc == 0 and lines and lines[-1].startswith("/"):
        return lines[-1]
    said = explain(target, rc, out)
    if said is None:
        said = f"could not make a working directory under {root!r} on machine {target.label}"
        if reason := _far_side_words(target, lines):
            said += f": {reason}"
    raise RemoteMachineError(said)


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
    "Permanently added '[<ip>]:<n>'"), so a line carrying it is skipped.
    """
    host = str(target._row.get("host") or "").strip()
    words = [line for line in lines if not (host and host in line)]
    return words[-1][:200] if words else ""


def _shell_path(path: str) -> str:
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
    "DEFAULT_ROOT",
    "KEEPALIVE",
    "Machine",
    "RemoteMachineError",
    "explain",
    "launch_command",
    "leaf",
    "machine",
    "read_ssh_log",
    "remote_command",
    "session_dir",
    "ssh_log_path",
]
