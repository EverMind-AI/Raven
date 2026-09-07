"""The contributed ops tool set: factories, the wake route, and the fork's faces.

Part 2b of the oncall-flow plugin. What these pin, in the order the loop meets
them: every manifest row's factory constructs the tool it names (and every one
declines together when the slice leaves the flow off, the D6 gate); the
scheduling faces hold the namespaced wake grant and write ``wake_route`` into
the campaign's meta on a live schedule -- the tools are that key's first
writer, the resident watcher its reader; ``ops_kill`` runs the landed
decision-basis gate before any backend cancel; and the model-facing text keeps
the fork's bytes where TOOLS_ONCALL.md quotes them. The real CronService backs
the grant exactly as the loop's minting path does -- the one place a test may
construct the scheduler organ directly.
"""

from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PLUGIN_DIR = REPO / "agents" / "raven-oncall" / "plugins" / "oncall-flow"
sys.path.insert(0, str(PLUGIN_DIR))

from oncall_flow import wakes  # noqa: E402
from oncall_flow.backend import JobHandle, JobResult, JobStatus  # noqa: E402
from oncall_flow.backends import register_backend  # noqa: E402
from oncall_flow.instrument import read_events, read_meta, write_meta  # noqa: E402
from oncall_flow.ledger import Ledger  # noqa: E402
from oncall_flow.state_claims import StateFacts, write_facts  # noqa: E402
from oncall_flow.tools import base as tools_base  # noqa: E402
from oncall_flow.tools.ops import (  # noqa: E402
    OpsCheckLaterTool,
    OpsKillTool,
    OpsSubmitTool,
    OpsTuneStatusTool,
)
from oncall_flow.tools.ops_connections import OpsConnectionsTool  # noqa: E402
from oncall_flow.tools.ops_declare import OpsDeclareTool  # noqa: E402
from oncall_flow.tools.ops_escalation import OpsAskOwnerTool, OpsFinishTool  # noqa: E402
from oncall_flow.tools.ops_exec import OpsExecTool  # noqa: E402

from raven.plugins.context import PluginContext, RuntimeHandles, ServiceLocator  # noqa: E402
from raven.plugins.manifest import PluginManifest  # noqa: E402
from raven.proactive_engine.schedulers.cron.grant import NamespacedWakeScheduler  # noqa: E402
from raven.proactive_engine.schedulers.cron.service import CronService  # noqa: E402

NS = "oncall-flow"


def _ctx(tmp_path: Path, *, enabled: bool = True) -> PluginContext:
    config = {"enabled": True, "stateRoot": str(tmp_path / "state")} if enabled else {}
    services = ServiceLocator(workspace=tmp_path / "ws", user_id="u", agent_id="a")
    return PluginContext(config=config, services=services)


def _grant(tmp_path: Path) -> tuple[CronService, NamespacedWakeScheduler]:
    svc = CronService(tmp_path / "jobs.json", allowed_channels=None)
    return svc, NamespacedWakeScheduler(svc, NS)


def _campaign(tmp_path: Path, name: str = "camp-a", **meta) -> Path:
    tools_base.set_home(tmp_path / "state")
    cdir = tools_base.ops_home() / name
    write_meta(cdir, {"objective_words": "hold the line", "backend": "mock", **meta})
    return cdir


def _fresh_probe(cdir: Path, seq: int = 1) -> None:
    write_facts(cdir, StateFacts(probe_seq=seq))


class _Backend:
    """A cancel-tracking probe surface, registered under the mock name."""

    def __init__(self) -> None:
        self.cancelled: list[JobHandle] = []

    async def submit(self, spec):
        return JobHandle("mock", f"job-{spec.idem_key}")

    async def poll(self, handle):
        return JobStatus.RUNNING

    async def fetch_result(self, handle):
        return JobResult(JobStatus.FAILED, error="cancelled")

    async def fetch_progress(self, handle, tail=5):
        return []

    async def cancel(self, handle):
        self.cancelled.append(handle)


# ── The manifest roster and its factories ───────────────────────────


def test_every_manifest_tool_factory_constructs_its_face(tmp_path: Path) -> None:
    manifest = PluginManifest.from_toml_path(PLUGIN_DIR / "raven-plugin.toml")
    ctx = _ctx(tmp_path)
    for row in manifest.contributes.tools:
        module_path, _, attr = row.factory.partition(":")
        factory = getattr(importlib.import_module(module_path), attr)
        tool = factory(ctx)
        assert tool is not None and tool.name == row.name
    assert "exec" not in {t.name for t in manifest.contributes.tools}, (
        "the machine face is ops_exec; the same-name exec shadow is an open ruling"
    )


