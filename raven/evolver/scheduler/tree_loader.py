"""Load an evolver tree (HarnessNode JSON files) into a
:class:`TreeAwareTaskScheduler`.

Bridge between the durable tree representation (
:mod:`raven.evolver.tree.store`) and the in-memory bandit
(:mod:`raven.evolver.scheduler.tree_aware_bandit`). This is the
canonical loader used for paper §15 #2 tree-aware bandit experiments
and for any future round's task subset selection.

Reads three kinds of outcome data from each :class:`HarnessNode`:

1. **Primary eval** — ``node.eval.per_task_results`` (single pass/fail
   per task).
2. **Multi-attempt replay** — when ``node.eval.dense_signals`` carries
   ``k_attempt_replay_dir``, the loader walks that trial directory and
   replays each per-attempt outcome (used for the v7 root's k=3 paired
   baseline so the bandit sees three independent observations per
   task instead of a single union).
3. **Secondary evals** — when ``dense_signals`` carries
   ``secondary_eval_<i>_path``, the loader reads the referenced JSON
   file and adds those outcomes (used for round-1 validation runs:
   11-borderline + rescue-retest-k=3).

Secondary eval formats supported (selected by
``secondary_eval_<i>_format``):

- ``task_dict_passed`` — ``{task_id: {passed: bool, ...}}`` (11-borderline)
- ``rescue_k3_attempts`` — ``{task: ..., attempts: [{passed: bool, ...}, ...]}`` (rescue retest)
- ``per_task_passed_block`` — ``{task: {passed: bool, ...}}`` (round 1 first_round_eval child block)
"""

from __future__ import annotations

import json
import logging
from collections import deque
from pathlib import Path
from typing import Optional

from raven.evolver.scheduler.tree_aware_bandit import TreeAwareTaskScheduler
from raven.evolver.tree.node import HarnessNode
from raven.evolver.tree.store import EvolverTreeStore


logger = logging.getLogger(__name__)


def load_tree_into_scheduler(
    nodes_dir: Path,
    *,
    repo_root: Optional[Path] = None,
    all_task_ids: Optional[list[str]] = None,
    ancestry_lambda: float = 0.7,
    exploration_weight: float = 1.0,
    rng_seed: Optional[int] = 42,
    skip_multi_attempt_replay: bool = False,
) -> TreeAwareTaskScheduler:
    """Build a fully populated :class:`TreeAwareTaskScheduler` by reading
    every :class:`HarnessNode` JSON in ``nodes_dir`` + their referenced
    outcome data files.

    Parameters
    ----------
    nodes_dir
        Directory containing ``<node_id>.json`` files (e.g.
        ``REPO_ROOT/evolver/nodes/``).
    repo_root
        Repository root. Used to resolve relative paths in
        ``dense_signals``. Defaults to ``nodes_dir.parent.parent``.
    all_task_ids
        Task pool the scheduler manages. Defaults to ``bandit_tasks_chosen``
        from the root node (typically all 89 TB2 tasks).
    ancestry_lambda / exploration_weight / rng_seed
        Forwarded to :class:`TreeAwareTaskScheduler`.
    skip_multi_attempt_replay
        If True, don't walk ``k_attempt_replay_dir`` even if present. The
        primary union outcomes get added instead. Used for fast tests.

    Returns
    -------
    A scheduler with the full tree topology registered and all known
    outcomes replayed. Ready for ``scheduler.choose(v_new_id, K=...)``.
    """
    repo_root = repo_root or nodes_dir.parent.parent
    store = EvolverTreeStore(repo_root=repo_root, nodes_dir=nodes_dir)
    nodes = store.load_all_nodes()
    if not nodes:
        raise ValueError(f"No HarnessNode JSONs found in {nodes_dir}")

    archived = [n for n in nodes if n.status.value.startswith("archived")]
    if archived:
        logger.info(
            "Excluded %d archived node(s) from scheduler: %s",
            len(archived),
            [n.node_id for n in archived],
        )
        nodes = [n for n in nodes if not n.status.value.startswith("archived")]
    if not nodes:
        raise ValueError(
            f"No non-archived HarnessNode JSONs found in {nodes_dir}"
        )

    node_by_id: dict[str, HarnessNode] = {n.node_id: n for n in nodes}

    # ── Determine the task pool ──
    if all_task_ids is None:
        # Use the root's bandit_tasks_chosen as the canonical pool
        roots = [n for n in nodes if n.parent_id is None]
        if len(roots) != 1:
            raise ValueError(
                f"Expected exactly one root node, found {len(roots)}: "
                f"{[r.node_id for r in roots]}"
            )
        root = roots[0]
        if root.eval is None:
            raise ValueError(
                f"Root node {root.node_id!r} must carry an eval to define "
                f"the task pool. Pass all_task_ids explicitly if intentional."
            )
        all_task_ids = list(root.eval.bandit_tasks_chosen)

    scheduler = TreeAwareTaskScheduler(
        all_task_ids=all_task_ids,
        ancestry_lambda=ancestry_lambda,
        exploration_weight=exploration_weight,
        rng_seed=rng_seed,
    )

    # ── Topological order — parents before children ──
    for node in _topological_order(nodes):
        scheduler.add_node(node.node_id, parent_id=node.parent_id)
        _record_outcomes(
            scheduler=scheduler,
            node=node,
            repo_root=repo_root,
            skip_multi_attempt_replay=skip_multi_attempt_replay,
        )

    logger.info(
        "Loaded tree from %s: %d nodes, %d observations",
        nodes_dir, scheduler.n_nodes(), scheduler.n_observations(),
    )
    return scheduler


