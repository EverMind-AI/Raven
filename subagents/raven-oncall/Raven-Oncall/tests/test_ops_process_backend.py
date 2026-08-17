"""ProcessExecutor against a fake command runner.

The runner is injected, so command construction and output parsing are testable
without a host. The cases that matter are not the happy path: they are the ones
where a loop with restart authority could otherwise get more compute than the
experiment allocated, and the ones where a missing number would silently read as
a good result.
"""

from __future__ import annotations

import base64
import json
import re

import pytest

from raven.ops.backend import JobBackendError, JobSpec, JobStatus
from raven.ops.process_backend import ProcessExecutor

CMD = "env CUDA_VISIBLE_DEVICES=1 python3 /frozen/train.py --config {config} --run-dir {job_dir}"


class FakeHost:
    """Just enough of a host: a job table, a clock, and a result store."""

    def __init__(self) -> None:
        self.jobs: dict[str, dict] = {}
        self.now = 1_700_000_000.0
        self.launched: list[str] = []
        self.killed: list[str] = []
        self.staged: list[str] = []
        self.detection: dict[str, dict] = {}
        self.configs: dict[str, dict] = {}

    @staticmethod
    def _key(cmd: str) -> str:
        """The job key out of the first /jobs/<key>/ in the command.

        Splitting on "/jobs/" and taking up to the next "/" is not enough: the
        submit command chains several paths with && and the naive split swallows
        the rest of the line.
        """
        m = re.search(r"/jobs/([A-Za-z0-9_.:-]+)", cmd)
        return m.group(1) if m else ""

    def __call__(self, cmd: str) -> tuple[int, str]:
        if cmd.startswith("date -u +%s.%N; stat -c %Y"):
            key = self._key(cmd)
            job = self.jobs.get(key, {})
            if "finished_at" not in job:
                return 0, f"{self.now}\nmissing"
            return 0, f"{self.now}\n{job['finished_at']}"
        if cmd.startswith("cd ") and "/jobs 2>/dev/null" in cmd:
            lines = []
            for key, job in self.jobs.items():
                res = job["result"].get("gpu_minutes_used", 0) if "result" in job else "-"
                alive = 1 if job.get("alive") else 0
                started = job.get("started_at", "-")
                prog = job.get("progress") or []
                elapsed = prog[-1].get("elapsed_s", "-") if prog else "-"
                mtime = job.get("progress_mtime", "-") if prog else "-"
                lines.append(f"{key}|{res}|{alive}|{started}|{elapsed}|{mtime}")
            lines.append(f"NOW|{self.now}")
            return 0, "\n".join(lines)
        if cmd.startswith("if [ -f ") and "result.json ]; then echo done;" in cmd:
            key = self._key(cmd)
            job = self.jobs.get(key)
            if job is None:
                return 0, "absent"
            if "result" in job:
                return 0, "done"
            return 0, "running" if job.get("alive") else "absent"
        if "base64 -d > " in cmd and cmd.rstrip().endswith("detection.json"):
            import base64 as _b64

            blob = cmd.split("echo ", 1)[1].split(" | base64 -d", 1)[0].strip().strip("'")
            self.detection[self._key(cmd)] = json.loads(_b64.b64decode(blob))
            return 0, ""
        if cmd.startswith("mkdir -p "):
            key = self._key(cmd)
            blob = cmd.split("echo ", 1)[1].split(" | base64 -d", 1)[0].strip().strip("'")
            self.configs[key] = json.loads(base64.b64decode(blob))
            self.staged.append(key)
            return 0, "staged"
        if cmd.startswith("cd ") and "nohup sh run.sh" in cmd:
            key = self._key(cmd)
            self.jobs[key] = {"alive": True, "started_at": self.now}
            self.launched.append(key)
            return 0, "4242"
        if cmd.startswith("if [ -f ") and "printf 'result '" in cmd:
            key = self._key(cmd)
            job = self.jobs.get(key)
            if job is None:
                return 0, "absent"
            if "result" in job:
                return 0, f"result {job['result'].get('status', 'unknown')}"
            return 0, "alive" if job.get("alive") else "gone"
        if cmd.startswith("if [ -f ") and "kill -TERM" in cmd:
            key = self._key(cmd)
            self.killed.append(key)
            if key in self.jobs:
                self.jobs[key]["alive"] = False
            return 0, ""
        if cmd.startswith("cat ") and cmd.endswith("result.json"):
            key = self._key(cmd)
            job = self.jobs.get(key, {})
            if "result" not in job:
                return 1, "no such file"
            return 0, json.dumps(job["result"])
        if cmd.startswith("tail -n") and "progress.jsonl" in cmd:
            key = self._key(cmd)
            return 0, "\n".join(json.dumps(r) for r in self.jobs.get(key, {}).get("progress", []))
        if cmd.startswith("tail -n") and "job.log" in cmd:
            return 0, "traceback: boom"
        return 0, ""

    # -- helpers a test uses to move the world --

    def finish(self, key: str, *, status="succeeded", minutes=10.0, points=None) -> None:
        self.jobs[key]["alive"] = False
        self.jobs[key]["finished_at"] = self.now
        self.jobs[key]["result"] = {
            "status": status,
            "gpu_minutes_used": minutes,
            "eval_points": points if points is not None else [[200, 0.35], [400, 0.37]],
        }


