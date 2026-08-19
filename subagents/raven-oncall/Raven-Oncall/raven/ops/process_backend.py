"""ProcessExecutor: run each job as a detached process on a remote host.

Docker is the wrong shape for GPU training on these machines. The model and data
live on a network mount, the driver stack belongs to the host, and the job is a
bare ``python3`` invocation -- containerising it would mean building an image to
wrap a command that already runs. So this backend launches the command with
``nohup``, records its pid, and decides terminality from the pid plus the durable
``result.json`` the job writes.

Two things it does that a plain launcher would not:

**It enforces a compute budget across the whole campaign.** The budget is a
property of the experiment, not of any one run, and a loop that may restart could
otherwise buy itself more compute by asking for a longer run each time. Spend is
summed from every job's own ``result.json`` (plus the elapsed time of anything
still running) and the remainder is written into the config, overriding whatever
the caller asked for. When it reaches zero, ``submit`` refuses.

**It measures detection latency on the host clock only.** The instant a job became
terminal is the mtime of its ``result.json``; the host's current time is read in
the same command, and the subtraction happens entirely in host time. A clock skew
of seconds between the loop's machine and the host would otherwise corrupt every
latency silently.
"""

from __future__ import annotations

import asyncio
import base64
import json
import shlex
from collections.abc import Callable
from typing import Any

from raven.ops.budget import SHARED, Budget, accumulate, from_meta as budget_from_meta
from raven.ops.backend import (
    JobBackend,
    JobBackendError,
    JobHandle,
    JobResult,
    JobSpec,
    JobStatus,
)

# How long a job script may take to write its result after the pid it advertised
# has exited. Long enough for a wrap-up that tails and greps a large solver log
# (13 seconds, measured 2026-08-14), short enough that a job really ended from
# outside is reported within one look.
_GONE_GRACE_TRIES = 10
_GONE_GRACE_SLEEP_S = 3.0

CommandRunner = Callable[[str], tuple[int, str]]

BUDGET_KEY = "budget_gpu_minutes"


def _num(text: str) -> float | None:
    try:
        return float(text)
    except (TypeError, ValueError):
        return None


def _stated_cause(data: dict[str, Any]) -> str:
    """What a failed job said about itself, never the empty string.

    ``error`` is one of several names a job gives this. Reading only that name
    dropped everything a run had written about why it stopped: measured
    2026-08-17, a CalculiX trial ended rc=201 with
    ``increment_size_too_small: true``, ``attempts_beyond_first: 39`` and
    ``error_lines: "*ERROR: increment size smaller than minimum"``, and the
    ledger recorded an empty cause. The arm then reported the trial as having
    "left no output, unexplained" -- which is the wording this harness reserves
    for a job ended from outside -- and never used the one line that says
    whether a beam stopped converging or actually collapsed, the whole subject
    of that task.

    An empty cause is worse than a rough one: it reads as "the job said
    nothing", which is a claim about the run rather than about this function.
    """
    for key in ("error", "error_lines", "error_message", "message", "reason"):
        said = str(data.get(key) or "").strip()
        if said:
            return said[:500]
    # Nothing named itself an error, so hand over what the job did state. The
    # flags a domain sets are the cause in every case measured so far.
    facts = [f"{k}={data[k]!r}" for k in sorted(data)
             if k not in ("status", "gpu_minutes_used", "wall_seconds", "threads")
             and not isinstance(data[k], (list, dict))]
    if facts:
        return ("the job wrote a result but named no error; what it did record: "
                + ", ".join(facts))[:500]
    return "the job wrote a result with nothing in it"


# Names this backend writes into a job directory itself. A case that happens to
# contain a file of the same name arrives as a symlink pointing back at it, and a
# redirect through a symlink writes to its target -- so staging a config would edit
# the owner's case, and the status this backend writes would land in it too. The
# links are removed; the files they point at are not.
#
# The launcher and the marker carry a prefix no case would use, because the
# launcher used to be called run.sh -- which is what a case calls its own entry
# script. A command that said `sh ./run.sh` then re-executed the launcher instead
# of the case and spun until it was killed by hand, with an empty log. Measured
# 2026-08-18, the first time a case was staged into a job directory.
_LAUNCHER = ".raven-launch.sh"
_MARKER = ".raven-started-at"
# Which of the case's files a run has been seen to write, per case, remembered on
# the machine. Not in the campaign's meta.json: that file is the apparatus'
# declaration and is refused if it changes, while this list grows as rounds run.
_WRITES_FILE = ".raven-case-writes.json"
_RESERVED = ("config.json", "result.json", "pid", "job.log", _LAUNCHER, _MARKER)


