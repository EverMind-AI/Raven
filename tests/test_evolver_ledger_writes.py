"""The evolver's durable ledgers are written through the atomic helper."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from evolver.orchestrator import loop
from evolver.orchestrator.gates.policy import CandidateOutcome
from evolver.orchestrator.loop import EvolutionOrchestrator, RoundResult
from evolver.tree.node import HarnessNode, NodeStatus


def test_failure_map_uses_atomic_replace(tmp_path: Path, monkeypatch):
    calls: list[tuple[Path, str]] = []
    monkeypatch.setattr(loop, "atomic_replace", lambda path, data: calls.append((Path(path), data)))
    path = tmp_path / "failure_map.json"
    orch = SimpleNamespace(_cfg=SimpleNamespace(failure_map_path=path), _failure_map={"a": 1})

    EvolutionOrchestrator._persist_failure_map(orch)

    assert [p for p, _ in calls] == [path]
    assert '"a": 1' in calls[0][1]


def test_node_ledger_uses_atomic_replace(tmp_path: Path, monkeypatch):
    calls: list[tuple[Path, str]] = []
    monkeypatch.setattr(loop, "atomic_replace", lambda path, data: calls.append((Path(path), data)))
    node = HarnessNode(
        node_id="n1",
        parent_id="root",
        git_commit_sha="deadbeef",
        git_branch="",
        created_at=HarnessNode.utc_now(),
        created_at_iter=1,
    )
    rr = RoundResult(
        round_index=1,
        parent_id="root",
        next_parent_id="n1",
        promoted=True,
        outcomes=[CandidateOutcome("n1", NodeStatus.promoted_to_baseline, score=0.5)],
    )
    orch = SimpleNamespace(
        _cfg=SimpleNamespace(nodes_dir=tmp_path / "nodes"),
        _node_registry={"n1": node},
        _cand_meta={},
    )

    EvolutionOrchestrator._persist_node_records(orch, rr)

    assert [p.name for p, _ in calls] == ["n1.json"]
    assert '"node_id": "n1"' in calls[0][1]


def test_failed_failure_map_write_keeps_the_previous_file(tmp_path: Path, monkeypatch):
    path = tmp_path / "failure_map.json"
    path.write_text('{"old": 1}', encoding="utf-8")
    orch = SimpleNamespace(_cfg=SimpleNamespace(failure_map_path=path), _failure_map={"new": 2})

    def boom(path, data):
        raise OSError("disk full")

    monkeypatch.setattr(loop, "atomic_replace", boom)

    EvolutionOrchestrator._persist_failure_map(orch)

    assert path.read_text(encoding="utf-8") == '{"old": 1}'
