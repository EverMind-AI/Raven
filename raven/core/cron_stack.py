"""Cron assembly for the runtime: the ``on_cron_job`` factory every entrance wires.

Used by the gateway, the REPL, the TUI and the rpc stack alike; it composes the
cron service with the spine (a reminder fires as a CRON-origin turn) and the
sentinel ledger, which is assembly, not transport.

A scheduled reminder fires as a CRON-origin spine turn bound to the
``cron:<job_id>`` session. Delivery is direct: the turn's source is the
job's creation-time binding ``(payload.channel, payload.to)``, so the hub
routes the reply to that one outlet — fire-at-origin, no trigger-time
re-routing.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any, Awaitable, Callable

from loguru import logger

if TYPE_CHECKING:
    from raven.proactive_engine.schedulers.cron.service import CronService
    from raven.proactive_engine.schedulers.cron.types import CronJob
    from raven.proactive_engine.sentinel.executor.runner import SentinelRunner
    from raven.proactive_engine.system_events import SystemEventQueue
    from raven.proactive_engine.wake import WakeScheduler
    from raven.spine import TurnHandle, TurnRequest

_RECURRING_KINDS = ("every", "cron")


def _ms_to_local_str(ms: int | None) -> str | None:
    """Render a ms-since-epoch timestamp as local HH:MM for user-facing text."""
    if not ms:
        return None
    try:
        return datetime.fromtimestamp(ms / 1000).strftime("%H:%M")
    except (OSError, ValueError):
        return None


def _emit_cron_event(
    system_events: "SystemEventQueue",
    wake: "WakeScheduler",
    job: "CronJob",
    detail: str,
    *,
    failed: bool,
) -> None:
    """Enqueue a cron outcome event and request an early heartbeat tick.

    Failure events use a distinct ``:fail`` context_key so a failure is not
    overwritten by a later completion event of the same job. A successful
    run discards its own pending ``:fail`` event instead: a recovered flake
    is stale by then and should not drive a user-facing follow-up — it
    remains in the cron service's error log (a successful retry resets
    ``last_error``).

    Best-effort like the ledger write: an emit failure must neither
    mask the original cron error (failure path re-raises it) nor turn a
    successful run into an error.
    """
    try:
        from raven.proactive_engine.system_events import SystemEvent

        if len(detail) > 200:
            detail = detail[:200] + "…"
        if failed:
            text = f"Cron job '{job.name}' failed: {detail}"
            context_key = f"cron:{job.id}:fail"
        else:
            system_events.discard(f"cron:{job.id}:fail")
            text = f"Cron job '{job.name}' completed. Result: {detail}"
            context_key = f"cron:{job.id}"
        system_events.enqueue(SystemEvent(text=text, source="cron", context_key=context_key))
        wake.request_wake_now(context_key)
    except Exception as exc:  # noqa: BLE001 — event emit is best-effort
        logger.warning(
            "cron event emit failed for {}: {}: {}",
            job.id,
            type(exc).__name__,
            exc,
        )


def _format_schedule_origin(job: "CronJob") -> str:
    """Describe when the reminder was originally set, for the user.

    - 'at' jobs: "set at <HH:MM>, scheduled for <HH:MM>" (at_ms is the fire time)
    - 'every' jobs: "set at <HH:MM>, recurring every <N>s"
    - 'cron' jobs: "set at <HH:MM>, cron <expr>"
    """
    created = _ms_to_local_str(job.created_at_ms) or "?"
    kind = job.schedule.kind
    if kind == "at":
        fire_at = _ms_to_local_str(job.schedule.at_ms) or "?"
        return f"set at {created}, scheduled for {fire_at}"
    if kind == "every":
        secs = (job.schedule.every_ms or 0) // 1000
        return f"set at {created}, recurring every {secs}s"
    if kind == "cron" and job.schedule.expr:
        return f"set at {created}, cron `{job.schedule.expr}`"
    return f"set at {created}"


def make_on_cron_job(
    *,
    submit: "Callable[[TurnRequest], TurnHandle]",
    readback_texts: "dict[str, str] | None" = None,
    default_channel: str = "tui",
    sentinel_runner: "SentinelRunner | None" = None,
    system_events: "SystemEventQueue | None" = None,
    wake: "WakeScheduler | None" = None,
    cron_service: "CronService | None" = None,
) -> Callable[["CronJob"], Awaitable[str | None]]:
    """Build the CronService.on_job callback. Every cron turn runs through the
    spine ``submit`` as a CRON-origin turn.

    ``submit`` (required) is the spine entry (build_gateway / build_repl /
    build_rpc_spine scheduler). The turn's source is the job's creation-time
    binding ``(payload.channel, payload.to)`` — the single delivery target.
    The hub routes the reply to that channel's outlet; there is no
    trigger-time resolution, forwarding, or broadcast.

    ``readback_texts`` is build_gateway's per-conversation reply-text map, the
    spine read-back channel for the system event: a CRON turn submits, then this
    reads back its reply from ``readback_texts[cron:<job_id>]`` (the runner stored
    it before result() resolved) and pops it. The submitter cannot pass run_turn's
    text_sink itself — text_sink is a runner-set per-call param, and cron is a
    submitter — so the gateway's capturing runner bridges it. Required whenever
    ``submit`` is wired; without it the system event sees no reply text.

    ``default_channel`` is used when the job payload doesn't specify one
    (legacy pre-attribution jobs) — every runner passes "tui" so such a
    job surfaces in the interactive TUI session (the "cli" channel value
    is retired).

    ``sentinel_runner`` is optional. When present, cron fires
    write to the shared NudgePolicy ledger (topic_fired_at +
    record_dispatched) so the L3 Sentinel suppresses its own proactive
    nudges on the same topic within the dedup window. Without this,
    Sentinel and Cron are blind to each other and the user gets double-
    nudged on the same subject (e.g. user-asked "5/25 birthday cron"
    fires AND Sentinel proactively reminds at 5/22).

    ``system_events`` / ``wake`` are optional. When wired (gateway path),
    each completed or failed cron run enqueues a system event and requests
    an early heartbeat tick, so the main heartbeat session learns what
    happened in the isolated ``cron:<job_id>`` session and can decide on
    follow-ups.
    Only effective for jobs executed in this process — a CLI test-fire
    runs in its own process and cannot reach the gateway's queue.

    ``cron_service`` is optional. When wired, each successful recurring
    fire ('every'/'cron' kinds; one-shots can't run away) is counted via
    ``record_fire`` — the anti-runaway guard that auto-disables a job
    after ``silent_fire_limit`` consecutive fires with no user activity
    on its (channel, to). Failed turns are not counted: a job that never
    delivers is a different problem than one that spams. On auto-disable
    the in-memory job is flipped too, so the service's post-run writeback
    persists the disable instead of recomputing a next run.
    """

    async def on_cron_job(job: "CronJob") -> str | None:
        from raven.spine import ChatType, Origin, Source, TurnRequest

        # Include the originally-scheduled time so the reminder text can
        # echo "set at 17:05" back to the user — otherwise the agent only
        # knows "right now".
        reminder_note = (
            "[Scheduled Task] Timer finished.\n\n"
            f"Task '{job.name}' ({_format_schedule_origin(job)}) "
            "has been triggered.\n"
            f"Scheduled instruction: {job.payload.message}\n\n"
            "When you reply, mention when the reminder was originally set "
            '(e.g. "the reminder you set at 17:05 ...") so the user remembers the '
            "context."
        )

        channel = job.payload.channel or default_channel
        chat_id = job.payload.to or "direct"
        source = Source(
            channel=channel,
            chat_id=chat_id,
            sender_id="cron",
            chat_type=ChatType.DM,
        )

        # A wake that names a sub-agent instance is that instance's turn, not a
        # reminder for the main agent. Both branches deliver to the same outlet;
        # what differs is who answers.
        #
        # The reminder shape above wraps the message and hands it to the MAIN
        # agent, whose reply is what the surface then shows. That is right for
        # "remind me to take my meds" and wrong for an on-call wake: the message
        # is addressed to the agent holding the campaign ("call ops_tune_status
        # on this ledger, then decide"), and the main agent has no ops tools, so
        # the best it can do is paraphrase. Measured 2026-08-25 on a campaign
        # hosted through /new-instance: round 1 reached the operator (it was the
        # direct turn that started the campaign) and every later round did not,
        # because the only thing firing them was a headless shell whose child
        # process wrote to a pipe nobody read.
        #
        # Sent as a direct turn instead, the round runs in the instance that owns
        # the campaign and its own words land in that instance's pane -- the same
        # path a typed message takes, so the reply, the record and the instance
        # log all happen by the ordinary mechanism rather than a second one.
        target = (
            getattr(job.payload, "direct_agent", None) or "",
            getattr(job.payload, "direct_handle", None) or "",
        )
        if all(target):
            from raven.spine import direct_lane

            # Derived, not hardcoded: the lane hangs off whatever session this
            # job's (channel, to) binds to, which is the same session key the
            # main-agent lane for this outlet uses. ``RpcOutlet`` maps it back
            # via ``session_of``, so the client's one subscription receives it.
            conversation = direct_lane(f"{channel}:{chat_id}", *target)
            req = TurnRequest(
                origin=Origin.CRON,
                source=source,
                # The wake message verbatim. The instance is the addressee, and
                # a wake turn's message is the whole of what it gets.
                text=job.payload.message,
                conversation=conversation,
                direct_target=target,
            )
        else:
            # The creation-time binding is the one delivery target: submitting
            # with it as the source lets the hub deliver the turn reply to that
            # channel's outlet. run_turn sets the cron-context guard itself (in
            # the lane task), keyed on origin=CRON.
            conversation = f"cron:{job.id}"
            req = TurnRequest(
                origin=Origin.CRON,
                source=source,
                text=reminder_note,
                conversation=conversation,
            )
        try:
            outcome = await submit(req).result()
            if outcome is None:
                # Cancelled or failed before it completed -- a runtime reload cuts
                # in-flight turns -- so the job records a failure, not a delivery.
                raise RuntimeError("turn was cancelled or failed before it completed")
        except Exception as exc:
            if system_events is not None and wake is not None:
                _emit_cron_event(system_events, wake, job, f"{type(exc).__name__}: {exc}", failed=True)
            raise
        # Read the reply back (for the system event) from the gateway runner's
        # capture, stored before result() resolved, and pop it so the
        # long-running map does not accumulate. Keyed on the conversation the
        # request actually used -- a direct wake runs on the instance's lane.
        response: str | None = readback_texts.pop(conversation, None) if readback_texts is not None else None

        # Anti-runaway: count this successful recurring fire. Best-effort —
        # a store I/O failure must not turn a delivered reminder into an
        # error status.
        if cron_service is not None and job.schedule.kind in _RECURRING_KINDS:
            try:
                if cron_service.record_fire(job.id):
                    # Flip the in-flight job too: _writeback_after_run patches
                    # enabled/next_run from this object and would otherwise
                    # clobber the disable record_fire just persisted.
                    job.enabled = False
                    job.state.next_run_at_ms = None
                    # Auto-disable must be user-visible, not a log line: ride
                    # the same event/heartbeat path as missed notices.
                    if system_events is not None and wake is not None:
                        from raven.proactive_engine.system_events import SystemEvent

                        system_events.enqueue(
                            SystemEvent(
                                text=(
                                    f"Recurring reminder '{job.name}' was auto-disabled after "
                                    f"{job.silent_fire_limit} fires with no reply from you. "
                                    f"Re-enable it with: raven cron enable {job.id}"
                                ),
                                source="cron",
                                context_key=f"cron:{job.id}:autodisabled",
                            )
                        )
                        wake.request_wake_now(f"cron:{job.id}:autodisabled")
            except Exception as exc:  # noqa: BLE001 — fire accounting is best-effort
                logger.warning(
                    "cron record_fire failed for {}: {}: {}",
                    job.id,
                    type(exc).__name__,
                    exc,
                )

        # Tell the L3 Sentinel this surface just nudged the user (topic_fired_at
        # + record_dispatched), so its next tick on the same topic skips via
        # topic_quota. Bypasses policy.check(): the user scheduled this cron, so a
        # self-imposed DND / quota must only INFORM, not veto. No-op without sentinel.
        if sentinel_runner is not None:
            _record_cron_dispatch_to_ledger(sentinel_runner, job)

        # Event wake: let the main heartbeat session learn what this isolated cron
        # run produced (and end its sleep early).
        if system_events is not None and wake is not None:
            _emit_cron_event(system_events, wake, job, (response or "(no response)").strip(), failed=False)

        return response

    return on_cron_job


def _record_cron_dispatch_to_ledger(
    sentinel_runner: "SentinelRunner",
    job: "CronJob",
) -> None:
    """Write a cron fire into the shared NudgePolicy ledger.

    The fire IS logged as ``dispatched`` so Sentinel's topic_quota gate
    sees it, but it's IMMEDIATELY marked NEUTRAL so it doesn't pollute
    ``acceptance_rate``. Rationale (B4): cron is user-initiated — the
    user explicitly scheduled it. Sentinel's adaptive-tuning uses
    acceptance_rate to decide "is the user receptive to OUR proactive
    nudges". Cron fires aren't OUR proposals; counting them as
    "dispatched but not accepted" would unfairly drag the rate down and
    over-tighten future Sentinel ticks. NEUTRAL signal is by-design
    excluded from acceptance_rate numerator + denominator.

    Best-effort and silent on failure — a flaky ledger write must NOT
    prevent the cron from delivering. Logs at warning level so the
    issue is observable without breaking the surface contract.
    """
    try:
        topic_tag = job.payload.topic_tag or None
        session_key = f"cron:{job.id}"
        content = job.payload.message or job.name or ""
        sentinel_runner.policy.record_fired(
            "nudge",
            session_key,
            content,
            topic_tag=topic_tag,
        )
        feedback = getattr(sentinel_runner, "feedback", None)
        if feedback is not None:
            from raven.proactive_engine.sentinel.feedback.tracker import (
                new_nudge_id,
            )

            nudge_id = new_nudge_id()
            feedback.record_dispatched(
                nudge_id,
                action="nudge",
                session_key=session_key,
                priority="low",  # user-scheduled — no quota pressure intended
                proactivity_score=0.0,
                source="cron",
                details={"topic_tag": topic_tag, "cron_id": job.id} if topic_tag else {"cron_id": job.id},
            )
            # B4: cron fires don't count toward acceptance_rate (denominator
            # OR numerator). Mark NEUTRAL right away.
            feedback.record_neutral(nudge_id, reason="cron-initiated")
    except Exception as exc:  # noqa: BLE001 — ledger write is best-effort
        logger.warning(
            "ledger write failed for cron {}: {}: {}",
            job.id,
            type(exc).__name__,
            exc,
        )


def make_on_missed_foreign(
    system_events: "SystemEventQueue",
    wake: "WakeScheduler",
) -> Callable[[list["CronJob"]], None]:
    """Build the ``CronService.on_missed_foreign`` observer for the gateway.

    Each missed foreign one-shot (a tui / cli reminder whose session closed
    before it fired) is surfaced ONCE as a system event with context_key
    ``cron:<job_id>:missed`` plus a heartbeat wake request — the heartbeat's
    existing target selection delivers the notice; there is no new delivery
    path, and the foreign job itself is never touched.

    Dedup is two-layer: while queued, the SystemEventQueue replaces events
    sharing a context_key (same semantics _emit_cron_event relies on); after
    the heartbeat consumed one, the in-process ``notified`` set stops any
    re-enqueue on later wake passes. The set is process-local by design —
    after a gateway restart a still-missed job may notify once more
    (accepted v1 trade-off; persisting would mean mutating a foreign job
    or growing a side store).
    """
    notified: set[str] = set()

    def on_missed_foreign(jobs: "list[CronJob]") -> None:
        from raven.proactive_engine.system_events import SystemEvent

        for job in jobs:
            if job.id in notified:
                continue
            fire_at = _ms_to_local_str(job.schedule.at_ms) or "?"
            channel = job.payload.channel or "?"
            context_key = f"cron:{job.id}:missed"
            text = f"Reminder '{job.name}' (scheduled {fire_at} on {channel}) was missed - its session was closed."
            try:
                system_events.enqueue(SystemEvent(text=text, source="cron", context_key=context_key))
                wake.request_wake_now(context_key)
            except Exception as exc:  # noqa: BLE001 — per-job, retried next pass
                logger.warning(
                    "missed-reminder event emit failed for {}: {}: {}",
                    job.id,
                    type(exc).__name__,
                    exc,
                )
                continue
            # Marked only after a successful enqueue so a transient emit
            # failure is retried on the next wake pass.
            notified.add(job.id)
            logger.info("Cron: missed-reminder event enqueued for '{}' ({})", job.name, job.id)

    return on_missed_foreign


def chain_cron_activity_reset(
    cron_service: "CronService",
    inner: "Callable[[TurnRequest], Any] | None" = None,
) -> "Callable[[TurnRequest], Any]":
    """Compose an AgentLoop ``on_user_inbound`` callback that resets the
    anti-runaway silent-fire counters on genuine user activity, chained
    after ``inner`` (the Sentinel engagement hook — preserved, not
    replaced).

    Only ``Origin.USER`` turns reset: the user-inbound hook chain also
    runs for CRON / HEARTBEAT origin turns (only SENTINEL / SUBAGENT are
    skipped at the AgentLoop gate), and a cron fire resetting its own
    counter would defeat the guard entirely.

    Returns ``inner``'s result so an async inner is still awaited by the
    OnUserInboundAdapter; the reset itself is synchronous and best-effort.
    """

    def on_user_inbound(req: "TurnRequest") -> Any:
        result = inner(req) if inner is not None else None
        try:
            from raven.spine import Origin

            if req.origin is Origin.USER and req.source is not None:
                cron_service.notify_user_active(req.source.channel, req.source.chat_id)
        except Exception as exc:  # noqa: BLE001 — reset is best-effort
            logger.warning(
                "cron notify_user_active failed: {}: {}",
                type(exc).__name__,
                exc,
            )
        return result

    return on_user_inbound


def build_cron_service(*, allowed_channels: "set[str] | None") -> "CronService":
    """Open the shared cron store for one process. ``allowed_channels`` is the
    set this process can deliver to (``None`` for management commands that
    only inspect the store), so two runners never claim each other's jobs."""
    from raven.config.paths import get_cron_dir
    from raven.proactive_engine.schedulers.cron.service import CronService

    return CronService(get_cron_dir() / "jobs.json", allowed_channels=allowed_channels)


__all__ = ["build_cron_service", "make_on_cron_job", "make_on_missed_foreign", "chain_cron_activity_reset"]