def _topological_order(nodes: list[HarnessNode]) -> list[HarnessNode]:
    """Return nodes ordered so every node's ``parent_id`` appears before
    it. Uses BFS from roots; raises on cycles or orphan parents."""
    by_id = {n.node_id: n for n in nodes}

    # Pre-check: every non-root node must reference a parent that exists
    # in the node set. (Otherwise BFS would silently skip it and fall
    # through to the "possible cycle" branch with a less useful message.)
    for n in nodes:
        if n.parent_id is not None and n.parent_id not in by_id:
            raise ValueError(
                f"Node {n.node_id!r} references parent_id "
                f"{n.parent_id!r} which is not in the node set"
            )

    children_of: dict[Optional[str], list[HarnessNode]] = {}
    for n in nodes:
        children_of.setdefault(n.parent_id, []).append(n)

    roots = children_of.get(None, [])
    if not roots:
        raise ValueError("No root node (parent_id=None) found")

    seen: set[str] = set()
    ordered: list[HarnessNode] = []
    queue: deque[HarnessNode] = deque(roots)
    while queue:
        n = queue.popleft()
        if n.node_id in seen:
            continue
        seen.add(n.node_id)
        ordered.append(n)
        for child in children_of.get(n.node_id, []):
            queue.append(child)
    if len(ordered) != len(nodes):
        missing = {n.node_id for n in nodes} - seen
        raise ValueError(
            f"Topological sort incomplete — possible cycle. Missing: {missing}"
        )
    return ordered


def _record_outcomes(
    *,
    scheduler: TreeAwareTaskScheduler,
    node: HarnessNode,
    repo_root: Path,
    skip_multi_attempt_replay: bool,
) -> None:
    """Replay all known outcomes for ``node`` into ``scheduler``."""
    if node.eval is None:
        return

    dense = node.eval.dense_signals or {}

    # ── 1. Multi-attempt replay (root k=3 case) ──
    replay_dir_rel = dense.get("k_attempt_replay_dir")
    if (
        isinstance(replay_dir_rel, str)
        and replay_dir_rel
        and not skip_multi_attempt_replay
    ):
        replay_dir = repo_root / replay_dir_rel
        if replay_dir.is_dir():
            _replay_k_attempts(
                scheduler=scheduler,
                node_id=node.node_id,
                trial_dir=replay_dir,
                task_pool=set(scheduler._task_ids),
            )
            # Skip the per_task_results union — we just replayed every attempt
        else:
            logger.warning(
                "node %s: k_attempt_replay_dir does not exist: %s — "
                "falling back to per_task_results union",
                node.node_id, replay_dir,
            )
            _replay_primary(scheduler, node)
    else:
        # ── 1b. Primary eval (single attempt per task) ──
        _replay_primary(scheduler, node)

    # ── 2. Secondary evals ──
    n_secondary = int(dense.get("secondary_eval_count", 0) or 0)
    for i in range(n_secondary):
        label = dense.get(f"secondary_eval_{i}_label")
        path_rel = dense.get(f"secondary_eval_{i}_path")
        fmt = dense.get(f"secondary_eval_{i}_format")
        if not (isinstance(path_rel, str) and isinstance(fmt, str)):
            logger.warning(
                "node %s secondary_eval_%d missing path/format — skipping",
                node.node_id, i,
            )
            continue
        path = repo_root / path_rel
        if not path.is_file():
            logger.warning(
                "node %s secondary_eval_%d path missing: %s — skipping",
                node.node_id, i, path,
            )
            continue
        try:
            data = json.loads(path.read_text())
        except json.JSONDecodeError:
            logger.warning(
                "node %s secondary_eval_%d not valid JSON: %s — skipping",
                node.node_id, i, path,
            )
            continue
        _apply_secondary_format(scheduler, node.node_id, label, fmt, data)


