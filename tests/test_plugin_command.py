"""Phase C — ``raven plugins`` CLI command.

Smoke-tests the command renders the registered entry-points plugin
(raven.plugin.memory.everos) without invoking any plugin runtime (no
``MemoryBackend.start`` is awaited). Verifies the three branches in
the backend-selection block: present-active / present-unknown /
explicitly-disabled.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

pytest.importorskip("raven.plugin.memory.everos")


def _make_runner_args(tmp_path: Path, config: dict[str, Any]) -> list[str]:
    """Write a config file + return the typer args to point at it."""
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(config), encoding="utf-8")
    # ``raven plugins -c <path>``
    return ["plugins", "-c", str(config_path)]


def _invoke(args: list[str], tmp_path: Path):
    """Run the CLI with a sandboxed HOME so user-level plugin discovery
    doesn't surface unrelated plugins on the developer's machine.

    ``COLUMNS=200`` overrides Rich's default terminal width so the
    table columns don't truncate the factory references / long
    metadata. Without this, assertions on full column text would fail
    against truncated ``...`` strings in the captured stdout.
    """
    # Lazy import so other tests that pin sys.modules aren't affected.
    from raven.cli.commands import app

    runner = CliRunner()
    fake_home = tmp_path / "home"
    fake_home.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, "HOME": str(fake_home), "COLUMNS": "200"}
    return runner.invoke(app, args, env=env)


# ---------------------------------------------------------------------------
# Default config — backend selected and available
# ---------------------------------------------------------------------------


class TestActiveBackend:
    def test_lists_everos_memory(self, tmp_path: Path) -> None:
        result = _invoke(
            _make_runner_args(tmp_path, {}),
            tmp_path,
        )
        assert result.exit_code == 0, result.stdout
        assert "everos-memory" in result.stdout
        assert "1.0.0" in result.stdout
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
        assert "raven.plugin.memory.everos.backend:make_backend" in result.stdout
