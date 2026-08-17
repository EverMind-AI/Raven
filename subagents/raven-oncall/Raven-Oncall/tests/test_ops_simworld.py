"""Tests for the time-compressed fake world: SimClock + ScriptedJobBackend.

The point of this layer is measurement, so the tests assert on the measurement
surface: a job's real finish instant is known, so detection latency is exact; a
no-op script never finishes, so the loop must keep waiting cheaply without
concluding.
"""

from __future__ import annotations

import json

from raven.ops import JobScript, JobSpec, JobStatus, ScriptedJobBackend, SimClock


def test_clock_advances_only_when_told_by_default() -> None:
    ticks = iter([100.0, 200.0, 300.0])
    clock = SimClock(monotonic=lambda: next(ticks))

    assert clock.now_ms() == 0  # real time moved 100s; campaign time did not
    clock.advance(hours=3)
    assert clock.now_ms() == 3 * 3_600_000
    assert clock.real_seconds_for(campaign_ms=86_400_000) == 0.0  # no waiting at all


def test_speed_factor_scales_real_time_into_campaign_time() -> None:
    now = [0.0]
    clock = SimClock(speed_factor=3600.0, monotonic=lambda: now[0])  # 1 real s = 1 campaign h

    now[0] = 2.0
    assert clock.now_ms() == 2 * 3_600_000
    clock.advance(days=1)  # explicit jumps still compose with the scaled flow
    assert clock.now_ms() == 2 * 3_600_000 + 86_400_000
    assert clock.real_seconds_for(campaign_ms=3_600_000) == 1.0  # a campaign hour costs 1 real second


async def test_job_finishes_on_the_clock_not_on_polls() -> None:
    clock = SimClock()
    backend = ScriptedJobBackend(clock, scripts={"t1": JobScript(finish_after_ms=3_600_000, metrics={"score": 0.7})})
    handle = await backend.submit(JobSpec({}, idem_key="t1"))

    assert await backend.poll(handle) is JobStatus.PENDING
    for _ in range(20):  # polling hard does not make it finish
        assert not (await backend.poll(handle)).is_terminal

    clock.advance(hours=1)
    assert await backend.poll(handle) is JobStatus.SUCCEEDED
    assert (await backend.fetch_result(handle)).metrics == {"score": 0.7}


async def test_detection_latency_is_ground_truth(tmp_path) -> None:
    clock = SimClock()
    backend = ScriptedJobBackend(clock, scripts={"t1": JobScript(finish_after_ms=60_000)})
    handle = await backend.submit(JobSpec({}, idem_key="t1"))

    clock.advance(minutes=5)  # it finished at t=1min; nobody looked until t=5min
    assert await backend.poll(handle) is JobStatus.SUCCEEDED

    assert backend.finished_at_ms("t1") == 60_000
    assert backend.detection_latency_ms("t1") == 4 * 60_000  # exactly the 4 minutes wasted
    clock.advance(minutes=10)
    assert backend.detection_latency_ms("t1") == 4 * 60_000  # first observation, not the latest


async def test_no_op_script_never_finishes_and_reports_no_latency() -> None:
    clock = SimClock()
    backend = ScriptedJobBackend(clock, scripts={"noop": JobScript(finish_after_ms=None)})
    handle = await backend.submit(JobSpec({}, idem_key="noop"))

    clock.advance(days=3)
    assert not (await backend.poll(handle)).is_terminal  # the awaited event never happens
    assert backend.detection_latency_ms("noop") is None
    assert backend.finished_at_ms("noop") is None


async def test_progress_is_revealed_on_the_clock() -> None:
    clock = SimClock()
    script = JobScript(
        finish_after_ms=600_000,
        progress=[(0, {"step": 1, "loss": 2.0}), (300_000, {"step": 2, "loss": float("nan")})],
    )
    backend = ScriptedJobBackend(clock, scripts={"t1": script})
    handle = await backend.submit(JobSpec({}, idem_key="t1"))

    assert await backend.fetch_progress(handle) == [{"step": 1, "loss": 2.0}]  # later sample not due yet
    clock.advance(minutes=5)
    samples = await backend.fetch_progress(handle)
    assert len(samples) == 2 and samples[-1]["step"] == 2  # divergence now visible


