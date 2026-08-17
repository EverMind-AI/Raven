"""Compute-job backends for the Ops orchestration loop.

Import the ``JobBackend`` interface from here; concrete backends
(``MockJobBackend`` for tests and fault-injection eval, ``VolcJobBackend``
for VolcEngine ML Platform custom tasks) live in sibling modules and are
never imported directly by orchestration code, so the loop stays decoupled
from any single compute provider.

A *job* here is one unit of long-lived external compute (a tuning trial, a
training run, a simulation) submitted to a backend and run asynchronously.
This is distinct from a proactive *cron job* (a scheduled turn) — see
``raven/proactive_engine``.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class JobBackendError(RuntimeError):
    """Raised when a backend call fails for an infrastructure reason.

    Distinct from a job that runs and *reports* failure (``JobStatus.FAILED``):
    this is the backend itself being unreachable or erroring, which the Ops
    loop treats as transient (retry) rather than as an agent-level failure.
    """


class JobStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"

    @property
    def is_terminal(self) -> bool:
        return self in (JobStatus.SUCCEEDED, JobStatus.FAILED)


@dataclass(frozen=True)
class JobSpec:
    """A backend-agnostic description of one unit of external compute.

    ``payload`` carries the work itself (e.g. a tuning-trial config); backends
    translate it into their own create-task request. ``idem_key`` is the
    caller-owned idempotency key: re-submitting a spec with an ``idem_key`` that
    was already submitted must return the original handle, never a second job —
    this is what makes crash-resume safe (the loop can re-drive submit without
    double-spending compute).
    """

    payload: dict[str, Any]
    idem_key: str
    labels: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        # An empty key does not fail, it collapses: backends join it into
        # ``jobs/{idem_key}`` and the job lands in the jobs root, sharing that
        # directory with the next one. Measured 2026-08-12 -- the run looked
        # entirely normal because only one job existed. Refusing here costs one
        # comparison and turns a silent overwrite into a stack trace.
        if not str(self.idem_key).strip():
            raise ValueError("JobSpec needs a non-empty idem_key to name its job")


@dataclass(frozen=True)
class JobHandle:
    """Backend-issued identifier for a submitted job, used to poll and fetch."""

    backend: str
    job_id: str


@dataclass(frozen=True)
class JobResult:
    """Terminal outcome of a job, fetched once its status is terminal."""

    status: JobStatus
    metrics: dict[str, float] = field(default_factory=dict)
    output: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    # What this run would hand over, named by the backend: ``{"ref", "label",
    # "value"}`` -- where the artifact is, what the number attached to it means,
    # and the number. None when the backend cannot name one.
    #
    # The backend chooses because the choice is domain-specific and only the
    # backend knows the domain. A fine-tune hands over the checkpoint at the
    # best-scoring step; a transient CFD run hands over its last converged time
    # directory and has no "best moment" within the run at all. A tool layer that
    # made this choice would have to know which domain it is in.
    deliverable: dict[str, Any] | None = None


class JobBackend(ABC):
    """A compute backend that runs long-lived jobs asynchronously.

    The Ops loop submits a ``JobSpec``, detaches, and later polls status and
    fetches the result — it never blocks waiting on the job. ``submit`` must be
    idempotent on ``JobSpec.idem_key`` so the loop can safely re-drive it after
    a crash. Infrastructure errors raise ``JobBackendError``; a job that runs
    and fails is reported as ``JobStatus.FAILED`` (not an exception).
    """

    name: str

    @abstractmethod
    async def submit(self, spec: JobSpec) -> JobHandle:
        """Submit a job; return its handle. Idempotent on ``spec.idem_key``."""

    @abstractmethod
    async def poll(self, handle: JobHandle) -> JobStatus:
        """Return the job's current status without blocking."""

    @abstractmethod
    async def fetch_result(self, handle: JobHandle) -> JobResult:
        """Return the terminal result. Caller must poll to a terminal status first."""

    async def cancel(self, handle: JobHandle) -> None:
        """Best-effort cancellation. Default: no-op."""

    async def fetch_progress(self, handle: JobHandle, tail: int = 5) -> list[dict[str, Any]]:
        """In-flight process-health samples for a RUNNING job (e.g. loss/residual
        per step), newest last. The trial owns the contract: it appends JSON lines
        to a progress file as it runs. Default: no progress channel ([]).
        """
        return []
