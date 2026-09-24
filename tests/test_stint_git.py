"""The project's own repository: the run's branch, its commits, and undoing a stray write."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from raven.stint.git import _IGNORED, AUTHOR_EMAIL, AUTHOR_NAME, HistoryError, ProjectGit


@pytest.fixture
def project(tmp_path: Path) -> Path:
    (tmp_path / "project").mkdir()
    (tmp_path / "project" / "main.gd").write_text("func _ready():\n\tpass\n", encoding="utf-8")
    return tmp_path


@pytest.fixture
def instances(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Somewhere to put a second checkout that is not inside the project itself."""
    return tmp_path_factory.mktemp("instances")


def _log(project: Path) -> list[str]:
    completed = subprocess.run(
        ["git", "-C", str(project), "log", "--oneline"], capture_output=True, text=True, check=True
    )
    return completed.stdout.splitlines()


def test_a_project_without_a_repository_gets_one(project: Path) -> None:
    git = ProjectGit(project)

    created = git.ensure_repo()

    assert created is True
    assert (project / ".git").is_dir()
    assert len(_log(project)) == 1
    assert not git.dirty()


def test_an_existing_repository_is_left_alone(project: Path) -> None:
    git = ProjectGit(project)
    git.ensure_repo()

    assert git.ensure_repo() is False


def test_the_run_works_on_its_own_branch(project: Path) -> None:
    git = ProjectGit(project)
    git.ensure_repo()
    original = git.branch()

    branch = git.checkout_branch("rounds/run-1")

    assert branch == "rounds/run-1"
    assert branch != original
    assert git.checkout_branch("rounds/run-1") == "rounds/run-1"


def test_a_commit_records_the_tree_and_an_empty_one_is_not_an_error(project: Path) -> None:
    git = ProjectGit(project)
    git.ensure_repo()
    base = git.head()

    (project / "project" / "arena.gd").write_text("# the arena\n", encoding="utf-8")
    head = git.commit("feat(round-01): the arena")

    assert head != base
    entries = git.log_since(base)
    assert len(entries) == 1 and entries[0].endswith("feat(round-01): the arena")
    assert git.commit("chore: nothing changed") == head


def test_a_stray_untracked_file_is_moved_aside_rather_than_deleted(project: Path) -> None:
    git = ProjectGit(project)
    git.ensure_repo()
    (project / "planner_was_here.txt").write_text("oops\n", encoding="utf-8")
    quarantine = project.parent / "violations"

    undone = git.restore(["planner_was_here.txt"], quarantine=quarantine)

    assert undone == ("planner_was_here.txt",)
    assert not (project / "planner_was_here.txt").exists()
    assert (quarantine / "planner_was_here.txt").read_text(encoding="utf-8") == "oops\n"


def test_a_stray_edit_to_a_tracked_file_goes_back_to_head(project: Path) -> None:
    git = ProjectGit(project)
    git.ensure_repo()
    (project / "project" / "main.gd").write_text("# changed by a role that may not write\n", encoding="utf-8")

    git.restore(["project/main.gd"])

    assert (project / "project" / "main.gd").read_text(encoding="utf-8") == "func _ready():\n\tpass\n"
    assert not git.dirty()


def test_a_stray_edit_to_a_tracked_file_is_kept_before_it_is_undone(project: Path) -> None:
    """Measured 2026-09-10: a Builder's edit to `docs/EVENTS.md` was reverted,
    the log said a copy was kept under `violations/builder`, and the directory
    was empty -- only untracked files were ever kept. An edit to a file that
    already exists is the case where "what did it try to change" is the whole
    question, so it is the one that most needs keeping."""
    git = ProjectGit(project)
    git.ensure_repo()
    (project / "project" / "main.gd").write_text("# what the role wanted\n", encoding="utf-8")
    quarantine = project.parent / "violations"

    undone = git.restore(["project/main.gd"], quarantine=quarantine)

    assert undone == ("project/main.gd",)
    assert (project / "project" / "main.gd").read_text(encoding="utf-8") == "func _ready():\n\tpass\n"
    assert (quarantine / "project" / "main.gd").read_text(encoding="utf-8") == "# what the role wanted\n"


