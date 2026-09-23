"""The project's own git repository, which is where a run's history lives.

Two things carry knowledge between rounds: the files a role writes and the
commit log. Nothing else -- no shadow snapshot store, no frozen copies of the
tree. Each role's work is its own commit, so "what happened in round 7" is
answered by ``git log`` in the project a person already has open.

A run works on a branch of its own so an autonomous role never commits onto
whatever the humans were using.

Two ways of asking what a stage touched, and they are not interchangeable:
``changed`` reads the worktree, ``touched_since`` reads the stage's own commits
as well. A role that commits its stray write vanishes from the first.
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

_IGNORED = (
    ".venv/",
    "node_modules/",
    "__pycache__/",
    ".godot/",
    ".import/",
)

HOST_STATE = ".raven"
"""Where the host keeps its own state inside a project it is working.

Not part of the project's tree as far as this class is concerned: not staged,
not reported as a change, and so never graded and never committed. The per-turn
checkpoint (``agent/loop/checkpoint.py``) writes ``.raven/shadow.git/`` into
whatever directory the host is working, which on the isolation that runs in the
project itself is the tree a round commits. Left in, a whole second git
repository went into a round's commit -- 25 files of hooks and config under
`round(01): planner` -- and `dirty()` was true forever after, because an
untracked directory nobody would ever commit never stops being untracked.

