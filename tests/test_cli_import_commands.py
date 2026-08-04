"""Tests for raven import CLI commands."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from typer.testing import CliRunner

from raven.cli._theme import POINTER, QMARK
from raven.cli.import_commands import (
    ImportRunResult,
    _build_and_run,
    _format_skill_summary,
    _install_hermes_skills,
    _land_hermes_user_md,
    _make_hermes_provider,
    _print_summary,
    import_app,
)
from raven.config.schema import Config
from raven.importer.hermes_user_md import ImportedSections
from raven.importer.orchestrator import ImportSummary
from raven.importer.skills import DiscoveredSkill, SkillOrigin
from raven.importer.skills.installer import SkillImportSummary
from raven.importer.state import ImportState
from raven.importer.types import Platform, Scanner, ScanResult, SourceKind, Tier
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
        # discover() has to be stubbed too, or the result depends on how many
        # skills the developer's own Hermes install happens to hold.
        with (
            patch(
                "raven.importer.scanners.scan_all",
                new=AsyncMock(return_value=[]),
            ),
            patch(
                "raven.importer.skills.hermes.HermesSkillSource.discover",
                new=AsyncMock(return_value=[]),
            ),
        ):
            result = runner.invoke(import_app, ["scan"])

        assert result.exit_code == 0
        assert "No importable data found" in result.stdout

    def test_scan_with_only_skills_does_not_say_there_is_nothing(self) -> None:
        """Skills never travel as ScanResults, so an install whose only
        importable data is skills produced an empty result list and was told
        there was nothing to import."""
        skills = [DiscoveredSkill(name="a", path=Path("/fake/a"), origin=SkillOrigin.LOCAL_UNKNOWN, registry_name="a")]
        with (
            patch("raven.importer.scanners.scan_all", new=AsyncMock(return_value=[])),
            patch(
                "raven.importer.skills.hermes.HermesSkillSource.discover",
                new=AsyncMock(return_value=skills),
            ),
        ):
            result = runner.invoke(import_app, ["scan"])

        assert result.exit_code == 0
        assert "Hermes skills: 1 importable" in result.stdout
        assert "No importable data found" not in result.stdout

    def test_scan_shows_importable_skill_count(self) -> None:
        skills = [
            DiscoveredSkill(name="a", path=Path("/fake/a"), origin=SkillOrigin.LOCAL_UNKNOWN, registry_name="a"),
            DiscoveredSkill(name="b", path=Path("/fake/b"), origin=SkillOrigin.BUNDLED_PRISTINE, registry_name="b"),
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
                new=AsyncMock(return_value=ImportRunResult(summary=summary)),
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

    def test_run_installs_skills_when_the_scan_finds_nothing(self, tmp_path: Path) -> None:
        """Skills are directories rather than message sources, so they never
        arrive as ScanResults. An install whose only importable data is skills
        reaches this early return, and stopping there tells that user there is
        nothing to import while a dozen skills sit on disk.
        """
        with (
            patch("raven.importer.scanners.scan_all", new=AsyncMock(return_value=[])),
            patch(
                "raven.cli.import_commands.install_skills",
                new=AsyncMock(return_value=SkillImportSummary(total=12, installed=12)),
            ),
            patch("raven.cli.import_commands.load_config", return_value=SimpleNamespace(workspace_path=tmp_path)),
        ):
            result = runner.invoke(import_app, ["run", "--platform", "hermes", "--tier", "full", "--yes"])

        assert result.exit_code == 0, result.output
        assert "12 installed" in result.stdout
        assert "No importable data found" not in result.stdout

    def test_run_installs_skills_when_the_tier_keeps_nothing(self, tmp_path: Path) -> None:
        """The tier filter has nothing of the skills' to keep either, so the
        memory-files tier on a Hermes install with conversations only lands on
        the same dead end.
        """
        results = [_scan_result("h1", platform=Platform.HERMES, kind=SourceKind.CONVERSATION)]

        with (
            patch("raven.importer.scanners.scan_all", new=AsyncMock(return_value=results)),
            patch("raven.cli.import_commands._importable_skill_count", new=AsyncMock(return_value=12)),
            patch(
                "raven.cli.import_commands.install_skills",
                new=AsyncMock(return_value=SkillImportSummary(total=12, installed=12)),
            ),
            patch("raven.cli.import_commands.load_config", return_value=SimpleNamespace(workspace_path=tmp_path)),
        ):
            result = runner.invoke(import_app, ["run", "--platform", "hermes", "--tier", "memory_files", "--yes"])

        assert result.exit_code == 0, result.output
        assert "12 installed" in result.stdout
        assert "No items match the selected tier" not in result.stdout

    def test_run_selects_platform_and_tier_inside_the_event_loop(self, tmp_path: Path) -> None:
        """The selectors run under `asyncio.run(_run_async(...))`, so they must
        reach questionary's async API: `ask()` drives prompt_toolkit through
        `asyncio.run()`, which raises inside a running loop. Both selectors are
        exercised here because only an unfiltered invocation reaches them.
        """
        state = ImportState(path=tmp_path / "state.json")
        summary = ImportSummary(total=1, submitted=1, skipped=0, failed=0, errors=())
        results = [
            _scan_result("c1", platform=Platform.CLAUDE_CODE),
            _scan_result("h1", platform=Platform.HERMES),
        ]
        fake = _RecordingQuestionary(iter([Platform.HERMES, Tier.FULL]))

        with self._patched_run(state, summary, results, fake):
            result = runner.invoke(import_app, ["run", "--yes"])

        assert result.exit_code == 0, result.output
        assert [message for message, _choices, _kwargs in fake.calls] == [
            "Select platform:",
            "Select import tier:",
        ]

    def test_run_selectors_carry_the_shared_prompt_chrome(self, tmp_path: Path) -> None:
        """Both selectors must pass the shared style and glyphs; questionary's
        defaults ("?" marker, ">>" pointer, no palette) read as another program.
        """
        state = ImportState(path=tmp_path / "state.json")
        summary = ImportSummary(total=1, submitted=1, skipped=0, failed=0, errors=())
        results = [
            _scan_result("c1", platform=Platform.CLAUDE_CODE),
            _scan_result("h1", platform=Platform.HERMES),
        ]
        fake = _RecordingQuestionary(iter([Platform.HERMES, Tier.FULL]))

        with self._patched_run(state, summary, results, fake):
            result = runner.invoke(import_app, ["run", "--yes"])

        assert result.exit_code == 0, result.output
        for _message, _choices, kwargs in fake.calls:
            assert kwargs["qmark"] == QMARK
            assert kwargs["pointer"] == POINTER
            assert kwargs["style"] is not None

    def test_run_menus_name_the_skills_that_would_be_installed(self, tmp_path: Path) -> None:
        """Skills never travel as ScanResults, so a menu built from them alone
        offers "2 items" and then installs a dozen skills nobody was told about.
        Factory-pristine skills are not installed and must not be counted.
        """
        state = ImportState(path=tmp_path / "state.json")
        summary = ImportSummary(total=1, submitted=1, skipped=0, failed=0, errors=())
        results = [
            _scan_result("c1", platform=Platform.CLAUDE_CODE),
            _scan_result("h1", platform=Platform.HERMES, kind=SourceKind.MEMORY_FILE),
            _scan_result("h2", platform=Platform.HERMES),
        ]
        skills = [
            DiscoveredSkill(name="a", path=Path("/fake/a"), origin=SkillOrigin.LOCAL_UNKNOWN, registry_name="a"),
            DiscoveredSkill(name="b", path=Path("/fake/b"), origin=SkillOrigin.CURATOR_MANAGED, registry_name="b"),
            DiscoveredSkill(name="c", path=Path("/fake/c"), origin=SkillOrigin.BUNDLED_PRISTINE, registry_name="c"),
        ]
        fake = _RecordingQuestionary(iter([Platform.HERMES, Tier.MEMORY_FILES]))

        with (
            self._patched_run(state, summary, results, fake),
            patch(
                "raven.importer.skills.hermes.HermesSkillSource.discover",
                new=AsyncMock(return_value=skills),
            ),
        ):
            result = runner.invoke(import_app, ["run", "--yes"])

        assert result.exit_code == 0, result.output
        platform_choices, tier_choices = (choices for _m, choices, _k in fake.calls)
        hermes_row = next(c["name"] for c in platform_choices if c["value"] is Platform.HERMES)
        assert "2 skills" in hermes_row
        claude_row = next(c["name"] for c in platform_choices if c["value"] is Platform.CLAUDE_CODE)
        assert "skills" not in claude_row
        assert any("2 skills" in c["name"] for c in tier_choices)
        assert "2 skills" in result.output

    def test_menu_rows_fit_eighty_columns(self, tmp_path: Path) -> None:
        """A row past 80 columns wraps mid-phrase, which costs far more
        legibility than wide padding buys. Four-digit counts are the widest
        case worth planning for, and every count of that many digits is the
        same width.
        """
        state = ImportState(path=tmp_path / "state.json")
        summary = ImportSummary(total=1, submitted=1, skipped=0, failed=0, errors=())
        results = [
            _scan_result("h-md", platform=Platform.HERMES, kind=SourceKind.MEMORY_FILE),
            *[_scan_result(f"h{i}", platform=Platform.HERMES) for i in range(1000)],
            _scan_result("c-md", platform=Platform.CLAUDE_CODE, kind=SourceKind.MEMORY_FILE),
            *[_scan_result(f"c{i}", platform=Platform.CLAUDE_CODE) for i in range(1000)],
        ]
        skills = [
            DiscoveredSkill(
                name=f"s{i}", path=Path(f"/fake/s{i}"), origin=SkillOrigin.LOCAL_UNKNOWN, registry_name=f"s{i}"
            )
            for i in range(1000)
        ]
        fake = _RecordingQuestionary(iter([Platform.HERMES, Tier.MEMORY_FILES]))

        with (
            self._patched_run(state, summary, results, fake),
            patch(
                "raven.importer.skills.hermes.HermesSkillSource.discover",
                new=AsyncMock(return_value=skills),
            ),
        ):
            result = runner.invoke(import_app, ["run", "--yes"])

        assert result.exit_code == 0, result.output
        # Two columns for the pointer and the space questionary puts before it.
        widest = max(len(c["name"]) + 2 for _m, choices, _k in fake.calls for c in choices)
        assert widest <= 80, widest

    @staticmethod
    def _patched_run(
        state: ImportState,
        summary: ImportSummary,
        results: list[ScanResult],
        questionary: "_RecordingQuestionary",
    ) -> Any:
        from contextlib import ExitStack

        stack = ExitStack()
        for ctx in (
            patch("raven.importer.scanners.scan_all", new=AsyncMock(return_value=results)),
            patch(
                "raven.cli.import_commands._build_and_run", new=AsyncMock(return_value=ImportRunResult(summary=summary))
            ),
            patch("raven.cli.import_commands._default_state", return_value=state),
            patch("raven.cli.import_commands._require_questionary", return_value=questionary),
        ):
            stack.enter_context(ctx)
        return stack


class _RecordingQuestionary:
    """questionary stand-in that records each prompt and refuses the sync `ask`.

    `ask()` fails the way prompt_toolkit does inside a running loop, so a
    selector that skips `ask_async` is caught rather than silently passing.
    """

    def __init__(self, answers: Iterator[object]) -> None:
        self._answers = answers
        self.calls: list[tuple[str, list[dict[str, Any]], dict[str, Any]]] = []

    def select(self, message: str, choices: list[dict[str, Any]], **kwargs: Any) -> Any:
        self.calls.append((message, choices, kwargs))
        answers = self._answers

        class _Question:
            def ask(self) -> object:
                raise RuntimeError("asyncio.run() cannot be called from a running event loop")

            async def ask_async(self) -> object:
                return next(answers)

        return _Question()


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
        assert result.summary is summary

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
        assert result.summary is summary
        assert "bad byte" in result.profile_error
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
        assert line == "12 installed, 70 kept as factory"

    def test_rerun_reports_already_present_rather_than_a_bare_zero(self) -> None:
        line = _format_skill_summary(SkillImportSummary(total=82, installed=0, pristine=70, skipped=12))
        assert line == "0 installed, 70 kept as factory, 12 already present"

    def test_failures_are_surfaced(self) -> None:
        line = _format_skill_summary(SkillImportSummary(total=2, installed=1, failed=1))
        assert line == "1 installed, 1 failed"


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
        assert result.summary is summary

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
        assert result.summary is summary
        assert result.skill_error == "disk full"
        assert calls == ["start", "run_import", "land", "stop"]


class TestPrintSummary:
    """The block `_build_and_run` no longer prints from inside the progress bar."""

    @staticmethod
    def _render(result: ImportRunResult, log_path: Path | None = None) -> str:
        import io

        from rich.console import Console

        buf = io.StringIO()
        with patch("raven.cli.import_commands.console", Console(file=buf, width=100)):
            _print_summary(result, log_path=log_path)
        return buf.getvalue()

    def test_every_phase_reports_inside_the_block(self) -> None:
        """Each phase used to print its own line the moment it finished, which
        put it above the progress bars -- before the numbers it qualifies."""
        out = self._render(
            ImportRunResult(
                summary=ImportSummary(total=4, submitted=4, skipped=0, failed=0, errors=()),
                profile=ImportedSections(written=("## Preferences", "## Notes"), skipped=1),
                skills=SkillImportSummary(total=82, installed=12, pristine=70),
            ),
            log_path=Path("/fake/import.log"),
        )

        header = out.index("Import Complete")
        assert header < out.index("Submitted:") < out.index("Profile:") < out.index("Skills:") < out.index("Log:")
        assert "2 entries into user.md, 1 already present" in out
        assert "12 installed, 70 kept as factory" in out

    def test_labels_share_one_column(self) -> None:
        out = self._render(
            ImportRunResult(
                summary=ImportSummary(total=1, submitted=1, skipped=0, failed=0, errors=()),
                profile=ImportedSections(written=("## Notes",), skipped=0),
                skills=SkillImportSummary(total=1, installed=1),
            ),
            log_path=Path("/fake/import.log"),
        )

        starts = {
            line.index(line.split(":", 1)[1].lstrip()[:1], line.index(":"))
            for line in out.splitlines()
            if line.startswith("  ") and ":" in line and line.split(":", 1)[1].strip()
        }
        assert len(starts) == 1, out

    def test_a_failed_phase_names_its_reason(self) -> None:
        """A phase that failed after a successful EverOS pass has no other way
        to reach the user: loguru is file-only during `run`."""
        out = self._render(
            ImportRunResult(
                summary=ImportSummary(total=1, submitted=1, skipped=0, failed=0, errors=()),
                profile_error="disk full",
                skill_error="permission denied",
            )
        )

        assert "not mirrored: disk full" in out
        assert "not installed: permission denied" in out

    def test_phases_that_did_not_run_stay_out_of_the_block(self) -> None:
        out = self._render(ImportRunResult(summary=ImportSummary(total=1, submitted=1, skipped=0, failed=0, errors=())))

        assert "Profile:" not in out
        assert "Skills:" not in out


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
