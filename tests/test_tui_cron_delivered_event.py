"""Tests for the ``cron.delivered`` RPC event surface.

Covers the TurnEvent ``cron.delivered`` variant + the UI handler that
renders it, plus the "cron output reaches the TUI UI" requirement on the
Python side (bus subscriber → emitter fan-out).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

# ---------------------------------------------------------------------------
# 2.4 — CronDeliveredEvent Pydantic model validates + round-trips via TurnEvent
# ---------------------------------------------------------------------------


def test_cron_delivered_event_pydantic_validates() -> None:
    """``CronDeliveredEvent`` SHALL be a member of the ``TurnEvent``
    discriminated union with payload {job_id, name, text, fired_at}.
    """
    from raven.rpc.models import CronDeliveredEvent

    event = CronDeliveredEvent(
        type="cron.delivered",
        payload={
            "job_id": "j1",
            "name": "hydrate",
            "text": "记得喝水",
            "fired_at": "2026-06-04T10:23:00Z",
        },
    )
    assert event.type == "cron.delivered"
    assert event.payload.job_id == "j1"
    assert event.payload.name == "hydrate"
    assert event.payload.text == "记得喝水"
    assert event.payload.fired_at == "2026-06-04T10:23:00Z"


def test_cron_delivered_event_in_turn_event_union() -> None:
    """``TurnEvent`` union SHALL dispatch ``type="cron.delivered"`` to
    ``CronDeliveredEvent`` via Pydantic discriminator.
    """
    from pydantic import TypeAdapter

    from raven.rpc.models import CronDeliveredEvent, TurnEvent

    adapter = TypeAdapter(TurnEvent)
    parsed = adapter.validate_python(
        {
            "type": "cron.delivered",
            "payload": {
                "job_id": "j2",
                "name": "stretch",
                "text": "起来活动一下",
                "fired_at": "2026-06-04T11:00:00Z",
            },
        }
    )
    assert isinstance(parsed, CronDeliveredEvent)


# ---------------------------------------------------------------------------
# cron.delivered fan-out (spine read-back -> wrapper fan-out, off the bus)
# ---------------------------------------------------------------------------


@pytest.fixture
def emitter_spy() -> MagicMock:
    """Stand-in for ``SubscriptionEmitter`` exposing ``_by_session`` keys
    + ``emit`` async method.
    """
    e = MagicMock()
    e._by_session = {"sess_user_default": [object()]}
    e.emit = AsyncMock()
    return e


async def test_fanout_cron_delivered_single_session(emitter_spy: MagicMock) -> None:
    """``_fanout_cron_delivered`` SHALL emit a ``cron.delivered`` event with the
    full {job_id, name, text, fired_at} payload to the active session."""
    from raven.cli.tui_commands import _fanout_cron_delivered

    await _fanout_cron_delivered(
        emitter_spy,
        job_id="j1",
        name="hydrate",
        text="time to hydrate",
        fired_at="2026-06-04T10:23:00Z",
    )

    emitter_spy.emit.assert_awaited_once()
    call_args = emitter_spy.emit.await_args
    assert call_args.args[0] == "sess_user_default"
    event = call_args.args[1]
    assert event["type"] == "cron.delivered"
    assert event["payload"] == {
        "job_id": "j1",
        "name": "hydrate",
        "text": "time to hydrate",
        "fired_at": "2026-06-04T10:23:00Z",
    }


async def test_fanout_cron_delivered_multi_session(emitter_spy: MagicMock) -> None:
    """Each active session_key SHALL receive the cron.delivered event (fan-out,
    because the cron:<job_id> conversation matches no user subscription)."""
    from raven.cli.tui_commands import _fanout_cron_delivered

    emitter_spy._by_session = {"sess_a": [object()], "sess_b": [object()]}
    await _fanout_cron_delivered(emitter_spy, job_id="j3", name="test", text="multi", fired_at="2026-06-04T12:00:00Z")

    assert emitter_spy.emit.await_count == 2
    keys_called = {c.args[0] for c in emitter_spy.emit.await_args_list}
    assert keys_called == {"sess_a", "sess_b"}


async def test_fanout_cron_delivered_no_active_sessions() -> None:
    """No active session subscribers -> a silent no-op (no emit, no exception)."""
    from raven.cli.tui_commands import _fanout_cron_delivered

    emitter = MagicMock()
    emitter._by_session = {}
    emitter.emit = AsyncMock()
    await _fanout_cron_delivered(emitter, job_id="j", name="n", text="t", fired_at="f")
    emitter.emit.assert_not_awaited()


async def test_cron_callback_spine_fans_out_reply(emitter_spy: MagicMock) -> None:
    """``_build_cron_callback_spine`` SHALL run the base callback (the spine
    submit + read-back) and fan its reply out as cron.delivered with the job's
    metadata; a job whose turn produced no reply SHALL NOT fan out."""
    from types import SimpleNamespace

    from raven.cli.tui_commands import _build_cron_callback_spine

    replies = {"j7": "reminder body", "j8": None}

    async def base_on_cron(job):
        return replies[job.id]  # the read-back reply

    wrapped = _build_cron_callback_spine(base_on_cron, emitter_spy)

    job = SimpleNamespace(id="j7", name="standup", payload=SimpleNamespace(channel=None))
    await wrapped(job)

    emitter_spy.emit.assert_awaited_once()
    event = emitter_spy.emit.await_args.args[1]
    assert event["type"] == "cron.delivered"
    assert event["payload"]["job_id"] == "j7"
    assert event["payload"]["name"] == "standup"
    assert event["payload"]["text"] == "reminder body"
    assert event["payload"]["fired_at"]  # stamped by the wrapper

    # No reply read back: base runs (side-effects) but nothing is fanned out.
    emitter_spy.emit.reset_mock()
    silent = SimpleNamespace(id="j8", name="silent", payload=SimpleNamespace(channel=None))
    await wrapped(silent)
    emitter_spy.emit.assert_not_awaited()


async def test_cron_callback_spine_fans_out_only_tui_jobs(emitter_spy: MagicMock) -> None:
    """On the gateway (default_channel is an IM/web surface) the fan-out SHALL
    follow the job's resolved channel: a tui job echoes to the page sessions, a
    channel-addressed job is already delivered by the hub on its own channel
    and SHALL NOT be echoed a second time."""
    from types import SimpleNamespace

    from raven.cli.tui_commands import _build_cron_callback_spine

    async def base_on_cron(job):
        return "reminder body"

    wrapped = _build_cron_callback_spine(base_on_cron, emitter_spy, default_channel="cli")

    tui_job = SimpleNamespace(id="j1", name="page", payload=SimpleNamespace(channel="tui"))
    await wrapped(tui_job)
    emitter_spy.emit.assert_awaited_once()

    emitter_spy.emit.reset_mock()
    feishu_job = SimpleNamespace(id="j2", name="im", payload=SimpleNamespace(channel="feishu"))
    assert await wrapped(feishu_job) == "reminder body"  # delivery itself untouched
    emitter_spy.emit.assert_not_awaited()

    # No channel on the payload resolves to the host's default -- not tui on
    # the gateway, so no fan-out there either.
    unaddressed = SimpleNamespace(id="j3", name="plain", payload=SimpleNamespace(channel=None))
    await wrapped(unaddressed)
    emitter_spy.emit.assert_not_awaited()


# ---------------------------------------------------------------------------
# cron.missed — startup notice for reminders dropped by the startup recompute
# ---------------------------------------------------------------------------


def test_cron_missed_event_pydantic_validates() -> None:
    """``CronMissedEvent`` SHALL be a member of the ``TurnEvent`` discriminated
    union with payload {count, items: [{name, scheduled_at, message}]}."""
    from raven.rpc.models import CronMissedEvent

    event = CronMissedEvent(
        type="cron.missed",
        payload={
            "count": 2,
            "items": [
                {"name": "hydrate", "scheduled_at": "2026-06-04T10:23:00+00:00", "message": "记得喝水"},
                {"name": "stretch", "scheduled_at": "2026-06-04T11:00:00+00:00", "message": "起来活动一下"},
            ],
        },
    )
    assert event.type == "cron.missed"
    assert event.payload.count == 2
    assert event.payload.items[0].name == "hydrate"
    assert event.payload.items[0].scheduled_at == "2026-06-04T10:23:00+00:00"
    assert event.payload.items[1].message == "起来活动一下"


def test_cron_missed_event_in_turn_event_union() -> None:
    from pydantic import TypeAdapter

    from raven.rpc.models import CronMissedEvent, TurnEvent

    adapter = TypeAdapter(TurnEvent)
    parsed = adapter.validate_python(
        {
            "type": "cron.missed",
            "payload": {
                "count": 1,
                "items": [{"name": "meds", "scheduled_at": "2026-06-04T08:00:00+00:00", "message": "吃药"}],
            },
        }
    )
    assert isinstance(parsed, CronMissedEvent)


def _drop(name: str, message: str, at_ms: int):
    from raven.proactive_engine.schedulers.cron.types import CronStartupDrop

    return CronStartupDrop(name=name, message=message, at_ms=at_ms)


async def test_fanout_cron_missed_active_session(emitter_spy: MagicMock) -> None:
    """With an attached session, ``_fanout_cron_missed`` SHALL emit one
    cron.missed event carrying every drop, scheduled_at as ISO-8601 UTC."""
    from raven.cli.tui_commands import _fanout_cron_missed

    await _fanout_cron_missed(
        emitter_spy,
        drops=[
            _drop("hydrate", "记得喝水", 1_749_031_380_000),
            _drop("stretch", "起来活动一下", 1_749_033_600_000),
        ],
    )

    emitter_spy.emit.assert_awaited_once()
    session_key, event = emitter_spy.emit.await_args.args
    assert session_key == "sess_user_default"
    assert event["type"] == "cron.missed"
    assert event["payload"]["count"] == 2
    assert [i["name"] for i in event["payload"]["items"]] == ["hydrate", "stretch"]
    assert event["payload"]["items"][0]["message"] == "记得喝水"
    assert event["payload"]["items"][0]["scheduled_at"] == "2025-06-04T10:03:00+00:00"


async def test_fanout_cron_missed_no_session_queues_startup_event() -> None:
    """With no subscription yet (server bring-up precedes turn.subscribe), the
    event SHALL be queued on the emitter for the first registration instead of
    being dropped like the cron.delivered no-subscriber no-op."""
    from raven.cli.tui_commands import _fanout_cron_missed

    emitter = MagicMock()
    emitter._by_session = {}
    emitter.emit = AsyncMock()

    await _fanout_cron_missed(emitter, drops=[_drop("meds", "吃药", 1_749_024_000_000)])

    emitter.emit.assert_not_awaited()
    emitter.queue_startup_event.assert_called_once()
    event = emitter.queue_startup_event.call_args.args[0]
    assert event["type"] == "cron.missed"
    assert event["payload"]["count"] == 1
    assert event["payload"]["items"][0] == {
        "name": "meds",
        "scheduled_at": "2025-06-04T08:00:00+00:00",
        "message": "吃药",
    }


def test_every_host_that_starts_cron_surfaces_the_startup_drops() -> None:
    """A reminder dropped at startup has to be told to whoever is listening.

    Discovered rather than listed: any file that starts a cron service is held to
    the property. The earlier version of this test named two files and passed
    while a third host violated it, which is the kind of green that reads as
    covered when it is not.

    ``EXEMPT`` is the honest half. ``gateway_commands.py`` starts a cron service
    partitioned over the enabled IM channels, so a past-due WhatsApp one-shot
    lands in *its* drops and nothing surfaces them -- it wires
    ``on_missed_foreign``, which is the opposite set (``list_missed_foreign_oneshots``
    skips every job the runner owns). Whether a host with no attached client
    should surface them at all is a design question, so it is written down here
    rather than silently passing.

    Structural on purpose, and worth being clear about the limit: this asserts
    the call is present after ``start()``, not that it runs. It would pass with
    the block under ``if False`` or with the guard inverted. The behaviour of
    ``_fanout_cron_missed`` itself is covered above; what this catches is a new
    serve path that forgets to call it, which is the regression that already
    happened once.
    """
    from pathlib import Path

    # host -> why it does not surface its own drops
    EXEMPT = {
        "cli/gateway_commands.py": (
            "cron is partitioned over the enabled IM channels here and the host has "
            "no attached client; it surfaces other partitions' misses via "
            "on_missed_foreign instead. Its own drops are an open design question."
        ),
    }

    root = Path(__file__).resolve().parents[1] / "raven"
    starters = []
    for path in sorted(root.rglob("*.py")):
        src = path.read_text(encoding="utf-8")
        marker = next(
            (m for m in ("await agent_loop.cron_service.start()", "await cron.start()") if m in src),
            None,
        )
        if marker is None:
            continue
        starters.append((path.relative_to(root).as_posix(), src, marker))

    assert starters, "no cron start site found -- the markers this test greps for have moved"

    for rel, src, marker in starters:
        if rel in EXEMPT:
            continue
        tail = src[src.index(marker) :]
        assert "last_startup_drops" in tail, f"{rel} starts cron without reading last_startup_drops"
        assert "_fanout_cron_missed" in tail, f"{rel} reads the drops without fanning them out"

    stale = sorted(set(EXEMPT) - {rel for rel, _, _ in starters})
    assert not stale, f"EXEMPT names a file that no longer starts cron: {stale}"