def test_a_disabled_slice_declines_every_factory(tmp_path: Path) -> None:
    manifest = PluginManifest.from_toml_path(PLUGIN_DIR / "raven-plugin.toml")
    ctx = _ctx(tmp_path, enabled=False)
    for row in manifest.contributes.tools:
        module_path, _, attr = row.factory.partition(":")
        factory = getattr(importlib.import_module(module_path), attr)
        assert factory(ctx) is None, f"{row.name} must decline when the flow is off"


def test_only_the_scheduling_faces_declare_the_wake_grant() -> None:
    for cls in (OpsSubmitTool, OpsCheckLaterTool, OpsAskOwnerTool, OpsFinishTool):
        assert callable(getattr(cls(), "bind_runtime", None)), cls.__name__
    for tool in (OpsTuneStatusTool(), OpsConnectionsTool(), OpsDeclareTool(), OpsKillTool()):
        assert not hasattr(tool, "bind_runtime"), tool.name


# ── The wake route: these tools are the first writer ────────────────


async def test_a_live_check_later_schedules_and_records_the_route(tmp_path: Path) -> None:
    svc, grant = _grant(tmp_path)
    cdir = _campaign(tmp_path)
    _fresh_probe(cdir)
    tool = OpsCheckLaterTool()
    tool.bind_runtime(RuntimeHandles(wake_scheduler=grant))
    tool.set_context("tui", "default", "tui:w1")

    note = await tool.execute(eta_seconds=120, campaign="camp-a", basis="still running; looked")

    assert note.startswith("Scheduled a wake")
    job = wakes.pending_look(grant, "camp-a")
    assert job is not None and "re-check" in job.payload.message
    assert read_meta(cdir)["wake_route"] == {"channel": "tui", "to": "default"}
    kinds = [e["kind"] for e in read_events(cdir)]
    assert "check_later" in kinds and "basis_accepted" in kinds


async def test_a_cold_turn_schedules_through_the_declared_route(tmp_path: Path) -> None:
    svc, grant = _grant(tmp_path)
    cdir = _campaign(tmp_path, wake_route={"direct_agent": "oncall-raven"})
    _fresh_probe(cdir)
    tool = OpsCheckLaterTool()
    tool.bind_runtime(RuntimeHandles(wake_scheduler=grant))
    tool.set_context("cron", "job-7")

    note = await tool.execute(eta_seconds=60, campaign="camp-a", basis="looked; not done")

    assert note.startswith("Scheduled a wake"), "a scheduler-fired turn is not a window; the declared wake_route serves"
    assert wakes.pending_look(grant, "camp-a") is not None


async def test_no_scheduler_answers_in_the_fork_prose(tmp_path: Path) -> None:
    cdir = _campaign(tmp_path)
    _fresh_probe(cdir)
    tool = OpsCheckLaterTool()
    tool.set_context("tui", "default")
    note = await tool.execute(eta_seconds=60, campaign="camp-a", basis="looked once more")
    assert note == wakes.NO_SCHEDULER_NOTE


async def test_a_refused_wait_arranges_nothing(tmp_path: Path) -> None:
    svc, grant = _grant(tmp_path)
    cdir = _campaign(tmp_path)  # no probe taken: the basis gate refuses
    tool = OpsCheckLaterTool()
    tool.bind_runtime(RuntimeHandles(wake_scheduler=grant))
    tool.set_context("tui", "default")
    note = await tool.execute(eta_seconds=60, campaign="camp-a", basis="loss is 2.0")
    assert note.startswith("REFUSED")
    assert wakes.pending_look(grant, "camp-a") is None
    assert "wake_route" not in read_meta(cdir), "a refusal writes no route"


async def test_a_live_submit_schedules_the_round_wake_and_records_the_route(tmp_path: Path) -> None:
    svc, grant = _grant(tmp_path)
    cdir = _campaign(tmp_path)
    register_backend("mock", lambda meta: _Backend())
    tool = OpsSubmitTool()
    tool.bind_runtime(RuntimeHandles(wake_scheduler=grant))
    tool.set_context("tui", "default", "tui:w1", "task-fp")

    out = await tool.execute(eta_seconds=300, campaign="camp-a", round=0, configs=[{"a": 1}])

    assert "Submitted 1 job(s) for campaign 'camp-a' round 0" in out
    job = wakes.pending_look(grant, "camp-a")
    assert job is not None and "round 0 due" in job.payload.message
    assert read_meta(cdir)["wake_route"] == {"channel": "tui", "to": "default"}
    kinds = [e["kind"] for e in read_events(cdir)]
    assert "submit" in kinds and "wake_scheduled" in kinds


