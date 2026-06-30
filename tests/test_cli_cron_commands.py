"""Tests for the ``raven cron`` CLI subapp."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import typer
from typer.testing import CliRunner

from raven.cli.cron_commands import cron_app
from raven.proactive_engine.schedulers.cron.service import CronService
from raven.proactive_engine.schedulers.cron.types import CronSchedule


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture
def fake_cron_dir(tmp_path: Path, monkeypatch) -> Path:
    """Redirect ~/.raven/cron/ to a tmp dir for the test."""
    cron_dir = tmp_path / "cron"
    cron_dir.mkdir(parents=True)
    monkeypatch.setattr(
        "raven.cli.cron_commands.get_cron_dir",
        lambda: cron_dir,
    )
    return cron_dir


@pytest.fixture
def populated_cron(fake_cron_dir: Path) -> CronService:
    """A CronService with two jobs pre-loaded."""
    svc = CronService(fake_cron_dir / "jobs.json")
    svc.add_job(
        name="morning meds",
        schedule=CronSchedule(kind="cron", expr="0 9 * * *", tz="Asia/Shanghai"),
        message="妈妈吃药提醒：早晨",
        deliver=True,
        channel="cli",
        to="direct",
    )
    svc.add_job(
        name="lunch break",
        schedule=CronSchedule(kind="every", every_ms=3600 * 1000),
        message="水分提醒",
        deliver=True,
        channel="feishu",
        to="ou_xxx",
    )
    return svc


# ── list ─────────────────────────────────────────────────────────────


def test_list_empty(runner, fake_cron_dir):
    result = runner.invoke(cron_app, ["list"])
    assert result.exit_code == 0
    assert "0 jobs" in result.stdout
    assert "no jobs to list" in result.stdout


def test_list_shows_jobs(runner, populated_cron):
    result = runner.invoke(cron_app, ["list"])
    assert result.exit_code == 0
    # Rich Table may line-wrap "morning meds" → "morning" + "meds" on
    # separate cell rows. Job IDs don't wrap, so match by ID instead.
    for j in populated_cron.list_jobs():
        assert j.id in result.stdout
    assert "2 jobs" in result.stdout


def test_list_hides_disabled_by_default(runner, populated_cron):
    jobs = populated_cron.list_jobs()
    disabled_id = jobs[0].id
    populated_cron.enable_job(disabled_id, enabled=False)

    result = runner.invoke(cron_app, ["list"])
    assert result.exit_code == 0
    assert "2 jobs" in result.stdout
    assert "1 enabled, 1 disabled" in result.stdout
    # Disabled job should NOT appear in body (banner counts but doesn't list)
    assert disabled_id not in result.stdout
    enabled_id = [j.id for j in populated_cron.list_jobs(include_disabled=True) if j.enabled][0]
    assert enabled_id in result.stdout


def test_list_all_includes_disabled(runner, populated_cron):
    jobs = populated_cron.list_jobs()
    populated_cron.enable_job(jobs[0].id, enabled=False)

    result = runner.invoke(cron_app, ["list", "--all"])
    assert result.exit_code == 0
    for j in populated_cron.list_jobs(include_disabled=True):
        assert j.id in result.stdout


# ── get ──────────────────────────────────────────────────────────────


def test_get_full_detail(runner, populated_cron):
    job = populated_cron.list_jobs()[0]
    result = runner.invoke(cron_app, ["get", job.id])
    assert result.exit_code == 0
    assert job.id in result.stdout
    assert job.name in result.stdout
    assert job.payload.message[:30] in result.stdout


def test_get_unknown_id_errors(runner, fake_cron_dir):
    result = runner.invoke(cron_app, ["get", "nonexistent"])
    assert result.exit_code == 1
    assert "No job matching" in result.stdout


def test_get_shows_topic_tag(runner, fake_cron_dir):
    """``cron get`` renders the payload's topic_tag — the dedup key the
    LLM sets when scheduling. Operators need it visible to debug
    "why didn't my reminder re-create" (dedup hit on existing tag)."""
    svc = CronService(fake_cron_dir / "jobs.json")
    job = svc.add_job(
        name="meds",
        schedule=CronSchedule(kind="cron", expr="0 9 * * *", tz="Asia/Shanghai"),
        message="吃药",
        deliver=True,
        channel="cli",
        to="direct",
        topic_tag="medication_morning",
    )
    r = runner.invoke(cron_app, ["get", job.id])
    assert r.exit_code == 0, r.output
    assert "topic_tag" in r.stdout
    assert "medication_morning" in r.stdout


def test_get_shows_topic_tag_dash_when_absent(runner, populated_cron):
    """Jobs without a topic_tag render '-' (not 'None' / blank) so the
    table layout stays predictable."""
    job = populated_cron.list_jobs()[0]
    assert job.payload.topic_tag is None  # sanity: populated_cron sets no tag
    r = runner.invoke(cron_app, ["get", job.id])
    assert r.exit_code == 0
    assert "topic_tag" in r.stdout
    # Confirm the dash placeholder is rendered for the topic_tag row,
    # not just elsewhere (other absent fields also use '-').
    topic_line = next(
        (line for line in r.stdout.splitlines() if "topic_tag" in line),
        None,
    )
    assert topic_line is not None
    assert "-" in topic_line


def test_get_prefix_match(runner, populated_cron):
    job = populated_cron.list_jobs()[0]
    short = job.id[:4]
    result = runner.invoke(cron_app, ["get", short])
    assert result.exit_code == 0
    assert job.id in result.stdout


def test_get_ambiguous_prefix(runner, fake_cron_dir):
    """Two jobs whose ids share a common prefix → ambiguous on short prefix.
    Force-deterministic by writing jobs.json with hand-crafted IDs."""
    jobs_path = fake_cron_dir / "jobs.json"
    jobs_path.write_text(
        json.dumps(
            {
                "version": 1,
                "jobs": [
                    {
                        "id": "ab12cd34",
                        "name": "a",
                        "enabled": True,
                        "schedule": {"kind": "every", "everyMs": 60000},
                        "payload": {
                            "kind": "agent_turn",
                            "message": "x",
                            "deliver": True,
                            "channel": "cli",
                            "to": "direct",
                        },
                        "state": {"nextRunAtMs": 1, "silentFireCount": 0},
                    },
                    {
                        "id": "ab56ef78",
                        "name": "b",
                        "enabled": True,
                        "schedule": {"kind": "every", "everyMs": 120000},
                        "payload": {
                            "kind": "agent_turn",
                            "message": "y",
                            "deliver": True,
                            "channel": "cli",
                            "to": "direct",
                        },
                        "state": {"nextRunAtMs": 1, "silentFireCount": 0},
                    },
                ],
            }
        )
    )
    # Common prefix "ab" matches both
    result = runner.invoke(cron_app, ["get", "ab"])
    assert result.exit_code == 1
    assert "Ambiguous" in result.stdout
    assert "ab12cd34" in result.stdout
    assert "ab56ef78" in result.stdout


# ── delete ───────────────────────────────────────────────────────────


def test_delete_with_yes_skips_confirm(runner, populated_cron):
    job = populated_cron.list_jobs()[0]
    result = runner.invoke(cron_app, ["delete", job.id, "--yes"])
    assert result.exit_code == 0
    assert "Removed" in result.stdout
    # Verify gone
    remaining = [j.id for j in populated_cron.list_jobs(include_disabled=True)]
    assert job.id not in remaining


def test_delete_aborts_on_no_confirm(runner, populated_cron):
    job = populated_cron.list_jobs()[0]
    result = runner.invoke(cron_app, ["delete", job.id], input="n\n")
    assert result.exit_code == 1
    assert "aborted" in result.stdout
    # Verify still there
    remaining = [j.id for j in populated_cron.list_jobs(include_disabled=True)]
    assert job.id in remaining


def test_delete_unknown_id(runner, fake_cron_dir):
    result = runner.invoke(cron_app, ["delete", "nope", "--yes"])
    assert result.exit_code == 1
    assert "No job matching" in result.stdout


# ── enable / disable ─────────────────────────────────────────────────


def test_disable_then_enable(runner, populated_cron):
    job = populated_cron.list_jobs()[0]
    # Disable
    r1 = runner.invoke(cron_app, ["disable", job.id, "--yes"])
    assert r1.exit_code == 0
    assert "Disabled" in r1.stdout
    refreshed = [j for j in populated_cron.list_jobs(include_disabled=True) if j.id == job.id][0]
    assert refreshed.enabled is False

    # Enable
    r2 = runner.invoke(cron_app, ["enable", job.id])
    assert r2.exit_code == 0
    assert "Enabled" in r2.stdout
    refreshed = [j for j in populated_cron.list_jobs(include_disabled=True) if j.id == job.id][0]
    assert refreshed.enabled is True


def test_enable_already_enabled_is_noop(runner, populated_cron):
    job = populated_cron.list_jobs()[0]
    r = runner.invoke(cron_app, ["enable", job.id])
    assert r.exit_code == 0
    assert "already enabled" in r.stdout


def test_disable_already_disabled_is_noop(runner, populated_cron):
    job = populated_cron.list_jobs()[0]
    populated_cron.enable_job(job.id, enabled=False)
    r = runner.invoke(cron_app, ["disable", job.id, "--yes"])
    assert r.exit_code == 0
    assert "already disabled" in r.stdout


# ── run ──────────────────────────────────────────────────────────────


def test_run_test_fire_on_recurring_job(runner, populated_cron):
    """cron run on a recurring job: --yes skips the state-mutation
    confirm; output must use [TEST-FIRE] (not [DRY-RUN]) and explain
    that state was advanced even though delivery was stubbed."""
    job = populated_cron.list_jobs()[0]  # cron 0 9 * * * (recurring)
    r = runner.invoke(cron_app, ["run", job.id, "--yes"])
    assert r.exit_code == 0
    assert "[TEST-FIRE]" in r.stdout
    # No leftover DRY-RUN labeling that contradicted the real semantics
    assert "[DRY-RUN]" not in r.stdout
    assert job.payload.channel in r.stdout or job.payload.to in r.stdout
    # Must say message was stubbed AND state moved
    assert "delivery was stubbed" in r.stdout
    assert "next_run advanced" in r.stdout


def test_run_warns_about_state_mutation_for_recurring(runner, populated_cron):
    """The pre-confirm warning must mention next_run/last_run advance —
    i.e. user can't claim they were misled into thinking it's a true
    dry-run."""
    job = populated_cron.list_jobs()[0]  # cron 0 9 * * *
    r = runner.invoke(cron_app, ["run", job.id, "--yes"])
    assert r.exit_code == 0
    assert "advance next_run_at" in r.stdout


def test_run_warns_when_active_claim_present(
    runner,
    fake_cron_dir,
    monkeypatch,
):
    """If another process holds a recent claim (within the 60s heartbeat
    window — distinct from CronService's 30min stale-claim TTL), the CLI
    surfaces a 'Possible gateway activity' warning so user knows the
    fcntl race is non-trivial."""
    from time import time as _time

    from raven.proactive_engine.schedulers.cron.types import CronSchedule as _Sched

    svc = CronService(fake_cron_dir / "jobs.json")
    j = svc.add_job(
        name="x",
        schedule=_Sched(kind="every", every_ms=60_000),
        message="m",
        deliver=True,
        channel="cli",
        to="direct",
    )
    # Hand-poke a fresh claim by another pid (simulate gateway running).
    jobs_path = fake_cron_dir / "jobs.json"
    data = json.loads(jobs_path.read_text())
    data["jobs"][0]["state"]["claimedByPid"] = 99999
    data["jobs"][0]["state"]["claimedAtMs"] = int(_time() * 1000) - 5_000  # 5s ago
    jobs_path.write_text(json.dumps(data))

    # Decline the state-mutation confirm so we just inspect the warning
    # without actually firing.
    r = runner.invoke(cron_app, ["run", j.id], input="n\n")
    # Confirm aborted (exit 1) but warning was printed before the prompt
    assert r.exit_code == 1
    assert "active claim" in r.stdout
    assert "Possible gateway activity" in r.stdout


def test_run_one_shot_at_with_delete_warns_about_removal(
    runner,
    fake_cron_dir,
):
    """For an at+delete_after_run=True job (the default for one-shots),
    the warning must use the word REMOVE so user is aware the reminder
    will vanish."""
    from raven.proactive_engine.schedulers.cron.types import CronSchedule as _Sched

    svc = CronService(fake_cron_dir / "jobs.json")
    j = svc.add_job(
        name="future thing",
        schedule=_Sched(kind="at", at_ms=2_000_000_000_000),  # year 2033
        message="x",
        deliver=True,
        channel="cli",
        to="direct",
        delete_after_run=True,  # this is the default for at-kind
    )
    # Decline the confirm so we just inspect the warning text without
    # actually deleting the job.
    r = runner.invoke(cron_app, ["run", j.id], input="n\n")
    assert r.exit_code == 1
    assert "REMOVE" in r.stdout
    assert "delete_after_run=True" in r.stdout
    # And it actually didn't run (still in store)
    assert j.id in {x.id for x in svc.list_jobs(include_disabled=True)}


def test_run_one_shot_at_without_delete_warns_about_disable(
    runner,
    fake_cron_dir,
):
    from raven.proactive_engine.schedulers.cron.types import CronSchedule as _Sched

    svc = CronService(fake_cron_dir / "jobs.json")
    j = svc.add_job(
        name="future demo",
        schedule=_Sched(kind="at", at_ms=2_000_000_000_000),
        message="x",
        deliver=True,
        channel="cli",
        to="direct",
        delete_after_run=False,
    )
    r = runner.invoke(cron_app, ["run", j.id], input="n\n")
    assert r.exit_code == 1
    assert "DISABLE" in r.stdout


def test_run_aborts_on_no_confirm(runner, populated_cron):
    """If user says 'n' to the state-mutation confirm, no state changes."""
    job = populated_cron.list_jobs()[0]
    before_next = job.state.next_run_at_ms
    r = runner.invoke(cron_app, ["run", job.id], input="n\n")
    assert r.exit_code == 1
    assert "aborted" in r.stdout
    # State unchanged
    refreshed = next(j for j in populated_cron.list_jobs(include_disabled=True) if j.id == job.id)
    assert refreshed.state.next_run_at_ms == before_next
    assert refreshed.state.last_run_at_ms is None


def test_run_disabled_without_force_errors(runner, populated_cron):
    job = populated_cron.list_jobs()[0]
    populated_cron.enable_job(job.id, enabled=False)
    r = runner.invoke(cron_app, ["run", job.id])
    assert r.exit_code == 1
    assert "disabled" in r.stdout
    assert "--force" in r.stdout


def test_run_disabled_with_force_succeeds(runner, populated_cron):
    job = populated_cron.list_jobs()[0]
    populated_cron.enable_job(job.id, enabled=False)
    r = runner.invoke(cron_app, ["run", job.id, "--force", "--yes"])
    assert r.exit_code == 0
    assert "[TEST-FIRE]" in r.stdout


def test_run_unknown_id(runner, fake_cron_dir):
    r = runner.invoke(cron_app, ["run", "nope"])
    assert r.exit_code == 1
    assert "No job matching" in r.stdout


# ── add ──────────────────────────────────────────────────────────────


def test_add_requires_exactly_one_schedule(runner, fake_cron_dir, monkeypatch):
    # Patch load_raven_config so it doesn't try to read the user's
    # real config during the test

    # No schedule → error
    r = runner.invoke(
        cron_app,
        [
            "add",
            "--name",
            "x",
            "--message",
            "y",
            "--channel",
            "feishu",
            "--to",
            "ou_x",
        ],
    )
    assert r.exit_code == 2
    assert "exactly one schedule flag" in r.stdout

    # Two schedules → error
    r = runner.invoke(
        cron_app,
        [
            "add",
            "--name",
            "x",
            "--message",
            "y",
            "--channel",
            "feishu",
            "--to",
            "ou_x",
            "--cron",
            "0 9 * * *",
            "--every",
            "1m",
        ],
    )
    assert r.exit_code == 2


def test_add_cron(runner, fake_cron_dir, monkeypatch):
    r = runner.invoke(
        cron_app,
        [
            "add",
            "--name",
            "morning",
            "--cron",
            "0 9 * * *",
            "--message",
            "wake up",
            "--channel",
            "feishu",
            "--to",
            "ou_xxx",
        ],
    )
    assert r.exit_code == 0
    assert "Created job" in r.stdout
    # Job in store
    svc = CronService(fake_cron_dir / "jobs.json")
    jobs = svc.list_jobs()
    assert len(jobs) == 1
    assert jobs[0].schedule.kind == "cron"
    assert jobs[0].schedule.expr == "0 9 * * *"


def test_add_at(runner, fake_cron_dir, monkeypatch):
    r = runner.invoke(
        cron_app,
        [
            "add",
            "--name",
            "demo",
            "--at",
            "2099-01-01T08:00:00",
            "--message",
            "future thing",
            "--channel",
            "feishu",
            "--to",
            "ou_xxx",
        ],
    )
    assert r.exit_code == 0
    svc = CronService(fake_cron_dir / "jobs.json")
    assert svc.list_jobs()[0].schedule.kind == "at"


def test_add_every(runner, fake_cron_dir, monkeypatch):
    r = runner.invoke(
        cron_app,
        [
            "add",
            "--name",
            "tick",
            "--every",
            "30m",
            "--message",
            "ping",
            "--channel",
            "feishu",
            "--to",
            "ou_xxx",
        ],
    )
    assert r.exit_code == 0
    svc = CronService(fake_cron_dir / "jobs.json")
    job = svc.list_jobs()[0]
    assert job.schedule.kind == "every"
    assert job.schedule.every_ms == 30 * 60 * 1000


def test_add_invalid_at_format(runner, fake_cron_dir, monkeypatch):
    r = runner.invoke(
        cron_app,
        [
            "add",
            "--name",
            "x",
            "--at",
            "not-iso",
            "--message",
            "y",
            "--channel",
            "feishu",
            "--to",
            "ou_xxx",
        ],
    )
    assert r.exit_code == 2
    assert "Invalid ISO datetime" in r.stdout


def test_add_invalid_cron(runner, fake_cron_dir, monkeypatch):
    """Garbage cron expressions are rejected at CLI boundary instead
    of silently creating a job that never fires (croniter exception
    inside _compute_next_run is caught + next_run_at_ms stays None).
    """
    r = runner.invoke(
        cron_app,
        [
            "add",
            "--name",
            "x",
            "--cron",
            "garbage not a cron expr",
            "--message",
            "y",
            "--channel",
            "feishu",
            "--to",
            "ou_xxx",
        ],
    )
    assert r.exit_code == 2
    assert "Invalid cron expression" in r.stdout
    # And no job was created (validation must fail before add_job)
    svc = CronService(fake_cron_dir / "jobs.json")
    assert svc.list_jobs() == []


def test_add_past_at_rejected(runner, fake_cron_dir, monkeypatch):
    """A past ``--at`` is rejected (the service raises) rather than creating a
    job that silently never fires — the CLI surfaces the service error."""
    r = runner.invoke(
        cron_app,
        [
            "add",
            "--name",
            "x",
            "--at",
            "2000-01-01T00:00:00",
            "--message",
            "y",
            "--channel",
            "feishu",
            "--to",
            "ou_xxx",
        ],
    )
    assert r.exit_code == 2
    assert "at time is in the past" in r.stdout
    svc = CronService(fake_cron_dir / "jobs.json")
    assert svc.list_jobs() == []


def test_add_invalid_tz(runner, fake_cron_dir, monkeypatch):
    r = runner.invoke(
        cron_app,
        [
            "add",
            "--name",
            "x",
            "--cron",
            "0 9 * * *",
            "--tz",
            "Mars/Olympus",
            "--message",
            "y",
            "--channel",
            "feishu",
            "--to",
            "ou_xxx",
        ],
    )
    assert r.exit_code == 2
    assert "Unknown timezone" in r.stdout


# ── _parse_duration ─────────────────────────────────────────────────


from raven.cli.cron_commands import _parse_duration


@pytest.mark.parametrize(
    "value, expected",
    [
        ("30s", 30),
        ("90s", 90),
        ("5m", 300),
        ("1h", 3600),
        ("1h30m", 5400),
        ("2h15m30s", 8130),
        ("60s", 60),
        ("7d", 7 * 86400),
        ("1d12h", 86400 + 12 * 3600),
        ("1d", 86400),
    ],
)
def test_parse_duration_accepts(value, expected):
    assert _parse_duration(value) == expected


@pytest.mark.parametrize(
    "value, hint_substr",
    [
        ("500ms", "seconds (s)"),
        ("100us", "seconds (s)"),
        ("1.5h", "integer"),
        ("30", "unit suffix"),
        ("0s", "positive"),
        ("", "empty"),
        ("abc", "invalid"),
        ("5x", "invalid"),
        # Length-variable units (weeks/months/years) belong under --cron.
        # The regex doesn't match these suffixes, so they surface as "invalid".
        ("1w", "invalid"),
        ("1mo", "invalid"),
        ("1y", "invalid"),
    ],
)
def test_parse_duration_rejects(value, hint_substr):
    with pytest.raises(typer.BadParameter) as exc_info:
        _parse_duration(value)
    assert hint_substr in str(exc_info.value)


# ── cron config sub-typer ─────────────────────────────────────────────


@pytest.fixture
def isolated_config(tmp_path: Path, monkeypatch) -> Path:
    """Redirect ``get_config_path`` to a tmp file so ``cron config set/reset``
    writes to an isolated location. Returns the config path (may not exist
    yet — ``load_config`` happily synthesizes defaults from absent files)."""
    config_path = tmp_path / "config.json"
    monkeypatch.setattr(
        "raven.config.loader.get_config_path",
        lambda: config_path,
    )
    monkeypatch.setattr(
        "raven.config.update.get_config_path",
        lambda: config_path,
    )
    return config_path


def test_config_get_all_shows_defaults(runner, isolated_config):
    """No flags → ``cron config get`` lists schema defaults as a table."""
    r = runner.invoke(cron_app, ["config", "get"])
    assert r.exit_code == 0, r.output
    assert "forward_channels" in r.stdout
    assert "*" in r.stdout
    assert "default_timezone" in r.stdout
    assert "Asia/Shanghai" in r.stdout


def test_config_get_single_flag(runner, isolated_config):
    """``--forward-channels`` flag prints just that key's value."""
    r = runner.invoke(cron_app, ["config", "get", "--forward-channels"])
    assert r.exit_code == 0, r.output
    assert "*" in r.stdout
    # Single-flag mode: no table header → no 'default_timezone' row leaks in
    assert "Asia/Shanghai" not in r.stdout


def test_config_get_both_flags(runner, isolated_config):
    """Multiple flags → one value per line, no table."""
    r = runner.invoke(
        cron_app,
        ["config", "get", "--forward-channels", "--default-timezone"],
    )
    assert r.exit_code == 0, r.output
    lines = [ln.strip() for ln in r.stdout.splitlines() if ln.strip()]
    assert lines == ["*", "Asia/Shanghai"]


def test_config_set_forward_channels_star(runner, isolated_config):
    r = runner.invoke(
        cron_app,
        ["config", "set", "--forward-channels", "*"],
    )
    assert r.exit_code == 0, r.output
    data = json.loads(isolated_config.read_text())
    assert data["cron"]["forwardChannels"] == ["*"]


def test_config_set_forward_channels_csv(runner, isolated_config):
    r = runner.invoke(
        cron_app,
        ["config", "set", "--forward-channels", "telegram,feishu"],
    )
    assert r.exit_code == 0, r.output
    data = json.loads(isolated_config.read_text())
    assert data["cron"]["forwardChannels"] == ["telegram", "feishu"]


def test_config_set_forward_channels_none(runner, isolated_config):
    """``none`` (sentinel) → empty list = no broadcast on next cron fire."""
    r = runner.invoke(
        cron_app,
        ["config", "set", "--forward-channels", "none"],
    )
    assert r.exit_code == 0, r.output
    data = json.loads(isolated_config.read_text())
    assert data["cron"]["forwardChannels"] == []


def test_config_set_default_timezone_valid(runner, isolated_config):
    r = runner.invoke(
        cron_app,
        ["config", "set", "--default-timezone", "America/New_York"],
    )
    assert r.exit_code == 0, r.output
    data = json.loads(isolated_config.read_text())
    assert data["cron"]["defaultTimezone"] == "America/New_York"


def test_config_set_default_timezone_invalid(runner, isolated_config):
    r = runner.invoke(
        cron_app,
        ["config", "set", "--default-timezone", "Mars/Olympus"],
    )
    assert r.exit_code == 1
    assert "Invalid value" in r.stdout or "unknown timezone" in r.stdout
    # File must not contain a half-written value
    assert not isolated_config.exists() or "defaultTimezone" not in (
        isolated_config.read_text() if isolated_config.exists() else ""
    )


def test_config_set_no_flags_errors(runner, isolated_config):
    """``cron config set`` with no flag → rc=1 + no write."""
    r = runner.invoke(cron_app, ["config", "set"])
    assert r.exit_code == 1
    assert "at least one flag" in r.stdout
    assert not isolated_config.exists()


def test_config_set_multiple_flags_one_call(runner, isolated_config):
    """Setting two keys in one invocation patches both atomically (well,
    serially; each write is atomic, and parse failures abort before any
    write so a single bad value never corrupts state)."""
    r = runner.invoke(
        cron_app,
        [
            "config",
            "set",
            "--forward-channels",
            "telegram",
            "--default-timezone",
            "UTC",
        ],
    )
    assert r.exit_code == 0, r.output
    data = json.loads(isolated_config.read_text())
    assert data["cron"]["forwardChannels"] == ["telegram"]
    assert data["cron"]["defaultTimezone"] == "UTC"


def test_config_set_multiple_flags_invalid_one_aborts_all(
    runner,
    isolated_config,
):
    """If any flag's value fails validation, NO key is written — pre-parse
    pass guarantees we never half-write."""
    r = runner.invoke(
        cron_app,
        [
            "config",
            "set",
            "--forward-channels",
            "telegram",  # valid
            "--default-timezone",
            "Mars/Olympus",  # invalid
        ],
    )
    assert r.exit_code == 1
    assert "Invalid value" in r.stdout or "unknown timezone" in r.stdout
    # No file written: pre-parse pass aborted before any update_cron_config call
    assert not isolated_config.exists()


def test_config_reset_with_yes(runner, isolated_config):
    """Reset removes the entire cron section from disk."""
    isolated_config.write_text(
        json.dumps(
            {
                "cron": {"forwardChannels": ["telegram"], "defaultTimezone": "UTC"},
                "agents": {"defaults": {"model": "kept"}},
            }
        )
    )
    r = runner.invoke(cron_app, ["config", "reset", "--yes"])
    assert r.exit_code == 0, r.output
    data = json.loads(isolated_config.read_text())
    assert "cron" not in data
    assert data["agents"]["defaults"]["model"] == "kept"  # sibling preserved


def test_config_reset_aborts_on_no(runner, isolated_config):
    """If user declines confirm, on-disk cron section is preserved."""
    isolated_config.write_text(
        json.dumps(
            {
                "cron": {"forwardChannels": ["telegram"]},
            }
        )
    )
    r = runner.invoke(cron_app, ["config", "reset"], input="n\n")
    assert r.exit_code == 0
    assert "Aborted" in r.stdout
    data = json.loads(isolated_config.read_text())
    assert data["cron"]["forwardChannels"] == ["telegram"]


def test_config_set_then_get_round_trip(runner, isolated_config):
    """Set, then get, returns the new value (dynamic reload)."""
    runner.invoke(
        cron_app,
        ["config", "set", "--forward-channels", "telegram"],
    )
    r = runner.invoke(cron_app, ["config", "get", "--forward-channels"])
    assert r.exit_code == 0, r.output
    assert "telegram" in r.stdout
