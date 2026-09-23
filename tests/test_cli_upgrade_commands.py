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
