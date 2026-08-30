"""Tests for ``raven plugins`` and ``raven plugin auth``.

``plugins`` is read-only: it renders the activated plugins -- here the
registered entry-points one, raven.plugins.memory.everos -- and resolves
``config.memory.backend`` against the live registry without invoking any
plugin runtime (no ``MemoryBackend.start`` is awaited). These tests pin the
table's contents and the three branches of the backend-selection block:
present-active, present-unknown, and explicitly-disabled.

``plugin auth`` is a chain of refusals in front of one network call: an
unknown server, a disabled one, and one not configured for OAuth are each
rejected before anything connects, and a connect that comes back in any state
other than ``connected`` exits non-zero. Only the last of those needs a
manager, and it gets a fake one -- the real flow opens a browser.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from raven.cli.commands import app
from raven.config.loader import set_config_path

runner = CliRunner()

# Through the root app rather than `plugin_app` directly: a Typer group with a
# single command collapses into that command, so invoking the sub-app with
# ["auth", name] parses "auth" as the server argument.


# ---------------------------------------------------------------------------
# ``raven plugin auth`` -- refusals in front of the one network call
# ---------------------------------------------------------------------------


@pytest.fixture
def config_with(tmp_path: Path):
    """Write a config carrying the given mcp_servers block, and point the
    loader at it. The command reads the loader's own path, not an argument."""

    def _write(servers: dict) -> Path:
        path = tmp_path / "config.json"
        path.write_text(json.dumps({"tools": {"mcpServers": servers}}))
        set_config_path(path)
        return path

    yield _write
    set_config_path(None)  # type: ignore[arg-type]


def test_an_unknown_server_is_refused_before_anything_connects(config_with) -> None:
    config_with({})

    r = runner.invoke(app, ["plugin", "auth", "ghost"])

    assert r.exit_code == 1
    assert "no MCP server named" in r.stdout


def test_a_disabled_server_is_refused(config_with) -> None:
    config_with({"svc": {"url": "https://svc.example/mcp", "auth": "oauth", "enabled": False}})

    r = runner.invoke(app, ["plugin", "auth", "svc"])

    assert r.exit_code == 1
    assert "disabled" in r.stdout


def test_a_server_not_configured_for_oauth_is_refused(config_with) -> None:
    """The message has to name the current mode, or the fix is a guess."""
    config_with({"svc": {"url": "https://svc.example/mcp", "auth": "apikey", "enabled": True}})

    r = runner.invoke(app, ["plugin", "auth", "svc"])

    assert r.exit_code == 1
    assert "apikey" in r.stdout


class _FakeManager:
    """Stands in for MCPConnectionManager: the real `connect` opens a browser."""

    def __init__(self, snapshot: dict):
        self._snapshot = snapshot
        self.closed = False

    async def connect(self, name: str, cfg) -> dict:
        return self._snapshot

    async def aclose(self) -> None:
        self.closed = True


def _patch_manager(monkeypatch: pytest.MonkeyPatch, snapshot: dict) -> list:
    made: list = []
    import raven.mcp.manager as mgr_mod

    def _factory(registry):
        m = _FakeManager(snapshot)
        made.append(m)
        return m

    monkeypatch.setattr(mgr_mod, "MCPConnectionManager", _factory)
    return made


def test_a_connected_snapshot_reports_the_tool_count(config_with, monkeypatch: pytest.MonkeyPatch) -> None:
    config_with({"svc": {"url": "https://svc.example/mcp", "auth": "oauth", "enabled": True}})
    made = _patch_manager(monkeypatch, {"state": "connected", "tool_count": 3, "error": None})

    r = runner.invoke(app, ["plugin", "auth", "svc"])

    assert r.exit_code == 0, r.stdout
    assert "authorized" in r.stdout
    assert "3 tools" in r.stdout
    assert made[0].closed, "the manager must be closed even on the happy path"


