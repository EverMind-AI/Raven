"""Tests for raven import CLI commands."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

from typer.testing import CliRunner

from raven.cli.import_commands import import_app
from raven.importer.orchestrator import ImportSummary
from raven.importer.state import ImportState
from raven.importer.types import Platform, ScanResult, SourceKind

runner = CliRunner()


def _scan_result(
    key: str = "k1",
    platform: Platform = Platform.CLAUDE_CODE,
    kind: SourceKind = SourceKind.CONVERSATION,
    size: int = 1000,
) -> ScanResult:
    return ScanResult(
        source_key=key,
        platform=platform,
        kind=kind,
        file_paths=(Path("/fake"),),
        estimated_size=size,
        mtime=1000.0,
    )


def _make_scan_results() -> list[ScanResult]:
    return [
        _scan_result("global-claude-md", kind=SourceKind.MEMORY_FILE, size=2048),
        _scan_result("proj-memory", kind=SourceKind.MEMORY_FILE, size=48000),
        _scan_result("sess-001", kind=SourceKind.CONVERSATION, size=120000),
    ]


# ---------------------------------------------------------------------------
# scan
# ---------------------------------------------------------------------------


class TestScan:
    def test_scan_shows_results(self) -> None:
        with patch(
            "raven.cli.import_commands._scan_all_platforms",
            new=AsyncMock(return_value=_make_scan_results()),
        ):
            result = runner.invoke(import_app, ["scan"])

        assert result.exit_code == 0
        assert "claude_code" in result.stdout
        assert "global-claude-md" in result.stdout

    def test_scan_empty(self) -> None:
        with patch(
            "raven.cli.import_commands._scan_all_platforms",
            new=AsyncMock(return_value=[]),
        ):
            result = runner.invoke(import_app, ["scan"])

        assert result.exit_code == 0
        assert "No importable data found" in result.stdout


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------


class TestStatus:
    def test_status_shows_summary(self, tmp_path: Path) -> None:
        state = ImportState(path=tmp_path / "state.json")
        state.set_total(10)
        state.mark_submitted("claude_code", "a")
        state.mark_submitted("claude_code", "b")
        state.mark_failed("claude_code", "c", "err")

        with patch("raven.cli.import_commands._default_state", return_value=state):
            result = runner.invoke(import_app, ["status"])

        assert result.exit_code == 0
        assert "10" in result.stdout
        assert "2" in result.stdout

    def test_status_json(self, tmp_path: Path) -> None:
        state = ImportState(path=tmp_path / "state.json")
        state.set_total(5)
        state.mark_submitted("claude_code", "a")

        with patch("raven.cli.import_commands._default_state", return_value=state):
            result = runner.invoke(import_app, ["status", "--json"])

        data = json.loads(result.stdout)
        assert data["total"] == 5
        assert data["submitted"] == 1

    def test_status_no_state(self) -> None:
        state = ImportState(path=Path("/nonexistent/state.json"))

        with patch("raven.cli.import_commands._default_state", return_value=state):
            result = runner.invoke(import_app, ["status"])

        assert result.exit_code == 0
        assert "No import in progress" in result.stdout


# ---------------------------------------------------------------------------
# run
# ---------------------------------------------------------------------------


class TestRun:
    def test_run_non_interactive(self, tmp_path: Path) -> None:
        state = ImportState(path=tmp_path / "state.json")
        summary = ImportSummary(total=2, submitted=2, skipped=0, failed=0, errors=())

        with (
            patch(
                "raven.cli.import_commands._scan_all_platforms",
                new=AsyncMock(return_value=_make_scan_results()),
            ),
            patch(
                "raven.cli.import_commands._build_and_run",
                new=AsyncMock(return_value=summary),
            ),
            patch("raven.cli.import_commands._default_state", return_value=state),
        ):
            result = runner.invoke(
                import_app,
                ["run", "--platform", "claude_code", "--tier", "full", "--yes"],
            )

        assert result.exit_code == 0

    def test_run_no_backend(self, tmp_path: Path) -> None:
        state = ImportState(path=tmp_path / "state.json")

        with (
            patch(
                "raven.cli.import_commands._scan_all_platforms",
                new=AsyncMock(return_value=_make_scan_results()),
            ),
            patch("raven.cli.import_commands._default_state", return_value=state),
            patch(
                "raven.cli.import_commands.maybe_build_memory_backend",
                return_value=None,
            ),
        ):
            result = runner.invoke(
                import_app,
                ["run", "--platform", "claude_code", "--tier", "full", "--yes"],
            )

        assert result.exit_code == 1

    def test_run_no_sources(self) -> None:
        with patch(
            "raven.cli.import_commands._scan_all_platforms",
            new=AsyncMock(return_value=[]),
        ):
            result = runner.invoke(
                import_app,
                ["run", "--platform", "claude_code", "--tier", "full", "--yes"],
            )

        assert result.exit_code == 0
        assert "No importable data found" in result.stdout
