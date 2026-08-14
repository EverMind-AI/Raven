"""Shared ``on_cron_job`` factory used by ``gateway``, ``agent`` and ``tui``.

A scheduled reminder fires as a CRON-origin spine turn bound to the
``cron:<job_id>`` session. Delivery is direct: the turn's source is the
job's creation-time binding ``(payload.channel, payload.to)``, so the hub
routes the reply to that one outlet — fire-at-origin, no trigger-time
re-routing.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Awaitable, Callable

from loguru import logger

if TYPE_CHECKING:
    from raven.proactive_engine.schedulers.cron.types import CronJob
    from raven.proactive_engine.sentinel.executor.runner import SentinelRunner
    from raven.proactive_engine.system_events import SystemEventQueue
    from raven.proactive_engine.wake import WakeScheduler
    from raven.spine import TurnHandle, TurnRequest


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

    Best-effort like the F-G ledger write: an emit failure must neither
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
    default_channel: str = "cli",
    sentinel_runner: "SentinelRunner | None" = None,
    system_events: "SystemEventQueue | None" = None,
    wake: "WakeScheduler | None" = None,
) -> Callable[["CronJob"], Awaitable[str | None]]:
    """Build the CronService.on_job callback. Every cron turn runs through the
    spine ``submit`` as a CRON-origin turn.

    ``submit`` (required) is the spine entry (build_gateway / build_repl /
    build_tui scheduler). The turn's source is the job's creation-time
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
    (legacy pre-attribution jobs) — REPL passes "cli" so the reminder
    renders inline in the terminal; the TUI passes "tui".

    ``sentinel_runner`` is optional. When present, F-G makes cron fires
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
            '(e.g. "你在 17:05 提醒的 ...") so the user remembers the '
            "context."
        )

        # The creation-time binding is the one delivery target: submitting
        # with it as the source lets the hub deliver the turn reply to that
        # channel's outlet. run_turn sets the cron-context guard itself (in
        # the lane task), keyed on origin=CRON.
        req = TurnRequest(
            origin=Origin.CRON,
            source=Source(
                channel=job.payload.channel or default_channel,
                chat_id=job.payload.to or "direct",
                sender_id="cron",
                chat_type=ChatType.DM,
            ),
            text=reminder_note,
            conversation=f"cron:{job.id}",
        )
        try:
            await submit(req).result()
        except Exception as exc:
            if system_events is not None and wake is not None:
                _emit_cron_event(system_events, wake, job, f"{type(exc).__name__}: {exc}", failed=True)
            raise
        # Read the reply back (for the system event) from the gateway runner's
        # capture, stored before result() resolved, and pop it so the
        # long-running map does not accumulate.
        response: str | None = readback_texts.pop(f"cron:{job.id}", None) if readback_texts is not None else None

        # F-G: tell the L3 Sentinel this surface just nudged the user (topic_fired_at
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
    """F-G internal: write a cron fire into the shared NudgePolicy ledger.

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
            "F-G ledger write failed for cron {}: {}: {}",
            job.id,
            type(exc).__name__,
            exc,
        )


__all__ = ["make_on_cron_job"]