async def test_the_recorded_route_never_trips_the_declaration_gate(tmp_path: Path) -> None:
    """The apparatus gate fingerprints the declaration minus the loop's own keys.

    Round 0 takes the meta baseline, the schedule then writes wake_route into
    the same file; without the meta_sha carve-out every round 1 would be
    refused as "meta.json has changed" by the campaign's own bookkeeping.
    """
    svc, grant = _grant(tmp_path)
    cdir = _campaign(tmp_path)
    register_backend("mock", lambda meta: _Backend())
    tool = OpsSubmitTool()
    tool.bind_runtime(RuntimeHandles(wake_scheduler=grant))
    tool.set_context("tui", "default", "tui:w1")
    out0 = await tool.execute(eta_seconds=60, campaign="camp-a", round=0, configs=[{"a": 1}])
    assert "Submitted 1 job(s)" in out0
    assert read_meta(cdir)["wake_route"] == {"channel": "tui", "to": "default"}

    _fresh_probe(cdir, seq=2)
    out1 = await tool.execute(
        eta_seconds=60, campaign="camp-a", round=1, configs=[{"a": 2}], basis="looked; step 2 recorded"
    )
    assert "REFUSED" not in out1 and "Submitted 1 job(s) for campaign 'camp-a' round 1" in out1


# ── ops_kill drives the landed basis gate, never a raw kill ─────────


async def test_kill_without_a_fresh_observation_is_refused(tmp_path: Path) -> None:
    cdir = _campaign(tmp_path)
    backend = _Backend()
    register_backend("mock", lambda meta: backend)
    led = Ledger(cdir / "ledger.json")
    led.record("t1", campaign="camp-a")
    led.set_handle("t1", JobHandle("mock", "job-1"))
    led.set_status("t1", JobStatus.RUNNING)

    out = await OpsKillTool().execute(trials=["t1"], campaign="camp-a", basis="loss is 2.0")

    assert out.startswith("REFUSED")
    assert backend.cancelled == [], "a refused kill must not reach the backend"
    assert led.get("t1").status is JobStatus.RUNNING
    assert "basis_refused" in [e["kind"] for e in read_events(cdir)]


async def test_kill_with_a_fresh_observation_cancels_and_records(tmp_path: Path) -> None:
    cdir = _campaign(tmp_path)
    backend = _Backend()
    register_backend("mock", lambda meta: backend)
    led = Ledger(cdir / "ledger.json")
    led.record("t1", campaign="camp-a")
    led.set_handle("t1", JobHandle("mock", "job-1"))
    led.set_status("t1", JobStatus.RUNNING)
    _fresh_probe(cdir)

    out = await OpsKillTool().execute(
        trials=["t1"], campaign="camp-a", basis="looked again; diverging", reason="diverged"
    )

    assert out.startswith("Killed 1 trial(s): t1")
    assert len(backend.cancelled) == 1
    rec = Ledger(cdir / "ledger.json").get("t1")
    assert rec.status is JobStatus.FAILED and "killed early: diverged" in rec.result.error
    kinds = [e["kind"] for e in read_events(cdir)]
    assert "kill" in kinds and "basis_accepted" in kinds


async def test_kill_records_what_the_backend_saw_at_kill_time(tmp_path: Path) -> None:
    """A backend that can see the process says whether it was alive and for how
    long. Measured 2026-09-03: a job at 5m11s was killed on a report that it had
    died, and "killed early" in the ledger read like cleanup. The note travels
    into the record's error and into the reply, so the kill of a live job is
    visible as that."""

    class _Seeing(_Backend):
        async def cancel(self, handle):
            self.cancelled.append(handle)
            return "process was alive (pid 4242, running 5.2 min) when killed"

    cdir = _campaign(tmp_path)
    backend = _Seeing()
    register_backend("mock", lambda meta: backend)
    led = Ledger(cdir / "ledger.json")
    led.record("t1", campaign="camp-a")
    led.set_handle("t1", JobHandle("mock", "job-1"))
    led.set_status("t1", JobStatus.RUNNING)
    _fresh_probe(cdir)

    out = await OpsKillTool().execute(trials=["t1"], campaign="camp-a", basis="looked again; dead", reason="dead")

    assert "t1: process was alive (pid 4242, running 5.2 min) when killed" in out
    rec = Ledger(cdir / "ledger.json").get("t1")
    assert "killed early: dead (process was alive (pid 4242, running 5.2 min) when killed)" == rec.result.error


