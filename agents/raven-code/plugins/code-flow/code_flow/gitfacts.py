"""Reading git facts from a working directory, fail-soft.

Every question here is answered from a subprocess with a short timeout, and
every failure -- no git on PATH, not a repository, a hung filesystem --
answers ``None`` rather than raising: the callers report what they know and
say what they could not read.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

GIT_TIMEOUT_S = 5.0


def run_git(cwd: Path | str, *args: str, timeout: float = GIT_TIMEOUT_S) -> subprocess.CompletedProcess | None:
    """``git -C cwd args``, or None when git could not run at all."""
    git = shutil.which("git")
    if git is None:
        return None
    try:
        return subprocess.run(
            [git, "-C", str(cwd), *args],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None


def head_of(cwd: Path | str | None) -> str | None:
    """The commit HEAD names in ``cwd``, or None outside a repository."""
    if cwd is None:
        return None
    result = run_git(cwd, "rev-parse", "HEAD")
    if result is None or result.returncode != 0:
        return None
    sha = result.stdout.strip()
    return sha or None


__all__ = ["GIT_TIMEOUT_S", "head_of", "run_git"]
