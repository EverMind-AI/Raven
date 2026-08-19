"""Tests for OpsSubmitTool: agent-in-the-loop submit + self-scheduled wake."""

from __future__ import annotations

import json
import shlex

import pytest
from pathlib import Path

import raven.ops as ops_pkg
from raven.agent.tools.ops import OpsCheckLaterTool, OpsNoteTool, OpsSubmitTool


class _FakeRunner:
    """Minimal docker-over-ssh: `ps` says not-present, `run` succeeds."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def __call__(self, cmd: str) -> tuple[int, str]:
        self.calls.append(cmd)
        if cmd.startswith("docker ps -aq -f name="):
            return 0, ""
        tokens = shlex.split(cmd)
        if "docker" in tokens and "run" in tokens:
            return 0, "cid-x"
        return 0, ""


class _Job:
    id = "job-1"


class _FakeCron:
    def __init__(self) -> None:
        self.jobs: list[dict] = []

    def add_job(self, **kw):
        self.jobs.append(kw)
        return _Job()

    def list_jobs(self):
        from types import SimpleNamespace

        return [SimpleNamespace(id=str(i), name=j["name"]) for i, j in enumerate(self.jobs)]

    def remove_job(self, job_id):
        del self.jobs[int(job_id)]
        return True


def _patch_remote(monkeypatch) -> _FakeRunner:
    runner = _FakeRunner()
    monkeypatch.setattr(ops_pkg, "make_ssh_runner", lambda *a, **k: runner)
    monkeypatch.setattr(ops_pkg, "make_ssh_sync", lambda *a, **k: (lambda l, r: (0, "")))
    monkeypatch.setattr(ops_pkg, "prepare_remote", lambda *a, **k: None)
    return runner


def _fresh_observation(campaign_dir, *, readings=(0.29,)) -> None:
    """Record a probe with readings, as ops_tune_status would.

    Every decision tool now needs a basis that cites a reading from an
    observation newer than the previous decision -- so a test that drives a
    decision has to have observed something first, exactly as a real turn does.
    """
    from raven.ops.state_claims import StateFacts, write_facts

    campaign_dir.mkdir(parents=True, exist_ok=True)
    write_facts(campaign_dir, StateFacts(metric_readings={"ndcg": tuple(readings)}, probe_seq=1))


async def test_submit_records_ledger_and_schedules_wake(monkeypatch, tmp_path: Path) -> None:
    _patch_remote(monkeypatch)
    cron = _FakeCron()
    tool = OpsSubmitTool(cron_service=cron)
    tool.set_context("cli", "direct")
    ledger = tmp_path / "l.json"

    out = await tool.execute(
        host="1.2.3.4", configs=[{"k1": 1.5, "b": 0.75}], objective="max ndcg",
        ledger=str(ledger), eta_seconds=120, round=0, metric="ndcg", campaign="bm25",
    )

    # ledger recorded the submitted trial under the campaign
    data = json.loads(ledger.read_text())
    assert "b0p75_k11p5" in data["records"]  # config_key sorts keys: b before k1
    assert data["records"]["b0p75_k11p5"]["campaign"] == "bm25"

    # a one-shot wake was scheduled to decide the next round
    assert len(cron.jobs) == 1
    job = cron.jobs[0]
    assert job["schedule"].kind == "at"
    assert "ops_submit" in job["message"] and "round=1" in job["message"]
    assert "Submitted 1 job(s)" in out and "Scheduled a wake" in out


async def test_submit_batch_runs_all_configs(monkeypatch, tmp_path: Path) -> None:
    _patch_remote(monkeypatch)
    tool = OpsSubmitTool(cron_service=_FakeCron())
    tool.set_context("cli", "direct")
    ledger = tmp_path / "l.json"

    await tool.execute(
        host="h", configs=[{"k1": 1.0, "b": 0.5}, {"k1": 2.0, "b": 0.9}],
        objective="o", ledger=str(ledger), eta_seconds=60, round=0,
    )

    data = json.loads(ledger.read_text())
    assert len(data["records"]) == 2  # batch submitted as two trials


async def test_submit_refuses_past_max_rounds(monkeypatch, tmp_path: Path) -> None:
    runner = _patch_remote(monkeypatch)
    cron = _FakeCron()
    tool = OpsSubmitTool(cron_service=cron)
    tool.set_context("cli", "direct")

    out = await tool.execute(
        host="h", configs=[{"k1": 1.5, "b": 0.75}], objective="o",
        ledger=str(tmp_path / "l.json"), eta_seconds=60, round=8, max_rounds=8,
    )

    assert "max_rounds" in out
    assert not runner.calls  # nothing submitted
    assert not cron.jobs  # no wake scheduled


async def test_relative_ledger_is_anchored_to_ops_home(monkeypatch, tmp_path: Path) -> None:
    _patch_remote(monkeypatch)
    monkeypatch.setattr("raven.config.paths.get_ops_home", lambda: tmp_path / "ops")
    tool = OpsSubmitTool(cron_service=_FakeCron())
    tool.set_context("cli", "direct")

    out = await tool.execute(
        host="h", configs=[{"k1": 1.5, "b": 0.75}], objective="o",
        ledger="benchmarks/ops_bm25/ledger.json", eta_seconds=60, round=0, campaign="bm25",
    )

    # relative path was replaced with a stable absolute one under the ops home
    anchored = tmp_path / "ops" / "bm25" / "ledger.json"
    assert anchored.exists()
    assert str(anchored) in out  # the absolute path is echoed back for later rounds


async def test_check_later_reschedules_without_submitting(tmp_path: Path) -> None:
    cron = _FakeCron()
    tool = OpsCheckLaterTool(cron_service=cron)
    tool.set_context("cli", "direct")

    _fresh_observation(tmp_path)
    out = await tool.execute(campaign="bm25", ledger=str(tmp_path / "l.json"), eta_seconds=300,
                             metric="ndcg", basis="ndcg 0.29 on the latest sample, still climbing")

    assert len(cron.jobs) == 1  # a re-check wake was scheduled
    assert cron.jobs[0]["schedule"].kind == "at"
    assert "ops_tune_status" in cron.jobs[0]["message"]
    assert "Scheduled a wake" in out


async def test_submit_without_scheduler_still_submits(monkeypatch, tmp_path: Path) -> None:
    _patch_remote(monkeypatch)
    tool = OpsSubmitTool(cron_service=None)  # no gateway scheduler
    ledger = tmp_path / "l.json"

    out = await tool.execute(
        host="h", configs=[{"k1": 1.5, "b": 0.75}], objective="o",
        ledger=str(ledger), eta_seconds=60, round=0,
    )

    assert "Submitted 1 job(s)" in out
    assert "No scheduler" in out  # degrades to manual check, does not crash
    assert (tmp_path / "l.json").exists()


async def test_later_round_reuses_connection_from_meta(monkeypatch, tmp_path: Path) -> None:
    # Round 1 often omits port/key; the campaign's meta.json (written round 0)
    # must supply them so it does not fall back to SSH port 22.
    captured = {}

    def make_runner(host, port, key, **kw):
        captured["port"] = port
        return _FakeRunner()

    monkeypatch.setattr(ops_pkg, "make_ssh_runner", make_runner)
    monkeypatch.setattr(ops_pkg, "make_ssh_sync", lambda *a, **k: (lambda l, r: (0, "")))
    monkeypatch.setattr(ops_pkg, "prepare_remote", lambda *a, **k: None)

    ledger = tmp_path / "ops" / "bm25" / "ledger.json"
    ledger.parent.mkdir(parents=True)
    (ledger.parent / "meta.json").write_text(
        json.dumps({"host": "h", "port": 64106, "key": "~/.ssh/id_rsa", "remote_dir": "/root/raven-ops", "image": "img"})
    )
    tool = OpsSubmitTool(cron_service=_FakeCron())
    tool.set_context("cli", "direct")

    _fresh_observation(ledger.parent)
    # agent omits port on round 1 (defaults to 22) -- meta must override it
    await tool.execute(host="h", configs=[{"k1": 1.4, "b": 0.5}], objective="o",
                       ledger=str(ledger), eta_seconds=60, round=1, campaign="bm25",
                       basis="round 0 reached ndcg 0.29; trying a different k1")

    assert captured["port"] == 64106  # reused from meta, not the dropped default 22


async def test_host_with_embedded_port_is_split(monkeypatch, tmp_path: Path) -> None:
    # The agent sometimes passes "host:port" as host (prompt says "host (port N)").
    captured = {}

    def make_runner(host, port, key, **kw):
        captured["host"], captured["port"] = host, port
        return _FakeRunner()

    monkeypatch.setattr(ops_pkg, "make_ssh_runner", make_runner)
    monkeypatch.setattr(ops_pkg, "make_ssh_sync", lambda *a, **k: (lambda l, r: (0, "")))
    monkeypatch.setattr(ops_pkg, "prepare_remote", lambda *a, **k: None)

    tool = OpsSubmitTool(cron_service=_FakeCron())
    tool.set_context("cli", "direct")
    await tool.execute(
        host="14.103.100.27:64106", configs=[{"k1": 1.5, "b": 0.75}], objective="o",
        ledger=str(tmp_path / "l.json"), eta_seconds=60, round=0,
    )

    assert captured["host"] == "14.103.100.27"  # port split off the host string
    assert captured["port"] == 64106


async def test_note_lands_on_the_chart_and_status_surfaces_it(monkeypatch, tmp_path: Path) -> None:
    """A chat-lane instruction must reach wake turns via disk: ops_note writes it,
    ops_tune_status shows it."""
    from raven.agent.tools.ops import OpsTuneStatusTool

    ledger = tmp_path / "ledger.json"
    ledger.write_text(json.dumps({"version": 1, "records": {
        "t1": {"idem_key": "t1", "status": "succeeded", "campaign": "bm25", "handle": None,
               "result": {"status": "succeeded", "metrics": {"ndcg": 0.3}, "output": {"config": {"k1": 1}}, "error": None},
               "attempts": 1, "escalated": False}}}), encoding="utf-8")

    await OpsNoteTool().execute(campaign="bm25", note="explore larger k1 next round", ledger=str(ledger))
    out = await OpsTuneStatusTool().execute(ledger=str(ledger), metric="ndcg")

    assert "explore larger k1 next round" in out  # the chart reached the (simulated) wake turn


async def test_finishing_concludes_removes_wakes_and_blocks_submit(monkeypatch, tmp_path: Path) -> None:
    """Conclusion is durable: finishing marks the chart, strips pending wakes,
    and any later submit stands down."""
    from raven.agent.tools.ops import OpsTuneStatusTool

    _patch_remote(monkeypatch)
    cron = _FakeCron()
    ledger = tmp_path / "ledger.json"

    submit = OpsSubmitTool(cron_service=cron)
    submit.set_context("cli", "direct")
    await submit.execute(host="h", configs=[{"k1": 1.5, "b": 0.75}], objective="o",
                         ledger=str(ledger), eta_seconds=60, round=0, campaign="bm25")
    assert len(cron.jobs) == 1  # a pending wake exists

    from raven.agent.tools.ops_escalation import OpsFinishTool

    finish = OpsFinishTool(cron_service=cron)
    out = await finish.execute(campaign="bm25", subject="bm25 sweep", outcome="done",
                               dedupe_key="k1", observed={"ndcg": 0.36},
                               condition_type="absolute", ledger=str(ledger))
    assert "1 pending wake" in out
    assert cron.jobs == []  # wake stripped
    assert (tmp_path / "concluded.json").exists()

    # a late turn (stale wake / heartbeat) trying to submit more rounds stands down
    late = await submit.execute(host="h", configs=[{"k1": 2.0, "b": 0.5}], objective="o",
                                ledger=str(ledger), eta_seconds=60, round=1, campaign="bm25")
    assert "REFUSED" in late and "bm25" in late
    assert "ops_tune_status" in late, "standing down means being told where the answer is"
    assert len(cron.jobs) == 0  # and scheduled nothing

    # status leads with the conclusion so any reader stands down too
    status = await OpsTuneStatusTool().execute(ledger=str(ledger), metric="ndcg")
    assert "CONCLUDED" in status and "bm25 sweep" in status


async def test_status_shows_running_trial_progress(monkeypatch, tmp_path: Path) -> None:
    """The woken agent must see in-flight health (loss/residual tail) to judge
    early-stop -- the saving-compute lever."""
    from raven.agent.tools.ops import OpsTuneStatusTool

    ledger = tmp_path / "ledger.json"
    ledger.write_text(json.dumps({"version": 1, "records": {
        "t1": {"idem_key": "t1", "status": "running", "campaign": "bm25",
               "handle": {"backend": "docker", "job_id": "ops-t1"},
               "result": None, "attempts": 1, "escalated": False}}}), encoding="utf-8")
    (tmp_path / "meta.json").write_text(json.dumps(
        {"host": "h", "port": 22, "key": "~/.ssh/id_rsa", "remote_dir": "/root/raven-ops", "image": "img"}))

    def runner(cmd: str):
        if cmd.startswith("docker inspect"):
            return 0, "running 0"
        if cmd.startswith("tail ") and "progress.jsonl" in cmd:
            return 0, '{"step": 7, "loss": 9.99}'
        return 0, ""

    monkeypatch.setattr(ops_pkg, "make_ssh_runner", lambda *a, **k: runner)

    out = await OpsTuneStatusTool().execute(ledger=str(ledger), metric="ndcg")

    assert "Running-trial progress" in out and '"loss": 9.99' in out


async def test_kill_cancels_running_trial_and_records_failed(monkeypatch, tmp_path: Path) -> None:
    from raven.agent.tools.ops import OpsKillTool
    from raven.ops import read_events

    ledger = tmp_path / "ledger.json"
    ledger.write_text(json.dumps({"version": 1, "records": {
        "t1": {"idem_key": "t1", "status": "running", "campaign": "bm25",
               "handle": {"backend": "docker", "job_id": "ops-t1"},
               "result": None, "attempts": 1, "escalated": False},
        "t2": {"idem_key": "t2", "status": "succeeded", "campaign": "bm25", "handle": None,
               "result": {"status": "succeeded", "metrics": {"ndcg": 0.3}, "output": {}, "error": None},
               "attempts": 1, "escalated": False}}}), encoding="utf-8")
    (tmp_path / "meta.json").write_text(json.dumps(
        {"host": "h", "port": 22, "key": "~/.ssh/id_rsa", "remote_dir": "/root/raven-ops", "image": "img"}))

    calls: list[str] = []

    def runner(cmd: str):
        calls.append(cmd)
        return 0, ""

    monkeypatch.setattr(ops_pkg, "make_ssh_runner", lambda *a, **k: runner)

    _fresh_observation(tmp_path)
    out = await OpsKillTool().execute(campaign="bm25", trials=["t1", "t2", "nope"],
                                      reason="loss NaN", ledger=str(ledger),
                                      basis="latest sample reads ndcg 0.29, well under where it should be")

    assert "Killed 1 trial(s): t1" in out and "Skipped" in out  # terminal + unknown skipped
    assert any(c.startswith("docker rm -f ops-t1") for c in calls)  # container really killed
    data = json.loads(ledger.read_text())
    assert data["records"]["t1"]["status"] == "failed"
    assert "killed early: loss NaN" in data["records"]["t1"]["result"]["error"]
    assert any(e["kind"] == "kill" for e in read_events(tmp_path))


# ---- a refused submit must not leave a trial pending forever ----


class _RefusingBackend:
    """A backend that refuses, the way the process backend does when the
    experiment's compute budget is spent."""

    def __init__(self, message: str = "compute budget exhausted") -> None:
        self.message = message

    async def submit(self, spec):
        from raven.ops.backend import JobBackendError

        raise JobBackendError(self.message)


