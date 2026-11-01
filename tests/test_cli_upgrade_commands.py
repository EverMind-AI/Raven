"""Source-install upgrade guidance and checkout status at the CLI boundary."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import Mock

import pytest
import typer
from typer.testing import CliRunner

from raven.cli import upgrade_commands
from raven.updates import upgrade


@pytest.fixture
def source_install(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> tuple[Path, Mock]:
    checkout = tmp_path / "source checkout"
    distribution = Mock()
    distribution.read_text.return_value = json.dumps({"url": checkout.as_uri(), "dir_info": {"editable": True}})
    monkeypatch.setattr(upgrade.metadata, "distribution", lambda name: distribution)
    monkeypatch.setattr(upgrade.shutil, "which", lambda name: "/usr/bin/git")
    # The rendered guidance is platform-dependent; pin the POSIX form here and
    # let the Windows test below read the other side of the same seam.
    monkeypatch.setattr(upgrade, "_on_windows", lambda: False)
    release = Mock(side_effect=AssertionError("Source installations must not query releases"))
    monkeypatch.setattr(upgrade, "fetch_latest_for_channel", release)
    monkeypatch.setattr(upgrade, "_handoff_upgrade", Mock(side_effect=AssertionError("Unexpected install")))
    return checkout, release


def invoke(*args: str):
    app = typer.Typer()
    upgrade_commands.register(app)
    return CliRunner().invoke(app, list(args))


@pytest.mark.parametrize(("ahead", "behind"), [(0, 0), (0, 5), (3, 0), (2, 4)])
def test_editable_check_reports_git_status_before_release_lookup(source_install, monkeypatch, ahead, behind):
    checkout, release = source_install
    run = Mock(return_value=subprocess.CompletedProcess([], 0, f"{behind}\t{ahead}\n", ""))
    monkeypatch.setattr(upgrade.subprocess, "run", run)

    result = invoke("--check")

    assert result.exit_code == 0, result.output
    output = " ".join(result.output.split())
    assert f"{ahead} commits ahead, {behind} commits behind" in output
    assert "last fetched" in output
    assert "git pull && ./install.sh" in output
    assert "official installer" not in output
    release.assert_not_called()
    run.assert_called_once_with(
        ["/usr/bin/git", "-C", str(checkout), "rev-list", "--left-right", "--count", "origin/main...HEAD"],
        capture_output=True,
        text=True,
        timeout=10,
        check=True,
    )


@pytest.mark.parametrize("check", [True, False])
@pytest.mark.parametrize(
    "error", [FileNotFoundError(), subprocess.CalledProcessError(128, "git"), subprocess.TimeoutExpired("git", 10)]
)
def test_git_failure_keeps_source_update_guidance(source_install, monkeypatch, check, error):
    monkeypatch.setattr(upgrade.subprocess, "run", Mock(side_effect=error))

    result = invoke(*(["--check"] if check else []))

    assert result.exit_code == 1
    output = " ".join(result.output.split())
    assert "git fetch origin main" in output
    assert "git pull && ./install.sh" in output
    assert "official installer" not in output


def test_editable_upgrade_requires_manual_install(source_install, monkeypatch):
    monkeypatch.setattr(upgrade.subprocess, "run", Mock(return_value=subprocess.CompletedProcess([], 0, "2 0", "")))

    result = invoke()

    assert result.exit_code == 1
    assert "git pull && ./install.sh" in " ".join(result.output.split())


def test_editable_update_guidance_is_runnable_on_windows(source_install, monkeypatch):
    """Windows PowerShell 5.1 has no `&&` statement separator, and cmd.exe does
    not run a .ps1 by name, so the Windows guidance has to be separate steps, the
    last naming the PowerShell that runs the installer. The one-line
    `git pull && .\\install.ps1` this used to print fails in both shells."""
    monkeypatch.setattr(upgrade, "_on_windows", lambda: True)
    monkeypatch.setattr(
        upgrade.subprocess,
        "run",
        Mock(return_value=subprocess.CompletedProcess([], 0, "0\t0\n", "")),
    )

    result = invoke("--check")

    assert result.exit_code == 0, result.output
    lines = [line.strip() for line in result.output.splitlines()]
    assert "To update, run these commands in the source checkout:" in lines
    assert "If `git pull` fails, fix it before installing." in lines
    assert "raven web --stop" in lines
    assert "git pull" in lines
    installer = 'powershell -NoProfile -ExecutionPolicy Bypass -File ".\\install.ps1"'
    assert installer in lines
    assert lines.index("raven web --stop") < lines.index("git pull") < lines.index(installer)
    assert "&&" not in result.output


def test_editable_update_guidance_keeps_the_windows_installer_on_one_line(source_install, monkeypatch):
    """A 67-character command on a 60-column console must stay one copyable
    line: rich wraps at the console width unless soft_wrap is set, and a wrap
    after `-File` turns the pasted command into a call with no script."""
    from rich.console import Console

    monkeypatch.setattr(upgrade, "_on_windows", lambda: True)
    monkeypatch.setattr(
        upgrade.subprocess,
        "run",
        Mock(return_value=subprocess.CompletedProcess([], 0, "0\t0\n", "")),
    )
    monkeypatch.setattr(upgrade_commands, "console", Console(width=60))

    result = invoke("--check")

    assert result.exit_code == 0, result.output
    installer = 'powershell -NoProfile -ExecutionPolicy Bypass -File ".\\install.ps1"'
    assert installer in result.output.splitlines()


@pytest.mark.parametrize("url", ["https://example.com/source", "file://remote/source", "file:relative"])
def test_invalid_checkout_url_does_not_run_git(source_install, monkeypatch, url):
    distribution = Mock()
    distribution.read_text.return_value = json.dumps({"url": url, "dir_info": {"editable": True}})
    monkeypatch.setattr(upgrade.metadata, "distribution", lambda name: distribution)
    run = Mock()
    monkeypatch.setattr(upgrade.subprocess, "run", run)

    result = invoke("--check")

    assert result.exit_code == 1
    assert "git pull && ./install.sh" in " ".join(result.output.split())
    run.assert_not_called()