async def test_cancel_marks_terminal_at_the_cancel_instant() -> None:
    clock = SimClock()
    backend = ScriptedJobBackend(clock, scripts={"t1": JobScript(finish_after_ms=86_400_000)})
    handle = await backend.submit(JobSpec({}, idem_key="t1"))

    clock.advance(hours=2)
    await backend.cancel(handle)  # killed early instead of paying the remaining 22h

    assert await backend.poll(handle) is JobStatus.FAILED
    assert backend.finished_at_ms("t1") == 2 * 3_600_000
    assert (await backend.fetch_result(handle)).error == "cancelled"


async def test_submit_is_idempotent_on_idem_key() -> None:
    clock = SimClock()
    backend = ScriptedJobBackend(clock, default_script=JobScript(finish_after_ms=1000))

    first = await backend.submit(JobSpec({"a": 1}, idem_key="same"))
    second = await backend.submit(JobSpec({"a": 2}, idem_key="same"))

    assert first == second  # crash-resume must never double-spend compute


# ---- the tool layer against the scripted world (compressed-time full loop) ----


class _FakeCron:
    def __init__(self) -> None:
        self.jobs: list[dict] = []

    def add_job(self, **kw):
        self.jobs.append(kw)
        return type("_J", (), {"id": str(len(self.jobs) - 1)})()

    def list_jobs(self):
        from types import SimpleNamespace

        return [SimpleNamespace(id=str(i), name=j["name"]) for i, j in enumerate(self.jobs)]

    def remove_job(self, job_id):
        del self.jobs[int(job_id)]
        return True


def _install(tmp_path, name, scripts):
    """A campaign whose meta points at a scripted world instead of a docker host."""
    import json

    from raven.ops import clear_worlds, install_world

    clear_worlds()
    clock = SimClock()
    world = ScriptedJobBackend(clock, scripts=scripts)
    install_world(name, world)
    (tmp_path / "meta.json").write_text(
        json.dumps({"backend": "scripted", "world": name, "host": "sim"}), encoding="utf-8"
    )
    return clock, world


async def test_full_loop_through_the_tools_in_compressed_time(tmp_path) -> None:
    """The whole on-call loop -- submit, wake, reconcile, report -- exercised through
    the real tools against a job that takes two campaign hours, in zero wall time."""
    from raven.agent.tools.ops import OpsSubmitTool, OpsTuneStatusTool

    clock, _ = _install(
        tmp_path,
        "w1",
        {
            "b0p6_k11p6": JobScript(
                finish_after_ms=2 * 3_600_000, metrics={"ndcg": 0.31}, output={"config": {"k1": 1.6, "b": 0.6}}
            ),
        },
    )
    ledger = str(tmp_path / "ledger.json")
    submit = OpsSubmitTool(cron_service=_FakeCron())
    submit.set_context("cli", "direct")

    out = await submit.execute(
        host="sim",
        configs=[{"k1": 1.6, "b": 0.6}],
        objective="max ndcg",
        ledger=ledger,
        eta_seconds=3600,
        round=0,
        campaign="w1",
    )
    assert "Submitted 1 job(s)" in out

    status = OpsTuneStatusTool()
    early = await status.execute(ledger=ledger, metric="ndcg")
    assert "in progress" in early  # the job needs two hours; nothing to report yet

    clock.advance(hours=2)
    late = await status.execute(ledger=ledger, metric="ndcg")

    assert "done" in late and "ndcg=0.31" in late  # reconciled through the real tool path


