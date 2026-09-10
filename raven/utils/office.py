"""Convert a document with LibreOffice headless, and leave nothing running.

The viewer renders a deck to show it and the ppt engine
renders one to measure it, and both need the same three things: an argv that
makes LibreOffice convert without asking anything, a profile of its own for that
run, and a teardown that reaches the process doing the work. Those were written
twice, identically, and had diverged within a day of the second copy landing --
one of them knew about Windows and the other did not. What is not here is what
differs: each caller keeps its own timeout, its own idea of what counts as
failure, and its own place to put the file.

Two callers, not every caller. The design engine converts Office documents too,
through a backend of its own with its own error codes, worker directories and
configured executable; it predates this module and has not been brought through
it. Saying so is the point: the copy this module ends came back once already
because a docstring claimed something the tree did not do.

**A shared user profile silently loses a conversion.** LibreOffice allows one
instance per profile; a second invocation against the same profile hands its
request to the running instance and exits 0, having written nothing. Two decks
rendering at once is ordinary here, so every call gets a profile of its own.

**Killing the launcher is not killing the conversion.** ``soffice`` is a shell
wrapper that execs ``oosplash``, which forks ``soffice.bin``; a timeout that kills
the wrapper leaves the real process holding the profile directory open. On POSIX
the run gets a session of its own and the teardown kills the group. Native
Windows has neither ``os.killpg`` nor ``SIGKILL`` and ignores ``start_new_session``,
so the run asks for a process group there instead, the teardown signals it with
CTRL_BREAK and then finishes the tree with ``taskkill /T``. ``Popen.kill`` is the
last resort on both, because a process outside any group we can name is still one
we started.

**Exit 0 is not proof of output, and stderr is not proof of failure.** LibreOffice
reports success for documents it could not load, and a stock container prints
``failed to launch javaldx`` on every run. So this returns what appeared and what
was said, and the caller decides what that means.
"""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import sys
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

KILL_TIMEOUT_S = 10.0
"""Long enough for a tree kill to walk the tree, short enough that a wedged one
does not hold the caller past the budget it has already given up on."""


def find_soffice() -> str | None:
    """LibreOffice's launcher, under either of the two names it ships as."""
    return shutil.which("soffice") or shutil.which("libreoffice")


def convert_command(source: Path, staged: Path, profile: Path, *, executable: str) -> list[str]:
    """The argv that converts `source` to PDF into `staged`, using `profile` alone."""
    return [
        executable,
        "--headless",
        "--nologo",
        "--nodefault",
        "--nolockcheck",
        "--nofirststartwizard",
        # A profile with nothing in it cannot offer to recover a document from a
        # previous crash, which is the other way headless startup blocks forever.
        "--norestore",
        f"-env:UserInstallation={profile.resolve().as_uri()}",
        "--convert-to",
        "pdf",
        "--outdir",
        str(staged),
        str(Path(source).resolve()),
    ]


@dataclass(frozen=True)
class Converted:
    """What one run left behind: the files that appeared, and what it said."""

    produced: list[Path]
    returncode: int
    stdout: str
    stderr: str


def to_pdf(
    source: Path,
    staged: Path,
    *,
    executable: str,
    timeout_s: float,
    profile_root: Path | None = None,
) -> Converted:
    """Run one conversion of `source` into `staged`, and say what came of it.

    `staged` is the caller's, because where the output lands decides how it is
    moved afterwards -- a rename is not a rename across filesystems. The profile
    is this function's and is thrown away with the run; `profile_root` puts it
    somewhere other than the system temp for a caller whose temp is a tmpfs it
    would rather not fill.

    Raises plainly, and each caller maps it: `FileNotFoundError` where the
    executable will not start, `OSError` where the spawn fails otherwise, and
    `TimeoutError` where the run outlives `timeout_s` -- after the tree is down.
    """
    with tempfile.TemporaryDirectory(prefix="raven-soffice-", dir=profile_root) as scratch:
        profile = Path(scratch) / "profile"
        profile.mkdir()
        command = convert_command(Path(source), Path(staged), profile, executable=executable)
        returncode, stdout, stderr = _run(command, timeout_s=timeout_s)
    return Converted(produced=sorted(Path(staged).glob("*.pdf")), returncode=returncode, stdout=stdout, stderr=stderr)


def _run(command: Sequence[str], *, timeout_s: float) -> tuple[int, str, str]:
    windows = sys.platform == "win32"
    process = subprocess.Popen(  # noqa: S603 - fixed argv, never a shell string
        list(command),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        # Its own session where there are sessions, and the process group the
        # teardown can signal where there are not.
        start_new_session=not windows,
        creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if windows else 0,
    )
    try:
        stdout, stderr = process.communicate(timeout=timeout_s)
    except subprocess.TimeoutExpired as exc:
        terminate(process)
        # Reaped here so the directory the run used can be removed after it.
        process.communicate()
        raise TimeoutError(f"the conversion exceeded {timeout_s:g}s and was stopped") from exc
    return process.returncode, stdout or "", stderr or ""


def terminate(process: subprocess.Popen) -> None:
    """Stop a converter and the children it forked, on either platform.

    Exported because every converter this repo spawns has the same problem: the
    thing that must die is not the thing that was started.
    """
    if sys.platform == "win32":
        # Two attempts, not one: CTRL_BREAK needs a console to be delivered into
        # and a service without one raises here, while the tree kill is the half
        # that actually reaches the children.
        try:
            process.send_signal(signal.CTRL_BREAK_EVENT)
        except (OSError, ValueError):
            pass
        try:
            taskkill = shutil.which("taskkill") or "taskkill"
            subprocess.run(  # noqa: S603, S607 - resolved above, argv is literals plus this child's pid
                [taskkill, "/F", "/T", "/PID", str(process.pid)],
                capture_output=True,
                timeout=KILL_TIMEOUT_S,
                check=False,
            )
        except (OSError, ValueError, subprocess.SubprocessError):
            pass
        if process.poll() is None:
            process.kill()
        return
    try:
        os.killpg(os.getpgid(process.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        process.kill()
