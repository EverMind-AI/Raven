"""Which runs a placed run turned into, and how a linked Task is doing now.

Two kinds of run start with no tool call of their own: a replan's successor and
every round after the first of a stint. Neither is in any turn's dispatch list,
so neither is placed by the model; each belongs wherever the run it continues
was placed. Both answers are read off disk -- the run directories, the stint
records and the session's node registry -- so they survive a restart and need no
live tool.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from raven.agent.subagent.dag_store import REGISTRY_FILENAME, RUNNING
from raven.agent.subagent.history import dag_root, session_history_root
from raven.agent.task_board.board import Board, TaskLink
from raven.stint.record import STINTS_DIRNAME, StintStore

_FAILED = frozenset({"failed", "exception", "cancelled", "interrupted"})
_SETTLED = frozenset({"completed", "skipped", "not_executed"})


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _successor(runs: Path, run_id: str) -> str | None:
    graph = _read_json(runs / run_id / "graph.json")
    replan = graph.get("replan") if isinstance(graph, dict) else None
    successor = replan.get("run_id") if isinstance(replan, dict) else None
    return str(successor) if successor else None


def descendants(session_dir: Path, placed_runs: Iterable[str]) -> dict[str, str]:
    """Every run descended from a placed run, mapped to the placed run it descends from."""
    runs = dag_root(Path(session_dir))
    origin = {run_id: run_id for run_id in placed_runs}
    found: dict[str, str] = {}

    def follow(run_id: str, ancestor: str) -> None:
        current = _successor(runs, run_id)
        while current and current not in origin:
            origin[current] = found[current] = ancestor
            current = _successor(runs, current)

    for run_id in list(origin):
        follow(run_id, run_id)
    for record in StintStore(runs / STINTS_DIRNAME).list():
        ordered = [r.run_id for r in sorted(record.rounds, key=lambda r: r.index) if r.run_id]
        if not ordered or ordered[0] not in origin:
            continue
        ancestor = origin[ordered[0]]
        for run_id in ordered[1:]:
            if run_id not in origin:
                origin[run_id] = found[run_id] = ancestor
                follow(run_id, ancestor)
    return found


def board_lineage(session_dir: Path, board: Board) -> dict[str, str]:
    """The descendants of every run the board places, as the page and recall both read them."""
    placed = [t.id for g in board.groups for s in g.steps for t in s.tasks if t.kind == "dag"]
    return descendants(session_dir, placed) if placed else {}


def task_status(session_dir: Path, link: TaskLink) -> str:
    """A linked Task's status as the session's node registry has it; ``unknown`` when it has none."""
    data = _read_json(session_history_root(Path(session_dir)) / REGISTRY_FILENAME)
    nodes = data.get("nodes") if isinstance(data, dict) else None
    if not isinstance(nodes, dict):
        return "unknown"
    if link.kind == "spawn":
        row = nodes.get(link.id)
        return str(row["status"]) if isinstance(row, dict) and row.get("status") else "unknown"
    statuses = [str(r.get("status")) for r in nodes.values() if isinstance(r, dict) and r.get("run_id") == link.id]
    if not statuses:
        return "unknown"
    if any(s in (RUNNING, "pending") for s in statuses):
        return "running"
    if any(s in _FAILED for s in statuses):
        return "failed"
    if all(s in _SETTLED for s in statuses):
        return "completed"
    return statuses[0]
