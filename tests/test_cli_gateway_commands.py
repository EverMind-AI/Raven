"""CLI tests for ``raven gateway``.

The ``gateway`` command spawns the full agent loop + channel manager + cron +
heartbeat stack and runs forever. Smoke-level coverage only: ``--help`` works,
options are surfaced, and first-run startup reaches runtime construction.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from raven.cli.commands import app
from raven.config.loader import set_config_path

runner = CliRunner()


@pytest.fixture
def tmp_config(tmp_path: Path) -> Path:
    cfg = tmp_path / "config.json"
    set_config_path(cfg)
    yield cfg
    set_config_path(None)  # type: ignore[arg-type]


def test_gateway_help_works() -> None:
    """``raven gateway --help`` lists the documented options."""
    r = runner.invoke(app, ["gateway", "--help"])
    assert r.exit_code == 0
    assert "Start the Raven gateway" in r.stdout
    assert "--port" in r.stdout
    assert "--workspace" in r.stdout
    assert "--verbose" in r.stdout
    assert "--config" in r.stdout


def test_gateway_help_describes_the_channel_workspace_root():
    """``-w`` names a root, and the root it names must exist.

    This assertion previously pinned "per-session workspaces (default:
    <agent home>/ws)", which is why that wording outlived the design it
    described -- it was load-bearing for a test rather than for a reader. The
    gateway isolates per channel now and `<agent home>/ws` was removed, so
    `raven gateway --help` must not send anyone looking for it.

    Pinned at 80 columns because Typer truncates help it cannot fit, and a
    half-printed path is worse than none -- the first replacement wording was
    long enough to be cut off exactly there.
    """
    r = runner.invoke(app, ["gateway", "--help"], env={"COLUMNS": "80"})
    assert r.exit_code == 0
    output = " ".join(r.output.split())
    assert "per-session" not in output
    assert "/ws" not in output
    assert "per-channel working directories" in output
    assert "~/.raven/tmp" in output


def test_gateway_config_short_alias_removed() -> None:
    """``-c`` no longer binds ``--config`` (UN-41); only the long form remains."""
    bad = runner.invoke(app, ["gateway", "-c", "/tmp/whatever.json"])
    assert bad.exit_code != 0

    r = runner.invoke(app, ["gateway", "--help"])
    assert r.exit_code == 0
    assert "--config" in r.stdout


def test_gateway_without_api_key_reaches_runtime_construction(tmp_config: Path, monkeypatch) -> None:
    """The hosted settings page must start before its first provider exists."""
    from raven.cli import gateway_commands
    from raven.config.loader import save_config
    from raven.config.schema import Config

    class RuntimeReached(Exception):
        pass

    seen: list[bool] = []

    def resolving_provider(_config, _supplier=None, *, allow_unconfigured: bool = False, **_kwargs):
        seen.append(allow_unconfigured)
        raise RuntimeReached

    monkeypatch.setattr(gateway_commands, "make_resolving_provider", resolving_provider)
    save_config(Config())

    r = runner.invoke(app, ["gateway"])
    assert isinstance(r.exception, RuntimeReached)
    assert seen == [True]


# Deeper coverage (mocked provider + early-exit) was attempted but hangs:
# gateway() builds AgentLoop + ChannelManager + Cron + Heartbeat stacks and
# their shutdown paths assume a running event loop. Unit-level mocking can't
# unwind that cleanly. Mark this as out-of-scope for unit tests — a real
# E2E harness (or a focused refactor that splits gateway init from run)
# is the right place to cover the deeper paths.


def test_run_starts_the_litellm_warm_up_before_the_first_request() -> None:
    """``run()``, the body ``bounded_asyncio.run`` executes, starts the LiteLLM
    import in the background at boot, or the first ``model.save_key`` pays it
    inline. ``run()`` is a closure that blocks forever, so its source is pinned
    rather than executed."""
    import inspect

    from raven.cli import gateway_commands

    src = inspect.getsource(gateway_commands.register)
    run_body = src.split("async def run():", 1)[1].split("bounded_asyncio.run(run())", 1)[0]
    assert "warm_up_in_background()" in run_body


def test_run_warms_the_deck_template_covers_once_the_page_is_mounted() -> None:
    """`raven web` is `raven gateway --page-port` underneath, so the gallery's
    covers are drawn from here, after the page mount, not from `raven serve`
    alone; pinned by source for the same reason as above."""
    import inspect

    from raven.cli import gateway_commands

    src = inspect.getsource(gateway_commands.register)
    page_branch = src.split("if page_mount is not None:", 1)[1]
    assert "deck_templates.warm_covers_in_background(" in page_branch
    assert "language=config.language" in page_branch, "in the language the page is in"


def test_run_stops_the_cover_warm_up_before_the_teardown_that_waits() -> None:
    """A cover still converting holds a LibreOffice child, and the thread waiting
    on it holds the interpreter open past every teardown below; the stop comes
    first in the same `finally`."""
    import inspect

    from raven.cli import gateway_commands

    src = inspect.getsource(gateway_commands.register)
    shutdown = src.split("            finally:\n", 1)[1]
    assert "_deck_templates.stop_warming()" in shutdown.split("health_server.close()", 1)[0]


def test_gateway_refuses_second_instance(tmp_config: Path, monkeypatch) -> None:
    """When the instance lock is already held, gateway exits 1 with a clear
    message and never builds the agent/channel stack."""
    from raven.config.loader import save_config
    from raven.config.schema import Config

    save_config(Config())

    from raven.gateway import lock as _gateway_lock

    def _raise(now: float):
        raise _gateway_lock.GatewayAlreadyRunningError(
            _gateway_lock.LockInfo(pid=4242, started_at=0.0, config_path=str(tmp_config))
        )

    monkeypatch.setattr(_gateway_lock, "acquire", _raise)

    r = runner.invoke(app, ["gateway"])
    assert r.exit_code == 1
    assert "already running for this instance" in r.stdout
    assert "4242" in r.stdout


class TestTheGatewayWillNotServeAHalfWrittenInstall:
    """`raven web` supervises the gateway, and the supervisor restarts it the
    moment it exits -- including during an upgrade, when uv has removed the old
    environment and not yet written the new one. `build_app` picks the page
    route once, so a gateway that came up in that window answers `/` with the
    placeholder for the rest of its life, on an installation that was sound
    seconds later."""

    def test_it_refuses_instead_of_serving(self, monkeypatch) -> None:
        from raven.cli import serve_commands
        from raven.updates import install_guard as _install_guard

        monkeypatch.setattr(
            _install_guard,
            "inspect_install",
            lambda: _install_guard.InstallFault("incomplete", "this installation is missing the packaged page"),
        )

        r = runner.invoke(app, ["gateway"])

        assert r.exit_code == serve_commands.INCOMPLETE_INSTALL_EXIT

    def test_it_refuses_before_it_takes_the_instance_lock(self, monkeypatch) -> None:
        """The wait inside the guard can last the whole install. Holding the
        lock through it would block the gateway the finished install is meant
        to bring back."""
        from raven.gateway import lock as _gateway_lock
        from raven.updates import install_guard as _install_guard

        monkeypatch.setattr(
            _install_guard,
            "inspect_install",
            lambda: _install_guard.InstallFault("incomplete", "this installation is missing the packaged page"),
        )

        def unreachable(**_kwargs):
            raise AssertionError("the gateway took the lock on a half-written installation")

        monkeypatch.setattr(_gateway_lock, "acquire", unreachable)

        r = runner.invoke(app, ["gateway"])

        assert r.exit_code != 0

    def test_the_refusal_names_the_gateway_not_serve(self, monkeypatch) -> None:
        """Both surfaces reach the same guard, and a reader told to restart
        `raven serve` when the gateway refused would restart the wrong one."""
        from raven.updates import install_guard as _install_guard

        monkeypatch.setattr(
            _install_guard,
            "inspect_install",
            lambda: _install_guard.InstallFault("incomplete", "this installation is missing the packaged page"),
        )

        r = runner.invoke(app, ["gateway"])

        # The module runner mixes the streams, so the refusal is read off output.
        assert "raven gateway:" in r.output
        assert "raven serve:" not in r.output

    def test_a_sound_install_is_not_stopped_here(self, monkeypatch) -> None:
        """The guard must be invisible on every normal start."""
        from raven.gateway import lock as _gateway_lock
        from raven.updates import install_guard as _install_guard

        monkeypatch.setattr(_install_guard, "inspect_install", lambda: None)
        reached: list[bool] = []

        def _raise(now: float):
            reached.append(True)
            raise _gateway_lock.GatewayAlreadyRunningError(
                _gateway_lock.LockInfo(pid=4242, started_at=0.0, config_path="/tmp/whatever.json")
            )

        monkeypatch.setattr(_gateway_lock, "acquire", _raise)

        runner.invoke(app, ["gateway"])

        assert reached == [True]


def test_gateway_log_config_defaults() -> None:
    from raven.config.schema import GatewayConfig

    log = GatewayConfig().log
    assert log.rotation == "10 MB"
    assert log.retention == 7
    assert log.level == "INFO"
    assert log.console_level == "INFO"


def test_gateway_log_config_omitted_section_is_backward_compatible() -> None:
    from raven.config.schema import GatewayConfig

    cfg = GatewayConfig.model_validate({"port": 18790, "heartbeat": {"enabled": True}})
    assert cfg.log.rotation == "10 MB"
    assert cfg.log.retention == 7
    assert cfg.log.console_level == "INFO"


def test_gateway_log_config_overrides_parse() -> None:
    from raven.config.schema import GatewayConfig

    cfg = GatewayConfig.model_validate(
        {
            "log": {
                "rotation": "00:00",
                "retention": "14 days",
                "level": "DEBUG",
                "console_level": "WARNING",
            }
        }
    )
    assert cfg.log.rotation == "00:00"
    assert cfg.log.retention == "14 days"
    assert cfg.log.level == "DEBUG"
    assert cfg.log.console_level == "WARNING"


def test_gateway_channels_excludes_tui_when_no_im_enabled() -> None:
    # A page-less gateway does not claim ephemeral "tui" cron jobs — those fire
    # in the TUI process, so a TUI-set reminder is never forwarded to an IM
    # channel. With the page mounted the gateway IS a tui surface (see below).
    from types import SimpleNamespace

    from raven.cli.gateway_commands import _build_gateway_channels
    from raven.config.schema import ChannelsConfig

    cfg = SimpleNamespace(
        channels=ChannelsConfig(),
        gateway=SimpleNamespace(page=SimpleNamespace(enabled=False)),
    )
    assert _build_gateway_channels(cfg) == set()  # no IM enabled, and no "tui"


def test_gateway_channels_excludes_tui_alongside_enabled_im() -> None:
    from types import SimpleNamespace

    from raven.cli.gateway_commands import _build_gateway_channels
    from raven.config.schema import ChannelsConfig

    cfg = SimpleNamespace(
        channels=ChannelsConfig.model_validate({"telegram": {"enabled": True}}),
        gateway=SimpleNamespace(page=SimpleNamespace(enabled=False)),
    )
    result = _build_gateway_channels(cfg)
    assert result == {"telegram"}
    assert "tui" not in result


def test_gateway_channels_derived_from_config_model_fields() -> None:
    # The partition is derived from the channels config model, not a
    # hardcoded list: every field with a truthy .enabled participates.
    from types import SimpleNamespace

    from raven.cli.gateway_commands import _build_gateway_channels
    from raven.config.schema import ChannelsConfig

    cfg = SimpleNamespace(
        channels=ChannelsConfig.model_validate(
            {
                "telegram": {"enabled": True},
                "feishu": {"enabled": True},
                "weixin": {"enabled": False},
            }
        ),
        gateway=SimpleNamespace(page=SimpleNamespace(enabled=False)),
    )
    assert _build_gateway_channels(cfg) == {"telegram", "feishu"}


def test_gateway_channels_exclude_tui_even_when_the_page_is_enabled() -> None:
    """Wanting the page is not hosting it. ``mount_page`` yields to a resident
    standalone `raven serve`, so ``page.enabled`` alone says nothing about
    whether this process has a tui outlet -- the partition must be decided by
    the mount's outcome, and this helper only ever answers for the IM side."""
    from types import SimpleNamespace

    from raven.cli.gateway_commands import _build_gateway_channels
    from raven.config.schema import ChannelsConfig

    cfg = SimpleNamespace(
        channels=ChannelsConfig.model_validate({"telegram": {"enabled": True}}),
        gateway=SimpleNamespace(page=SimpleNamespace(enabled=True)),
    )
    result = _build_gateway_channels(cfg)
    assert result == {"telegram"}
    assert "tui" not in result
    assert "cli" not in result