def _exe(host, budget=None):
    return ProcessExecutor(host, remote_dir="/w", command=CMD, budget_minutes_total=budget)


def _spec(cfg, key="j1"):
    return JobSpec(payload=cfg, idem_key=key)


@pytest.mark.asyncio
async def test_submit_launches_the_command_with_the_config_written_out():
    host = FakeHost()
    await _exe(host).submit(_spec({"lr": 2e-6}))
    assert host.launched == ["j1"]
    assert host.configs["j1"]["lr"] == 2e-6


@pytest.mark.asyncio
async def test_a_running_job_is_not_launched_twice():
    host = FakeHost()
    exe = _exe(host)
    await exe.submit(_spec({"lr": 2e-6}))
    await exe.submit(_spec({"lr": 2e-6}))
    assert host.launched == ["j1"], "the idempotency key must resolve to the same job"


@pytest.mark.asyncio
async def test_a_finished_job_is_not_relaunched():
    host = FakeHost()
    exe = _exe(host)
    await exe.submit(_spec({"lr": 2e-6}))
    host.finish("j1")
    await exe.submit(_spec({"lr": 2e-6}))
    assert host.launched == ["j1"]


@pytest.mark.asyncio
async def test_status_follows_the_pid_then_the_result_file():
    host = FakeHost()
    exe = _exe(host)
    handle = await exe.submit(_spec({"lr": 2e-6}))
    assert await exe.poll(handle) is JobStatus.RUNNING
    host.finish("j1", status="succeeded")
    assert await exe.poll(handle) is JobStatus.SUCCEEDED


@pytest.mark.asyncio
async def test_a_process_that_vanished_without_a_result_is_a_failure():
    host = FakeHost()
    exe = _exe(host)
    handle = await exe.submit(_spec({"lr": 2e-6}))
    host.jobs["j1"]["alive"] = False
    assert await exe.poll(handle) is JobStatus.FAILED, (
        "gone with no result is a failure, not a pending job to keep waiting on"
    )


@pytest.mark.asyncio
async def test_an_unreachable_host_is_not_reported_as_a_job_outcome():
    host = FakeHost()
    exe = _exe(host)
    handle = await exe.submit(_spec({"lr": 2e-6}))

    def broken(cmd):
        return 255, "ssh: connect timed out"

    exe._run = broken
    assert await exe.poll(handle) is JobStatus.RUNNING, (
        "an SSH failure says nothing about the job; calling it failed would invent an outcome the host never reported"
    )


# ---- the budget: the reason this backend is not a plain launcher ----


