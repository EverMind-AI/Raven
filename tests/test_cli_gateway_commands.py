"""CLI tests for ``raven gateway``.

The ``gateway`` command spawns the full agent loop + channel manager + cron +
heartbeat stack and runs forever. Smoke-level coverage only: ``--help`` works,
options are surfaced, the no-API-key path exits cleanly.
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


def test_gateway_without_api_key_exits_with_error(tmp_config: Path) -> None:
    """With no provider configured, gateway must exit non-zero — and crucially
    must not raise a crash-class exception (NameError / AttributeError /
    ImportError). Those would indicate a regression like a missing import.
    """
    from raven.config.loader import save_config
    from raven.config.schema import Config

    save_config(Config())  # default config, no keys

    r = runner.invoke(app, ["gateway"])
    if r.exception is not None:
        assert not isinstance(r.exception, (NameError, AttributeError, ImportError)), (
            f"Crash-class exception leaked through: {r.exception!r}"
        )
    assert r.exit_code != 0


# Deeper coverage (mocked provider + early-exit) was attempted but hangs:
# gateway() builds AgentLoop + ChannelManager + Cron + Heartbeat stacks and
# their shutdown paths assume a running event loop. Unit-level mocking can't
# unwind that cleanly. Mark this as out-of-scope for unit tests — a real
# E2E harness (or a focused refactor that splits gateway init from run)
# is the right place to cover the deeper paths.


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
    assert "cancel_by_session(cid)" in stop_branch
    assert "stopped +=" in stop_branch


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
    assert "on_user_inbound=on_user_inbound," in src


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
from raven.core.provider_stack import build_model_routing
from raven.providers.base import GenerationSettings
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
    from raven.agent.tools._deliverables import DeliverableStore

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
    cancel = shutdown.index("await agent.subagents.cancel_all()")
    pool = shutdown.index("await close_pool()")

    assert drain < cancel < pool


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
        _async_value({"pid": 4242, "started_at": 0.0, "generation": 3, "swap_in_flight": False,
                      "config_path": "/tmp/c.json", "page": {"mounted": False}}),
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