def test_the_live_page_is_what_adds_tui_to_the_cron_partition() -> None:
    """``tui`` must join the partition inside the ``page_mount is not None``
    block and before ``cron.start()``, so the sweep that deletes past-due
    one-shots sees the final partition. Like the /stop test above this lives in
    the serve command with no import seam, so pin the command source."""
    import inspect

    from raven.cli import gateway_commands

    src = inspect.getsource(gateway_commands.register)
    mounted = src.split("if page_mount is not None:", 1)[1].split("# Channel inbound runs through", 1)[0]
    assert 'cron.allowed_channels.add("tui")' in mounted
    before_start, _, after_start = src.partition("await cron.start()")
    assert 'cron.allowed_channels.add("tui")' in before_start
    assert 'cron.allowed_channels.add("tui")' not in after_start
    # And nowhere else: the config helper must not put it back.
    helper_body = inspect.getsource(gateway_commands._build_gateway_channels).split('"""')[-1]
    assert "tui" not in helper_body
    assert "page" not in helper_body


def test_a_mounted_page_takes_the_relays_that_belong_to_its_sessions() -> None:
    """A sub-agent's result relay into a page session must queue on the page
    spine's lane for that session, or it runs beside the page's next turn to the
    same session (2026-09-08: tool calls drawn twice, the result delivered twice).
    The wiring lives in the serve command with no import seam, so pin the source:
    the router is built inside the mounted block, over the page's submit and the
    gateway's, and both runtime submitters take it."""
    import inspect

    from raven.cli import gateway_commands

    src = inspect.getsource(gateway_commands.register)
    mounted = src.split("if page_mount is not None:", 1)[1].split("# Channel inbound runs through", 1)[0]
    assert "route_submit(page=page_mount.submit, channel=pro_submit)" in mounted
    assert "agent.subagents.set_submit(routed_submit)" in mounted
    # The gateway-spine binding still precedes it, for a gateway without a page.
    before_mount = src.split("if page_mount is not None:", 1)[0]
    assert "agent.subagents.set_submit(pro_submit)" in before_mount


class _FakeIntake:
    def __init__(self) -> None:
        self.submit = None

    def set_submit(self, fn) -> None:
        self.submit = fn


