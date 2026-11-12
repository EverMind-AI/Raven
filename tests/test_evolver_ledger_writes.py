"""The evolver's durable ledgers are written through the atomic helper."""

from __future__ import annotations

import json
import logging
import threading
from pathlib import Path
from types import SimpleNamespace

from evolver.orchestrator import loop
from evolver.orchestrator.gates.policy import CandidateOutcome
from evolver.orchestrator.loop import EvolutionOrchestrator, RoundResult
from evolver.tree import node as node_module
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


def _node() -> HarnessNode:
    return HarnessNode(
        node_id="n1",
        parent_id="root",
        git_commit_sha="deadbeef",
        git_branch="",
        created_at=HarnessNode.utc_now(),
        created_at_iter=1,
    )


def test_node_save_uses_the_ledger_helper(tmp_path: Path, monkeypatch):
    calls: list[tuple[Path, str]] = []
    monkeypatch.setattr(node_module, "atomic_replace", lambda path, data: calls.append((Path(path), data)))

    _node().save(tmp_path / "nodes" / "n1.json")

    assert [p.name for p, _ in calls] == ["n1.json"]
    assert '"node_id": "n1"' in calls[0][1]


def test_node_save_does_not_eat_an_in_flight_ledger_temp(tmp_path: Path):
    """Both writers name their temp ``n1.json.tmp``. An unlocked save deleted
    the helper's temp mid-write and the helper's own rename then failed; the
    save must now wait for the sidecar lock and leave that temp alone."""
    path = tmp_path / "nodes" / "n1.json"
    path.parent.mkdir(parents=True)
    path.write_text('{"ledger": 1}', encoding="utf-8")
    in_flight = path.with_name(path.name + ".tmp")
    in_flight.write_text('{"ledger": ', encoding="utf-8")
    started = threading.Event()

    def save():
        started.set()
        _node().save(path)

    with atomic_io.write_transaction(path):
        saver = threading.Thread(target=save, daemon=True)
        saver.start()
        assert started.wait(10)
        saver.join(0.5)
        assert saver.is_alive()
        assert in_flight.read_text(encoding="utf-8") == '{"ledger": '
    saver.join(10)

    assert not saver.is_alive()
    assert json.loads(path.read_text(encoding="utf-8"))["node_id"] == "n1"
    assert not list(path.parent.glob("*.tmp"))


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

    # Literal, not the constant: asserting against it stays green when the
    # budget is cut, and only "all five attempts" is the behaviour under test.
    assert len(replaced) == 5
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
