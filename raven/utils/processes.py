"""Whether a recorded process is still ours, beyond the pid being taken.

``raven/utils/pid.py`` answers "is something running under this pid", and its
deliberate conservatism -- a process another account owns counts as alive --
is the right answer to that question. It is the wrong answer to the question
state files actually pose: pids are recycled by the kernel, so "alive" says
nothing about whether the process is the raven one the record names.

The one discriminator already on every such process is its argv: every raven
entrance runs through the interpreter as ``python -m raven ...``, read back
through the OS's own process table with no extra dependency. A process whose
argv cannot be read -- another account's, or one the OS will not describe --
answers "not positively ours": it is never signalled for it, and the caller
that must never be wrong keeps its own stronger evidence (the gateway's
flock).
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
    if sys.platform == "win32":
        return _command_line_windows(pid)
    return _command_line_posix(pid)


def _command_line_posix(pid: int) -> Optional[str]:
    from pathlib import Path

    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return None
    return raw.replace(b"\0", b" ").decode("utf-8", "replace").strip() or None


def _command_line_windows(pid: int) -> Optional[str]:
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


def looks_like_raven(pid: int) -> bool:
    """Whether ``pid`` runs a ``python -m raven`` module invocation.

    Every entrance raven leaves running -- the gateway, standalone serve, the
    web supervisor -- is launched that way, so the record's claim "this pid is
    raven's" is decidable without trusting the pid alone. An unreadable or
    empty command line answers False: nothing is signalled on a guess. The
    match is on the flag, quoted or not: a path separator that happens to
    spell ``/m`` is not a module flag, which is what looking at raw tokens
    would answer.
    """
    import re

    line = command_line(pid)
    if line is None:
        return False
    return bool(re.search(r"(?<![\w/\\.-])-m\s+raven(?![\w.-])", line, re.IGNORECASE))


__all__ = ["command_line", "looks_like_raven"]