class _FakeChannel:
    def __init__(self, name: str = "") -> None:
        self.name = name
        self.intake = _FakeIntake()


class _FakeChannelManager:
    """The members the wiring helpers touch: the table and the two hooks."""

    def __init__(self, *channels, on_started=None, on_stopped=None) -> None:
        self.channels = {f"ch{i}": ch for i, ch in enumerate(channels)}
        self.on_started = on_started
        self.on_stopped = on_stopped


def test_every_channel_present_at_launch_gets_the_inbound_dispatch() -> None:
    from raven.cli.gateway_commands import _wire_channel_intake

    a, b = _FakeChannel(), _FakeChannel()
    manager = _FakeChannelManager(a, b)

    def dispatch(req) -> None: ...

    _wire_channel_intake(manager, dispatch)

    assert a.intake.submit is dispatch
    assert b.intake.submit is dispatch


def test_a_channel_started_while_the_gateway_runs_gets_the_inbound_dispatch() -> None:
    """Both halves of a channel's wiring must survive the launch loop.

    The page enables an entrance by writing config, and the manager builds and
    starts the adapter on the spot -- after the loop that hands every channel
    its dispatch has already run. The outlet half was taught this (`on_started`
    registers an outlet, so a hot-started channel could be replied to); the
    intake half was not, so such a channel logged "no spine dispatch wired" once
    per message and dropped every one, while the page drew it connected and the
    adapter's own login had succeeded (2026-09-14, weixin).
    """
    from raven.cli.gateway_commands import _wire_channel_intake

    outlets: list[object] = []
    manager = _FakeChannelManager(on_started=outlets.append)

    def dispatch(req) -> None: ...

    _wire_channel_intake(manager, dispatch)
    late = _FakeChannel()
    assert late.intake.submit is None, "nothing is wired before the manager starts it"

    manager.on_started(late)

    assert late.intake.submit is dispatch, "a hot-started channel must be given the dispatch"
    assert outlets == [late], "and must keep the outlet the hook already carried"


def test_a_manager_with_no_outlet_hook_still_wires_the_intake() -> None:
    """A gateway built without the hub (no outlet hook) is not a reason to drop
    the intake: the hook is composed over whatever was there, including nothing."""
    from raven.cli.gateway_commands import _wire_channel_intake

    manager = _FakeChannelManager(on_started=None)

    def dispatch(req) -> None: ...

    _wire_channel_intake(manager, dispatch)
    late = _FakeChannel()
    manager.on_started(late)

    assert late.intake.submit is dispatch


def test_the_gateway_command_wires_the_intake_through_the_helper() -> None:
    """Pinned on the source for the reason the mounted-relay test above records:
    the command body has no import seam, so the tie between it and the helper
    the tests above exercise is asserted by name."""
    import inspect

    from raven.cli import gateway_commands

    src = inspect.getsource(gateway_commands.register)
    assert "_wire_channel_intake(channels, _inbound_dispatch)" in src
    # The old launch-only loop is gone: one path wires both the present and the late.
    assert "_ch.intake.set_submit(_inbound_dispatch)" not in src


class _FakeCron:
    """The two members the partition wiring touches, plus a count of the wakes:
    the service's real methods wake its loop, and that is the half a bare set
    could not have shown."""

    def __init__(self, allowed: set[str]) -> None:
        self.allowed_channels = allowed
        self.wakes = 0

    def admit_channel(self, name: str) -> None:
        self.allowed_channels.add(name)
        self.wakes += 1

    def retire_channel(self, name: str) -> None:
        self.allowed_channels.discard(name)
        self.wakes += 1


def test_a_channel_started_while_the_gateway_runs_joins_the_cron_partition() -> None:
    """The cron partition is a launch-time snapshot, so a channel the page enabled
    stayed outside it for the life of the process: it received and replied, while a
    reminder addressed to it was logged once as foreign and never fired until a
    restart (2026-09-23, weixin)."""
    from raven.cli.gateway_commands import _wire_cron_partition

    outlets: list[object] = []
    manager = _FakeChannelManager(on_started=outlets.append)
    cron = _FakeCron({"telegram"})

    _wire_cron_partition(manager, cron)
    late = _FakeChannel("weixin")
    manager.on_started(late)

    assert cron.allowed_channels == {"telegram", "weixin"}
    assert cron.wakes == 1, "the loop is asleep on its poll cap and has to be told"
    assert outlets == [late], "and must keep the outlet the hook already carried"


async def test_a_channel_stopped_leaves_the_cron_partition() -> None:
    """The mirror, and the half that also covers a channel enabled at launch: once
    it is off, the gateway has no outlet for it, so claiming its jobs would burn a
    model turn on a reply the hub drops."""
    from raven.cli.gateway_commands import _wire_cron_partition

    retired: list[str] = []

    async def retire(name: str) -> None:
        retired.append(name)

    manager = _FakeChannelManager(on_stopped=retire)
    cron = _FakeCron({"telegram", "weixin"})

    _wire_cron_partition(manager, cron)
    await manager.on_stopped("telegram")

    assert cron.allowed_channels == {"weixin"}
    assert retired == ["telegram"], "and must keep the outlet retirement the hook carried"


async def test_the_cron_partition_follows_a_manager_with_no_outlet_hooks() -> None:
    """A gateway built without the hub is not a reason to drop the partition half:
    both hooks are composed over whatever was there, including nothing."""
    from raven.cli.gateway_commands import _wire_cron_partition

    manager = _FakeChannelManager(on_started=None, on_stopped=None)
    cron = _FakeCron(set())

    _wire_cron_partition(manager, cron)
    manager.on_started(_FakeChannel("weixin"))
    assert cron.allowed_channels == {"weixin"}

    await manager.on_stopped("weixin")
    assert cron.allowed_channels == set()


def test_the_gateway_command_wires_the_cron_partition_through_the_helper() -> None:
    """Same pin as the intake above, plus the order: the outlet hooks are assigned
    rather than composed, so a partition wired before them would be thrown away."""
    import inspect

    from raven.cli import gateway_commands

    src = inspect.getsource(gateway_commands.register)
    assert "_wire_cron_partition(channels, cron)" in src
    assert src.index("channels.on_started = lambda ch:") < src.index("_wire_cron_partition(channels, cron)")
    assert src.index("channels.on_stopped = gw_hub.retire") < src.index("_wire_cron_partition(channels, cron)")


def _gateway_partition_with_the_page_enabled() -> set[str]:
    """The cron partition a page-wanting gateway is built with."""
    from types import SimpleNamespace

    from raven.cli.gateway_commands import _build_gateway_channels
    from raven.config.schema import ChannelsConfig

    return _build_gateway_channels(
        SimpleNamespace(
            channels=ChannelsConfig.model_validate({"telegram": {"enabled": True}}),
            gateway=SimpleNamespace(page=SimpleNamespace(enabled=True)),
        )
    )


def _write_jobs(store_path: Path, jobs: list[dict]) -> None:
    import json

    store_path.parent.mkdir(parents=True, exist_ok=True)
    store_path.write_text(json.dumps({"version": 1, "jobs": jobs}), encoding="utf-8")


def _due_recurring_tui_job(now_ms: int) -> dict:
    return {
        "id": "recurring",
        "name": "page reminder",
        "enabled": True,
        "schedule": {"kind": "every", "everyMs": 600_000},
        "payload": {"message": "drink water", "channel": "tui", "to": "default"},
        "state": {"nextRunAtMs": 1},
        "createdAtMs": now_ms - 600_000,
        "updatedAtMs": now_ms - 600_000,
    }


