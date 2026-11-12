"""The evolver's durable artifacts survive an interrupted write (#1220)."""

from __future__ import annotations

import json
from pathlib import Path

from evolver.analysis.stability_bucket import StabilityBucket, TaskStability
from evolver.orchestrator.config import OrchestratorConfig
from evolver.orchestrator.loop import EvolutionOrchestrator
from evolver.tree.node import HarnessNode, NodeStatus


class _Backend:
    """An eval backend that is never asked for anything here."""

    train_task_ids: list[str] = ["task-1"]
    test_task_ids: list[str] = []

    def eval(self, *args, **kwargs):  # pragma: no cover - not reached
        raise AssertionError("not used")

    def cold_start(self):
        return {
            "task-1": TaskStability(
                task_id="task-1",
                attempts=1,
                passes=1,
                bucket=StabilityBucket.STABLE_PASS,
            )
        }


def _orchestrator(work_dir: Path) -> EvolutionOrchestrator:
    config = OrchestratorConfig(
        repo_root=work_dir,
        work_dir=work_dir / "work",
        driver_llm_spec={},
    )
    return EvolutionOrchestrator(
        config,
        backend=_Backend(),  # type: ignore[arg-type]
        diagnose_fn=lambda round_index, parent: {},
        design_fn=lambda round_index, failure_map, parent: [],
        apply_fn=lambda parent_id, patch, round_index: None,  # type: ignore[arg-type,return-value]
    )


def test_a_failed_failure_map_write_keeps_the_previous_file(tmp_path, monkeypatch):
    orchestrator = _orchestrator(tmp_path)
    orchestrator._failure_map = {"WHY_a": {"count": 1}}
    orchestrator._persist_failure_map()

    path = orchestrator._cfg.failure_map_path
    assert json.loads(path.read_text()) == {"WHY_a": {"count": 1}}

    # The write lands through a temp file and a rename: a failure at the
    # rename must leave the previous file intact, not a truncated one.
    orchestrator._failure_map = {"WHY_a": {"count": 1}, "WHY_b": {"count": 2}}
    monkeypatch.setattr(
        "raven.utils.atomic_io.os.replace",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("disk full")),
    )
    orchestrator._persist_failure_map()

    assert json.loads(path.read_text()) == {"WHY_a": {"count": 1}}


def test_a_failed_node_record_write_keeps_the_previous_file(tmp_path, monkeypatch):
    from evolver.orchestrator.loop import CandidateOutcome, RoundResult

    orchestrator = _orchestrator(tmp_path)
    node = HarnessNode(
        node_id="node-1",
        parent_id=None,
        git_commit_sha="abc123",
        git_branch="main",
        created_at=0,
        created_at_iter=0,
        status=NodeStatus.active,
    )
    orchestrator._node_registry["node-1"] = node

    outcome = CandidateOutcome(node_id="node-1", status=NodeStatus.active)
    round_result = RoundResult(
        round_index=0,
        parent_id="root",
        next_parent_id="root",
        promoted=False,
        outcomes=[outcome],
    )
    orchestrator._persist_node_records(round_result)

    path = orchestrator._cfg.nodes_dir / "node-1.json"
    assert json.loads(path.read_text())["node_id"] == "node-1"
    assert json.loads(path.read_text())["status"] == NodeStatus.active.value

    # A second round with a different outcome must not leave a half-written
    # record behind: the loader skips an unreadable node file, so the node
    # would disappear from the audit tree.
    second = RoundResult(
        round_index=1,
        parent_id="root",
        next_parent_id="root",
        promoted=False,
        outcomes=[CandidateOutcome(node_id="node-1", status=NodeStatus.pruned_at_screen)],
    )
    monkeypatch.setattr(
        "raven.utils.atomic_io.os.replace",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("disk full")),
    )
    orchestrator._persist_node_records(second)

    assert json.loads(path.read_text())["status"] == NodeStatus.active.value
