"""Tests for the resident wake shell (raven.ops.wake_shell).

The load-bearing properties are: it fires a due job that nothing else would
fire, it refuses the store a live run owns, it does not claim IM jobs, and every
spawn is recorded (nothing downstream can tell "fired and did nothing" from
"never fired").

No real agent is launched -- the spawner is injected.
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import pytest

from raven.ops.wake_shell import (
    DEFAULT_CHANNELS,
    SharedStoreRefused,
    Spawn,
    SubprocessTurn,
    WakeShell,
    assert_store_is_isolated,
    default_store_path,
    main,
)


def _store(tmp_path: Path, jobs: list[dict]) -> Path:
    path = tmp_path / "cron" / "jobs.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"version": 1, "jobs": jobs}, ensure_ascii=False), encoding="utf-8")
    return path


def _job(job_id: str, *, channel: str | None, due_ms: int, message: str = "go", name: str | None = None) -> dict:
    return {
        "id": job_id,
        "name": name or f"ops:{job_id}:recheck",
        "enabled": True,
        "schedule": {"kind": "at", "atMs": due_ms, "everyMs": None, "expr": None, "tz": None},
        "payload": {"kind": "agent_turn", "message": message, "deliver": True,
                    "channel": channel, "to": "default", "topicTag": None},
        "state": {"nextRunAtMs": due_ms, "lastRunAtMs": None, "lastStatus": None, "lastError": None,
                  "claimedByPid": None, "claimedAtMs": None, "silentFireCount": 0},
        "createdAtMs": due_ms - 1000,
        "updatedAtMs": due_ms - 1000,
        "deleteAfterRun": True,
        "silentFireLimit": 12,
    }


class _RecordingSpawner:
    """Stands in for launching a turn. Records the job it was handed."""

    def __init__(self, returncode: int = 0, error: str | None = None):
        self.jobs: list = []
        self._rc = returncode
        self._error = error

    async def __call__(self, job) -> Spawn:
        self.jobs.append(job)
        return Spawn(
            job_id=job.id, job_name=job.name, at_ms=int(time.time() * 1000),
            argv=["fake", "agent", "-m", job.payload.message],
            returncode=self._rc, duration_ms=7, error=self._error,
        )


async def _drain(shell: WakeShell, *, ticks: int = 20) -> None:
    """Let the service's timer fire. It arms a real asyncio timer, so this
    yields repeatedly rather than sleeping a fixed wall-clock amount."""
    await shell.start()
    try:
        for _ in range(ticks):
            await asyncio.sleep(0.1)
    finally:
        shell.stop()


# --- the point of the whole thing ---


@pytest.mark.asyncio
async def test_a_due_tui_job_is_fired(tmp_path):
    """The case nothing fires today: a wake the loop scheduled from a TUI, with
    no TUI still open."""
    store = _store(tmp_path, [_job("j1", channel="tui", due_ms=int(time.time() * 1000) + 200)])
    spawner = _RecordingSpawner()

    await _drain(WakeShell(store, spawner=spawner))

    assert [j.id for j in spawner.jobs] == ["j1"]


@pytest.mark.asyncio
async def test_the_message_is_passed_through_untouched(tmp_path):
    """The shell must not interpret the payload -- it has no task semantics."""
    message = "[Ops campaign 'x' re-check] Call ops_tune_status(...) again."
    store = _store(tmp_path, [_job("j1", channel="tui", due_ms=int(time.time() * 1000) + 200, message=message)])
    spawner = _RecordingSpawner()

    await _drain(WakeShell(store, spawner=spawner))

    assert spawner.jobs[0].payload.message == message


@pytest.mark.asyncio
async def test_a_job_not_yet_due_is_left_alone(tmp_path):
    store = _store(tmp_path, [_job("j1", channel="tui", due_ms=int(time.time() * 1000) + 3_600_000)])
    spawner = _RecordingSpawner()

    await _drain(WakeShell(store, spawner=spawner))

    assert spawner.jobs == []


@pytest.mark.asyncio
async def test_an_im_channel_job_is_not_claimed(tmp_path):
    """Claiming a feishu job would race the gateway and forward a wake to a real
    person's IM."""
    store = _store(tmp_path, [_job("j1", channel="feishu", due_ms=int(time.time() * 1000) + 200)])
    spawner = _RecordingSpawner()

    await _drain(WakeShell(store, spawner=spawner))

    assert spawner.jobs == []