def test_keeping_a_copy_is_optional_and_the_revert_is_not(project: Path) -> None:
    """A caller with nowhere to put the copy still gets the boundary enforced."""
    git = ProjectGit(project)
    git.ensure_repo()
    (project / "project" / "main.gd").write_text("# stray\n", encoding="utf-8")

    git.restore(["project/main.gd"])

    assert (project / "project" / "main.gd").read_text(encoding="utf-8") == "func _ready():\n\tpass\n"
    assert not git.dirty()


def test_a_read_only_path_is_restored_from_the_stage_base(project: Path) -> None:
    git = ProjectGit(project)
    git.ensure_repo()
    (project / "refs").mkdir()
    (project / "refs" / "brief.md").write_text("the brief\n", encoding="utf-8")
    base = git.commit("chore: the brief")

    (project / "refs" / "brief.md").write_text("edited by the builder\n", encoding="utf-8")
    (project / "refs" / "new.md").write_text("added by the builder\n", encoding="utf-8")
    git.commit("feat(round-01): work that touched a read-only path")

    assert set(git.diff_names(base)) == {"refs/brief.md", "refs/new.md"}
    git.restore_from(base, ["refs/brief.md", "refs/new.md"])

    assert (project / "refs" / "brief.md").read_text(encoding="utf-8") == "the brief\n"
    assert not (project / "refs" / "new.md").exists()


def test_changed_lists_untracked_and_modified_paths(project: Path) -> None:
    git = ProjectGit(project)
    git.ensure_repo()
    (project / "project" / "main.gd").write_text("# edited\n", encoding="utf-8")
    (project / "demo_outputs").mkdir()
    (project / "demo_outputs" / "boot.log").write_text("ok\n", encoding="utf-8")

    assert set(git.changed()) == {"project/main.gd", "demo_outputs/boot.log"}


def _commit_by_hand(tree: Path, message: str) -> None:
    for args in (["add", "--all"], ["commit", "--quiet", "--no-verify", "-m", message]):
        subprocess.run(["git", "-C", str(tree), *args], capture_output=True, text=True, check=True)


def test_whether_git_is_on_the_path_is_answerable_before_anything_needs_it(project: Path, monkeypatch) -> None:
    """Everything a stint remembers between rounds is in the commit log, so a
    machine with no git is refused up front rather than mid-round."""
    git = ProjectGit(project)

    assert git.available is True
    monkeypatch.setattr("raven.stint.git.shutil.which", lambda name: None)
    assert git.available is False


def test_a_git_command_that_fails_carries_gits_own_words_out(project: Path) -> None:
    """A bare `the run could not check out a branch` is not something a person can act on."""
    git = ProjectGit(project)
    git.ensure_repo()

    with pytest.raises(HistoryError) as caught:
        git.checkout_branch("rounds/a name git will not take")

    assert str(caught.value).startswith("git checkout")
    assert "not a valid branch name" in str(caught.value)


def test_a_projects_own_gitignore_is_added_to_rather_than_replaced(project: Path) -> None:
    (project / ".gitignore").write_text("*.tmp", encoding="utf-8")

    ProjectGit(project).ensure_repo()

    text = (project / ".gitignore").read_text(encoding="utf-8")
    assert text.startswith("*.tmp\n")
    for pattern in _IGNORED:
        assert f"\n{pattern}\n" in text


def test_a_gitignore_that_already_covers_everything_is_left_byte_for_byte(project: Path) -> None:
    body = "".join(f"{pattern}\n" for pattern in _IGNORED)
    (project / ".gitignore").write_text(body, encoding="utf-8")

    ProjectGit(project).ensure_repo()

    assert (project / ".gitignore").read_text(encoding="utf-8") == body


def test_a_second_instance_gets_its_own_checkout_of_the_same_history(project: Path, instances: Path) -> None:
    """Two Developers cannot share one working tree: each writes files the other
    is mid-read of, and `git status` stops meaning "what this role changed"."""
    git = ProjectGit(project)
    git.ensure_repo()
    base = git.head()

    tree = git.worktree_add(instances / "dev-a", "rounds/dev-a", base)

    assert (tree / "project" / "main.gd").is_file()
    assert ProjectGit(tree).branch() == "rounds/dev-a"
    assert not git.dirty(), "the instance's checkout is not a change to the project's own tree"


