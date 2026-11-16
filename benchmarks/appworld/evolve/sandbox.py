"""Isolated git-worktree sandbox the driver bash-edits (edit-then-commit design).

The evolution model (SOP §3.1) is "the model edits code freely; robustness via
snapshot/restore + versioning", not diff-application — qwen is unreliable at
writing applyable unified diffs. So a candidate is produced by letting the driver
run ``bash`` in a detached worktree checked out at the PARENT commit, editing any
whitelisted file; we then capture exactly what it changed vs the parent as
``{repo-rel: bytes}``. Those bytes go to ``make_git_commit_apply_fn`` which
commits them onto the parent — giving a real child commit (no RealPathSync, no
live-repo writes; eval checks the commit out into its own worktree).

The subject checkout (the checkout being evolved, ``repo_root``) is out of
bounds for the driver: a command that reaches it reads the wrong version, and a
write to it poisons every later measurement without ever reaching the candidate.
The guard below refuses commands that name the subject checkout, compares the
checkout's state around every command, and undoes a write it catches.

The comparison leans on git's own view of the checkout — the resolved and
symbolic HEAD, the index entries and their flags, the worktree against the
index, and the refs that existed when the command started — plus the places git
does not look: the paths marked assume-unchanged or skip-worktree, the untracked
paths, and the configuration, HEAD and hooks under the git dir. Two limits follow
from leaning on git: a write that keeps a file's size and mtime is invisible to
both, and an untracked path is compared by size and mtime rather than by content.
"""

from __future__ import annotations

import ast
import hashlib
import os
import signal
import stat
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from evolver.tree import git_ops

# The harness surface the driver may edit; anything else it touches is reverted.
WHITELIST_PREFIXES = (
    "benchmarks/appworld/",
    "raven/agent/",
)
# Build artifacts only — every OTHER whitelist file is captured regardless of
# suffix, because a suffix filter silently halves candidates whose fix includes
# a .toml/.cfg/extensionless file: the driver verified the full change in the
# sandbox but the commit (and thus the eval) carried only part of it.
_IGNORED_SUFFIXES = (".pyc", ".pyo")
_IGNORED_DIR_PARTS = ("__pycache__",)

# How much of the subject checkout the guard keeps in memory so it can undo a
# write: the tracked files that were already dirty when the command started, and
# the untracked files. A subject checkout with a larger dirty set is reported as
# partly unrestored instead of being read back on every command.
_RESTORE_CAPTURE_FILES = 512
_RESTORE_CAPTURE_BYTES = 32 * 1024 * 1024


def _in_whitelist(rel: str) -> bool:
    return any(rel.startswith(p) for p in WHITELIST_PREFIXES)


def _capturable(rel_parts: tuple[str, ...], suffix: str) -> bool:
    if suffix in _IGNORED_SUFFIXES:
        return False
    return not any(part in _IGNORED_DIR_PARTS for part in rel_parts)