async def test_kill_through_the_tools_stops_a_diverged_trial(tmp_path) -> None:
    """Process health end to end: status surfaces the diverged sample, ops_kill
    ends the trial, and the ledger records why."""
    import json

    from raven.agent.tools.ops import OpsKillTool, OpsSubmitTool, OpsTuneStatusTool

    clock, world = _install(
        tmp_path,
        "w2",
        {
            "k11p0": JobScript(
                finish_after_ms=86_400_000,  # a day-long run...
                progress=[(0, {"step": 1, "loss": 2.0}), (3_600_000, {"step": 2, "loss": float("nan")})],
            ),
        },
    )
    ledger = str(tmp_path / "ledger.json")
    submit = OpsSubmitTool(cron_service=_FakeCron())
    submit.set_context("cli", "direct")
    await submit.execute(
        host="sim", configs=[{"k1": 1.0}], objective="o", ledger=ledger, eta_seconds=3600, round=0, campaign="w2"
    )

    clock.advance(hours=1)  # ... that diverges after one hour
    report = await OpsTuneStatusTool().execute(ledger=ledger, metric="ndcg")
    assert "Running-trial progress" in report and "NaN" in report.replace("nan", "NaN")

    killed = await OpsKillTool().execute(
        campaign="w2",
        trials=["k11p0"],
        reason="loss NaN at step 2",
        ledger=ledger,
        basis="the sample just read has loss NaN",
    )

    assert "Killed 1 trial(s)" in killed
    assert world.finished_at_ms("k11p0") == 3_600_000  # stopped at the hour, not after a day
    record = json.loads((tmp_path / "ledger.json").read_text())["records"]["k11p0"]
    assert record["status"] == "failed" and "loss NaN" in record["result"]["error"]


async def test_conclusion_stands_down_through_the_tools(tmp_path) -> None:
    """The chart is authoritative in the tool path too: after ops_finish, a later
    wake's submit refuses and schedules nothing."""
    from raven.agent.tools.ops import OpsSubmitTool
    from raven.agent.tools.ops_escalation import OpsFinishTool

    _install(tmp_path, "w3", {"k11p0": JobScript(finish_after_ms=1000, metrics={"ndcg": 0.3})})
    ledger = str(tmp_path / "ledger.json")
    cron = _FakeCron()
    submit = OpsSubmitTool(cron_service=cron)
    submit.set_context("cli", "direct")
    await submit.execute(
        host="sim", configs=[{"k1": 1.0}], objective="o", ledger=ledger, eta_seconds=60, round=0, campaign="w3"
    )

    await OpsFinishTool(cron_service=cron).execute(
        campaign="w3",
        subject="w3 sweep",
        outcome="done",
        dedupe_key="k1",
        observed={"ndcg": 0.3},
        condition_type="absolute",
        ledger=ledger,
    )
    late = await submit.execute(
        host="sim", configs=[{"k1": 2.0}], objective="o", ledger=ledger, eta_seconds=60, round=1, campaign="w3"
    )

    assert "CONCLUDED" in late
    assert cron.jobs == []


async def test_campaign_survives_a_restart_through_the_tools(tmp_path) -> None:
    """Crash-resume at the tool layer: a fresh tool instance (new process, same
    disk) reconciles the campaign it never submitted, without re-running it."""
    from raven.agent.tools.ops import OpsSubmitTool, OpsTuneStatusTool

    clock, world = _install(
        tmp_path,
        "w4",
        {
            "k11p4": JobScript(finish_after_ms=600_000, metrics={"ndcg": 0.29}, output={"config": {"k1": 1.4}}),
        },
    )
    ledger = str(tmp_path / "ledger.json")
    submit = OpsSubmitTool(cron_service=_FakeCron())
    submit.set_context("cli", "direct")
    await submit.execute(
        host="sim", configs=[{"k1": 1.4}], objective="o", ledger=ledger, eta_seconds=600, round=0, campaign="w4"
    )
    submitted_jobs = world.poll_count()  # the world remembers what was actually started

    clock.advance(minutes=20)
    del submit  # the process that submitted is gone

    revived = await OpsTuneStatusTool().execute(ledger=ledger, metric="ndcg")

    assert "ndcg=0.29" in revived  # picked the result up from disk state alone
    assert world.poll_count() >= submitted_jobs  # polled, never re-submitted
    assert len(json.loads((tmp_path / "ledger.json").read_text())["records"]) == 1
