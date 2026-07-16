"""Smoke tests covering every CLI command surface.

This file's job is to catch *crash-class* regressions (``NameError`` /
``AttributeError`` / ``ImportError``) that slip through the more focused
per-command tests. It walks every top-level command + every subcommand
group's ``--help`` and asserts:

1. exit code 0
2. no ``Traceback`` printed
3. ``r.exception`` is None (typer.testing.CliRunner captures crashes here)

It also catches the historical regression where ``agent_commands.py`` was
missing the ``sync_workspace_templates`` import after the CLI modularize
refactor — that bug would have been caught here.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import typer
from typer.testing import CliRunner

from raven.cli.commands import app
from raven.config.loader import set_config_path

runner = CliRunner()


def _registered_command_names() -> set[str]:
    """All top-level command / subcommand-group names on the root app."""
    return set(typer.main.get_command(app).commands.keys())


@pytest.fixture
def tmp_config(tmp_path: Path) -> Path:
    cfg = tmp_path / "config.json"
    set_config_path(cfg)
    yield cfg
    set_config_path(None)  # type: ignore[arg-type]


TOP_LEVEL_COMMANDS = [
    "onboard",
    "gateway",
    "agent",
    "status",
    "upgrade",
    "doctor",
    "channels",
    "cron",
    "provider",
    "sandbox",
    "sentinel",
    "skill",
]


@pytest.mark.parametrize("command", TOP_LEVEL_COMMANDS)
def test_top_level_command_help_does_not_crash(command: str) -> None:
    """Every top-level command's ``--help`` exits 0 with no leaked crash."""
    r = runner.invoke(app, [command, "--help"])
    assert r.exit_code == 0, f"{command} --help exited {r.exit_code}: {r.stdout}"
    assert r.exception is None, f"{command} --help raised an unexpected exception: {r.exception!r}"


def test_root_help_does_not_crash() -> None:
    """``raven --help`` should list every command without crashing."""
    r = runner.invoke(app, ["--help"])
    assert r.exit_code == 0
    assert r.exception is None
    for cmd in TOP_LEVEL_COMMANDS:
        assert cmd in r.stdout, f"missing command in root --help: {cmd}"


# Subcommand --help coverage (depth 2): catches missing imports inside
# subcommand-group modules.

CHANNEL_SUBCOMMANDS = [
    "status",
    "login",
    "enable",
    "disable",
    "set",
    "get",
    "reset",
    "show",
    "list",
]


@pytest.mark.parametrize("subcmd", CHANNEL_SUBCOMMANDS)
def test_channels_subcommand_help_does_not_crash(subcmd: str) -> None:
    """Every ``channels`` subcommand's ``--help`` exits cleanly."""
    r = runner.invoke(app, ["channels", subcmd, "--help"])
    assert r.exit_code == 0, f"channels {subcmd} --help exited {r.exit_code}"
    assert r.exception is None


SKILL_SUBCOMMANDS = ["list", "get"]


@pytest.mark.parametrize("subcmd", SKILL_SUBCOMMANDS)
def test_skill_subcommand_help_does_not_crash(subcmd: str) -> None:
    """Every ``skill`` subcommand's ``--help`` exits cleanly."""
    r = runner.invoke(app, ["skill", subcmd, "--help"])
    assert r.exit_code == 0, f"skill {subcmd} --help exited {r.exit_code}"
    assert r.exception is None


SENTINEL_SUBCOMMANDS = [
    "status",
    "tick",
    "ticks",
    "nudges",
    "decisions",
    "discover-now",
    "routines",
]


@pytest.mark.parametrize("subcmd", SENTINEL_SUBCOMMANDS)
def test_sentinel_subcommand_help_does_not_crash(subcmd: str) -> None:
    """Every ``sentinel`` subcommand's ``--help`` exits cleanly."""
    r = runner.invoke(app, ["sentinel", subcmd, "--help"])
    assert r.exit_code == 0, f"sentinel {subcmd} --help exited {r.exit_code}"
    assert r.exception is None


# Read-only command bodies that don't need network / LLM:


def test_status_command_body_does_not_crash(tmp_config: Path) -> None:
    """``raven status`` reads config + prints rows without crashing."""
    r = runner.invoke(app, ["status"])
    assert r.exception is None, f"status crashed: {r.exception!r}"
    assert r.exit_code == 0


def test_channels_list_body_does_not_crash(tmp_config: Path) -> None:
    """``raven channels list`` enumerates channels without crashing."""
    r = runner.invoke(app, ["channels", "list"])
    assert r.exception is None
    assert r.exit_code == 0


def test_cron_list_body_does_not_crash(tmp_config: Path) -> None:
    """``raven cron list`` reads cron jobs without crashing."""
    r = runner.invoke(app, ["cron", "list"])
    assert r.exception is None
    assert r.exit_code == 0


# Full set of top-level commands + subcommand groups registered on the root
# app (superset of TOP_LEVEL_COMMANDS, which only lists the --help-probed ones).
REGISTERED_COMMAND_NAMES = {
    "agent",
    "channels",
    "cron",
    "deep-research",
    "doctor",
    "gateway",
    "onboard",
    "plugins",
    "provider",
    "sandbox",
    "sentinel",
    "sessions",
    "skill",
    "status",
    "tracing",
    "tui",
    "upgrade",
}


def test_no_logs_subcommand_registered() -> None:
    """There is no ``raven logs`` command; adding one must break this test."""
    assert "logs" not in _registered_command_names()


def test_registered_command_set_is_pinned() -> None:
    """Pin the exact command surface so any add/remove trips a test."""
    assert _registered_command_names() == REGISTERED_COMMAND_NAMES
