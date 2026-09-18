"""Undoing what a role wrote outside its own paths, including what it committed."""

from __future__ import annotations

from pathlib import Path

import pytest

from raven.stint.enforce import enforce, roster_grader
from raven.stint.git import ProjectGit
from raven.stint.ownership import Role, Roster


@pytest.fixture
def project(tmp_path: Path) -> Path:
    tree = tmp_path / "project"
    tree.mkdir()
    (tree / "src").mkdir()
    (tree / "src" / "main.py").write_text("print('hello')\n", encoding="utf-8")
    (tree / "NOTES.md").write_text("# Notes\n\nfirst line\n", encoding="utf-8")
    return tree


@pytest.fixture
def git(project: Path) -> ProjectGit:
    repo = ProjectGit(project)
    repo.ensure_repo()
    return repo


def _roster() -> Roster:
    return Roster(
        roles=[
            Role(name="developer", owns=("src/**",), appends=("NOTES.md",)),
            Role(name="reviewer", owns=("reports/round_{NN}.md",)),
        ]
    )


def _run(git: ProjectGit, project: Path, role: str, base: str, round_index: int = 0, **kwargs):
    return enforce(
        git,
        role=role,
        grade=roster_grader(_roster(), role, round_index),
        quarantine=project.parent / "violations" / role,
        stage_base=base,
        **kwargs,
    )


def test_a_role_writing_inside_what_it_owns_is_left_alone(git: ProjectGit, project: Path) -> None:
    base = git.head()
    (project / "src" / "main.py").write_text("print('changed')\n", encoding="utf-8")

    report = _run(git, project, "developer", base)

    assert report.clean
    assert (project / "src" / "main.py").read_text(encoding="utf-8") == "print('changed')\n"


def test_a_stray_write_is_undone_and_kept_where_a_person_can_read_it(git: ProjectGit, project: Path) -> None:
    base = git.head()
    (project / "reports").mkdir()
    (project / "reports" / "round_00.md").write_text("the reviewer's, not mine\n", encoding="utf-8")

    report = _run(git, project, "developer", base)

    assert report.quarantined == ("reports/round_00.md",)
    assert not (project / "reports" / "round_00.md").exists()
    kept = project.parent / "violations" / "developer" / "reports" / "round_00.md"
    assert kept.read_text(encoding="utf-8") == "the reviewer's, not mine\n"
    assert "developer wrote 1 path(s) it may not write" in report.violations[-1]


def test_a_stray_write_the_role_committed_is_undone_too(git: ProjectGit, project: Path) -> None:
    """The reason the pass reads the stage's commits and not only `git status`.

    A role that knows how to use git can put its stray write beyond the reach of
    a worktree-only check by committing it, and a boundary only the careless
    trip over is not a boundary.
    """
    base = git.head()
    (project / "reports").mkdir()
    (project / "reports" / "round_00.md").write_text("committed, so `git status` is clean\n", encoding="utf-8")
    git.commit("feat: work the developer did")
    assert not git.changed(), "the stray write is invisible to a worktree-only check"

    reverts: list[str] = []
    report = _run(git, project, "developer", base, commit_revert=lambda: reverts.append(git.commit("revert: stray")))

    assert report.reverted is True
    assert report.stray == ("reports/round_00.md",)
    assert not (project / "reports" / "round_00.md").exists()
    assert len(reverts) == 1
    # Putting the base back removed the file before anything could be copied
    # aside, which is why the revert commit is what a person reads instead.
    assert report.quarantined == ()
    assert "developer wrote 1 path(s) it may not write" in report.violations[-1]


def test_without_a_stage_base_a_committed_stray_write_escapes(git: ProjectGit, project: Path) -> None:
    """The negative half of the rule above, so the reason for `stage_base` stays visible."""
    (project / "reports").mkdir()
    (project / "reports" / "round_00.md").write_text("committed\n", encoding="utf-8")
    git.commit("feat: work the developer did")

    report = _run(git, project, "developer", "")

    assert report.clean
    assert (project / "reports" / "round_00.md").exists()


def test_an_append_only_file_may_grow(git: ProjectGit, project: Path) -> None:
    base = git.head()
    (project / "NOTES.md").write_text("# Notes\n\nfirst line\nsecond line\n", encoding="utf-8")

    report = _run(git, project, "developer", base)

    assert report.clean
    assert "second line" in (project / "NOTES.md").read_text(encoding="utf-8")


def test_an_append_only_file_that_lost_a_line_is_put_back(git: ProjectGit, project: Path) -> None:
    base = git.head()
    (project / "NOTES.md").write_text("# Notes\n\nrewritten\n", encoding="utf-8")

    report = _run(git, project, "developer", base)

    assert report.trimmed == ("NOTES.md",)
    assert (project / "NOTES.md").read_text(encoding="utf-8") == "# Notes\n\nfirst line\n"
    assert report.violations[0] == "developer may only append to NOTES.md, and it removed lines"


def test_what_the_caller_wrote_before_the_role_started_is_not_the_role_s_doing(git: ProjectGit, project: Path) -> None:
    base = git.head()
    (project / "JOURNAL.md").write_text("## Round 00\n", encoding="utf-8")

    report = _run(git, project, "developer", base, allowed=["JOURNAL.md"])

    assert report.clean
    assert (project / "JOURNAL.md").exists()


def test_a_path_with_no_author_is_not_graded_by_ownership(git: ProjectGit, project: Path) -> None:
    """A build writes it, so whoever ran the build would be reverted for measuring."""
    base = git.head()
    (project / "build").mkdir()
    (project / "build" / "out.log").write_text("ran\n", encoding="utf-8")

    report = _run(git, project, "developer", base, artifacts=["build/**"])

    assert report.clean
    assert (project / "build" / "out.log").exists()


def test_a_grade_the_pass_does_not_know_is_refused_rather_than_guessed(git: ProjectGit, project: Path) -> None:
    base = git.head()
    (project / "reports").mkdir()
    (project / "reports" / "round_00.md").write_text("stray\n", encoding="utf-8")

    with pytest.raises(ValueError, match="is not a grade"):
        enforce(
            git,
            role="developer",
            grade=lambda _path: "maybe",
            quarantine=project.parent / "violations",
            stage_base=base,
        )