@pytest.mark.asyncio
async def test_the_config_budget_is_replaced_by_what_is_actually_left():
    host = FakeHost()
    exe = _exe(host, budget=90)
    await exe.submit(_spec({"budget_gpu_minutes": 10_000}, key="j1"))
    assert host.configs["j1"]["budget_gpu_minutes"] == 90, "asking for a longer run must not buy one"


@pytest.mark.asyncio
async def test_a_restart_only_gets_the_remaining_budget():
    host = FakeHost()
    exe = _exe(host, budget=90)
    await exe.submit(_spec({"lr": 2e-6}, key="j1"))
    host.finish("j1", minutes=30.0)

    await exe.submit(_spec({"lr": 5e-6}, key="j2"))
    assert host.configs["j2"]["budget_gpu_minutes"] == pytest.approx(60.0)


@pytest.mark.asyncio
async def test_spend_counts_a_job_that_is_still_running():
    host = FakeHost()
    exe = _exe(host, budget=90)
    await exe.submit(_spec({"lr": 2e-6}, key="j1"))
    host.now += 20 * 60  # twenty minutes later, j1 has not finished

    assert await exe.spent_minutes() == pytest.approx(20.0)
    await exe.submit(_spec({"lr": 5e-6}, key="j2"))
    assert host.configs["j2"]["budget_gpu_minutes"] == pytest.approx(70.0), (
        "a run that never finishes must not hide its spend"
    )


@pytest.mark.asyncio
async def test_submitting_with_the_budget_gone_is_refused():
    host = FakeHost()
    exe = _exe(host, budget=90)
    await exe.submit(_spec({"lr": 2e-6}, key="j1"))
    host.finish("j1", minutes=90.0)

    with pytest.raises(JobBackendError, match="budget exhausted"):
        await exe.submit(_spec({"lr": 5e-6}, key="j2"))


@pytest.mark.asyncio
async def test_with_no_budget_configured_nothing_is_clamped():
    host = FakeHost()
    await _exe(host).submit(_spec({"budget_gpu_minutes": 5}, key="j1"))
    assert host.configs["j1"]["budget_gpu_minutes"] == 5


# ---- detection latency, on the host clock only ----


@pytest.mark.asyncio
async def test_detection_latency_is_the_host_side_gap_to_the_first_terminal_poll():
    host = FakeHost()
    exe = _exe(host)
    handle = await exe.submit(_spec({"lr": 2e-6}))
    host.finish("j1")
    host.now += 45  # nobody looked for forty-five seconds

    await exe.poll(handle)
    assert exe.detection_latency_ms("j1") == 45_000


@pytest.mark.asyncio
async def test_a_finish_that_cannot_be_dated_is_reported_not_defaulted_to_zero():
    host = FakeHost()
    exe = _exe(host)
    handle = await exe.submit(_spec({"lr": 2e-6}))
    host.jobs["j1"]["alive"] = False  # gone, no result.json written

    await exe.poll(handle)
    assert exe.detection_latency_ms("j1") is None
    assert "no result.json" in exe.unknown_finish_times()["j1"], "a silent zero would read as instant detection"


@pytest.mark.asyncio
async def test_polls_are_counted_so_wake_overhead_pairs_with_latency():
    host = FakeHost()
    exe = _exe(host)
    handle = await exe.submit(_spec({"lr": 2e-6}))
    for _ in range(3):
        await exe.poll(handle)
    assert exe.poll_count("j1") == 3
    assert exe.poll_count() == 3


# ---- results and progress ----


@pytest.mark.asyncio
async def test_the_result_exposes_last_and_best_without_hiding_the_raw_points():
    host = FakeHost()
    exe = _exe(host)
    exe = ProcessExecutor(host, remote_dir="/w", command=CMD, objective={"metric": "ndcg", "direction": "max"})
    handle = await exe.submit(_spec({"lr": 2e-6}))
    host.finish("j1", points=[[200, 0.35], [400, 0.39], [600, 0.37]])

    res = await exe.fetch_result(handle)
    assert res.metrics["ndcg"] == pytest.approx(0.37)
    assert res.output["eval_points"] == [[200, 0.35], [400, 0.39], [600, 0.37]], (
        "the raw curve must survive; summarising it away would decide for the reader"
    )