def _replay_primary(scheduler: TreeAwareTaskScheduler, node: HarnessNode) -> None:
    """Add the primary eval's per_task_results as one outcome each."""
    assert node.eval is not None
    pool = set(scheduler._task_ids)
    for task_id, result in node.eval.per_task_results.items():
        if task_id not in pool:
            logger.debug(
                "node %s: per_task task %s not in task pool, skipping",
                node.node_id, task_id,
            )
            continue
        scheduler.add_outcome(node.node_id, task_id, result.pass_outcome)


def _replay_k_attempts(
    *,
    scheduler: TreeAwareTaskScheduler,
    node_id: str,
    trial_dir: Path,
    task_pool: set[str],
) -> None:
    """Walk a legacy-runner trial dir (each subdir = one trial = one task ×
    attempt) and replay each attempt as a separate outcome."""
    n_replayed = 0
    for result_json in trial_dir.glob("*/result.json"):
        try:
            d = json.loads(result_json.read_text())
        except json.JSONDecodeError:
            continue
        task_name = d.get("task_name")
        if not task_name or task_name not in task_pool:
            continue
        reward = (d.get("verifier_result") or {}).get("rewards", {}).get("reward", 0.0)
        passed = bool(reward) and reward > 0
        scheduler.add_outcome(node_id, task_name, passed)
        n_replayed += 1
    logger.info(
        "node %s: replayed %d k-attempt outcomes from %s",
        node_id, n_replayed, trial_dir,
    )


def _apply_secondary_format(
    scheduler: TreeAwareTaskScheduler,
    node_id: str,
    label: Optional[str],
    fmt: str,
    data: dict,
) -> None:
    pool = set(scheduler._task_ids)
    n_added = 0
    if fmt == "task_dict_passed":
        # {task_id: {passed: bool, ...}}
        for task_id, v in data.items():
            if task_id not in pool:
                continue
            if isinstance(v, dict) and "passed" in v:
                scheduler.add_outcome(node_id, task_id, bool(v["passed"]))
                n_added += 1
    elif fmt == "rescue_k3_attempts":
        # {task: <task_id>, attempts: [{passed: bool, ...}, ...]}
        task_id = data.get("task")
        if task_id and task_id in pool:
            for attempt in data.get("attempts", []):
                if isinstance(attempt, dict) and "passed" in attempt:
                    scheduler.add_outcome(node_id, task_id, bool(attempt["passed"]))
                    n_added += 1
    elif fmt == "per_task_passed_block":
        # {task_id: {passed: bool, ...}} (same shape as task_dict_passed)
        for task_id, v in data.items():
            if task_id not in pool:
                continue
            if isinstance(v, dict) and "passed" in v:
                scheduler.add_outcome(node_id, task_id, bool(v["passed"]))
                n_added += 1
    else:
        logger.warning(
            "node %s secondary_eval %r unknown format: %r — skipping",
            node_id, label, fmt,
        )
        return
    logger.info(
        "node %s secondary_eval %r (%s): added %d outcomes",
        node_id, label, fmt, n_added,
    )


__all__ = ["load_tree_into_scheduler"]