@dataclass(frozen=True)
class _SubjectState:
    """One observation of the subject checkout.

    The state keeps git's own output rather than a digest of it, so a
    difference reads as something an operator can act on ("the index",
    "refs/evolver/probe") and so the restore knows what to put back.
    """

    git_dir: Path
    common_dir: Path
    head: str
    head_ref: str
    index_entries: str
    worktree: dict[str, str] = field(default_factory=dict)
    untracked: dict[str, str] = field(default_factory=dict)
    uncompared: dict[str, str] = field(default_factory=dict)
    refs: dict[str, str] = field(default_factory=dict)
    config: Optional[bytes] = None
    head_file: Optional[bytes] = None
    hooks: dict[str, tuple[int, bytes]] = field(default_factory=dict)
    index_bytes: Optional[bytes] = None
    worktree_bytes: dict[str, bytes] = field(default_factory=dict)
    uncompared_bytes: dict[str, bytes] = field(default_factory=dict)
    untracked_bytes: dict[str, bytes] = field(default_factory=dict)

    def differences(self, other: "_SubjectState") -> list[str]:
        """What changed between two observations of the subject checkout."""
        changes: list[str] = []
        if self.head != other.head:
            changes.append("HEAD")
        if self.head_ref != other.head_ref:
            changes.append("the checked-out branch")
        if self.index_entries != other.index_entries:
            changes.append("the index")
        if self.worktree != other.worktree:
            changes.append("tracked files")
        if self.untracked != other.untracked:
            changes.append("untracked paths")
        if self.uncompared != other.uncompared:
            changes.append("assume-unchanged files")
        if self.config != other.config:
            changes.append("git config")
        if self.head_file != other.head_file:
            changes.append("git HEAD")
        if self.hooks != other.hooks:
            changes.append("git hooks")
        # Only the refs that existed when the command started: a ref the
        # command created is the sandbox's own branch, tag or stash, which says
        # nothing about a write to the subject checkout.
        moved = sorted(name for name, sha in self.refs.items() if other.refs.get(name) != sha)
        if moved:
            changes.append(f"refs ({', '.join(moved[:5])})")
        return changes