@pytest.mark.asyncio
async def test_channels_any_claims_an_im_job(tmp_path):
    """The escape hatch exists but must be explicit."""
    store = _store(tmp_path, [_job("j1", channel="feishu", due_ms=int(time.time() * 1000) + 200)])
    spawner = _RecordingSpawner()

    await _drain(WakeShell(store, spawner=spawner, allowed_channels=None))

    assert [j.id for j in spawner.jobs] == ["j1"]


@pytest.mark.asyncio
async def test_default_channels_are_the_ephemeral_ones():
    assert DEFAULT_CHANNELS == frozenset({"tui", "cli"})
    assert "feishu" not in DEFAULT_CHANNELS


# --- the guard against colliding with a live run ---


def test_the_default_store_is_refused(tmp_path):
    with pytest.raises(SharedStoreRefused) as excinfo:
        assert_store_is_isolated(default_store_path())

    assert "--allow-shared-store" in str(excinfo.value)


def test_the_default_store_is_allowed_when_insisted():
    assert_store_is_isolated(default_store_path(), allow_shared=True)


def test_an_isolated_store_passes(tmp_path):
    assert_store_is_isolated(tmp_path / "cron" / "jobs.json")


def test_constructing_the_shell_on_the_default_store_raises():
    with pytest.raises(SharedStoreRefused):
        WakeShell(default_store_path(), spawner=_RecordingSpawner())


def test_main_refuses_the_default_store_with_exit_2(capsys):
    code = main(["--store", str(default_store_path())])

    assert code == 2
    assert "REFUSED" in capsys.readouterr().err


# --- the ledger ---


@pytest.mark.asyncio
async def test_every_spawn_is_recorded(tmp_path):
    """Nothing downstream distinguishes "fired and the turn did nothing" from
    "never fired", and the cron store only keeps the most recent run."""
    store = _store(tmp_path, [_job("j1", channel="tui", due_ms=int(time.time() * 1000) + 200)])
    shell = WakeShell(store, spawner=_RecordingSpawner())

    await _drain(shell)

    rows = [json.loads(line) for line in shell.ledger_path.read_text(encoding="utf-8").splitlines()]
    assert len(rows) == 1
    assert rows[0]["record"] == "spawn"
    assert rows[0]["job_id"] == "j1"
    assert rows[0]["returncode"] == 0
    assert rows[0]["late_ms"] is None, "an on-time wake is not a late one"
    assert rows[0]["argv"][:2] == ["fake", "agent"]


@pytest.mark.asyncio
async def test_a_failing_turn_is_recorded_not_hidden(tmp_path):
    store = _store(tmp_path, [_job("j1", channel="tui", due_ms=int(time.time() * 1000) + 200)])
    shell = WakeShell(store, spawner=_RecordingSpawner(returncode=1, error="boom"))

    await _drain(shell)

    assert shell.status()["spawns_nonzero_exit"] == 1
    assert shell.status()["spawns_with_error"] == 1
    rows = [json.loads(line) for line in shell.ledger_path.read_text(encoding="utf-8").splitlines()]
    assert rows[0]["error"] == "boom"


@pytest.mark.asyncio
async def test_an_unwritable_ledger_does_not_stop_the_shell(tmp_path):
    """A gap in the log is visible; a shell that died over its own bookkeeping
    would lose the wake itself."""
    store = _store(tmp_path, [_job("j1", channel="tui", due_ms=int(time.time() * 1000) + 200)])
    shell = WakeShell(store, spawner=_RecordingSpawner())
    # A directory where the file should go makes the append fail.
    shell.ledger_path.mkdir(parents=True, exist_ok=True)

    await _drain(shell)

    assert len(shell.spawns) == 1


