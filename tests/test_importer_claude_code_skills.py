"""Claude Code skill discovery and its landing in the pool."""

from __future__ import annotations

from pathlib import Path

import pytest

from raven.importer.skills import SkillOrigin, installer
from raven.importer.skills.claude_code import ClaudeCodeSkillSource
from raven.importer.skills.installer import SkillImportSummary, install_skills
from raven.importer.state import ImportState
from raven.importer.types import Platform


def _skill(skills_root: Path, name: str, *, frontmatter_name: str | None = None) -> Path:
    directory = skills_root / name
    directory.mkdir(parents=True)
    frontmatter = f"---\nname: {frontmatter_name}\n---\n" if frontmatter_name else ""
    (directory / "SKILL.md").write_text(frontmatter + "body\n", encoding="utf-8")
    return directory


async def test_discovers_only_direct_children_that_hold_a_skill_md(tmp_path: Path) -> None:
    claude = tmp_path / ".claude"
    skills = claude / "skills"
    _skill(skills, "archify", frontmatter_name="archify")
    (skills / "notes.md").write_text("not a skill", encoding="utf-8")
    (skills / "empty").mkdir()
    nested = skills / "nested" / "deeper"
    nested.mkdir(parents=True)
    (nested / "SKILL.md").write_text("---\nname: deep\n---\n", encoding="utf-8")

    found = await ClaudeCodeSkillSource(claude).discover()

    assert [(s.name, s.registry_name, s.origin) for s in found] == [("archify", "archify", SkillOrigin.LOCAL_UNKNOWN)]
    assert found[0].path == skills / "archify"


async def test_registry_name_falls_back_to_the_directory_name(tmp_path: Path) -> None:
    claude = tmp_path / ".claude"
    _skill(claude / "skills", "plain")

    (found,) = await ClaudeCodeSkillSource(claude).discover()

    assert found.registry_name == "plain"


async def test_a_machine_without_the_skills_directory_offers_nothing(tmp_path: Path) -> None:
    assert await ClaudeCodeSkillSource(tmp_path / ".claude").discover() == []


async def test_installs_under_the_platforms_own_pool_directory(tmp_path: Path) -> None:
    claude = tmp_path / ".claude"
    _skill(claude / "skills", "archify", frontmatter_name="archify")
    workspace = tmp_path / "ws"
    workspace.mkdir()
    state = ImportState(path=tmp_path / "state.json")

    summary = await install_skills(ClaudeCodeSkillSource(claude), workspace, state)

    assert (summary.installed, summary.skipped, summary.failed, summary.pristine) == (1, 0, 0, 0)
    assert (workspace / "skills" / "claude_code" / "archify" / "SKILL.md").is_file()
    assert not (workspace / "skills" / "hermes").exists()
    assert state.is_submitted(Platform.CLAUDE_CODE, "skill-archify")


async def test_install_reports_progress_before_each_copy_and_once_at_the_end(tmp_path: Path) -> None:
    claude = tmp_path / ".claude"
    _skill(claude / "skills", "a", frontmatter_name="a")
    _skill(claude / "skills", "b", frontmatter_name="b")
    workspace = tmp_path / "ws"
    workspace.mkdir()
    seen: list[tuple[int, int]] = []

    await install_skills(
        ClaudeCodeSkillSource(claude),
        workspace,
        ImportState(path=tmp_path / "state.json"),
        on_progress=lambda done, total: seen.append((done, total)),
    )

    assert seen == [(0, 2), (1, 2), (2, 2)]


async def test_a_stop_between_skills_leaves_the_rest_for_the_next_run(tmp_path: Path) -> None:
    claude = tmp_path / ".claude"
    _skill(claude / "skills", "a", frontmatter_name="a")
    _skill(claude / "skills", "b", frontmatter_name="b")
    workspace = tmp_path / "ws"
    workspace.mkdir()
    state = ImportState(path=tmp_path / "state.json")
    landed = workspace / "skills" / "claude_code"

    summary = await install_skills(
        ClaudeCodeSkillSource(claude),
        workspace,
        state,
        cancelled=lambda: (landed / "a" / "SKILL.md").is_file(),
    )

    assert (summary.installed, summary.skipped, summary.failed) == (1, 1, 0)
    assert (landed / "a" / "SKILL.md").is_file()
    assert not (landed / "b").exists()
    assert state.is_submitted(Platform.CLAUDE_CODE, "skill-a")
    assert not state.is_submitted(Platform.CLAUDE_CODE, "skill-b")


async def test_a_copy_that_fails_is_named_in_the_summary_and_retried_next_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    claude = tmp_path / ".claude"
    _skill(claude / "skills", "a", frontmatter_name="a")
    _skill(claude / "skills", "b", frontmatter_name="b")
    workspace = tmp_path / "ws"
    workspace.mkdir()
    state = ImportState(path=tmp_path / "state.json")
    real_copytree = installer.shutil.copytree
    broken = {"b"}

    def _copytree(src: Path, dst: Path, **kwargs: object) -> object:
        if src.name in broken:
            raise OSError("disk full")
        return real_copytree(src, dst, **kwargs)

    monkeypatch.setattr(installer.shutil, "copytree", _copytree)

    first = await install_skills(ClaudeCodeSkillSource(claude), workspace, state)

    assert first == SkillImportSummary(total=2, installed=1, failed=1, errors=("b: disk full",))
    assert not (workspace / "skills" / "claude_code" / "b").exists()
    assert not state.is_submitted(Platform.CLAUDE_CODE, "skill-b")

    broken.clear()
    second = await install_skills(ClaudeCodeSkillSource(claude), workspace, state)

    assert second == SkillImportSummary(total=2, installed=1, skipped=1)
    assert state.is_submitted(Platform.CLAUDE_CODE, "skill-b")