class Sandbox:
    """A detached worktree at ``base_sha`` the driver bash-edits; captures the
    whitelist files it changed vs that base."""

    def __init__(
        self,
        repo_root: str | Path,
        worktree_path: str | Path,
        base_sha: str,
        *,
        whitelist_prefixes: tuple[str, ...] = WHITELIST_PREFIXES,
    ):
        self.repo_root = Path(repo_root)
        self.root = Path(worktree_path)
        self.whitelist = whitelist_prefixes
        git_ops.remove_worktree(self.repo_root, self.root, force=True) if self.root.exists() else None
        git_ops.create_worktree(self.repo_root, self.root, base_sha)
        self._orig = self._snapshot()

    def _iter_whitelist(self):
        for prefix in self.whitelist:
            d = self.root / prefix
            if d.exists():
                for p in d.rglob("*"):
                    if p.is_file():
                        rel = p.relative_to(self.root)
                        if _capturable(rel.parts, p.suffix):
                            yield rel.as_posix(), p

    def _snapshot(self) -> dict[str, bytes]:
        return {rel: p.read_bytes() for rel, p in self._iter_whitelist()}

    # ---- subject checkout guard -------------------------------------------

    def _git(self, *args: str) -> subprocess.CompletedProcess[str]:
        # Preserve path bytes so NUL-delimited ls-files output can be used to
        # fingerprint files even when a name is not valid UTF-8.
        return subprocess.run(
            ["git", "-C", str(self.repo_root), *args],
            capture_output=True,
            encoding="utf-8",
            errors="surrogateescape",
            check=True,
            timeout=30,
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
        )

    @staticmethod
    def _diff_paths(raw: str) -> tuple[str, ...]:
        """The paths ``git diff --raw`` reports as differing from the index."""
        tokens = raw.split("\0")
        paths: list[str] = []
        index = 0
        while index < len(tokens):
            info = tokens[index]
            if not info.startswith(":"):
                index += 1
                continue
            status = info.split()[-1]
            # A rename or a copy carries the old path and the new one.
            width = 3 if status[:1] in ("R", "C") else 2
            paths.append(tokens[index + width - 1] if index + width - 1 < len(tokens) else "")
            index += width
        return tuple(path for path in paths if path)

    @staticmethod
    def _uncompared_paths(index_entries: str) -> tuple[str, ...]:
        """The tracked paths git refuses to compare for us.

        ``git ls-files -v`` tags a path assume-unchanged (a lowercase letter) or
        skip-worktree (``S``); ``git diff`` skips both, so their content is read
        directly.
        """
        paths: list[str] = []
        for record in index_entries.split("\0"):
            meta, _, path = record.partition("\t")
            if path and meta.split(" ", 1)[0] != "H":
                paths.append(path)
        return tuple(paths)

    @staticmethod
    def _file_token(path: Path) -> Optional[tuple[int, bytes]]:
        """The mode and the bytes of ``path``, without following a symlink."""
        try:
            mode = path.lstat().st_mode
        except OSError:
            return None
        if stat.S_ISLNK(mode):
            try:
                return mode, os.fsencode(os.readlink(path))
            except OSError:
                return mode, b""
        if stat.S_ISREG(mode):
            try:
                return mode, path.read_bytes()
            except OSError:
                return mode, b""
        return mode, b""

    @staticmethod
    def _stat_token(path: Path) -> Optional[str]:
        """A cheap token for a path: its size, mtime and mode."""
        try:
            info = path.lstat()
        except OSError:
            return "missing"
        return f"{info.st_size}:{info.st_mtime_ns}:{info.st_mode}"

    def _path_state(self, path: str) -> tuple[Optional[str], Optional[bytes]]:
        """A comparison token for one tracked path, and its content.

        The token is the content, so a rewrite that keeps the file's size is
        still seen as a change.
        """
        token = self._file_token(self.repo_root / path)
        if token is None:
            return "missing", None
        mode, payload = token
        digest = hashlib.sha256(f"{mode}\0".encode("ascii") + payload).hexdigest()
        return digest, payload

    @staticmethod
    def _spend(budget: list[int], size: int) -> bool:
        """Take ``size`` bytes out of the capture budget, if it fits."""
        if budget[0] <= 0 or budget[1] < size:
            return False
        budget[0] -= 1
        budget[1] -= size
        return True

    def _capture_one(self, path: Path, budget: list[int]) -> Optional[bytes]:
        """The content of ``path``, or None when it is over the capture budget."""
        if budget[0] <= 0 or budget[1] <= 0:
            return None
        try:
            if path.is_symlink() or not path.is_file():
                return None
            size = path.stat().st_size
        except OSError:
            return None
        if size > budget[1]:
            return None
        try:
            content = path.read_bytes()
        except OSError:
            return None
        if not self._spend(budget, len(content)):
            return None
        return content

    @staticmethod
    def _read_optional(path: Path) -> Optional[bytes]:
        """The bytes of ``path``, or None when it does not exist."""
        try:
            return path.read_bytes()
        except (OSError, ValueError):
            return None

    def _hooks(self, common_dir: Path) -> dict[str, tuple[int, bytes]]:
        """Every file under the hooks directory, keyed by its relative path."""
        hooks: dict[str, tuple[int, bytes]] = {}
        root = common_dir / "hooks"
        if not root.is_dir():
            return hooks
        for path in sorted(root.rglob("*")):
            token = self._file_token(path)
            if token is None or stat.S_ISDIR(token[0]):
                continue
            hooks[path.relative_to(root).as_posix()] = token
        return hooks

    def _refs(self) -> dict[str, str]:
        """Every ref of the subject checkout, name -> sha."""
        raw = self._git("for-each-ref", "--format=%(refname) %(objectname)").stdout
        refs: dict[str, str] = {}
        for line in raw.splitlines():
            name, _, sha = line.partition(" ")
            if name:
                refs[name] = sha
        return refs

    def _subject_state(self, capture: bool) -> Optional[_SubjectState]:
        """Observe the subject checkout, or None when a probe fails.

        ``capture`` keeps the content the restore needs — the tracked files
        that were already dirty and the untracked files — so a detected write
        can be undone instead of only reported.
        """
        try:
            git_dir_raw = self._git("rev-parse", "--absolute-git-dir").stdout.strip()
            common_dir_raw = self._git("rev-parse", "--path-format=absolute", "--git-common-dir").stdout.strip()
            if not git_dir_raw or not common_dir_raw:
                return None
            head = self._git("rev-parse", "HEAD").stdout.strip()
            head_ref = self._git("rev-parse", "--symbolic-full-name", "HEAD").stdout.strip()
            index_entries = self._git("ls-files", "--stage", "-v", "-z").stdout
            worktree_diff = self._git("diff", "--raw", "--no-renames", "-z").stdout
            untracked_raw = self._git("ls-files", "--others", "--exclude-standard", "--directory", "-z").stdout
            refs = self._refs()
        except (OSError, ValueError, subprocess.SubprocessError):
            return None

        budget = [_RESTORE_CAPTURE_FILES, _RESTORE_CAPTURE_BYTES]
        worktree: dict[str, str] = {}
        worktree_bytes: dict[str, bytes] = {}
        for path in self._diff_paths(worktree_diff):
            token, content = self._path_state(path)
            worktree[path] = token or "missing"
            if capture and content is not None and self._spend(budget, len(content)):
                worktree_bytes[path] = content

        untracked: dict[str, str] = {}
        untracked_bytes: dict[str, bytes] = {}
        for path in untracked_raw.split("\0"):
            if not path:
                continue
            # An untracked path is compared by size and mtime: git lists an
            # untracked directory as one entry, and its content may be large.
            token = self._stat_token(self.repo_root / path)
            if token is None:
                continue
            untracked[path] = token
            if capture and not path.endswith("/"):
                content = self._capture_one(self.repo_root / path, budget)
                if content is not None:
                    untracked_bytes[path] = content

        uncompared: dict[str, str] = {}
        uncompared_bytes: dict[str, bytes] = {}
        for path in self._uncompared_paths(index_entries):
            token, content = self._path_state(path)
            if token is None:
                continue
            uncompared[path] = token
            if capture and content is not None and self._spend(budget, len(content)):
                uncompared_bytes[path] = content

        git_dir = Path(git_dir_raw)
        common_dir = Path(common_dir_raw)
        return _SubjectState(
            git_dir=git_dir,
            common_dir=common_dir,
            head=head,
            head_ref=head_ref,
            index_entries=index_entries,
            worktree=worktree,
            untracked=untracked,
            uncompared=uncompared,
            refs=refs,
            config=self._read_optional(common_dir / "config"),
            head_file=self._read_optional(git_dir / "HEAD"),
            hooks=self._hooks(common_dir),
            index_bytes=self._read_optional(git_dir / "index") if capture else None,
            worktree_bytes=worktree_bytes,
            uncompared_bytes=uncompared_bytes,
            untracked_bytes=untracked_bytes,
        )

    def _subject_gitdir(self) -> Path | None:
        """The subject checkout's gitdir named by this worktree's .git file, if it is one."""
        gitfile = self.root / ".git"
        try:
            text = gitfile.read_text(encoding="utf-8")
        except OSError:
            return None
        if not text.startswith("gitdir:"):
            return None
        path = Path(text.split(":", 1)[1].strip())
        if not path.is_absolute():
            # A relative gitdir is relative to this worktree, not to the process cwd.
            path = (self.root / path).resolve()
        else:
            path = path.resolve()
        return path

    @staticmethod
    def _kill_process_group(proc: subprocess.Popen[str]) -> None:
        """Kill the process group started by ``bash`` so no same-group child outlives it.

        A child that leaves the group (``setsid``, shell job control) is out of
        reach; containing the whole tree is a jail concern, not this guard's.
        """
        if os.name == "posix":
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except OSError:
                pass
            return
        if os.name == "nt":
            try:
                # taskkill walks the live process tree; once bash has exited,
                # reparented descendants are not reachable.
                subprocess.run(
                    ["taskkill", "/T", "/F", "/PID", str(proc.pid)],
                    capture_output=True,
                    check=False,
                )
            except OSError:
                pass

    def _write_bytes(self, path: Path, content: bytes) -> None:
        """Replace a file's content, keeping its mode."""
        try:
            mode = path.stat().st_mode & 0o777
        except OSError:
            mode = 0o644
        path.write_bytes(content)
        os.chmod(path, mode)

    def _restore_subject(self, before: _SubjectState, after: _SubjectState) -> list[str]:
        """Put the subject checkout back the way the command found it.

        Returns the paths it could not put back, so the abort message can name
        them instead of claiming a clean restore.
        """
        problems: list[str] = []
        # The index defines what `git checkout --` restores from, so it goes first.
        if before.index_bytes is not None:
            self._write_bytes(before.git_dir / "index", before.index_bytes)
        if before.head_file is not None:
            self._write_bytes(before.git_dir / "HEAD", before.head_file)
        if before.config is not None:
            self._write_bytes(before.common_dir / "config", before.config)
        self._restore_hooks(before, problems)
        self._restore_refs(before, after, problems)
        self._restore_worktree(before, after, problems)
        return problems

    def _restore_hooks(self, before: _SubjectState, problems: list[str]) -> None:
        """Put the hooks directory back the way it was."""
        root = before.common_dir / "hooks"
        if not root.is_dir():
            return
        for path in sorted(root.rglob("*")):
            rel = path.relative_to(root).as_posix()
            if rel in before.hooks:
                continue
            if path.is_symlink() or path.is_file():
                try:
                    path.unlink()
                except OSError:
                    problems.append(f"hooks/{rel}")
        for rel, (mode, payload) in before.hooks.items():
            path = root / rel
            try:
                if stat.S_ISLNK(mode):
                    target = payload.decode("utf-8", "surrogateescape")
                    if path.is_symlink() and os.readlink(path) == target:
                        continue
                    if path.is_symlink() or path.exists():
                        path.unlink()
                    os.symlink(target, path)
                    continue
                path.parent.mkdir(parents=True, exist_ok=True)
                self._write_bytes(path, payload)
                os.chmod(path, mode & 0o777)
            except OSError:
                problems.append(f"hooks/{rel}")

    def _restore_refs(self, before: _SubjectState, after: _SubjectState, problems: list[str]) -> None:
        """Put the refs back: the ones the command moved, and the ones it deleted."""
        for name, sha in sorted(before.refs.items()):
            if after.refs.get(name) == sha:
                continue
            try:
                self._git("update-ref", name, sha)
            except subprocess.SubprocessError:
                problems.append(f"ref {name}")
        for name in sorted(set(after.refs) - set(before.refs)):
            # A ref the command created — the sandbox's own branch, tag or
            # stash. It says nothing about a write to the subject checkout.
            try:
                self._git("update-ref", "-d", name)
            except subprocess.SubprocessError:
                problems.append(f"ref {name}")

    def _restore_worktree(self, before: _SubjectState, after: _SubjectState, problems: list[str]) -> None:
        """Put the working tree back: what the command wrote, and what was
        already dirty when it started."""
        # The untracked paths, listed again now that the index is back: a file
        # the command staged is untracked again once the index is restored.
        try:
            current = self._git("ls-files", "--others", "--exclude-standard", "--directory", "-z").stdout
        except subprocess.SubprocessError:
            current = ""
        fresh = set(after.untracked) - set(before.untracked)
        fresh.update(path for path in current.split("\0") if path and path not in before.untracked)
        for path in sorted(fresh):
            if path.endswith("/"):
                # A directory the command created: its content is not read back
                # (it may be large), so it is reported instead of removed.
                problems.append(path)
                continue
            try:
                (self.repo_root / path).unlink()
            except OSError:
                problems.append(path)
        for path, content in before.untracked_bytes.items():
            try:
                if not (self.repo_root / path).is_file():
                    continue
                self._write_bytes(self.repo_root / path, content)
            except OSError:
                problems.append(path)
        for path, content in before.uncompared_bytes.items():
            # git skips these paths, so `git checkout --` would leave them
            # alone; the captured content is what puts them back.
            try:
                current = self._file_token(self.repo_root / path)
                if current is not None and current[1] != content:
                    self._write_bytes(self.repo_root / path, content)
            except OSError:
                problems.append(path)
        try:
            dirty = self._diff_paths(self._git("diff", "--raw", "--no-renames", "-z").stdout)
        except subprocess.SubprocessError:
            problems.append("the working tree")
            return
        for path in sorted(dirty):
            saved = before.worktree_bytes.get(path)
            if saved is not None:
                # The file was already dirty before the command started; put
                # back the edit that predates it rather than the index version.
                try:
                    self._write_bytes(self.repo_root / path, saved)
                except OSError:
                    problems.append(path)
                continue
            try:
                self._git("checkout", "--", path)
            except subprocess.SubprocessError:
                problems.append(path)

    def bash(self, command: str, timeout: float = 60.0) -> str:
        # The candidate is the diff of THIS worktree vs base; a command that
        # names the subject checkout escapes the experiment — reads see the
        # wrong version (the worktree is pinned at the parent commit), writes
        # poison every later measurement AND are never captured into the
        # candidate. The path is discoverable in-sandbox (the worktree's .git
        # file names the subject checkout's gitdir), so an in-context
        # prohibition alone is not enough.
        subject_gitdir = self._subject_gitdir()
        blocked = [str(self.repo_root.resolve())]
        if subject_gitdir is not None:
            blocked.append(str(subject_gitdir))
        # Compare case-insensitively with forward slashes so a differently
        # spelled subject path (different case, mixed separators, \\?\ prefix on
        # Windows) still trips the refusal; the state comparison below stays
        # as the backstop for writes spelled any other way.
        normalised = command.casefold().replace("\\", "/")
        if any(path.casefold().replace("\\", "/") in normalised for path in blocked):
            return (
                f"[refused: command references the subject checkout "
                f"({self.repo_root}). Work ONLY inside your cwd (this worktree "
                "holds the exact code version you are patching); the subject "
                "checkout is a different copy and out of bounds.]"
            )
        before = self._subject_state(capture=True)
        if before is None:
            # Fail closed: without a snapshot there is no way to detect a
            # write to the subject checkout this command is about to make.
            return "[refused: subject checkout state unavailable (git or file probe failed); command not run]"
        timed_out = False
        proc: subprocess.Popen[str] | None = None
        out, err = "", ""
        error_text = None
        try:
            proc = subprocess.Popen(
                ["bash", "-c", command],
                cwd=str(self.root),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                encoding="utf-8",
                errors="replace",
                # Its own process group, so a surviving grandchild can be killed
                # with the group before the subject checkout is re-snapshotted.
                start_new_session=(os.name == "posix"),
            )
            try:
                out, err = proc.communicate(timeout=timeout)
            except subprocess.TimeoutExpired:
                timed_out = True
                self._kill_process_group(proc)
                try:
                    out, err = proc.communicate(timeout=5)
                except subprocess.TimeoutExpired:
                    out, err = "", ""
        except Exception as e:  # noqa: BLE001 — a bad command must not crash the run
            error_text = f"[bash error: {type(e).__name__}: {str(e)[:150]}]"
        finally:
            if proc is not None:
                # A background child that stays in bash's process group can
                # outlive bash; kill the group on every exit path so it cannot
                # write the subject checkout after the snapshot. A child that
                # leaves the group (setsid / job control) is outside this guard.
                self._kill_process_group(proc)
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    pass
            after = self._subject_state(capture=False)
        if after is None:
            raise RuntimeError(
                "sandbox command left the subject checkout unverifiable (git or file probe failed); round aborted"
            )
        changes = before.differences(after)
        if changes:
            try:
                unrestored = self._restore_subject(before, after)
            except OSError as exc:
                unrestored = [f"the restore failed ({type(exc).__name__})"]
            raise RuntimeError(
                "sandbox command modified the subject checkout "
                f"({', '.join(changes)}); round aborted"
                + (f"; could not undo: {', '.join(unrestored)}" if unrestored else "")
            )
        if timed_out:
            return (
                f"[TIMEOUT after {timeout:.0f}s — command too slow, aborted. Do NOT scan the "
                "whole filesystem (no `find /`); you are at the repo root, use relative paths "
                "like benchmarks/appworld/agent_cli.py.]"
            )
        if error_text is not None:
            return error_text
        if proc is None:
            return "[bash error: process did not start]"
        out = (out or "")[-4000:]
        err = (err or "")[-1500:]
        return f"[exit {proc.returncode}]\n{out}" + (f"\n[stderr]\n{err}" if err else "")

    def write_text(self, rel: str, content: str) -> str:
        """Write a whole file (the editor's ``write_file`` action) — bypasses
        shell quoting entirely. Out-of-whitelist paths are refused up front
        (``scope_restore`` would revert them anyway; failing fast saves turns)."""
        rel = rel.lstrip("/")
        if ".." in Path(rel).parts:
            return f"[refused: path escapes the worktree: {rel}]"
        if not any(rel.startswith(p) for p in self.whitelist):
            return (
                f"[refused: {rel} is outside the editable whitelist "
                f"({', '.join(self.whitelist)}) — it would be reverted]"
            )
        p = self.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
        return f"[wrote {rel} ({len(content)} chars)]"

    def scope_restore(self) -> list[str]:
        """Revert any edit OUTSIDE the whitelist (the worktree is a full checkout)."""
        r = subprocess.run(
            ["git", "status", "--porcelain", "-uall"],
            cwd=str(self.root),
            capture_output=True,
            text=True,
        )
        reverted = []
        for line in r.stdout.splitlines():
            rel = line[3:].strip()
            if rel and not any(rel.startswith(p) for p in self.whitelist):
                subprocess.run(["git", "checkout", "--", rel], cwd=str(self.root), capture_output=True)
                subprocess.run(["git", "clean", "-fdq", "--", rel], cwd=str(self.root), capture_output=True)
                reverted.append(rel)
        return reverted

    def original(self, rel: str) -> Optional[bytes]:
        """The base-commit bytes of one whitelist file (None = didn't exist)."""
        return self._orig.get(rel)

    def changed_whitelist(self) -> dict[str, bytes]:
        """Whitelist files the driver changed vs the base — this candidate's edits."""
        now = self._snapshot()
        return {rel: data for rel, data in now.items() if self._orig.get(rel) != data}

    def deleted_whitelist(self) -> list[str]:
        """Whitelist files the driver deleted vs the base (a rename shows up as
        one changed file + one deletion; without this the commit keeps the old
        file and the candidate diverges from what the driver verified)."""
        now = self._snapshot()
        return sorted(rel for rel in self._orig if rel not in now)

    def compile_check(self, files: dict[str, bytes]) -> tuple[bool, str]:
        for rel, data in files.items():
            if rel.endswith(".py"):
                try:
                    ast.parse(data.decode("utf-8", "replace"))
                except SyntaxError as e:
                    return False, f"{rel}: {e}"
        return True, ""

    def import_check(
        self,
        module: str,
        *,
        python_exe: str | None = None,
        timeout: float = 120.0,
    ) -> tuple[bool, str]:
        """Import ``module`` in a subprocess with cwd = this worktree, so the
        candidate's edited tree is what resolves — the same cwd-first import the
        eval's batch subprocess relies on. Catches what ``compile_check``'s AST
        parse cannot (bad imports, module-level NameError, a deleted file
        breaking the package) before a full eval is burned on a dead candidate."""
        import sys

        exe = python_exe or sys.executable
        try:
            r = subprocess.run(
                [exe, "-c", f"import {module}"],
                cwd=str(self.root),
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            return False, f"import {module} timed out after {timeout:.0f}s"
        except OSError as e:
            return False, f"import check could not run ({exe}): {e}"
        if r.returncode != 0:
            return False, (r.stderr or r.stdout or "")[-800:]
        return True, ""

    def close(self):
        git_ops.remove_worktree(self.repo_root, self.root, force=True)


__all__ = ["Sandbox", "WHITELIST_PREFIXES"]