def test_a_worktree_can_leave_the_heavy_directories_unwritten(project: Path, instances: Path) -> None:
    """A worktree is a full second checkout, so on a project carrying tens of
    gigabytes of archived evidence three instances cost a hundred gigabytes of
    copying a round. The excluded path stays in the history, only unwritten."""
    (project / "assets_raw").mkdir()
    (project / "assets_raw" / "source.blend").write_text("a large thing\n", encoding="utf-8")
    git = ProjectGit(project)
    git.ensure_repo()
    base = git.head()

    tree = git.worktree_add(instances / "dev-a", "rounds/dev-a", base, exclude=["assets_raw"])

    assert (tree / "project" / "main.gd").is_file()
    assert not (tree / "assets_raw").exists()
    listed = subprocess.run(
        ["git", "-C", str(tree), "ls-tree", "-r", "--name-only", "HEAD"], capture_output=True, text=True, check=True
    ).stdout.split()
    assert "assets_raw/source.blend" in listed
    assert not ProjectGit(tree).dirty(), "an unwritten path must not read as a deletion"


def test_a_branch_a_killed_run_left_held_is_taken_back(project: Path, instances: Path) -> None:
    """`git branch --force` refuses while a worktree holds the name, and a run
    killed mid-round leaves one registered (2026-09-15)."""
    git = ProjectGit(project)
    git.ensure_repo()
    base = git.head()
    first = git.worktree_add(instances / "dev-a", "rounds/dev-a", base)
    assert [path.resolve() for path in git.worktrees_on("rounds/dev-a")] == [first.resolve()]

    second = git.worktree_add(instances / "dev-b", "rounds/dev-a", base)

    assert [path.resolve() for path in git.worktrees_on("rounds/dev-a")] == [second.resolve()]
    assert not first.exists()


def test_a_directory_left_where_a_worktree_was_does_not_block_the_next_one(project: Path, instances: Path) -> None:
    """`worktree add` refuses a path that exists, and a run killed between the
    remove and the prune leaves exactly that."""
    git = ProjectGit(project)
    git.ensure_repo()
    base = git.head()
    stale = instances / "dev-a"
    stale.mkdir()
    (stale / "leftover.txt").write_text("from a run that was killed\n", encoding="utf-8")

    tree = git.worktree_add(stale, "rounds/dev-a", base)

    assert tree == stale
    assert not (stale / "leftover.txt").exists()
    assert (stale / "project" / "main.gd").is_file()


def test_a_role_that_commits_by_itself_still_commits_as_the_run(project: Path, instances: Path) -> None:
    """Given a shell a role runs `git commit` itself, and then the history carries
    whoever the machine's git is configured as -- measured 2026-09-17, six commits
    attributed to the person who started the run and wrote none of them."""
    git = ProjectGit(project)
    git.ensure_repo()
    tree = git.worktree_add(instances / "dev-a", "rounds/dev-a", git.head())
    (tree / "project" / "arena.gd").write_text("# the arena\n", encoding="utf-8")

    _commit_by_hand(tree, "feat: the arena, committed by the role")

    who = subprocess.run(
        ["git", "-C", str(tree), "log", "-1", "--format=%an <%ae>"], capture_output=True, text=True, check=True
    ).stdout.strip()
    assert who == f"{AUTHOR_NAME} <{AUTHOR_EMAIL}>"


def test_an_instances_branch_merges_back_when_nothing_clashes(project: Path, instances: Path) -> None:
    git = ProjectGit(project)
    git.ensure_repo()
    tree = git.worktree_add(instances / "dev-a", "rounds/dev-a", git.head())
    (tree / "project" / "arena.gd").write_text("# the arena\n", encoding="utf-8")
    _commit_by_hand(tree, "feat: the arena")

    took, clashing = git.merge_branch("rounds/dev-a", "merge dev-a")

    assert took is True
    assert clashing == ()
    assert (project / "project" / "arena.gd").read_text(encoding="utf-8") == "# the arena\n"