def _past_due_oneshot_tui_job(now_ms: int) -> dict:
    return {
        "id": "oneshot",
        "name": "stretch",
        "enabled": True,
        "schedule": {"kind": "at", "atMs": now_ms - 60_000},
        "payload": {"message": "stretch", "channel": "tui", "to": "default"},
        "state": {"nextRunAtMs": now_ms - 60_000},
        "createdAtMs": now_ms - 120_000,
        "updatedAtMs": now_ms - 120_000,
        "deleteAfterRun": True,
    }


async def _fired_ids(store_path: Path, allowed: set[str]) -> list[str]:
    """Job ids a runner on ``allowed`` claims and runs on one due tick."""
    from raven.proactive_engine.schedulers.cron.service import CronService

    fired: list[str] = []

    async def on_job(job) -> None:
        fired.append(job.id)

    svc = CronService(store_path, allowed_channels=allowed)
    svc.on_job = on_job
    await svc._process_due()
    return fired


async def _survivors_after_restart(store_path: Path, allowed: set[str]) -> list[str]:
    """Job ids left in the shared store after a runner on ``allowed`` starts."""
    import json

    from raven.proactive_engine.schedulers.cron.service import CronService

    svc = CronService(store_path, allowed_channels=allowed)
    await svc.start()
    svc.stop()
    return [j["id"] for j in json.loads(store_path.read_text(encoding="utf-8"))["jobs"]]


async def test_a_page_less_gateway_neither_runs_nor_drops_a_tui_reminder(tmp_path: Path) -> None:
    """``page.enabled`` true, mount skipped -- a resident standalone `raven
    serve` owns serve.json. This process has no tui outlet, so both tui jobs
    must be left to the process that has one: the due job is not claimed (its
    reply would be dropped by the hub after a full model turn had already run)
    and the past-due one-shot is not deleted from the shared store."""
    import time

    partition = _gateway_partition_with_the_page_enabled()
    now_ms = int(time.time() * 1000)

    due = tmp_path / "due.json"
    _write_jobs(due, [_due_recurring_tui_job(now_ms)])
    assert await _fired_ids(due, partition) == []

    missed = tmp_path / "missed.json"
    _write_jobs(missed, [_past_due_oneshot_tui_job(now_ms)])
    assert await _survivors_after_restart(missed, partition) == ["oneshot"]


async def test_a_gateway_hosting_the_page_does_claim_tui_jobs(tmp_path: Path) -> None:
    """Mount succeeded, so the wiring adds ``tui`` to the very set the service
    was built with -- ``_owns_channel`` reads it live, so both halves flip: the
    due job runs here, and the past-due one-shot is this runner's to retire."""
    import time

    partition = _gateway_partition_with_the_page_enabled()
    partition.add("tui")  # what the page_mount block does
    now_ms = int(time.time() * 1000)

    due = tmp_path / "due.json"
    _write_jobs(due, [_due_recurring_tui_job(now_ms)])
    assert await _fired_ids(due, partition) == ["recurring"]

    missed = tmp_path / "missed.json"
    _write_jobs(missed, [_past_due_oneshot_tui_job(now_ms)])
    assert await _survivors_after_restart(missed, partition) == []


async def test_a_hot_started_channel_can_then_claim_its_own_cron_jobs(tmp_path: Path) -> None:
    """The far end of the same chain: the set the start hook mutates is the one the
    live service reads, so a reminder addressed to a channel enabled from the page
    fires on the next due tick rather than waiting for the next restart."""
    import time

    from raven.cli.gateway_commands import _wire_cron_partition
    from raven.proactive_engine.schedulers.cron.service import CronService

    now_ms = int(time.time() * 1000)
    store = tmp_path / "jobs.json"
    _write_jobs(
        store,
        [
            {
                "id": "standup",
                "name": "standup nudge",
                "enabled": True,
                "schedule": {"kind": "every", "everyMs": 600_000},
                "payload": {"message": "standup", "channel": "weixin", "to": "default"},
                "state": {"nextRunAtMs": 1},
                "createdAtMs": now_ms - 600_000,
                "updatedAtMs": now_ms - 600_000,
            }
        ],
    )

    fired: list[str] = []

    async def on_job(job) -> None:
        fired.append(job.id)

    svc = CronService(store, allowed_channels=_gateway_partition_with_the_page_enabled())
    svc.on_job = on_job
    await svc._process_due()
    assert fired == [], "weixin was off at launch, so the job is outside the partition"

    manager = _FakeChannelManager()
    _wire_cron_partition(manager, svc)
    manager.on_started(_FakeChannel("weixin"))

    await svc._process_due()
    assert fired == ["standup"]


def test_stop_dispatch_cancels_both_scheduler_and_subagents() -> None:
    """The gateway ``/stop`` path must fan out to BOTH the scheduler lane cancel
    and the subagent-session cancel, summing their counts.

    ``_inbound_dispatch`` is a closure nested inside the gateway serve command
    with no import seam, so this pins the /stop branch of the command source:
    dropping either cancel call (or the summed count) breaks this test.
    """
    import inspect

    from raven.cli import gateway_commands

    src = inspect.getsource(gateway_commands.register)
    stop_branch = src.split('if cmd == "/stop":', 1)[1].split('elif cmd == "/restart":', 1)[0]
    assert "cancel_conversation(cid)" in stop_branch
    assert "cancel_by_session(cid, reason=" in stop_branch
    assert "stopped +=" in stop_branch


def test_shutdown_stops_the_skill_file_watcher() -> None:
    """The teardown chain must stop the skill watcher before the process exits.

    ``LocalSkillCatalog`` auto-starts ``SkillFileWatcher``, a daemon thread that
    parks inside ``watchfiles``' Rust ``watch()``. Daemon status does not make
    process exit safe here: CPython runs ``Py_FinalizeEx`` while that native
    call is still live and the process dies of SIGSEGV (-11, or 139 in a shell)
    *after* every Python shutdown step has already succeeded. A systemd or
    docker stop then records a crash rather than the clean stop it was.

    ``raven/trajectory/replay.py`` stops the same watcher for the same reason;
    the teardown chain here is the other long-lived owner.

    The chain is a closure inside the serve command with no import seam, so what
    this pins is the wiring: that the teardown reaches the seam. What the seam
    then does is pinned by the executable tests further down.
    """
    import inspect

    from raven.cli import gateway_commands

    src = inspect.getsource(gateway_commands.register)
    teardown = src.split("cron.stop()", 1)[1]
    assert "_retire_generation_watchers(swaps, agent)" in teardown


def test_unbinding_a_generation_retires_it_through_dispose() -> None:
    """A generation swap must retire the outgoing generation, watcher included.

    ``build_runtime`` mints a fresh ``AgentLoop`` per generation, so each swap
    starts a new ``SkillFileWatcher`` and abandons the previous one. Measured
    2026-09-18 on this tree -- boot, one SIGHUP, then SIGTERM -- the gateway
    exited -11 with only the teardown chain stopping the live generation's
    watcher, and exits 0 once the unbind retires the outgoing one too.

    Retirement is ``RavenRuntime.dispose``'s job rather than this closure's, so
    what this pins is that the unbind reaches it; that dispose stops the watcher
    is pinned in ``test_core_runtime_swap.py``.
    """
    import inspect

    from raven.cli import gateway_commands

    src = inspect.getsource(gateway_commands.register)
    unbind = src.split("async def _unbind_generation", 1)[1].split("async def _request_swap", 1)[0]
    assert "runtime.dispose()" in unbind


