"""The evolver's durable ledgers are written through the atomic helper."""

from __future__ import annotations

import logging
from pathlib import Path
from types import SimpleNamespace

from evolver.orchestrator import loop
from evolver.orchestrator.gates.policy import CandidateOutcome
from evolver.orchestrator.loop import EvolutionOrchestrator, RoundResult
from evolver.tree.node import HarnessNode, NodeStatus
from raven.utils import atomic_io


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


def test_failed_failure_map_write_keeps_the_previous_file(tmp_path: Path, monkeypatch, caplog):
    path = tmp_path / "failure_map.json"
    path.write_text('{"old": 1}', encoding="utf-8")
    orch = SimpleNamespace(_cfg=SimpleNamespace(failure_map_path=path), _failure_map={"new": 2})
    replaced: list[Path] = []

    def sharing_violation(src, dst):
        replaced.append(dst)
        raise PermissionError("sharing violation")

    monkeypatch.setattr(atomic_io.os, "replace", sharing_violation)

    with caplog.at_level(logging.WARNING, logger=loop.__name__):
        EvolutionOrchestrator._persist_failure_map(orch)

    assert len(replaced) == atomic_io._REPLACE_ATTEMPTS
    assert all(dst == path for dst in replaced)
    assert path.read_text(encoding="utf-8") == '{"old": 1}'
    assert not list(tmp_path.glob("*.tmp"))
    assert str(path) in caplog.text


def test_failed_node_ledger_write_names_the_node(tmp_path: Path, monkeypatch, caplog):
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

    def sharing_violation(src, dst):
        raise PermissionError("sharing violation")

    monkeypatch.setattr(atomic_io.os, "replace", sharing_violation)

    with caplog.at_level(logging.WARNING, logger=loop.__name__):
        EvolutionOrchestrator._persist_node_records(orch, rr)

    ledger = tmp_path / "nodes" / "n1.json"
    assert str(ledger) in caplog.text
    assert not ledger.exists()
    assert not list((tmp_path / "nodes").glob("*.tmp"))