@pytest.mark.asyncio
async def test_a_failed_job_carries_its_error_through():
    host = FakeHost()
    exe = _exe(host)
    handle = await exe.submit(_spec({"lr": 2e-6}))
    host.jobs["j1"]["alive"] = False
    host.jobs["j1"]["finished_at"] = host.now
    host.jobs["j1"]["result"] = {
        "status": "failed",
        "error_kind": "cuda_oom",
        "error": "out of memory",
        "gpu_minutes_used": 0.1,
    }

    res = await exe.fetch_result(handle)
    assert res.status is JobStatus.FAILED
    assert "out of memory" in (res.error or "")
    assert res.output["error_kind"] == "cuda_oom", (
        "the failure kind is what separates a broken job from a badly training one"
    )


@pytest.mark.asyncio
async def test_progress_returns_raw_lines_and_tolerates_a_torn_last_line():
    host = FakeHost()
    exe = _exe(host)
    handle = await exe.submit(_spec({"lr": 2e-6}))
    host.jobs["j1"]["progress"] = [{"step": 20, "loss": 1.2}, {"step": 40, "eval_ndcg": 0.35}]

    rows = await exe.fetch_progress(handle, tail=10)
    assert rows == [{"step": 20, "loss": 1.2}, {"step": 40, "eval_ndcg": 0.35}]


@pytest.mark.asyncio
async def test_cancel_kills_the_process():
    host = FakeHost()
    exe = _exe(host)
    handle = await exe.submit(_spec({"lr": 2e-6}))
    await exe.cancel(handle)
    assert host.killed == ["j1"]
    assert await exe.poll(handle) is JobStatus.FAILED


# ---- a killed run must not get its compute back ----


@pytest.mark.asyncio
async def test_a_killed_run_is_charged_from_its_own_last_progress_line():
    host = FakeHost()
    exe = _exe(host, budget=90)
    handle = await exe.submit(_spec({"lr": 2e-6}, key="j1"))
    # It ran for 25 minutes and said so in progress, then was killed. SIGTERM does
    # not run Python's finally, so no result.json exists.
    host.jobs["j1"]["progress"] = [{"step": 900, "elapsed_s": 1500.0}]
    host.jobs["j1"]["progress_mtime"] = host.now + 1500
    await exe.cancel(handle)

    assert await exe.spent_minutes() == pytest.approx(25.0), (
        "counting a killed run as zero refunds its compute, and it refunds it "
        "exactly when the loop does the right thing by stopping early"
    )
    assert await exe.remaining_minutes() == pytest.approx(65.0)


@pytest.mark.asyncio
async def test_killing_and_resubmitting_cannot_buy_extra_compute():
    host = FakeHost()
    exe = _exe(host, budget=90)
    for i, minutes in enumerate([40.0, 40.0], start=1):
        h = await exe.submit(_spec({"lr": 1e-6 * i}, key=f"j{i}"))
        host.jobs[f"j{i}"]["progress"] = [{"step": 1, "elapsed_s": minutes * 60}]
        host.jobs[f"j{i}"]["progress_mtime"] = host.now + minutes * 60
        await exe.cancel(h)
        # The resubmit happens after the kill, so the clock moves with it. Leaving
        # both runs stamped at the same instant would make a sequence look like an
        # overlap, and the two are charged differently on purpose.
        host.now += minutes * 60

    assert await exe.spent_minutes() == pytest.approx(80.0)
    await exe.submit(_spec({"lr": 9e-6}, key="j3"))
    assert host.configs["j3"]["budget_gpu_minutes"] == pytest.approx(10.0)


