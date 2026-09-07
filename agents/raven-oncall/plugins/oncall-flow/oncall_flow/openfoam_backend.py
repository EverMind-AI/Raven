"""Bare-process backend for OpenFOAM jobs, with core-hour accounting.

Why this is separate from ``ProcessExecutor`` rather than a change to it: that
class prices work in GPU minutes for a campaign that is mid-analysis, and a CFD
job is priced in core-hours. Sharing the launcher and the idempotent submit while
overriding only the measurement keeps the two cost models from colliding.

Three things differ from a training job, all of them because OpenFOAM keeps its
own books and none of ours:

  - **cost is cores x wall clock, not wall clock.** Without the core factor a
    24-core run and a 1-core run of the same duration cost the same, so the loop
    could buy speed by raising ``numberOfSubdomains`` and never pay for it.
  - **there is no ``progress.jsonl``.** The spend of a killed job is recovered
    from what the solver itself left: the last ``ClockTime = N s`` it printed, and
    the mtimes of its log and time directories. Counting a killed job as zero
    refunds exactly the compute a cancel-and-resubmit loop would burn.
  - **there is no ``result.json``.** A finished solver prints ``End``; that, not a
    file we invented, is what says the process ran to completion. Note this is a
    statement about the process, not about the physics -- whether the answer is
    usable is the reader's judgement and stays out of here.
"""

from __future__ import annotations

from typing import Any

from oncall_flow.backend import JobBackendError, JobHandle, JobResult, JobStatus
from oncall_flow.budget import ADDITIVE, accumulate
from oncall_flow.budget import from_meta as budget_from_meta
from oncall_flow.process_backend import ProcessExecutor, _num

# How much of the solver's own ending to carry in the failure reason. Enough for
# the crash banner and the first frames under it; the reader truncates again.
_LAST_WORDS_LINES = 12
_LAST_WORDS_CHARS = 200


def _failure_reason(log_tail: str) -> str:
    """Why a job is failed: how it was detected, then what it last printed.

    Detection alone is a tautology -- "solver did not print End" restates the rule
    that produced the verdict. The lines are appended verbatim and unranked:
    deciding which one explains the crash is domain knowledge, and this backend
    serves every solver that writes a log.
    """
    detected = "solver did not print End"
    lines = [ln.strip()[:_LAST_WORDS_CHARS] for ln in log_tail.splitlines() if ln.strip()]
    if not lines:
        return f"{detected}; no log to read either"
    return detected + ", and its last lines were:\n" + "\n".join(lines[-_LAST_WORDS_LINES:])