@pytest.mark.asyncio
async def test_status_reports_counts_without_a_verdict(tmp_path):
    store = _store(tmp_path, [_job("j1", channel="tui", due_ms=int(time.time() * 1000) + 200)])
    shell = WakeShell(store, spawner=_RecordingSpawner())

    await _drain(shell)
    status = shell.status()

    assert status["spawns"] == 1
    assert set(status) >= {"store_path", "ledger_path", "spawns", "spawns_nonzero_exit", "spawns_with_error"}
    for verdict_word in ("ok", "success", "passed", "healthy"):
        assert verdict_word not in json.dumps(status).lower().replace('"lastStatus": null', "")


# --- the spawned command line ---


def test_the_turn_is_a_fresh_session_not_a_resume():
    """Resuming would hand the turn a conversation to lean on, and picking the
    trail back up off disk is the behaviour under test."""
    argv = SubprocessTurn()._argv("hello")

    assert "-m" in argv and "hello" in argv
    assert "--resume" not in argv and "--continue" not in argv and "-c" not in argv


def test_the_config_path_is_forwarded_so_the_child_shares_the_isolated_home(tmp_path):
    cfg = tmp_path / "config.json"

    argv = SubprocessTurn(config_path=cfg)._argv("hello")

    assert "--config" in argv and str(cfg) in argv


@pytest.mark.asyncio
async def test_the_local_cost_map_is_set_for_the_child(monkeypatch):
    """LiteLLM otherwise fetches a remote price table on each process's first
    completion -- once for a resident process, every wake for this shell (~4.4s
    each on a box with no route out)."""
    seen: dict = {}

    class _Proc:
        returncode = 0

        async def communicate(self):
            return (b"", b"")

    async def _fake_exec(*argv, env=None, **kw):
        seen["argv"] = list(argv)
        seen["env"] = env
        return _Proc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_exec)

    class _J:
        id = "j1"
        name = "n"

        class payload:
            message = "hi"

    spawn = await SubprocessTurn(executable="/bin/true")(_J())

    assert seen["env"]["LITELLM_LOCAL_MODEL_COST_MAP"] == "True"
    assert spawn.returncode == 0


# --- the already-due wake (CronService drops it on purpose) ---


@pytest.mark.asyncio
async def test_an_already_due_wake_is_dropped_by_default(tmp_path):
    """CronService.start discards a past-due one-shot: not re-delivering a missed
    reminder is deliberate product behaviour, so the shell inherits it unless
    asked otherwise."""
    store = _store(tmp_path, [_job("j1", channel="tui", due_ms=int(time.time() * 1000) - 60_000)])
    spawner = _RecordingSpawner()

    shell = WakeShell(store, spawner=spawner)
    await _drain(shell)

    assert spawner.jobs == []
    assert shell.status()["wakes_fired_late"] == []
    # But the miss is RECORDED, not silent: a wake that vanishes because a flag
    # defaulted off must not look like a wake that was never scheduled.
    dropped = shell.status()["wakes_dropped_unfired"]
    assert len(dropped) == 1
    assert dropped[0]["late_ms"] >= 60_000
    assert "fire_missed is off" in dropped[0]["not_fired_reason"]
    rows = [json.loads(line) for line in shell.ledger_path.read_text(encoding="utf-8").splitlines()]
    assert [r["record"] for r in rows] == ["overdue_wake"]
    assert rows[0]["fired"] is False


