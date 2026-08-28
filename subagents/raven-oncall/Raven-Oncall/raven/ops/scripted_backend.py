"""Scripted, clock-driven ``JobBackend``: the fake world on-call evals run against.

Where ``MockJobBackend`` advances a job one step per ``poll()`` (enough for
orchestration unit tests), this backend advances jobs by *campaign time* from a
deterministic script: a job finishes when the clock passes its scheduled finish,
whether or not anyone polled. That difference is what makes the two headline
efficiency metrics measurable at all:

  - **detection latency** -- the script knows exactly when a job became terminal,
    so the gap between that instant and the loop's first observation of it is
    ground truth, not an estimate. On a real host that instant is unobservable.
  - **wake overhead** -- polls are counted, so "how many checks did the loop spend
    to detect that fast" is the paired cost of the latency above.

A script with no finish time is the no-op case (the awaited event never happens):
the loop must keep waiting cheaply and must never report a conclusion. Scripts
also carry progress samples on the same clock, so process-health decisions
(a diverged run killed early) are evaluated on known timelines.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import Any

from raven.ops.backend import (
    JobBackend,
    JobBackendError,
    JobHandle,
    JobResult,
    JobSpec,
    JobStatus,
)
from raven.ops.backends import register_backend
from raven.ops.simclock import SimClock


@dataclass
class JobScript:
    """One job's timeline, in campaign-time offsets from its own submission.

    ``finish_after_ms=None`` means it never finishes (no-op case). ``progress``
    is ``(offset_ms, sample)`` pairs, revealed as the clock passes each offset.
    """

    finish_after_ms: int | None = 1000
    fail: bool = False
    metrics: dict[str, float] = field(default_factory=dict)
    output: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    progress: list[tuple[int, dict[str, Any]]] = field(default_factory=list)


def _curve(
    *,
    samples: int,
    seed: int,
    interval_ms: int,
    start: float,
    floor: float,
    jitter: float,
    decay: float,
    climb: float,
    diverge_at: int | None,
) -> list[tuple[int, dict[str, Any]]]:
    # One RNG draw per sample in both modes, and the descent is computed the same
    # way before diverge_at, so a converging and a diverging curve built from the
    # same seed share their prefix exactly. That shared prefix is the point: it
    # leaves the agent no way to tell them apart until the turn, forcing it to
    # judge a trend rather than a single rise.
    rng = random.Random(seed)
    out: list[tuple[int, dict[str, Any]]] = []
    value = start
    for i in range(samples):
        if diverge_at is not None and i >= diverge_at:
            value += climb
        else:
            value -= (value - floor) * decay
        noisy = round(max(0.0, value + rng.uniform(-jitter, jitter)), 4)
        out.append((i * interval_ms, {"step": i, "loss": noisy}))
    return out


def converging_curve(
    *,
    samples: int = 40,
    seed: int = 0,
    interval_ms: int = 1_000,
    start: float = 2.5,
    floor: float = 0.3,
    jitter: float = 0.15,
    decay: float = 0.12,
) -> JobScript:
    """A jittering loss that keeps descending: left alone it succeeds, so killing
    it is the wrong call."""
    progress = _curve(
        samples=samples, seed=seed, interval_ms=interval_ms, start=start,
        floor=floor, jitter=jitter, decay=decay, climb=0.0, diverge_at=None,
    )
    return JobScript(
        finish_after_ms=samples * interval_ms,
        fail=False,
        metrics={"final_loss": progress[-1][1]["loss"]},
        progress=progress,
    )


def diverging_curve(
    *,
    samples: int = 40,
    diverge_at: int = 20,
    seed: int = 0,
    interval_ms: int = 1_000,
    start: float = 2.5,
    floor: float = 0.3,
    jitter: float = 0.15,
    decay: float = 0.12,
    climb: float = 0.18,
) -> JobScript:
    """The same jittering descent, which turns upward at ``diverge_at`` and never
    recovers: left alone it fails, so killing it is right -- and the sooner the
    more machine time is saved."""
    progress = _curve(
        samples=samples, seed=seed, interval_ms=interval_ms, start=start,
        floor=floor, jitter=jitter, decay=decay, climb=climb, diverge_at=diverge_at,
    )
    return JobScript(
        finish_after_ms=samples * interval_ms,
        fail=True,
        error="diverged",
        metrics={"final_loss": progress[-1][1]["loss"]},
        progress=progress,
    )


@dataclass
class KillVerdict:
    """Whether one kill was right, and what it cost.

    ``correct`` is None for a job whose script never finishes: there is no
    horizon, so its remaining time is undefined and it is reported separately
    rather than folded into either side.
    """

    idem_key: str
    killed_at_ms: int
    ran_ms: int
    correct: bool | None
    saved_ms: int = 0
    wasted_ms: int = 0


@dataclass
class _ScriptedJob:
    spec: JobSpec
    script: JobScript
    submitted_at_ms: int
    polls: int = 0
    first_observed_terminal_ms: int | None = None
    cancelled_at_ms: int | None = None
    # cancel() rewrites `script` so the job reads as terminal, which destroys the
    # counterfactual. Keep the pre-cancel script: it is the only record of what
    # the job would have done if left alone, and that is what makes a kill
    # judgeable at all.
    counterfactual: JobScript | None = None

    @property
    def finished_at_ms(self) -> int | None:
        """Campaign instant the job actually became terminal (ground truth)."""
        if self.cancelled_at_ms is not None:
            return self.cancelled_at_ms
        if self.script.finish_after_ms is None:
            return None
        return self.submitted_at_ms + self.script.finish_after_ms


class ScriptedJobBackend(JobBackend):
    name = "scripted"

    def __init__(
        self,
        clock: SimClock,
        *,
        scripts: dict[str, JobScript] | None = None,
        default_script: JobScript | None = None,
    ) -> None:
        self._clock = clock
        self._scripts = scripts or {}
        self._default_script = default_script or JobScript()
        self._by_idem: dict[str, str] = {}
        self._jobs: dict[str, _ScriptedJob] = {}
        self._seq = 0

    async def submit(self, spec: JobSpec) -> JobHandle:
        if spec.idem_key in self._by_idem:
            return JobHandle(self.name, self._by_idem[spec.idem_key])
        self._seq += 1
        job_id = f"scripted-{self._seq}"
        self._by_idem[spec.idem_key] = job_id
        self._jobs[job_id] = _ScriptedJob(
            spec=spec,
            script=self._scripts.get(spec.idem_key, self._default_script),
            submitted_at_ms=self._clock.now_ms(),
        )
        return JobHandle(self.name, job_id)

    async def poll(self, handle: JobHandle) -> JobStatus:
        job = self._job(handle)
        job.polls += 1
        status = self._status(job)
        if status.is_terminal and job.first_observed_terminal_ms is None:
            job.first_observed_terminal_ms = self._clock.now_ms()
        return status

    async def fetch_result(self, handle: JobHandle) -> JobResult:
        job = self._job(handle)
        status = self._status(job)
        if not status.is_terminal:
            raise JobBackendError(f"job {handle.job_id} is not terminal ({status.value})")
        if status is JobStatus.FAILED:
            return JobResult(JobStatus.FAILED, error=job.script.error or "job failed")
        return JobResult(JobStatus.SUCCEEDED, metrics=dict(job.script.metrics), output=dict(job.script.output))

    async def cancel(self, handle: JobHandle) -> None:
        job = self._job(handle)
        if self._status(job).is_terminal:
            return
        job.cancelled_at_ms = self._clock.now_ms()
        job.counterfactual = job.script
        job.script = JobScript(
            finish_after_ms=job.cancelled_at_ms - job.submitted_at_ms,
            fail=True,
            error="cancelled",
            progress=job.script.progress,
        )

    async def fetch_progress(self, handle: JobHandle, tail: int = 5) -> list[dict[str, Any]]:
        job = self._job(handle)
        elapsed = self._clock.now_ms() - job.submitted_at_ms
        revealed = [sample for offset, sample in job.script.progress if offset <= elapsed]
        return revealed[-tail:]

    # ---- measurement surface (evals read these; orchestration never does) ----

    def detection_latency_ms(self, idem_key: str) -> int | None:
        """Campaign time between the job actually finishing and the loop first
        observing it. None if it never finished or was never observed terminal."""
        job = self._jobs.get(self._by_idem.get(idem_key, ""))
        if job is None or job.finished_at_ms is None or job.first_observed_terminal_ms is None:
            return None
        return job.first_observed_terminal_ms - job.finished_at_ms

    def poll_count(self, idem_key: str | None = None) -> int:
        """Polls spent on one job, or across all jobs (the wake-overhead side)."""
        if idem_key is None:
            return sum(job.polls for job in self._jobs.values())
        job = self._jobs.get(self._by_idem.get(idem_key, ""))
        return job.polls if job else 0

    def finished_at_ms(self, idem_key: str) -> int | None:
        job = self._jobs.get(self._by_idem.get(idem_key, ""))
        return job.finished_at_ms if job else None

    def kill_verdicts(self) -> list[KillVerdict]:
        """One verdict per killed job, judged against what the script says it
        would have done. Only the scripted world can answer this -- production
        never learns the counterfactual -- so this is carrier-side scoring."""
        out: list[KillVerdict] = []
        for key, job_id in self._by_idem.items():
            job = self._jobs.get(job_id)
            if job is None or job.cancelled_at_ms is None or job.counterfactual is None:
                continue
            ran = job.cancelled_at_ms - job.submitted_at_ms
            would_finish_after = job.counterfactual.finish_after_ms
            if would_finish_after is None:
                out.append(KillVerdict(key, job.cancelled_at_ms, ran, None))
            elif job.counterfactual.fail:
                out.append(
                    KillVerdict(key, job.cancelled_at_ms, ran, True,
                                saved_ms=max(0, would_finish_after - ran))
                )
            else:
                out.append(KillVerdict(key, job.cancelled_at_ms, ran, False, wasted_ms=ran))
        return out

    def kill_cost(self) -> dict[str, int]:
        """Aggregate of ``kill_verdicts``.

        ``wasted_ms`` counts only the compute burned before a wrong kill; the
        lost result is not priced here because its worth is the campaign's to
        judge, not the backend's.
        """
        verdicts = self.kill_verdicts()
        return {
            "kills": len(verdicts),
            "correct_kills": sum(1 for v in verdicts if v.correct is True),
            "wrong_kills": sum(1 for v in verdicts if v.correct is False),
            "killed_never_finishing": sum(1 for v in verdicts if v.correct is None),
            "saved_ms": sum(v.saved_ms for v in verdicts),
            "wasted_ms": sum(v.wasted_ms for v in verdicts),
        }

    def _status(self, job: _ScriptedJob) -> JobStatus:
        finished_at = job.finished_at_ms
        now = self._clock.now_ms()
        if finished_at is None or now < finished_at:
            return JobStatus.PENDING if job.polls <= 1 else JobStatus.RUNNING
        return JobStatus.FAILED if job.script.fail else JobStatus.SUCCEEDED

    def _job(self, handle: JobHandle) -> _ScriptedJob:
        try:
            return self._jobs[handle.job_id]
        except KeyError:
            raise JobBackendError(f"unknown job {handle.job_id}") from None


_WORLDS: dict[str, ScriptedJobBackend] = {}


def install_world(name: str, backend: ScriptedJobBackend) -> None:
    """Make a scripted world reachable from the tool layer.

    A campaign whose ``meta.json`` reads ``{"backend": "scripted", "world": name}``
    then resolves to this instance, so an evaluation exercises the real tools --
    ledger, reconcile, chart, triage -- against compressed time. The world is
    in-process by nature (its jobs live in memory), so an eval that spans a
    process restart needs a persisted world instead.
    """
    _WORLDS[name] = backend
    register_backend("scripted", _world_from_meta)


def clear_worlds() -> None:
    _WORLDS.clear()


def _world_from_meta(meta: dict[str, Any]) -> ScriptedJobBackend:
    name = meta.get("world")
    try:
        return _WORLDS[str(name)]
    except KeyError:
        known = ", ".join(sorted(_WORLDS)) or "none"
        raise JobBackendError(f"no scripted world {name!r} installed (have: {known})") from None
