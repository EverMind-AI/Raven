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
from typing import Any, Iterator
from uuid import uuid4

from raven.agent.subagent.dag_graph import SubAgentDagSpec


def _now() -> str:
    return datetime.now(UTC).isoformat()


@dataclass
class DagEvidence:
    run_id: str
    spec: SubAgentDagSpec


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
            "dags": [
                {"runId": dag.run_id, "spec": dag.spec.model_dump(by_alias=True, exclude_none=True)}
                for dag in self.dags
            ],
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


def record_completed_dag(spec: SubAgentDagSpec, run_id: str, capture: PlaybookRunCapture | None = None) -> None:
    """Record a graph only after every node completed successfully."""
    capture = capture or _CAPTURE.get()
    if capture is not None:
        capture.dags.append(DagEvidence(run_id=run_id, spec=spec))


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
    "workflow_capture_requested",
]
