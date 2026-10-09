"""Whether a recorded process is still ours, beyond the pid being taken.

``raven/utils/pid.py`` answers "is something running under this pid", and its
deliberate conservatism -- a process another account owns counts as alive --
is the right answer to that question. It is the wrong answer to the question
state files actually pose: pids are recycled by the kernel, so "alive" says
nothing about whether the process is the raven one the record names.

The discriminator is the recorded process's own command line, read back from
the OS's process table. POSIX asks ``ps``; Windows asks CIM for its
``Win32_Process`` row. An unreadable identity stays unknown: the stop keeps
its hands off, and state readers keep the record of a process still alive.
"""

from __future__ import annotations

import subprocess
import sys
from typing import Optional


def command_line(pid: int) -> Optional[str]:
    """The argv of ``pid`` as one string, or None where it cannot be read.

    A dead pid, another account's process, and an OS with nothing to ask all
    land on the same None: the caller only ever acts on a positive read.
    """
    if pid <= 0:
        return None
    if sys.platform == "win32":  # pragma: no cover - linux is the covered CI host
        return _command_line_windows(pid)
    return _command_line_posix(pid)


def _command_line_posix(pid: int) -> Optional[str]:
    """The process's argv through ``ps``, which POSIX guarantees everywhere.

    ``/proc/<pid>/cmdline`` answers the same question on Linux but does not
    exist on macOS, which is a documented platform; ``ps -p -o command=`` is
    the one spelling both accept. ``-ww`` is not decoration: without it both
    GNU and BSD ``ps`` size ``command`` output from the inherited display
    width, and a long install path is exactly where the ``-m raven`` token
    this reader exists to find gets cut off.
    """
    try:
        out = subprocess.run(  # noqa: S603 - pid is an int
            ["ps", "-ww", "-p", str(pid), "-o", "command="],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if out.returncode != 0:
        return None
    return out.stdout.strip() or None


def _command_line_windows(pid: int) -> Optional[str]:  # pragma: no cover - no Windows CI
    # CIM rather than the legacy WMI class: Win32_Process's getter opens each
    # process it lists, which is seconds of failures on a full table, while
    # the CIM instance read answers from the snapshot. The class itself never
    # carries this account's own name, so it is a stable spelling to ask for.
    import shutil

    powershell = shutil.which("powershell.exe") or shutil.which("powershell")
    if powershell is None:
        return None
    try:
        out = subprocess.run(  # noqa: S603 - argv is literals plus an int
            [
                powershell,
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                "$ProgressPreference='SilentlyContinue'; "
                "$InformationPreference='SilentlyContinue'; "
                "$WarningPreference='SilentlyContinue'; "
                "$p = Get-CimInstance Win32_Process -Filter 'ProcessId = %d'; "
                "if ($null -ne $p) { [Console]::Out.Write($p.CommandLine) }" % pid,
            ],
            capture_output=True,
            text=True,
            timeout=15,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if out.returncode != 0:
        return None
    return out.stdout.strip() or None


_RESIDENT_SUBCOMMANDS = frozenset({"serve", "web", "gateway"})
"""The subcommands a state file may have come from, and only those.

``raven web`` leaves a supervisor; ``raven serve`` and ``raven gateway`` are
the engines it watches. The other verbs are run-and-done -- they may get a
recycled pid attached to their name, but they never write serve.json or
web.json, so the stop paths under this module cannot confuse them with the
records they do not produce. Narrow on purpose: every alias admitted here is
one more spelling a stranger could share."""


def looks_like_raven(pid: int) -> bool:
    """Whether ``pid`` runs a raven entrance that a state file could name.

    Two spellings the installs this code runs under:

    - ``python -m raven ...`` -- what the supervisor spawns for its gateway
      (``_spawn_supervisor``, ``_gateway_argv``), and how ``uv run raven``
      leaves the process looking too;
    - ``raven <serve|web|gateway> ...`` -- the console-script entry point,
      with or without its ``.exe`` suffix on Windows.

    An unreadable or empty command line answers False: nothing is signalled
    on a guess. The match is on the flag form, quoted or not -- a path that
    happens to spell ``/m raven/...`` is not a module invocation.
    """
    return raven_identity(pid) is True


def raven_identity(pid: int) -> Optional[bool]:
    """True for a resident, False for a foreign command, None for an unreadable one.

    Unknown does not license deleting a live process's record or starting a
    second resident, and only a positive identity licenses signalling it.
    """
    import re

    line = command_line(pid)
    if not line:
        return None
    if re.search(r"(?<![\w/\\.-])-m\s+raven(?![\w.-])", line, re.IGNORECASE):
        return True
    return _is_console_serve(line)


def _is_console_serve(line: str) -> bool:
    """Whether the argv spells ``raven <a resident subcommand>``.

    The console script is a tiny shim that forwards to ``raven.cli.commands:run``;
    its argv[0] is the script name and argv[1] is the subcommand. Anything else
    (``raven --version``, ``raven tui``) is fine to reject: it is not a process
    this module's state files can point at.
    """
    import re

    match = re.match(r'.*?(?:^|[/\\])raven(?:\.exe)?"?\s+(\w+)', line, re.IGNORECASE)
    return match is not None and match.group(1).lower() in _RESIDENT_SUBCOMMANDS


__all__ = ["command_line", "looks_like_raven", "raven_identity"]
