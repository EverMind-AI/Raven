"""Deterministic in-memory ``JobBackend`` for tests and fault-injection eval.

No wall-clock time and no real compute: a job advances one status step per
``poll()`` following a per-job plan, so campaign orchestration can be exercised
at second-scale and replayed identically. Two fault kinds are injectable so the
loop's retry/resume/idempotency paths can be tested deterministically:

  - a job that *reports* failure (``JobStatus.FAILED``) via ``JobPlan.fail``;
  - the *backend* being transiently unreachable, via ``transient_poll_errors`` —
    the next N ``poll()`` calls raise ``JobBackendError`` before recovering.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from raven.ops.backend import (
    JobBackend,
    JobBackendError,
    JobHandle,
    JobResult,
    JobSpec,
    JobStatus,
)


@dataclass
class JobPlan:
    """Scripted outcome for one job: reach a terminal status after N polls."""

    succeed_after_polls: int = 1
    fail: bool = False
    metrics: dict[str, float] = field(default_factory=dict)
    output: dict[str, object] = field(default_factory=dict)
    error: str | None = None
    # In-flight health samples fetch_progress returns while the job runs
    # (e.g. [{"step": 1, "loss": 2.0}, {"step": 2, "loss": float("nan")}]).
    progress: list[dict] = field(default_factory=list)


@dataclass
class _MockJob:
    spec: JobSpec
    plan: JobPlan
    polls: int = 0

    @property
    def status(self) -> JobStatus:
        if self.polls == 0:
            return JobStatus.PENDING
        if self.polls < self.plan.succeed_after_polls:
            return JobStatus.RUNNING
        return JobStatus.FAILED if self.plan.fail else JobStatus.SUCCEEDED


class MockJobBackend(JobBackend):
    name = "mock"

    def __init__(
        self,
        *,
        default_plan: JobPlan | None = None,
        plans: dict[str, JobPlan] | None = None,
        transient_poll_errors: int = 0,
        idempotent: bool = True,
    ) -> None:
        self._default_plan = default_plan or JobPlan()
        self._plans = plans or {}
        self._transient_poll_errors = transient_poll_errors
        self._idempotent = idempotent
        self._by_idem: dict[str, str] = {}
        self._jobs: dict[str, _MockJob] = {}
        self._seq = 0

    async def submit(self, spec: JobSpec) -> JobHandle:
        if self._idempotent and spec.idem_key in self._by_idem:
            return JobHandle(self.name, self._by_idem[spec.idem_key])
        self._seq += 1
        job_id = f"mock-{self._seq}"
        self._by_idem[spec.idem_key] = job_id
        self._jobs[job_id] = _MockJob(spec, self._plans.get(spec.idem_key, self._default_plan))
        return JobHandle(self.name, job_id)

    async def poll(self, handle: JobHandle) -> JobStatus:
        if self._transient_poll_errors > 0:
            self._transient_poll_errors -= 1
            raise JobBackendError("mock backend transiently unreachable")
        job = self._job(handle)
        if not job.status.is_terminal:
            job.polls += 1
        return job.status

    async def fetch_result(self, handle: JobHandle) -> JobResult:
        job = self._job(handle)
        if not job.status.is_terminal:
            raise JobBackendError(f"job {handle.job_id} is not terminal ({job.status.value})")
        if job.status is JobStatus.FAILED:
            return JobResult(JobStatus.FAILED, error=job.plan.error or "job failed")
        return JobResult(JobStatus.SUCCEEDED, metrics=job.plan.metrics, output=job.plan.output)

    async def fetch_progress(self, handle: JobHandle, tail: int = 5) -> list[dict]:
        return self._job(handle).plan.progress[-tail:]

    def _job(self, handle: JobHandle) -> _MockJob:
        try:
            return self._jobs[handle.job_id]
        except KeyError:
            raise JobBackendError(f"unknown job {handle.job_id}") from None
