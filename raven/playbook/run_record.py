"""Per-turn evidence used to compile and audit durable Playbooks."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Iterator
from uuid import uuid4

if TYPE_CHECKING:
    from collections.abc import Mapping

from raven.agent.subagent.dag_graph import DagNodeSpec, SubAgentDagSpec
from raven.agent.subagent.delegate import Worker
from raven.playbook.agent_spec import AgentPlaybookSpec, DelegateEntry


def _now() -> str:
    return datetime.now(UTC).isoformat()


@dataclass
class DagEvidence:
    run_id: str
    spec: SubAgentDagSpec
    harness: AgentPlaybookSpec | None = None
    """The run's frozen Harness when a reassignment extended it with a derived role.

    ``None`` for a run that moved nothing: the turn's own Harness describes it
    exactly, and a record carries no second copy of it.
    """

    attempt_agents: dict[str, list[str]] | None = None
    """Every node's started attempts, in order, as the audit trail keeps them.

    Factual rather than replay advice: a node captured under the agent its
    session ended on still lists the agent each attempt actually ran on.
    """

    def as_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "runId": self.run_id,
            "spec": self.spec.model_dump(by_alias=True, exclude_none=True),
        }
        if self.harness is not None:
            payload["harness"] = self.harness.model_dump(by_alias=True, exclude_none=True)
        if self.attempt_agents:
            payload["attemptAgents"] = {node_id: list(agents) for node_id, agents in self.attempt_agents.items()}
        return payload


@dataclass
class PlaybookRunCapture:
    """The minimum replay evidence from one enabled Playbook turn."""

    query: str
    disposition: str = "none"
    selected_playbook: str | None = None
    artifact_name: str | None = None
    capture_workflow: bool = False
    run_id: str = field(
        default_factory=lambda: f"pb-{datetime.now(UTC).strftime('%Y%m%dT%H%M%S%fZ')}-{uuid4().hex[:8]}"
    )
    started_at: str = field(default_factory=_now)
    completed_at: str | None = None
    status: str = "running"
    dags: list[DagEvidence] = field(default_factory=list)
    saved_playbook: str | None = None
    error: str | None = None

    def finish(self, *, status: str = "completed", error: str | None = None) -> None:
        self.completed_at = _now()
        self.status = status
        self.error = error

    def as_dict(self) -> dict[str, Any]:
        return {
            "runId": self.run_id,
            "queryDigest": hashlib.sha256(self.query.encode("utf-8")).hexdigest(),
            "disposition": self.disposition,
            "selectedPlaybook": self.selected_playbook,
            "artifactName": self.artifact_name,
            "captureWorkflow": self.capture_workflow,
            "startedAt": self.started_at,
            "completedAt": self.completed_at,
            "status": self.status,
            "dags": [dag.as_dict() for dag in self.dags],
            "savedPlaybook": self.saved_playbook,
            "error": self.error,
        }


_CAPTURE: ContextVar[PlaybookRunCapture | None] = ContextVar("playbook_run_capture", default=None)


@contextmanager
def capture_scope(capture: PlaybookRunCapture | None) -> Iterator[None]:
    token = _CAPTURE.set(capture)
    try:
        yield
    finally:
        _CAPTURE.reset(token)


def current_capture() -> PlaybookRunCapture | None:
    """Return the capture object inherited by the current turn task."""
    return _CAPTURE.get()


def workflow_capture_requested() -> bool:
    """Whether this turn must wait for a successful DAG before promotion."""
    capture = _CAPTURE.get()
    return bool(capture is not None and capture.capture_workflow)


def record_completed_dag(
    spec: SubAgentDagSpec,
    run_id: str,
    capture: PlaybookRunCapture | None = None,
    *,
    harness: AgentPlaybookSpec | None = None,
    attempt_agents: "Mapping[str, list[str]] | None" = None,
) -> None:
    """Record a graph only after every node completed successfully."""
    capture = capture or _CAPTURE.get()
    if capture is not None:
        capture.dags.append(
            DagEvidence(
                run_id=run_id,
                spec=spec,
                harness=harness,
                attempt_agents=(
                    {node_id: list(agents) for node_id, agents in attempt_agents.items()} if attempt_agents else None
                ),
            )
        )


_REASSIGN_LABEL = "{label}--reassign-{n}"


def _derived_label(original: str, target: str, occupied: set[str], cache: dict[tuple[str, str], str]) -> str:
    """A collision-free label for one ``(role, finishing agent)`` pair.

    Cached so two nodes of one role that moved to the same agent share a row,
    and derived against every label already in play so a Harness that happens to
    carry ``<role>--reassign-1`` still yields a name no row answers to.
    """
    key = (original, target)
    if (cached := cache.get(key)) is not None:
        return cached
    attempt = 1
    while (label := _REASSIGN_LABEL.format(label=original, n=attempt)) in occupied:
        attempt += 1
    occupied.add(label)
    cache[key] = label
    return label


def _final_pair(
    node: DagNodeSpec,
    worker: Worker | None,
    groups: "Mapping[str, tuple[str, str]]",
    execution_nodes: "Mapping[str, DagNodeSpec]",
    instance_assignments: "Mapping[tuple[str, str], tuple[str, str | None]]",
) -> tuple[str, str | None]:
    """The agent and handle a completed node's replay should open.

    A graph-named group is one session, so every member -- including a sibling
    that finished before the move -- is recorded under the pair the handle ended
    the run on. An ordinary node records the agent it would dispatch as, and
    keeps the handle the graph gave it (which is none for a minted one).

    A node absent from the effective graph keeps its own submitted identity.
    The audit history is deliberately not a fallback: the last attempt is not
    where a session necessarily ended, and reading it here would let a group
    sibling replay on the agent that finished before the move.
    """
    resolved = worker.agent if worker is not None else node.subagent
    group = groups.get(node.id)
    if group is not None:
        return instance_assignments.get(group, (resolved, node.instance))
    effective = execution_nodes.get(node.id)
    if effective is not None and effective.subagent:
        return (effective.subagent, node.instance)
    return (resolved, node.instance)


def rewrite_completed_dag(
    submitted: SubAgentDagSpec,
    *,
    harness: AgentPlaybookSpec | None,
    workers: "Mapping[str, Worker]",
    named_instance_groups: "Mapping[str, tuple[str, str]]",
    execution_nodes: "Mapping[str, DagNodeSpec]",
    instance_assignments: "Mapping[tuple[str, str], tuple[str, str | None]]",
) -> tuple[SubAgentDagSpec, AgentPlaybookSpec | None]:
    """Project a completed run onto the graph a replay should submit.

    Starts from the graph as submitted -- never the prompts the runner folded
    into it and never a handle it minted -- so a saved Playbook keeps the
    authored order, branches, inputs, dependencies, prompt templates and skill
    declarations. Only the two identity fields move: each node's agent becomes
    the one its session ended on, and a group member's handle follows its group.
    A worker-labelled node that moved keeps its role by gaining one derived row
    per ``(role, agent)`` pair in the Harness; the source Harness is untouched.

    The run's attempt history is not an input: it is audit evidence recorded
    beside the projected graph, and reading it here would make a replay choice
    out of it.
    """
    occupied: set[str] = set()
    if harness is not None:
        occupied.update(entry.label for entry in harness.delegate)
    occupied.update(worker.label for worker in workers.values())
    roles = {entry.label: entry for entry in harness.delegate} if harness is not None else {}
    cache: dict[tuple[str, str], str] = {}
    emitted: set[tuple[str, str]] = set()
    derived: list[DelegateEntry] = []
    nodes: list[DagNodeSpec] = []
    for node in submitted.nodes:
        worker = workers.get(node.id)
        final_agent, final_instance = _final_pair(
            node, worker, named_instance_groups, execution_nodes, instance_assignments
        )
        recorded = final_agent
        if worker is not None and worker.agent != final_agent:
            pair = (worker.label, final_agent)
            role = roles.get(worker.label)
            if role is not None:
                recorded = _derived_label(*pair, occupied, cache)
                if pair not in emitted:
                    emitted.add(pair)
                    derived.append(role.model_copy(deep=True, update={"as_": recorded, "name": final_agent}))
        elif worker is not None:
            recorded = node.subagent
        nodes.append(node.model_copy(update={"subagent": recorded, "instance": final_instance}))
    if nodes == submitted.nodes:
        return submitted, harness
    rewritten = submitted.model_copy(update={"nodes": nodes})
    if not derived or harness is None:
        return rewritten, harness
    extended = harness.model_copy(deep=True)
    return rewritten, extended.model_copy(update={"delegate": [*extended.delegate, *derived]})


class RunRecordStore:
    """Atomic local run records, deliberately outside the scanned library."""

    def __init__(self, playbook_root: Path) -> None:
        self._root = playbook_root / ".runs"

    def save(self, capture: PlaybookRunCapture) -> Path:
        self._root.mkdir(parents=True, exist_ok=True)
        path = self._root / f"{capture.run_id}.json"
        fd, raw_tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=self._root)
        tmp = Path(raw_tmp)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(capture.as_dict(), handle, ensure_ascii=False, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(tmp, 0o600)
            os.replace(tmp, path)
        finally:
            tmp.unlink(missing_ok=True)
        return path


__all__ = [
    "DagEvidence",
    "PlaybookRunCapture",
    "RunRecordStore",
    "capture_scope",
    "current_capture",
    "record_completed_dag",
    "rewrite_completed_dag",
    "workflow_capture_requested",
]
