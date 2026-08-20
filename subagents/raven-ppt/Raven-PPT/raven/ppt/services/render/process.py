"""Running a converter without leaving anything behind.

Every subprocess in this package goes through here, for one reason:
`subprocess.run(timeout=...)` kills the process it started, and `/usr/bin/soffice`
is not the process that does the work. It is a `/bin/sh` wrapper that execs
`oosplash`, which forks `soffice.bin`; killing the wrapper on timeout leaves
`soffice.bin` running, holding the throwaway profile directory open, and the
cleanup that follows races it. That is not hypothetical -- this machine was found
carrying an `oosplash` with PPID 1 from an abandoned test run days earlier, still
pointing at a profile path that no longer existed.

So: each call gets its own session, and a timeout kills the whole group.
"""

from __future__ import annotations

import os
import signal
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass

from raven.ppt.services.render.errors import LOG_TAIL_CHARS, RenderError, RenderTimeoutError, RenderUnavailableError


@dataclass(frozen=True)
class Completed:
    """What a converter left on its way out."""

    returncode: int
    stdout: str
    stderr: str

    @property
    def log_tails(self) -> dict[str, object]:
        """The bits worth quoting when the run produced no usable output."""
        return {
            "exit_code": self.returncode,
            "stdout": self.stdout.strip()[-LOG_TAIL_CHARS:],
            "stderr": self.stderr.strip()[-LOG_TAIL_CHARS:],
        }


def run(command: Sequence[str], *, timeout_s: float, what: str) -> Completed:
    """Run `command` to completion, killing its whole process group on timeout.

    Returns rather than raises on a non-zero exit: LibreOffice reports success
    for conversions that produced nothing and prints its real complaints on a
    stream it also uses for `failed to launch javaldx`, so the caller decides
    what counts as failure by looking for the file it asked for.
    """
    try:
        # Fixed argv, never a shell string.
        process = subprocess.Popen(
            list(command),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
    except FileNotFoundError as exc:
        raise RenderUnavailableError(f"{what} is not installed: {command[0]!r} could not be executed") from exc
    except OSError as exc:
        raise RenderError(f"{what} could not be started: {exc}") from exc
    try:
        stdout, stderr = process.communicate(timeout=timeout_s)
    except subprocess.TimeoutExpired as exc:
        _kill_group(process)
        # Reap the group so the temporary directory this ran in can be removed.
        stdout, stderr = process.communicate()
        raise RenderTimeoutError(
            f"{what} exceeded its {timeout_s:g}s budget and was killed",
            detail={"stdout": (stdout or "")[-LOG_TAIL_CHARS:], "stderr": (stderr or "")[-LOG_TAIL_CHARS:]},
        ) from exc
    return Completed(returncode=process.returncode, stdout=stdout or "", stderr=stderr or "")


def _kill_group(process: subprocess.Popen[str]) -> None:
    """SIGKILL the session `run` started, ignoring a group that already left."""
    try:
        os.killpg(os.getpgid(process.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        process.kill()
