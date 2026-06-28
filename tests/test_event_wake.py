"""Tests for the event-driven heartbeat wake stack.

Covers the three new pieces and their composition:
  - ``SystemEventQueue`` — peek/ack two-phase consumption, seq watermark,
    context-key dedup, bounded drop.
  - ``WakeScheduler`` — coalescing, busy deferral, turn-complete re-fire.
  - ``HeartbeatService`` loop — wake-triggered ticks, spurious-wake
    short-circuit (no LLM call), failure-keeps-events, legacy
    interval-only equivalence.
  - ``make_on_cron_job`` producer wiring.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from raven.proactive_engine.system_events import SystemEvent, SystemEventQueue
from raven.proactive_engine.schedulers.heartbeat.service import HeartbeatService
from raven.proactive_engine.wake import WakeScheduler

# ---------------------------------------------------------------------------
# SystemEventQueue
# ---------------------------------------------------------------------------


def test_queue_peek_does_not_consume():
    q = SystemEventQueue()
    q.enqueue(SystemEvent(text="a", source="test"))
    assert len(q.peek_all()) == 1
    assert len(q.peek_all()) == 1  # still there


def test_queue_ack_removes_only_seen_events():
    q = SystemEventQueue()
    q.enqueue(SystemEvent(text="a", source="test"))
    snapshot = q.peek_all()
    q.enqueue(SystemEvent(text="b", source="test"))  # arrives mid-tick
    q.ack(snapshot)
    remaining = q.peek_all()
    assert [e.text for e in remaining] == ["b"]


def test_queue_context_key_replacement_survives_inflight_ack():
    """A replacement under the same context_key gets a fresh seq, so an
    in-flight tick's ack cannot delete the newer payload."""
    q = SystemEventQueue()
    q.enqueue(SystemEvent(text="old", source="cron", context_key="cron:j1"))
    snapshot = q.peek_all()
    q.enqueue(SystemEvent(text="new", source="cron", context_key="cron:j1"))
    q.ack(snapshot)
    remaining = q.peek_all()
    assert [e.text for e in remaining] == ["new"]


def test_queue_context_key_dedup_keeps_single_entry():
    q = SystemEventQueue()
    q.enqueue(SystemEvent(text="v1", source="cron", context_key="cron:j1"))
    q.enqueue(SystemEvent(text="v2", source="cron", context_key="cron:j1"))
    events = q.peek_all()
    assert len(events) == 1
    assert events[0].text == "v2"


def test_queue_discard_removes_by_context_key():
    q = SystemEventQueue()
    q.enqueue(SystemEvent(text="boom", source="cron", context_key="cron:j1:fail"))
    q.enqueue(SystemEvent(text="other", source="cron", context_key="cron:j2"))
    assert q.discard("cron:j1:fail") is True
    assert q.discard("cron:j1:fail") is False  # already gone
    assert [e.context_key for e in q.peek_all()] == ["cron:j2"]


def test_queue_bounded_drops_oldest():
    q = SystemEventQueue(max_events=2)
    q.enqueue(SystemEvent(text="a", source="test"))
    q.enqueue(SystemEvent(text="b", source="test"))
    q.enqueue(SystemEvent(text="c", source="test"))
    assert [e.text for e in q.peek_all()] == ["b", "c"]


# ---------------------------------------------------------------------------
# WakeScheduler
# ---------------------------------------------------------------------------


async def test_wake_coalesces_multiple_requests():
    wake = WakeScheduler(coalesce_s=0.02)
    wake.request_wake_now("r1")
    wake.request_wake_now("r2")
    await asyncio.sleep(0.05)
    assert wake.wake_event.is_set()
    assert wake.consume_reasons() == ["r1", "r2"]


async def test_wake_deferred_while_busy_refires_on_turn_complete():
    busy = True
    wake = WakeScheduler(is_busy=lambda: busy, coalesce_s=0.02)
    wake.request_wake_now("r1")
    await asyncio.sleep(0.05)
    assert not wake.wake_event.is_set()  # parked

    busy = False
    wake.on_turn_complete()
    await asyncio.sleep(0.05)
    assert wake.wake_event.is_set()