def _install_refusing_backend(monkeypatch, message="compute budget exhausted"):
    from raven.ops.backends import register_backend

    register_backend("refusing", lambda meta: _RefusingBackend(message))


@pytest.mark.asyncio
async def test_a_refused_submit_records_the_trial_failed_not_pending(tmp_path, monkeypatch):
    from raven.ops import Ledger
    from raven.agent.tools.ops import OpsSubmitTool

    _install_refusing_backend(monkeypatch)
    ledger = tmp_path / "ledger.json"
    (tmp_path / "meta.json").write_text(
        json.dumps({"backend": "refusing", "host": "h", "port": 1, "key": "k"}), encoding="utf-8"
    )

    out = await OpsSubmitTool().execute(
        host="h", configs=[{"lr": 1e-5}], objective="o", ledger=str(ledger), eta_seconds=60
    )

    led = Ledger(str(ledger))
    assert led.pending() == [], (
        "a record with no handle is skipped by reconciliation, so leaving it pending "
        "would read as in-progress forever and keep the agent waiting"
    )
    assert all(r.status.value == "failed" for r in led.all())
    assert "refused" in out and "budget exhausted" in out


@pytest.mark.asyncio
async def test_a_fully_refused_round_schedules_no_wake(tmp_path, monkeypatch):
    from raven.agent.tools.ops import OpsSubmitTool

    _install_refusing_backend(monkeypatch)
    ledger = tmp_path / "ledger.json"
    (tmp_path / "meta.json").write_text(
        json.dumps({"backend": "refusing", "host": "h", "port": 1, "key": "k"}), encoding="utf-8"
    )

    class _Cron:
        def __init__(self):
            self.added = []

        def add_job(self, **kw):
            self.added.append(kw)
            raise AssertionError("must not schedule a wake when nothing is running")

    cron = _Cron()
    tool = OpsSubmitTool(cron_service=cron)
    out = await tool.execute(
        host="h", configs=[{"lr": 1e-5}], objective="o", ledger=str(ledger), eta_seconds=60
    )
    assert cron.added == []
    assert "Nothing is running and nothing is scheduled" in out


