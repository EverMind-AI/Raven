"""Tests for the Ops JobBackend contract, exercised through MockJobBackend.

Covers the contract the orchestration loop relies on: status advances to a
terminal state, results carry metrics, submit is idempotent on idem_key
(crash-resume safety), reported job failure vs infrastructure error are
distinct, and transient backend errors are raised (not swallowed).
"""

from __future__ import annotations

import pytest

from raven.ops import (
    JobBackendError,
    JobHandle,
    JobPlan,
    JobSpec,
    JobStatus,
    MockJobBackend,
)


def _spec(idem_key: str = "trial-1", **payload: object) -> JobSpec:
    return JobSpec(payload=dict(payload), idem_key=idem_key)


async def test_submit_then_poll_to_success_and_fetch_metrics() -> None:
    backend = MockJobBackend(default_plan=JobPlan(succeed_after_polls=2, metrics={"ndcg": 0.71}))
    handle = await backend.submit(_spec())

    assert await backend.poll(handle) is JobStatus.RUNNING
    status = await backend.poll(handle)
    assert status is JobStatus.SUCCEEDED and status.is_terminal

    result = await backend.fetch_result(handle)
    assert result.status is JobStatus.SUCCEEDED
    assert result.metrics == {"ndcg": 0.71}


async def test_submit_is_idempotent_on_idem_key() -> None:
    backend = MockJobBackend()
    first = await backend.submit(_spec(idem_key="trial-7"))
    second = await backend.submit(_spec(idem_key="trial-7", k1=1.5))

    assert first == second
    assert len(backend._by_idem) == 1


async def test_reported_failure_is_terminal_with_error() -> None:
    backend = MockJobBackend(default_plan=JobPlan(fail=True, error="diverged"))
    handle = await backend.submit(_spec())

    assert await backend.poll(handle) is JobStatus.FAILED
    result = await backend.fetch_result(handle)
    assert result.status is JobStatus.FAILED
    assert result.error == "diverged"


async def test_fetch_before_terminal_raises() -> None:
    backend = MockJobBackend(default_plan=JobPlan(succeed_after_polls=3))
    handle = await backend.submit(_spec())
    with pytest.raises(JobBackendError):
        await backend.fetch_result(handle)


async def test_transient_backend_error_then_recovers() -> None:
    backend = MockJobBackend(transient_poll_errors=1)
    handle = await backend.submit(_spec())

    with pytest.raises(JobBackendError):
        await backend.poll(handle)
    assert await backend.poll(handle) is JobStatus.SUCCEEDED


async def test_poll_unknown_job_raises() -> None:
    backend = MockJobBackend()
    with pytest.raises(JobBackendError):
        await backend.poll(JobHandle("mock", "does-not-exist"))