def test_cron_config_notify_missed_defaults_on() -> None:
    from raven.config.schema import CronConfig

    assert CronConfig().notify_missed is True
    assert CronConfig.model_validate({"notify_missed": False}).notify_missed is False


def test_gateway_wires_anti_runaway_count_and_reset() -> None:
    """The anti-runaway guard must be WIRED, not just defined: the cron
    handler gets the service to count fires on (``cron_service=cron``) and
    the AgentLoop's user-inbound hook chains the counter reset after the
    Sentinel hook. Like the /stop test above, these live in closures inside
    the serve command with no import seam, so pin the command source.
    """
    import inspect

    from raven.cli import gateway_commands

    src = inspect.getsource(gateway_commands.register)
    assert "cron_service=cron," in src
    assert "chain_cron_activity_reset(cron, inner=sentinel_on_user_inbound)" in src
    assert "hooks=sentinel_hooks(on_user_inbound, sentinel_response_modifier)," in src


def test_gateway_wires_missed_reminder_observer_behind_config() -> None:
    """The missed-reminder observer must be wired onto the gateway's cron
    service, gated on the event-wake plumbing existing AND
    ``cron.notify_missed`` — with either off there is no sink, so the
    observer must stay unset."""
    import inspect

    from raven.cli import gateway_commands

    src = inspect.getsource(gateway_commands.register)
    gate = src.split("cron.on_missed_foreign", 1)[0].rsplit("if ", 1)[1]
    assert "system_events is not None" in gate
    assert "wake is not None" in gate
    assert "config.cron.notify_missed" in gate
    assert "cron.on_missed_foreign = make_on_missed_foreign(system_events, wake)" in src


# ---------------------------------------------------------------------------
# build_model_routing — routing backend selection
# ---------------------------------------------------------------------------

from types import SimpleNamespace

from raven.config.schema import ModelEndpoint, ProvidersConfig, RoutingConfig
from raven.contracts.llm_provider import GenerationSettings
from raven.core.provider_stack import build_model_routing
from raven.providers.per_model_provider import PerModelProvider
from raven.routing.knn_router import KNNModelRouter
from raven.routing.router import ModelRouter


class _FakeProvider:
    generation = GenerationSettings()

    def get_default_model(self):
        return "default-model"


def _routing_config_obj(routing):
    return SimpleNamespace(
        routing=routing,
        providers=ProvidersConfig.model_validate({}),
        agents=SimpleNamespace(defaults=SimpleNamespace(model="default-model")),
    )


def test_build_routing_disabled_returns_same_provider():
    prov = _FakeProvider()
    router, out = build_model_routing(_routing_config_obj(RoutingConfig(enabled=False)), prov)
    assert router is None
    assert out is prov


def test_build_routing_knn_wraps_provider():
    routing = RoutingConfig(
        enabled=True,
        backend="knn",
        embedding_endpoint="http://e/embed",
        models=[
            ModelEndpoint(model="small", api_base="http://a/v1"),
            ModelEndpoint(model="large", api_base="http://b/v1"),
        ],
    )
    router, out = build_model_routing(_routing_config_obj(routing), _FakeProvider())
    assert isinstance(router, KNNModelRouter)
    assert isinstance(out, PerModelProvider)


def test_build_routing_ecoclaw_with_key_keeps_provider():
    routing = RoutingConfig(enabled=True, backend="ecoclaw", api_key="sk-or-x")
    prov = _FakeProvider()
    router, out = build_model_routing(_routing_config_obj(routing), prov)
    assert isinstance(router, ModelRouter)
    assert out is prov


def test_build_routing_ecoclaw_no_key_disabled():
    routing = RoutingConfig(enabled=True, backend="ecoclaw", api_key="")
    prov = _FakeProvider()
    router, out = build_model_routing(_routing_config_obj(routing), prov)
    assert router is None
    assert out is prov


def _config(default_model: str = "deepseek/deepseek-v3"):
    from raven.config.schema import Config

    cfg = Config()
    cfg.agents.defaults.model = default_model
    cfg.providers.deepseek.api_key = "KD"
    cfg.providers.anthropic.api_key = "KA"
    return cfg


def test_gateway_provider_resolves_vendors_per_call():
    """The gateway serves many sessions at once, so the provider it ends up
    holding must resolve a vendor per call rather than bake in the default
    model's vendor. Asserted on the actual composed object, not on source text."""
    from raven.providers.factory import make_resolving_provider

    cfg = _config()
    router, provider = build_model_routing(cfg, make_resolving_provider(cfg))

    assert router is None  # routing.enabled defaults False
    anthropic = provider._pick("anthropic/claude-opus-4-5")
    deepseek = provider._pick("deepseek/deepseek-v3")
    assert anthropic is not deepseek
    assert anthropic.get_default_model() == "anthropic/claude-opus-4-5"
    assert deepseek.get_default_model() == "deepseek/deepseek-v3"


# ---------------------------------------------------------------------------
# Deliverables — the store now rides the assembly door
# ---------------------------------------------------------------------------
#
# build_runtime defaults deliverables to DeliverableStore(get_deliverables_path())
# and the gateway takes the handle back via RavenRuntime.deliverables (the web
# surface serves the same store). This pins the door default, replacing the
# retired _build_deliverable_store helper.


def test_the_door_defaults_a_deliverables_store(tmp_path, monkeypatch) -> None:
    import raven.core.runtime as runtime_mod
    from raven.agent.tools.deliverables import DeliverableStore

    captured = {}

    class _Spy:
        def __init__(self, **kwargs):
            captured.update(kwargs)
            self.tools = {}

        def configure_personalization(self, *_a, **_k):
            pass

    monkeypatch.setattr("raven.agent.loop.AgentLoop", _Spy)
    monkeypatch.setattr(runtime_mod.plugin_stack, "build_plugin_registry", lambda *a, **k: None)
    monkeypatch.setattr(runtime_mod.plugin_stack, "maybe_build_memory_backend", lambda *a, **k: None)
    monkeypatch.setattr(runtime_mod.plugin_stack, "build_plugin_tools", lambda *a, **k: [])
    monkeypatch.setattr(runtime_mod.token_wise_stack, "install_from_config", lambda *a, **k: None)
    monkeypatch.setattr(runtime_mod.token_wise_stack, "caching_probe", lambda *a, **k: False)

    from raven.config.raven import RavenConfig
    from raven.config.schema import Config

    class _P:
        def get_default_model(self):
            return "fake/default"

    rt = runtime_mod.build_runtime(Config(), RavenConfig(), provider=_P())

    assert isinstance(rt.deliverables, DeliverableStore)


# ---------------------------------------------------------------------------
# _risk_banner: sandbox=none + open allow_from startup warning
# ---------------------------------------------------------------------------


def _config_with(*, backend: str, telegram_enabled: bool, allow_from: list[str]):
    from raven.config.schema import Config

    cfg = Config()
    cfg.tools.sandbox.backend = backend
    cfg.channels.telegram.enabled = telegram_enabled
    cfg.channels.telegram.allow_from = allow_from
    return cfg


def test_risk_banner_fires_on_combo() -> None:
    from raven.cli.gateway_commands import _risk_banner

    banner = _risk_banner(_config_with(backend="none", telegram_enabled=True, allow_from=["*"]))
    assert banner
    assert "sandbox" in banner
    assert "allow_from" in banner
    assert "telegram" in banner