# ---- status must not rank outcomes for the agent ----


def _terminal_ledger(tmp_path, rows):
    """A ledger with finished trials, newest last, as submitted."""
    from raven.ops import Ledger
    from raven.ops.backend import JobResult, JobStatus

    led = Ledger(str(tmp_path / "ledger.json"))
    for key, cfg, ndcg in rows:
        led.record(key, campaign="c")
        led.set_result(
            key,
            JobResult(JobStatus.SUCCEEDED, metrics={"ndcg": ndcg, "gpu_minutes_used": 30.0},
                      output={"config": cfg}),
        )
    return led


@pytest.mark.asyncio
async def test_status_lists_trials_without_ranking_them(tmp_path):
    from raven.agent.tools.ops import OpsTuneStatusTool

    _terminal_ledger(tmp_path, [
        ("r0", {"lr": 2e-06}, 0.351),
        ("r1", {"lr": 8e-06}, 0.298),   # the agent just made it worse
    ])

    out = await OpsTuneStatusTool().execute(ledger=str(tmp_path / "ledger.json"))

    assert "Best so far" not in out, (
        "telling the agent which trial is best answers the judgement being "
        "measured, at the moment it is being measured"
    )
    assert "0.351" in out and "0.298" in out, "every number must still be visible"
    assert out.index("0.351") < out.index("0.298"), "submission order, not ranked"


