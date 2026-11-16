"""Isolated git-worktree sandbox the driver bash-edits (edit-then-commit design).

The evolution model (SOP §3.1) is "the model edits code freely; robustness via
snapshot/restore + versioning", not diff-application — qwen is unreliable at
writing applyable unified diffs. So a candidate is produced by letting the driver
run ``bash`` in a detached worktree checked out at the PARENT commit, editing any
whitelisted file; we then capture exactly what it changed vs the parent as
``{repo-rel: bytes}``. Those bytes go to ``make_git_commit_apply_fn`` which
commits them onto the parent — giving a real child commit (no RealPathSync, no
live-repo writes; eval checks the commit out into its own worktree).
"""

from __future__ import annotations

import ast
import hashlib
import os
import signal
import stat
import subprocess
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


def _in_whitelist(rel: str) -> bool:
    return any(rel.startswith(p) for p in WHITELIST_PREFIXES)


def _capturable(rel_parts: tuple[str, ...], suffix: str) -> bool:
    if suffix in _IGNORED_SUFFIXES:
        return False
    return not any(part in _IGNORED_DIR_PARTS for part in rel_parts)


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
    def _path_fingerprint(path: Path) -> bytes:
        """Hash contents and permissions without following symbolic links."""
        digest = hashlib.sha256()
        try:
            mode = path.lstat().st_mode
        except FileNotFoundError:
            digest.update(b"missing")
            return digest.digest()
        digest.update(f"{mode}\0".encode("ascii"))
        if stat.S_ISREG(mode):
            with path.open("rb") as stream:
                digest.update(hashlib.file_digest(stream, "sha256").digest())
        elif stat.S_ISLNK(mode):
            digest.update(os.fsencode(os.readlink(path)))
        return digest.digest()

    def _origin_fingerprint(self) -> str | None:
        """Hash origin files, index entries, refs, configuration and hook modes.

        File contents catch writes to already-dirty or untracked paths and to
        files already marked assume-unchanged or skip-worktree. Index entries
        cover staged changes, and the symbolic HEAD distinguishes branches at
        the same commit. Ignored files and the object database are excluded.
        """
        try:
            refs = self._git("for-each-ref").stdout
            index_entries = self._git("ls-files", "--stage", "-v", "-z").stdout
            head_ref = self._git("rev-parse", "--symbolic-full-name", "HEAD").stdout
            paths = self._git("ls-files", "--cached", "--others", "--exclude-standard", "-z").stdout.split("\0")
            common = self._git("rev-parse", "--path-format=absolute", "--git-common-dir").stdout.strip()
            if not common:
                return None

            digest = hashlib.sha256()
            for output in (refs, index_entries, head_ref):
                digest.update(hashlib.sha256(output.encode("utf-8", "surrogateescape")).digest())
            for rel in sorted(set(paths) - {""}):
                digest.update(b"worktree\0" + os.fsencode(rel) + b"\0")
                digest.update(self._path_fingerprint(self.repo_root / rel))
            base = Path(common)
            files = []
            for pattern in ("config", "packed-refs"):
                candidate = base / pattern
                if candidate.is_file() or candidate.is_symlink():
                    files.append(candidate)
            for dirname in ("hooks", "refs", "info"):
                root_dir = base / dirname
                if root_dir.is_dir():
                    files.extend(p for p in root_dir.rglob("*") if p.is_file() or p.is_symlink())
            for file in sorted(files, key=lambda p: p.relative_to(base).as_posix()):
                digest.update(b"git\0" + os.fsencode(file.relative_to(base).as_posix()) + b"\0")
                digest.update(self._path_fingerprint(file))
        except (OSError, ValueError, subprocess.SubprocessError):
            return None
        return digest.hexdigest()

    def _origin_state(self) -> tuple[str, str, str] | None:
        """Origin HEAD, status and content fingerprint, or None on probe failure.

        ``git status`` covers tracked and untracked paths; ignored paths are not
        compared.
        """
        try:
            head = self._git("rev-parse", "HEAD").stdout.strip()
            status = self._git("status", "--porcelain", "-uall").stdout
        except (OSError, subprocess.SubprocessError):
            return None
        fingerprint = self._origin_fingerprint()
        if fingerprint is None:
            return None
        return head, status, fingerprint

    def _origin_gitdir(self) -> Path | None:
        """The origin gitdir named by this worktree's .git file, if it is one."""
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

    def bash(self, command: str, timeout: float = 60.0) -> str:
        # The candidate is the diff of THIS worktree vs base; a command that
        # names the origin repo escapes the experiment — reads see the wrong
        # version (the worktree is pinned at the parent commit), writes poison
        # every later measurement AND are never captured into the candidate.
        # The path is discoverable in-sandbox (the worktree's .git file names
        # the origin gitdir), so an in-context prohibition alone is not enough.
        origin_gitdir = self._origin_gitdir()
        blocked = [str(self.repo_root.resolve())]
        if origin_gitdir is not None:
            blocked.append(str(origin_gitdir))
        # Compare case-insensitively with forward slashes so a differently
        # spelled origin path (different case, mixed separators, \\?\ prefix on
        # Windows) still trips the refusal; the state comparison below stays
        # as the backstop for writes spelled any other way.
        normalised = command.casefold().replace("\\", "/")
        if any(path.casefold().replace("\\", "/") in normalised for path in blocked):
            return (
                f"[refused: command references the origin repo "
                f"({self.repo_root}). Work ONLY inside your cwd (this worktree "
                "holds the exact code version you are patching); the origin is "
                "a different checkout and out of bounds.]"
            )
        before = self._origin_state()
        if before is None:
            # Fail closed: without a snapshot there is no way to detect an
            # origin write this command is about to make.
            return "[refused: origin repository state unavailable (git or file probe failed); command not run]"
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
                # with the group before the origin is re-snapshotted.
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
                # write the origin after the snapshot. A child that leaves the
                # group (setsid / job control) is outside this guard.
                self._kill_process_group(proc)
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    pass
            after = self._origin_state()
        if after is None:
            raise RuntimeError(
                "sandbox command left the origin repository unverifiable (git or file probe failed); round aborted"
            )
        if after != before:
            raise RuntimeError(
                "sandbox command modified the origin repository (working tree, HEAD, refs, config or hooks); round aborted"
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
