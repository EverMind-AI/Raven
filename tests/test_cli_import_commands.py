"""Tests for raven import CLI commands."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from typer.testing import CliRunner

from raven.cli.import_commands import (
    _build_and_run,
    _format_skill_summary,
    _install_hermes_skills,
    _land_hermes_user_md,
    _make_hermes_provider,
    import_app,
)
from raven.config.schema import Config
from raven.importer.orchestrator import ImportSummary
from raven.importer.skills import DiscoveredSkill, SkillOrigin
from raven.importer.skills.installer import SkillImportSummary
from raven.importer.state import ImportState
from raven.importer.types import Platform, Scanner, ScanResult, SourceKind
from raven.memory_engine.consolidate.consolidator import MemoryStore

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
            "raven.importer.scanners.scan_all",
            new=AsyncMock(return_value=_make_scan_results()),
        ):
            result = runner.invoke(import_app, ["scan"])

        assert result.exit_code == 0
        assert "Claude Code" in result.stdout
        assert "global-claude-md" in result.stdout

    def test_scan_empty(self) -> None:
        with patch(
            "raven.importer.scanners.scan_all",
            new=AsyncMock(return_value=[]),
        ):
            result = runner.invoke(import_app, ["scan"])

        assert result.exit_code == 0
        assert "No importable data found" in result.stdout

    def test_scan_shows_importable_skill_count(self) -> None:
        skills = [
            DiscoveredSkill(name="a", path=Path("/fake/a"), origin=SkillOrigin.LOCAL_UNKNOWN, size=1),
            DiscoveredSkill(name="b", path=Path("/fake/b"), origin=SkillOrigin.BUNDLED_PRISTINE, size=1),
        ]
        with (
            patch(
                "raven.importer.scanners.scan_all",
                new=AsyncMock(return_value=_make_scan_results()),
            ),
            patch(
                "raven.importer.skills.hermes.HermesSkillSource.discover",
                new=AsyncMock(return_value=skills),
            ),
        ):
            result = runner.invoke(import_app, ["scan"])

        assert result.exit_code == 0
        assert "Hermes skills: 1 importable" in result.stdout

    def test_scan_with_other_platform_filter_skips_skill_line(self) -> None:
        with (
            patch(
                "raven.importer.scanners.scan_all",
                new=AsyncMock(return_value=_make_scan_results()),
            ),
            patch("raven.importer.skills.hermes.HermesSkillSource.discover") as mocked_discover,
        ):
            result = runner.invoke(import_app, ["scan", "--platform", "claude_code"])

        assert result.exit_code == 0
        mocked_discover.assert_not_called()
        assert "Hermes skills" not in result.stdout


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
                "raven.importer.scanners.scan_all",
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
                "raven.importer.scanners.scan_all",
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
            "raven.importer.scanners.scan_all",
            new=AsyncMock(return_value=[]),
        ):
            result = runner.invoke(
                import_app,
                ["run", "--platform", "claude_code", "--tier", "full", "--yes"],
            )

        assert result.exit_code == 0
        assert "No importable data found" in result.stdout


# ---------------------------------------------------------------------------
# stop
# ---------------------------------------------------------------------------


class TestStop:
    def test_stop_creates_cancel_file(self, tmp_path: Path) -> None:
        state = ImportState(path=tmp_path / "state.json")
        state.set_total(5)
        state.mark_submitted("claude_code", "item1")
        with patch("raven.cli.import_commands._default_state", return_value=state):
            result = runner.invoke(import_app, ["stop"])
        assert result.exit_code == 0
        assert state.cancel_path.exists()
        assert "cancel" in result.output.lower() or "Cancel" in result.output

    def test_stop_no_import(self, tmp_path: Path) -> None:
        state = ImportState(path=tmp_path / "state.json")
        with patch("raven.cli.import_commands._default_state", return_value=state):
            result = runner.invoke(import_app, ["stop"])
        assert result.exit_code == 0
        assert "No import" in result.output

    def test_stop_already_cancelled(self, tmp_path: Path) -> None:
        state = ImportState(path=tmp_path / "state.json")
        state.set_total(5)
        state.cancel_path.touch()
        with patch("raven.cli.import_commands._default_state", return_value=state):
            result = runner.invoke(import_app, ["stop"])
        assert result.exit_code == 0
        assert "already" in result.output.lower()


def test_build_scanners_includes_hermes() -> None:
    from raven.importer.scanners import build_scanners

    platforms = {s.platform for s in build_scanners()}
    assert Platform.HERMES in platforms
    assert Platform.CLAUDE_CODE in platforms


# ---------------------------------------------------------------------------
# Hermes user.md native landing
# ---------------------------------------------------------------------------


def _hermes_user_md_result(path: Path) -> ScanResult:
    return ScanResult(
        source_key="user-md",
        platform=Platform.HERMES,
        kind=SourceKind.MEMORY_FILE,
        file_paths=(path,),
        estimated_size=path.stat().st_size if path.exists() else 0,
        mtime=path.stat().st_mtime if path.exists() else 0.0,
    )


class TestLandHermesUserMd:
    async def test_skips_non_hermes_results(self, tmp_path: Path) -> None:
        result = _scan_result(platform=Platform.CLAUDE_CODE, kind=SourceKind.MEMORY_FILE)
        items: list[tuple[Scanner, ScanResult]] = [(object(), result)]  # type: ignore[list-item]

        await _land_hermes_user_md(items, tmp_path, Config())

        assert not (tmp_path / "user_memory").exists()

    async def test_missing_file_does_not_raise(self, tmp_path: Path) -> None:
        result = _hermes_user_md_result(tmp_path / "does-not-exist.md")
        items: list[tuple[Scanner, ScanResult]] = [(object(), result)]  # type: ignore[list-item]

        await _land_hermes_user_md(items, tmp_path, Config())

    async def test_falls_back_and_lands_entries_when_no_credentials(self, tmp_path: Path) -> None:
        hermes_file = tmp_path / "USER.md"
        hermes_file.write_text("fact one\n§\nfact two", encoding="utf-8")
        items: list[tuple[Scanner, ScanResult]] = [(object(), _hermes_user_md_result(hermes_file))]  # type: ignore[list-item]

        await _land_hermes_user_md(items, tmp_path, Config())

        body = MemoryStore(tmp_path).read_long_term()
        assert "fact one" in body
        assert "fact two" in body
        assert "## Notes" in body

    async def test_log_counts_entries_not_sections(self, tmp_path: Path) -> None:
        from loguru import logger as _logger

        hermes_file = tmp_path / "USER.md"
        hermes_file.write_text("fact one\n§\nfact two", encoding="utf-8")
        items: list[tuple[Scanner, ScanResult]] = [(object(), _hermes_user_md_result(hermes_file))]  # type: ignore[list-item]

        messages: list[str] = []
        sink_id = _logger.add(lambda msg: messages.append(msg.record["message"]), level="INFO")
        try:
            await _land_hermes_user_md(items, tmp_path, Config())
        finally:
            _logger.remove(sink_id)

        # Both entries fall back to the same "## Notes" heading, so a
        # count keyed on unique sections would wrongly report 1.
        assert any("2 entries landed" in m for m in messages)


class TestMakeHermesProvider:
    def test_returns_none_without_credentials(self) -> None:
        assert _make_hermes_provider(Config()) is None

    def test_returns_provider_with_credentials(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from raven.cli import _helpers

        stub = SimpleNamespace(name="stub")
        monkeypatch.setattr(_helpers, "make_provider", lambda _c: stub)
        config = Config()
        config.providers.anthropic.api_key = "sk-ant-test"

        assert _make_hermes_provider(config) is stub

    def test_strips_tty_handlers_after_building(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """litellm reattaches its stderr handler when it is imported, which happens
        inside make_provider -- after redirect_loguru_to_file already stripped."""
        from raven.cli import _helpers, _log_file

        calls: list[str] = []
        monkeypatch.setattr(_helpers, "make_provider", lambda _c: SimpleNamespace(name="stub"))
        monkeypatch.setattr(
            _log_file,
            "_strip_tty_stream_handlers",
            lambda: calls.append("strip"),
        )
        config = Config()
        config.providers.anthropic.api_key = "sk-ant-test"

        _make_hermes_provider(config)

        assert calls == ["strip"]

    def test_does_not_strip_when_no_provider(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from raven.cli import _log_file

        calls: list[str] = []
        monkeypatch.setattr(_log_file, "_strip_tty_stream_handlers", lambda: calls.append("strip"))

        assert _make_hermes_provider(Config()) is None
        assert calls == []


class TestBuildAndRunHermesOrdering:
    async def test_lands_hermes_after_run_import_before_backend_stop(self, tmp_path: Path) -> None:
        calls: list[str] = []

        class _FakeBackend:
            async def start(self) -> None:
                calls.append("start")

            async def stop(self) -> None:
                calls.append("stop")

        summary = ImportSummary(total=1, submitted=1, skipped=0, failed=0, errors=())

        async def _fake_run_import(*_args: object, **_kwargs: object) -> ImportSummary:
            calls.append("run_import")
            return summary

        async def _fake_land(*_args: object, **_kwargs: object) -> None:
            calls.append("land")

        state = ImportState(path=tmp_path / "state.json")
        with (
            patch("raven.cli.import_commands.maybe_build_memory_backend", return_value=_FakeBackend()),
            patch("raven.cli.import_commands.run_import", new=_fake_run_import),
            patch("raven.cli.import_commands._land_hermes_user_md", new=_fake_land),
        ):
            result = await _build_and_run([], state)

        assert calls == ["start", "run_import", "land", "stop"]
        assert result is summary

    async def test_mirror_failure_does_not_fail_the_import(self, tmp_path: Path) -> None:
        calls: list[str] = []

        class _FakeBackend:
            async def start(self) -> None:
                calls.append("start")

            async def stop(self) -> None:
                calls.append("stop")

        summary = ImportSummary(total=1, submitted=1, skipped=0, failed=0, errors=())

        async def _fake_run_import(*_args: object, **_kwargs: object) -> ImportSummary:
            calls.append("run_import")
            return summary

        async def _boom(*_args: object, **_kwargs: object) -> None:
            raise UnicodeDecodeError("utf-8", b"", 0, 1, "bad byte")

        state = ImportState(path=tmp_path / "state.json")
        with (
            patch("raven.cli.import_commands.maybe_build_memory_backend", return_value=_FakeBackend()),
            patch("raven.cli.import_commands.run_import", new=_fake_run_import),
            patch("raven.cli.import_commands._land_hermes_user_md", new=_boom),
        ):
            result = await _build_and_run([], state)

        # The EverOS pass already succeeded, so its result must survive.
        assert result is summary
        assert calls == ["start", "run_import", "stop"]


class TestInstallHermesSkills:
    async def test_returns_none_when_hermes_not_in_scope(self, tmp_path: Path) -> None:
        result = _scan_result(platform=Platform.CLAUDE_CODE, kind=SourceKind.MEMORY_FILE)
        items: list[tuple[Scanner, ScanResult]] = [(object(), result)]  # type: ignore[list-item]
        state = ImportState(path=tmp_path / "state.json")

        assert await _install_hermes_skills(items, tmp_path, state) is None

    async def test_installs_once_when_hermes_in_scope(self, tmp_path: Path) -> None:
        result = _hermes_user_md_result(tmp_path / "does-not-exist.md")
        items: list[tuple[Scanner, ScanResult]] = [(object(), result)]  # type: ignore[list-item]
        state = ImportState(path=tmp_path / "state.json")
        summary = SkillImportSummary(total=1, installed=1, skipped=0, failed=0)

        with patch(
            "raven.cli.import_commands.install_skills",
            new=AsyncMock(return_value=summary),
        ) as mocked:
            got = await _install_hermes_skills(items, tmp_path, state)

        mocked.assert_awaited_once()
        assert got is summary


class TestFormatSkillSummary:
    def test_names_the_untouched_factory_skills(self) -> None:
        line = _format_skill_summary(SkillImportSummary(total=82, installed=12, pristine=70))
        assert line == "Hermes skills: 12 installed, 70 left as factory content"

    def test_rerun_reports_already_present_rather_than_a_bare_zero(self) -> None:
        line = _format_skill_summary(SkillImportSummary(total=82, installed=0, pristine=70, skipped=12))
        assert line == "Hermes skills: 0 installed, 70 left as factory content, 12 already present"

    def test_failures_are_surfaced(self) -> None:
        line = _format_skill_summary(SkillImportSummary(total=2, installed=1, failed=1))
        assert line == "Hermes skills: 1 installed, 1 failed"


class TestBuildAndRunHermesSkills:
    async def test_installs_hermes_skills_after_land_before_stop(self, tmp_path: Path) -> None:
        calls: list[str] = []

        class _FakeBackend:
            async def start(self) -> None:
                calls.append("start")

            async def stop(self) -> None:
                calls.append("stop")

        summary = ImportSummary(total=1, submitted=1, skipped=0, failed=0, errors=())

        async def _fake_run_import(*_args: object, **_kwargs: object) -> ImportSummary:
            calls.append("run_import")
            return summary

        async def _fake_land(*_args: object, **_kwargs: object) -> None:
            calls.append("land")

        async def _fake_install(*_args: object, **_kwargs: object) -> SkillImportSummary:
            calls.append("skills")
            return SkillImportSummary(total=1, installed=1, skipped=0, failed=0)

        state = ImportState(path=tmp_path / "state.json")
        with (
            patch("raven.cli.import_commands.maybe_build_memory_backend", return_value=_FakeBackend()),
            patch("raven.cli.import_commands.run_import", new=_fake_run_import),
            patch("raven.cli.import_commands._land_hermes_user_md", new=_fake_land),
            patch("raven.cli.import_commands._install_hermes_skills", new=_fake_install),
        ):
            result = await _build_and_run([], state)

        assert calls == ["start", "run_import", "land", "skills", "stop"]
        assert result is summary

    async def test_skill_install_failure_does_not_fail_the_import(self, tmp_path: Path) -> None:
        calls: list[str] = []

        class _FakeBackend:
            async def start(self) -> None:
                calls.append("start")

            async def stop(self) -> None:
                calls.append("stop")

        summary = ImportSummary(total=1, submitted=1, skipped=0, failed=0, errors=())

        async def _fake_run_import(*_args: object, **_kwargs: object) -> ImportSummary:
            calls.append("run_import")
            return summary

        async def _fake_land(*_args: object, **_kwargs: object) -> None:
            calls.append("land")

        async def _boom(*_args: object, **_kwargs: object) -> SkillImportSummary:
            raise OSError("disk full")

        state = ImportState(path=tmp_path / "state.json")
        with (
            patch("raven.cli.import_commands.maybe_build_memory_backend", return_value=_FakeBackend()),
            patch("raven.cli.import_commands.run_import", new=_fake_run_import),
            patch("raven.cli.import_commands._land_hermes_user_md", new=_fake_land),
            patch("raven.cli.import_commands._install_hermes_skills", new=_boom),
        ):
            result = await _build_and_run([], state)

        # The EverOS pass and the USER.md mirror already succeeded, so their
        # results must survive a skill-install failure.
        assert result is summary
        assert calls == ["start", "run_import", "land", "stop"]


class TestStatusCancelled:
    def test_status_shows_cancelled(self, tmp_path: Path) -> None:
        state = ImportState(path=tmp_path / "state.json")
        state.set_total(5)
        state.mark_submitted("claude_code", "item1")
        state.cancel_path.touch()
        with patch("raven.cli.import_commands._default_state", return_value=state):
            result = runner.invoke(import_app, ["status"])
        assert result.exit_code == 0
        assert "Cancelled" in result.output or "cancelled" in result.output
