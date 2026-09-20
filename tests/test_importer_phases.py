"""Post-import phases: which platforms contribute what, and how it lands."""

from __future__ import annotations

from pathlib import Path

import pytest

from raven.importer import phases
from raven.importer.phases import PhaseOutcome, profile_entries, run_phases, skill_sources
from raven.importer.skills.claude_code import ClaudeCodeSkillSource
from raven.importer.skills.hermes import HermesSkillSource
from raven.importer.skills.installer import SkillImportSummary
from raven.importer.state import ImportState
from raven.importer.types import Platform, ScanResult, SourceKind
from raven.memory_engine import MemoryStore


def _result(
    platform: Platform,
    key: str,
    paths: tuple[Path, ...] = (),
    kind: SourceKind = SourceKind.MEMORY_FILE,
) -> ScanResult:
    return ScanResult(source_key=key, platform=platform, kind=kind, file_paths=paths, estimated_size=1, mtime=1.0)


def _item(result: ScanResult) -> tuple[object, ScanResult]:
    return (object(), result)


def _memory(directory: Path, name: str, body: str, *, kind: str | None) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    frontmatter = f"---\nname: {name}\nmetadata:\n  type: {kind}\n---\n" if kind else ""
    path.write_text(frontmatter + body, encoding="utf-8")
    return path


def _claude_memory_dir(root: Path) -> tuple[Path, ...]:
    return (
        _memory(root, "who.md", "Works on Raven, prefers Chinese in chat.\n", kind="user"),
        _memory(root, "how.md", "Push back with rigor; no thanks-you phrases.\n", kind="feedback"),
        _memory(root, "what.md", "The importer batches at fifty.\n", kind="project"),
        _memory(root, "loose.md", "No frontmatter at all.\n", kind=None),
    )


class TestProfileEntries:
    def test_claude_code_contributes_user_and_feedback_memories_only(self, tmp_path: Path) -> None:
        paths = _claude_memory_dir(tmp_path / "mem")
        items = [_item(_result(Platform.CLAUDE_CODE, "proj-memory", paths))]

        assert profile_entries(items) == [
            "Works on Raven, prefers Chinese in chat.",
            "Push back with rigor; no thanks-you phrases.",
        ]

    def test_the_same_library_reached_through_two_project_directories_counts_once(self, tmp_path: Path) -> None:
        """Git worktrees symlink their memory directory to the main checkout's,
        so the scanner yields the same files under several source keys; each
        duplicate would otherwise cost a classification call."""
        paths = _claude_memory_dir(tmp_path / "mem")
        items = [
            _item(_result(Platform.CLAUDE_CODE, "repo-memory", paths)),
            _item(_result(Platform.CLAUDE_CODE, "repo-a-memory", paths)),
        ]

        assert len(profile_entries(items)) == 2

    def test_hermes_contributes_its_user_md_entries(self, tmp_path: Path) -> None:
        user_md = tmp_path / "USER.md"
        user_md.write_text("fact one\n§\nfact two", encoding="utf-8")
        items = [_item(_result(Platform.HERMES, "user-md", (user_md,)))]

        assert profile_entries(items) == ["fact one", "fact two"]

    def test_conversations_and_other_memory_sources_contribute_nothing(self, tmp_path: Path) -> None:
        user_md = tmp_path / "MEMORY.md"
        user_md.write_text("fact\n", encoding="utf-8")
        items = [
            _item(_result(Platform.HERMES, "memory-md", (user_md,))),
            _item(_result(Platform.CLAUDE_CODE, "sess-1", (user_md,), kind=SourceKind.CONVERSATION)),
            _item(_result(Platform.CODEX, "anything", (user_md,))),
        ]

        assert profile_entries(items) == []

    def test_an_unreadable_file_is_skipped_not_raised(self, tmp_path: Path) -> None:
        items = [_item(_result(Platform.HERMES, "user-md", (tmp_path / "missing.md",)))]

        assert profile_entries(items) == []


class TestSkillSources:
    def test_follow_the_platforms_present_in_the_run(self) -> None:
        both = [_item(_result(Platform.HERMES, "user-md")), _item(_result(Platform.CLAUDE_CODE, "m"))]
        claude_only = [_item(_result(Platform.CLAUDE_CODE, "m"))]
        codex_only = [_item(_result(Platform.CODEX, "m"))]

        assert [type(s) for s in skill_sources(both)] == [HermesSkillSource, ClaudeCodeSkillSource]
        assert [type(s) for s in skill_sources(claude_only)] == [ClaudeCodeSkillSource]
        assert skill_sources(codex_only) == []


