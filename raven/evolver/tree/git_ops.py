"""Low-level Git command wrappers for the evolver tree (C2.1).

These functions wrap ``subprocess`` calls to ``git`` so the evolver can
construct commits without depending on a particular Python Git library.
They are intentionally small, side-effect-explicit, and easy to mock in
tests via a custom ``repo_root``.

Two operational categories:

1. **Read** (cheap, no state mutation):
   :func:`get_current_sha`, :func:`commit_exists`, :func:`get_tree_sha`,
   :func:`get_commit_message`.

2. **Write that does NOT touch the working tree**:
   :func:`apply_patch_as_commit` uses Git plumbing (``read-tree`` /
   ``apply --cached`` / ``write-tree`` / ``commit-tree``) so the
   user's working directory is never modified. This is what
   ``EvolverTreeStore.create_child_node`` uses to spawn child nodes.

3. **Write that DOES create a working tree**:
   :func:`create_worktree` / :func:`remove_worktree` use
   ``git worktree`` to materialise a checkout of a specific commit
   at a separate path — used later for evaluation runs. Not used by
   commit construction.

Why subprocess and not pygit2 / GitPython:
- Zero extra dependency
- Git plumbing semantics are stable and well-documented
- Easy to debug (we just print commands on failure)

Error model: any non-zero exit code raises :class:`GitOpError` with the
exact command and stderr. Callers should not catch this except at the
top of a logical operation.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Optional


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


_EPHEMERAL_ROOT: Optional[Path] = None


def set_ephemeral_root(path: Optional[Path]) -> None:
    """Route ephemeral worktree temp dirs under a run-scoped directory.

    By default they land in the system temp dir; the context managers clean
    them on any Python exit (including Ctrl-C), but a hard kill (SIGKILL,
    power loss) leaves them behind — still registered as worktrees of the
    subject repo. Parking them under the run's work_dir makes leftovers
    discoverable and lets the next launch sweep exactly its own garbage
    without touching concurrent runs. ``None`` restores the system default.
    """
    global _EPHEMERAL_ROOT
    _EPHEMERAL_ROOT = None if path is None else Path(path)


def _ephemeral_dir() -> Optional[str]:
    if _EPHEMERAL_ROOT is None:
        return None
    _EPHEMERAL_ROOT.mkdir(parents=True, exist_ok=True)
    return str(_EPHEMERAL_ROOT)


class GitOpError(RuntimeError):
    """Raised when a git command exits non-zero."""

    def __init__(self, cmd: list[str], returncode: int, stderr: str, stdout: str = ""):
        self.cmd = cmd
        self.returncode = returncode
        self.stderr = stderr
        self.stdout = stdout
        super().__init__(
            f"git command failed (exit {returncode}): {' '.join(cmd)}\n"
            f"stderr: {stderr.strip()}\n"
            f"stdout: {stdout.strip()}"
        )


# ---------------------------------------------------------------------------
# Core runner
# ---------------------------------------------------------------------------


def _run(
    repo_root: Path,
    *args: str,
    env: Optional[dict[str, str]] = None,
    input_text: Optional[str] = None,
    check: bool = True,
) -> str:
    """Run ``git <args>`` in ``repo_root`` and return stdout.

    :param env: extra env vars merged on top of os.environ
    :param input_text: passed to stdin (used by ``git apply``)
    :param check: raise :class:`GitOpError` on non-zero exit
    """
    cmd = ["git", *args]
    merged_env = {**os.environ, **(env or {})}
    proc = subprocess.run(
        cmd,
        cwd=str(repo_root),
        env=merged_env,
        input=input_text,
        capture_output=True,
        text=True,
    )
    if check and proc.returncode != 0:
        raise GitOpError(
            cmd=cmd,
            returncode=proc.returncode,
            stderr=proc.stderr,
            stdout=proc.stdout,
        )
    return proc.stdout


# ---------------------------------------------------------------------------
# Read operations
# ---------------------------------------------------------------------------


def get_current_sha(repo_root: Path) -> str:
    """Return the SHA of the current HEAD."""
    return _run(repo_root, "rev-parse", "HEAD").strip()


def get_tree_sha(repo_root: Path, commit_sha: str) -> str:
    """Return the tree SHA referenced by ``commit_sha``."""
    return _run(repo_root, "rev-parse", f"{commit_sha}^{{tree}}").strip()


def commit_exists(repo_root: Path, sha: str) -> bool:
    """Return True iff ``sha`` is a known commit object in this repo."""
    try:
        out = _run(repo_root, "cat-file", "-t", sha).strip()
    except GitOpError:
        return False
    return out == "commit"


def get_commit_message(repo_root: Path, sha: str) -> str:
    """Return the full commit message (subject + body) for ``sha``."""
    return _run(repo_root, "log", "-1", "--format=%B", sha)


def read_file_at(repo_root: Path, sha: str, rel_path: str) -> bytes:
    """Return the raw bytes of ``rel_path`` as stored in commit ``sha``.

    Binary-safe (``_run`` decodes text; blobs may be arbitrary bytes). Raises
    :class:`GitOpError` when the path does not exist in that commit.
    """
    cmd = ["git", "cat-file", "blob", f"{sha}:{rel_path}"]
    proc = subprocess.run(cmd, cwd=str(repo_root), capture_output=True)
    if proc.returncode != 0:
        raise GitOpError(
            cmd=cmd,
            returncode=proc.returncode,
            stderr=proc.stderr.decode("utf-8", errors="replace"),
        )
    return proc.stdout


# ---------------------------------------------------------------------------
# Write — construct commits WITHOUT touching the working tree
# ---------------------------------------------------------------------------


@contextmanager
def _temp_index() -> Iterator[Path]:
    """Yield a path for a temporary git index file, cleaning up after."""
    fd, name = tempfile.mkstemp(prefix="evolver-idx-", suffix=".tmp")
    os.close(fd)
    path = Path(name)
    # ``git read-tree`` requires the file not to exist yet; remove it.
    if path.exists():
        path.unlink()
    try:
        yield path
    finally:
        if path.exists():
            path.unlink()


def apply_patch_as_commit(
    repo_root: Path,
    parent_sha: str,
    unified_diff: str,
    message: str,
    *,
    author_name: str = "evolver-bot",
    author_email: str = "evolver@raven.local",
) -> str:
    """Construct a child commit by applying ``unified_diff`` on top of
    ``parent_sha``. **Does not touch the working tree or move HEAD.**

    Returns the SHA of the new commit.

    Algorithm (Git plumbing):

    1. ``git read-tree parent_sha`` into a temp index file
    2. ``git apply --cached --index=<temp>`` the patch into that index
    3. ``git write-tree`` from that index → tree_sha
    4. ``git commit-tree tree_sha -p parent_sha -m message`` → child_sha

    The new commit is reachable only via its returned SHA — no branch
    ref is updated. Callers (typically :meth:`EvolverTreeStore.
    create_child_node`) record the SHA in
    :attr:`HarnessNode.git_commit_sha` and may later create a ref
    via :func:`create_branch`.

    If the patch fails to apply cleanly, raises :class:`GitOpError`
    and no commit is created.

    :raises GitOpError: if any sub-step fails
    :raises ValueError: if ``parent_sha`` doesn't exist locally
    """
    if not commit_exists(repo_root, parent_sha):
        raise ValueError(
            f"parent_sha {parent_sha!r} is not a known commit in {repo_root}"
        )

    git_dir = (repo_root / ".git").resolve()
    if not git_dir.exists():
        # Worktrees have .git as a file pointing to gitdir; fall back to
        # asking git itself.
        git_dir_str = _run(repo_root, "rev-parse", "--git-dir").strip()
        git_dir = (repo_root / git_dir_str).resolve()

    with _temp_index() as idx:
        # The custom index file is selected via GIT_INDEX_FILE env var
        # for the duration of each plumbing call.
        env = {"GIT_INDEX_FILE": str(idx)}
        # Step 1: load parent tree into temp index
        _run(repo_root, "read-tree", parent_sha, env=env)
        # Step 2: apply the diff to the temp index (no working-tree touch)
        _run(
            repo_root,
            "apply",
            "--cached",
            "--allow-empty",
            "-",  # read patch from stdin
            env=env,
            input_text=unified_diff,
        )
        # Step 3: write a tree object from the temp index
        tree_sha = _run(repo_root, "write-tree", env=env).strip()

    # Step 4: commit-tree to create the actual commit. Use environment
    # variables for author / committer so the call doesn't depend on
    # the global git config (which might not have user.name/email set
    # on a CI box).
    commit_env = {
        "GIT_AUTHOR_NAME": author_name,
        "GIT_AUTHOR_EMAIL": author_email,
        "GIT_COMMITTER_NAME": author_name,
        "GIT_COMMITTER_EMAIL": author_email,
    }
    child_sha = _run(
        repo_root,
        "commit-tree",
        tree_sha,
        "-p",
        parent_sha,
        "-m",
        message,
        env=commit_env,
    ).strip()
    return child_sha


def commit_files_as_child(
    repo_root: Path,
    parent_sha: str,
    files: dict[str, bytes],
    message: str,
    *,
    deletions: tuple[str, ...] = (),
    author_name: str = "evolver-bot",
    author_email: str = "evolver@raven.local",
) -> tuple[str, list[str]]:
    """Commit a set of edited files as a child of ``parent_sha``.

    The "edit-then-commit" apply path: the driver edits files directly (weak
    models do this far more reliably than emitting a valid unified diff), and we
    capture the result as a real git commit off the parent — giving the node a
    reproducible SHA, git ancestry, and an immutable-guardable changed-file list,
    instead of a "sandbox" placeholder + live-tree mutation.

    ``files`` maps repo-relative paths to their full new bytes; ``deletions``
    lists repo-relative paths to remove. Materialises them into a detached
    worktree at ``parent_sha``, stages everything, and ``commit-tree``s onto
    ``parent_sha``. **The main working tree is never touched.** Returns
    ``(child_sha, changed_paths)``.
    """
    if not commit_exists(repo_root, parent_sha):
        raise ValueError(f"parent_sha {parent_sha!r} is not a known commit in {repo_root}")

    with tempfile.TemporaryDirectory(prefix="evolver-edit-", dir=_ephemeral_dir()) as tmp:
        wt = Path(tmp) / "wt"
        create_worktree(repo_root, wt, parent_sha)
        try:
            for rel, data in files.items():
                dest = wt / rel
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(data if isinstance(data, bytes) else str(data).encode())
            for rel in deletions:
                fp = wt / rel
                if fp.exists():
                    fp.unlink()
            _run(wt, "add", "-A")
            changed = [
                line for line in _run(
                    wt, "diff", "--cached", "--name-only", parent_sha
                ).splitlines() if line.strip()
            ]
            tree_sha = _run(wt, "write-tree").strip()
            commit_env = {
                "GIT_AUTHOR_NAME": author_name,
                "GIT_AUTHOR_EMAIL": author_email,
                "GIT_COMMITTER_NAME": author_name,
                "GIT_COMMITTER_EMAIL": author_email,
            }
            child_sha = _run(
                wt, "commit-tree", tree_sha, "-p", parent_sha, "-m", message,
                env=commit_env,
            ).strip()
        finally:
            remove_worktree(repo_root, wt)
    return child_sha, changed


@contextmanager
def worktree_at(repo_root: Path, sha: str) -> Iterator[Path]:
    """Yield an ephemeral detached worktree checked out at ``sha``, cleaned up
    after. The eval-time counterpart of :func:`commit_files_as_child` — a scorer
    runs against this checkout instead of the mutated live repo."""
    with tempfile.TemporaryDirectory(prefix="evolver-wt-", dir=_ephemeral_dir()) as tmp:
        wt = Path(tmp) / "wt"
        create_worktree(repo_root, wt, sha)
        try:
            yield wt
        finally:
            remove_worktree(repo_root, wt)


# ---------------------------------------------------------------------------
# Branch / ref management (lightweight; tag-style use only)
# ---------------------------------------------------------------------------


def create_ref(repo_root: Path, ref_name: str, sha: str) -> None:
    """Point ``ref_name`` (e.g. ``refs/evolver/<node_id>``) at ``sha``.

    Evolver-constructed commits are otherwise reachable only through SHAs
    recorded in JSON (journal / node ledger), which ``git gc`` does not see —
    an unreferenced candidate commit would be pruned after the default grace
    period, breaking late worktree evals and the post-hoc sealed unseal. A
    plain ref (not a branch: never checked out, invisible to ``git branch``)
    anchors it. Overwrites if the ref already exists.
    """
    _run(repo_root, "update-ref", ref_name, sha)


def delete_ref(repo_root: Path, ref_name: str) -> None:
    """Remove a ref created by :func:`create_ref` (no-op if absent)."""
    _run(repo_root, "update-ref", "-d", ref_name, check=False)


def create_branch(repo_root: Path, branch_name: str, sha: str) -> None:
    """Create a branch named ``branch_name`` pointing at ``sha``.

    Used to give an evolver-constructed commit a human-readable ref so
    it isn't garbage-collected. Does **not** check out the branch —
    HEAD remains where it was.
    """
    _run(repo_root, "branch", branch_name, sha)


def branch_exists(repo_root: Path, branch_name: str) -> bool:
    """True iff a local branch with this name exists."""
    out = _run(
        repo_root,
        "show-ref",
        "--verify",
        "--quiet",
        f"refs/heads/{branch_name}",
        check=False,
    )
    # show-ref with --quiet sets exit code; _run returns "" on success
    # but with check=False we have to call it differently
    proc = subprocess.run(
        ["git", "show-ref", "--verify", "--quiet", f"refs/heads/{branch_name}"],
        cwd=str(repo_root),
        capture_output=True,
        text=True,
    )
    return proc.returncode == 0


def delete_branch(repo_root: Path, branch_name: str, *, force: bool = True) -> None:
    """Delete a local branch. Force-delete by default since evolver
    branches are throwaway anchors, not main-line work."""
    flag = "-D" if force else "-d"
    _run(repo_root, "branch", flag, branch_name)


# ---------------------------------------------------------------------------
# Worktree management (for future eval runs; not used by commit construction)
# ---------------------------------------------------------------------------


def create_worktree(repo_root: Path, target_path: Path, sha: str) -> None:
    """Create a temporary worktree at ``target_path`` checked out at
    ``sha``. Caller must clean up with :func:`remove_worktree`.

    Used by the evaluation driver (later C-series tasks) to get a clean
    checkout of a specific harness version for running tests, without
    disturbing the main working tree.
    """
    _run(repo_root, "worktree", "add", "--detach", str(target_path), sha)


def remove_worktree(repo_root: Path, target_path: Path, *, force: bool = True) -> None:
    """Tear down a worktree previously created by :func:`create_worktree`."""
    args = ["worktree", "remove", str(target_path)]
    if force:
        args.insert(2, "--force")
    _run(repo_root, *args)
    # On rare interrupt cases git may leave the directory; clean up.
    if target_path.exists():
        shutil.rmtree(target_path, ignore_errors=True)


__all__ = [
    "GitOpError",
    "apply_patch_as_commit",
    "commit_files_as_child",
    "worktree_at",
    "branch_exists",
    "commit_exists",
    "create_branch",
    "create_ref",
    "create_worktree",
    "delete_branch",
    "delete_ref",
    "get_commit_message",
    "get_current_sha",
    "get_tree_sha",
    "read_file_at",
    "remove_worktree",
]