@pytest.mark.asyncio
async def test_an_already_due_wake_is_fired_when_asked(tmp_path):
    """For an on-call wake the opposite is wanted: due and unfired is the whole
    condition this shell exists to fix."""
    store = _store(tmp_path, [_job("j1", channel="tui", due_ms=int(time.time() * 1000) - 60_000)])
    spawner = _RecordingSpawner()

    shell = WakeShell(store, spawner=spawner, fire_missed=True)
    await _drain(shell)

    assert [j.id for j in spawner.jobs] == ["j1"]
    late = shell.status()["wakes_fired_late"]
    assert len(late) == 1 and late[0]["job_name"] == "ops:j1:recheck"
    assert late[0]["late_ms"] >= 60_000 and late[0]["fired"] is True
    assert shell.status()["wakes_dropped_unfired"] == []
    # How late it was travels on the turn's own record too, so a reader of the
    # spawn line does not have to join it against the survey.
    assert shell.spawns[0].late_ms >= 60_000
    records = [json.loads(line)["record"] for line in shell.ledger_path.read_text(encoding="utf-8").splitlines()]
    assert records == ["overdue_wake", "spawn"]


@pytest.mark.asyncio
async def test_fire_missed_does_not_rescue_an_im_job(tmp_path):
    """The rescue must respect the channel allow-list, or it becomes a way to
    forward a stale wake to a real person's IM."""
    store = _store(tmp_path, [_job("j1", channel="feishu", due_ms=int(time.time() * 1000) - 60_000)])
    spawner = _RecordingSpawner()

    shell = WakeShell(store, spawner=spawner, fire_missed=True)
    await _drain(shell)

    assert spawner.jobs == []
    assert shell.status()["wakes_fired_late"] == []
    dropped = shell.status()["wakes_dropped_unfired"]
    assert len(dropped) == 1 and "allow-list" in dropped[0]["not_fired_reason"]


@pytest.mark.asyncio
async def test_subprocess_turn_records_a_failure_to_launch(tmp_path):
    """A missing executable is a recorded spawn with an error, not an exception
    that takes the shell down."""
    turn = SubprocessTurn(executable=str(tmp_path / "does-not-exist"))

    class _J:
        id = "j1"
        name = "n"

        class payload:
            message = "hi"

    spawn = await turn(_J())

    assert spawn.error is not None
    assert spawn.returncode is None


# --- handing a wake back to the window that is watching it ---


def _owned_job(job_id: str, *, owner: str, due_ms: int, campaign: str = "beam-limit-load") -> dict:
    job = _job(job_id, channel="tui", due_ms=due_ms, name=f"ops:{campaign}:r1")
    job["payload"]["owner"] = owner
    job["payload"]["campaign"] = campaign
    return job


