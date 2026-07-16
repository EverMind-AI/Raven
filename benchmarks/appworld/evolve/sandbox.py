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
import subprocess
from pathlib import Path
from typing import Optional

from raven.evolver.tree import git_ops

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

    def bash(self, command: str, timeout: float = 60.0) -> str:
        # The candidate is the diff of THIS worktree vs base; a command that
        # names the origin repo escapes the experiment — reads see the wrong
        # version (the worktree is pinned at the parent commit), writes poison
        # every later measurement AND are never captured into the candidate.
        # The path is discoverable in-sandbox (the worktree's .git file names
        # the origin gitdir), so an in-context prohibition alone is not enough.
        if str(self.repo_root.resolve()) in command:
            return (
                f"[refused: command references the origin repo "
                f"({self.repo_root}). Work ONLY inside your cwd (this worktree "
                "holds the exact code version you are patching); the origin is "
                "a different checkout and out of bounds.]"
            )
        try:
            r = subprocess.run(
                ["bash", "-c", command], cwd=str(self.root),
                capture_output=True, text=True, timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            return (
                f"[TIMEOUT after {timeout:.0f}s — command too slow, aborted. Do NOT scan the "
                "whole filesystem (no `find /`); you are at the repo root, use relative paths "
                "like benchmarks/appworld/agent_cli.py.]"
            )
        except Exception as e:  # noqa: BLE001 — a bad command must not crash the run
            return f"[bash error: {type(e).__name__}: {str(e)[:150]}]"
        out = (r.stdout or "")[-4000:]
        err = (r.stderr or "")[-1500:]
        return f"[exit {r.returncode}]\n{out}" + (f"\n[stderr]\n{err}" if err else "")

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
            cwd=str(self.root), capture_output=True, text=True,
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
                [exe, "-c", f"import {module}"], cwd=str(self.root),
                capture_output=True, text=True, timeout=timeout,
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