@pytest.mark.asyncio
async def test_status_shows_a_failed_trials_numbers_too(tmp_path):
    from raven.agent.tools.ops import OpsTuneStatusTool
    from raven.ops import Ledger
    from raven.ops.backend import JobResult, JobStatus

    led = _terminal_ledger(tmp_path, [("r0", {"lr": 2e-06}, 0.351)])
    led.record("r1", campaign="c")
    led.set_result("r1", JobResult(JobStatus.FAILED, error="cuda_oom", output={"config": {"lr": 1e-05}}))

    out = await OpsTuneStatusTool().execute(ledger=str(tmp_path / "ledger.json"))
    assert "[failed]" in out, "a config that broke is something the agent has to know"


@pytest.mark.asyncio
async def test_submitting_to_a_campaign_nobody_declared_is_refused(
    monkeypatch, tmp_path: Path
) -> None:
    # Creating a campaign out of a round's arguments is what ops_declare is for.
    # Measured 2026-08-18: an arm submitted into a name it had just invented, got a
    # campaign with an empty address and a container image holding none of the
    # owner's software, and spent three minutes repairing it.
    import raven.agent.tools.ops as ops_mod

    monkeypatch.setattr(ops_mod, "_ops_home", lambda: tmp_path / "ops")

    out = await OpsSubmitTool(cron_service=_FakeCron()).execute(
        objective="limit load", eta_seconds=60, campaign="beam", configs=[{"nx": 40}])

    assert out.startswith("REFUSED") and "ops_declare" in out, out
    assert not (tmp_path / "ops" / "beam" / "meta.json").exists(), (
        "a campaign that was never declared must not be left on disk for a later "
        "round to find"
    )


