"""How a machine is reached, so nothing above this has to know.

A machine the owner registered is not always a different computer. Someone who
writes their own solver usually runs it where they wrote it and installs raven on
that same box: there is no host to reach and no key to present, and a command is
simply run. Measured need, 2026-08-18 -- the CFD group's in-house Fortran code
lives on the machine raven would be installed on.

Everything above this seam names a machine and nothing else. Which runner answers
is a property of the connection, so a campaign, a tool call and a task statement
all read the same whether the work runs across the room or in this process's own
filesystem.
"""

from __future__ import annotations

import os
import subprocess
from typing import Any

from oncall_flow.backend import JobBackendError
from oncall_flow.docker_backend import CommandRunner
from raven.ops import transport as trunk
from raven.ops.connections import AUTH_PASSWORD, password_auth

LOCAL = "local"
SSH = "ssh"


def transport_of(meta: dict[str, Any]) -> str:
    """How this campaign or connection is reached; ``ssh`` unless it says otherwise.

    Defaulting to ssh rather than to "whatever has an address" keeps every
    campaign written before this seam existed meaning exactly what it meant.
    """
    return LOCAL if str(meta.get("transport") or SSH).strip().lower() == LOCAL else SSH


def logs_in_by_password(meta: dict[str, Any]) -> bool:
    """Whether this meta reaches its machine over ssh with the password kept for it (``auth: password``)."""
    return transport_of(meta) == SSH and password_auth(meta)


def password_row(meta: dict[str, Any], *, what: str = "campaign") -> dict[str, Any]:
    """The registry row trunk logs in with, for a meta that logs in by password.

    Its id is the meta's connection, because that is what the password is kept
    under: a meta that names none has nothing to log in with, and is refused
    saying so rather than sent to a machine that would refuse it. The address
    and account are the registry's, never the meta's: a kept password goes
    only to the machine it was registered for, and a meta -- which the model's
    own tool calls write -- that carries another host, port or user for such a
    connection is refused before any ssh is started. A key machine's meta may
    still override those, since a key never leaves this computer.
    """
    from oncall_flow.connections import get

    named = str(meta.get("connection") or "").strip()
    if not named:
        raise JobBackendError(
            f"this {what} logs in with a password and names no connection: a password is kept for a "
            "registered machine, under its id, so the machine has to be named by its connection"
        )
    registered = get(named)
    if registered is None:
        raise JobBackendError(
            f"this {what} names connection {named!r}, which is not in the connection registry; the password kept "
            "for a machine goes only to the machine it was registered for"
        )
    host = str(registered.get("host") or "").strip()
    port = int(registered.get("port") or 22)
    user = str(registered.get("user") or "root")
    for field, given, theirs in (
        ("host", str(meta.get("host") or "").strip().lower(), host.lower()),
        ("port", int(meta.get("port") or port), port),
        ("user", str(meta.get("user") or user), user),
    ):
        if given and given != theirs:
            raise JobBackendError(
                f"this {what} names connection {named!r}, a machine logged in to with the password kept for it, but "
                f"carries a {field} of its own that differs from the registered one: a kept password goes only to "
                "the machine it was registered for -- leave host, port and user to the registry, or register that "
                "other machine"
            )
    return {"id": named, "transport": SSH, "host": host, "port": port, "user": user, "auth": AUTH_PASSWORD}


def password_env(row: dict[str, Any]) -> dict[str, str]:
    """This process's environment plus what ssh needs to log in as ``row``, for a caller that starts ssh itself.

    rsync is that caller: it runs ssh with the environment it was given. Trunk's
    refusals -- no password kept here, a Windows computer -- arrive as a
    :class:`JobBackendError` in trunk's words.
    """
    try:
        return {**os.environ, **trunk.ssh_env(row)}
    except trunk.TransportError as exc:
        raise JobBackendError(str(exc)) from None


# What ``timeout`` returns when it kills something, reused so a caller reads one
# code whichever way the cap was applied.
TIMED_OUT_RC = 124


def make_local_runner(cap_seconds: float = 600.0) -> CommandRunner:
    """Run the command here, answering in the shape an ssh runner answers in.

    ``shell=True`` because that is what the ssh runner effectively gives a caller:
    the remote sshd puts the string through a shell, so the commands written for
    it -- pipes, redirects, ``&&`` chains, heredocs -- keep working unchanged.

    The cap is enforced here rather than by wrapping the command in ``timeout``.
    That binary is GNU coreutils and macOS does not ship it, which is exactly the
    machine a local connection is most likely to be: measured 2026-08-19, the
    first local look came back ``exit 127, /bin/sh: timeout: command not found``.
    """

    def run(cmd: str) -> tuple[int, str]:
        try:
            proc = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=cap_seconds)
        except subprocess.TimeoutExpired as exc:
            out = exc.stdout or b""
            body = out.decode(errors="replace") if isinstance(out, bytes) else out
            return TIMED_OUT_RC, body
        tail = f"\n{proc.stderr}" if proc.stderr and proc.returncode != 0 else ""
        return proc.returncode, proc.stdout + tail

    return run


def runner_from(meta: dict[str, Any], *, what: str = "campaign", cap_seconds: float | None = None) -> CommandRunner:
    """The runner this meta -- or connection row -- calls for.

    Raises rather than handing back something half-connected: a runner built on an
    empty address fails later, at the staging step, with a message that reads as a
    network fault rather than as a campaign that was never told where to run.
    Measured 2026-08-17, and again 2026-08-18 when the failure reached the owner
    as "your machine refused the connection" while that machine was answering in
    under 200ms.

    A machine that takes a password is logged in to by trunk's runner
    (:func:`raven.ops.transport.runner_from`) rather than by a copy of its login
    here: the options that make each password connection log in on its own, and
    the askpass hook that hands ssh the password kept for the machine, are
    trunk's, and a copy drifts the first time either changes. Trunk's refusals
    -- no password kept here, a Windows computer -- arrive as a
    :class:`JobBackendError` in trunk's words, before anything is started.
    """
    if transport_of(meta) == LOCAL:
        # The caller's cap, not this module's default. Looking at a machine is
        # capped at one minute whichever way it is reached; without this the local
        # transport quietly allowed ten, because the cap for a remote look rides
        # on the `timeout` wrapper and a local one has only the runner.
        return make_local_runner(cap_seconds) if cap_seconds else make_local_runner()

    from oncall_flow.docker_backend import make_ssh_runner

    host = str(meta.get("host") or "").strip()
    named = meta.get("connection")
    if not host and named:
        # Only when a connection was named: that one resolved to nothing, so the
        # registry lost it, and the failure used to reach the owner as their
        # machine refusing the connection. A meta with neither host nor
        # connection is the hand-written shape that predates all of this, and is
        # left to fail where it always did rather than newly refused here.
        raise JobBackendError(
            f"this {what} has no address to run on: connection {named!r} is not in the connection registry"
        )
    if logs_in_by_password(meta):
        try:
            return trunk.runner_from(password_row(meta, what=what), cap_seconds=cap_seconds)
        except trunk.TransportError as exc:
            raise JobBackendError(str(exc)) from None
    return make_ssh_runner(
        host,
        int(meta.get("port") or 22),
        os.path.expanduser(str(meta.get("key") or "~/.ssh/id_rsa")),
        # A connection names the account to log in as. Dropping it would make that
        # field one more setting that is written, accepted and does nothing.
        user=str(meta.get("user") or "root"),
    )
