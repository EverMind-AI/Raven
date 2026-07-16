"""AppWorld eval + candidate representation for the in-package evolution.

A candidate is the driver's edited harness files (``Candidate.files`` =
``{repo-rel path: bytes}``); ``make_git_commit_apply_fn`` (with ``files_of``)
turns them into a real child commit. Eval then checks that commit out into an
ephemeral worktree and runs ``batch.py`` with ``cwd=worktree`` so the candidate's
committed harness is what imports — no writing into the live repo (the
zero-contamination replacement for RealPathSync).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from benchmarks.appworld.evolve import adapter as aw_adapter
from raven.evolver.tree import git_ops


@dataclass
class Candidate:
    """One candidate harness edit produced by the bash-editor design step.

    ``has_beacon`` marks a code edit carrying an ``activation_beacon`` call —
    only those get Gate-b per-task attribution (prompt/config edits have no
    code execution point and fail open). ``activation_spec`` is the driver's
    optional self-declared trigger predicate, consumed by the zero-hit
    preflight.
    """

    files: dict[str, bytes]          # full new bytes for each edited repo-rel path
    why: str                          # the WHY this candidate targets
    focused_task_ids: list[str] = field(default_factory=list)  # WHY's evidence subset
    summary: str = ""                 # the editor's one-line "what I changed"
    deletions: list[str] = field(default_factory=list)  # repo-rel paths removed
    has_beacon: bool = False
    activation_spec: dict | None = None


def files_of(cand: Candidate) -> dict[str, bytes]:
    """Extract the edited file bytes for ``make_git_commit_apply_fn``."""
    return cand.files


def deletions_of(cand: Candidate) -> list[str]:
    """Extract the deleted paths for ``make_git_commit_apply_fn``."""
    return cand.deletions


def make_appworld_eval_fn(aw: "aw_adapter.AppWorldConfig", repo_root: str | Path):
    """Eval a node by checking its commit out into a worktree and running
    ``batch.py`` there (``cwd=worktree``). No activation env, no live-repo writes.
    """
    root = Path(repo_root)

    def eval_fn(node, task_ids, k, job_name, *, split="train"):
        cfg = aw
        if split != aw.split:
            from dataclasses import replace
            cfg = replace(aw, split=split)
        with git_ops.worktree_at(root, node.git_commit_sha) as wt:
            return aw_adapter.run_eval(
                cfg, K=k, experiment=job_name, task_ids=task_ids, cwd=wt
            )

    return eval_fn


__all__ = ["Candidate", "files_of", "deletions_of", "make_appworld_eval_fn"]