@pytest.mark.asyncio
async def test_two_configs_sharing_the_device_are_charged_once_for_the_overlap():
    """A campaign's device is fixed in its command template, so a round that
    submits two configs runs them on the same GPU. Summing their durations bills
    the campaign twice for one minute of the device. Measured 2026-08-06 (round
    11): 39.4 minutes charged over 20 minutes of wall clock, and the campaign ran
    to 152.38 of a 140-minute budget while its own reading, taken from one job's
    progress, said it had room."""
    host = FakeHost()
    exe = _exe(host, budget=140)
    for i in (1, 2):
        h = await exe.submit(_spec({"lr": 1e-6 * i}, key=f"j{i}"))
        host.jobs[f"j{i}"]["progress"] = [{"step": 1, "elapsed_s": 20 * 60}]
        host.jobs[f"j{i}"]["progress_mtime"] = host.now + 20 * 60
        await exe.cancel(h)

    assert await exe.spent_minutes() == pytest.approx(20.0)
    assert await exe.remaining_minutes() == pytest.approx(120.0)


@pytest.mark.asyncio
async def test_a_partial_overlap_is_charged_end_to_end():
    """Neither the sum (60) nor the longer run alone (40): the device was held from
    the first start to the last finish."""
    host = FakeHost()
    exe = _exe(host, budget=140)
    h1 = await exe.submit(_spec({"lr": 1e-6}, key="j1"))
    host.jobs["j1"]["progress"] = [{"step": 1, "elapsed_s": 40 * 60}]
    host.jobs["j1"]["progress_mtime"] = host.now + 40 * 60
    await exe.cancel(h1)

    host.now += 30 * 60
    h2 = await exe.submit(_spec({"lr": 2e-6}, key="j2"))
    host.jobs["j2"]["progress"] = [{"step": 1, "elapsed_s": 20 * 60}]
    host.jobs["j2"]["progress_mtime"] = host.now + 20 * 60
    await exe.cancel(h2)

    assert await exe.spent_minutes() == pytest.approx(50.0)


@pytest.mark.asyncio
async def test_a_gap_between_rounds_is_not_charged():
    """The device is only held while something is running on it."""
    host = FakeHost()
    exe = _exe(host, budget=140)
    h1 = await exe.submit(_spec({"lr": 1e-6}, key="j1"))
    host.jobs["j1"]["progress"] = [{"step": 1, "elapsed_s": 10 * 60}]
    host.jobs["j1"]["progress_mtime"] = host.now + 10 * 60
    await exe.cancel(h1)

    host.now += 120 * 60
    h2 = await exe.submit(_spec({"lr": 2e-6}, key="j2"))
    host.jobs["j2"]["progress"] = [{"step": 1, "elapsed_s": 10 * 60}]
    host.jobs["j2"]["progress_mtime"] = host.now + 10 * 60
    await exe.cancel(h2)

    assert await exe.spent_minutes() == pytest.approx(20.0)


@pytest.mark.asyncio
async def test_a_kill_with_nothing_to_measure_is_recorded_not_silently_free():
    host = FakeHost()
    exe = _exe(host, budget=90)
    handle = await exe.submit(_spec({"lr": 2e-6}, key="j1"))
    await exe.cancel(handle)  # died before writing any progress

    await exe.spent_minutes()
    assert "j1" in exe.unmeasured_spend(), "a silent zero is the same refund by another route"


@pytest.mark.asyncio
async def test_the_result_reports_only_the_value_the_run_ended_on():
    host = FakeHost()
    exe = _exe(host)
    exe = ProcessExecutor(host, remote_dir="/w", command=CMD, objective={"metric": "ndcg", "direction": "max"})
    handle = await exe.submit(_spec({"lr": 2e-6}))
    host.finish("j1", points=[[200, 0.35], [400, 0.39], [600, 0.37]])

    res = await exe.fetch_result(handle)
    assert res.metrics["ndcg"] == pytest.approx(0.37), (
        "named for the metric the campaign declared, or the tool reads a succeeded run as having produced nothing"
    )
    assert "best" not in res.metrics, (
        "the highest point of the curve is the judgement under test; handing it over answers the question for the agent"
    )
    assert res.output["eval_points"] == [[200, 0.35], [400, 0.39], [600, 0.37]]


