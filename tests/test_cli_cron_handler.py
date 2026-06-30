"""Trigger-path tests for ``make_on_cron_job`` (raven/cli/_cron_handler.py).

Distinct scope from:
  - ``test_cron_delivery.py`` — unit-tests the ``resolve_cron_delivery``
    helper in isolation.
  - ``test_cron_handler_ledger.py`` — covers the sentinel ledger write
    side-effect.

This file targets the factory closure itself: trigger-time dynamic
config read, outbound expansion, ephemeral vs pass-through routing.
Heavily mock-based so it stays a pure unit test (no real channels,
no real agent loop, no real session disk I/O).

Every cron turn now runs through the spine ``submit``. A single-target
delivering job rides the hub (the test asserts the submitted request's
source, since the outlets are wired only in the assembly tests); a
broadcast (len > 1) submits with an ephemeral source the hub drops and is
delivered explicitly via ``hub.post`` (one spine Text per target); a silent
job delivers nothing.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from raven.cli._cron_handler import make_on_cron_job
from raven.proactive_engine.schedulers.cron.types import (
    CronJob,
    CronJobState,
    CronPayload,
    CronSchedule,
)
from raven.spine import Origin, Text


def _make_job(
    *,
    channel: str = "cli",
    to: str = "direct",
    deliver: bool = True,
    name: str = "test_job",
) -> CronJob:
    return CronJob(
        id=f"job_{name}",
        name=name,
        enabled=True,
        schedule=CronSchedule(kind="at", at_ms=1000),
        payload=CronPayload(
            kind="agent_turn",
            message="reminder body source",
            deliver=deliver,
            channel=channel,
            to=to,
        ),
        state=CronJobState(),
    )


@pytest.fixture
def fake_agent() -> MagicMock:
    """AgentLoop mock. Unused on the spine path (kept for the factory signature)."""
    return MagicMock()


@pytest.fixture
def fake_hub() -> MagicMock:
    hub = MagicMock()
    hub.post = AsyncMock()
    return hub


@pytest.fixture
def spine() -> SimpleNamespace:
    """Spine submit mock + readback map, mimicking the gateway capturing runner:
    submit records the request and stores the reply text under req.conversation
    before result() resolves (so the handler reads it back)."""
    readback: dict[str, str] = {}
    captured: list = []

    class _Handle:
        async def result(self):
            return None

    def _submit(req):
        captured.append(req)
        readback[req.conversation] = "resolved body"
        return _Handle()

    return SimpleNamespace(submit=_submit, readback=readback, captured=captured)


@pytest.fixture
def fake_session_mgr() -> MagicMock:
    """SessionManager mock — find_most_recent_chat_id returns fixed ids."""
    mgr = MagicMock()
    mgr.find_most_recent_chat_id.side_effect = lambda ch: {
        "telegram": "tg_user_1",
        "feishu": "ou_user_1",
    }.get(ch)
    return mgr


@pytest.fixture
def patch_cron_config(monkeypatch):
    """Patch ``load_config()`` to return a stub with controllable
    ``cron.forward_channels``. Returns a setter the test calls."""

    def _patch(forward_channels: list[str]):
        config = MagicMock()
        config.cron = SimpleNamespace(forward_channels=forward_channels)
        monkeypatch.setattr(
            "raven.config.loader.load_config",
            lambda: config,
        )

    return _patch


# ─────────────────────────────────────────────────────────────────────
# Ephemeral broadcast (cli → forward_channels, more than one target)
# ─────────────────────────────────────────────────────────────────────


async def test_trigger_ephemeral_broadcasts_to_all_enabled(
    fake_agent,
    fake_hub,
    spine,
    fake_session_mgr,
    patch_cron_config,
):
    """forward_channels=['*'] + cli job → broadcast to every enabled channel.

    More than one target → the turn submits with the ephemeral source (hub
    drops the reply) and the resolved text is posted to each target.
    """
    patch_cron_config(["*"])
    channel_manager = SimpleNamespace(enabled_channels=["telegram", "feishu"])

    handler = make_on_cron_job(
        fake_agent,
        fake_hub,
        submit=spine.submit,
        readback_texts=spine.readback,
        channel_manager=channel_manager,
        session_manager=fake_session_mgr,
    )

    await handler(_make_job(channel="cli"))

    assert len(spine.captured) == 1 and spine.captured[0].origin is Origin.CRON
    assert fake_hub.post.await_count == 2
    sent = [call.args[0] for call in fake_hub.post.await_args_list]
    assert all(isinstance(out, Text) for out in sent)
    by_channel = {out.source.channel: out.source.chat_id for out in sent}
    assert by_channel == {"telegram": "tg_user_1", "feishu": "ou_user_1"}
    assert all(out.content == "resolved body" for out in sent)


# ─────────────────────────────────────────────────────────────────────
# Single-target delivering job rides the hub (no explicit broadcast post)
# ─────────────────────────────────────────────────────────────────────


async def test_trigger_ephemeral_restricted_to_single_target_rides_hub(
    fake_agent,
    fake_hub,
    spine,
    fake_session_mgr,
    patch_cron_config,
):
    """forward_channels=['telegram'] resolves to one target → the turn submits
    with that target as the source (hub delivers); no explicit broadcast post."""
    patch_cron_config(["telegram"])
    channel_manager = SimpleNamespace(enabled_channels=["telegram", "feishu"])

    handler = make_on_cron_job(
        fake_agent,
        fake_hub,
        submit=spine.submit,
        readback_texts=spine.readback,
        channel_manager=channel_manager,
        session_manager=fake_session_mgr,
    )

    await handler(_make_job(channel="cli"))

    assert len(spine.captured) == 1
    assert spine.captured[0].source.channel == "telegram"
    assert spine.captured[0].source.chat_id == "tg_user_1"
    fake_hub.post.assert_not_awaited()


async def test_trigger_tui_treated_same_as_cli(
    fake_agent,
    fake_hub,
    spine,
    fake_session_mgr,
    patch_cron_config,
):
    """channel='tui' is also ephemeral and gets forwarded (single target → hub)."""
    patch_cron_config(["feishu"])
    channel_manager = SimpleNamespace(enabled_channels=["telegram", "feishu"])

    handler = make_on_cron_job(
        fake_agent,
        fake_hub,
        submit=spine.submit,
        readback_texts=spine.readback,
        channel_manager=channel_manager,
        session_manager=fake_session_mgr,
    )

    await handler(_make_job(channel="tui", to="default"))

    assert spine.captured[0].source.channel == "feishu"
    fake_hub.post.assert_not_awaited()


async def test_trigger_real_channel_passthrough(
    fake_agent,
    fake_hub,
    spine,
    fake_session_mgr,
    patch_cron_config,
):
    """telegram job ignores forward_channels — delivered to its own (channel, to)
    via the hub (single pass-through target).

    Guards against the regression where someone wires forward_channels
    into every path; per-job binding for real channels must stay sacred.
    """
    patch_cron_config(["feishu"])  # would re-route an ephemeral job; should NOT touch telegram
    channel_manager = SimpleNamespace(enabled_channels=["telegram", "feishu"])

    handler = make_on_cron_job(
        fake_agent,
        fake_hub,
        submit=spine.submit,
        readback_texts=spine.readback,
        channel_manager=channel_manager,
        session_manager=fake_session_mgr,
    )

    await handler(_make_job(channel="telegram", to="explicit_chat_999"))

    assert spine.captured[0].source.channel == "telegram"
    assert spine.captured[0].source.chat_id == "explicit_chat_999"
    fake_hub.post.assert_not_awaited()


# ─────────────────────────────────────────────────────────────────────
# No-delivery edge cases (turn still runs for side-effects)
# ─────────────────────────────────────────────────────────────────────


async def test_trigger_no_overlap_emits_no_outbound(
    fake_agent,
    fake_hub,
    spine,
    fake_session_mgr,
    patch_cron_config,
):
    """forward_channels=['xyz'] but enabled={'telegram'} → 0 targets.

    The turn still submits (for side-effects); the ephemeral source has no
    outlet so the hub drops it and there is no broadcast.
    """
    patch_cron_config(["xyz"])
    channel_manager = SimpleNamespace(enabled_channels=["telegram"])

    handler = make_on_cron_job(
        fake_agent,
        fake_hub,
        submit=spine.submit,
        readback_texts=spine.readback,
        channel_manager=channel_manager,
        session_manager=fake_session_mgr,
    )

    await handler(_make_job(channel="cli"))

    assert len(spine.captured) == 1
    fake_hub.post.assert_not_awaited()


async def test_trigger_deliver_false_emits_no_outbound(
    fake_agent,
    fake_hub,
    spine,
    fake_session_mgr,
    patch_cron_config,
):
    """A job with deliver=False is run for side-effects only — the turn submits
    but nothing is delivered (no hub outlet for the ephemeral source, no
    broadcast)."""
    patch_cron_config(["*"])
    channel_manager = SimpleNamespace(enabled_channels=["telegram"])

    handler = make_on_cron_job(
        fake_agent,
        fake_hub,
        submit=spine.submit,
        readback_texts=spine.readback,
        channel_manager=channel_manager,
        session_manager=fake_session_mgr,
    )

    await handler(_make_job(channel="cli", deliver=False))

    assert len(spine.captured) == 1
    fake_hub.post.assert_not_awaited()


# ─────────────────────────────────────────────────────────────────────
# Dynamic config binding (cron config set takes effect on next trigger)
# ─────────────────────────────────────────────────────────────────────


async def test_trigger_dynamic_config_reload(
    fake_agent,
    fake_hub,
    spine,
    fake_session_mgr,
    monkeypatch,
):
    """``cron config set forward_channels`` must take effect on the next
    trigger WITHOUT recreating the handler — the closure reads config
    fresh each fire. Each resolves to a single target → rides the hub."""
    state = {"forward_channels": ["telegram"]}

    def _make_config():
        config = MagicMock()
        config.cron = SimpleNamespace(forward_channels=state["forward_channels"])
        return config

    monkeypatch.setattr("raven.config.loader.load_config", _make_config)
    channel_manager = SimpleNamespace(enabled_channels=["telegram", "feishu"])

    handler = make_on_cron_job(
        fake_agent,
        fake_hub,
        submit=spine.submit,
        readback_texts=spine.readback,
        channel_manager=channel_manager,
        session_manager=fake_session_mgr,
    )

    # First fire — telegram only
    await handler(_make_job(channel="cli", name="t1"))
    assert spine.captured[-1].source.channel == "telegram"

    # User runs `cron config set forward_channels feishu` between fires
    state["forward_channels"] = ["feishu"]

    # Second fire — feishu only (NEW config, same handler instance)
    await handler(_make_job(channel="cli", name="t2"))
    assert spine.captured[-1].source.channel == "feishu"
    fake_hub.post.assert_not_awaited()


# ─────────────────────────────────────────────────────────────────────
# Spine read-back: a delivering single-target job runs as a CRON turn; the
# reply is read back from readback_texts for the system event (the submitter
# cannot pass run_turn's text_sink — the gateway's capturing runner stores it
# before result() resolves).
# ─────────────────────────────────────────────────────────────────────


async def test_spine_path_reads_back_reply_into_system_event(
    fake_agent,
    fake_hub,
    fake_session_mgr,
    patch_cron_config,
):
    patch_cron_config(["*"])
    channel_manager = SimpleNamespace(enabled_channels=["telegram"])
    system_events = MagicMock()
    wake = MagicMock()
    readback_texts: dict[str, str] = {}
    captured: dict[str, object] = {}

    class _Handle:
        async def result(self):
            # The gateway runner stores the reply before result() resolves.
            readback_texts["cron:job_t1"] = "reminder done at 17:05"
            return None

    def _submit(req):
        captured["req"] = req
        return _Handle()

    handler = make_on_cron_job(
        fake_agent,
        fake_hub,
        submit=_submit,
        readback_texts=readback_texts,
        channel_manager=channel_manager,
        session_manager=fake_session_mgr,
        system_events=system_events,
        wake=wake,
    )

    await handler(_make_job(channel="telegram", to="c1", name="t1"))

    # Single target → rides the hub; no explicit broadcast post.
    fake_hub.post.assert_not_awaited()
    assert captured["req"].origin is Origin.CRON
    assert captured["req"].conversation == "cron:job_t1"
    # Read back into the system event, then popped (no leak in the long-running map).
    system_events.enqueue.assert_called_once()
    assert "reminder done at 17:05" in system_events.enqueue.call_args.args[0].text
    assert "cron:job_t1" not in readback_texts


async def test_spine_path_no_reply_falls_back_to_no_response(
    fake_agent,
    fake_hub,
    fake_session_mgr,
    patch_cron_config,
):
    patch_cron_config(["*"])
    channel_manager = SimpleNamespace(enabled_channels=["telegram"])
    system_events = MagicMock()
    wake = MagicMock()
    readback_texts: dict[str, str] = {}  # runner stored nothing (empty reply)

    class _Handle:
        async def result(self):
            return None

    handler = make_on_cron_job(
        fake_agent,
        fake_hub,
        submit=lambda req: _Handle(),
        readback_texts=readback_texts,
        channel_manager=channel_manager,
        session_manager=fake_session_mgr,
        system_events=system_events,
        wake=wake,
    )

    await handler(_make_job(channel="telegram", to="c1", name="t2"))

    system_events.enqueue.assert_called_once()
    assert "(no response)" in system_events.enqueue.call_args.args[0].text


async def test_silent_job_on_non_ephemeral_channel_warns(
    fake_agent,
    fake_hub,
    spine,
    fake_session_mgr,
    patch_cron_config,
):
    """gap2 edge: a silent job (deliver=False) on a non-ephemeral channel is only
    reachable by hand-editing jobs.json (every creation path sets deliver=True).
    Under the spine its reply IS delivered (no outlet-less suppression for real
    channels) — the handler warns so the edge is visible."""
    from loguru import logger

    patch_cron_config(["*"])
    channel_manager = SimpleNamespace(enabled_channels=["telegram"])

    handler = make_on_cron_job(
        fake_agent,
        fake_hub,
        submit=spine.submit,
        readback_texts=spine.readback,
        channel_manager=channel_manager,
        session_manager=fake_session_mgr,
    )

    warnings: list[str] = []
    sink_id = logger.add(lambda m: warnings.append(str(m)), level="WARNING")
    try:
        await handler(_make_job(channel="telegram", to="c1", deliver=False, name="silent_im"))
    finally:
        logger.remove(sink_id)

    assert len(spine.captured) == 1
    assert any("non-ephemeral" in m for m in warnings)


# ─────────────────────────────────────────────────────────────────────
# REPL assembly: cron wired with submit=build_repl's scheduler.
# A delivering cli job renders once via the CliOutlet and never broadcasts
# explicitly — single target rides the hub through the turn reply.
# ─────────────────────────────────────────────────────────────────────


async def test_repl_assembly_cron_renders_once_via_clioutlet_not_broadcast(fake_hub, patch_cron_config):
    from raven.cli._repl_spine import build_repl
    from raven.spine import Text, TurnOutcome, Usage

    patch_cron_config(["*"])

    class _CronEchoLoop:
        async def run_turn(self, req, emit, drain, *, stream) -> TurnOutcome:
            await emit(Text(content=f"cron-reply<{req.conversation}>", source=req.source))
            return TurnOutcome(usage=Usage(0, 0, 0), explicit_reply=True)

    rendered: list[str] = []
    scheduler, hub, teardown = build_repl(_CronEchoLoop(), "cli", rendered.append)
    # cli_shim makes "cli" non-ephemeral -> resolve_cron_delivery passes through
    # to a single "cli" target -> rides the hub. agent is unused on the spine path.
    handler = make_on_cron_job(
        MagicMock(),
        hub,
        submit=scheduler.submit,
        channel_manager=SimpleNamespace(enabled_channels=["cli"]),
        session_manager=None,
        default_channel="cli",
    )

    try:
        await handler(_make_job(channel="cli", to="direct", name="repl1"))
        await hub.wait_idle("cli")
    finally:
        await teardown()

    # Rendered exactly once via the CliOutlet (the single-target turn reply);
    # the broadcast path never fired (single target rides the hub).
    assert rendered == ["cron-reply<cron:job_repl1>"]


# ─────────────────────────────────────────────────────────────────────
# Broadcast delivery edge cases
# ─────────────────────────────────────────────────────────────────────


async def test_fan_out_tui_offline_no_im_configured_drops_with_warning(
    fake_agent,
    fake_hub,
    spine,
    fake_session_mgr,
    patch_cron_config,
):
    """When gateway claims a stale TUI cron job but the user has no IM channel
    enabled, ``resolve_cron_delivery`` SHALL return zero targets and the
    handler SHALL emit no outbound (reminder is dropped). Verifies the
    gateway-fallback degraded path: TUI offline + no IM channel configured.
    """
    patch_cron_config(["*"])
    channel_manager = SimpleNamespace(enabled_channels=[])

    handler = make_on_cron_job(
        fake_agent,
        fake_hub,
        submit=spine.submit,
        readback_texts=spine.readback,
        channel_manager=channel_manager,
        session_manager=fake_session_mgr,
    )
    await handler(_make_job(channel="tui", name="hydrate_no_im"))

    assert len(spine.captured) == 1
    fake_hub.post.assert_not_awaited()


async def test_fan_out_posts_text_per_target(
    fake_agent,
    fake_hub,
    spine,
    fake_session_mgr,
    patch_cron_config,
):
    """A broadcast (more than one target) posts one spine Text per target,
    carrying the resolved reply content and a cron-stamped source."""
    patch_cron_config(["*"])
    channel_manager = SimpleNamespace(enabled_channels=["telegram", "feishu"])

    handler = make_on_cron_job(
        fake_agent,
        fake_hub,
        submit=spine.submit,
        readback_texts=spine.readback,
        channel_manager=channel_manager,
        session_manager=fake_session_mgr,
    )

    await handler(_make_job(channel="cli", name="hydrate"))

    assert fake_hub.post.await_count == 2
    out = fake_hub.post.await_args_list[0].args[0]
    assert isinstance(out, Text)
    assert out.content == "resolved body"
    assert out.source.sender_id == "cron"
    assert out.source.channel in {"telegram", "feishu"}
