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

    job = SimpleNamespace(id="j7", name="standup")
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
    silent = SimpleNamespace(id="j8", name="silent")
    await wrapped(silent)
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
