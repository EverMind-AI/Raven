"""Tests for the ``raven sentinel`` CLI subapp — covers all
user-facing commands plus the ``[Internal]`` ``discover-now`` entry
used by the proactivity-eval longrun.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from typer.testing import CliRunner

from raven.cli.commands import sentinel_app
from raven.proactive_engine.sentinel.executor.pending_decision import PendingDecisionStore
from raven.proactive_engine.sentinel.predictor.routine_store import RoutineStore
from raven.proactive_engine.sentinel.types import PendingDecision, Routine, TaskOption


# Use real wall-clock for fixture timestamps so decisions are "live"
# (not is_expired) relative to whatever time the CLI is_expired check
# runs at. Decisions whose created_at_ms is hours stale would be
# silently filtered out by all_active() and the test would see "no
# live decisions" instead of the populated set.
def _now_ms() -> int:
    return int(datetime.now().timestamp() * 1000)


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture
def fake_sentinel_dir(tmp_path: Path, monkeypatch) -> Path:
    sentinel_dir = tmp_path / "sentinel"
    sentinel_dir.mkdir(parents=True)
    monkeypatch.setattr(
        "raven.config.paths.get_sentinel_dir",
        lambda: sentinel_dir,
    )
    return sentinel_dir


def _opt(oid: str = "opt_a", title: str = "task A") -> TaskOption:
    return TaskOption(
        id=oid,
        title=title,
        why="why",
        type="ad_hoc",
        exec_kind="reply",
        exec_payload={"prompt": f"do {oid}"},
        created_at_ms=_now_ms(),
    )


def _decision(
    *,
    decision_id: str = "dec_test",
    channel: str = "cli",
    to: str = "direct",
    awaiting: bool = False,
    consumed: bool = False,
    created_at_ms: int | None = None,
) -> PendingDecision:
    return PendingDecision(
        decision_id=decision_id,
        channel=channel,
        to=to,
        created_at_ms=created_at_ms if created_at_ms is not None else _now_ms(),
        ttl_min=60,
        options=[_opt("opt_a", "task A"), _opt("opt_b", "task B")],
        consumed=consumed,
        awaiting_confirm=awaiting,
        picked_option_id="opt_a" if awaiting else None,
    )


# ── decisions ────────────────────────────────────────────────────────


def test_decisions_empty(runner, fake_sentinel_dir):
    result = runner.invoke(sentinel_app, ["decisions"])
    assert result.exit_code == 0
    assert "No live decisions" in result.stdout


def test_decisions_lists_pending(runner, fake_sentinel_dir):
    store = PendingDecisionStore(fake_sentinel_dir / "pending_decisions.json")
    store.put(_decision(decision_id="dec_aa11"))
    store.put(_decision(decision_id="dec_bb22", channel="feishu", to="ou_xxx", awaiting=True))

    result = runner.invoke(sentinel_app, ["decisions"])
    assert result.exit_code == 0
    assert "dec_aa11" in result.stdout
    assert "dec_bb22" in result.stdout
    assert "pending" in result.stdout
    # Rich Table may line-wrap "awaiting_confirm" → "awaiting_co\nnfirm"
    # Match a substring that's atomic
    assert "awaiting" in result.stdout


def test_decisions_hides_consumed_by_default(runner, fake_sentinel_dir):
    store = PendingDecisionStore(fake_sentinel_dir / "pending_decisions.json")
    store.put(_decision(decision_id="dec_live"))
    consumed = _decision(decision_id="dec_done", consumed=True)
    # Hand-poke a consumed decision (put() refuses to put consumed ones
    # cleanly, so go via store internals)
    raw_state = store._store.load()
    raw_state["decisions"].append(store._decision_to_raw(consumed))
    store._store.update(lambda s: raw_state)

    r1 = runner.invoke(sentinel_app, ["decisions"])
    assert r1.exit_code == 0
    assert "dec_live" in r1.stdout
    assert "dec_done" not in r1.stdout

    r2 = runner.invoke(sentinel_app, ["decisions", "--all"])
    assert r2.exit_code == 0
    assert "dec_live" in r2.stdout
    assert "dec_done" in r2.stdout


def test_decisions_show_options(runner, fake_sentinel_dir):
    store = PendingDecisionStore(fake_sentinel_dir / "pending_decisions.json")
    store.put(_decision(decision_id="dec_xx"))
    result = runner.invoke(sentinel_app, ["decisions", "--show-options"])
    assert result.exit_code == 0
    assert "task A" in result.stdout
    assert "task B" in result.stdout


# ── routines ─────────────────────────────────────────────────────────


def test_routines_empty(runner, fake_sentinel_dir):
    result = runner.invoke(sentinel_app, ["routines"])
    assert result.exit_code == 0
    assert "No routines in store" in result.stdout


def test_routines_lists_with_status(runner, fake_sentinel_dir):
    store = RoutineStore(fake_sentinel_dir / "routines.json")
    store.merge(
        [
            Routine(id="dow1-h09-meet", pattern="x", occurrence_count=4, weight=4.0),
            Routine(id="dow6-h08-run", pattern="y", occurrence_count=3, weight=3.0),
        ],
        now_ms=_now_ms() - 1000,
    )
    store.upgrade("dow1-h09-meet", confirmed_at_ms=_now_ms())

    result = runner.invoke(sentinel_app, ["routines"])
    assert result.exit_code == 0
    assert "dow1-h09-meet" in result.stdout
    assert "dow6-h08-run" in result.stdout
    assert "active" in result.stdout
    assert "candidate" in result.stdout


def test_routines_filter_status(runner, fake_sentinel_dir):
    store = RoutineStore(fake_sentinel_dir / "routines.json")
    store.merge(
        [
            Routine(id="r-active", pattern="x", occurrence_count=4, weight=4.0),
            Routine(id="r-cand", pattern="y", occurrence_count=3, weight=3.0),
        ],
        now_ms=_now_ms() - 1000,
    )
    store.upgrade("r-active", confirmed_at_ms=_now_ms())

    r1 = runner.invoke(sentinel_app, ["routines", "--status", "active"])
    assert r1.exit_code == 0
    assert "r-active" in r1.stdout
    assert "r-cand" not in r1.stdout

    r2 = runner.invoke(sentinel_app, ["routines", "--status", "candidate"])
    assert r2.exit_code == 0
    assert "r-cand" in r2.stdout
    assert "r-active" not in r2.stdout


def test_routines_invalid_status(runner, fake_sentinel_dir):
    result = runner.invoke(sentinel_app, ["routines", "--status", "weird"])
    assert result.exit_code == 2
    assert "Unknown --status" in result.stdout


def test_routines_no_match_with_status_filter(runner, fake_sentinel_dir):
    store = RoutineStore(fake_sentinel_dir / "routines.json")
    store.merge(
        [
            Routine(id="r-only-cand", pattern="x", occurrence_count=4),
        ],
        now_ms=_now_ms(),
    )

    result = runner.invoke(sentinel_app, ["routines", "--status", "active"])
    assert result.exit_code == 0
    assert "No routines with status=active" in result.stdout


# ── discover-now (smoke; uses mock SentinelRunner) ──────────────────


def test_discover_now_aborts_when_disabled(runner, monkeypatch):
    """If sentinel.task_discovery_enabled=False, command should refuse
    to run with a clear error."""
    from raven.config.raven import SentinelConfig

    stub_sentinel_cfg = SentinelConfig(
        enabled=True,
        task_discovery_enabled=False,
    )
    stub_config = MagicMock()
    stub_config.sentinel = stub_sentinel_cfg
    stub_config.base = MagicMock()
    monkeypatch.setattr(
        "raven.cli.sentinel_commands._load_sentinel_config",
        lambda: stub_config,
    )

    result = runner.invoke(
        sentinel_app,
        ["discover-now", "feishu", "ou_xxx", "--yes"],
    )
    assert result.exit_code == 1
    assert "task_discovery_enabled is False" in result.stdout


def test_discover_now_aborts_when_sentinel_disabled(runner, monkeypatch):
    from raven.config.raven import SentinelConfig

    stub_sentinel_cfg = SentinelConfig(enabled=False)
    stub_config = MagicMock()
    stub_config.sentinel = stub_sentinel_cfg
    stub_config.base = MagicMock()
    monkeypatch.setattr(
        "raven.cli.sentinel_commands._load_sentinel_config",
        lambda: stub_config,
    )

    result = runner.invoke(
        sentinel_app,
        ["discover-now", "feishu", "ou_xxx", "--yes"],
    )
    assert result.exit_code == 1
    assert "sentinel.enabled is False" in result.stdout


def test_discover_now_aborts_on_no_confirm(runner, monkeypatch):
    """With config valid (enabled + task_discovery_enabled), should prompt
    and abort on 'n'. Config validation now happens BEFORE the confirm,
    so we need a valid stub to reach the prompt."""
    from raven.config.raven import SentinelConfig

    stub_sentinel_cfg = SentinelConfig(
        enabled=True,
        task_discovery_enabled=True,
    )
    stub_config = MagicMock()
    stub_config.sentinel = stub_sentinel_cfg
    stub_config.base = MagicMock()
    monkeypatch.setattr(
        "raven.cli.sentinel_commands._load_sentinel_config",
        lambda: stub_config,
    )

    result = runner.invoke(
        sentinel_app,
        ["discover-now", "cli", "direct"],
        input="n\n",
    )
    assert result.exit_code == 1
    assert "aborted" in result.stdout


def test_discover_now_happy_path(runner, monkeypatch):
    """Mock build_sentinel_stack to return a runner with a stub
    task_discoverer; verify discover_now is called and runner.stop()
    fires for cleanup."""

    from raven.config.raven import SentinelConfig

    # Stub config: enabled + task_discovery_enabled = True
    stub_sentinel_cfg = SentinelConfig(
        enabled=True,
        task_discovery_enabled=True,
    )
    stub_config = MagicMock()
    stub_config.sentinel = stub_sentinel_cfg
    stub_config.base = MagicMock()
    monkeypatch.setattr(
        "raven.cli.sentinel_commands._load_sentinel_config",
        lambda: stub_config,
    )
    # Stub provider
    monkeypatch.setattr(
        "raven.cli.sentinel_commands.make_provider",
        lambda config: MagicMock(),
    )

    # Stub build_sentinel_stack — return a mock runner
    discover_calls: list[tuple[str, str]] = []
    stop_called: dict[str, bool] = {"called": False}

    class _StubRunner:
        feedback = MagicMock()

        def __init__(self):
            self.task_discoverer = MagicMock()  # truthy

        async def discover_now(self, channel: str, to: str) -> None:
            discover_calls.append((channel, to))

        async def stop(self) -> None:
            stop_called["called"] = True

    monkeypatch.setattr(
        "raven.cli._proactive_stack.build_sentinel_stack",
        lambda *a, **kw: (_StubRunner(), None, None),
    )

    # --inproc routes through build_sentinel_stack → stubbed runner; the
    # default path queues a trigger file instead and never touches the stub.
    result = runner.invoke(
        sentinel_app,
        ["discover-now", "feishu", "ou_xxx", "--yes", "--inproc"],
    )
    assert result.exit_code == 0, result.stdout
    assert "discover_now invoked" in result.stdout
    assert discover_calls == [("feishu", "ou_xxx")]
    assert stop_called["called"], "runner.stop() must be called in finally block for resource cleanup"


def test_discover_now_cleans_up_runner_on_exception(runner, monkeypatch):
    """If runner.discover_now raises, runner.stop() must still be called."""
    from raven.config.raven import SentinelConfig

    stub_sentinel_cfg = SentinelConfig(
        enabled=True,
        task_discovery_enabled=True,
    )
    stub_config = MagicMock()
    stub_config.sentinel = stub_sentinel_cfg
    stub_config.base = MagicMock()
    monkeypatch.setattr(
        "raven.cli.sentinel_commands._load_sentinel_config",
        lambda: stub_config,
    )
    monkeypatch.setattr(
        "raven.cli.sentinel_commands.make_provider",
        lambda config: MagicMock(),
    )

    stop_called: dict[str, bool] = {"called": False}

    class _CrashRunner:
        feedback = MagicMock()

        def __init__(self):
            self.task_discoverer = MagicMock()

        async def discover_now(self, *a, **kw):
            raise RuntimeError("simulated LLM crash")

        async def stop(self) -> None:
            stop_called["called"] = True

    monkeypatch.setattr(
        "raven.cli._proactive_stack.build_sentinel_stack",
        lambda *a, **kw: (_CrashRunner(), None, None),
    )

    result = runner.invoke(
        sentinel_app,
        ["discover-now", "feishu", "ou_xxx", "--yes", "--inproc"],
    )
    # Crash propagates out of asyncio.run, so exit code is non-zero
    assert result.exit_code != 0
    # But stop() was still called for cleanup
    assert stop_called["called"], "runner.stop() must be called even if discover_now raises"


# ── subapp registration / status smoke ───────────────────────────────


def test_sentinel_subapp_registered(runner):
    """``raven sentinel --help`` should list the user-facing commands."""
    result = runner.invoke(sentinel_app, ["--help"])
    assert result.exit_code == 0
    for cmd in ("status", "enable", "disable", "tick", "ticks", "nudges", "decisions", "routines"):
        assert cmd in result.output


def test_sentinel_enable_disable_persists(runner, tmp_path: Path, monkeypatch):
    """``sentinel enable``/``disable`` patch sentinel.enabled on disk and
    are idempotent in their messaging."""
    import json

    from raven.config.loader import set_config_path

    cfg = tmp_path / "config.json"
    set_config_path(cfg)
    # Pin the writer's config-path binding (mirrors cron's ``isolated_config``)
    # so a leaked monkeypatch from another test file can't redirect the write.
    monkeypatch.setattr("raven.config.update.get_config_path", lambda: cfg)
    try:
        r = runner.invoke(sentinel_app, ["enable"])
        assert r.exit_code == 0
        assert json.loads(cfg.read_text())["sentinel"]["enabled"] is True

        r = runner.invoke(sentinel_app, ["enable"])
        assert "already enabled" in r.output

        r = runner.invoke(sentinel_app, ["disable"])
        assert r.exit_code == 0
        assert json.loads(cfg.read_text())["sentinel"]["enabled"] is False

        r = runner.invoke(sentinel_app, ["disable"])
        assert "already disabled" in r.output
    finally:
        set_config_path(None)  # type: ignore[arg-type]


def test_sentinel_config_set_quota(runner, tmp_path: Path, monkeypatch):
    """``sentinel config set`` patches nudge-policy quotas on disk."""
    import json

    from raven.config.loader import set_config_path

    cfg = tmp_path / "config.json"
    set_config_path(cfg)
    monkeypatch.setattr("raven.config.update.get_config_path", lambda: cfg)
    try:
        r = runner.invoke(
            sentinel_app,
            ["config", "set", "--max-nudges-per-hour", "1", "--max-nudges-per-day", "3"],
        )
        assert r.exit_code == 0, r.output
        np = json.loads(cfg.read_text())["sentinel"]["nudgePolicy"]
        assert np == {"maxNudgesPerHour": 1, "maxNudgesPerDay": 3}

        # No flags → error.
        r = runner.invoke(sentinel_app, ["config", "set"])
        assert r.exit_code != 0

        # Below-one is rejected.
        r = runner.invoke(sentinel_app, ["config", "set", "--max-nudges-per-hour", "0"])
        assert r.exit_code != 0
    finally:
        set_config_path(None)  # type: ignore[arg-type]


def test_sentinel_status_runs(runner):
    """Status should print config fields without crashing, even at defaults."""
    result = runner.invoke(sentinel_app, ["status"])
    assert result.exit_code == 0
    assert "Sentinel Status" in result.output
    assert "max_nudges_per_hour" in result.output


def test_ticks_config_short_alias_removed(runner):
    """``-c`` no longer binds ``--config`` on ``sentinel ticks`` (UN-41);
    only the long form remains."""
    bad = runner.invoke(sentinel_app, ["ticks", "-c", "/tmp/whatever.json"])
    assert bad.exit_code != 0

    help_r = runner.invoke(sentinel_app, ["ticks", "--help"])
    assert help_r.exit_code == 0
    assert "--config" in help_r.stdout


# ── ticks: headless nudge sink wiring ─────────────────────────────────
# The headless ``sentinel ticks`` CLI has no channel outlet, so the spine
# DeliveryHub post stays unbound. Without a sink, every policy-approved
# nudge dies as ``no_post`` (delivered=False) — which silently zeroed
# proactivity-eval's anticipatory (Type-A) score. --live must wire the
# headless sink; --dry-run must neutralize the dispatcher entirely.


def _stub_tick_outcome():
    """A minimal outcome object the ``ticks`` loop can serialize to JSON."""
    decision = MagicMock()
    decision.action = "skip"
    decision.reason = "test"
    decision.priority = "low"
    decision.target_session = None
    decision.nudge_message = None
    decision.spawn_task = None
    decision.proactivity_score = 0.0
    decision.topic_tag = None
    outcome = MagicMock()
    outcome.decision = decision
    outcome.route = "skip"
    outcome.result = None
    return outcome


class _StubTickRunner:
    def __init__(self):
        self.dispatcher = MagicMock()
        self.injector = MagicMock()
        self.defer_manager = MagicMock()
        self.task_discoverer = MagicMock()
        self.assembler = MagicMock()

    async def tick_with_context(self, _ctx):
        return _stub_tick_outcome()


def _patch_ticks_stack(monkeypatch, runner_obj):
    """Stub out config/provider/session/stack so ``ticks`` runs one fake
    tick against ``runner_obj`` without touching real LLM/disk."""
    from raven.config.raven import SentinelConfig

    stub_config = MagicMock()
    stub_config.sentinel = SentinelConfig(enabled=True)
    stub_config.base = MagicMock()
    monkeypatch.setattr(
        "raven.cli.sentinel_commands._load_sentinel_config",
        lambda: stub_config,
    )
    monkeypatch.setattr(
        "raven.cli.sentinel_commands.make_provider",
        lambda config: MagicMock(),
    )
    monkeypatch.setattr(
        "raven.session.manager.SessionManager",
        lambda *a, **kw: MagicMock(),
    )
    monkeypatch.setattr(
        "raven.cli._proactive_stack.build_sentinel_stack",
        lambda *a, **kw: (runner_obj, None, None),
    )


def test_ticks_live_wires_headless_nudge_sink(runner, monkeypatch):
    """``ticks --live`` must call ``dispatcher.set_post`` with the headless
    sink so policy-approved nudges report delivered instead of no_post."""
    from raven.cli.sentinel_commands import _headless_nudge_sink

    stub_runner = _StubTickRunner()
    _patch_ticks_stack(monkeypatch, stub_runner)

    result = runner.invoke(
        sentinel_app,
        ["ticks", "--from", "2026-05-01T09:00:00", "--to", "2026-05-01T09:00:00", "--live"],
    )
    assert result.exit_code == 0, result.stdout
    stub_runner.dispatcher.set_post.assert_called_once_with(_headless_nudge_sink)


def test_ticks_dry_run_neutralizes_dispatcher_and_skips_sink(runner, monkeypatch):
    """``ticks --dry-run`` (default) must null out the dispatcher and never
    wire a sink, so no spine dispatch side effects leak from a benchmark."""
    stub_runner = _StubTickRunner()
    original_dispatcher = stub_runner.dispatcher
    _patch_ticks_stack(monkeypatch, stub_runner)

    result = runner.invoke(
        sentinel_app,
        ["ticks", "--from", "2026-05-01T09:00:00", "--to", "2026-05-01T09:00:00", "--dry-run"],
    )
    assert result.exit_code == 0, result.stdout
    assert stub_runner.dispatcher is None
    original_dispatcher.set_post.assert_not_called()