class OpenFoamExecutor(ProcessExecutor):
    """``ProcessExecutor`` whose budget is core-minutes and whose evidence is a solver log.

    ``budget_minutes_total`` is inherited but means CORE-minutes here, so a job on
    8 cores draws down 8 minutes of budget per wall minute.
    """

    name = "openfoam"

    def __init__(self, *args: Any, default_cores: int = 1, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._default_cores = max(1, int(default_cores))
        self._cores: dict[str, int] = {}

    @property
    def budget_overlap(self) -> str:
        """Additive unless the campaign says otherwise.

        Cores running side by side are separately busy, so the default has to hold
        even when no budget was declared -- the spend is measured and printed
        either way, and a rule that only appeared alongside a total would make the
        unbounded case the one that is quietly wrong.
        """
        return self._budget_decl.overlap if self._budget_decl else ADDITIVE

    @property
    def budget_unit(self) -> str:
        return self._budget_decl.unit if self._budget_decl else "core-minute"

    def cores_used(self, idem_key: str | None = None) -> int | dict[str, int]:
        """Cores each measured job actually ran on. Read alongside any spend number."""
        if idem_key is None:
            return dict(self._cores)
        return self._cores.get(idem_key, self._default_cores)

    async def spent_minutes(self) -> float:
        """Core-minutes this campaign has consumed, whatever ended the runs.

        Cores are counted from the ``processor*`` directories ``decomposePar``
        actually created rather than from ``decomposeParDict``, because the loop is
        allowed to edit that dict and the two can disagree; a serial run has none
        and counts as one core.

        Wall clock is the larger of two independent estimates, never the smaller:
        the span between the pid file and the last thing the solver touched, and
        the ``ClockTime`` the solver printed itself. They measure different spans
        (the first includes meshing and decomposition, the second only the solve)
        and taking the maximum means no plausible evidence of spend is discarded.

        A job with nothing at all to measure is recorded in ``unmeasured_spend``
        rather than silently counted as zero.
        """
        script = (
            f"cd {self._remote_dir}/jobs 2>/dev/null || exit 0; "
            "for d in */; do d=${d%/}; "
            # Cores: what decomposePar really produced.
            'P=$(ls -d "$d"/processor[0-9]* 2>/dev/null | wc -l); '
            # Start marker and liveness.
            'A=0; if [ -f "$d/pid" ] && kill -0 "$(cat "$d/pid")" 2>/dev/null; then A=1; fi; '
            'S="-"; if [ -f "$d/pid" ]; then S=$(stat -c %Y "$d/pid"); fi; '
            # The solver's own wall-clock figure: last "ClockTime = N s" in any log.
            'C="-"; C=$(grep -ho "ClockTime = [0-9]* s" "$d"/job.log "$d"/log.* 2>/dev/null '
            "| tail -1 | tr -dc '0-9'); "
            # Last moment anything in the job was written: logs or time directories.
            'L="-"; L=$(find "$d" -maxdepth 2 \\( -name "job.log" -o -name "log.*" '
            '-o -name "*.[0-9]" -o -name "uniform" \\) -printf "%T@\\n" 2>/dev/null '
            "| sort -n | tail -1 | cut -d. -f1); "
            # Did the solver finish cleanly? job.log only -- the preprocessing
            # utilities print their own End into log.*, see _status.
            'E=0; if grep -qx "End" "$d"/job.log 2>/dev/null; then E=1; fi; '
            'printf \'%s|%s|%s|%s|%s|%s|%s\\n\' "$d" "$P" "$A" "$S" "$C" "$L" "$E"; '
            "done; printf 'NOW|%s\\n' \"$(date -u +%s)\""
        )
        rc, out = await self._arun(script)
        if rc != 0:
            return 0.0

        rows: list[list[str]] = []
        now: float | None = None
        for line in out.splitlines():
            parts = line.strip().split("|")
            if len(parts) == 2 and parts[0] == "NOW":
                now = _num(parts[1])
            elif len(parts) == 7:
                rows.append(parts)

        unplaced = 0.0
        placed: list[tuple[float, float, float]] = []
        self._unmeasured.clear()
        self._cores.clear()
        for key, procs, alive, started, clock, last_touch, _end in rows:
            np_ = int(_num(procs) or 0) or self._default_cores
            self._cores[key] = np_
            st, ct, lt = _num(started), _num(clock), _num(last_touch)

            estimates: list[float] = []
            if ct is not None:
                estimates.append(ct)
            if st is not None:
                end = now if alive == "1" and now is not None else lt
                if end is not None:
                    estimates.append(max(0.0, end - st))
            if not estimates:
                self._unmeasured[key] = "killed with no solver log, time directory or pid to measure from"
                continue
            seconds = max(estimates)
            # Width is the core count, and the campaign's declared rule decides
            # whether widths that overlap add up. Under the core-minute default
            # they do -- the cores were separately busy -- and the shared accountant
            # this shares with the training backend is the same function either way.
            if st is None:
                unplaced += seconds * np_ / 60.0
            else:
                placed.append((st, st + seconds, float(np_)))
        return unplaced + accumulate(placed, overlap=self.budget_overlap)

    async def _status(self, idem: str) -> JobStatus:
        """Terminal state from the solver's own markers, since there is no result.json.

        ``End`` is what OpenFOAM prints after a clean shutdown, so its presence is
        the completion signal and its absence in a dead run means the process was
        killed or crashed. Whether the converged answer is any good is a separate
        question that belongs to whoever reads the residuals.
        """
        job_dir = self._job_dir(idem)
        # ``job.log`` ONLY, never log.*: blockMesh, setFields and decomposePar each
        # print their own "End" on success, so scanning every log file reported a
        # killed solver as SUCCEEDED as soon as meshing had finished. job.log is the
        # job's own stdout by construction, so it carries the solver's End and
        # nothing else's. If a command template sends the solver elsewhere this
        # reads FAILED instead, which is the safe direction to be wrong in.
        rc, out = await self._arun(
            f"if [ -f {job_dir}/pid ] && kill -0 $(cat {job_dir}/pid) 2>/dev/null; then echo alive; "
            f"elif grep -qx End {job_dir}/job.log 2>/dev/null; then echo ended; "
            f"elif [ -f {job_dir}/pid ]; then echo gone; "
            f"else echo absent; fi"
        )
        text = out.strip()
        if rc != 0:
            return JobStatus.RUNNING
        if text == "alive":
            return JobStatus.RUNNING
        if text == "ended":
            return JobStatus.SUCCEEDED
        if text == "gone":
            return JobStatus.FAILED
        return JobStatus.PENDING

    async def _stamp_terminal(self, idem: str) -> None:
        """Date the finish from the solver's last write; there is no result.json to stat."""
        job_dir = self._job_dir(idem)
        rc, out = await self._arun(
            f"date -u +%s.%N; "
            f'find {job_dir} -maxdepth 2 \\( -name "job.log" -o -name "log.*" \\) '
            f'-printf "%T@\\n" 2>/dev/null | sort -n | tail -1 || echo missing'
        )
        lines = [ln.strip() for ln in out.splitlines() if ln.strip()]
        if rc != 0 or len(lines) < 2 or lines[-1] == "missing":
            self._finish_unknown[idem] = "no solver log to date the finish from"
            return
        try:
            now, finished = float(lines[0]), float(lines[-1])
        except ValueError:
            self._finish_unknown[idem] = f"unreadable timestamps {lines[:2]}"
            return
        self._latency_ms[idem] = max(0, int(round((now - finished) * 1000)))

    async def fetch_result(self, handle: JobHandle) -> JobResult:
        """Raw evidence only: no metric is derived and no verdict on the physics.

        ``output`` carries the tail of the solver log and the time directories that
        exist. Deciding whether the run converged, or whether the answer is
        physical, is the reading the experiment is trying to measure -- returning it
        from here would hand over the judgement.
        """
        idem = self._idem(handle)
        job_dir = self._job_dir(idem)
        status = await self._status(idem)
        rc, tail = await self._arun(f"tail -n 40 {job_dir}/job.log {job_dir}/log.* 2>/dev/null | tail -n 40")
        _, times = await self._arun(
            f'ls -1d {job_dir}/[0-9]* 2>/dev/null | xargs -r -n1 basename | sort -g | tr "\\n" " "'
        )
        output: dict[str, Any] = {
            "log_tail": tail.strip()[:4000] if rc == 0 else "",
            "time_directories": times.split(),
            "cores": self._cores.get(idem, self._default_cores),
        }
        # The gate admitted the job as wide as it was declared; the machine ran it
        # as wide as decomposePar produced. Billing follows the measurement (the
        # box was busy that wide); the disagreement is worth a line, because the
        # declaration is what every other job's admission trusted.
        metrics: dict[str, Any] = {}
        declared = getattr(self, "_declared_width", {}).get(idem)
        if declared:
            output["cores_declared"] = int(declared)
            if int(declared) != int(output["cores"]):
                metrics = {"cores_declared": int(declared), "cores_measured": int(output["cores"])}
        return JobResult(
            status,
            metrics=metrics,
            output=output,
            error=None if status is JobStatus.SUCCEEDED else _failure_reason(output["log_tail"]),
        )

    async def fetch_progress(self, handle: JobHandle, tail: int = 5) -> list[dict]:
        """Solver log lines, verbatim.

        Deliberately not parsed into residual series or trends: which way the
        residuals are going is the judgement under test, so the caller gets the
        same text a person would read.
        """
        job_dir = self._job_dir(self._idem(handle))
        rc, out = await self._arun(
            f"tail -n {int(tail)} {job_dir}/job.log {job_dir}/log.* 2>/dev/null | tail -n {int(tail)}"
        )
        if rc != 0:
            return []
        return [{"line": ln} for ln in out.splitlines() if ln.strip()]


def openfoam_from_meta(meta: dict[str, Any]) -> OpenFoamExecutor:

    # ``user`` matters here in a way it does not for a container backend: Open MPI
    # refuses to run as root, so a parallel solve has to come in as an
    # unprivileged account. Defaulting to root silently limits the campaign to
    # serial runs.
    from oncall_flow.transport import runner_from

    run = runner_from(meta)
    command = meta.get("command")
    if not command:
        raise JobBackendError("openfoam backend needs a 'command' template in the campaign meta")
    return OpenFoamExecutor(
        run,
        remote_dir=meta.get("remote_dir", "/root/raven-ops"),
        command=str(command),
        budget=budget_from_meta(meta),
        default_cores=int(meta.get("cores", 1)),
    )


__all__ = ["OpenFoamExecutor", "openfoam_from_meta"]