def test_risk_banner_silent_when_sandboxed_or_restricted() -> None:
    from raven.cli.gateway_commands import _risk_banner

    assert _risk_banner(_config_with(backend="boxlite", telegram_enabled=True, allow_from=["*"])) is None
    assert _risk_banner(_config_with(backend="none", telegram_enabled=True, allow_from=["u1"])) is None
    assert _risk_banner(_config_with(backend="none", telegram_enabled=False, allow_from=["*"])) is None


def test_the_gateway_shutdown_cancels_subagents_before_it_closes_the_transports() -> None:
    """An ACP connection closed first fails every pending turn with a connection
    error, which records as a failure rather than as the stop it is. And the
    gateway never closed the ACP pool at all, so its servers outlived it."""
    src = (Path(__file__).resolve().parents[1] / "raven" / "cli" / "gateway_commands.py").read_text(encoding="utf-8")
    # Scope to the shutdown path: the generation-swap path above it tears the
    # same spines down in its own order, pinned by test_generation_swap.py.
    shutdown = src[src.index("except KeyboardInterrupt:") :]
    drain = shutdown.index("begin_drain()")
    cancel = shutdown.index("await agent.subagents.cancel_all(reason=")
    pool = shutdown.index("await close_pool()")

    assert drain < cancel < pool


def test_the_gateway_shutdown_cancels_subagents_before_the_page_spine_seals() -> None:
    """A sub-agent whose conversation lives on the page announces into the
    page's own spine, and the mount's teardown seals that spine first -- so a
    run finishing between the two would announce into a refusal. The swap path
    already cancels before the mount goes; the shutdown path has to match."""
    src = (Path(__file__).resolve().parents[1] / "raven" / "cli" / "gateway_commands.py").read_text(encoding="utf-8")
    shutdown = src[src.index("except KeyboardInterrupt:") :]
    cancel = shutdown.index("await agent.subagents.cancel_all(reason=")
    page = shutdown.index("await page_mount.teardown()")
    spine = shutdown.index("await gw_teardown()")

    assert cancel < page < spine


def test_question_body_numbers_choices_and_shows_batch_progress() -> None:
    """On a chat channel the batch has no dialog to show progress in, so the
    position has to ride in the message text itself."""
    from raven.cli.gateway_commands import _format_question_body

    body = _format_question_body(
        {"question": "Squash?", "choices": ["yes", "no"], "index": 1, "total": 3, "header": "Squash"}
    )

    assert body.splitlines() == ["(2/3) Squash?", "1. yes", "2. no"]


def test_lone_question_carries_no_progress_prefix() -> None:
    from raven.cli.gateway_commands import _format_question_body

    assert _format_question_body({"question": "Which?", "choices": [], "index": 0, "total": 1}) == "Which?"


@pytest.mark.asyncio
async def test_only_a_question_is_rendered_to_the_channel() -> None:
    """This sink is the question broker's whole surface on a chat channel, so
    every frame the broker emits arrives here -- including ``clarify.closed``,
    which has no question in it. Rendering that put an empty message in the
    user's chat every time a question timed out or its turn was cancelled.
    """
    from raven.cli.gateway_commands import _deliver_question_to_channel

    class _Hub:
        def __init__(self) -> None:
            self.sent: list = []

        async def dispatch(self, msg) -> None:
            self.sent.append(msg)

    source = object()
    sources = {"c1": source}

    closed = _Hub()
    await _deliver_question_to_channel(
        {"method": "clarify.closed", "params": {"conversation_id": "c1", "request_id": "q1"}},
        sources=sources,
        hub=closed,
    )
    assert closed.sent == [], "a close notification was rendered as a chat message"

    # The positive half, so a guard that refused everything could not pass.
    asked = _Hub()
    await _deliver_question_to_channel(
        {"method": "clarify.request", "params": {"conversation_id": "c1", "question": "Which?"}},
        sources=sources,
        hub=asked,
    )
    assert [m.content for m in asked.sent] == ["Which?"]


@pytest.mark.asyncio
async def test_question_for_a_dead_conversation_is_reported_not_swallowed() -> None:
    """Swallowing the drop left the broker waiting out its whole budget on a
    question nobody would ever see."""
    from raven.cli.gateway_commands import _deliver_question_to_channel
    from raven.rpc.question_broker import QuestionUndeliverableError

    with pytest.raises(QuestionUndeliverableError):
        # Carries the method because the sink now renders only a question; the
        # frame this asserts about has always been one.
        await _deliver_question_to_channel(
            {"method": "clarify.request", "params": {"conversation_id": "gone"}}, sources={}, hub=None
        )


class TestWhereThePageIsServed:
    """``page_target``: whether this gateway serves the browser page, and where.

    `raven web` supervises a gateway now rather than a standalone `raven serve`,
    because only the gateway runs the channel adapters the entrances page acts
    on. Its ``--page-port`` therefore has to be able to overrule config: the
    supervisor started this process for the page, and its tab is on that port.
    """

    def target(self, *, enabled: bool = True, port: int = 18792, flag: int | None = None) -> int | None:
        from raven.cli.gateway_commands import page_target
        from raven.config.schema import GatewayPageConfig

        return page_target(GatewayPageConfig(enabled=enabled, port=port), flag)

    def test_config_decides_when_nobody_passed_the_flag(self) -> None:
        assert self.target(port=18800) == 18800

    def test_no_page_when_config_turned_it_off_and_nobody_asked(self) -> None:
        """The channel-only run `gateway.page.enabled = false` exists for."""
        assert self.target(enabled=False) is None

    def test_the_flag_mounts_the_page_config_had_switched_off(self) -> None:
        """A supervisor started for the page's sake, running a process with no
        page, would be listening to nothing the open tab can reach."""
        assert self.target(enabled=False, flag=18999) == 18999

    def test_the_flag_pins_the_port_over_the_configured_one(self) -> None:
        """A page that comes back on a different port strands the tab it came
        back for."""
        assert self.target(port=18792, flag=18999) == 18999


# The control plane's token gate is exercised behaviourally in
# tests/test_rpc_control.py (wrong first frame closes the socket, the right
# one answers); the gateway mints the token per boot and publishes it in the
# lock, so there is no config-dependent branch left to read here.


# ---------------------------------------------------------------------------
# raven gateway reload | status | stop -- the control plane's CLI clients
# ---------------------------------------------------------------------------


def _async_value(value):
    async def _f(*a, **k):
        return value

    return _f


def test_gateway_status_reports_no_gateway_as_exactly_that(monkeypatch) -> None:
    from raven.gateway import live_probe

    monkeypatch.setattr(live_probe, "status", _async_value(None))
    r = runner.invoke(app, ["gateway", "status"])
    assert r.exit_code == 1
    assert "No running gateway" in r.stdout


def test_gateway_status_renders_the_generation(monkeypatch) -> None:
    from raven.gateway import live_probe

    monkeypatch.setattr(
        live_probe,
        "status",
        _async_value(
            {
                "pid": 4242,
                "started_at": 0.0,
                "generation": 3,
                "swap_in_flight": False,
                "config_path": "/tmp/c.json",
                "page": {"mounted": False},
            }
        ),
    )
    r = runner.invoke(app, ["gateway", "status"])
    assert r.exit_code == 0
    assert "4242" in r.stdout and "generation 3" in r.stdout