def test_a_merge_takes_on_a_machine_with_no_git_identity_of_its_own(
    project: Path, instances: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A merge writes a commit, and an unattended box has nobody to attribute it to.

    A container, a CI runner and a fresh devbox all run git with no configured
    `user.email`, which git refuses a commit for. The round read that refusal
    as a merge that would not take, named no clashing file, and left the
    instance's work on its branch.
    """
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", os.devnull)
    git = ProjectGit(project)
    git.ensure_repo()
    tree = git.worktree_add(instances / "dev-a", "rounds/dev-a", git.head())
    (tree / "project" / "arena.gd").write_text("# the arena\n", encoding="utf-8")
    _commit_by_hand(tree, "feat: the arena")

    took, clashing = git.merge_branch("rounds/dev-a", "merge dev-a")

    assert (took, clashing) == (True, ())
    assert (project / "project" / "arena.gd").is_file()


def test_two_instances_that_wrote_the_same_file_leave_it_to_be_merged_by_hand(project: Path, instances: Path) -> None:
    """Resolving code conflicts unattended is how a tree ends up committed and broken."""
    git = ProjectGit(project)
    git.ensure_repo()
    tree = git.worktree_add(instances / "dev-a", "rounds/dev-a", git.head())
    (tree / "project" / "main.gd").write_text("# what the second instance wrote\n", encoding="utf-8")
    _commit_by_hand(tree, "feat: the instance's version")
    (project / "project" / "main.gd").write_text("# what the first instance wrote\n", encoding="utf-8")
    git.commit("feat: the other version")

    took, clashing = git.merge_branch("rounds/dev-a", "merge dev-a")

    assert took is False
    assert clashing == ("project/main.gd",)
    assert not git.dirty(), "the merge was aborted rather than left half-done"
    assert git.worktrees_on("rounds/dev-a"), "the branch is still there to merge by hand"


def test_a_merge_refused_for_anything_else_reports_what_git_said(project: Path, instances: Path) -> None:
    """No unmerged paths means it was not a content conflict -- a dirty tree, an
    unrelated history, a hook -- and "conflicting history" would name none of them."""
    git = ProjectGit(project)
    git.ensure_repo()
    tree = git.worktree_add(instances / "dev-a", "rounds/dev-a", git.head())
    (tree / "project" / "arena.gd").write_text("# the arena\n", encoding="utf-8")
    _commit_by_hand(tree, "feat: the arena")
    (project / "project" / "arena.gd").write_text("# uncommitted, in the way\n", encoding="utf-8")

    took, said = git.merge_branch("rounds/dev-a", "merge dev-a")

    assert took is False
    assert len(said) == 1
    assert "project/arena.gd" in said[0]
    assert (project / "project" / "arena.gd").read_text(encoding="utf-8") == "# uncommitted, in the way\n"


def test_a_commit_is_named_by_the_first_nine_characters_of_its_sha(project: Path) -> None:
    git = ProjectGit(project)
    git.ensure_repo()

    assert git.short() == git.head()[:9]
    assert git.short("0123456789abcdef") == "012345678"


def test_a_renamed_file_is_reported_under_the_name_it_now_has(project: Path) -> None:
    """The enforcement pass grades the path a role wrote, and after a rename that
    is the new name; grading the old one lets a role move a file it may not touch."""
    git = ProjectGit(project)
    git.ensure_repo()
    subprocess.run(
        ["git", "-C", str(project), "mv", "project/main.gd", "project/arena.gd"], capture_output=True, check=True
    )

    assert git.changed() == ("project/arena.gd",)


def test_a_turn_that_rewrites_a_file_it_already_created_is_visible(project: Path) -> None:
    """`changed()` answers with names alone, so a Builder spending a second turn
    filling in a thirty-kilobyte report looked exactly like one doing nothing, and
    the stall detector ended its round saying it changed nothing in the tree."""
    git = ProjectGit(project)
    git.ensure_repo()
    report = project / "report.md"
    report.write_text("half of it\n", encoding="utf-8")
    before = git.footprint()

    report.write_text("half of it, and then the rest of it\n", encoding="utf-8")
    after = git.footprint()

    assert git.changed() == ("report.md",)
    assert [row[0] for row in after] == ["report.md"]
    assert after != before
    assert after[0][1] > before[0][1]


def test_a_path_git_reports_but_nothing_can_stat_still_counts_as_a_change(project: Path) -> None:
    """A dangling symlink is a write, and a footprint that raised on one would
    end the round instead of recording it."""
    git = ProjectGit(project)
    git.ensure_repo()
    (project / "dangling").symlink_to(project / "never-written")

    assert ("dangling", -1, 0) in git.footprint()


def test_asking_what_happened_since_nowhere_answers_nothing(project: Path) -> None:
    """The first stage of the first round has no base to compare against."""
    git = ProjectGit(project)
    git.ensure_repo()
    (project / "project" / "main.gd").write_text("# edited\n", encoding="utf-8")

    assert git.log_since("") == ()
    assert git.diff_names("") == ()
    assert git.touched_since("") == ("project/main.gd",)


def test_a_binary_file_git_will_not_count_lines_for_is_not_read_as_a_deletion(project: Path) -> None:
    """`--numstat` prints a dash for a binary file, and an append-only path holding
    a screenshot would otherwise take the whole run down on the first replacement."""
    git = ProjectGit(project)
    git.ensure_repo()
    (project / "shot.png").write_bytes(b"\x89PNG\r\n\x1a\n" + bytes(range(256)))
    base = git.commit("chore: a screenshot")
    (project / "shot.png").write_bytes(b"\x89PNG\r\n\x1a\n" + bytes(range(255, -1, -1)))

    assert git.deletions_since(base, "shot.png") == 0


def test_committing_where_there_is_no_repository_is_an_error(project: Path) -> None:
    """The empty commit is the case allowed to pass quietly; a broken one is not."""
    with pytest.raises(HistoryError) as caught:
        ProjectGit(project).commit("chore: nowhere to put this")

    assert str(caught.value).startswith("git commit")


def test_a_keepsake_that_cannot_be_written_does_not_stop_the_revert(project: Path, instances: Path) -> None:
    """The boundary is the point and the copy is the courtesy."""
    git = ProjectGit(project)
    git.ensure_repo()
    (project / "project" / "main.gd").write_text("# what the role wanted\n", encoding="utf-8")
    blocked = instances / "quarantine-that-is-a-file"
    blocked.write_text("not a directory\n", encoding="utf-8")

    undone = git.restore(["project/main.gd"], quarantine=blocked)

    assert undone == ("project/main.gd",)
    assert (project / "project" / "main.gd").read_text(encoding="utf-8") == "func _ready():\n\tpass\n"


def test_a_stray_directory_is_removed_when_there_is_nowhere_to_keep_it(project: Path) -> None:
    git = ProjectGit(project)
    git.ensure_repo()
    (project / "scratch").mkdir()
    (project / "scratch" / "notes.md").write_text("working it out\n", encoding="utf-8")
    (project / "stray.txt").write_text("oops\n", encoding="utf-8")

    undone = git.restore(["scratch", "stray.txt", "never-written.txt"])

    assert undone == ("scratch", "stray.txt")
    assert not (project / "scratch").exists()
    assert not (project / "stray.txt").exists()
    assert not git.dirty()


#: A filename git escapes under its default ``core.quotePath``: every byte >=
#: 0x80 comes back as an octal run. Written as escapes to keep this source
#: ASCII; on disk it is an ordinary name a role could plausibly write.
_QUOTED_NAME = "\u62a5\u544a.md"  # "report" in Chinese


def test_a_stray_write_to_a_non_ascii_path_is_still_undone(project: Path) -> None:
    """The boundary holds for a name git would rather print in octal.

    With git's default quoting the name that comes back is not the name on
    disk, and every step downstream reads the difference as "no such file": it
    matches no ownership glob, `restore` finds nothing to put back, and the
    write is not even recorded as a violation. A role that wrote a CJK filename
    walked through the fourth layer untouched -- and this product is used in
    Chinese, so that is not an exotic path.
    """
    git = ProjectGit(project)
    git.ensure_repo()

    (project / _QUOTED_NAME).write_text("a role wrote this and does not own it\n", encoding="utf-8")

    assert git.changed() == (_QUOTED_NAME,)
    assert git.restore(git.changed()) == (_QUOTED_NAME,)
    assert not (project / _QUOTED_NAME).exists()


def test_a_non_ascii_path_is_stattable_in_the_footprint(project: Path) -> None:
    """Its size, not the ``-1`` an unreadable name produces.

    ``footprint`` stats what ``changed`` names, so an escaped name stats
    nothing and reports ``-1`` -- which the stall detector reads as a file that
    exists but cannot be measured, rather than as work the role did.
    """
    git = ProjectGit(project)
    git.ensure_repo()
    body = "work this role really did\n"
    (project / _QUOTED_NAME).write_text(body, encoding="utf-8")

    rows = {name: size for name, size, _mtime in git.footprint()}

    assert rows[_QUOTED_NAME] == len(body.encode("utf-8"))


def test_the_host_s_own_directory_is_not_part_of_the_tree(tmp_path: Path) -> None:
    """`.raven/shadow.git/` is the per-turn checkpoint, written into whatever
    directory the host is working. On the isolation that runs in the project
    itself that is the tree a round commits, and a whole second git repository
    went in under `round(01): planner` -- 25 files of hooks and config.
    """
    project = tmp_path / "project"
    (project / ".raven" / "shadow.git" / "hooks").mkdir(parents=True)
    (project / ".raven" / "shadow.git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    (project / "src").mkdir()
    (project / "src" / "main.py").write_text("print('hi')\n", encoding="utf-8")
    git = ProjectGit(project)
    git.ensure_repo()

    assert git.changed() == (), "an untracked host directory would make the tree dirty forever"
    assert ".raven" not in git._run("ls-files").stdout, "it must not be staged either"
    assert (project / ".raven" / "shadow.git" / "HEAD").is_file(), "and it must still be there"


def test_the_project_s_own_files_are_still_seen(tmp_path: Path) -> None:
    """The exclusion is one directory, not a habit of ignoring things."""
    project = tmp_path / "project"
    project.mkdir()
    (project / "NOTES.md").write_text("hello\n", encoding="utf-8")
    git = ProjectGit(project)
    git.ensure_repo()
    (project / "NOTES.md").write_text("hello again\n", encoding="utf-8")
    (project / ".raven").mkdir()
    (project / ".raven" / "junk").write_text("x\n", encoding="utf-8")

    assert git.changed() == ("NOTES.md",)


def test_a_path_the_base_carries_is_known_there_and_a_later_one_is_not(project: Path) -> None:
    git = ProjectGit(project)
    git.ensure_repo()
    base = git.head()
    (project / "later.md").write_text("after the base\n", encoding="utf-8")
    git.commit("later")

    assert git.known_at(base, "project/main.gd")
    assert not git.known_at(base, "later.md")
    assert not git.known_at("", "project/main.gd"), "no base knows nothing"


def test_build_litter_stays_out_of_a_commit_whatever_the_ignore_file_says(project: Path) -> None:
    """The ignore file is written only for a repository this class created. A
    project the person initialised keeps theirs, and a round's commit carried
    `src/__pycache__/*.pyc` for having run the code it had just written."""
    git = ProjectGit(project)
    git._run("init", "--quiet")
    (project / "src").mkdir()
    (project / "src" / "a.py").write_text("x = 1\n", encoding="utf-8")
    (project / "src" / "__pycache__").mkdir()
    (project / "src" / "__pycache__" / "a.pyc").write_bytes(b"\x00")
    (project / "node_modules" / "left-pad").mkdir(parents=True)
    (project / "node_modules" / "left-pad" / "index.js").write_text("", encoding="utf-8")

    git.commit("round(01): builder")

    listed = git._run("ls-files", check=False).stdout.split()
    assert "src/a.py" in listed and "project/main.gd" in listed
    assert not any("__pycache__" in path or "node_modules" in path for path in listed), listed
