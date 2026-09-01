"""The oncall event watcher: the probe drives the loop over the landed seams.

Part 2a of the oncall-flow plugin: the resident service still satisfies the
PluginService paper (contracts/services.py) and rides the keyed WakeScheduler
grant (contracts/scheduling.py) under the plugin's own namespace -- and the
loop is now poll -> probe -> decide -> act for real: a due round advances (or,
when the agent left no wake pending, schedules) the next look, a failed trial
whose retry budget is spent escalates exactly once, and a concluded campaign's
pending wake is stood down. The real CronService backs the grant here exactly
as the loop's minting path does -- the one place a test may construct the
scheduler organ directly.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

from loguru import logger

REPO = Path(__file__).resolve().parent.parent
PLUGIN_DIR = REPO / "agents" / "raven-oncall" / "plugins" / "oncall-flow"
sys.path.insert(0, str(PLUGIN_DIR))

from oncall_flow import wakes  # noqa: E402
from oncall_flow.backend import JobHandle, JobResult, JobStatus  # noqa: E402
from oncall_flow.backends import backend_from_meta  # noqa: E402
from oncall_flow.config import FlowConfig, state_root  # noqa: E402
from oncall_flow.instrument import (  # noqa: E402
    CampaignStore,
    conclude,
    read_events,
    write_meta,
)
from oncall_flow.ledger import Ledger, LedgerCorruptError  # noqa: E402
from oncall_flow.watcher import OpsEventWatcher, make_event_watcher  # noqa: E402

from raven.contracts.services import PluginService  # noqa: E402
from raven.plugins.context import PluginContext, RuntimeHandles, ServiceLocator  # noqa: E402
from raven.plugins.manifest import PluginManifest  # noqa: E402
from raven.proactive_engine.schedulers.cron.grant import NamespacedWakeScheduler  # noqa: E402
from raven.proactive_engine.schedulers.cron.service import CronService  # noqa: E402

NS = "oncall-flow"

ROUTE = {"wake_route": {"channel": "tui", "to": "default"}}


def _grant(tmp_path: Path) -> tuple[CronService, NamespacedWakeScheduler]:
    svc = CronService(tmp_path / "jobs.json", allowed_channels=None)
    return svc, NamespacedWakeScheduler(svc, NS)


def _watcher(tmp_path: Path, **kwargs) -> OpsEventWatcher:
    return OpsEventWatcher(CampaignStore(tmp_path / "state"), **kwargs)


def _campaign(
    store: CampaignStore,
    name: str,
    *,
    status: JobStatus = JobStatus.RUNNING,
    meta: dict | None = None,
) -> Path:
    cdir = store.dir_for(name)
    write_meta(cdir, {"objective": "hold the line", "backend": "mock", **(meta or {})})
    led = Ledger(cdir / "ledger.json")
    led.record("t1", campaign=name)
    led.set_handle("t1", JobHandle("mock", "job-1"))
    led.set_status("t1", status)
    return cdir


class _Probe:
    """A fake backend poll surface with the JobBackend result shape."""

    def __init__(self, status: JobStatus, samples: list[dict] | None = None, error: str | None = None) -> None:
        self._status = status
        self._samples = samples or []
        self._error = error

    async def poll(self, handle):
        return self._status

    async def fetch_result(self, handle):
        return JobResult(self._status, error=self._error)

    async def fetch_progress(self, handle, tail=1):
        return self._samples


async def _started(watcher: OpsEventWatcher, grant) -> None:
    await watcher.start(RuntimeHandles(wake_scheduler=grant))
    await watcher.stop()


def test_the_manifest_contributes_the_watcher_service_and_the_ops_tools() -> None:
    manifest = PluginManifest.from_toml_path(PLUGIN_DIR / "raven-plugin.toml")
    assert manifest.id == NS
    assert [s.name for s in manifest.contributes.services] == ["oncall_event_watcher"]
    assert manifest.contributes.services[0].factory == "oncall_flow.watcher:make_event_watcher"
    # Part 2b's roster: the fork's thirteen agent tools plus the machine face
    # under its own name (ops_exec; the exec shadow is an open ruling). No
    # entry shadows a built-in, ops_tune_launch stays off the menu on purpose,
    # and the gate axes still add their own [[hooks]] rows when 2c lands.
    assert [t.name for t in manifest.contributes.tools] == [
        "ops_tune_status",
        "ops_submit",
        "ops_check_later",
        "ops_note",
        "ops_campaigns",
        "ops_connections",
        "ops_declare",
        "ops_kill",
        "ops_outputs",
        "ops_edit_case_dict",
        "ops_case_changes",
        "ops_ask_owner",
        "ops_finish",
        "ops_exec",
    ]
    assert all(t.factory == f"oncall_flow.tools:make_{t.name}" for t in manifest.contributes.tools), (
        "one factory per face, all in oncall_flow.tools"
    )
    assert manifest.contributes.hooks == [], (
        "part 2b still declares only what it implements; the gate axes add their own entries when they land (2c)"
    )


def test_the_watcher_satisfies_the_services_paper(tmp_path: Path) -> None:
    assert isinstance(_watcher(tmp_path), PluginService)


def test_the_factory_declines_when_off_and_wires_the_real_probe_seam(tmp_path: Path) -> None:
    locator = ServiceLocator(workspace=tmp_path, user_id="u", agent_id="a")
    assert make_event_watcher(PluginContext(config={}, services=locator)) is None
    assert make_event_watcher(PluginContext(config={"enabled": False}, services=locator)) is None

    built = make_event_watcher(
        PluginContext(
            config={"enabled": True, "watcher": {"pollIntervalSeconds": 5.0}},
            services=locator,
        )
    )
    assert built is not None
    assert built.poll_interval == 5.0
    assert built.store.root == tmp_path / "oncall_flow", "stateRoot defaults under the workspace"
    assert built._probe_from_meta is backend_from_meta, (
        "the probe seam resolves through the same registry the tools use"
    )

    cfg = FlowConfig.from_slice({"enabled": True})
    assert cfg.watcher.poll_interval_seconds == 20.0, "the fork's default pace"
    assert state_root({"stateRoot": str(tmp_path / "s")}, tmp_path) == tmp_path / "s"


def test_the_backend_registry_names_what_it_knows() -> None:
    try:
        backend_from_meta({"backend": "no-such-backend"})
        raise AssertionError("an unknown backend must refuse with the known names")
    except ValueError as exc:
        text = str(exc)
        for name in ("docker", "openfoam", "process"):
            assert name in text


def test_wake_verbs_ride_the_grant_under_the_plugin_namespace(tmp_path: Path) -> None:
    svc, grant = _grant(tmp_path)
    note = wakes.schedule_next_look(
        grant, campaign="camp-a", eta_seconds=3600, message="look", channel="tui", to="default"
    )
    assert note.startswith("Scheduled a wake at ~") and "replaced" not in note
    job = wakes.pending_look(grant, "camp-a")
    assert job is not None and job.id == f"wake:{NS}:camp-a"

    note2 = wakes.schedule_next_look(
        grant, campaign="camp-a", eta_seconds=7200, message="look later", channel="tui", to="default"
    )
    assert "This replaced the campaign's previous pending wake" in note2
    pending = svc.pending_wakes(f"{NS}:camp-a")
    assert len(pending) == 1, "replace, not join: one campaign, one pending wake"
    assert pending[0].payload.message == "look later"

    assert wakes.advance_look(grant, "camp-a") is True
    assert wakes.pending_look(grant, "camp-a").state.next_run_at_ms <= int(time.time() * 1000)
    assert wakes.cancel_look(grant, "camp-a") is True
    assert wakes.pending_look(grant, "camp-a") is None
    assert wakes.advance_look(grant, "camp-a") is False

    assert wakes.schedule_next_look(None, campaign="c", eta_seconds=1, message="m") == wakes.NO_SCHEDULER_NOTE
    assert wakes.schedule_next_look(grant, campaign="c", eta_seconds=1, message="m") == wakes.NO_CONTEXT_NOTE


def test_the_route_is_a_campaign_fact() -> None:
    assert wakes.wake_route({}) == {}
    assert wakes.wake_route({"wake_route": {"channel": "tui"}}) == {}, "channel without to is unusable"
    assert wakes.wake_route(ROUTE) == {"channel": "tui", "to": "default"}
    assert wakes.wake_route({"wake_route": {"direct_agent": "oncall", "extra": "x"}}) == {"direct_agent": "oncall"}, (
        "direct_agent alone routes (the D9 rebuilt addressing); unknown keys are dropped"
    )


async def test_a_terminal_round_pulls_the_wake_forward(tmp_path: Path) -> None:
    _svc, grant = _grant(tmp_path)
    watcher = _watcher(tmp_path, probe_from_meta=lambda meta: _Probe(JobStatus.SUCCEEDED))
    await watcher.start(RuntimeHandles(wake_scheduler=grant))
    assert watcher.running
    await watcher.stop()

    cdir = _campaign(watcher.store, "camp-a")
    wakes.schedule_next_look(grant, campaign="camp-a", eta_seconds=3600, message="look", channel="tui", to="default")

    assert await watcher.tick() == ["camp-a"]
    assert wakes.pending_look(grant, "camp-a").state.next_run_at_ms <= int(time.time() * 1000)
    assert wakes.pending_look(grant, "camp-a").payload.message == "look", (
        "the event advances the agent's own wake; its message is the better cold-start context"
    )
    assert Ledger(cdir / "ledger.json").get("t1").status is JobStatus.SUCCEEDED
    kinds = [e["kind"] for e in read_events(cdir)]
    assert kinds == ["trial_terminal_observed", "event_wake_advanced"]

    assert await watcher.tick() == [], "a settled campaign has nothing in flight to probe"


async def test_a_due_round_with_no_pending_wake_schedules_the_look(tmp_path: Path) -> None:
    _svc, grant = _grant(tmp_path)
    watcher = _watcher(tmp_path, probe_from_meta=lambda meta: _Probe(JobStatus.SUCCEEDED))
    await _started(watcher, grant)

    cdir = _campaign(watcher.store, "camp-a", meta=ROUTE)
    assert wakes.pending_look(grant, "camp-a") is None, "the agent never scheduled (or crashed mid-turn)"

    assert await watcher.tick() == ["camp-a"]
    job = wakes.pending_look(grant, "camp-a")
    assert job is not None and job.payload.message.startswith("[Ops campaign 'camp-a' re-check]"), (
        "the watcher's own look carries the recheck composer: read status, decide, every branch naming its tool"
    )
    kinds = [e["kind"] for e in read_events(cdir)]
    assert kinds == ["trial_terminal_observed", "wake_scheduled"]


async def test_a_due_round_with_neither_wake_nor_route_waits(tmp_path: Path) -> None:
    _svc, grant = _grant(tmp_path)
    watcher = _watcher(tmp_path, probe_from_meta=lambda meta: _Probe(JobStatus.SUCCEEDED))
    await _started(watcher, grant)

    cdir = _campaign(watcher.store, "camp-a")
    assert await watcher.tick() == [], "no pending wake and no declared route: delayed, not lost"
    assert wakes.pending_look(grant, "camp-a") is None
    kinds = [e["kind"] for e in read_events(cdir)]
    assert kinds == ["trial_terminal_observed"], "the result is still recorded either way"


async def test_a_concluded_campaign_cancels_its_pending_wake(tmp_path: Path) -> None:
    _svc, grant = _grant(tmp_path)
    watcher = _watcher(tmp_path, probe_from_meta=lambda meta: _Probe(JobStatus.SUCCEEDED))
    await _started(watcher, grant)

    cdir = _campaign(watcher.store, "camp-a")
    wakes.schedule_next_look(grant, campaign="camp-a", eta_seconds=3600, message="look", channel="tui", to="default")
    conclude(cdir, {"outcome": "done"})

    assert await watcher.tick() == ["camp-a"]
    assert wakes.pending_look(grant, "camp-a") is None, "a closed watch must not come back"
    assert read_events(cdir)[-1]["kind"] == "wake_cancelled"
    assert await watcher.tick() == [], "cancelling is once; a concluded campaign then stays quiet"


async def test_escalation_fires_once_and_rides_the_wake(tmp_path: Path) -> None:
    _svc, grant = _grant(tmp_path)
    watcher = _watcher(tmp_path, probe_from_meta=lambda meta: _Probe(JobStatus.FAILED, error="cuda OOM"))
    await _started(watcher, grant)

    cdir = _campaign(watcher.store, "camp-a", meta=ROUTE)

    assert await watcher.tick() == ["camp-a"]
    rec = Ledger(cdir / "ledger.json").get("t1")
    assert rec.status is JobStatus.FAILED and rec.escalated is True
    assert rec.result.error == "cuda OOM"
    job = wakes.pending_look(grant, "camp-a")
    assert job is not None
    assert job.payload.message.startswith("[Ops campaign 'camp-a' escalation]")
    assert "cuda OOM" in job.payload.message
    kinds = [e["kind"] for e in read_events(cdir)]
    assert kinds == ["trial_terminal_observed", "escalated", "wake_scheduled"]

    assert await watcher.tick() == [], "nothing left in flight"
    kinds = [e["kind"] for e in read_events(cdir)]
    assert kinds.count("escalated") == 1, "escalation fires at most once per trial, across ticks"


async def test_escalation_replaces_the_pending_wake_with_its_context(tmp_path: Path) -> None:
    _svc, grant = _grant(tmp_path)
    watcher = _watcher(tmp_path, probe_from_meta=lambda meta: _Probe(JobStatus.FAILED, error="diverged"))
    await _started(watcher, grant)

    _campaign(watcher.store, "camp-a", meta=ROUTE)
    wakes.schedule_next_look(
        grant, campaign="camp-a", eta_seconds=3600, message="routine look", channel="tui", to="default"
    )

    assert await watcher.tick() == ["camp-a"]
    job = wakes.pending_look(grant, "camp-a")
    assert job.payload.message.startswith("[Ops campaign 'camp-a' escalation]"), (
        "last request wins: the escalation context replaces the routine look"
    )
    assert job.state.next_run_at_ms <= int(time.time() * 1000) + 2_000


async def test_a_retry_still_allowed_is_not_an_escalation(tmp_path: Path) -> None:
    _svc, grant = _grant(tmp_path)
    watcher = _watcher(tmp_path, probe_from_meta=lambda meta: _Probe(JobStatus.FAILED, error="flaky node"))
    await _started(watcher, grant)

    cdir = _campaign(watcher.store, "camp-a", meta={**ROUTE, "retry": {"max_retries": 1}})

    assert await watcher.tick() == ["camp-a"]
    rec = Ledger(cdir / "ledger.json").get("t1")
    assert rec.escalated is False, "the policy still allows a retry; the resubmit is the agent's"
    job = wakes.pending_look(grant, "camp-a")
    assert job.payload.message.startswith("[Ops campaign 'camp-a' re-check]")
    kinds = [e["kind"] for e in read_events(cdir)]
    assert "escalated" not in kinds


async def test_unhealthy_progress_is_an_event_too(tmp_path: Path) -> None:
    _svc, grant = _grant(tmp_path)
    probe = _Probe(JobStatus.RUNNING, samples=[{"step": 3, "loss": float("nan")}])
    watcher = _watcher(tmp_path, probe_from_meta=lambda meta: probe)
    await _started(watcher, grant)

    cdir = _campaign(watcher.store, "camp-a")
    wakes.schedule_next_look(grant, campaign="camp-a", eta_seconds=3600, message="look", channel="tui", to="default")

    assert await watcher.tick() == ["camp-a"]
    events = read_events(cdir)
    assert events[-1]["kind"] == "event_wake_advanced"
    assert events[-1]["reason"].startswith("unhealthy progress")


async def test_a_corrupt_campaign_is_skipped_and_the_rest_still_advance(tmp_path: Path) -> None:
    _svc, grant = _grant(tmp_path)
    watcher = _watcher(tmp_path, probe_from_meta=lambda meta: _Probe(JobStatus.SUCCEEDED))
    await _started(watcher, grant)

    bad = watcher.store.dir_for("a-corrupt")
    write_meta(bad, {"backend": "mock"})
    (bad / "ledger.json").write_text("{not json", encoding="utf-8")
    _campaign(watcher.store, "b-good")
    wakes.schedule_next_look(grant, campaign="b-good", eta_seconds=3600, message="look", channel="tui", to="default")

    assert await watcher.tick() == ["b-good"], "one unreadable campaign must not stall the others"
    try:
        Ledger(bad / "ledger.json")
        raise AssertionError("a corrupt ledger must refuse to open")
    except LedgerCorruptError:
        pass


async def test_a_missing_grant_is_a_loud_no_op_and_stop_is_idempotent(tmp_path: Path) -> None:
    watcher = _watcher(tmp_path)
    await watcher.stop()

    records: list[str] = []
    sink = logger.add(lambda m: records.append(str(m)), level="ERROR")
    try:
        await watcher.start(RuntimeHandles())
    finally:
        logger.remove(sink)
    assert watcher.running is False, "no scheduler on this host: the watcher stays stopped"
    assert any("wake scheduler" in r for r in records), "and it says so at error level"

    assert await watcher.tick() == []
    await watcher.stop()
    await watcher.stop()


async def test_start_is_once_and_restart_after_stop_works(tmp_path: Path) -> None:
    _svc, grant = _grant(tmp_path)
    watcher = _watcher(tmp_path)
    handles = RuntimeHandles(wake_scheduler=grant)

    await watcher.start(handles)
    first = watcher._task
    await watcher.start(handles)
    assert watcher._task is first, "a second start does not stack a second loop"
    await watcher.stop()
    assert watcher.running is False

    await watcher.start(handles)
    assert watcher.running
    await watcher.stop()