def test_a_failed_authorization_exits_non_zero_and_names_the_reason(
    config_with, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`auth_required` back from connect means the flow did not complete; the
    command must not report success just because the call returned."""
    config_with({"svc": {"url": "https://svc.example/mcp", "auth": "oauth", "enabled": True}})
    made = _patch_manager(monkeypatch, {"state": "auth_required", "tool_count": 0, "error": "denied by user"})

    r = runner.invoke(app, ["plugin", "auth", "svc"])

    assert r.exit_code == 1
    assert "authorization failed" in r.stdout
    assert "denied by user" in r.stdout
    assert made[0].closed, "the manager must be closed on the failure path too"


# ---------------------------------------------------------------------------
# ``raven plugins`` -- the listing table and the backend-selection block
# ---------------------------------------------------------------------------


def _make_runner_args(tmp_path: Path, config: dict[str, Any]) -> list[str]:
    """Write a config file + return the typer args to point at it."""
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(config), encoding="utf-8")
    # ``raven plugins -c <path>``
    return ["plugins", "-c", str(config_path)]


@pytest.fixture(autouse=True)
def _wide_console(monkeypatch: pytest.MonkeyPatch) -> None:
    """Give every command in this file a console wide enough to print a
    factory reference.

    ``COLUMNS`` in the invocation's environment is not enough on its own. The
    command renders through ``plugin_commands.console``, a module-level
    ``Console()`` whose width is fixed when the module is first imported, so
    the variable only lands if this file is what imported it -- which held
    until the suite started running in parallel, where another worker's test
    gets there first and the width is Rich's default 80. The factory column
    then truncates to ``...`` and the assertions read as a missing string
    rather than a narrow terminal.

    Reproduce by importing the CLI at 80 columns before the test runs: the
    failure is `assert 'raven.plugins.memory.everos.backend:make_backend' in`
    a string that ends `...___/`.
    """
    from rich.console import Console

    from raven.cli import plugin_commands

    monkeypatch.setattr(plugin_commands, "console", Console(width=200))


def _invoke(args: list[str], tmp_path: Path):
    """Run the CLI with a sandboxed HOME so user-level plugin discovery
    doesn't surface unrelated plugins on the developer's machine.

    ``COLUMNS=200`` is kept for the child's own sake -- anything the command
    renders through a console it builds itself reads it -- but the table this
    file asserts on comes from the module-level console that ``_wide_console``
    replaces.
    """
    fake_home = tmp_path / "home"
    fake_home.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, "HOME": str(fake_home), "COLUMNS": "200"}
    return runner.invoke(app, args, env=env)


# ---------------------------------------------------------------------------
# Default config -- backend selected and available
# ---------------------------------------------------------------------------


class TestActiveBackend:
    def test_lists_everos_memory(self, tmp_path: Path) -> None:
        result = _invoke(
            _make_runner_args(tmp_path, {}),
            tmp_path,
        )
        assert result.exit_code == 0, result.stdout
        assert "everos-memory" in result.stdout
        assert "1.2.0" in result.stdout
        assert "bundled" in result.stdout

    def test_shows_active_backend_with_track_ids(
        self,
        tmp_path: Path,
    ) -> None:
        result = _invoke(
            _make_runner_args(
                tmp_path,
                {
                    "memory": {
                        "backend": "everos",
                        "userId": "alice",
                        "agentId": "robo",
                    },
                },
            ),
            tmp_path,
        )
        assert result.exit_code == 0
        assert "Active memory backend" in result.stdout
        assert "everos" in result.stdout
        assert "from plugin: everos-memory" in result.stdout
        assert "User id:" in result.stdout
        assert "alice" in result.stdout
        assert "Agent id:" in result.stdout
        assert "robo" in result.stdout


# ---------------------------------------------------------------------------
# Backend explicitly disabled
# ---------------------------------------------------------------------------


class TestBackendDisabled:
    def test_null_backend_reports_none(self, tmp_path: Path) -> None:
        result = _invoke(
            _make_runner_args(tmp_path, {"memory": {"backend": None}}),
            tmp_path,
        )
        assert result.exit_code == 0
        assert "Active memory backend" in result.stdout
        assert "none" in result.stdout
        # The legacy-fallback hint surfaces.
        assert "legacy" in result.stdout


# ---------------------------------------------------------------------------
# Backend name set but no contribution matches
# ---------------------------------------------------------------------------


class TestBackendUnavailable:
    def test_unknown_backend_flagged(self, tmp_path: Path) -> None:
        result = _invoke(
            _make_runner_args(
                tmp_path,
                {
                    "memory": {"backend": "nonexistent"},
                },
            ),
            tmp_path,
        )
        assert result.exit_code == 0
        assert "nonexistent" in result.stdout
        assert "not available" in result.stdout


# ---------------------------------------------------------------------------
# Plugin disabled list shows in table
# ---------------------------------------------------------------------------


class TestDisabledList:
    def test_disabled_plugin_status(self, tmp_path: Path) -> None:
        result = _invoke(
            _make_runner_args(
                tmp_path,
                {
                    "plugins": {"disabled": ["everos-memory"]},
                },
            ),
            tmp_path,
        )
        assert result.exit_code == 0
        assert "everos-memory" in result.stdout
        # The status column shows "disabled" for the row.
        assert "disabled" in result.stdout


# ---------------------------------------------------------------------------
# Verbose flag shows factory references
# ---------------------------------------------------------------------------


class TestVerboseFlag:
    def test_verbose_shows_factory(self, tmp_path: Path) -> None:
        args = _make_runner_args(tmp_path, {})
        args.append("--verbose")
        result = _invoke(args, tmp_path)
        assert result.exit_code == 0
        # The factory reference is the canonical ``module:callable`` form.
        assert "raven.plugins.memory.everos.backend:make_backend" in result.stdout