class ProcessExecutor(JobBackend):
    name = "process"

    def __init__(
        self,
        run: CommandRunner,
        *,
        remote_dir: str,
        command: str,
        budget_minutes_total: float | None = None,
        budget: Budget | None = None,
        objective: dict[str, Any] | None = None,
        staged_case: str = "",
        prefix: str = "ops-",
    ) -> None:
        """``command`` is a template expanded with ``{job_dir}`` and ``{config}``.

        ``budget`` is the campaign's declared allowance; ``None`` means none was
        declared, which is not zero -- nothing is refused and the spend is still
        measured and reported, because an unbounded run is exactly where the only
        way to notice it has gone long is the number itself.

        ``objective`` is the campaign's declared ``{"metric", "direction"}``. Without
        it there is no deliverable: which point of a curve is the good one depends on
        whether the number goes up or down, and a guessed direction names the worst
        checkpoint as confidently as the best.
        """
        self._run = run
        self._remote_dir = remote_dir.rstrip("/")
        self._command = command
        # ``budget`` carries the unit and the accumulation rule with the number.
        # ``budget_minutes_total`` remains for callers that predate it and means
        # what it always did here: GPU minutes on one device, so an overlap is
        # occupancy and counts once.
        self._budget_decl = budget or (
            Budget(unit="gpu-minute", total=float(budget_minutes_total), overlap=SHARED)
            if budget_minutes_total is not None
            else None
        )
        self._budget = self._budget_decl.total if self._budget_decl else None
        self._objective = dict(objective or {})
        self._staged_case = staged_case.rstrip("/")
        self._prefix = prefix
        self._case_written: dict[str, list[str]] = {}
        self._known_writes: list[str] = []
        self._known_writes_loaded = False
        self._polls: dict[str, int] = {}
        self._latency_ms: dict[str, int] = {}
        self._finish_unknown: dict[str, str] = {}
        self._unmeasured: dict[str, str] = {}

    # ---- paths ----

    def _job_dir(self, idem_key: str) -> str:
        return f"{self._remote_dir}/jobs/{idem_key}"

    def _shadow_tree_cmd(self, job_dir: str) -> str:
        """Give this trial the owner's case without copying its bytes, or nothing.

        ``cp -as`` builds real directories holding symlinks to the files, so output
        a run creates lands in the trial's own directory while the mesh, the tables
        and the binaries stay shared and read-only. A case of several GB stages in
        under a second and costs no disk -- measured on the FEA arena, 1.6 KB of
        links against 7.0 MB of case.

        What it does NOT prevent is a run overwriting a file that already exists:
        that write follows the symlink into the case. ``fetch_result`` measures
        exactly which files that happened to, which is the point of doing it this
        way rather than trusting the case to behave.
        """
        if not self._staged_case:
            return ""
        reserved = " ".join(f"{job_dir}/{name}" for name in _RESERVED)
        cmd = f"cp -asf {self._staged_case}/. {job_dir}/ && rm -f {reserved} && "
        for rel in self._known_writes:
            src = f"{self._staged_case}/{rel}"
            dst = f"{job_dir}/{rel}"
            # A link removed and replaced by the file it pointed at. Done for every
            # path a run has been seen to write, so the write lands here instead of
            # in the case, and rounds stop overwriting each other's inputs. Nothing
            # is judged: a file that was written has to be a copy.
            cmd += (f"rm -f {shlex.quote(dst)} && cp -p {shlex.quote(src)} "
                    f"{shlex.quote(dst)} && ")
        return cmd

    def _idem(self, handle: JobHandle) -> str:
        return handle.job_id[len(self._prefix) :]

    async def _arun(self, cmd: str) -> tuple[int, str]:
        return await asyncio.to_thread(self._run, cmd)

    # ---- budget ----

    async def spent_minutes(self) -> float:
        """Minutes this campaign has held the device, whatever ended the runs.

        Four cases per job, and the third is the one that matters. A job killed by
        ``cancel`` never writes ``result.json``: SIGTERM does not run Python's
        ``finally``, so the job's own accounting is lost. Counting that as zero
        refunds every minute the run burned -- and it refunds it precisely when the
        loop does the right thing by stopping early, so a loop that kills and
        resubmits would get unlimited compute. The job's last progress line carries
        its own ``elapsed_s``, which is exact and fsynced, so that is used instead.

        A job that leaves nothing at all to measure is counted as zero and
        recorded in ``unmeasured_spend``, because a silent zero here is the same
        refund by another route.

        The per-job durations are then unioned rather than summed. A campaign's
        device is fixed in its command template -- ``{config}`` and ``{job_dir}``
        are the only substitutions, so every job of one campaign runs on the same
        one -- and two jobs sharing it for a wall-clock minute occupy it for a
        minute, not two. Summing charged twice: measured on 2026-08-06 (round 11),
        a round of two configs was billed 39.4 minutes over 20 minutes of wall
        clock, and the campaign ran to 152.38 of a 140-minute budget while its own
        reading, taken from one job's progress, said it had room. Overlap is the
        normal case here, not the exception: a round submits its configs together.

        A job whose start time is unreadable cannot be placed on the timeline, so
        its duration is added on its own. That over-charges if it overlapped, which
        is the safe direction for a budget.
        """
        script = (
            f"cd {self._remote_dir}/jobs 2>/dev/null || exit 0; "
            "for d in */; do d=${d%/}; "
            'R="-"; if [ -f "$d/result.json" ]; then '
            "R=$(sed -n 's/.*\"gpu_minutes_used\": *\\([0-9.]*\\).*/\\1/p' \"$d/result.json\" | tail -1); "
            'fi; '
            'A=0; if [ -f "$d/pid" ] && kill -0 "$(cat "$d/pid")" 2>/dev/null; then A=1; fi; '
            'S="-"; if [ -f "$d/pid" ]; then S=$(stat -c %Y "$d/pid"); fi; '
            'E="-"; M="-"; if [ -f "$d/progress.jsonl" ]; then '
            "E=$(tail -1 \"$d/progress.jsonl\" | sed -n 's/.*\"elapsed_s\": *\\([0-9.]*\\).*/\\1/p'); "
            'M=$(stat -c %Y "$d/progress.jsonl"); fi; '
            'printf \'%s|%s|%s|%s|%s|%s\\n\' "$d" "$R" "$A" "$S" "$E" "$M"; '
            'done; printf \'NOW|%s\\n\' "$(date -u +%s)"'
        )
        rc, out = await self._arun(script)
        if rc != 0:
            return 0.0
        rows, now = [], None
        for line in out.splitlines():
            parts = line.strip().split("|")
            if len(parts) == 2 and parts[0] == "NOW":
                now = _num(parts[1])
            elif len(parts) == 6:
                rows.append(parts)
        unplaced = 0.0
        spans: list[tuple[float, float, float]] = []
        self._unmeasured.clear()
        for key, result_min, alive, started, elapsed, prog_mtime in rows:
            rm, st, el, pm = _num(result_min), _num(started), _num(elapsed), _num(prog_mtime)
            if rm is not None:
                minutes = rm
            elif alive == "1" and st is not None and now is not None:
                minutes = max(0.0, (now - st) / 60.0)
            elif el is not None:
                minutes = el / 60.0
            elif pm is not None and st is not None:
                minutes = max(0.0, (pm - st) / 60.0)
            else:
                self._unmeasured[key] = "killed with nothing to measure its spend from"
                continue
            if st is None:
                unplaced += minutes
            else:
                # Width one: a training job here has the device to itself, and the
                # campaign's command template pins which one. A backend whose jobs
                # are decomposed reports its own width instead.
                spans.append((st, st + minutes * 60.0, 1.0))
        return unplaced + accumulate(spans, overlap=self.budget_overlap)

    @property
    def budget_overlap(self) -> str:
        """How this campaign's concurrent spend adds up, from its declaration."""
        return self._budget_decl.overlap if self._budget_decl else SHARED

    @property
    def budget_unit(self) -> str:
        """What this campaign spends, for printing. Never interpreted."""
        return self._budget_decl.unit if self._budget_decl else "unit"

    def unmeasured_spend(self) -> dict[str, str]:
        """Jobs whose spend could not be measured, and why. Their minutes are
        missing from the total, so this must be read alongside it."""
        return dict(self._unmeasured)

    async def remaining_minutes(self) -> float | None:
        if self._budget is None:
            return None
        return max(0.0, self._budget - await self.spent_minutes())

    # ---- artifacts ----

    async def list_artifacts(self, handle: JobHandle, pattern: str = "step-*") -> tuple[str, ...] | None:
        """Names of a job's artifact directories, as the host has them now.

        Raw listing, deliberately: which one is best is a comparison the loop is
        being scored on, so it is not made here. ``None`` means the listing could
        not be taken, which is not the same as an empty directory and must not be
        reported as one.
        """
        job_dir = self._job_dir(self._idem(handle))
        rc, out = await self._arun(
            f"cd {job_dir} 2>/dev/null || exit 3; ls -d {pattern} 2>/dev/null; exit 0"
        )
        if rc != 0:
            return None
        return tuple(line.strip().rstrip("/") for line in out.splitlines() if line.strip())

    # ---- JobBackend ----

    async def submit(self, spec: JobSpec) -> JobHandle:
        job_id = f"{self._prefix}{spec.idem_key}"
        job_dir = self._job_dir(spec.idem_key)

        # Idempotent by idem_key: a job that is running, or that already finished
        # and wrote a result, is never launched again.
        rc, out = await self._arun(
            f"if [ -f {job_dir}/result.json ]; then echo done; "
            f"elif [ -f {job_dir}/pid ] && kill -0 $(cat {job_dir}/pid) 2>/dev/null; then echo running; "
            f"else echo absent; fi"
        )
        if rc == 0 and out.strip() in {"done", "running"}:
            return JobHandle(self.name, job_id)

        config = dict(spec.payload)
        if self._budget is not None:
            remaining = await self.remaining_minutes() or 0.0
            if remaining <= 0:
                raise JobBackendError(
                    f"compute budget exhausted: {self._budget} GPU minutes are spent, "
                    "no further jobs can be submitted"
                )
            # Clamped in one direction only. The guarantee is that a round cannot
            # buy more compute than the experiment has left, so a larger request
            # is cut down to what remains.
            #
            # A SMALLER request is honoured, because it is the allocation the task
            # asks for: told that the total is spent across attempts, a loop that
            # reserves part of it for a second try is doing the right thing.
            # Replacing that with the full remainder cancelled the plan and left
            # the loop believing the run would stop at the time it had asked for.
            # Measured 2026-08-06: a first round submitted at 40 of 140 minutes ran
            # under 140, and half an hour later the loop declined to act because,
            # on its own reading, the run had thirty minutes left and would end by
            # itself. Its reasoning was sound; the instrument had made it false.
            #
            # Friction only on the side that was already the easier one is the same
            # mistake as a gate that accepts "keep waiting" and refuses the reasons
            # for acting: never allocating means never having to decide.
            asked = config.get(BUDGET_KEY)
            try:
                asked = float(asked) if asked is not None else None
            except (TypeError, ValueError):
                asked = None
            # Zero or negative is a broken field, not an allocation -- honouring it
            # would look like an instant, silent failure.
            allowed = min(asked, remaining) if asked is not None and asked > 0 else remaining
            config[BUDGET_KEY] = round(allowed, 3)

        await self._load_known_writes()
        cfg_path = f"{job_dir}/config.json"
        cmd = self._command.format(job_dir=job_dir, config=cfg_path)
        # The command goes into a launcher script rather than onto the ssh command
        # line, for two reasons. Backgrounding with `&` applies to the whole `&&`
        # chain, so writing the pid on the same line runs it before mkdir has
        # finished; and the command carries quotes that would have to survive two
        # levels of shell. The script writes its own pid and then execs, so the pid
        # file holds the training process itself rather than a wrapper.
        launcher = (
            "#!/bin/sh\n"
            f"cd {job_dir}\n"
            "echo $$ > pid\n"
            f"exec {cmd}\n"
        )
        staged = await self._arun(
            f"mkdir -p {job_dir} && "
            f"{self._shadow_tree_cmd(job_dir)}"
            f"echo {shlex.quote(base64.b64encode(json.dumps(config).encode()).decode())}"
            f" | base64 -d > {cfg_path} && "
            f"echo {shlex.quote(base64.b64encode(launcher.encode()).decode())}"
            f" | base64 -d > {job_dir}/{_LAUNCHER} && echo staged"
        )
        if staged[0] != 0 or "staged" not in staged[1]:
            raise JobBackendError(f"could not stage {job_id}: {staged[1].strip()[:300]}")
        # A retry of the same config against the same case lands in this same
        # directory -- that sameness is what idempotency is keyed on -- and the
        # redirect used to truncate, so the earlier attempt's log was gone.
        # Appending would be worse: success is decided by finding "End" in this
        # file, so the first attempt's End would make a failed retry read as a
        # success. Move it aside instead, and keep every attempt.
        rc, out = await self._arun(
            f"cd {job_dir} && "
            f"{{ [ -s job.log ] && mv job.log \"job.log.$(date -u +%Y%m%dT%H%M%SZ)\" ; }} 2>/dev/null; "
            # Stamped per attempt, immediately before the launch, so what it dates
            # is this run and not the staging that preceded it.
            f"touch {_MARKER}; "
            f"(nohup sh {_LAUNCHER} > job.log 2>&1 &) ; sleep 1; cat {job_dir}/pid"
        )
        if rc != 0 or not out.strip():
            raise JobBackendError(f"launch failed for {job_id}: {out.strip()[:300]}")
        return JobHandle(self.name, job_id)

    async def poll(self, handle: JobHandle) -> JobStatus:
        idem = self._idem(handle)
        self._polls[idem] = self._polls.get(idem, 0) + 1
        status = await self._status(idem)
        if status.is_terminal and idem not in self._latency_ms and idem not in self._finish_unknown:
            await self._stamp_terminal(idem)
        return status

    def _probe_cmd(self, job_dir: str) -> str:
        return (
            f"if [ -f {job_dir}/result.json ]; then "
            f"  printf 'result '; python3 -c \"import json,sys;"
            f"print(json.load(open(sys.argv[1])).get('status','unknown'))\" {job_dir}/result.json 2>/dev/null || echo unknown; "
            f"elif [ -f {job_dir}/pid ] && kill -0 $(cat {job_dir}/pid) 2>/dev/null; then echo alive; "
            f"elif [ -f {job_dir}/pid ]; then echo gone; "
            f"else echo absent; fi"
        )

    @staticmethod
    def _read_status(text: str) -> JobStatus | None:
        if text.startswith("result"):
            return JobStatus.SUCCEEDED if text.split()[-1] == "succeeded" else JobStatus.FAILED
        if text == "alive":
            return JobStatus.RUNNING
        if text == "absent":
            return JobStatus.PENDING
        return None

    async def _status(self, idem: str) -> JobStatus:
        job_dir = self._job_dir(idem)
        cmd = self._probe_cmd(job_dir)
        rc, out = await self._arun(cmd)
        if rc != 0:
            return JobStatus.RUNNING  # unreachable host is not a job outcome
        decided = self._read_status(out.strip())
        if decided is not None:
            return decided
        # "gone": the pid in the file is no longer running and there is no result
        # yet. That is not the same as a job that ended from outside, because the
        # pid file does not name the job -- a job script writes its solver's pid
        # there so the backend's cancel can reach the solver, and the script
        # itself is still running its own wrap-up after that solver exits.
        #
        # Measured 2026-08-14 on the FEA contact trial: the solver exited at
        # 11:41:18, the probe read "gone" at 11:41:22 and recorded "ended from
        # outside", and the script wrote result.json saying succeeded rc=0 at
        # 11:41:31 -- thirteen seconds spent on a final progress line, a log
        # tail and several greps over a 2 MB solver log. The ledger then said
        # failed while the job said succeeded, and the loop spent the rest of
        # that campaign unsure which was true, ending it with 84% of the budget
        # unspent.
        #
        # So a first "gone" only starts a wait. A job truly ended from outside
        # simply stays gone and is reported that way once the wait is over.
        for _ in range(_GONE_GRACE_TRIES):
            await asyncio.sleep(_GONE_GRACE_SLEEP_S)
            rc, out = await self._arun(cmd)
            if rc != 0:
                return JobStatus.RUNNING
            decided = self._read_status(out.strip())
            if decided is not None:
                return decided
        return JobStatus.FAILED

    async def _stamp_terminal(self, idem: str) -> None:
        job_dir = self._job_dir(idem)
        rc, out = await self._arun(
            f"date -u +%s.%N; stat -c %Y {job_dir}/result.json 2>/dev/null || echo missing"
        )
        lines = [line.strip() for line in out.splitlines() if line.strip()]
        if rc != 0 or len(lines) < 2 or lines[-1] == "missing":
            self._finish_unknown[idem] = "no result.json to date the finish from"
            return
        try:
            now, finished = float(lines[0]), float(lines[-1])
        except ValueError:
            self._finish_unknown[idem] = f"unreadable timestamps {lines[:2]}"
            return
        latency = max(0, int(round((now - finished) * 1000)))
        self._latency_ms[idem] = latency
        # Also to disk. This number lives only in the executor's memory
        # otherwise, so after the process that measured it exits there is no way
        # to recover it -- an observer writing up the run afterwards can only
        # subtract two timestamps and hope they mean the same thing.
        payload = json.dumps(
            {"finished_at_host": finished, "first_observed_host": now, "detection_latency_ms": latency},
            ensure_ascii=False,
        )
        await self._arun(
            f"echo {shlex.quote(base64.b64encode(payload.encode()).decode())}"
            f" | base64 -d > {job_dir}/detection.json"
        )

    async def fetch_result(self, handle: JobHandle) -> JobResult:
        idem = self._idem(handle)
        rc, out = await self._arun(f"cat {self._job_dir(idem)}/result.json")
        if rc != 0:
            # A job that is still running has no result file either, and saying it
            # was ended from outside is then simply false. This function had no
            # notion of "still running": absence of the file had one explanation.
            # Measured 2026-08-17, mid-run on a contact trial -- ops_outputs
            # answered "FAILED (ended from outside)" for a job whose heartbeat was
            # 15 seconds old, and the arm had to argue the harness down from its
            # own verdict using the progress file. It got that right; it should not
            # have had to.
            alive_rc, alive = await self._arun(
                f'if [ -f {self._job_dir(idem)}/pid ] && '
                f'kill -0 "$(cat {self._job_dir(idem)}/pid)" 2>/dev/null; '
                f"then echo alive; else echo gone; fi"
            )
            if alive_rc == 0 and alive.strip() == "alive":
                return JobResult(
                    JobStatus.RUNNING,
                    error="still running: no result yet, which is what a job that has "
                          "not finished looks like. Read its progress instead.",
                )
            # No result.json means the script never reached its own exit path, so
            # it was ended from outside and the reason is not in what it left
            # behind. The harness knows that for certain and used to drop it,
            # handing over a bare log tail instead -- and a tail can look like
            # anything. Measured 2026-08-14: the last thirty lines of a killed
            # training job were the progress bars of the previous checkpoint
            # write, and the loop read them as the crash site and named a code
            # fault that did not exist. The lines still come along, after the
            # verdict rather than in place of it.
            _, log = await self._arun(f"tail -n 30 {self._job_dir(idem)}/job.log")
            tail = log.strip()[:500]
            said = ("the job was ended from outside before it could record a result, "
                    "so the reason is not in its own output")
            detail = f", and its last lines were:\n{tail}" if tail else ", and it left no log either"
            return JobResult(JobStatus.FAILED, error=said + detail)
        try:
            data = json.loads(out)
        except json.JSONDecodeError as exc:
            raise JobBackendError(f"bad result.json for {handle.job_id}: {exc}") from None
        metrics: dict[str, float] = {}
        # Only the value the run ended on. Exposing the highest point of the curve
        # HERE would hand over the one judgement being measured -- which step was
        # the good one to stop at -- and a scalar named anything else leaves the
        # metric with nothing to read, so the tool reports "no successful trial"
        # for a run that succeeded. The raw pairs stay in output for the loop to
        # reason over itself. The peak reaches the reader as a deliverable instead,
        # below, and only for a terminal run.
        #
        # No name is invented when the campaign declares none. This backend is
        # shared with every domain that runs a plain command, and it used to label
        # the number "ndcg" by default: a CFD run's residual was filed under a name
        # it had never asked for, and read back as "no trial reported ndcg". An
        # unlabelled number is recoverable -- the raw pairs stay in ``output``, and
        # the reader says what is missing -- while a wrongly labelled one is not.
        metric = str(self._objective.get("metric") or "")
        points = data.get("eval_points") or []
        if points and metric:
            metrics[metric] = float(points[-1][1])
        if data.get("gpu_minutes_used") is not None:
            metrics["gpu_minutes_used"] = float(data["gpu_minutes_used"])
        written = await self._case_files_written(idem)
        if written is not None:
            data["case_files_written"] = written
        status = JobStatus.SUCCEEDED if data.get("status") == "succeeded" else JobStatus.FAILED
        deliverable = (
            self._deliverable(data, metric, self._job_dir(idem))
            if status is JobStatus.SUCCEEDED
            else None
        )
        return JobResult(status, metrics=metrics, output=data, deliverable=deliverable,
                         error=None if status is JobStatus.SUCCEEDED else _stated_cause(data))

    def _writes_path(self) -> str:
        return f"{self._remote_dir}/{_WRITES_FILE}"

    async def _load_known_writes(self) -> None:
        """The paths earlier rounds were seen to write, read once per backend.

        Keyed by case: one remote directory can hold jobs for more than one
        campaign, and a list learned about one case says nothing about another.
        """
        if self._known_writes_loaded or not self._staged_case:
            return
        self._known_writes_loaded = True
        rc, out = await self._arun(f"cat {self._writes_path()} 2>/dev/null")
        if rc != 0 or not out.strip():
            return
        try:
            stored = json.loads(out)
        except ValueError:
            return
        if isinstance(stored, dict):
            found = stored.get(self._staged_case)
            if isinstance(found, list):
                self._known_writes = [str(x) for x in found]

    async def _remember_writes(self, paths: list[str]) -> None:
        """Add newly measured paths to the remembered list, on the machine.

        Read-modify-write of a small JSON file. Two campaigns finishing a trial in
        the same instant could lose one entry, and the next round measures it again
        -- a lost entry costs one more round of the same finding, while a lock here
        would be a lock held across an ssh round trip.
        """
        fresh = [p for p in paths if p not in self._known_writes]
        if not fresh:
            return
        self._known_writes = sorted({*self._known_writes, *fresh})
        payload = base64.b64encode(
            json.dumps({self._staged_case: self._known_writes}).encode()
        ).decode()
        await self._arun(
            f"python3 - <<'PY' 2>/dev/null || true\n"
            f"import base64, json, os\n"
            f"path = {self._writes_path()!r}\n"
            f"add = json.loads(base64.b64decode({payload!r}))\n"
            f"cur = {{}}\n"
            f"if os.path.exists(path):\n"
            f"    try:\n"
            f"        cur = json.load(open(path))\n"
            f"    except Exception:\n"
            f"        cur = {{}}\n"
            f"for case, rels in add.items():\n"
            f"    cur[case] = sorted(set(cur.get(case, [])) | set(rels))\n"
            f"open(path, 'w').write(json.dumps(cur))\n"
            f"PY"
        )

    async def _case_files_written(self, idem: str) -> list[str] | None:
        """Files in the owner's case this run modified, or None if not measured.

        An empty list is a finding, not an absence: it says every round can run at
        the same time, because nothing they share gets written. A non-empty one
        names the files that have to be real copies before trials run in parallel,
        and until then each round is overwriting what the last one read.

        Measured once per trial and remembered -- the result cannot change after
        the run has ended, and the case can be a large tree on a shared mount.
        """
        if not self._staged_case:
            return None
        if idem in self._case_written:
            return self._case_written[idem]
        marker = f"{self._job_dir(idem)}/{_MARKER}"
        rc, out = await self._arun(
            f"[ -f {marker} ] || exit 3; "
            f"find {self._staged_case} -type f -newer {marker} 2>/dev/null | head -40"
        )
        if rc != 0:
            return None
        prefix = self._staged_case + "/"
        found = [
            line.strip()[len(prefix):] if line.strip().startswith(prefix) else line.strip()
            for line in out.splitlines()
            if line.strip()
        ]
        self._case_written[idem] = found
        await self._remember_writes(found)
        return found

    def _deliverable(self, data: dict, metric: str, job_dir: str) -> dict[str, Any] | None:
        """The checkpoint this run would hand over, or None if it cannot be named.

        Only reachable from ``fetch_result``, which runs only on a terminal job: the
        peak of a curve that is still moving is the kill-or-wait decision itself.

        The step named here survives pruning by construction -- the training script's
        ``prune_checkpoints`` keeps the most recent few PLUS the best-scoring step --
        so this never points at a directory that was deleted to save disk. Measured
        2026-08-07 on four runs; the one that peaked at step 800 kept four
        checkpoints, not three.
        """
        direction = str(self._objective.get("direction") or "").lower()
        if direction not in ("max", "min"):
            return None
        pairs = [
            (int(step), float(value))
            for step, value in (data.get("eval_points") or [])
        ]
        if not pairs:
            return None
        pick = max if direction == "max" else min
        step, value = pick(pairs, key=lambda kv: kv[1])
        run_dir = str(data.get("run_dir") or job_dir).rstrip("/")
        return {"ref": f"{run_dir}/step-{step}", "label": metric, "value": value}

    async def cancel(self, handle: JobHandle) -> None:
        job_dir = self._job_dir(self._idem(handle))
        await self._arun(
            f"if [ -f {job_dir}/pid ]; then p=$(cat {job_dir}/pid); "
            f"kill -TERM $p 2>/dev/null; sleep 2; kill -9 $p 2>/dev/null; fi; true"
        )

    async def fetch_progress(self, handle: JobHandle, tail: int = 5) -> list[dict]:
        rc, out = await self._arun(
            f"tail -n {int(tail)} {self._job_dir(self._idem(handle))}/progress.jsonl"
        )
        if rc != 0:
            return []
        rows = []
        for line in out.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return rows

    # ---- measurement surface (evals read these; orchestration never does) ----

    def detection_latency_ms(self, idem_key: str) -> int | None:
        return self._latency_ms.get(idem_key)

    def poll_count(self, idem_key: str | None = None) -> int:
        if idem_key is None:
            return sum(self._polls.values())
        return self._polls.get(idem_key, 0)

    def unknown_finish_times(self) -> dict[str, str]:
        return dict(self._finish_unknown)


