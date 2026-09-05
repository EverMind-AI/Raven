"""Run a command on a machine the owner registered.

``exec`` runs on the computer the loop itself lives on. The owner's code and
their cases live on the machines in the connection registry, which may be a
different box or may be this one -- and the credentials for a different box
belong to the connection rather than to the loop. So "what is in that directory /
what did the solver print / how big is that output" is answered here, by naming
the machine. The fork taught its own ``exec`` a ``machine`` parameter that
routed here; the trunk's exec cannot be wrapped by a plugin today (the shadow
rule replaces a built-in outright, and no grant hands the shadow its inner
tool), so the machine face is its own contributed tool, ``ops_exec`` -- same
verbs, its own name -- and the same-name shadow stays an open ruling (see the
draft's deviation table). ``waiting_only``/``waiting_note`` are the local
sleep-clip halves of that shadow and travel here for it.

Two things this refuses that a plain shell would not:

**Anything that outlives the call.** ``nohup``, a trailing ``&``, ``screen``,
``tmux``, ``at``, ``crontab``: on a registered machine, work that keeps running
belongs to ``ops_submit``, which is what makes its budget enforceable, its state
survive a restart, and its results wake the loop when they are due. On the loop's
own computer there is no ledger to escape, so a plain shell keeps that freedom.

**Anything slower than a look.** Every command is capped; something that needs
longer is a job, and a job has a different home.
"""

from __future__ import annotations

import asyncio
import re
import shlex
from typing import Any

from raven.contracts.tool import Tool

# Long enough to tail a large log or hash a directory; far short of any solver
# round measured on these campaigns (16 to 30 minutes). That gap is the boundary.
_TIMEOUT_S = 60
_TIMED_OUT_RC = 124
_MAX_BYTES = 20000