Enforced here rather than by writing the person a `.gitignore`: their ignore
file is theirs, and a run that edits it to make its own bookkeeping invisible
has changed the project to suit itself."""

AUTHOR_NAME = "raven-rounds"
AUTHOR_EMAIL = "rounds@localhost"


class HistoryError(RuntimeError):
    """A git operation the run depends on failed."""


@dataclass
class ProjectGit:
    """Commits in the project itself, on a branch the run owns."""

    work_tree: Path
    author_name: str = AUTHOR_NAME
    author_email: str = AUTHOR_EMAIL

    def _run(self, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        # quotePath off for every call, not just the two that parse paths today.
        # With git's default on, any byte >= 0x80 comes back octal-escaped --
        # `cafe.md` with an accent reads as `caf\303\251.md` -- and a name that
        # is not the name on disk matches no ownership glob, reverts to nothing,
        # and is not even reported as a violation. A role writing a CJK filename
        # walked straight through the boundary. Set here because the next call
        # site to parse a path would otherwise have to remember this.
        argv = ["git", "-c", "core.quotePath=false", "-C", str(self.work_tree), *args]
        completed = subprocess.run(
            argv,  # noqa: S607 -- git location varies; PATH lookup intended
            capture_output=True,
            text=True,
        )
        if check and completed.returncode != 0:
            raise HistoryError(f"git {' '.join(args)}: {completed.stderr.strip() or completed.stdout.strip()}")
        return completed

    @property
    def available(self) -> bool:
        return shutil.which("git") is not None

    @property
    def initialized(self) -> bool:
        return (self.work_tree / ".git").exists()

    def ensure_repo(self) -> bool:
        """A repository to commit into, created only if the project has none."""
        if self.initialized:
            return False
        self._run("init", "--quiet")
        self._write_ignore()
        self._stage_all()
        self.commit("chore: the tree this run started from")
        return True

    def _write_ignore(self) -> None:
        path = self.work_tree / ".gitignore"
        existing = path.read_text(encoding="utf-8") if path.is_file() else ""
        missing = [pattern for pattern in _IGNORED if pattern not in existing]
        if missing:
            body = existing + ("\n" if existing and not existing.endswith("\n") else "")
            path.write_text(body + "\n".join(missing) + "\n", encoding="utf-8")

    def _stage_all(self) -> None:
        """Everything in the tree except the host's own state and the build litter.

        One method because there were two ``add --all`` call sites and only one
        of them was given the exclusion, which is exactly how a shadow git
        repository reached a round's commit through the other.

        ``_IGNORED`` is excluded here as well as written to the ignore file,
        because the file is written only for a repository this class created:
        a project the person initialised keeps their ignore file as it is, and
        a round's commit then carried ``src/__pycache__/*.pyc`` for having run
        the code it had just written.
        """
        litter = [f":(exclude,glob)**/{name.strip('/')}/**" for name in _IGNORED]
        self._run("add", "--all", "--", ".", f":(exclude){HOST_STATE}", *litter, check=False)

    def checkout_branch(self, name: str) -> str:
        """The run's branch, cut from wherever the project is now."""
        known = self._run("rev-parse", "--verify", "--quiet", f"refs/heads/{name}", check=False)
        if known.returncode == 0:
            self._run("checkout", "--quiet", name)
        else:
            self._run("checkout", "--quiet", "-b", name)
        return self.branch()

    def branch(self) -> str:
        return self._run("rev-parse", "--abbrev-ref", "HEAD", check=False).stdout.strip()

    def worktree_add(self, path: Path, branch: str, base: str, exclude: Sequence[str] = ()) -> Path:
        """A second checkout of this project, on a branch of its own.

        Two Developers cannot share one working tree: each writes files the
        other is mid-read of, and `git status` stops meaning "what this role
        changed". A worktree gives each its own files and its own HEAD while
        the object store -- and so the history they merge back into -- stays
        one.
        """
        path = Path(path)
        self.worktree_remove(path)
        # The branch can still be held by a worktree elsewhere: a run killed
        # mid-round leaves one registered, and `git branch --force` refuses
        # while anything holds the name. Drop the holder before taking it.
        for held in self.worktrees_on(branch):
            self.worktree_remove(held)
        self._run("branch", "--force", branch, base or "HEAD")
        if not exclude:
            self._run("worktree", "add", "--quiet", str(path), branch)
            self._claim_identity(path)
            return path
        # Top-level directories a role never needs materialised. A worktree is a
        # full second checkout, so on a project carrying tens of gigabytes of
        # archived evidence and source assets, three instances cost a hundred
        # gigabytes of copying a round. Excluded paths stay in the history and
        # in the object store; only the files are left unwritten, and a role
        # that creates new files under such a path still can.
        self._run("worktree", "add", "--quiet", "--no-checkout", str(path), branch)
        here = ProjectGit(path, author_name=self.author_name, author_email=self.author_email)
        patterns = ["/*"] + [f"!/{name.strip('/')}/" for name in exclude]
        here._run("sparse-checkout", "set", "--no-cone", *patterns)
        here._run("checkout", "--quiet")
        self._claim_identity(path)
        return path

    def _claim_identity(self, path: Path) -> None:
        """Whose name goes on a commit made in this checkout, by anyone.

        ``commit`` sets an author for the commits this module makes. A role does
        not have to go through it: given a shell it will run ``git commit``
        itself, and then the history carries whoever the machine's git is
        configured as -- measured 2026-09-17, six commits attributed to the
        person who started the plan and wrote none of them.

        Set per worktree, which needs saying: a linked worktree shares the
        repository's ``config``, so a plain ``git -C <tree> config user.name``
        renames the person in their own tree as well (measured). ``--worktree``
        writes to the checkout's own file instead, and needs the repository-level
        extension turned on first -- one benign line in the repository's config,
        which changes nothing for a worktree that does not override.
        """
        here = ProjectGit(path, author_name=self.author_name, author_email=self.author_email)
        if self._run("config", "extensions.worktreeConfig", "true", check=False).returncode:
            return
        here._run("config", "--worktree", "user.name", self.author_name, check=False)
        here._run("config", "--worktree", "user.email", self.author_email, check=False)

    def worktrees_on(self, branch: str) -> tuple[Path, ...]:
        """The worktrees git says are holding a branch right now."""
        listed = self._run("worktree", "list", "--porcelain", check=False).stdout
        found: list[Path] = []
        current = ""
        for line in listed.splitlines():
            if line.startswith("worktree "):
                current = line[len("worktree ") :].strip()
            elif line.strip() == f"branch refs/heads/{branch}" and current:
                found.append(Path(current))
        return tuple(found)

    def worktree_remove(self, path: Path) -> None:
        """Drop a worktree and forget it, leaving its branch behind.

        The branch outlives the worktree on purpose: a merge that conflicted
        leaves its work reachable by name rather than only by reflog.
        """
        # Unlock first: a locked worktree refuses `remove --force`, and the
        # refusal is silent here -- which is how a killed run left one behind
        # and the next round could not take its branch back (2026-09-15).
        self._run("worktree", "unlock", str(path), check=False)
        self._run("worktree", "remove", "--force", str(path), check=False)
        self._run("worktree", "prune", check=False)
        # A directory git no longer counts as a worktree it will not remove, and
        # `worktree add` refuses a path that exists. Left behind by a run killed
        # between the two steps.
        if Path(path).exists():
            shutil.rmtree(path, ignore_errors=True)

    def merge_branch(self, branch: str, message: str) -> tuple[bool, tuple[str, ...]]:
        """Merge one instance's branch back. Returns whether it took, and what clashed.

        A conflict aborts rather than leaving the tree half-merged: the round
        can report which files two instances both claimed, and the branch is
        still there to merge by hand. Resolving code conflicts unattended is
        how a tree ends up committed and broken.
        """
        # The run's identity, for the same reason `commit` names one: a merge
        # writes a commit, and a machine with no ambient `user.email` -- a
        # container, a CI runner, a fresh box -- refuses one it cannot
        # attribute. Without this the round reports that refusal as a merge
        # that would not take, naming no clashing file, which is not what
        # happened.
        merged = self._run(
            "-c",
            f"user.name={self.author_name}",
            "-c",
            f"user.email={self.author_email}",
            "merge",
            "--no-ff",
            "--no-edit",
            "-m",
            message,
            branch,
            check=False,
        )
        if merged.returncode == 0:
            return True, ()
        clashing = self._run("diff", "--name-only", "--diff-filter=U", check=False).stdout.split()
        self._run("merge", "--abort", check=False)
        if clashing:
            return False, tuple(clashing)
        # No unmerged paths, so the refusal was not a content conflict -- a dirty
        # tree, an unrelated history, a hook. Carrying git's own words out is the
        # difference between a report someone can act on and "conflicting history".
        said = " ".join((merged.stderr or merged.stdout or "").split())[:300]
        return False, (said or "git refused the merge and said nothing",)

    def head(self) -> str:
        completed = self._run("rev-parse", "HEAD", check=False)
        return completed.stdout.strip() if completed.returncode == 0 else ""

    def short(self, sha: str = "") -> str:
        return (sha or self.head())[:9]

    def dirty(self) -> bool:
        return bool(self.changed())

    def changed(self) -> tuple[str, ...]:
        """Every path git reports as changed, staged, or untracked."""
        completed = self._run("status", "--porcelain", "--untracked-files=all", check=False)
        paths: list[str] = []
        for line in completed.stdout.splitlines():
            if len(line) < 4:
                continue
            path = line[3:].strip()
            if " -> " in path:
                path = path.split(" -> ", 1)[1]
            path = path.strip('"')
            if path == HOST_STATE or path.startswith(f"{HOST_STATE}/"):
                continue
            paths.append(path)
        return tuple(paths)

    def footprint(self) -> tuple[tuple[str, int, int], ...]:
        """What the working tree looks like now, content included.

        `changed()` answers with names alone, and a turn that rewrites a file it
        already created moves no name: a Builder spending two turns filling in
        a thirty-kilobyte report looked exactly like one doing nothing, and the
        stall detector ended its round saying it changed nothing in the tree.
        Size and mtime are what make an in-place edit visible, and both come
        from a stat of the handful of paths git already reported.
        """
        rows: list[tuple[str, int, int]] = []
        for path in self.changed():
            whole = self.work_tree / path
            try:
                info = whole.stat()
            except OSError:
                rows.append((path, -1, 0))
                continue
            rows.append((path, int(info.st_size), int(info.st_mtime_ns)))
        return tuple(sorted(rows))

    def commit(self, message: str, author: str = "", paths: Sequence[str] = ()) -> str:
        """Everything in the tree as one commit; the empty case is not an error.

        ``author`` is the role whose stage this is, and it goes in as the commit
        author so ``git log --author`` and ``git blame`` answer "which role wrote
        this" without a reader having to map commit subjects back to stages. The
        whole ownership design is about telling the three apart; a history where
        all three are one name throws that away at the last step.

        ``paths`` narrows the commit to those paths and nothing else. The revert
        the boundary pass makes is the caller: staging the whole tree there
        committed the untracked stray it was meant to undo, and a boundary a role
        steps around by leaving a file untracked is not one.
        """
        if paths:
            self._run("reset", "--quiet", "--", ".", check=False)
            for path in paths:
                self._run("add", "--all", "--", path, check=False)
        else:
            self._stage_all()
        name = f"{self.author_name} ({author})" if author else self.author_name
        email = f"{author}@{self.author_email.partition('@')[2]}" if author else self.author_email
        completed = self._run(
            "-c",
            f"user.name={name}",
            "-c",
            f"user.email={email}",
            "commit",
            "--quiet",
            "--no-verify",
            "-m",
            message,
            check=False,
        )
        if completed.returncode != 0 and "nothing to commit" not in (completed.stdout + completed.stderr):
            raise HistoryError(f"git commit: {completed.stderr.strip() or completed.stdout.strip()}")
        return self.head()

    def log_since(self, sha: str, limit: int = 20) -> tuple[str, ...]:
        if not sha:
            return ()
        completed = self._run("log", "--oneline", f"{sha}..HEAD", f"-{limit}", check=False)
        return tuple(line.strip() for line in completed.stdout.splitlines() if line.strip())

    def diff_names(self, base: str) -> tuple[str, ...]:
        """Paths that differ between ``base`` and HEAD, which is a stage's footprint."""
        if not base:
            return ()
        completed = self._run("diff", "--name-only", f"{base}..HEAD", check=False)
        return tuple(line.strip() for line in completed.stdout.splitlines() if line.strip())

    def touched_since(self, base: str) -> tuple[str, ...]:
        """Every path a stage touched: committed since ``base``, plus uncommitted.

        Two sources because a role can reach the tree by either road. ``status``
        alone was the hole: a role that committed its own stray write vanished
        from it, and the enforcement pass then found nothing to undo.
        """
        return tuple(dict.fromkeys((*self.diff_names(base), *self.changed())))

    def deletions_since(self, base: str, path: str) -> int:
        """Lines this path lost since ``base``, committed and uncommitted alike.

        Zero is what makes an append an append. A role allowed to add to a file
        may not edit or remove what is already in it, and that is checkable.

        Asked of the text before it is asked of the diff: a file whose last line
        had no newline gains one when anything is added after it, and git counts
        that line as removed and added back. A Verifier that appended two rows to a
        table ending that way was told it had removed lines, three times, and
        gave up on the file.
        """
        if self._only_grew(base, path):
            return 0
        total = 0
        for args in (("diff", "--numstat", f"{base}..HEAD", "--", path), ("diff", "--numstat", "HEAD", "--", path)):
            completed = self._run(*args, check=False)
            for line in completed.stdout.splitlines():
                parts = line.split("\t")
                if len(parts) >= 2 and parts[1].isdigit():
                    total += int(parts[1])
        return total

    def _only_grew(self, base: str, path: str) -> bool:
        """Whether the file as it is now starts with the file as ``base`` had it."""
        try:
            shown = self._run("show", f"{base}:{path}", check=False)
            now = (self.work_tree / path).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            # A file git will not count lines for is not one this can read either.
            return False
        if shown.returncode != 0:
            return False
        old = shown.stdout
        return now.startswith(old) or (not old.endswith("\n") and now.startswith(old + "\n"))

    def known_at(self, base: str, path: str) -> bool:
        """Whether ``base`` already carried ``path``.

        The question behind "was this file nobody's when the stint opened": a
        path exempt from the boundary because the setup pass left it uncommitted
        stops being exempt the moment a round commits it, since from then on an
        edit to it is a role's edit and the base has the text to put back.
        """
        if not base:
            return False
        return self._run("cat-file", "-e", f"{base}:{path}", check=False).returncode == 0

    def restore_from(self, base: str, paths: Sequence[str]) -> tuple[str, ...]:
        """``paths`` back to how ``base`` had them, for a path a role may not write."""
        restored: list[str] = []
        for path in paths:
            if self.known_at(base, path):
                self._run("checkout", base, "--", path, check=False)
            elif self.tracked(path):
                self._run("rm", "-r", "--force", "--quiet", "--", path, check=False)
            else:
                # `git rm` knows nothing about a path that was never added, and
                # its failure used to be swallowed -- the stray stayed on disk,
                # `add --all` in the revert commit picked it up, and the revert
                # committed what it was undoing.
                self._remove(self.work_tree / path)
            restored.append(path)
        return tuple(restored)

    def tracked(self, path: str) -> bool:
        return self._run("ls-files", "--error-unmatch", "--", path, check=False).returncode == 0

    @staticmethod
    def _remove(target: Path) -> None:
        if target.is_dir() and not target.is_symlink():
            shutil.rmtree(target, ignore_errors=True)
        elif target.exists() or target.is_symlink():
            target.unlink(missing_ok=True)

    def keep(self, paths: Sequence[str], quarantine: Path) -> tuple[str, ...]:
        """Copy ``paths`` as the worktree holds them now, before anything undoes them.

        The copy is what the role wrote, which is the one thing worth reading
        about a violation. Taken before the revert rather than during it: a
        committed stray put back to its base and then copied aside kept the base
        content, and a note saying the role's version was kept was false.
        """
        kept: list[str] = []
        for path in paths:
            target = self.work_tree / path
            if not target.is_file():
                continue
            self._keep(target, quarantine, path)
            if (quarantine / path).is_file():
                kept.append(path)
        return tuple(kept)

    @staticmethod
    def _keep(target: Path, quarantine: Path | None, path: str) -> None:
        """Copy a file about to be reverted, so the violation can be read afterwards."""
        if quarantine is None or not target.is_file():
            return
        try:
            destination = quarantine / path
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(target, destination)
        except OSError:
            # A copy that fails must not stop the revert: the boundary is the
            # point, and the keepsake is the courtesy.
            return

    def restore(self, paths: Sequence[str], *, quarantine: Path | None = None) -> tuple[str, ...]:
        """Undo ``paths``, keeping a copy of what was undone.

        A role that wrote where it may not write still wrote something, and
        losing it makes the violation unreadable. That was only honoured for
        untracked files: a tracked one went straight back to HEAD and its
        content was gone, which is the case where "what did it try to change"
        is the whole question. Measured 2026-09-10: a Builder's edit to
        `docs/EVENTS.md` was reverted, the log said a copy was kept, and the
        quarantine directory was empty.
        """
        undone: list[str] = []
        for path in paths:
            target = self.work_tree / path
            tracked = self._run("ls-files", "--error-unmatch", "--", path, check=False).returncode == 0
            if tracked:
                self._keep(target, quarantine, path)
                self._run("checkout", "HEAD", "--", path, check=False)
                undone.append(path)
                continue
            if not target.exists():
                continue
            if quarantine is not None:
                destination = quarantine / path
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(target), str(destination))
            elif target.is_dir():
                shutil.rmtree(target, ignore_errors=True)
            else:
                target.unlink(missing_ok=True)
            undone.append(path)
        return tuple(undone)