async def test_wake_turn_complete_noop_without_pending():
    wake = WakeScheduler(coalesce_s=0.02)
    wake.on_turn_complete()
    await asyncio.sleep(0.05)
    assert not wake.wake_event.is_set()


async def test_wake_min_interval_first_fire_immediate():
    """The guard spaces consecutive fires; it must not delay the first one."""
    wake = WakeScheduler(coalesce_s=0.01, min_interval_s=10.0)
    wake.request_wake_now("r1")
    await asyncio.sleep(0.05)
    assert wake.wake_event.is_set()


async def test_wake_min_interval_defers_and_accumulates():
    wake = WakeScheduler(coalesce_s=0.01, min_interval_s=0.2)
    wake.request_wake_now("r1")
    await asyncio.sleep(0.05)
    assert wake.wake_event.is_set()
    wake.wake_event.clear()
    assert wake.consume_reasons() == ["r1"]

    # Second and third requests inside the guard window: no fire yet,
    # reasons accumulate.
    wake.request_wake_now("r2")
    wake.request_wake_now("r3")
    await asyncio.sleep(0.05)
    assert not wake.wake_event.is_set()

    # One fire at the window boundary, carrying both reasons.
    await asyncio.sleep(0.25)
    assert wake.wake_event.is_set()
    assert wake.consume_reasons() == ["r2", "r3"]


async def test_wake_min_interval_zero_disables_guard():
    wake = WakeScheduler(coalesce_s=0.01, min_interval_s=0.0)
    wake.request_wake_now("r1")
    await asyncio.sleep(0.05)
    wake.wake_event.clear()
    wake.consume_reasons()
    wake.request_wake_now("r2")
    await asyncio.sleep(0.05)
    assert wake.wake_event.is_set()


# ---------------------------------------------------------------------------
# HeartbeatService — event wake loop
# ---------------------------------------------------------------------------


class FakeProvider:
    """LLM provider stub: phase-1 always answers with a fixed decision."""

    def __init__(self, action: str = "skip", tasks: str = ""):
        self.action = action
        self.tasks = tasks
        self.calls: list[list[dict]] = []

    async def chat_with_retry(self, messages, tools, model):
        self.calls.append(messages)
        return SimpleNamespace(
            has_tool_calls=True,
            tool_calls=[
                SimpleNamespace(arguments={"action": self.action, "tasks": self.tasks})
            ],
        )


def _make_service(
    workspace: Path,
    provider: FakeProvider,
    *,
    wake: WakeScheduler | None = None,
    queue: SystemEventQueue | None = None,
    on_execute=None,
    interval_s: int = 60,
) -> HeartbeatService:
    return HeartbeatService(
        workspace=workspace,
        provider=provider,  # type: ignore[arg-type]
        model="test-model",
        on_execute=on_execute,
        interval_s=interval_s,
        enabled=True,
        wake=wake,
        system_events=queue,
    )


async def _run_briefly(service: HeartbeatService, seconds: float) -> None:
    await service.start()
    try:
        await asyncio.sleep(seconds)
    finally:
        service.stop()
        await asyncio.sleep(0)  # let cancellation propagate


async def test_wake_tick_sees_events_and_acks(tmp_path: Path):
    provider = FakeProvider(action="skip")
    wake = WakeScheduler(coalesce_s=0.01)
    queue = SystemEventQueue()
    service = _make_service(tmp_path, provider, wake=wake, queue=queue)

    await service.start()
    try:
        queue.enqueue(SystemEvent(text="cron job done", source="cron"))
        wake.request_wake_now("cron:j1")
        await asyncio.sleep(0.1)
    finally:
        service.stop()

    assert len(provider.calls) == 1
    prompt = provider.calls[0][1]["content"]
    assert "cron job done" in prompt
    # skip is a successful decision — events are consumed.
    assert len(queue) == 0


async def test_spurious_wake_costs_no_llm_call(tmp_path: Path):
    provider = FakeProvider()
    wake = WakeScheduler(coalesce_s=0.01)
    queue = SystemEventQueue()
    service = _make_service(tmp_path, provider, wake=wake, queue=queue)

    await service.start()
    try:
        wake.request_wake_now("phantom")  # no event enqueued
        await asyncio.sleep(0.1)
    finally:
        service.stop()

    assert provider.calls == []