@pytest.mark.asyncio
async def test_the_detection_latency_is_written_to_disk_not_only_held_in_memory():
    host = FakeHost()
    exe = _exe(host)
    handle = await exe.submit(_spec({"lr": 2e-6}))
    host.finish("j1")
    host.now += 45

    await exe.poll(handle)
    assert exe.detection_latency_ms("j1") == 45_000
    assert host.detection["j1"]["detection_latency_ms"] == 45_000, (
        "held only in memory, the number dies with the process that measured it "
        "and whoever writes the run up can only subtract two timestamps and hope"
    )


@pytest.mark.asyncio
async def test_asking_for_less_than_is_left_is_honoured():
    """Reserving budget for a later attempt is the behaviour the task asks for, so
    the clamp has to be one-directional.

    Measured 2026-08-06: told that the 140 GPU minutes were a total to spend across
    attempts, a loop submitted its first round with ``budget_gpu_minutes: 40`` and
    said in as many words that it was leaving margin for adjustments. The backend
    replaced the 40 with 140. Half an hour later it declined to act because, on its
    own reading, the run had thirty minutes left and would stop by itself -- a
    belief the harness had manufactured. Its plan was right; the instrument had
    quietly cancelled it.

    Refusing a request for MORE than remains is the guarantee and stays. Refusing a
    request for less put friction on the one side that was already the easier one:
    a loop that never allocates never has to decide anything.
    """
    host = FakeHost()
    exe = _exe(host, budget=140)

    await exe.submit(_spec({"budget_gpu_minutes": 40}, key="j1"))

    assert host.configs["j1"]["budget_gpu_minutes"] == pytest.approx(40.0), (
        "a smaller request is a deliberate allocation, not a mistake to correct"
    )


@pytest.mark.asyncio
async def test_asking_for_less_still_leaves_the_rest_available():
    """The reserved part is still there afterwards -- otherwise "leaving margin"
    would mean throwing it away."""
    host = FakeHost()
    exe = _exe(host, budget=140)
    await exe.submit(_spec({"budget_gpu_minutes": 40}, key="j1"))
    host.finish("j1", minutes=40.0)

    await exe.submit(_spec({"lr": 2e-6}, key="j2"))

    assert host.configs["j2"]["budget_gpu_minutes"] == pytest.approx(100.0)


@pytest.mark.asyncio
async def test_a_non_positive_request_falls_back_to_what_is_left():
    """Zero or a negative is not an allocation, it is a broken field; treating it
    as "run for no time" would look like an instant, silent failure."""
    host = FakeHost()
    exe = _exe(host, budget=90)

    await exe.submit(_spec({"budget_gpu_minutes": 0}, key="j1"))
    assert host.configs["j1"]["budget_gpu_minutes"] == pytest.approx(90.0)

    await exe.submit(_spec({"budget_gpu_minutes": -5}, key="j2"))
    assert host.configs["j2"]["budget_gpu_minutes"] == pytest.approx(90.0)


# ---- deliverable ----
#
# What the campaign would hand over at the end. The backend names it because the
# choice is domain-specific and only the backend knows the domain: a fine-tune
# hands over the checkpoint at the best-scoring step, a transient CFD run hands
# over its last converged time directory and has no "best moment" at all. Put the
# choice in the tool layer and the tool layer has to know which domain it is in.
#
# Computed only in fetch_result, which runs only on a terminal job. That is what
# keeps it from handing over the judgement under test: naming the peak of a curve
# that is still moving is the kill-or-wait decision itself.