class TestRunPhases:
    async def test_nothing_in_scope_is_an_empty_outcome(self, tmp_path: Path) -> None:
        state = ImportState(path=tmp_path / "state.json")

        outcome = await run_phases([], tmp_path, state, provider=None, model="")

        assert outcome == PhaseOutcome()

    async def test_entries_land_under_the_fallback_heading_without_a_provider(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        paths = _claude_memory_dir(tmp_path / "mem")
        items = [_item(_result(Platform.CLAUDE_CODE, "proj-memory", paths))]
        monkeypatch.setitem(
            phases._SKILL_SOURCES, Platform.CLAUDE_CODE, lambda: ClaudeCodeSkillSource(tmp_path / "none")
        )
        workspace = tmp_path / "ws"
        workspace.mkdir()

        outcome = await run_phases(items, workspace, ImportState(path=tmp_path / "state.json"), provider=None, model="")

        body = MemoryStore(workspace).read_long_term()
        assert "prefers Chinese in chat" in body
        assert "no thanks-you phrases" in body
        assert "The importer batches at fifty" not in body
        assert "## Notes" in body
        assert outcome.profile is not None and len(outcome.profile.written) == 2
        assert outcome.profile_error == ""

    async def test_progress_is_reported_by_phase_kind(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        paths = _claude_memory_dir(tmp_path / "mem")
        claude = tmp_path / ".claude"
        skill = claude / "skills" / "archify"
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text("---\nname: archify\n---\n", encoding="utf-8")
        monkeypatch.setitem(phases._SKILL_SOURCES, Platform.CLAUDE_CODE, lambda: ClaudeCodeSkillSource(claude))
        items = [_item(_result(Platform.CLAUDE_CODE, "proj-memory", paths))]
        workspace = tmp_path / "ws"
        workspace.mkdir()
        seen: list[tuple[str, int, int]] = []

        outcome = await run_phases(
            items,
            workspace,
            ImportState(path=tmp_path / "state.json"),
            provider=None,
            model="",
            on_phase=lambda kind, done, total: seen.append((kind, done, total)),
        )

        assert seen == [("profile", 0, 2), ("profile", 1, 2), ("profile", 2, 2), ("skills", 0, 1), ("skills", 1, 1)]
        assert outcome.skills == SkillImportSummary(total=1, installed=1)
        assert (workspace / "skills" / "claude_code" / "archify" / "SKILL.md").is_file()

    async def test_a_profile_failure_is_reported_and_the_skills_still_land(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        paths = _claude_memory_dir(tmp_path / "mem")
        claude = tmp_path / ".claude"
        skill = claude / "skills" / "archify"
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text("---\nname: archify\n---\n", encoding="utf-8")
        monkeypatch.setitem(phases._SKILL_SOURCES, Platform.CLAUDE_CODE, lambda: ClaudeCodeSkillSource(claude))

        async def _boom(*_a: object, **_k: object) -> None:
            raise UnicodeDecodeError("utf-8", b"", 0, 1, "bad byte")

        monkeypatch.setattr("raven.importer.hermes_user_md.import_user_md_sections", _boom)
        items = [_item(_result(Platform.CLAUDE_CODE, "proj-memory", paths))]
        workspace = tmp_path / "ws"
        workspace.mkdir()

        outcome = await run_phases(items, workspace, ImportState(path=tmp_path / "state.json"), provider=None, model="")

        assert outcome.profile is None
        assert "bad byte" in outcome.profile_error
        assert outcome.skills == SkillImportSummary(total=1, installed=1)

    async def test_a_skill_failure_is_reported_with_its_platform(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def _boom(*_a: object, **_k: object) -> SkillImportSummary:
            raise OSError("disk full")

        monkeypatch.setattr(phases, "install_skills", _boom)
        items = [_item(_result(Platform.CLAUDE_CODE, "proj-memory"))]

        outcome = await run_phases(items, tmp_path, ImportState(path=tmp_path / "state.json"), provider=None, model="")

        assert outcome.skills is None
        assert outcome.skill_error == "claude_code: disk full"
