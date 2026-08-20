"""Tests for ``raven plugin auth``.

The command is a chain of refusals in front of one network call: an unknown
server, a disabled one, and one not configured for OAuth are each rejected
before anything connects, and a connect that comes back in any state other
than ``connected`` exits non-zero. Only the last of those needs a manager, and
it gets a fake one -- the real flow opens a browser.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from raven.cli.commands import app
from raven.config.loader import set_config_path

runner = CliRunner()

# Through the root app rather than `plugin_app` directly: a Typer group with a
# single command collapses into that command, so invoking the sub-app with
# ["auth", name] parses "auth" as the server argument.


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
    import raven.agent.tools.mcp_manager as mgr_mod

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
