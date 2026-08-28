"""The intervention demonstration must fail on the defects it exists to catch.

A gate that cannot fail is the very thing it is meant to guard against, so each
test here hands it a backend with one specific defect and asserts the verdict is
negative: a cancelled job reported as SUCCEEDED, a cancelled job refunded, and a
job whose spend vanishes without being named.
"""

from __future__ import annotations

import pytest

from benchmarks.ops_forks.intervention_demo import SKIPPED, demonstrate
from raven.ops.backend import JobHandle, JobResult, JobStatus
from raven.ops.backends import register_backend


class FakeBackend:
    """Minimal backend whose defects are switchable."""

    name = "fake"

    def __init__(
        self,
        *,
        cancel_reads_succeeded: bool = False,
        refunds_cancelled: bool = False,
        loses_spend: bool = False,
        applies_payload: bool = True,
    ) -> None:
        self.cancel_reads_succeeded = cancel_reads_succeeded
        self.refunds_cancelled = refunds_cancelled
        self.loses_spend = loses_spend
        self.applies_payload = applies_payload
        self.jobs: dict[str, dict] = {}
        self._spend = 0.0

    async def submit(self, spec):
        self.jobs[spec.idem_key] = {"payload": dict(spec.payload), "cancelled": False}
        self._spend += 1.0
        return JobHandle(self.name, spec.idem_key)

    async def poll(self, handle):
        job = self.jobs[handle.job_id]
        if not job["cancelled"]:
            return JobStatus.RUNNING
        return JobStatus.SUCCEEDED if self.cancel_reads_succeeded else JobStatus.FAILED

    async def cancel(self, handle):
        self.jobs[handle.job_id]["cancelled"] = True
        if self.refunds_cancelled:
            self._spend -= 1.0
        if self.loses_spend:
            self._spend = 0.0

    async def fetch_result(self, handle):
        return JobResult(await self.poll(handle))

    async def fetch_progress(self, handle, tail=5):
        job = self.jobs[handle.job_id]
        if not self.applies_payload:
            return [{"line": "ran with defaults"}]
        return [{"line": f"applied {k}={v}" for k, v in job["payload"].items()}]

    def spent_minutes(self):
        return max(0.0, self._spend)

    def unmeasured_spend(self):
        return {}


def _meta(**flags):
    register_backend("fake", lambda _meta: FakeBackend(**flags))
    return {"backend": "fake"}


FAST = {"poll_tries": 3, "poll_delay": 0.01, "settle": 0.01}


@pytest.mark.asyncio
async def test_healthy_backend_passes():
    verdict, _, checks = await demonstrate(
        _meta(), payload_a={"deltaT": 1}, payload_b={"deltaT": 2},
        evidence_a="deltaT=1", evidence_b="deltaT=2", **FAST,
    )
    assert verdict is True
    assert checks["change_reached_executor"] is True


@pytest.mark.asyncio
async def test_cancelled_job_reported_as_succeeded_fails_the_gate():
    verdict, _, checks = await demonstrate(
        _meta(cancel_reads_succeeded=True), payload_a={"a": 1}, payload_b={"a": 2}, **FAST,
    )
    assert verdict is False
    assert checks["cancelled_is_not_success"] is False


@pytest.mark.asyncio
async def test_refunding_a_cancelled_job_fails_the_gate():
    verdict, _, checks = await demonstrate(
        _meta(refunds_cancelled=True), payload_a={"a": 1}, payload_b={"a": 2}, **FAST,
    )
    assert verdict is False
    assert checks["cancelled_not_refunded"] is False


@pytest.mark.asyncio
async def test_spend_vanishing_without_being_named_fails_the_gate():
    verdict, _, checks = await demonstrate(
        _meta(loses_spend=True), payload_a={"a": 1}, payload_b={"a": 2}, **FAST,
    )
    assert verdict is False


@pytest.mark.asyncio
async def test_config_written_but_ignored_fails_when_evidence_is_given():
    # The defect this catches is invisible without evidence markers: the ledger
    # shows two trials either way.
    verdict, _, checks = await demonstrate(
        _meta(applies_payload=False), payload_a={"deltaT": 1}, payload_b={"deltaT": 2},
        evidence_a="deltaT=1", evidence_b="deltaT=2", **FAST,
    )
    assert verdict is False
    assert checks["change_reached_executor"] is False


@pytest.mark.asyncio
async def test_without_evidence_the_step_is_skipped_not_silently_passed():
    verdict, log, checks = await demonstrate(
        _meta(applies_payload=False), payload_a={"a": 1}, payload_b={"a": 2}, **FAST,
    )
    assert checks["change_reached_executor"] is SKIPPED
    assert verdict is True
    # A skipped check must announce that it weakened the verdict.
    assert any("did not verify" in line for line in log)
