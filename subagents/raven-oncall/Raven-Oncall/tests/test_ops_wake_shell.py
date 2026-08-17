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
        "payload": {
            "kind": "agent_turn",
            "message": message,
            "deliver": True,
            "channel": channel,
            "to": "default",
            "topicTag": None,
        },
        "state": {
            "nextRunAtMs": due_ms,
            "lastRunAtMs": None,
            "lastStatus": None,
            "lastError": None,
            "claimedByPid": None,
            "claimedAtMs": None,
            "silentFireCount": 0,
        },
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
            job_id=job.id,
            job_name=job.name,
            at_ms=int(time.time() * 1000),
            argv=["fake", "agent", "-m", job.payload.message],
            returncode=self._rc,
            duration_ms=7,
            error=self._error,
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