# Shapes whose only purpose is to outlive the call. Matched loosely on purpose:
# this is here to stop the casual reach for a background job, and it says which
# tool does that job instead.
_DETACH = (
    (r"\bnohup\b", "nohup"),
    (r"&\s*$", "a trailing &"),
    (r"&\s*;", "a backgrounding &"),
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


# A wait long enough to be a wake. Two seconds settles a filesystem; a minute of
# it is the loop standing in for the scheduler.
_WAIT_CAP_S = 5


def waiting_only(command: str) -> int:
    """Seconds this command spends purely waiting, or 0 if it does something.

    Only a leading ``sleep`` counts. ``build && sleep 2 && check`` waits for a
    reason and is left alone; ``sleep 290 && echo check`` is a wake written by
    hand -- twelve of those in one session on 2026-08-19, the longest 290s, four
    of them after being told on the machine path that anything longer than a
    minute is a job.
    """
    import re

    m = re.match(r"\s*sleep\s+(\d+(?:\.\d+)?)\b", command or "")
    if not m:
        return 0
    seconds = int(float(m.group(1)))
    return seconds if seconds > _WAIT_CAP_S else 0


def waiting_note(asked_for: int, capped_to: int) -> str:
    """What a clipped wait says, in the words the machine path already uses."""
    return (
        f"\n\nThat asked to wait {asked_for}s and was cut to {capped_to}s. Waiting inside a "
        f"call keeps this turn open: the window has to stay up, nothing survives a restart, "
        f"and the context grows for the whole wait. A wait long enough to matter is a wake --"
        f" ops_check_later(eta_seconds={asked_for}, basis=...) ends the turn and brings you "
        f"back to the ledger when it is worth looking."
    )


def _runner_for_connection(conn_id: str, *, cap_seconds: float | None = None):
    """A command runner for a machine named directly, plus a meta-shaped dict.

    Returns ``(None, message)`` when the id is unknown, so the caller can hand
    the reason back rather than a traceback.
    """
    from oncall_flow.connections import get

    row = get(conn_id)
    if row is None:
        from oncall_flow.connections import describe

        return None, (f"No connection with id {conn_id!r}.\n" + describe())
    from oncall_flow.backend import JobBackendError
    from oncall_flow.transport import runner_from

    try:
        runner = runner_from(row, what="machine", cap_seconds=cap_seconds)
    except JobBackendError as exc:
        return None, f"Cannot reach {conn_id!r}: {exc}"
    # The transport travels with it: the caller decides from this whether the cap
    # can go through the `timeout` binary, and a meta that omits it silently reads
    # as ssh -- which is how the first local look ended in "timeout: command not
    # found" even after the runner already knew better.
    return runner, {"connection": conn_id, "remote_dir": "", "transport": row.get("transport") or "ssh"}


def resolve_machine(name: str) -> tuple[str, str]:
    """A machine named by a caller, as ``(connection_id, campaign)``.

    One name, two kinds of thing: an id from the connection list, or a campaign
    whose declaration already says which machine it runs on. The registry decides
    which -- asking the caller to know would be asking them to keep two
    namespaces apart for no reason.
    """
    from oncall_flow.connections import get

    return (name, "") if get(name) is not None else ("", name)


async def run_on_machine(
    command: str, *, campaign: str = "", connection: str = "", cwd: str | None = None, ledger: str | None = None
) -> str:
    """Run one command on a registered machine and return what it said.

    Shared by ``exec`` (when it is given a machine) and by this module's tool, so
    there is one implementation of what running on someone else's machine means.
    """
    command = (command or "").strip()
    if not command:
        return "No command given."
    detached = _detaching(command)
    if detached:
        # Refused before anything is sent, so a refusal never half-runs.
        return (
            f"Refusing this command: it uses {detached}, which is a way to leave work "
            "running after this call returns. Submit work with ops_submit -- that is what "
            "makes its compute budget enforceable, its state survive a restart, and its "
            "results wake you when they are due. This tool is for looking at the machine; "
            f"anything it runs is killed after {_TIMEOUT_S}s. Nothing was run."
        )

    # A machine named directly, with no campaign in the picture. That is the
    # order the work actually happens in: the owner names a path, the machine
    # is chosen from the connection list, and what is on it is what tells you
    # what a campaign should run. Requiring a campaign first inverted that --
    # measured 2026-08-18, an arm had the machine id and the path in hand and
    # no tool that took both, so it created an empty campaign to get a runner
    # and spent the next three minutes repairing it.
    if connection:
        runner, meta = _runner_for_connection(connection, cap_seconds=_TIMEOUT_S)
        if runner is None:
            return meta  # the message explaining why not
    else:
        from oncall_flow.tools.ops_case_dict import _campaign

        try:
            resolved = _campaign(campaign, ledger)
        except ValueError as exc:
            # Only once resolution has actually failed: a name that resolves is
            # not worth second-guessing, and checking first made this branch
            # answer for campaigns the caller had named perfectly well.
            return _unknown_machine(campaign) or (
                f"{exc}\n"
                "If you are looking at a machine before any campaign exists, name a machine "
                "instead -- ops_connections lists them and their ids."
            )
        if isinstance(resolved, str):
            return _unknown_machine(campaign) or resolved
        backend, _led, _cdir, meta = resolved
        runner = getattr(backend, "_run", None)
        if runner is None:
            return "This campaign's backend cannot run commands on a host."

    where = (cwd or str(meta.get("remote_dir") or "") or ".").rstrip("/") or "/"
    # bash, not sh: /bin/sh is dash on these hosts, and shell gets written the way
    # bash is written -- `[[ ]]`, brace expansion, arrays. Verified present
    # (5.1.16) before relying on it.
    #
    # The cap goes through `timeout` only where that binary exists. It is GNU
    # coreutils, macOS does not ship it, and a local connection is most likely to
    # BE a mac: the first local look came back `exit 127, timeout: command not
    # found`. Where it is missing the runner caps the call itself.
    from oncall_flow.transport import LOCAL, transport_of

    body = f"bash -c {shlex.quote(command)} 2>&1"
    if transport_of(meta) != LOCAL:
        body = f"timeout {_TIMEOUT_S} {body}"
    # to_thread, the same idiom both backends' `_arun` uses: the runner is a
    # synchronous subprocess.run (transport.py), and calling it bare from this
    # async def froze the server's whole event loop for up to _TIMEOUT_S per
    # look. Measured 2026-08-27: two back-to-back remote docker probes blocked
    # frame reading long enough that a concurrent `session/new` sat unparsed
    # for the host's full 120s readyTimeoutMs and the second instance was
    # declared dead. Wakes and `session/cancel` stalled the same way.
    rc, out = await asyncio.to_thread(runner, f"cd {shlex.quote(where)} 2>/dev/null || exit 66; {body}")
    body = out if len(out) <= _MAX_BYTES else (out[:_MAX_BYTES] + f"\n...[truncated, {len(out)} bytes total]")
    head = f"on {_where(meta)} in {where} (exit {rc})"
    if rc == 66:
        return f"{head}: no such directory. Nothing was run."
    if rc == _TIMED_OUT_RC:
        return (
            f"{head}: killed at the {_TIMEOUT_S}s limit -- the host answered, the "
            f"command did not finish. Anything that takes longer is a job, and a job "
            f"belongs to ops_submit.\n{body}"
        )
    return f"{head}\n{body}" if body.strip() else f"{head}, no output"


def _unknown_machine(name: str) -> str | None:
    """A message when ``name`` is neither a machine nor a campaign, else None.

    A caller who names one thing and gets told about the other has to work out
    which of two namespaces it missed. Measured while wiring this up: a mistyped
    connection id came back as "No campaign meta under .../conn-typo", which is
    true and answers a question nobody asked.
    """
    name = (name or "").strip()
    if not name:
        return None
    from oncall_flow.connections import describe, get
    from oncall_flow.tools.ops import _ops_home, _slug

    if get(name) is not None or (_ops_home() / _slug(name)).exists():
        return None
    return (
        f"No machine and no campaign called {name!r}.\n{describe()}\n"
        f"ops_campaigns lists the campaigns, if you meant one of those. Nothing was run."
    )


def _where(meta: dict[str, Any]) -> str:
    conn = str(meta.get("connection") or "").strip()
    if conn:
        from oncall_flow.connections import display_name

        return display_name(conn)
    return str(meta.get("host") or "the campaign's machine")


class OpsExecTool(Tool):
    """The machine face as its own tool, until the exec shadow is ruled on."""

    timeout_seconds = 120.0

    @property
    def name(self) -> str:
        return "ops_exec"

    @property
    def description(self) -> str:
        return (
            "Run one command on a machine the owner registered and return what it said. "
            "'machine' is an id from ops_connections, or a campaign name -- its declaration "
            "says which machine. A machine of the owner's is where their code and their cases "
            "live: use this to look at anything there -- list a directory, read or grep a "
            "file, tail a log, check a size or a hash. Looking before you spend compute is "
            "cheap and expected, and has repeatedly been what separated a useful first round "
            "from a wasted one. You never need a host, a port, a user or a key: those belong "
            "to the connection and this tool uses them for you. On these machines it refuses "
            "anything that would outlive the call (nohup, a trailing &, screen): work that "
            "keeps running there belongs to ops_submit, and anything this runs is killed "
            "after 60s -- a command that needs longer is a job, and a job has a different "
            "home. The plain exec tool runs on THIS computer; this one is how a registered "
            "machine is reached."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "The shell command to run there."},
                "machine": {
                    "type": "string",
                    "description": "Where to run it: a machine id from ops_connections, or a "
                    "campaign name (its declaration says which machine).",
                },
                "cwd": {
                    "type": "string",
                    "description": "Working directory on that machine. Defaults to the "
                    "campaign's remote_dir, else its home.",
                },
            },
            "required": ["command", "machine"],
        }

    async def execute(self, command: str, machine: str, cwd: str | None = None, **kwargs: Any) -> str:
        name = (machine or "").strip()
        if not name:
            return (
                "No machine named. ops_connections lists the machines and their ids; "
                "a command for THIS computer belongs to the plain exec tool."
            )
        connection, campaign = resolve_machine(name)
        return await run_on_machine(command, campaign=campaign, connection=connection, cwd=cwd)
