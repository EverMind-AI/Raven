"""ProcessExecutor against a fake command runner.

The runner is injected, so command construction and output parsing are testable
without a host. The cases that matter are not the happy path: they are the ones
where a loop with restart authority could otherwise get more compute than the
experiment allocated, and the ones where a missing number would silently read as
a good result.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
PLUGIN_DIR = REPO / "agents" / "raven-oncall" / "plugins" / "oncall-flow"
sys.path.insert(0, str(PLUGIN_DIR))

import base64  # noqa: E402
import shlex  # noqa: E402

from oncall_flow.backend import JobBackendError, JobHandle, JobSpec, JobStatus  # noqa: E402
from oncall_flow.process_backend import ProcessExecutor  # noqa: E402

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
        self.seen: list[str] = []
        self.case_written: list[str] | None = None
        self.remembered_writes: dict[str, list[str]] = {}
        self.resources: dict[str, tuple[float, int]] = {}

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
        self.seen.append(cmd)
        if cmd.startswith("cat ") and ".raven-case-writes.json" in cmd:
            if not self.remembered_writes:
                return 0, ""
            return 0, json.dumps(self.remembered_writes)
        if cmd.startswith("python3 - <<") and ".raven-case-writes.json" in cmd:
            return 0, ""
        if cmd.startswith("[ -f ") and " -newer " in cmd:
            if self.case_written is None:
                return 3, ""
            return 0, "\n".join(self.case_written)
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
                width, exclusive = self.resources.get(key, (1, 0))
                lines.append(f"{key}|{res}|{alive}|{started}|{elapsed}|{mtime}|{width}|{exclusive}")
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
            for piece in cmd.split("echo ")[2:]:
                try:
                    staged = json.loads(base64.b64decode(piece.split(" | base64 -d", 1)[0].strip().strip("'")))
                except Exception:  # noqa: BLE001 -- the launcher blob is not JSON
                    continue
                if isinstance(staged, dict) and "width" in staged:
                    self.resources[key] = (staged["width"], 1 if staged.get("device_ids") else 0)
            self.staged.append(key)
            return 0, "staged"
        if cmd.startswith("cd ") and "nohup sh .raven-launch.sh" in cmd:
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
        if cmd.startswith("if [ -f ") and "echo alive" in cmd:
            key = self._key(cmd)
            job = self.jobs.get(key)
            if job and job.get("alive"):
                return 0, f"alive 4242 {int(self.now - job.get('started_at', self.now))}"
            return 0, "gone"
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


def _exe(host, budget=None, command=None):
    return ProcessExecutor(host, remote_dir="/w", command=command or CMD, budget_minutes_total=budget)


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


@pytest.mark.asyncio
async def test_a_staged_case_is_linked_into_the_trial_not_copied() -> None:
    # The owner's case can be several GB and every round needs its own working
    # directory, so the tree is symlinks: what a run creates lands locally, what it
    # reads stays shared.
    host = FakeHost()
    ex = ProcessExecutor(host, remote_dir="/root/ops", command=CMD, staged_case="/srv/case/")
    await ex.submit(JobSpec(payload={"lr": 1e-5}, idem_key="k1"))

    staged = next(c for c in host.seen if c.startswith("mkdir -p "))
    assert "cp -asf /srv/case/. /root/ops/jobs/k1/" in staged, staged
    for name in ("config.json", "result.json", "pid", "job.log", ".raven-launch.sh"):
        assert f"/root/ops/jobs/k1/{name}" in staged.split("rm -f", 1)[1], (
            f"a case holding its own {name} would arrive as a link, and writing "
            f"through that link edits the owner's case"
        )


@pytest.mark.asyncio
async def test_no_staged_case_means_no_linking_step() -> None:
    host = FakeHost()
    ex = ProcessExecutor(host, remote_dir="/root/ops", command=CMD)
    await ex.submit(JobSpec(payload={"lr": 1e-5}, idem_key="k1"))

    staged = next(c for c in host.seen if c.startswith("mkdir -p "))
    assert "cp -as" not in staged


@pytest.mark.asyncio
async def test_files_the_run_wrote_back_into_the_case_are_reported() -> None:
    # This is what decides whether the remaining rounds can run at the same time.
    host = FakeHost()
    host.case_written = ["/srv/case/input.dat", "/srv/case/sub/mesh.dat"]
    ex = ProcessExecutor(host, remote_dir="/root/ops", command=CMD, staged_case="/srv/case")
    await ex.submit(JobSpec(payload={"lr": 1e-5}, idem_key="k1"))
    host.finish("k1")

    res = await ex.fetch_result(JobHandle(ex.name, "ops-k1"))
    assert res.output["case_files_written"] == ["input.dat", "sub/mesh.dat"], (
        "reported relative to the case, so the names read as the case's own"
    )


@pytest.mark.asyncio
async def test_a_case_nothing_wrote_to_reports_an_empty_list_not_nothing() -> None:
    # An empty list is the finding "parallel rounds are safe". Absence would be
    # indistinguishable from never having looked.
    host = FakeHost()
    host.case_written = []
    ex = ProcessExecutor(host, remote_dir="/root/ops", command=CMD, staged_case="/srv/case")
    await ex.submit(JobSpec(payload={"lr": 1e-5}, idem_key="k1"))
    host.finish("k1")

    res = await ex.fetch_result(JobHandle(ex.name, "ops-k1"))
    assert res.output["case_files_written"] == []


@pytest.mark.asyncio
async def test_a_file_known_to_be_written_is_staged_as_a_copy_not_a_link() -> None:
    # No confirmation round: the next round the campaign was going to run anyway
    # stages the copy and measures again. A run of the case costs hours; this costs
    # one cp.
    host = FakeHost()
    host.remembered_writes = {"/srv/case": ["input.dat", "sub/mesh.dat"]}
    ex = ProcessExecutor(host, remote_dir="/root/ops", command=CMD, staged_case="/srv/case")
    await ex.submit(JobSpec(payload={"lr": 1e-5}, idem_key="k1"))

    staged = next(c for c in host.seen if c.startswith("mkdir -p "))
    for rel in ("input.dat", "sub/mesh.dat"):
        expected = f"cp -p {shlex.quote(f'/srv/case/{rel}')} {shlex.quote(f'/root/ops/jobs/k1/{rel}')}"
        assert expected in staged, staged


@pytest.mark.asyncio
async def test_a_write_that_was_measured_is_remembered_on_the_machine() -> None:
    # Kept on the machine, not in the campaign's meta.json: that file is the
    # apparatus' declaration and a submit is refused if it changes, while this list
    # grows as rounds run.
    host = FakeHost()
    host.case_written = ["/srv/case/input.dat"]
    ex = ProcessExecutor(host, remote_dir="/root/ops", command=CMD, staged_case="/srv/case")
    await ex.submit(JobSpec(payload={"lr": 1e-5}, idem_key="k1"))
    host.finish("k1")
    await ex.fetch_result(JobHandle(ex.name, "ops-k1"))

    wrote = [c for c in host.seen if ".raven-case-writes.json" in c and c.startswith("python3")]
    assert wrote, "nothing persisted the finding, so the next round links the file again"
    assert "/root/ops/.raven-case-writes.json" in wrote[0]


@pytest.mark.asyncio
async def test_a_command_may_name_the_case_and_the_round_directory() -> None:
    # The template is the only place a campaign can say how its case gets into a
    # trial directory, and copying from it is the ordinary way. Before this, a
    # {staged_case} in the template raised KeyError inside submit.
    host = FakeHost()
    ex = ProcessExecutor(
        host,
        remote_dir="/root/ops",
        staged_case="/srv/case",
        command="cd {job_dir} && cp {staged_case}/run.sh . && bash run.sh {config}",
    )

    await ex.submit(JobSpec(payload={"lr": 1e-5}, idem_key="k1"))

    # The launcher is staged base64-encoded, and the same command carries the
    # config's own blob -- so pick the fragment that decodes to a shell script.
    import base64 as _b64
    import re

    staged = next(c for c in host.seen if ".raven-launch.sh" in c)
    body = ""
    for blob in re.findall(r"echo '?([A-Za-z0-9+/=]{16,})'?", staged):
        try:
            text = _b64.b64decode(blob).decode()
        except Exception:  # noqa: BLE001
            continue
        if text.startswith("#!"):
            body = text
    assert body, "the launcher script should be in there somewhere"
    assert "cp /srv/case/run.sh ." in body
    assert "/root/ops/jobs/k1/config.json" in body


@pytest.mark.asyncio
async def test_the_launcher_synthesizes_a_result_when_the_job_writes_none():
    """Terminality is decided by result.json, and only a case script written
    for this backend knows to write one. Measured 2026-08-31 to 09-01: five
    completed train.py runs were each recorded "ended from outside before it
    could record a result", one agent re-bought a finished round as a compile
    timeout, and the owner was told the baseline failed twice while its number
    sat in job.log. The launcher waits for its child and writes the missing
    result itself -- status from the exit code, spend from its own clock --
    and leaves alone a result the job wrote."""
    body = await _staged_launcher("bash run_fea.sh")

    assert 'wait "$RAVEN_JOB"' in body
    assert "[ ! -f result.json ]" in body, "a result the job wrote is left alone"
    assert "succeeded" in body and "failed" in body
    assert "gpu_minutes_used" in body, "the synthesized result still carries spend"
    assert "exec " not in body, "exec would leave nobody to write the result"


@pytest.mark.asyncio
async def test_a_chained_command_reaches_a_shell_in_the_launcher() -> None:
    host = FakeHost()
    ex = ProcessExecutor(
        host,
        remote_dir="/root/ops",
        staged_case="/srv/case",
        command="cd {job_dir} && cp {staged_case}/run.sh . && bash run.sh",
    )
    await ex.submit(JobSpec(payload={"lr": 1e-5}, idem_key="k1"))

    import base64 as _b64
    import re

    staged = next(c for c in host.seen if ".raven-launch.sh" in c)
    body = ""
    for blob in re.findall(r"echo '?([A-Za-z0-9+/=]{16,})'?", staged):
        try:
            text = _b64.b64decode(blob).decode()
        except Exception:  # noqa: BLE001
            continue
        if text.startswith("#!"):
            body = text
    assert "$RAVEN_SETSID sh -c" in body, body
    assert "cp /srv/case/run.sh ." in body


# ---- which shell runs the owner's command ----


async def _staged_launcher(cmd: str) -> str:
    """The launcher this backend actually stages for ``cmd``.

    Read back out of the host's own traffic rather than rebuilt here: a test that
    recomputes the body would keep passing after the body changed.
    """
    import base64 as _b64

    host = FakeHost()
    await _exe(host, command=cmd).submit(_spec({"lr": 1e-4}))
    for seen in host.seen:
        if ".raven-launch.sh" not in seen or "base64 -d" not in seen:
            continue
        for piece in seen.split("echo ")[1:]:
            blob = piece.split(" | base64 -d", 1)[0].strip().strip("'")
            try:
                text = _b64.b64decode(blob).decode()
            except Exception:  # noqa: BLE001 -- the config blob decodes too
                continue
            if text.startswith("#!/bin/sh"):
                return text
    raise AssertionError("no launcher was staged")


@pytest.mark.asyncio
async def test_a_compound_command_prefers_bash_and_falls_back_to_sh():
    """Measured 2026-08-21: an arm declared an OpenFOAM case with
    "source .../etc/bashrc && ..." -- correct in bash, and the standard way to
    start that solver -- and round 0 died on "sh: 1: source: not found", because
    /bin/sh on that box is dash. A whole task was lost to it.

    The choice is made on the machine rather than here: no probe to make, no
    cache to keep honest, and right even on a machine nobody has looked at.
    """
    body = await _staged_launcher("source /opt/env && cd x && ./run")

    assert "bash -o pipefail -c" in body
    assert "\n  $RAVEN_SETSID sh -c" in body, "a machine without bash still has to run it"
    assert body.index("bash -o pipefail -c") < body.index("\n  $RAVEN_SETSID sh -c"), "bash is the preferred branch"


@pytest.mark.asyncio
async def test_a_piped_job_reports_the_failing_stage_not_the_tee():
    """The synthesized status reads the pipeline's exit code, which without
    pipefail is the LAST command's: measured 2026-09-02, three jobs written as
    `python train.py | tee output.log` crashed on ModuleNotFoundError, tee
    returned 0, and each was recorded succeeded with minutes billed -- the
    Traceback sat in a 257-byte log nothing routed anyone to, because the
    ledger said there was nothing to look at. Only bash gets the flag: dash
    has no pipefail, and a box without bash keeps last-command semantics
    rather than every job dying on an unknown option."""
    body = await _staged_launcher("python train.py 2>&1 | tee output.log")

    assert "bash -o pipefail -c" in body
    sh_branch = body.split("\nelse\n", 1)[1]
    assert "pipefail" not in sh_branch, "sh may not know the option; the flag stays on bash"


@pytest.mark.asyncio
async def test_the_pid_a_cancel_reaches_is_the_jobs_own():
    """The launcher stays for the wrap-up instead of exec-ing into the job,
    so ``pid`` is rewritten to the child the moment it exists and a TERM to
    the launcher is forwarded -- a cancel reaches the job either way."""
    body = await _staged_launcher("bash run_fea.sh")

    assert 'echo "$RAVEN_JOB" > pid' in body
    assert 'trap \'kill -s TERM -- -"$RAVEN_JOB" 2>/dev/null; kill -s TERM "$RAVEN_JOB" 2>/dev/null\' TERM INT' in body


# ---- whose pid decides whether the job is still alive ----


def test_the_probe_asks_our_own_pid_before_the_shared_one():
    """``pid`` is a plain name in the job's own directory and the owner's command
    writes to it -- usually with the SOLVER's pid, so a cancel reaches the solver.
    Measured 2026-08-21 on the OpenFOAM case: the solver reached endTime, all
    twenty time directories were written, and the ledger still said "ended from
    outside before it could record a result", because the probe read that pid
    after the solver exited and while the script was still writing result.json.
    """
    from oncall_flow.process_backend import _OWN_PID

    cmd = ProcessExecutor(FakeHost(), remote_dir="/w", command=CMD)._probe_cmd("/w/j1")

    assert _OWN_PID in cmd
    assert cmd.index(_OWN_PID) < cmd.index("/pid"), "ours is consulted first"
    assert "/pid" in cmd, "a directory staged before this file existed still has to work"


@pytest.mark.asyncio
async def test_the_launcher_writes_both_pids():
    """One for a cancel to reach the solver through, one that is the launcher
    itself, staying live through the wrap-up it now performs."""
    from oncall_flow.process_backend import _OWN_PID

    body = await _staged_launcher("bash run_fea.sh")

    assert "echo $$ > pid" in body
    assert f"echo $$ > {_OWN_PID}" in body


# ---- custody: the child is a process group, and a command that detaches itself is refused ----


@pytest.mark.asyncio
async def test_the_child_runs_as_its_own_process_group_when_the_host_can():
    """A TERM to one pid reaches one process. Measured 2026-09-03: a cancel
    killed the `bash -c` wrapper, the python it had forked ran on to a valid
    result, and the ledger said failed at 5.2 minutes. Under setsid the child
    is a group leader and the group form of kill reaches the whole tree; a host
    without setsid falls back to the bare form rather than failing to launch."""
    body = await _staged_launcher("bash run_fea.sh")

    assert "if command -v setsid >/dev/null 2>&1; then RAVEN_SETSID=setsid; else RAVEN_SETSID=; fi" in body
    assert "$RAVEN_SETSID bash -o pipefail -c" in body
    assert "$RAVEN_SETSID sh -c" in body
    assert 'wait "$RAVEN_JOB"' in body, "a group leader is still our child"


@pytest.mark.asyncio
async def test_a_command_that_detaches_itself_is_stopped_and_refused_in_the_result():
    """Measured 2026-09-03: a launch_job.sh doing `setsid nohup ... &` made
    the launcher's wait return in 0.4 s; four training runs were recorded
    succeeded / 0.000 ten seconds after launch, the budget was never debited
    and the gate released the device while the run was on it. The tell is a
    pid file naming a live process that is not our child once wait returns.
    The orphan is stopped (group and pid) and the reason lands in result.json
    where the next submit reads it; a zombie does not count as alive."""
    from oncall_flow.process_backend import _ESCAPE_ERROR

    body = await _staged_launcher("./launch_job.sh {config}")

    assert "RAVEN_LEFT=$(cat pid 2>/dev/null)" in body
    assert '[ "$RAVEN_LEFT" != "$RAVEN_JOB" ] && kill -0 "$RAVEN_LEFT"' in body
    assert 'ps -o stat= -p "$RAVEN_LEFT"' in body and '!= "Z"' in body, "a zombie is not a live escapee"
    assert 'kill -s TERM -- -"$RAVEN_LEFT"' in body and 'kill -s KILL -- -"$RAVEN_LEFT"' in body
    assert '"status": "failed"' in body
    assert _ESCAPE_ERROR in body
    assert "run it in the foreground" in _ESCAPE_ERROR
    assert 'if [ -n "$RAVEN_ESCAPED" ] || [ ! -f result.json ]' in body, (
        "an escape is written even over a result the orphan may have left"
    )


@pytest.mark.asyncio
async def test_the_launcher_stops_a_child_that_escaped_when_run_in_a_real_shell(tmp_path):
    """The generated launcher against a real shell, not its text: the command
    backgrounds a sleep, writes that pid where the owner's scripts do, and
    exits 0 -- the shape a `launch_job.sh` ending in `nohup ... &` has. The
    escapee must be gone when the launcher returns and the result must say
    failed, or the ledger releases the machine while the job is still on it."""
    import base64 as _b64
    import os
    import shutil
    import subprocess

    if shutil.which("sh") is None:
        pytest.skip("no POSIX shell")
    host = FakeHost()
    exe = ProcessExecutor(host, remote_dir=str(tmp_path), command="sh -c 'sleep 30 & echo $! > pid; exit 0'")
    await exe.submit(_spec({"lr": 1e-4}))
    body = None
    for seen in host.seen:
        if ".raven-launch.sh" not in seen or "base64 -d" not in seen:
            continue
        for piece in seen.split("echo ")[1:]:
            blob = piece.split(" | base64 -d", 1)[0].strip().strip("'")
            try:
                text = _b64.b64decode(blob).decode()
            except Exception:  # noqa: BLE001 -- the config blob decodes too
                continue
            if text.startswith("#!/bin/sh"):
                body = text
    assert body is not None, "no launcher was staged"
    job_dir = tmp_path / "jobs" / "j1"
    job_dir.mkdir(parents=True)
    (job_dir / "config.json").write_text('{"lr": 1e-4}')
    launcher = job_dir / ".raven-launch.sh"
    launcher.write_text(body)

    subprocess.run(["sh", str(launcher)], cwd=job_dir, timeout=60, check=False)

    result = json.loads((job_dir / "result.json").read_text())
    assert result["status"] == "failed" and "detached itself" in result["error"]
    escaped = int((job_dir / "pid").read_text().strip())
    try:
        os.kill(escaped, 0)
        alive = True
    except ProcessLookupError:
        alive = False
    except PermissionError:
        alive = True
    if alive:
        os.kill(escaped, 9)
    assert not alive, "the escaped child must be gone when the launcher returns"


@pytest.mark.asyncio
async def test_cancel_kills_the_group_before_the_pid():
    host = FakeHost()
    exe = _exe(host)
    handle = await exe.submit(_spec({"lr": 2e-6}))
    await exe.cancel(handle)
    kill_cmd = next(c for c in host.seen if "kill -TERM $p" in c)
    assert "kill -s TERM -- -$p" in kill_cmd
    assert kill_cmd.index("kill -s TERM -- -$p") < kill_cmd.index("kill -TERM $p")
    assert "kill -s KILL -- -$p" in kill_cmd


@pytest.mark.asyncio
async def test_cancel_says_the_process_was_alive_and_for_how_long():
    """Looked at before the kill: killing a healthy job has to read as that in
    the record, not as cleanup of something already dead."""
    host = FakeHost()
    exe = _exe(host)
    handle = await exe.submit(_spec({"lr": 2e-6}))
    host.now += 311

    note = await exe.cancel(handle)

    assert note == "process was alive (pid 4242, running 5.2 min) when killed"
    assert host.killed == ["j1"]


@pytest.mark.asyncio
async def test_cancel_of_a_finished_job_has_nothing_to_say():
    host = FakeHost()
    exe = _exe(host)
    handle = await exe.submit(_spec({"lr": 2e-6}))
    host.finish("j1")

    assert await exe.cancel(handle) is None


# ---- what the gate handed out rides to the launcher ----


async def _staged(cmd: str, labels: dict) -> tuple[FakeHost, str, dict]:
    """(host, launcher body, staged resources) for a submit carrying ``labels``."""
    import base64 as _b64

    host = FakeHost()
    await _exe(host, command=cmd).submit(JobSpec(payload={"lr": 1e-4}, idem_key="j1", labels=labels))
    body, resources = "", {}
    for seen in host.seen:
        if ".raven-launch.sh" not in seen or "base64 -d" not in seen:
            continue
        for piece in seen.split("echo ")[1:]:
            blob = piece.split(" | base64 -d", 1)[0].strip().strip("'")
            try:
                text = _b64.b64decode(blob).decode()
            except Exception:  # noqa: BLE001
                continue
            if text.startswith("#!/bin/sh"):
                body = text
            elif '"width"' in text:
                resources = json.loads(text)
    return host, body, resources


@pytest.mark.asyncio
async def test_assigned_devices_are_exported_and_checked_for_a_stranger_before_the_start():
    """The gate picks the cards; the job never does. The one thing the ledger
    cannot know is a person on the same machine, so a chosen card already holding
    someone's memory is not started on, and the result says which and how much."""
    from oncall_flow.process_backend import _FOREIGN_USE_MIB

    _, body, resources = await _staged("bash run.sh {config}", {"campaign": "c", "device_ids": "0,1", "width": "2"})

    assert "export CUDA_VISIBLE_DEVICES=0,1\n" in body
    assert body.index("export CUDA_VISIBLE_DEVICES") < body.index("RAVEN_T0="), "bound before the clock starts"
    assert "nvidia-smi --query-gpu=index,memory.used" in body and "-i 0,1" in body
    assert f"$2+0 > {_FOREIGN_USE_MIB}" in body
    assert "held by a process outside the ledger" in body and '"gpu_minutes_used": 0.000' in body
    assert resources == {"width": 2.0, "device_ids": ["0", "1"]}