async def test_failed_tick_keeps_events_queued(tmp_path: Path):
    provider = FakeProvider(action="run", tasks="do the thing")
    wake = WakeScheduler(coalesce_s=0.01)
    queue = SystemEventQueue()
    on_execute = AsyncMock(side_effect=RuntimeError("boom"))
    service = _make_service(
        tmp_path, provider, wake=wake, queue=queue, on_execute=on_execute
    )

    await service.start()
    try:
        queue.enqueue(SystemEvent(text="needs follow-up", source="cron"))
        wake.request_wake_now("cron:j1")
        await asyncio.sleep(0.1)
    finally:
        service.stop()

    on_execute.assert_awaited()
    # Failure must NOT ack — the event survives for the next attempt.
    assert len(queue) == 1


async def test_run_decision_executes_and_acks(tmp_path: Path):
    provider = FakeProvider(action="run", tasks="follow up on cron")
    wake = WakeScheduler(coalesce_s=0.01)
    queue = SystemEventQueue()
    on_execute = AsyncMock(return_value="done")
    service = _make_service(
        tmp_path, provider, wake=wake, queue=queue, on_execute=on_execute
    )

    await service.start()
    try:
        queue.enqueue(SystemEvent(text="report ready", source="cron"))
        wake.request_wake_now("cron:j1")
        await asyncio.sleep(0.1)
    finally:
        service.stop()

    on_execute.assert_awaited_once_with("follow up on cron")
    assert len(queue) == 0


async def test_legacy_interval_mode_unchanged(tmp_path: Path):
    """Without wake/queue collaborators the service ticks on the interval."""
    (tmp_path / "HEARTBEAT.md").write_text("- check things", encoding="utf-8")
    provider = FakeProvider(action="skip")
    service = _make_service(tmp_path, provider, interval_s=0)

    await _run_briefly(service, 0.1)

    assert len(provider.calls) >= 1


async def test_interval_tick_skips_when_no_file_and_no_events(tmp_path: Path):
    provider = FakeProvider()
    service = _make_service(tmp_path, provider, interval_s=0)

    await _run_briefly(service, 0.1)

    assert provider.calls == []


async def test_trigger_now_consumes_events(tmp_path: Path):
    provider = FakeProvider(action="skip")
    queue = SystemEventQueue()
    service = _make_service(tmp_path, provider, queue=queue)
    queue.enqueue(SystemEvent(text="manual check", source="manual"))

    result = await service.trigger_now()

    assert result is None  # skip decision
    assert len(provider.calls) == 1
    assert len(queue) == 0


# ---------------------------------------------------------------------------
# Producer wiring — make_on_cron_job
# ---------------------------------------------------------------------------


def _make_job():
    from raven.proactive_engine.schedulers.cron.types import (
        CronJob,
        CronJobState,
        CronPayload,
        CronSchedule,
    )

    return CronJob(
        id="job_wake",
        name="wake_test",
        enabled=True,
        schedule=CronSchedule(kind="at", at_ms=1000),
        payload=CronPayload(
            kind="agent_turn",
            message="reminder body",
            deliver=False,
            channel="cli",
            to="direct",
        ),
        state=CronJobState(),
    )


def _spine_submit(outcomes: list):
    """Spine submit mock + readback map. ``outcomes`` is consumed one per fire:
    an Exception → result() raises (cron failure path); a str → stored in
    readback under req.conversation (mimics the gateway capturing runner) so the
    handler reads it back for the system event."""
    readback: dict[str, str] = {}
    it = iter(outcomes)

    def _submit(req):
        outcome = next(it)

        class _Handle:
            async def result(self_inner):
                if isinstance(outcome, Exception):
                    raise outcome
                readback[req.conversation] = outcome
                return None

        return _Handle()

    return _submit, readback


async def test_cron_completion_enqueues_event_and_wakes():
    from raven.cli._cron_handler import make_on_cron_job

    agent = MagicMock()
    hub = MagicMock()
    submit, readback = _spine_submit(["cron result text"])

    queue = SystemEventQueue()
    wake = WakeScheduler(coalesce_s=0.01)

    on_job = make_on_cron_job(
        agent, hub, submit=submit, readback_texts=readback, system_events=queue, wake=wake
    )
    await on_job(_make_job())

    events = queue.peek_all()
    assert len(events) == 1
    assert events[0].source == "cron"
    assert events[0].context_key == "cron:job_wake"
    assert "wake_test" in events[0].text
    assert "cron result text" in events[0].text

    await asyncio.sleep(0.05)
    assert wake.wake_event.is_set()
    assert wake.consume_reasons() == ["cron:job_wake"]