@pytest.mark.asyncio
async def test_command_backend_and_staged_case_are_recorded_on_the_campaign(
    monkeypatch, tmp_path: Path
) -> None:
    import raven.agent.tools.ops as ops_mod

    _patch_remote(monkeypatch)
    monkeypatch.setattr(ops_mod, "_ops_home", lambda: tmp_path / "ops")
    monkeypatch.setattr("raven.ops.connections.get",
                        lambda cid: {"host": "h", "port": 64106, "key": "~/.ssh/id_rsa"})

    tool = OpsSubmitTool(cron_service=_FakeCron())
    tool.set_context("cli", "direct")
    await tool.execute(
        objective="limit load", eta_seconds=60, campaign="beam",
        connection="conn_cpu_32c", staged_case="/srv/case",
        command="sh -c 'cd {job_dir} && bash /srv/case/run.sh'",
        configs=[{"nx": 40}], metric="collapse_load", goal="max")

    meta = json.loads((tmp_path / "ops" / "beam" / "meta.json").read_text(encoding="utf-8"))
    assert meta["command"] == "sh -c 'cd {job_dir} && bash /srv/case/run.sh'"
    assert meta["staged_case"] == "/srv/case"
    assert meta["backend"] == "process", (
        "a command is only run by the process backend, so passing one has already "
        "said which backend is meant"
    )
    assert "host" not in meta, "the address stays the connection's business"