def test_gateway_reload_relays_busy_and_force(monkeypatch) -> None:
    from raven.gateway import live_probe

    seen: list[bool] = []

    async def _reload(*, force: bool = False):
        seen.append(force)
        if not force:
            return {"ok": False, "reason": "busy", "subagents": 2, "questions": 0}
        return {"ok": True, "generation": 2, "swap": "pending", "grace_s": 5.0}

    monkeypatch.setattr(live_probe, "reload", _reload)
    refused = runner.invoke(app, ["gateway", "reload"])
    assert refused.exit_code == 1 and "busy" in refused.stdout and "--force" in refused.stdout
    forced = runner.invoke(app, ["gateway", "reload", "--force"])
    assert forced.exit_code == 0 and "generation 2" in forced.stdout
    assert seen == [False, True]


def test_gateway_stop_defers_to_raven_web_when_supervised(monkeypatch) -> None:
    from raven.cli import serve_commands
    from raven.gateway import live_probe

    monkeypatch.setattr(serve_commands, "_read_web_state", lambda: 12345)
    called: list[str] = []

    async def _shutdown():
        called.append("shutdown")
        return True

    monkeypatch.setattr(live_probe, "shutdown", _shutdown)
    r = runner.invoke(app, ["gateway", "stop"])
    assert r.exit_code == 1
    # Rich wraps at the runner's width; compare whitespace-normalized.
    assert "raven web --stop" in " ".join(r.stdout.split())
    assert called == []


def test_the_gateway_group_lists_its_verbs(monkeypatch) -> None:
    """The group's callback is the daemon; a sub-command must not fall into it
    and the bare command must not be swallowed by the group."""
    r = runner.invoke(app, ["gateway", "--help"])
    assert r.exit_code == 0
    assert "reload" in r.stdout and "status" in r.stdout and "stop" in r.stdout


def test_gateway_stop_asks_the_control_plane_when_unsupervised(monkeypatch) -> None:
    from raven.cli import serve_commands
    from raven.gateway import live_probe

    monkeypatch.setattr(serve_commands, "_read_web_state", lambda: None)
    monkeypatch.setattr(live_probe, "shutdown", _async_value(True))
    r = runner.invoke(app, ["gateway", "stop"])
    assert r.exit_code == 0 and "stopping" in r.stdout


def test_gateway_reload_renders_build_failed_and_no_gateway(monkeypatch) -> None:
    from raven.gateway import live_probe

    async def _failed(**_k):
        return {"ok": False, "reason": "build_failed", "error": "ValueError: boom"}

    monkeypatch.setattr(live_probe, "reload", _failed)
    r = runner.invoke(app, ["gateway", "reload"])
    assert r.exit_code == 1 and "build_failed" in r.stdout and "boom" in r.stdout

    async def _nobody(**_k):
        return None

    monkeypatch.setattr(live_probe, "reload", _nobody)
    r = runner.invoke(app, ["gateway", "reload"])
    assert r.exit_code == 1 and "No running gateway" in r.stdout


def test_sigterm_cancels_the_main_task_instead_of_raising_ki() -> None:
    """[N7-F5] The old SIGTERM handler raised KeyboardInterrupt from a signal
    frame; the stdlib runner converts only SIGINT into a main-task cancel, so
    that KI escaped run_until_complete WITHOUT cancelling the main task -- the
    graceful chain never ran and teardown fell to the runner's 2-second sweep.
    Parity is an in-loop add_signal_handler(SIGTERM, main_task.cancel), and
    the cancelled branch must own the SIGTERM case."""
    import inspect

    from raven.cli import gateway_commands

    src = inspect.getsource(gateway_commands.register)
    assert "add_signal_handler(signal.SIGTERM" in src
    assert "main_task = asyncio.current_task()" in src
    assert "raise KeyboardInterrupt" not in src, "the signal-frame KI shortcut must stay gone"
    assert "if term_signalled:" in src, "the cancelled branch owns the SIGTERM case"


def _watched_loop(stopped: list[str], name: str):
    """A stand-in loop carrying the one collaborator retirement must reach."""
    from types import SimpleNamespace

    skills = SimpleNamespace(stop_file_watcher=lambda: stopped.append(name))
    return SimpleNamespace(context=SimpleNamespace(skills=skills))


class _Swaps:
    """A coordinator stub shaped like the two seats a shutdown has to drain."""

    def __init__(self, staged=None, in_transition=None) -> None:
        self._staged = staged
        self.in_transition = in_transition

    def take(self):
        staged, self._staged = self._staged, None
        return staged


def test_retiring_generation_watchers_reaches_every_seat() -> None:
    """Three generations can own a watcher at once, and all three must go.

    A generation's watcher starts in the context builder's ``__init__``, so
    every built generation owns one whether or not it ever served: the bound
    one, a candidate staged but never consumed, and a candidate ``take`` handed
    out that a cancelled unbind never finished binding. Any one of them left
    running is a daemon thread parked in watchfiles' Rust ``watch()`` when
    ``Py_FinalizeEx`` tears the interpreter down, which is the SIGSEGV.
    """
    from types import SimpleNamespace

    from raven.cli.gateway_commands import _retire_generation_watchers

    stopped: list[str] = []
    swaps = _Swaps(
        staged=SimpleNamespace(runtime=SimpleNamespace(loop=_watched_loop(stopped, "staged"))),
        in_transition=SimpleNamespace(runtime=SimpleNamespace(loop=_watched_loop(stopped, "in_transition"))),
    )

    _retire_generation_watchers(swaps, _watched_loop(stopped, "bound"))

    assert sorted(stopped) == ["bound", "in_transition", "staged"]


def test_retiring_generation_watchers_survives_a_failing_stop() -> None:
    """One stop raising must not strand the watchers behind it.

    The whole point of the sweep is that no watcher outlives it; a sweep that
    abandons the rest on the first exception still exits -11, so the failure
    is logged and the next seat is tried.
    """
    from types import SimpleNamespace

    from raven.cli.gateway_commands import _retire_generation_watchers

    stopped: list[str] = []

    def _boom() -> None:
        raise RuntimeError("watcher stop failed")

    bound = SimpleNamespace(context=SimpleNamespace(skills=SimpleNamespace(stop_file_watcher=_boom)))
    swaps = _Swaps(
        staged=SimpleNamespace(runtime=SimpleNamespace(loop=_watched_loop(stopped, "staged"))),
        in_transition=SimpleNamespace(runtime=SimpleNamespace(loop=_watched_loop(stopped, "in_transition"))),
    )

    _retire_generation_watchers(swaps, bound)

    assert sorted(stopped) == ["in_transition", "staged"]


def test_retiring_generation_watchers_tolerates_empty_seats() -> None:
    """The common shutdown has nothing staged and nothing in transition."""
    from raven.cli.gateway_commands import _retire_generation_watchers

    stopped: list[str] = []
    _retire_generation_watchers(_Swaps(), _watched_loop(stopped, "bound"))

    assert stopped == ["bound"]


