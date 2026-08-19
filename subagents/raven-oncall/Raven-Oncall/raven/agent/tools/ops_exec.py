"""Run a command on the machine a campaign uses.

The loop's exec runs here, on the operator's laptop. The work runs on a host it
reaches only through this tool family, and the credentials for that host belong
to the connection rather than to the loop. So every question of the form "what is
actually in that directory / what did the solver print / how big is that output"
had to be answered by whichever narrow tool we had thought to build, and when
none of them fitted, the loop improvised with the exec it did have -- which
reaches the operator's own filesystem and nothing else.

Measured 2026-08-17, in one afternoon:

  * a case directory could be read a file at a time but not listed, so one arm
    guessed seven filenames, missed all seven, and went reading raven's own state
    files instead -- where it found a session from a discarded round and rebuilt
    its idea of the campaign from it;
  * a 2 MB ``job.dat`` held the contact pressures a task was asking for, and the
    arm reported the metric as unobtainable because no tool served that file;
  * three separate turns spent themselves on ``ls`` and ``ssh`` against the local
    machine, because the remote path in the task statement looks like a path.

None of that is a shell being dangerous. It is a shell pointed at the wrong
machine. Pointed at the right one, ``ls``, ``cat``, ``tail``, ``grep``,
``sha256sum`` and ``diff`` answer all of the above, and answer questions nobody
thought to build a tool for.

**This is for looking, not for starting work.** A job has to go through
``ops_submit`` -- that is what makes its compute budget enforceable, its state
survive a restart and its results wake the loop when they are due. Two things
hold that line here: a hard timeout, since a solver round takes tens of minutes
and a command that cannot outlive its call cannot become an experiment; and a
refusal of the shapes that detach. Neither stops a determined author -- a
self-triggering script would slip through both -- and that is the point of the
apparatus fingerprint, which reports what changed after the fact. What these two
stop is the casual version, which is the one that actually happened.

Writes are not filtered. A shell can edit a case in place, and
``ops_edit_case_dict`` exists so that such a change is recorded where a grader
can see it. But the apparatus fingerprint already compares the case file by file
and reports drift, so an unrecorded edit is visible afterwards -- while a string
filter over commands would only look like it had prevented one.
"""

from __future__ import annotations

import re
import shlex
from typing import Any

from raven.agent.tools.base import Tool

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


def _runner_for_connection(conn_id: str):
    """A command runner for a machine named directly, plus a meta-shaped dict.

    Returns ``(None, message)`` when the id is unknown, so the caller can hand
    the reason back rather than a traceback.
    """
    from raven.ops.connections import get

    row = get(conn_id)
    if row is None:
        from raven.ops.connections import describe

        return None, (f"No connection with id {conn_id!r}.\n" + describe())
    import os

    from raven.ops import make_ssh_runner

    runner = make_ssh_runner(
        str(row.get("host") or ""), int(row.get("port") or 22),
        os.path.expanduser(str(row.get("key") or "~/.ssh/id_rsa")),
        user=str(row.get("user") or "root"),
    )
    return runner, {"connection": conn_id, "remote_dir": ""}


class OpsExecTool(Tool):
    """Look at a campaign's machine with a shell, under a hard timeout."""

    timeout_seconds = float(_TIMEOUT_S + 30)

    @property
    def name(self) -> str:
        return "ops_exec"

    @property
    def description(self) -> str:
        return (
            "Run a shell command ON THE MACHINE a campaign runs on, and return what it "
            "printed. Use this to look at anything remote: list a directory, read or "
            "grep a file, tail a log, check a size or a hash, diff two files. The plain "
            "exec tool runs on this computer instead, which is why a remote path looks "
            "missing there. "
            "cwd defaults to the campaign's working directory, the one holding jobs/; "
            "pass cwd to look elsewhere, such as the staged case. A non-zero exit is "
            "returned as it happened and is not an error of this tool -- grep says 1 when "
            "it matches nothing. "
            f"FOR LOOKING, NOT FOR STARTING WORK: commands are killed after {_TIMEOUT_S}s "
            "and anything that would detach is refused. A job belongs to ops_submit, which "
            "is what makes its compute budget enforceable, its state survive a restart, and "
            "its results wake you when they are due."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "command": {"type": "string",
                            "description": "Shell command to run on the campaign's machine."},
                "campaign": {"type": "string",
                             "description": "Campaign name. Omit when this window is watching one."},
                "connection": {
                    "type": "string",
                    "description": "Machine id from ops_connections. Use this before any campaign "
                                   "exists -- looking at a machine is what tells you what a "
                                   "campaign should run.",
                },
                "cwd": {"type": "string",
                        "description": "Absolute directory to run in. Defaults to the "
                                       "campaign's working directory."},
                "ledger": {"type": "string", "description": "Ledger path (locates the campaign)."},
            },
            "required": ["command"],
        }

    async def execute(self, command: str, campaign: str = "", connection: str = "",
                      cwd: str | None = None, ledger: str | None = None, **kwargs: Any) -> str:
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
            runner, meta = _runner_for_connection(connection)
            if runner is None:
                return meta  # the message explaining why not
        else:
            from raven.agent.tools.ops_case_dict import _campaign

            try:
                resolved = _campaign(campaign, ledger)
            except ValueError as exc:
                return (
                    f"{exc}\n"
                    "If you are looking at a machine before any campaign exists, pass "
                    "'connection' instead -- ops_connections lists the machines and their ids."
                )
            if isinstance(resolved, str):
                return resolved
            backend, _led, _cdir, meta = resolved
            runner = getattr(backend, "_run", None)
            if runner is None:
                return "This campaign's backend cannot run commands on a host."

        where = (cwd or str(meta.get("remote_dir") or "") or ".").rstrip("/") or "/"
        rc, out = runner(
            f"cd {shlex.quote(where)} 2>/dev/null || exit 66; "
            # bash, not sh: /bin/sh is dash on these hosts, and shell gets written
            # the way bash is written -- `[[ ]]`, brace expansion, arrays. Verified
            # present (5.1.16) before relying on it.
            f"timeout {_TIMEOUT_S} bash -c {shlex.quote(command)} 2>&1"
        )
        body = out if len(out) <= _MAX_BYTES else (
            out[:_MAX_BYTES] + f"\n...[truncated, {len(out)} bytes total]"
        )
        head = f"on {_where(meta)} in {where} (exit {rc})"
        if rc == 66:
            return f"{head}: no such directory. Nothing was run."
        if rc == _TIMED_OUT_RC:
            return (f"{head}: killed at the {_TIMEOUT_S}s limit -- the host answered, the "
                    f"command did not finish. Anything that takes longer is a job, and a job "
                    f"belongs to ops_submit.\n{body}")
        return f"{head}\n{body}" if body.strip() else f"{head}, no output"


def _where(meta: dict[str, Any]) -> str:
    conn = str(meta.get("connection") or "").strip()
    if conn:
        from raven.ops.connections import display_name

        return display_name(conn)
    return str(meta.get("host") or "the campaign's machine")
