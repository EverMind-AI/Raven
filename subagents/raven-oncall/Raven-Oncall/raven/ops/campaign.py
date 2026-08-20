"""Campaign orchestrator: run a set of trials on a JobBackend, tracked in a Ledger.

One ``step()`` reconciles every unfinished trial once — submit the un-submitted,
poll the running, record the terminal, and on failure apply the decision policy
(retry or escalate). All state lives in the ledger, so a pass is idempotent and
crash-resumable: a fresh ``Campaign`` from the same ledger + backend continues
where a crashed one left off, never double-submitting (submit is idempotent on
the attempt key) and never re-running a finished trial.

Retries use a fresh idempotency key per attempt (attempt 1 keeps the trial id;
attempt N>1 uses ``<trial>#a<N>``) so each retry is a distinct backend job.
Escalation fires at most once per trial, even across a crash (persisted via the
ledger's ``escalated`` flag).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from typing import Any

from raven.ops.backend import JobBackend, JobBackendError, JobSpec, JobStatus
from raven.ops.ledger import JobRecord, Ledger
from raven.ops.policy import RetryPolicy

EscalationHandler = Callable[[str, JobRecord], Awaitable[None]]


@dataclass(frozen=True)
class Trial:
    """One unit of the campaign; ``trial_id`` is the idempotency key of attempt 1."""

    trial_id: str
    payload: dict[str, Any] = field(default_factory=dict)


class Campaign:
    def __init__(
        self,
        name: str,
        trials: Iterable[Trial],
        backend: JobBackend,
        ledger: Ledger,
        *,
        metric: str = "score",
        goal: str = "max",
        retry_policy: RetryPolicy | None = None,
        escalation: EscalationHandler | None = None,
    ) -> None:
        if goal not in ("max", "min"):
            raise ValueError(f"goal must be 'max' or 'min', got {goal!r}")
        self.name = name
        self._trials = list(trials)
        self._backend = backend
        self._ledger = ledger
        self._metric = metric
        self._goal = goal
        self._policy = retry_policy or RetryPolicy()
        self._escalation = escalation

    def _attempt_key(self, trial_id: str, attempt: int) -> str:
        return trial_id if attempt == 1 else f"{trial_id}#a{attempt}"

    def add_trials(self, trials: Iterable[Trial]) -> int:
        existing = {t.trial_id for t in self._trials}
        added = [t for t in trials if t.trial_id not in existing]
        self._trials.extend(added)
        return len(added)

    def history(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for rec in self._ledger.by_campaign(self.name):
            if rec.status is JobStatus.SUCCEEDED and rec.result and self._metric in rec.result.metrics:
                cfg = rec.result.output.get("config")
                if cfg is not None:
                    out.append({"config": cfg, "score": rec.result.metrics[self._metric]})
        return out

    async def step(self) -> None:
        for trial in self._trials:
            await self._reconcile_trial(trial)

    async def _reconcile_trial(self, trial: Trial) -> None:
        attempt = 1
        while True:
            key = self._attempt_key(trial.trial_id, attempt)
            rec = self._ledger.get(key)
            if rec is None or not rec.is_terminal:
                break
            if rec.status is JobStatus.SUCCEEDED:
                return
            if self._policy.should_retry(attempt):
                attempt += 1
                continue
            await self._maybe_escalate(trial.trial_id, key, rec)
            return

        rec = self._ledger.get(key) or self._ledger.record(key, campaign=self.name)
        try:
            await self._reconcile_job(trial, key, rec, attempt)
        except JobBackendError:
            return

        rec = self._ledger.get(key)
        if rec is not None and rec.status is JobStatus.FAILED and not self._policy.should_retry(attempt):
            await self._maybe_escalate(trial.trial_id, key, rec)

    async def _reconcile_job(self, trial: Trial, key: str, rec: JobRecord, attempt: int) -> None:
        handle = rec.handle
        if handle is None:
            spec = JobSpec(trial.payload, idem_key=key, labels={"campaign": self.name, "attempt": str(attempt)})
            handle = await self._backend.submit(spec)
            self._ledger.set_handle(key, handle)
        status = await self._backend.poll(handle)
        if status.is_terminal:
            self._ledger.set_result(key, await self._backend.fetch_result(handle))
        else:
            self._ledger.set_status(key, status)

    async def _maybe_escalate(self, trial_id: str, key: str, rec: JobRecord) -> None:
        if rec.escalated:
            return
        self._ledger.mark_escalated(key)
        if self._escalation is not None:
            await self._escalation(trial_id, rec)

    def _trial_done(self, trial: Trial) -> bool:
        attempt = 1
        while True:
            rec = self._ledger.get(self._attempt_key(trial.trial_id, attempt))
            if rec is None or not rec.is_terminal:
                return False
            if rec.status is JobStatus.SUCCEEDED:
                return True
            if self._policy.should_retry(attempt):
                attempt += 1
                continue
            return True

    def is_done(self) -> bool:
        return all(self._trial_done(t) for t in self._trials)

    async def run(self, *, max_passes: int = 1000) -> None:
        """Drive ``step()`` until done. No inter-pass delay — for the mock backend
        and tests; production drives ``step()`` from the proactive scheduler / on a
        job-completion wake, so it isn't busy-polling a real backend.
        """
        passes = 0
        while not self.is_done():
            if passes >= max_passes:
                raise RuntimeError(f"campaign {self.name!r} not done after {max_passes} passes")
            await self.step()
            passes += 1

    def best(self) -> JobRecord | None:
        succeeded = [
            r
            for r in self._ledger.by_campaign(self.name)
            if r.status is JobStatus.SUCCEEDED and r.result is not None and self._metric in r.result.metrics
        ]
        if not succeeded:
            return None
        key = lambda r: r.result.metrics[self._metric]  # noqa: E731
        return max(succeeded, key=key) if self._goal == "max" else min(succeeded, key=key)