@pytest.mark.asyncio
async def test_an_unknown_connection_id_is_refused_before_anything_is_written(
    monkeypatch, tmp_path: Path
) -> None:
    # A campaign written with an id nothing resolves has no address, and every
    # round after it fails in a way that reads as the machine being down.
    import raven.agent.tools.ops as ops_mod

    monkeypatch.setattr(ops_mod, "_ops_home", lambda: tmp_path / "ops")
    monkeypatch.setattr("raven.ops.connections.get", lambda cid: None)

    out = await OpsSubmitTool(cron_service=_FakeCron()).execute(
        objective="limit load", eta_seconds=60, campaign="beam",
        connection="conn_typo", command="bash run.sh", configs=[{"nx": 40}])

    assert out.startswith("REFUSED") and "conn_typo" in out
    assert not (tmp_path / "ops" / "beam" / "meta.json").exists()


def test_case_isolation_states_unproven_apart_from_proven_clean() -> None:
    from raven.agent.tools.ops import _case_isolation_lines
    from raven.ops.backend import JobResult, JobStatus
    from types import SimpleNamespace

    def rec(output):
        return SimpleNamespace(result=JobResult(JobStatus.SUCCEEDED, output=output))

    assert _case_isolation_lines([], has_staged_case=False) == [], (
        "a campaign with no case of the owner's has nothing to say here"
    )

    unproven = _case_isolation_lines([], has_staged_case=True)
    assert len(unproven) == 1 and "unproven, not proven safe" in unproven[0], (
        "no finished round is a different claim from having looked and found nothing"
    )

    clean = _case_isolation_lines([rec({"case_files_written": []})])
    assert len(clean) == 1 and "can run at the same time" in clean[0]

    dirty = _case_isolation_lines([
        rec({"case_files_written": []}),
        rec({"case_files_written": ["input.dat", "sub/mesh.dat"]}),
    ])
    assert "input.dat" in dirty[0] and "sub/mesh.dat" in dirty[0]
    assert "nothing to run on purpose to find out" in dirty[1], (
        "the fix rides on the next real round; a confirmation run would cost one "
        "full run of the case for a finding that arrives free"
    )
    assert "do NOT run trials at the same time" in dirty[2]
    assert "ops_ask_owner" in dirty[3], (
        "the one branch raven cannot fix has to name who decides"
    )

    settled = _case_isolation_lines([
        rec({"case_files_written": ["input.dat"]}),
        rec({"case_files_written": []}),
    ])
    assert len(settled) == 1 and "input.dat" in settled[0]
    assert "can run at the same time" in settled[0], (
        "a finding that has since stopped is a resolved one, not a standing warning"
    )
