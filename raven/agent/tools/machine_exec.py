"""Run one command on a machine the owner registered.

``exec`` runs on the computer the loop itself lives on. The owner's code and
their cases live on the machines in the connection registry, which may be a
different box or may be this one -- and the credentials for a different box
belong to the connection rather than to the loop. So "what is in that
directory / what did the solver print / how big is that output" is answered
here, by naming the machine, and ``exec`` routes to this the moment it is
given one.

This channel exists so the address never has to: with no ``machine``
parameter, every remote look is a raw ``ssh -p <port> root@<ip> ...`` typed
from an address the task statement had to carry -- measured 2026-09-03, a
whole field run where the registry's own projection (id and capabilities, no
host, no port, no key) was bypassed on every call because the raw address sat
in context. The registry resolves the id here, below the model.

Two things this refuses that a plain shell would not:

**Anything that outlives the call.** ``nohup``, a trailing ``&``, ``screen``,
``tmux``, ``at``, ``crontab``: on a registered machine, work that keeps
running belongs to a governed job runner (the on-call agent's ``ops_submit``),
which is what makes its budget enforceable, its state survive a restart, and
its results arrive when they are due. On the loop's own computer there is no
ledger to escape, so the plain shell keeps that freedom.

**Anything slower than a look.** Every command is capped, and the cap is
enforced on the machine itself so a kill reaches both ends -- a cap that only
killed the local ssh client left the remote process running (measured
2026-09-02: an orphaned training run held a GPU at 100% after its local
caller died). Something that needs longer is a job, and a job has a different
home.

The refusal above is a hint, not the fence. The fence is that the command runs
in a process group of its own and the group is stopped when the call ends,
whether the shell finished or the cap fired: reviewed 2026-09-07, a command
that backgrounded something with output redirected (``python train.py > log
2>&1 & echo ok``) slipped past the old ``&``-at-the-end guard, and a remote
``timeout`` wrapper had nothing left to kill once the shell's foreground half
had returned -- the call reported exit 0 in under a second while the child ran
on unbounded. Now the wrapper sweeps the group afterwards and says so in the
output, so a detach that was not refused is still not a detach.
"""

from __future__ import annotations

import asyncio
import re
import shlex
from typing import Any

# Long enough to tail a large log or hash a directory; far short of any solver
# round measured on these campaigns (16 to 30 minutes). That gap is the boundary.
_TIMEOUT_S = 60
_MAX_BYTES = 20000

# Shapes whose only purpose is to outlive the call. Matched loosely on purpose:
# this is here to stop the casual reach for a background job, and it says which
# door does that job instead.
_DETACH = (
    (r"\bnohup\b", "nohup"),
    # A single & that is not && (a chain), >& / <& / &> (redirections) or |&
    # (a pipe). Anywhere, not only at the end: `sleep 300 & echo started`,
    # `(sleep 300 &)` and `bash -c 'sleep 300 &'` all background something
    # (reviewed 2026-09-07). A literal & inside quotes is refused too; the
    # wrapper below is what actually holds the line, so this can stay loose.
    (r"(?<![&<>|])&(?![&>])", "a backgrounding &"),
    (r"\bsetsid\b", "setsid"),
    (r"\bdisown\b", "disown"),
    (r"\bscreen\b", "screen"),
    (r"\btmux\b", "tmux"),
    (r"\bat\s+(now|\d)", "at"),
    (r"\bcrontab\b", "crontab"),
    (r"\bsystemd-run\b", "systemd-run"),
)


def _detaching(command: str) -> str | None:
    for pattern, name in _DETACH:
        if re.search(pattern, command):
            return name
    return None


def machines_registered() -> bool:
    """Whether this install has any connection to offer the channel for.

    Read per schema build rather than cached: the registry can be written
    mid-session, and the loop rebuilds the tool schema every turn.
    """
    try:
        from raven.ops.connections import load

        return bool(load())
    except Exception:  # noqa: BLE001 -- a malformed registry must leave the plain shell working
        return False


def _runner_for_connection(conn_id: str, *, cap_seconds: float | None = None):
    """A command runner for a machine named by id, plus its row.

    Returns ``(None, message)`` when the id is unknown or unreachable, so the
    caller can hand the reason back rather than a traceback.
    """
    from raven.ops.connections import load, shown

    # Looked up here rather than through a registry-side ``get``: main keeps
    # the registry's readers to what the CLI and this lane need (0fd38ef8),
    # and the model-facing rendering of the list lives with the caller.
    rows = load()
    wanted = str(conn_id or "").strip()
    row = next((r for r in rows if str(r.get("id") or "").strip() == wanted), None)
    if row is None:
        listing = "\n".join(
            f"  {r.get('display_name') or r.get('id')}   (id {r.get('id')})"
            + "".join(f"   {k} {v}" for k, v in shown(r).items() if k not in ("id", "display_name"))
            for r in rows
        )
        return None, (
            f"No connection with id {conn_id!r}.\n"
            + (f"Connections this instance can run on:\n{listing}" if rows else "No connection is registered.")
        )
    from raven.ops.transport import TransportError, runner_from

    try:
        runner = runner_from(row, cap_seconds=cap_seconds)
    except TransportError as exc:
        return None, f"Cannot reach {conn_id!r}: {exc}"
    return runner, row