def test_a_swap_cancelled_mid_unbind_leaves_no_live_watcher_thread(tmp_path) -> None:
    """The cancellation window, executed against real watcher threads.

    ``_serve_generations`` binds ``take``'s result to a local and only then
    awaits the outgoing generation's unbind. A shutdown cancelling that await
    unwinds the coroutine, and with it the only reference to a generation that
    was fully built -- so its ``SkillFileWatcher`` thread is still parked in
    watchfiles' Rust ``watch()`` when ``Py_FinalizeEx`` runs, which is the
    SIGSEGV. Counting live threads by name is what makes the leak visible:
    the candidate is reachable from neither the coordinator's staging slot nor
    the bound loop, so no assertion about those two would catch it.
    """
    import threading
    import time
    from types import SimpleNamespace

    from raven.cli.gateway_commands import _retire_generation_watchers
    from raven.core.runtime import SwapCandidate, SwapCoordinator
    from raven.memory_engine.skill_forge.catalog import LocalSkillCatalog

    def _live() -> int:
        return sum(1 for t in threading.enumerate() if t.name == "SkillFileWatcher")

    def _loop_over(catalog):
        return SimpleNamespace(context=SimpleNamespace(skills=catalog))

    (tmp_path / "bound").mkdir()
    (tmp_path / "candidate").mkdir()
    before = _live()
    bound = LocalSkillCatalog(tmp_path / "bound")
    candidate = LocalSkillCatalog(tmp_path / "candidate")
    assert _live() == before + 2, "both generations must really be watching"

    swaps = SwapCoordinator(min_interval_s=0.0)
    swaps.stage(SwapCandidate(None, None, None, None, SimpleNamespace(loop=_loop_over(candidate))))
    assert swaps.take() is not None
    # The local a cancelled unbind would drop is simply never stored.

    _retire_generation_watchers(swaps, _loop_over(bound))

    # stop() joins with a 1s timeout, so this only ever spans a slow join --
    # and stays under the suite's 3s idle ceiling on the failing path too.
    for _ in range(20):
        if _live() == before:
            break
        time.sleep(0.1)
    assert _live() == before, "a generation's watcher outlived the shutdown"


async def test_the_control_plane_keeps_the_historical_port_when_the_span_is_free(monkeypatch) -> None:
    """The fallback below must not move the port on a host that has one free."""
    from raven.cli.gateway_commands import _CONTROL_PORT_DEFAULT, _control_plane_port
    from raven.rpc.transports import ws

    async def _free(preferred: int, **_kwargs) -> int:
        return preferred + 1

    monkeypatch.setattr(ws, "pick_port", _free)
    assert await _control_plane_port() == _CONTROL_PORT_DEFAULT + 1


async def test_the_control_plane_falls_back_to_an_os_assigned_port(monkeypatch) -> None:
    """Every port in the probe span can be refused at once, and then the gateway
    must still come up. Windows reserves whole hundred-port blocks (winnat), a
    host whose dynamic range starts low gets them over 8765..8784, and a bind
    inside one fails while netstat shows the port unused. The raise reached no
    handler, so `raven web` died on a port nobody had asked for."""
    from raven.cli.gateway_commands import _control_plane_port
    from raven.rpc.transports import ws

    async def _none_free(preferred: int, **_kwargs) -> int:
        raise OSError(f"no free port in {preferred}..{preferred + 20}")

    monkeypatch.setattr(ws, "pick_port", _none_free)
    assert await _control_plane_port() == 0


def _hold_exclusively(sock) -> None:
    """Ask for the exclusivity the running platform actually means by it.

    ``SO_REUSEADDR`` is what POSIX needs: with a live listener behind it the
    port is taken, and the option only lets the test reclaim it without waiting
    out TIME_WAIT. Winsock reads the same option as permission for a second
    socket to bind the identical address and port, which is the opposite of
    what the holder wants, so Windows gets ``SO_EXCLUSIVEADDRUSE`` instead.

    Keyed on the constant rather than on ``sys.platform`` because the constant
    is the thing that decides: a platform that does not define it has no
    Winsock semantics to defend against.
    """
    import socket

    exclusive = getattr(socket, "SO_EXCLUSIVEADDRUSE", None)
    option = socket.SO_REUSEADDR if exclusive is None else exclusive
    sock.setsockopt(socket.SOL_SOCKET, option, 1)


@pytest.mark.parametrize("windows", [True, False], ids=["windows", "posix"])
def test_the_port_holder_asks_for_the_exclusivity_its_platform_means(
    windows: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The holder below has to keep a port from being bound twice, and the two
    platforms spell that differently.

    On POSIX ``SO_REUSEADDR`` plus a live listener does it. On Windows the same
    option does the opposite: Winsock lets a second socket bind the identical
    address and port, with indeterminate ownership, so ``_port_is_free`` -- which
    sets ``SO_REUSEADDR`` itself -- can bind a port this holder is listening on.
    ``pick_port`` would then return the base port and the exhaustion case below
    would fail without anything being wrong with the code it guards.

    Both branches are driven here because they cannot both be driven anywhere
    else: the unit matrix is one cell, ubuntu, and ``SO_EXCLUSIVEADDRUSE`` does
    not exist on it. Only the option asked for is asserted. Whether Winsock then
    refuses the second bind is Winsock's contract, not this repository's, and is
    not claimed to have been observed here.
    """
    import socket

    from tests.test_cli_gateway_commands import _hold_exclusively

    asked: list[tuple[int, int, int]] = []

    class _Sock:
        def setsockopt(self, level: int, option: int, value: int) -> None:
            asked.append((level, option, value))

    exclusive = 0x4321
    if windows:
        monkeypatch.setattr(socket, "SO_EXCLUSIVEADDRUSE", exclusive, raising=False)
    else:
        monkeypatch.delattr(socket, "SO_EXCLUSIVEADDRUSE", raising=False)

    _hold_exclusively(_Sock())

    wanted = exclusive if windows else socket.SO_REUSEADDR
    assert asked == [(socket.SOL_SOCKET, wanted, 1)]


async def test_pick_port_raises_the_class_the_fallback_catches() -> None:
    """The fallback catches one exception class, decided in another module.

    Both cases above replace ``pick_port`` with a stub that raises ``OSError``
    itself, so they pin the helper's reaction to a raise they authored and would
    not notice ``pick_port`` starting to raise something else -- which turns the
    fallback into dead code and brings back the bring-up crash this change
    exists to remove. This case reaches the real function instead.
    """
    import socket

    from raven.rpc.transports.ws import _PORT_PROBE_SPAN, pick_port

    def _hold(base: int) -> list[socket.socket] | None:
        """The whole span held here, or None if any port was already taken.

        Occupied the way ``_port_is_free`` probes for it: that probe sets
        SO_REUSEADDR, so a socket merely bound does not keep it out and only a
        live listener does -- on POSIX. Winsock reads that option as leave to
        bind the same address and port a second time, so the holder asks for
        the platform's own spelling of exclusivity; see ``_hold_exclusively``.
        Holding every port here rather than counting a stranger's as one of
        them is what stops this racing them releasing it.
        """
        held: list[socket.socket] = []
        for port in range(base, base + _PORT_PROBE_SPAN):
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            _hold_exclusively(sock)
            try:
                sock.bind(("127.0.0.1", port))
                sock.listen(1)
            except OSError:
                sock.close()
                for other in held:
                    other.close()
                return None
            held.append(sock)
        return held

    for base in range(41000, 41000 + 10 * _PORT_PROBE_SPAN, _PORT_PROBE_SPAN):
        held = _hold(base)
        if held is not None:
            break
    else:
        pytest.fail("no span of free ports to exhaust; the contract went unchecked")

    try:
        with pytest.raises(OSError):
            await pick_port(base)
    finally:
        for sock in held:
            sock.close()


def test_the_gateway_takes_its_control_port_from_the_fallback() -> None:
    """The two tests above only bind the helper; this pins the caller to it.
    Both passed while the command still probed inline, which is the state that
    shipped the failure."""
    import inspect

    from raven.cli import gateway_commands

    src = inspect.getsource(gateway_commands.register)
    assert "ControlPlaneServer(await _control_plane_port()" in src
    assert "pick_port(8765)" not in src, "an inline probe has no fallback to fall back to"