@pytest.mark.asyncio
async def test_the_deliverable_names_the_step_that_scored_best():
    host = FakeHost()
    exe = ProcessExecutor(
        host,
        remote_dir="/w",
        command=CMD,
        objective={"metric": "ndcg", "direction": "max"},
    )
    handle = await exe.submit(_spec({"lr": 1e-6}))
    host.finish("j1", points=[[200, 0.3512], [1200, 0.362], [1518, 0.3564]])

    res = await exe.fetch_result(handle)

    assert res.deliverable is not None
    assert res.deliverable["value"] == pytest.approx(0.362)
    assert res.deliverable["label"] == "ndcg"
    assert res.deliverable["ref"].endswith("/step-1200"), res.deliverable["ref"]
    assert res.metrics["ndcg"] == pytest.approx(0.3564), (
        "the metric stays the value the run ended on; the deliverable is a separate claim"
    )


@pytest.mark.asyncio
async def test_a_minimised_objective_picks_the_lowest_point():
    """Direction cannot be assumed. For a loss the best point is the smallest, and
    a backend that always takes the maximum would hand over the worst checkpoint."""
    host = FakeHost()
    exe = ProcessExecutor(
        host,
        remote_dir="/w",
        command=CMD,
        objective={"metric": "loss", "direction": "min"},
    )
    handle = await exe.submit(_spec({"lr": 1e-6}))
    host.finish("j1", points=[[200, 0.9], [400, 0.4], [600, 0.6]])

    res = await exe.fetch_result(handle)

    assert res.deliverable["value"] == pytest.approx(0.4)
    assert res.deliverable["ref"].endswith("/step-400")


@pytest.mark.asyncio
async def test_no_declared_objective_means_no_deliverable():
    """A campaign that never said what it optimises gets no deliverable rather than
    a guessed one. Naming the wrong checkpoint is worse than naming none: the reader
    cannot tell a guess from a fact, and the number reads as measured either way."""
    host = FakeHost()
    exe = _exe(host)
    handle = await exe.submit(_spec({"lr": 1e-6}))
    host.finish("j1", points=[[200, 0.35], [400, 0.39]])

    res = await exe.fetch_result(handle)

    assert res.deliverable is None
    assert res.metrics == {"gpu_minutes_used": 10.0}, (
        "the spend still reads -- it is a reading about the run, not the score the "
        "run is judged by -- but there is no invented name for the score. This layer is shared with "
        "every domain that runs a command; labelling the value 'ndcg' by default "
        "is how a CFD run's residual was filed under a name it never asked for and "
        "read back as 'no trial reported ndcg'. The raw pairs stay in output, so an "
        "unlabelled number is recoverable -- a wrongly labelled one is not."
    )
    assert res.output["eval_points"] == [[200, 0.35], [400, 0.39]]


@pytest.mark.asyncio
async def test_a_failed_job_has_no_deliverable():
    host = FakeHost()
    exe = ProcessExecutor(
        host,
        remote_dir="/w",
        command=CMD,
        objective={"metric": "ndcg", "direction": "max"},
    )
    handle = await exe.submit(_spec({"lr": 1e-6}))
    host.jobs["j1"]["alive"] = False
    host.jobs["j1"]["finished_at"] = host.now
    host.jobs["j1"]["result"] = {"status": "failed", "error": "boom", "gpu_minutes_used": 0.1}

    res = await exe.fetch_result(handle)

    assert res.deliverable is None


@pytest.mark.asyncio
async def test_the_declared_metric_name_is_used_not_ndcg():
    """ndcg is hardcoded in this backend today, and the backend is shared with every
    other domain that runs a plain command. A campaign optimising something else got
    its number filed under a name it never asked for."""
    host = FakeHost()
    exe = ProcessExecutor(
        host,
        remote_dir="/w",
        command=CMD,
        objective={"metric": "recall", "direction": "max"},
    )
    handle = await exe.submit(_spec({"lr": 1e-6}))
    host.finish("j1", points=[[200, 0.5], [400, 0.6]])

    res = await exe.fetch_result(handle)

    assert res.metrics["recall"] == pytest.approx(0.6)
    assert "ndcg" not in res.metrics