# The look itself, run through bash on the machine. `$1` is the command.
#
# `set -m` puts the command's shell in a process group of its own (job control
# does that for a backgrounded job, no `setsid` binary needed -- macOS has
# none), so everything it starts can be signalled as one group: the children a
# plain `&` leaves behind stay in that group, and `setsid`/`nohup`/`disown`
# are refused before this runs. A watchdog subshell stops the group at the cap
# and the exit code becomes 124, the code GNU `timeout` uses, so the caller
# reads one code for a cap kill however the machine is reached. After the
# shell returns, anything still alive in the group is stopped and the output
# says so: a look ends when the call ends. `exec 2>/dev/null` hides the
# wrapper's own job-control chatter ("Terminated: 15"); the command's stderr
# was merged into stdout before that, inside its own shell.
#
# The watchdog is a group of its own as well, and holds neither stdout nor
# stderr: a `sleep` left inside it would otherwise keep the output pipe open
# after the command was done, and ssh (or the local runner) waits for that
# pipe to close -- reviewed 2026-09-07, a swept look took the whole cap to
# return for exactly that reason.
_LOOK = """exec 2>/dev/null
set -m
bash -c "$1" 2>&1 &
pid=$!
( sleep {cap}; kill -TERM -- -$pid; sleep 5; kill -KILL -- -$pid ) >/dev/null 2>&1 &
wd=$!
set +m
wait $pid; rc=$?
[ $rc -ge 128 ] && [ $SECONDS -ge {cap} ] && rc=124
if kill -0 -- -$pid; then
  echo {note}
  kill -TERM -- -$pid; sleep 1; kill -KILL -- -$pid
fi
kill -KILL -- -$wd
exit $rc
"""

_SWEPT_NOTE = (
    "[raven: this command left work running in the background; it was stopped, because a look "
    "on a registered machine ends when the call ends. Work that has to keep running there is a "
    "job for the on-call agent's ops_submit.]"
)


def look_script(command: str) -> str:
    """The bash line that runs ``command`` on a machine as one capped, swept group."""
    script = _LOOK.format(cap=_TIMEOUT_S, note=shlex.quote(_SWEPT_NOTE))
    return f"bash -c {shlex.quote(script)} raven-look {shlex.quote(command)}"


async def run_on_machine(command: str, *, connection: str, cwd: str | None = None) -> str:
    """Run one command on a registered machine and return what it said."""
    command = (command or "").strip()
    if not command:
        return "No command given."
    detached = _detaching(command)
    if detached:
        # Refused before anything is sent, so a refusal never half-runs.
        return (
            f"Refusing this command: it uses {detached}, which is a way to leave work "
            "running after this call returns. On a registered machine that work belongs "
            "to a governed job runner -- the on-call agent's ops_submit -- which makes "
            "its compute budget enforceable, its state survive a restart, and its "
            "results arrive when they are due. This channel is for looking at the "
            f"machine; anything it runs is killed after {_TIMEOUT_S}s, and anything it leaves "
            "running in the background is stopped when the call returns. Nothing was run."
        )

    # The runner's own cap is a backstop behind the wrapper's: the wrapper
    # fires first, keeps the output and reports 124; the runner's group kill is
    # for a wrapper that could not run at all (a machine with no bash).
    runner, row = _runner_for_connection(connection, cap_seconds=_TIMEOUT_S + 15)
    if runner is None:
        return row  # the message explaining why not

    where = (cwd or ".").rstrip("/") or "/"
    # bash, not sh: /bin/sh is dash on these hosts, and shell gets written the
    # way bash is written -- `[[ ]]`, brace expansion, arrays. The cap and the
    # sweep are the wrapper's own (see _LOOK), not GNU `timeout`: that binary
    # is coreutils, macOS does not ship it, and a local connection is most
    # likely to BE a mac -- measured 2026-08-19, the first local look came back
    # `exit 127, timeout: command not found`.
    from raven.ops.transport import TIMED_OUT_RC

    body = look_script(command)
    # to_thread: the runner is a synchronous subprocess.run, and calling it
    # bare from this async def would freeze the whole event loop for up to
    # _TIMEOUT_S per look. Measured 2026-08-27 on the fork: two back-to-back
    # remote probes blocked frame reading long enough that a concurrent
    # `session/new` sat unparsed for the host's full 120s readyTimeoutMs and
    # the second instance was declared dead.
    rc, out = await asyncio.to_thread(runner, f"cd {shlex.quote(where)} 2>/dev/null || exit 66; {body}")
    text = out if len(out) <= _MAX_BYTES else out[:_MAX_BYTES] + f"\n...[truncated, {len(out)} bytes total]"
    head = f"on {_where(row)} in {where} (exit {rc})"
    if rc == 66:
        return f"{head}: no such directory. Nothing was run."
    if rc == TIMED_OUT_RC:
        return (
            f"{head}: killed at the {_TIMEOUT_S}s limit -- the host answered, the "
            f"command did not finish. Anything that takes longer is a job, and a job "
            f"belongs to the on-call agent's ops_submit.\n{text}"
        )
    return f"{head}\n{text}" if text.strip() else f"{head}, no output"


def _where(row: dict[str, Any]) -> str:
    return str(row.get("display_name") or row.get("id") or "the machine")