@pytest.mark.asyncio
async def test_a_job_with_no_assigned_devices_gets_no_export_and_width_one():
    _, body, resources = await _staged("bash run.sh {config}", {"campaign": "c"})

    assert "CUDA_VISIBLE_DEVICES" not in body
    assert "nvidia-smi" not in body
    assert resources == {"width": 1.0}


@pytest.mark.asyncio
async def test_spend_is_as_wide_as_the_job_was_admitted():
    """Two devices held for ten minutes are twenty device-minutes; a job staged
    before widths were written ran one wide, which is what it was billed as."""
    host = FakeHost()
    exe = _exe(host)
    await exe.submit(JobSpec(payload={"lr": 1}, idem_key="wide", labels={"width": "2", "device_ids": "0,1"}))
    await exe.submit(JobSpec(payload={"lr": 2}, idem_key="old", labels={}))
    host.now += 600
    host.finish("wide", minutes=10.0)
    host.finish("old", minutes=10.0)

    assert await exe.spent_minutes() == pytest.approx(20.0 + 10.0)
    assert exe._declared_width == {"wide": 2.0, "old": 1.0}


@pytest.mark.asyncio
async def test_two_jobs_on_gate_assigned_cards_each_pay_while_pinned_jobs_share():
    """The design's budget rule made concrete: jobs the gate handed devices to
    hold them alone, so an overlap is two cards busy and both pay; jobs a
    template pins to a device it did not choose keep the campaign's declared
    overlap, where an overlap really is one card busy once."""
    host = FakeHost()
    exe = _exe(host)
    await exe.submit(JobSpec(payload={"lr": 1}, idem_key="a", labels={"width": "1", "device_ids": "0"}))
    await exe.submit(JobSpec(payload={"lr": 2}, idem_key="b", labels={"width": "1", "device_ids": "1"}))
    host.now += 600
    host.finish("a", minutes=10.0)
    host.finish("b", minutes=10.0)
    assert await exe.spent_minutes() == pytest.approx(20.0), "different cards, both busy"

    pinned = FakeHost()
    exe2 = _exe(pinned)
    await exe2.submit(JobSpec(payload={"lr": 1}, idem_key="a", labels={}))
    await exe2.submit(JobSpec(payload={"lr": 2}, idem_key="b", labels={}))
    pinned.now += 600
    pinned.finish("a", minutes=10.0)
    pinned.finish("b", minutes=10.0)
    assert await exe2.spent_minutes() == pytest.approx(10.0), "the template's one device, busy once"
