"""The suite's per-test home, which no test may see past to the home of whoever runs it."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from raven.home import raven_home


def _own_home(tmp_path: Path) -> Path:
    """Where ``_no_real_raven_home`` puts this test's home."""
    return tmp_path.with_name(f"{tmp_path.name}-home")


def test_a_test_resolves_its_own_home(tmp_path: Path) -> None:
    """``Path.home()`` reads ``HOME`` on POSIX and ``USERPROFILE`` on Windows.

    With ``HOME`` alone, a run on Windows read and rewrote the real profile:
    its ``.raven`` and its ``.ssh/config``.
    """
    home = _own_home(tmp_path)

    assert Path.home() == home
    assert Path("~/.ssh/config").expanduser() == home / ".ssh" / "config"
    assert raven_home() == home / ".raven"


def test_a_child_process_resolves_the_same_home(tmp_path: Path) -> None:
    """An agent or a ``raven`` child inherits the environment, never a monkeypatch."""
    said = subprocess.run(
        [sys.executable, "-c", "from pathlib import Path; print(Path.home())"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()

    assert Path(said) == _own_home(tmp_path)
