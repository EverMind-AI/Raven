"""Shared ``on_cron_job`` factory used by both ``gateway`` and ``agent``.

Extracted from commands.gateway() so both entry points run cron jobs with
identical semantics: a scheduled reminder fires as a CRON-origin spine turn
bound to the ``cron:<job_id>`` session, and the reply is delivered by the hub
(single target) or broadcast to the resolved targets.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Awaitable, Callable

from loguru import logger

if TYPE_CHECKING:
    from raven.agent.loop import AgentLoop
    from raven.channels.manager import ChannelManager
    from raven.proactive_engine.schedulers.cron.types import CronJob
    from raven.proactive_engine.sentinel.executor.runner import SentinelRunner
    from raven.proactive_engine.system_events import SystemEventQueue
    from raven.proactive_engine.wake import WakeScheduler
    from raven.session.manager import SessionManager
    from raven.spine import TurnHandle, TurnRequest
    from raven.spine.delivery import DeliveryHub


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
    agent: "AgentLoop",
    hub: "DeliveryHub",
    *,
    submit: "Callable[[TurnRequest], TurnHandle]",
    readback_texts: "dict[str, str] | None" = None,
    channel_manager: "ChannelManager | None" = None,
    session_manager: "SessionManager | None" = None,
    default_channel: str = "cli",
    sentinel_runner: "SentinelRunner | None" = None,
    system_events: "SystemEventQueue | None" = None,
    wake: "WakeScheduler | None" = None,
) -> Callable[["CronJob"], Awaitable[str | None]]:
    """Build the CronService.on_job callback. Every cron turn runs through the
    spine ``submit`` as a CRON-origin turn.

    ``submit`` (required) is the spine entry (build_gateway / build_repl /
    build_tui scheduler). A single-target delivering job (deliver=True, the
    user-facing reminder the cron tool creates) rides the hub to its one outlet.
    A broadcast (more than one resolved target) or a silent job (deliver=False)
    submits with the job's own (ephemeral) channel as the source so the hub drops
    the reply; a broadcast is then delivered explicitly to every target, a silent
    job delivers nothing.

    ``readback_texts`` is build_gateway's per-conversation reply-text map, the
    spine read-back channel for the system event: a CRON turn submits, then this
    reads back its reply from ``readback_texts[cron:<job_id>]`` (the runner stored
    it before result() resolved) and pops it. The submitter cannot pass run_turn's
    text_sink itself — text_sink is a runner-set per-call param, and cron is a
    submitter — so the gateway's capturing runner bridges it. Required whenever
    ``submit`` is wired; without it the system event sees no reply text.

    ``default_channel`` is used when the job payload doesn't specify one —
    REPL passes "cli" so the reminder renders inline in the terminal; the
    gateway lets the payload's own channel decide.

    ``channel_manager`` / ``session_manager`` drive delivery resolution
    (``resolve_cron_delivery``). Without ``channel_manager`` the enabled
    set is empty — meaning every channel is considered ephemeral, and
    delivery falls back to forward_channels broadcast. REPL paths that
    have no real channels (agent-only mode) pass ``None`` for both and
    accept that ephemeral reminders are dropped with a warning.

    ``sentinel_runner`` is optional. When present, F-G makes cron fires
    write to the shared NudgePolicy ledger (topic_fired_at +
    record_dispatched) so the L3 Sentinel suppresses its own proactive
    nudges on the same topic within the dedup window. Without this,
    Sentinel and Cron are blind to each other and the user gets double-
    nudged on the same subject (e.g. user-asked "5/25 birthday cron"
    fires AND Sentinel proactively reminds at 5/22).

    ``system_events`` / ``wake`` are optional. When wired (gateway path),
    a failed cron run or a successful silent run enqueues a system event and
    requests an early heartbeat tick, so the main heartbeat session learns
    what happened in the isolated ``cron:<job_id>`` session and can decide on
    follow-ups. A successful user-facing run is already delivered directly,
    so it must not be re-enqueued for Heartbeat to deliver a second time.
    Only effective for jobs executed in this process — a CLI test-fire
    runs in its own process and cannot reach the gateway's queue.
    """

    async def on_cron_job(job: "CronJob") -> str | None:
        from raven.config.loader import load_config
        from raven.proactive_engine.schedulers.cron.tool import (
            is_ephemeral_channel,
            resolve_cron_delivery,
        )
        from raven.spine import ChatType, Origin, Source, Text, TurnRequest

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

        # Resolve delivery targets at TRIGGER time (reading cron config now lets
        # ``cron config set`` take effect on the next fire) — response-independent,
        # so it can run before the turn; len(targets) decides the path.
        cron_cfg = load_config().cron
        enabled_channels = set(channel_manager.enabled_channels) if channel_manager is not None else set()
        targets, warnings = resolve_cron_delivery(
            channel=job.payload.channel or default_channel,
            chat_id=job.payload.to or "direct",
            forward_channels=cron_cfg.forward_channels,
            enabled_channels=enabled_channels,
            session_manager=session_manager,
        )
        for w in warnings:
            logger.warning("Cron job '{}' ({}): {}", job.name, job.id, w)

        # Every cron turn runs through the spine. Delivery is explicit per branch:
        # a single-target delivering job rides the hub to its one outlet; every
        # other case (no forward, a broadcast, or a silent job) submits with the
        # job's own channel as the source — ephemeral for the realistic cases, so
        # it has no gateway outlet and the hub drops the reply. A broadcast then
        # delivers explicitly below; a silent job delivers nothing. run_turn sets
        # the cron-context guard itself (in the lane task), keyed on origin=CRON.
        deliver_via_hub = job.payload.deliver and len(targets) == 1
        if deliver_via_hub:
            src_channel, src_chat = targets[0].channel, targets[0].chat_id
        else:
            src_channel = job.payload.channel or default_channel
            src_chat = job.payload.to or "direct"
            # A silent job (deliver=False) stays silent because its ephemeral
            # source has no gateway outlet (the hub drops the reply). A silent job
            # on a non-ephemeral channel — only reachable by hand-editing jobs.json,
            # since every creation path sets deliver=True — WOULD be delivered by
            # the hub; warn so this edge is visible rather than a silent change.
            if not job.payload.deliver and not is_ephemeral_channel(src_channel, enabled_channels):
                logger.warning(
                    "Cron job '{}' ({}): silent job on non-ephemeral channel '{}' "
                    "is delivered under the spine (no outlet-less suppression for "
                    "real channels)",
                    job.name,
                    job.id,
                    src_channel,
                )
        req = TurnRequest(
            origin=Origin.CRON,
            source=Source(channel=src_channel, chat_id=src_chat, sender_id="cron", chat_type=ChatType.DM),
            text=reminder_note,
            conversation=f"cron:{job.id}",
        )
        try:
            await submit(req).result()
        except Exception as exc:
            if system_events is not None and wake is not None:
                _emit_cron_event(system_events, wake, job, f"{type(exc).__name__}: {exc}", failed=True)
            raise
        # Read the reply back (for the system event and any broadcast) from the
        # gateway runner's capture, stored before result() resolved, and pop it so
        # the long-running map does not accumulate.
        response: str | None = readback_texts.pop(f"cron:{job.id}", None) if readback_texts is not None else None

        # F-G: tell the L3 Sentinel this surface just nudged the user (topic_fired_at
        # + record_dispatched), so its next tick on the same topic skips via
        # topic_quota. Bypasses policy.check(): the user scheduled this cron, so a
        # self-imposed DND / quota must only INFORM, not veto. No-op without sentinel.
        if sentinel_runner is not None:
            _record_cron_dispatch_to_ledger(sentinel_runner, job)

        # Event wake: silent jobs need the main heartbeat session to learn what
        # this isolated cron run produced. A delivering job already reached the
        # user through the hub/broadcast path; re-enqueuing its response would let
        # Heartbeat deliver the same reminder a second time.
        if system_events is not None and wake is not None and not job.payload.deliver:
            _emit_cron_event(system_events, wake, job, (response or "(no response)").strip(), failed=False)

        # Broadcast a multi-target delivering job to every resolved target: the hub
        # dropped the reply (no outlet for the ephemeral source), so this is the
        # only delivery. It does NOT skip on a message-tool self-send — a self-send
        # to an ephemeral source never reaches the user under the daemon, so
        # broadcasting the reply to all targets is both simpler and strictly better
        # than the legacy self-send guard, which suppressed the broadcast and left
        # the other targets with nothing.
        if job.payload.deliver and len(targets) > 1 and response:
            for t in targets:
                await hub.post(
                    Text(
                        content=response,
                        source=Source(
                            channel=t.channel,
                            chat_id=t.chat_id,
                            sender_id="cron",
                            chat_type=ChatType.DM,
                        ),
                    )
                )
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
