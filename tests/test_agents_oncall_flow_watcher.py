"""The oncall event watcher rides the two landed seams, and nothing else.

Part one of the oncall-flow plugin: the service satisfies the PluginService
paper (contracts/services.py), its wakes ride the keyed WakeScheduler grant
(contracts/scheduling.py) under the plugin's own namespace, stop is
idempotent, and a host that lends no scheduler gets a loud no-op. The real
CronService backs the grant here exactly as the loop's minting path does --
the one place a test may construct the scheduler organ directly.
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
from oncall_flow.config import FlowConfig, state_root  # noqa: E402
from oncall_flow.state import (  # noqa: E402
    CampaignStore,
    Ledger,
    LedgerCorruptError,
    TaskHandle,
    TaskStatus,
    read_events,
    write_meta,
)
from oncall_flow.watcher import OpsEventWatcher, make_event_watcher  # noqa: E402

from raven.contracts.services import PluginService  # noqa: E402
from raven.plugins.context import PluginContext, RuntimeHandles, ServiceLocator  # noqa: E402
from raven.plugins.manifest import PluginManifest  # noqa: E402
from raven.proactive_engine.schedulers.cron.grant import NamespacedWakeScheduler  # noqa: E402
from raven.proactive_engine.schedulers.cron.service import CronService  # noqa: E402

NS = "oncall-flow"


def _grant(tmp_path: Path) -> tuple[CronService, NamespacedWakeScheduler]:
    svc = CronService(tmp_path / "jobs.json", allowed_channels=None)
    return svc, NamespacedWakeScheduler(svc, NS)


def _watcher(tmp_path: Path, **kwargs) -> OpsEventWatcher:
    return OpsEventWatcher(CampaignStore(tmp_path / "state"), **kwargs)


def _campaign(store: CampaignStore, name: str, *, status: TaskStatus = TaskStatus.RUNNING) -> Path:
    cdir = store.dir_for(name)
    write_meta(cdir, {"objective": "hold the line", "backend": "mock"})
    led = Ledger(cdir / "ledger.json")
    led.record("t1", campaign=name)
    led.set_handle("t1", TaskHandle("mock", "job-1"))
    led.set_status("t1", status)
    return cdir


class _Probe:
    """A fake backend poll surface; part two's backends implement the real one."""

    def __init__(self, status: TaskStatus, samples: list[dict] | None = None) -> None:
        self._status = status
        self._samples = samples or []

    async def poll(self, handle):
        return self._status

    async def fetch_result(self, handle):
        return {"status": self._status.value}

    async def fetch_progress(self, handle, tail=1):
        return self._samples


def test_the_manifest_contributes_exactly_the_watcher_service() -> None:
    manifest = PluginManifest.from_toml_path(PLUGIN_DIR / "raven-plugin.toml")
    assert manifest.id == NS
    assert [s.name for s in manifest.contributes.services] == ["oncall_event_watcher"]
    assert manifest.contributes.services[0].factory == "oncall_flow.watcher:make_event_watcher"
    assert manifest.contributes.tools == [] and manifest.contributes.hooks == [], (
        "part one declares only what part one implements; the gate axes and "
        "ops tools add their own entries when they land"
    )


def test_the_watcher_satisfies_the_services_paper(tmp_path: Path) -> None:
    assert isinstance(_watcher(tmp_path), PluginService)


def test_the_factory_declines_when_the_slice_leaves_the_flow_off(tmp_path: Path) -> None:
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

    cfg = FlowConfig.from_slice({"enabled": True})
    assert cfg.watcher.poll_interval_seconds == 20.0, "the fork's default pace"
    assert state_root({"stateRoot": str(tmp_path / "s")}, tmp_path) == tmp_path / "s"


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


async def test_a_terminal_round_pulls_the_wake_forward(tmp_path: Path) -> None:
    _svc, grant = _grant(tmp_path)
    watcher = _watcher(tmp_path, probe_from_meta=lambda meta: _Probe(TaskStatus.SUCCEEDED))
    await watcher.start(RuntimeHandles(wake_scheduler=grant))
    assert watcher.running
    await watcher.stop()

    cdir = _campaign(watcher.store, "camp-a")
    wakes.schedule_next_look(grant, campaign="camp-a", eta_seconds=3600, message="look", channel="tui", to="default")

    assert await watcher.tick() == ["camp-a"]
    assert wakes.pending_look(grant, "camp-a").state.next_run_at_ms <= int(time.time() * 1000)
    assert Ledger(cdir / "ledger.json").get("t1").status is TaskStatus.SUCCEEDED
    kinds = [e["kind"] for e in read_events(cdir)]
    assert kinds == ["trial_terminal_observed", "event_wake_advanced"]

    assert await watcher.tick() == [], "a settled campaign has nothing pending to advance"


async def test_unhealthy_progress_is_an_event_too(tmp_path: Path) -> None:
    _svc, grant = _grant(tmp_path)
    probe = _Probe(TaskStatus.RUNNING, samples=[{"step": 3, "loss": float("nan")}])
    watcher = _watcher(tmp_path, probe_from_meta=lambda meta: probe)
    await watcher.start(RuntimeHandles(wake_scheduler=grant))
    await watcher.stop()

    cdir = _campaign(watcher.store, "camp-a")
    wakes.schedule_next_look(grant, campaign="camp-a", eta_seconds=3600, message="look", channel="tui", to="default")

    assert await watcher.tick() == ["camp-a"]
    events = read_events(cdir)
    assert events[-1]["kind"] == "event_wake_advanced"
    assert events[-1]["reason"].startswith("unhealthy progress")


async def test_a_corrupt_campaign_is_skipped_and_the_rest_still_advance(tmp_path: Path) -> None:
    _svc, grant = _grant(tmp_path)
    watcher = _watcher(tmp_path, probe_from_meta=lambda meta: _Probe(TaskStatus.SUCCEEDED))
    await watcher.start(RuntimeHandles(wake_scheduler=grant))
    await watcher.stop()

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
