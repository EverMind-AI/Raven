"""The macOS app's launcher, against a fake `raven` CLI.

The launcher used to probe the gateway itself and run `raven web --stop` when
the probe came back empty -- which is also what a gateway that is still booting
looks like. It now hands the whole lifecycle to `raven web --print-url`, so the
contract worth testing is small: pass the mode through, print what the CLI
printed, and never signal a gateway from here.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

LAUNCHER = Path(__file__).resolve().parents[1] / "desktop" / "macos" / "scripts" / "launch.sh"

pytestmark = pytest.mark.skipif(
    os.name == "nt" or not Path("/bin/sh").exists(),
    reason="the launcher is a POSIX shell script",
)


def _fake_raven(home: Path, *, stdout: str = "", stderr: str = "", code: int = 0) -> None:
    """A `raven` on the launcher's first search path, recording its arguments."""
    bin_dir = home / ".local" / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    cli = bin_dir / "raven"
    cli.write_text(
        "#!/bin/sh\n"
        'printf \'%s\\n\' "$@" > "$HOME/raven-args"\n'
        f"printf '%s' '{stdout}'\n"
        f"printf '%s' '{stderr}' >&2\n"
        f"exit {code}\n",
        encoding="utf-8",
    )
    cli.chmod(0o755)


def _run(tmp_path: Path) -> subprocess.CompletedProcess[str]:
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    env = {
        "HOME": str(home),
        "PATH": "/usr/bin:/bin",
    }
    return subprocess.run(
        ["/bin/sh", str(LAUNCHER)],
        capture_output=True,
        text=True,
        env=env,
        timeout=10,
        check=False,
    )


def test_it_prints_the_url_the_cli_minted(tmp_path: Path) -> None:
    url = "http://127.0.0.1:18792/auth#nonce"
    _fake_raven(tmp_path / "home", stdout=url)

    result = _run(tmp_path)

    assert result.returncode == 0
    assert result.stdout == url, "the window is handed exactly what the CLI minted"


def test_it_asks_for_the_url_and_nothing_else(tmp_path: Path) -> None:
    _fake_raven(tmp_path / "home", stdout="http://127.0.0.1:18792/auth#nonce")

    result = _run(tmp_path)

    assert result.returncode == 0
    args = (tmp_path / "home" / "raven-args").read_text(encoding="utf-8").splitlines()
    assert args == ["web", "--print-url"], "the launcher stopped driving the gateway itself"
    assert "--stop" not in args


def test_a_failed_launch_keeps_stdout_empty_and_the_reason_on_stderr(tmp_path: Path) -> None:
    _fake_raven(tmp_path / "home", stderr="No page is built.", code=1)

    result = _run(tmp_path)

    assert result.returncode == 1
    assert result.stdout == ""
    assert "No page is built." in result.stderr


def test_a_missing_cli_is_reported_before_running_anything(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()

    result = _run(tmp_path)

    assert result.returncode == 1
    assert result.stdout == ""
    assert "raven CLI not found" in result.stderr
    assert not (home / "raven-args").exists()