def process_from_meta(meta: dict[str, Any]) -> JobBackend:
    import os

    from raven.ops import make_ssh_runner

    host = str(meta.get("host") or "").strip()
    if not host and meta.get("connection"):
        # The campaign names a connection that the registry no longer has, so
        # nothing filled the address in. A KeyError here reads to a caller as "the
        # tool wants a host", and the repair it invites is to pass one by hand or
        # to edit the campaign's own declaration -- both measured 2026-08-17.
        raise JobBackendError(
            f"campaign has no address to run on: connection {meta['connection']!r} "
            "is not in the connection registry"
        )
    run = make_ssh_runner(
        host, int(meta.get("port", 22)),
        os.path.expanduser(meta.get("key", "~/.ssh/id_rsa")),
        # A connection names the account to log in as. Dropping it here would
        # make that field one more setting that is written, accepted and does
        # nothing -- the failure this line of work exists to stop.
        user=str(meta.get("user") or "root"),
    )
    command = meta.get("command")
    if not command:
        raise JobBackendError("process backend needs a 'command' template in the campaign meta")
    declared = budget_from_meta(meta)
    objective = meta.get("objective")
    return ProcessExecutor(
        run,
        remote_dir=meta.get("remote_dir", "/root/raven-ops"),
        command=str(command),
        budget=declared,
        objective=objective if isinstance(objective, dict) else None,
        staged_case=str(meta.get("staged_case") or ""),
    )
