"""The bench plugin contract: what a benchmark implements to become runnable.

A bench module exposes ``build(ctx: LaunchContext) -> BenchBundle``. The
bundle is pure wiring — nothing expensive happens until the runner calls the
closures, so ``status`` can build a bundle just to count artifacts.

What a new bench must bring (see docs/specs/evolve-bench-contract.md):
scorer subprocess + result files with an infra marker, a result->TaskEval
reader, per-attempt trajectory files, a train/test split, and an editable-path
whitelist for its subject repo. Everything else (funnel, gates, sealed test,
resume) is the shared loop.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

from raven.evolver.launch.config import RunSpec
from raven.evolver.launch.models import CallFn


@dataclass(frozen=True)
class LaunchContext:
    spec: RunSpec
    models: dict[str, Optional[CallFn]]

    @property
    def smoke(self) -> bool:
        return self.spec.smoke


@dataclass
class BenchBundle:
    """Everything the runner's state machine needs, all lazily evaluated.

    ``cold_start_done``/``run_cold_start`` must be idempotent at trial
    granularity: re-invocation only fills missing trials. The runner invokes
    ``run_cold_start`` on every run — including when all base trials exist —
    so any infra-salvage rerun the bench owes (SOP §0 ladder) belongs inside
    it, not behind the done-count. ``unseal`` receives
    the journal records plus the built orchestrator and returns a plain-dict
    report; None means the bench has no sealed test set configured.
    ``precheck`` (optional) raises RuntimeError with an actionable message
    when the environment cannot support a run (dead subject endpoint, bound
    ports, missing install); ``check`` invokes it so environment problems
    surface before any trial is paid for, not at cold start.
    """

    root_node_id: str
    root_node: Any
    journal_path: Path
    cold_start_total: int
    cold_start_done: Callable[[], int]
    run_cold_start: Callable[[], None]
    build_orchestrator: Callable[[], Any]
    unseal: Optional[Callable[[list[dict], Any], dict]] = None
    precheck: Optional[Callable[[], None]] = None


def validate_whitelist(
    repo_root: Path, base_sha: str, prefixes: tuple[str, ...]
) -> None:
    """Fail loudly on a whitelist prefix matching nothing at ``base_sha``.

    A dead prefix does not error at run time — the designer's edits are
    silently reverted as out-of-whitelist and every candidate arrives empty,
    which once cost a full run. Refusing to start is the only honest behavior.
    """
    if not prefixes:
        raise ValueError("whitelist is empty: the designer would have no editable surface")
    proc = subprocess.run(
        ["git", "ls-tree", "-r", "--name-only", base_sha],
        cwd=str(repo_root), capture_output=True, text=True,
    )
    if proc.returncode != 0:
        raise ValueError(
            f"cannot list {base_sha} in {repo_root}: {proc.stderr.strip()}"
        )
    paths = proc.stdout.splitlines()
    dead = [p for p in prefixes if not any(f.startswith(p) for f in paths)]
    if dead:
        raise ValueError(
            f"whitelist prefixes match no files at {base_sha[:12]}: {dead} — "
            "designer edits would be silently dropped; fix the prefixes "
            "(or the base_sha) before running"
        )


__all__ = ["BenchBundle", "LaunchContext", "validate_whitelist"]
