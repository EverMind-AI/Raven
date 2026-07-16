"""Sealed test scoring — the train/test firewall as a mechanism, not a rule.

The SOP's iron law is that the sealed test set never influences an evolution
decision: the loop compares candidates against vanilla on *train* only, and test
is scored blind, its numbers unread until the run ends (retention = test_lift /
train_lift, computed at unseal). Historically this was enforced by discipline.
Here it's enforced by construction:

- :func:`assert_no_test_leak` fails loudly if any test id has crept into the
  anchor or train task sets (an anchor/train that contains test = leakage).
- :class:`SealedTestRunner.score` writes per-task test results to a sealed dir
  the driver never reads and **returns nothing** — there is no path for a test
  number to reach a gate or a verdict. Only :meth:`unseal`, called after the
  loop finishes, reads them back.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from raven.evolver.orchestrator.gates.paired import paired_lift
from raven.evolver.orchestrator.scoring import EvalFn, TaskEval
from raven.evolver.tree.node import HarnessNode

# The sealed scorer shares the loop's EvalFn signature exactly — it *is* the same
# bench scorer (worktree checkout / activation), just invoked with split="test".
SealedEvalFn = EvalFn


class TestLeakError(RuntimeError):
    """Raised when sealed test ids appear in the anchor or train sets."""

    __test__ = False  # not a pytest test class despite the Test* name


def assert_no_test_leak(
    *,
    anchor_task_ids: list[str],
    train_task_ids: list[str],
    sealed_test_ids: list[str],
) -> None:
    """Fail if any sealed test id is present in the anchor or train sets."""
    test = set(sealed_test_ids)
    anchor_leak = sorted(test & set(anchor_task_ids))
    train_leak = sorted(test & set(train_task_ids))
    if anchor_leak or train_leak:
        raise TestLeakError(
            f"sealed test ids leaked into decision sets: "
            f"anchor={anchor_leak} train={train_leak}"
        )


@dataclass
class SealedTestRunner:
    """Scores nodes on the sealed test set into a driver-invisible directory.

    ``eval_fn`` is the loop's own :class:`~raven.evolver.orchestrator.scoring.EvalFn`
    (``(node, task_ids, k, job_name, *, split) -> {task_id: TaskEval}``) — the
    same bench scorer, invoked with ``split="test"``. ``score`` takes a real
    :class:`HarnessNode` (so a worktree eval can check out its commit), writes to
    ``sealed_dir``, and returns None so no test number can enter the decision path.
    """

    eval_fn: SealedEvalFn
    test_task_ids: list[str]
    sealed_dir: Path
    k: int = 3
    split: str = "test"

    def __post_init__(self) -> None:
        self.sealed_dir = Path(self.sealed_dir)

    def score(self, node: HarnessNode, round_index: int) -> None:
        """Blind-score ``node`` on the test set; persist, return nothing."""
        evals = self.eval_fn(
            node, self.test_task_ids, self.k, f"{node.node_id}_sealed_test",
            split=self.split,
        )
        self.sealed_dir.mkdir(parents=True, exist_ok=True)
        # pass@1 over a FIXED denominator = the full test set (SOP §0 hard rule):
        # a test task that produced no result scores 0, never dropped from the
        # denominator (dropping shrinks it and overestimates generalisation).
        n_test = len(self.test_task_ids)
        record = {
            "round": round_index,
            "node_id": node.node_id,
            "k": self.k,
            "per_task": {t: [ev.passes, ev.attempts] for t, ev in evals.items()},
            "pass_at_1": (
                sum(evals[t].pass_rate if t in evals else 0.0 for t in self.test_task_ids)
                / n_test if n_test else 0.0
            ),
        }
        (self.sealed_dir / f"round_{round_index}_{node.node_id}.json").write_text(
            json.dumps(record, indent=2)
        )

    def unseal(self) -> list[dict]:
        """Read all sealed test results (call only after the loop ends)."""
        if not self.sealed_dir.exists():
            return []
        out = []
        for path in sorted(self.sealed_dir.glob("round_*.json")):
            out.append(json.loads(path.read_text()))
        return out


def retention(*, vanilla_train: float, best_train: float,
              vanilla_test: float, best_test: float) -> float | None:
    """Retention = test lift / train lift (generalisation), or None if no train lift."""
    train_lift = best_train - vanilla_train
    if train_lift <= 0:
        return None
    return (best_test - vanilla_test) / train_lift


@dataclass(frozen=True)
class CurvePoint:
    """One (round, harness) point on the train/test generalisation curve."""

    round_index: int
    node_id: str
    train_pass_at_1: float
    test_pass_at_1: float


@dataclass(frozen=True)
class RetentionReport:
    """The C3 deliverable: the train/test curve, the train-selected harness's
    sealed score + paired significance, and retention.

    ``best_*`` is the TRAIN-argmax deliverable (Alg.1 L140: selection spends its
    degrees of freedom on train; its sealed score is a single unbiased
    measurement). The per-round ``curve`` is display/audit only — never pick a
    reported number by scanning it for the highest test score: a max over many
    noisy sealed measurements is inflated by selection (winner's curse).
    ``sealed_z`` / ``sealed_credited_2sigma`` are the paper's credit label,
    from per-task paired diffs deliverable-vs-vanilla on the sealed set
    (None when the deliverable is vanilla itself)."""

    curve: list[CurvePoint] = field(default_factory=list)
    vanilla_train: float = 0.0
    vanilla_test: float = 0.0
    best_round: int = 0
    best_node_id: str = ""
    best_train: float = 0.0
    best_test: float = 0.0
    retention: float | None = None
    sealed_z: float | None = None
    sealed_credited_2sigma: bool | None = None


def _shim_node(node_id: str, sha: str) -> HarnessNode:
    """A minimal node carrying just the identity + commit an eval needs."""
    return HarnessNode(
        node_id=node_id, parent_id=None, git_commit_sha=sha,
        git_branch="sealed", created_at=HarnessNode.utc_now(), created_at_iter=0,
    )


def unseal_retention(
    runner: SealedTestRunner,
    journal_records: list[dict],
    *,
    vanilla_node: HarnessNode,
    vanilla_train: float,
) -> RetentionReport:
    """Post-hoc C3 unseal (approach B): blind-score the vanilla node and each
    round's deliverable (its ``next_parent``, reconstructed from the journal's
    recorded commit SHA) on the sealed test set, build the train/test curve,
    and report the TRAIN-argmax deliverable's sealed score, paired
    significance, and retention.

    All test scoring happens HERE, after evolution finishes — the loop never saw
    a test number (SOP §0 sealed-but-logged, fully-sealed-equivalent: every
    round's harness is a durable commit, so the per-round curve is reconstructed
    without any decision-time test signal). Each distinct node is scored once;
    per-round train pass@1 comes from the journal and is carried forward across
    rounds that did not promote.

    Deliverable selection is by TRAIN score (Alg.1 L140: ``argmax`` over train,
    ties -> earliest round), never by test: picking the max of many noisy sealed
    measurements would inflate the reported gain by selection (winner's curse)
    and void the paper's no-multiple-comparison argument. Overfitting shows up
    as an honestly-low retention, not as a silently swapped deliverable.
    """
    runner.score(vanilla_node, 0)
    scored: set[str] = {vanilla_node.node_id}
    for rec in journal_records:
        nid, sha = rec["next_parent_id"], rec.get("next_parent_sha")
        # "unknown" = the root shim's placeholder in journals written before the
        # loop recorded None; checking it out would crash the whole unseal.
        if nid in scored or not sha or sha == "unknown":
            continue
        runner.score(_shim_node(nid, sha), rec["round_index"])
        scored.add(nid)

    records = runner.unseal()
    test_by_node: dict[str, float] = {d["node_id"]: d["pass_at_1"] for d in records}
    per_task_by_node: dict[str, dict] = {
        d["node_id"]: d.get("per_task", {}) for d in records
    }
    van_test = test_by_node.get(vanilla_node.node_id, 0.0)

    curve: list[CurvePoint] = []
    last_train = vanilla_train
    for rec in journal_records:
        nid = rec["next_parent_id"]
        tr = rec.get("next_parent_train")
        if tr is not None:
            last_train = tr
        curve.append(CurvePoint(
            round_index=rec["round_index"], node_id=nid,
            train_pass_at_1=last_train,
            test_pass_at_1=test_by_node.get(nid, van_test),
        ))

    # Train-argmax over the promoting rounds whose deliverable was measurable
    # (has a recorded train score and a scored commit); ties -> earliest round
    # (same train, fewer stacked patches). Falls back to vanilla when nothing
    # promoted — then there is no lift to report and no paired test to run.
    promoting = [
        rec for rec in journal_records
        if rec.get("next_parent_train") is not None
        and rec["next_parent_id"] in test_by_node
    ]
    if promoting:
        best_rec = max(
            promoting,
            key=lambda r: (r["next_parent_train"], -r["round_index"]),
        )
        best_round, best_node = best_rec["round_index"], best_rec["next_parent_id"]
        best_train, best_test = best_rec["next_parent_train"], test_by_node[best_node]
    else:
        best_round, best_node = 0, vanilla_node.node_id
        best_train, best_test = vanilla_train, van_test

    sealed_z: float | None = None
    sealed_credited: bool | None = None
    if best_node != vanilla_node.node_id and runner.test_task_ids:
        def _evals(nid: str) -> dict[str, TaskEval]:
            return {
                t: TaskEval(t, int(p), int(a))
                for t, (p, a) in per_task_by_node.get(nid, {}).items()
            }

        paired = paired_lift(
            candidate_evals=_evals(best_node),
            control_evals=_evals(vanilla_node.node_id),
            task_ids=list(runner.test_task_ids),
        )
        sealed_z, sealed_credited = paired.z, paired.credited_2sigma

    return RetentionReport(
        curve=curve,
        vanilla_train=vanilla_train, vanilla_test=van_test,
        best_round=best_round, best_node_id=best_node,
        best_train=best_train, best_test=best_test,
        retention=retention(
            vanilla_train=vanilla_train, best_train=best_train,
            vanilla_test=van_test, best_test=best_test,
        ),
        sealed_z=sealed_z,
        sealed_credited_2sigma=sealed_credited,
    )


__all__ = [
    "SealedTestRunner",
    "assert_no_test_leak",
    "TestLeakError",
    "retention",
    "SealedEvalFn",
    "CurvePoint",
    "RetentionReport",
    "unseal_retention",
]