async def test_cron_failure_enqueues_failure_event_and_reraises():
    from raven.cli._cron_handler import make_on_cron_job

    agent = MagicMock()
    hub = MagicMock()
    submit, readback = _spine_submit([RuntimeError("provider down")])

    queue = SystemEventQueue()
    wake = WakeScheduler(coalesce_s=0.01)

    on_job = make_on_cron_job(
        agent, hub, submit=submit, readback_texts=readback, system_events=queue, wake=wake
    )
    with pytest.raises(RuntimeError, match="provider down"):
        await on_job(_make_job())

    events = queue.peek_all()
    assert len(events) == 1
    assert events[0].source == "cron"
    assert events[0].context_key == "cron:job_wake:fail"
    assert "failed" in events[0].text
    assert "provider down" in events[0].text

    await asyncio.sleep(0.05)
    assert wake.wake_event.is_set()
    assert wake.consume_reasons() == ["cron:job_wake:fail"]


async def test_cron_recovery_drops_stale_failure_event():
    """A successful retry discards the job's pending :fail event — a
    recovered flake must not drive a user-facing follow-up. The reverse
    (success pending, then failure) keeps both: the failure is news."""
    from raven.cli._cron_handler import make_on_cron_job

    agent = MagicMock()
    hub = MagicMock()
    submit, readback = _spine_submit([RuntimeError("flaky"), "recovered fine"])

    queue = SystemEventQueue()
    wake = WakeScheduler(coalesce_s=0.01)

    on_job = make_on_cron_job(
        agent, hub, submit=submit, readback_texts=readback, system_events=queue, wake=wake
    )
    with pytest.raises(RuntimeError):
        await on_job(_make_job())
    assert [e.context_key for e in queue.peek_all()] == ["cron:job_wake:fail"]

    await on_job(_make_job())

    keys = [e.context_key for e in queue.peek_all()]
    assert keys == ["cron:job_wake"]


async def test_cron_failure_after_success_keeps_both_events():
    from raven.cli._cron_handler import make_on_cron_job

    agent = MagicMock()
    hub = MagicMock()
    submit, readback = _spine_submit(["all good", RuntimeError("broke later")])

    queue = SystemEventQueue()
    wake = WakeScheduler(coalesce_s=0.01)

    on_job = make_on_cron_job(
        agent, hub, submit=submit, readback_texts=readback, system_events=queue, wake=wake
    )
    await on_job(_make_job())
    with pytest.raises(RuntimeError):
        await on_job(_make_job())

    keys = [e.context_key for e in queue.peek_all()]
    assert keys == ["cron:job_wake", "cron:job_wake:fail"]


async def test_cron_event_emit_is_best_effort():
    """A broken queue must not fail a successful run, and on the failure
    path it must not mask the original cron error."""
    from raven.cli._cron_handler import make_on_cron_job

    agent = MagicMock()
    hub = MagicMock()
    submit, readback = _spine_submit(["fine", RuntimeError("the real error")])

    queue = MagicMock()
    queue.discard.side_effect = ValueError("queue broken")
    queue.enqueue.side_effect = ValueError("queue broken")
    wake = WakeScheduler(coalesce_s=0.01)

    on_job = make_on_cron_job(
        agent, hub, submit=submit, readback_texts=readback, system_events=queue, wake=wake
    )
    assert await on_job(_make_job()) == "fine"
    with pytest.raises(RuntimeError, match="the real error"):
        await on_job(_make_job())


async def test_cron_without_wake_wiring_unchanged():
    from raven.cli._cron_handler import make_on_cron_job

    agent = MagicMock()
    hub = MagicMock()
    submit, readback = _spine_submit(["resolved body"])

    on_job = make_on_cron_job(agent, hub, submit=submit, readback_texts=readback)
    result = await on_job(_make_job())
    assert result == "resolved body"