def _host_registry(host_store: Path, *, agent_id: str, handle: str, session_key: str) -> None:
    """The host's instance registry, beside the store it owns.

    A provisioned agent id and an instance handle are two names the host keeps
    for one instance, and only this file relates them -- ``{agent_id}`` is the
    only one of the two a command template can substitute, so the sub-agent
    never learns the other on its own.
    """
    reg = host_store.parent.parent / "subagent_instances.json"
    reg.parent.mkdir(parents=True, exist_ok=True)
    reg.write_text(
        json.dumps(
            {
                "version": 1,
                "instances": [
                    {
                        "kind": "cli",
                        "sessionKey": session_key,
                        "agent": "Raven-Oncall",
                        "handle": handle,
                        "status": "completed",
                        "agentId": agent_id,
                        "updatedAtMs": 1787711527705,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )


def _host_jobs(host_store: Path) -> list[dict]:
    if not host_store.is_file():
        return []
    return json.loads(host_store.read_text(encoding="utf-8")).get("jobs", [])


@pytest.mark.asyncio
async def test_a_wake_owned_by_a_host_instance_is_handed_back_not_answered_here(tmp_path):
    """The gap this closes, measured 2026-08-25 through ``/new-instance``.

    Hosted as a sub-agent, this shell still fired every wake correctly -- and the
    operator saw only the first round, because the turn it spawns writes to a
    pipe this shell reads and drops. The round happened; nobody was told. So the
    assertion is in two halves and both matter: the wake must reach the host's
    store addressed to the instance, and it must NOT also be answered here, or
    the campaign gets two turns for one wake.
    """
    host_store = tmp_path / "host" / "cron" / "jobs.json"
    _host_registry(host_store, agent_id="inst-7", handle="raven-oncall-ca43bc",
                   session_key="tui:20260826_102911_ff4d5f")
    store = _store(tmp_path, [_owned_job("j1", owner="cli:inst-7", due_ms=int(time.time() * 1000) + 200)])
    spawner = _RecordingSpawner()

    await _drain(
        WakeShell(store, spawner=spawner, notify_store=host_store, dispatch_agent="Raven-Oncall")
    )

    assert spawner.jobs == [], "a dispatched wake must not also run in a child process here"
    handed = _host_jobs(host_store)
    assert len(handed) == 1
    assert handed[0]["payload"]["directAgent"] == "Raven-Oncall"
    # The handle, not the agent id the owner carries: those are different names.
    assert handed[0]["payload"]["directHandle"] == "raven-oncall-ca43bc"
    # channel + to rebuild the host's OWN session key, which is the session the
    # instance lives under -- "tui:default" would name a lane no pane subscribes to.
    assert handed[0]["payload"]["channel"] == "tui"
    assert handed[0]["payload"]["to"] == "20260826_102911_ff4d5f"


@pytest.mark.asyncio
async def test_the_handed_over_message_is_passed_through_untouched(tmp_path):
    """A wake turn starts from an empty history, so the message is the whole of
    what it gets. A shell that edited it on the way would be deciding something."""
    host_store = tmp_path / "host" / "cron" / "jobs.json"
    _host_registry(host_store, agent_id="inst-7", handle="raven-oncall-ca43bc",
                   session_key="tui:20260826_102911_ff4d5f")
    job = _owned_job("j1", owner="cli:inst-7", due_ms=int(time.time() * 1000) + 200)
    job["payload"]["message"] = "[Ops campaign 'beam-limit-load' round 2 due] Call ops_tune_status(...)"
    store = _store(tmp_path, [job])

    await _drain(
        WakeShell(store, spawner=_RecordingSpawner(), notify_store=host_store, dispatch_agent="Raven-Oncall")
    )

    assert _host_jobs(host_store)[0]["payload"]["message"] == job["payload"]["message"]


@pytest.mark.asyncio
async def test_without_a_dispatch_agent_the_wake_is_answered_here_as_before(tmp_path):
    """The headless hosting this shell was written for. Nobody is watching, so a
    turn in a child process is the best available outcome -- and it is what the
    cold-start benchmark measures, so enabling the hand-off must be the explicit
    act rather than the default."""
    host_store = tmp_path / "host" / "cron" / "jobs.json"
    store = _store(tmp_path, [_owned_job("j1", owner="cli:inst-7", due_ms=int(time.time() * 1000) + 200)])
    spawner = _RecordingSpawner()

    await _drain(WakeShell(store, spawner=spawner, notify_store=host_store))

    assert [j.id for j in spawner.jobs] == ["j1"]
    assert _host_jobs(host_store) == []


@pytest.mark.asyncio
async def test_a_wake_no_window_owns_is_answered_here_even_in_dispatch_mode(tmp_path):
    """An owner that is not a host-minted session names no instance to hand the
    wake to. Dropping it would be the one outcome worse than answering it in a
    child process: the campaign would stop being watched at all."""
    host_store = tmp_path / "host" / "cron" / "jobs.json"
    job = _owned_job("j1", owner="", due_ms=int(time.time() * 1000) + 200)
    store = _store(tmp_path, [job])
    spawner = _RecordingSpawner()

    await _drain(
        WakeShell(store, spawner=spawner, notify_store=host_store, dispatch_agent="Raven-Oncall")
    )

    assert [j.id for j in spawner.jobs] == ["j1"]
    assert _host_jobs(host_store) == []


@pytest.mark.asyncio
async def test_a_dispatched_wake_is_recorded_in_the_ledger(tmp_path):
    """Nothing downstream can tell "the wake fired and was handed over" from "the
    wake never fired" -- this shell's spawn ledger holds no turn for a dispatched
    wake, and that absence must not read as a wake that never went off."""
    host_store = tmp_path / "host" / "cron" / "jobs.json"
    _host_registry(host_store, agent_id="inst-7", handle="raven-oncall-ca43bc",
                   session_key="tui:20260826_102911_ff4d5f")
    store = _store(tmp_path, [_owned_job("j1", owner="cli:inst-7", due_ms=int(time.time() * 1000) + 200)])
    shell = WakeShell(store, spawner=_RecordingSpawner(), notify_store=host_store, dispatch_agent="Raven-Oncall")

    await _drain(shell)

    rows = [json.loads(line) for line in shell.ledger_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    dispatched = [r for r in rows if r.get("record") == "dispatch"]
    assert len(dispatched) == 1
    assert dispatched[0]["handle"] == "raven-oncall-ca43bc"
    assert dispatched[0]["session_key"] == "tui:20260826_102911_ff4d5f"
    assert dispatched[0]["agent"] == "Raven-Oncall"


@pytest.mark.asyncio
async def test_an_instance_the_host_has_no_row_for_is_answered_here(tmp_path):
    """A handle this shell cannot resolve is not a handle.

    The owner names a provisioned agent id, and only the host's registry says
    which instance that is. No row -- an instance the operator closed, a registry
    written by a different host -- and addressing it would queue a turn against
    something that may not exist. Answering it here is the outcome that keeps the
    campaign watched.
    """
    host_store = tmp_path / "host" / "cron" / "jobs.json"
    _host_registry(host_store, agent_id="somebody-else", handle="raven-oncall-zzz",
                   session_key="tui:20260826_102911_ff4d5f")
    store = _store(tmp_path, [_owned_job("j1", owner="cli:inst-7", due_ms=int(time.time() * 1000) + 200)])
    spawner = _RecordingSpawner()

    await _drain(
        WakeShell(store, spawner=spawner, notify_store=host_store, dispatch_agent="Raven-Oncall")
    )

    assert [j.id for j in spawner.jobs] == ["j1"]
    assert _host_jobs(host_store) == []


@pytest.mark.asyncio
async def test_handing_a_wake_over_does_not_arm_a_second_driver(tmp_path, monkeypatch):
    """The re-arm must not judge a turn that has not started.

    ``_keep_ops_campaign_watched`` asks "did the turn schedule its next look?".
    For a handed-off wake the answer at that instant is necessarily no -- the turn
    is queued for another process -- so the hook would arm a wake of its own and
    the campaign would have two drivers.

    Measured 2026-08-26 on the first live dispatch: the hand-off logged
    "turn ended with no campaign action and no next wake" onto the campaign's
    trail, and the wake it armed survived until the real turn's own wake replaced
    it 90 seconds later. A slower turn and both would have run.
    """
    from raven.config import paths as config_paths

    ops_home = tmp_path / "ops"
    cdir = ops_home / "beam-limit-load"
    cdir.mkdir(parents=True, exist_ok=True)
    (cdir / "meta.json").write_text(json.dumps({"backend": "process"}), encoding="utf-8")
    monkeypatch.setattr(config_paths, "get_ops_home", lambda: ops_home)

    host_store = tmp_path / "host" / "cron" / "jobs.json"
    _host_registry(host_store, agent_id="inst-7", handle="raven-oncall-ca43bc",
                   session_key="tui:20260826_102911_ff4d5f")
    store = _store(tmp_path, [_owned_job("j1", owner="cli:inst-7", due_ms=int(time.time() * 1000) + 200)])

    await _drain(
        WakeShell(store, spawner=_RecordingSpawner(), notify_store=host_store, dispatch_agent="Raven-Oncall")
    )

    events_path = cdir / "events.jsonl"
    kinds = []
    if events_path.exists():
        kinds = [json.loads(line).get("kind")
                 for line in events_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert "wake_rearmed" not in kinds, "a handed-off wake has no turn to have ignored anything yet"


# --- the net under a wake somebody else ran ---


def _live_campaign(monkeypatch, tmp_path: Path, name: str = "beam-limit-load") -> Path:
    from raven.config import paths as config_paths

    home = tmp_path / "ops"
    cdir = home / name
    cdir.mkdir(parents=True, exist_ok=True)
    (cdir / "meta.json").write_text(json.dumps({"backend": "process"}), encoding="utf-8")
    monkeypatch.setattr(config_paths, "get_ops_home", lambda: home)
    return cdir


def _kinds(cdir: Path) -> list[str]:
    path = cdir / "events.jsonl"
    if not path.exists():
        return []
    return [json.loads(line).get("kind")
            for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


@pytest.mark.asyncio
async def test_a_dispatched_turn_that_scheduled_nothing_is_caught_by_the_sweep(tmp_path, monkeypatch):
    """The gap dispatch opened, measured 2026-08-26.

    ``_execute_job``'s re-arm asks "did the turn schedule its next look?" the
    instant the wake is handed over -- before the turn starts, when the answer is
    necessarily no. Answering it there would arm a second driver, so the hand-off
    suppresses it (see HANDED_OFF); answering it nowhere is what actually
    happened: a dispatched turn reported four trials' results in prose, called no
    ops tool, and the campaign stopped for good.

    So the question is asked again once the turn has had time to run. This is the
    whole of the net for the dispatch hosting, and it has to fire on a turn that
    simply said nothing -- not only on one that ended in a recognised way.
    """
    cdir = _live_campaign(monkeypatch, tmp_path)
    host_store = tmp_path / "host" / "cron" / "jobs.json"
    _host_registry(host_store, agent_id="inst-7", handle="raven-oncall-ca43bc",
                   session_key="tui:20260826_145054_82abce")
    store = _store(tmp_path, [_owned_job("j1", owner="cli:inst-7", due_ms=int(time.time() * 1000) + 200)])
    shell = WakeShell(store, spawner=_RecordingSpawner(), notify_store=host_store,
                      dispatch_agent="Raven-Oncall")

    await _drain(shell)
    assert "wake_rearmed" not in _kinds(cdir), "not while the turn could still be working"

    # The host ran it and cleared it away, and the turn left nothing behind.
    assert len(shell._rearm_due) == 1, "the hand-off must leave a receipt to watch"
    host_store.write_text(json.dumps({"version": 1, "jobs": []}), encoding="utf-8")
    shell._sweep_dispatched()

    assert "wake_rearmed" in _kinds(cdir)
    svc_jobs = json.loads(store.read_text(encoding="utf-8"))["jobs"]
    assert [j["name"] for j in svc_jobs if j["enabled"]] == ["ops:beam-limit-load:rearm1"]


@pytest.mark.asyncio
async def test_the_sweep_leaves_a_turn_that_scheduled_its_own_next_look_alone(tmp_path, monkeypatch):
    """The other half, and the one that decides whether this is safe to run at
    all: the ordinary case is a turn that DID schedule, and re-arming over it
    would replace the loop's own ETA with this shell's guess on every round."""
    cdir = _live_campaign(monkeypatch, tmp_path)
    host_store = tmp_path / "host" / "cron" / "jobs.json"
    _host_registry(host_store, agent_id="inst-7", handle="raven-oncall-ca43bc",
                   session_key="tui:20260826_145054_82abce")
    store = _store(tmp_path, [_owned_job("j1", owner="cli:inst-7", due_ms=int(time.time() * 1000) + 200)])
    shell = WakeShell(store, spawner=_RecordingSpawner(), notify_store=host_store,
                      dispatch_agent="Raven-Oncall")

    await _drain(shell)

    # What the turn does, in the process that actually runs it.
    from raven.proactive_engine.schedulers.cron.service import CronService
    from raven.proactive_engine.schedulers.cron.types import CronSchedule

    child = CronService(store, allowed_channels={"tui"})
    child.add_job(
        campaign="beam-limit-load",
        name="ops:beam-limit-load:r2",
        schedule=CronSchedule(kind="at", at_ms=child._now_ms() + 300_000),
        message="round 1 due",
        channel="tui",
        to="direct",
        delete_after_run=True,
        dedup=False,
    )

    host_store.write_text(json.dumps({"version": 1, "jobs": []}), encoding="utf-8")
    shell._sweep_dispatched()

    assert "wake_rearmed" not in _kinds(cdir)
    svc_jobs = json.loads(store.read_text(encoding="utf-8"))["jobs"]
    assert [j["name"] for j in svc_jobs if j["enabled"]] == ["ops:beam-limit-load:r2"]


@pytest.mark.asyncio
async def test_a_wake_answered_here_is_not_swept(tmp_path, monkeypatch):
    """Headless is untouched: that turn was already judged when its child exited,
    which is the right moment for that hosting. Sweeping it too would ask twice."""
    cdir = _live_campaign(monkeypatch, tmp_path)
    store = _store(tmp_path, [_owned_job("j1", owner="cli:inst-7", due_ms=int(time.time() * 1000) + 200)])
    shell = WakeShell(store, spawner=_RecordingSpawner())

    await _drain(shell)

    assert shell._rearm_due == []
    before = _kinds(cdir)
    shell._sweep_dispatched()
    assert _kinds(cdir) == before


@pytest.mark.asyncio
async def test_a_wake_still_queued_on_the_host_is_not_rearmed(tmp_path, monkeypatch):
    """Queued is not unwatched, and the difference is the whole reason this keys
    on the receipt rather than on a timer.

    With no window open the handed-over wake simply waits in the host's store --
    it fires the moment one opens. A timer would call that a stalled campaign and
    arm a second wake in the sub-agent's store, leaving one campaign with a
    pending wake in each of two stores and two turns coming for it.
    """
    cdir = _live_campaign(monkeypatch, tmp_path)
    host_store = tmp_path / "host" / "cron" / "jobs.json"
    _host_registry(host_store, agent_id="inst-7", handle="raven-oncall-ca43bc",
                   session_key="tui:20260826_145054_82abce")
    store = _store(tmp_path, [_owned_job("j1", owner="cli:inst-7", due_ms=int(time.time() * 1000) + 200)])
    shell = WakeShell(store, spawner=_RecordingSpawner(), notify_store=host_store,
                      dispatch_agent="Raven-Oncall")

    await _drain(shell)

    # Untouched: nobody has opened a window, so the wake is still sitting there.
    assert len(_host_jobs(host_store)) == 1
    shell._sweep_dispatched()

    assert "wake_rearmed" not in _kinds(cdir)
    assert len(shell._rearm_due) == 1, "still watching, not given up on"


@pytest.mark.asyncio
async def test_an_unreadable_host_store_is_no_conclusion(tmp_path, monkeypatch):
    """A read error must not read as "every dispatched turn finished". That would
    re-arm the lot of them over turns that may still be running."""
    cdir = _live_campaign(monkeypatch, tmp_path)
    host_store = tmp_path / "host" / "cron" / "jobs.json"
    _host_registry(host_store, agent_id="inst-7", handle="raven-oncall-ca43bc",
                   session_key="tui:20260826_145054_82abce")
    store = _store(tmp_path, [_owned_job("j1", owner="cli:inst-7", due_ms=int(time.time() * 1000) + 200)])
    shell = WakeShell(store, spawner=_RecordingSpawner(), notify_store=host_store,
                      dispatch_agent="Raven-Oncall")

    await _drain(shell)
    host_store.write_text("{ this is not json", encoding="utf-8")

    shell._sweep_dispatched()

    assert "wake_rearmed" not in _kinds(cdir)
    assert len(shell._rearm_due) == 1

    # The backstop is what keeps that from being forever.
    shell._rearm_due = [(0.0, hid, j) for _, hid, j in shell._rearm_due]
    shell._sweep_dispatched()
    assert "wake_rearmed" in _kinds(cdir)
