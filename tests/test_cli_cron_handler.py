"""Trigger-path tests for ``make_on_cron_job`` (raven/core/cron_stack.py).

Distinct scope from ``test_cron_handler_ledger.py``, which covers the
sentinel ledger write side-effect.

Fire-at-origin contract: every cron turn runs through the spine ``submit``
with the job's creation-time binding ``(payload.channel, payload.to)`` as
the request source — the hub routes the reply to that one outlet. There is
no trigger-time resolution, forwarding, or broadcast to test anymore; what
matters is the source binding, the read-back into the system event, and the
failure path.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from raven.core.cron_stack import make_on_cron_job
from raven.proactive_engine.schedulers.cron.types import (
    CronJob,
    CronJobState,
    CronPayload,
    CronSchedule,
)
from raven.spine import Origin


def _make_job(
    *,
    channel: str | None = "tui",
    to: str | None = "direct",
    name: str = "test_job",
    kind: str = "at",
) -> CronJob:
    schedule = (
        CronSchedule(kind="at", at_ms=1000)
        if kind == "at"
        else CronSchedule(kind="every", every_ms=60_000)
        if kind == "every"
        else CronSchedule(kind="cron", expr="0 9 * * *")
    )
    return CronJob(
        id=f"job_{name}",
        name=name,
        enabled=True,
        schedule=schedule,
        payload=CronPayload(
            message="reminder body source",
            channel=channel,
            to=to,
        ),
        state=CronJobState(),
    )


@pytest.fixture
def spine() -> SimpleNamespace:
    """Spine submit mock + readback map, mimicking the gateway capturing runner:
    submit records the request and stores the reply text under req.conversation
    before result() resolves (so the handler reads it back)."""
    readback: dict[str, str] = {}
    captured: list = []

    class _Handle:
        async def result(self):
            return object()  # a completed turn resolves with its outcome; None means it was cut

    def _submit(req):
        captured.append(req)
        readback[req.conversation] = "resolved body"
        return _Handle()

    return SimpleNamespace(submit=_submit, readback=readback, captured=captured)


# ─────────────────────────────────────────────────────────────────────
# Source binding: the stored (channel, to) IS the delivery target
# ─────────────────────────────────────────────────────────────────────


async def test_source_binding_is_the_delivery_target(spine):
    handler = make_on_cron_job(submit=spine.submit, readback_texts=spine.readback)

    response = await handler(_make_job(channel="telegram", to="tg_user_1", name="b1"))

    assert len(spine.captured) == 1
    req = spine.captured[0]
    assert req.origin is Origin.CRON
    assert req.source.channel == "telegram"
    assert req.source.chat_id == "tg_user_1"
    assert req.conversation == "cron:job_b1"
    assert response == "resolved body"


async def test_tui_binding_passes_through_unchanged(spine):
    handler = make_on_cron_job(submit=spine.submit, readback_texts=spine.readback, default_channel="tui")

    await handler(_make_job(channel="tui", to="default", name="b2"))

    req = spine.captured[0]
    assert req.source.channel == "tui"
    assert req.source.chat_id == "default"


async def test_legacy_job_without_channel_uses_default(spine):
    handler = make_on_cron_job(submit=spine.submit, readback_texts=spine.readback)

    await handler(_make_job(channel=None, to=None, name="b3"))

    req = spine.captured[0]
    assert req.source.channel == "tui"
    assert req.source.chat_id == "direct"


async def test_reminder_note_carries_schedule_origin(spine):
    handler = make_on_cron_job(submit=spine.submit, readback_texts=spine.readback)

    await handler(_make_job(name="b4"))

    text = spine.captured[0].text
    assert "Scheduled instruction: reminder body source" in text
    assert "set at" in text


# ─────────────────────────────────────────────────────────────────────
# Read-back into the system event
# ─────────────────────────────────────────────────────────────────────


async def test_spine_path_reads_back_reply_into_system_event():
    system_events = MagicMock()
    wake = MagicMock()
    readback_texts: dict[str, str] = {}
    captured: dict[str, object] = {}

    class _Handle:
        async def result(self):
            # The gateway runner stores the reply before result() resolves.
            readback_texts["cron:job_t1"] = "reminder done at 17:05"
            return object()  # a completed turn resolves with its outcome; None means it was cut

    def _submit(req):
        captured["req"] = req
        return _Handle()

    handler = make_on_cron_job(
        submit=_submit,
        readback_texts=readback_texts,
        system_events=system_events,
        wake=wake,
    )

    await handler(_make_job(channel="telegram", to="c1", name="t1"))

    assert captured["req"].origin is Origin.CRON
    assert captured["req"].conversation == "cron:job_t1"
    # Read back into the system event, then popped (no leak in the long-running map).
    system_events.enqueue.assert_called_once()
    assert "reminder done at 17:05" in system_events.enqueue.call_args.args[0].text
    assert "cron:job_t1" not in readback_texts
    wake.request_wake_now.assert_called_once()


async def test_spine_path_no_reply_falls_back_to_no_response():
    system_events = MagicMock()
    wake = MagicMock()

    class _Handle:
        async def result(self):
            return object()  # a completed turn resolves with its outcome; None means it was cut

    handler = make_on_cron_job(
        submit=lambda req: _Handle(),
        readback_texts={},
        system_events=system_events,
        wake=wake,
    )

    await handler(_make_job(channel="telegram", to="c1", name="t2"))

    system_events.enqueue.assert_called_once()
    assert "(no response)" in system_events.enqueue.call_args.args[0].text


# ─────────────────────────────────────────────────────────────────────
# Failure path
# ─────────────────────────────────────────────────────────────────────


async def test_turn_failure_emits_failed_event_and_reraises():
    system_events = MagicMock()
    wake = MagicMock()

    class _Handle:
        async def result(self):
            raise RuntimeError("provider down")

    handler = make_on_cron_job(
        submit=lambda req: _Handle(),
        readback_texts={},
        system_events=system_events,
        wake=wake,
    )

    with pytest.raises(RuntimeError, match="provider down"):
        await handler(_make_job(name="f1"))

    system_events.enqueue.assert_called_once()
    event = system_events.enqueue.call_args.args[0]
    assert "failed" in event.text
    assert event.context_key.endswith(":fail")


# ─────────────────────────────────────────────────────────────────────
# Anti-runaway count point: record_fire after a successful recurring turn
# ─────────────────────────────────────────────────────────────────────


async def test_record_fire_called_on_successful_recurring_turn(spine):
    cron_service = MagicMock()
    cron_service.record_fire.return_value = False
    handler = make_on_cron_job(submit=spine.submit, readback_texts=spine.readback, cron_service=cron_service)

    job = _make_job(name="r1", kind="every")
    await handler(job)

    cron_service.record_fire.assert_called_once_with("job_r1")
    assert job.enabled is True


async def test_record_fire_not_called_on_turn_failure():
    cron_service = MagicMock()

    class _Handle:
        async def result(self):
            raise RuntimeError("provider down")

    handler = make_on_cron_job(submit=lambda req: _Handle(), readback_texts={}, cron_service=cron_service)

    with pytest.raises(RuntimeError, match="provider down"):
        await handler(_make_job(name="r2", kind="every"))

    cron_service.record_fire.assert_not_called()


async def test_record_fire_not_called_for_oneshot_at_job(spine):
    cron_service = MagicMock()
    handler = make_on_cron_job(submit=spine.submit, readback_texts=spine.readback, cron_service=cron_service)

    await handler(_make_job(name="r3", kind="at"))

    cron_service.record_fire.assert_not_called()


async def test_auto_disable_flips_the_inflight_job(spine):
    """record_fire returning True (limit hit) must flip the in-memory job:
    the service's post-run writeback patches enabled/next_run from it and
    would otherwise clobber the persisted disable."""
    cron_service = MagicMock()
    cron_service.record_fire.return_value = True
    handler = make_on_cron_job(submit=spine.submit, readback_texts=spine.readback, cron_service=cron_service)

    job = _make_job(name="r4", kind="cron")
    job.state.next_run_at_ms = 999_999
    await handler(job)

    assert job.enabled is False
    assert job.state.next_run_at_ms is None


async def test_record_fire_error_does_not_fail_the_turn(spine):
    cron_service = MagicMock()
    cron_service.record_fire.side_effect = OSError("store locked")
    handler = make_on_cron_job(submit=spine.submit, readback_texts=spine.readback, cron_service=cron_service)

    response = await handler(_make_job(name="r5", kind="every"))

    assert response == "resolved body"


async def test_without_cron_service_no_counting(spine):
    handler = make_on_cron_job(submit=spine.submit, readback_texts=spine.readback)
    job = _make_job(name="r6", kind="every")

    assert await handler(job) == "resolved body"
    assert job.enabled is True


# ─────────────────────────────────────────────────────────────────────
# Anti-runaway reset point: chain_cron_activity_reset
# ─────────────────────────────────────────────────────────────────────


def _user_req(*, origin=None, channel: str = "telegram", chat_id: str = "chat9"):
    from raven.spine import ChatType, Origin, Source, TurnRequest

    return TurnRequest(
        origin=origin or Origin.USER,
        source=Source(
            channel=channel,
            chat_id=chat_id,
            sender_id="u1",
            chat_type=ChatType.DM,
        ),
        text="hello",
    )


def test_chain_resets_on_user_origin():
    from raven.core.cron_stack import chain_cron_activity_reset

    cron_service = MagicMock()
    hook = chain_cron_activity_reset(cron_service)

    hook(_user_req(channel="telegram", chat_id="chat9"))

    cron_service.notify_user_active.assert_called_once_with("telegram", "chat9")


def test_chain_ignores_non_user_origins():
    """CRON / HEARTBEAT turns run the user-inbound hook chain too (only
    SENTINEL / SUBAGENT are skipped at the AgentLoop gate) — a cron fire
    resetting its own counter would defeat the guard."""
    from raven.core.cron_stack import chain_cron_activity_reset
    from raven.spine import Origin

    cron_service = MagicMock()
    hook = chain_cron_activity_reset(cron_service)

    hook(_user_req(origin=Origin.CRON))
    hook(_user_req(origin=Origin.HEARTBEAT))

    cron_service.notify_user_active.assert_not_called()


def test_chain_calls_inner_first_and_returns_its_result():
    from raven.core.cron_stack import chain_cron_activity_reset

    calls: list[str] = []
    cron_service = MagicMock()
    cron_service.notify_user_active.side_effect = lambda *a: calls.append("reset")
    sentinel_result = object()

    def inner(req):
        calls.append("sentinel")
        return sentinel_result

    hook = chain_cron_activity_reset(cron_service, inner=inner)

    assert hook(_user_req()) is sentinel_result
    assert calls == ["sentinel", "reset"]


def test_chain_reset_error_is_swallowed():
    from raven.core.cron_stack import chain_cron_activity_reset

    cron_service = MagicMock()
    cron_service.notify_user_active.side_effect = OSError("store locked")
    inner = MagicMock(return_value=None)
    hook = chain_cron_activity_reset(cron_service, inner=inner)

    assert hook(_user_req()) is None
    inner.assert_called_once()


# ─────────────────────────────────────────────────────────────────────
# Interactive-surface assembly: cron wired with submit=a spine scheduler
# whose hub owns the "tui" outlet. A tui-bound job renders once via that
# outlet — the source binding is the outlet, nothing else fires.
# ─────────────────────────────────────────────────────────────────────


async def test_interactive_assembly_cron_renders_once_via_outlet():
    from raven.cli._repl_spine import build_repl
    from raven.spine import Text, TurnOutcome, Usage

    class _CronEchoLoop:
        async def run_turn(self, req, emit, drain, *, stream, inline_tool_stream=False) -> TurnOutcome:
            await emit(Text(content=f"cron-reply<{req.conversation}>", source=req.source))
            return TurnOutcome(usage=Usage(0, 0, 0), explicit_reply=True)

    rendered: list[str] = []
    scheduler, hub, teardown = build_repl(_CronEchoLoop(), "tui", rendered.append)
    handler = make_on_cron_job(submit=scheduler.submit)

    try:
        await handler(_make_job(channel="tui", to="direct", name="tui1"))
        await hub.wait_idle("tui")
    finally:
        await teardown()

    assert rendered == ["cron-reply<cron:job_tui1>"]


# ─────────────────────────────────────────────────────────────────────
# A wake addressed to a sub-agent instance runs as that instance's turn
# ─────────────────────────────────────────────────────────────────────


def _instance_job(*, agent: str = "Raven-Oncall", handle: str = "inst-7", name: str = "d1") -> CronJob:
    job = _make_job(channel="tui", to="default", name=name)
    job.payload.message = "[Ops campaign 'beam-limit-load' round 2 due] Call ops_tune_status(...)"
    job.payload.direct_agent = agent
    job.payload.direct_handle = handle
    return job


async def test_a_wake_naming_an_instance_runs_as_that_instance_turn(spine):
    """The gap this closes, measured 2026-08-25 through ``/new-instance``.

    An on-call wake's message is addressed to the agent holding the campaign --
    "call ops_tune_status on this ledger, then decide". Handed to the main agent
    as a reminder it can only be paraphrased, because the main agent has no ops
    tools at all. ``direct_target`` puts the round in the instance that owns the
    campaign instead, on that instance's own lane, which is the lane the pane the
    operator is looking at subscribes to.
    """
    handler = make_on_cron_job(submit=spine.submit, readback_texts=spine.readback, default_channel="tui")

    response = await handler(_instance_job())

    req = spine.captured[0]
    assert req.direct_target == ("Raven-Oncall", "inst-7")
    # Derived from this job's own (channel, to), not hardcoded: the direct lane
    # hangs off the session key the main-agent lane for this outlet uses, which
    # is what lets RpcOutlet map it back to the client's one subscription.
    assert req.conversation == "tui:default#Raven-Oncall/inst-7"
    # Read back off the lane the request actually used, not "cron:<job id>".
    assert response == "resolved body"


async def test_the_instance_gets_the_wake_message_not_the_reminder_wrapper(spine):
    """A wake turn arrives with no conversation history, so its message is the
    whole of what it has. Wrapping it in "[Scheduled Task] Timer finished ... When
    you reply, mention when the reminder was originally set" would hand the
    on-call loop an instruction written for a different addressee."""
    handler = make_on_cron_job(submit=spine.submit, readback_texts=spine.readback, default_channel="tui")
    job = _instance_job(name="d2")

    await handler(job)

    assert spine.captured[0].text == job.payload.message
    assert "[Scheduled Task]" not in spine.captured[0].text


async def test_an_ordinary_reminder_is_untouched_by_the_instance_branch(spine):
    """Half a target is no target. The two fields are written together by one
    producer, and a job carrying only one of them is a reminder, not a wake --
    guessing the other half would run somebody's meds reminder as a sub-agent
    turn against an instance that may not exist."""
    handler = make_on_cron_job(submit=spine.submit, readback_texts=spine.readback, default_channel="tui")
    job = _make_job(channel="tui", to="default", name="d3")
    job.payload.direct_agent = "Raven-Oncall"  # handle missing

    await handler(job)

    req = spine.captured[0]
    assert req.direct_target is None
    assert req.conversation == "cron:job_d3"
    assert "[Scheduled Task]" in req.text


def test_every_process_opens_the_one_cron_store(monkeypatch, tmp_path) -> None:
    from raven.core.cron_stack import build_cron_service

    monkeypatch.setenv("RAVEN_HOME", str(tmp_path))
    service = build_cron_service(allowed_channels={"tui"})

    assert service.store_path == tmp_path / "cron" / "jobs.json"
    assert service.allowed_channels == {"tui"}