# ── The escalation faces over the guard (D4: model-mediated) ────────


async def test_a_refused_ask_returns_nothing_sendable(tmp_path: Path) -> None:
    cdir = _campaign(tmp_path, interruption_contract={"min_expected_loss_ms": 60 * 60_000})
    tool = OpsAskOwnerTool()
    out = await tool.execute(
        campaign="camp-a", question="may I stop it?", expected_loss_minutes=1, blocks_progress=False
    )
    assert out.startswith("NOT DELIVERED")
    assert "message tool" not in out, "a refusal hands the model nothing to send"
    event = [e for e in read_events(cdir) if e["kind"] == "ask_owner"][-1]
    assert event["allowed"] is False


async def test_an_allowed_ask_hands_the_exact_text_and_arms_the_safety_wake(tmp_path: Path) -> None:
    svc, grant = _grant(tmp_path)
    cdir = _campaign(tmp_path)
    tool = OpsAskOwnerTool()
    tool.bind_runtime(RuntimeHandles(wake_scheduler=grant))
    tool.set_context("tui", "default")

    out = await tool.execute(
        campaign="camp-a", question="which case wins?", expected_loss_minutes=90, blocks_progress=True
    )

    assert "ALLOWED by the campaign's contract" in out
    assert "[ops campaign 'camp-a'] which case wins?" in out
    assert "message tool" in out
    job = wakes.pending_look(grant, "camp-a")
    assert job is not None and "nobody answered" in job.payload.message
    assert read_meta(cdir)["wake_route"] == {"channel": "tui", "to": "default"}


async def test_finish_files_the_report_and_stands_the_wake_down(tmp_path: Path) -> None:
    svc, grant = _grant(tmp_path)
    cdir = _campaign(tmp_path)
    _fresh_probe(cdir)
    later = OpsCheckLaterTool()
    later.bind_runtime(RuntimeHandles(wake_scheduler=grant))
    later.set_context("tui", "default")
    assert (await later.execute(eta_seconds=600, campaign="camp-a", basis="looked; waiting")).startswith("Scheduled")

    tool = OpsFinishTool()
    tool.bind_runtime(RuntimeHandles(wake_scheduler=grant))
    out = await tool.execute(
        campaign="camp-a",
        subject="the line held",
        outcome="done",
        dedupe_key="line-held",
        observed={"volt": 101.2},
        condition_type="absolute",
    )

    assert "closed (done)" in out and "1 pending wake(s) stood down" in out
    assert "Deliver the report below to the owner with the message tool" in out
    assert wakes.pending_look(grant, "camp-a") is None
    assert (cdir / "concluded.json").exists()
    assert list(cdir.glob("report-*.md")), "the report file is the owner's copy"
    assert json.loads((cdir / "reports.jsonl").read_text().splitlines()[-1])["dedupe_key"] == "line-held"


# ── The machine face, and the fork bytes the prompt quotes ──────────


async def test_ops_exec_refuses_work_that_outlives_the_call(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("RAVEN_CONNECTIONS", str(tmp_path / "none.json"))
    tools_base.set_home(tmp_path / "state")
    out = await OpsExecTool().execute(command="nohup ./solve &", machine="box")
    assert "nohup" in out and "ops_submit" in out and "Nothing was run" in out


def test_model_facing_text_keeps_the_fork_bytes() -> None:
    assert (
        OpsTuneStatusTool().description.count("Call it with NO ARGUMENTS to read the campaign that is already set up")
        == 1
    )
    assert OpsSubmitTool().description.startswith(
        "Run a long computation on a remote machine -- ONE ROUND AT A TIME, steering it yourself."
    )
    assert OpsCheckLaterTool().description.startswith("Wait longer on a campaign whose jobs are not finished yet")
    assert OpsDeclareTool().description.startswith("Declare a campaign ONCE, before any of it runs.")
    assert "This kills individual trials; to conclude the whole campaign use ops_finish." in (OpsKillTool().description)
    assert OpsConnectionsTool().description.startswith("List the machines this instance can run work on")
    assert "The report is CHECKED before it is accepted" in OpsFinishTool().description
    assert "You MUST estimate expected_loss_minutes" in OpsAskOwnerTool().description
    # The one deliberate rewording (D4): the ask face still names itself the
    # only door, and now says what the model does with an allowed ask.
    assert "This is the ONLY door to the owner during a campaign" in OpsAskOwnerTool().description
