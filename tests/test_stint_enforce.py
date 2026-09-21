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
    report = _run(
        git,
        project,
        "developer",
        base,
        commit_revert=lambda paths: reverts.append(git.commit("revert: stray", paths=paths)),
    )

    assert report.reverted is True
    assert report.stray == ("reports/round_00.md",)
    assert not (project / "reports" / "round_00.md").exists()
    assert len(reverts) == 1
    # Copied aside before the base was put back, so what is kept is the role's
    # version and not the base's -- which for a committed stray is "no file".
    assert report.quarantined == ("reports/round_00.md",)
    kept = project.parent / "violations" / "developer" / "reports" / "round_00.md"
    assert kept.read_text(encoding="utf-8") == "committed, so `git status` is clean\n"
    assert "developer wrote 1 path(s) it may not write" in report.violations[-1]


def test_a_mixed_stray_set_is_undone_whole_and_the_revert_commits_only_the_revert(
    git: ProjectGit, project: Path
) -> None:
    """One stray committed, one left untracked. The revert used to put the base
    back for both -- which for the untracked one meant a `git rm` that failed
    quietly -- then stage the whole tree, so the revert commit *added* the
    untracked stray and the pass that followed found it tracked and kept it.
    A boundary a role steps around by not committing is not one."""
    base = git.head()
    (project / "reports").mkdir()
    (project / "reports" / "round_00.md").write_text("committed\n", encoding="utf-8")
    git.commit("feat: the committed stray")
    (project / "reports" / "extra.md").write_text("left untracked\n", encoding="utf-8")
    (project / "src" / "main.py").write_text("print('mine')\n", encoding="utf-8")

    report = _run(
        git,
        project,
        "developer",
        base,
        commit_revert=lambda paths: git.commit("revert: stray", paths=paths),
    )

    assert sorted(report.stray) == ["reports/extra.md", "reports/round_00.md"]
    assert sorted(report.quarantined) == ["reports/extra.md", "reports/round_00.md"]
    assert not (project / "reports" / "round_00.md").exists()
    assert not (project / "reports" / "extra.md").exists()
    shown = git._run("show", "--stat", "--name-only", "--format=", "HEAD", check=False).stdout
    assert "reports/round_00.md" in shown
    assert "reports/extra.md" not in shown, "the revert commit carried the untracked stray"
    assert "src/main.py" not in shown, "the revert commit carried the role's own uncommitted work"
    # and what it owned is still as it left it
    assert (project / "src" / "main.py").read_text(encoding="utf-8") == "print('mine')\n"
    kept = project.parent / "violations" / "developer" / "reports"
    assert (kept / "extra.md").read_text(encoding="utf-8") == "left untracked\n"
    assert (kept / "round_00.md").read_text(encoding="utf-8") == "committed\n"


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
    assert report.violations[0].startswith("developer may only append to NOTES.md, and its version removed lines")


def test_appending_to_a_file_with_no_final_newline_is_still_an_append(git: ProjectGit, project: Path) -> None:
    """Git counts the last line as removed and added back when it gains its
    newline. A QA that appended two rows to a table ending that way was told
    it had removed lines, three times, and gave up on the file."""
    (project / "NOTES.md").write_text("# Notes\n\nfirst line", encoding="utf-8")
    git.commit("notes without a final newline")
    base = git.head()
    (project / "NOTES.md").write_text("# Notes\n\nfirst line\nsecond line\n", encoding="utf-8")

    report = _run(git, project, "developer", base)

    assert report.clean, report.violations
    assert (project / "NOTES.md").read_text(encoding="utf-8").endswith("second line\n")


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


class TestWhatWasAlreadyThere:
    """Paths the tree held before any role ran, and the host's own directory.

    The first role judged in a round has no earlier commit to be measured from,
    so it is graded against the whole dirty tree. Everything uncommitted at that
    moment therefore reads as its writes -- which in a real run meant the stint's
    own standing orders (`.stint/planner.md` and its siblings, written by the
    setup pass and not committed) were quarantined as the Planner's stray
    writes, and the round after could not read the file it was told to read.
    """

    def test_a_file_the_run_itself_left_is_not_the_role_s_stray_write(self, git: ProjectGit, project: Path) -> None:
        orders = project / ".stint"
        orders.mkdir()
        (orders / "planner.md").write_text("read this before planning\n", encoding="utf-8")

        report = _run(git, project, "developer", "", allowed=[".stint/planner.md"])

        assert report.stray == ()
        assert (orders / "planner.md").is_file(), "the round after this one has to read it"

    def test_the_same_file_unannounced_is_undone_as_it_always_was(self, git: ProjectGit, project: Path) -> None:
        """The guard above is a list the caller passes, not a rule about the
        path: a role really writing where it may not is still caught."""
        orders = project / ".stint"
        orders.mkdir()
        (orders / "planner.md").write_text("read this before planning\n", encoding="utf-8")

        report = _run(git, project, "developer", "")

        assert report.stray == (".stint/planner.md",)
        assert not (orders / "planner.md").exists()

    def test_the_host_s_own_state_is_nobody_s_write(self, git: ProjectGit, project: Path) -> None:
        """`.raven/shadow.git/` is this program's per-turn checkpoint, written
        into whatever directory it is working. It appeared in the tree with no
        role having touched it, and a whole shadow git repository was
        quarantined as the Planner's."""
        shadow = project / ".raven" / "shadow.git"
        shadow.mkdir(parents=True)
        (shadow / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")

        report = _run(git, project, "developer", "")

        assert report.stray == ()
        assert (shadow / "HEAD").is_file()
