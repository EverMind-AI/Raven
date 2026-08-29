"""Cron events on the rpc surface: fan-out and the callback spine wrapper.

A reminder that fires for a local (``tui``) job runs as a CRON turn whose
conversation has no subscriber, so its reply reaches the client only through
the ``cron.delivered`` fan-out here; a past-due one-shot dropped at start-up
is reported through ``cron.missed``. Shared by the TUI launcher, the headless
rpc stack (``raven serve``, ACP) and the gateway's page mount.
"""

from __future__ import annotations

from raven.rpc import LOCAL_CHANNEL


async def fanout_cron_delivered(emitter, *, job_id, name, text, fired_at) -> None:
    """Fan a ``cron.delivered`` event out to every active TUI session.

    Fan-out (rather than a session-keyed emit) is required because a cron turn
    runs in the ``cron:<job_id>`` conversation, which matches no user
    subscription key, and the TUI is single-session, so fan-out is how the event
    reaches it.
    """
    payload = {"job_id": job_id, "name": name, "text": text, "fired_at": fired_at}
    for session_key in list(emitter._by_session.keys()):
        await emitter.emit(session_key, {"type": "cron.delivered", "payload": payload})


async def fanout_cron_missed(emitter, *, drops) -> None:
    """Fan a ``cron.missed`` event out to every active TUI session.

    Fired once, right after ``cron_service.start()`` dropped past-due one-shot
    reminders. At that point the client has not called ``turn.subscribe`` yet
    (server bring-up precedes the handshake), so with no active session the
    event is queued on the emitter and flushed to the first subscription that
    registers — unlike ``cron.delivered``, whose no-subscriber case is a
    silent no-op because a job fire always happens after the client attached.
    """
    from datetime import datetime, timezone

    items = [
        {
            "name": d.name,
            "scheduled_at": datetime.fromtimestamp(d.at_ms / 1000, tz=timezone.utc).isoformat(),
            "message": d.message,
        }
        for d in drops
    ]
    event = {"type": "cron.missed", "payload": {"count": len(items), "items": items}}
    sessions = list(emitter._by_session.keys())
    if not sessions:
        emitter.queue_startup_event(event)
        return
    for session_key in sessions:
        await emitter.emit(session_key, event)


def build_cron_callback_spine(
    base_on_cron,
    emitter,
    *,
    default_channel: str = LOCAL_CHANNEL,
    direct_targets: dict[str, dict[str, str]] | None = None,
):
    """Wrap the spine cron callback so a **tui** job's reply is fanned out as a
    ``cron.delivered`` event. ``base_on_cron`` (``make_on_cron_job`` with
    ``submit=``) runs the reminder as a CRON turn through the TUI scheduler and
    returns its reply (read back from the runner via ``readback_texts``); a tui
    turn's own hub deliverables target the ``cron:<job_id>`` conversation, which
    has no subscriber and so no-op, making this fan-out the only delivery path.

    The fan-out is gated on the job's resolved channel (``payload.channel`` or
    ``default_channel``, the same resolution ``make_on_cron_job`` applies) being
    ``tui``: a job addressed to an IM channel is delivered there by the hub, and
    echoing its reply to every page session would deliver it twice. In `raven
    tui` / `raven serve` every job resolves to tui and the gate never bites; the
    gateway passes its own default so only its tui-addressed jobs fan out.

    A job naming a sub-agent instance is skipped for the same "already delivered"
    reason the IM gate exists for. That wake runs as a direct turn on the
    instance's own lane, which *does* have a subscriber -- the pane the operator
    is looking at -- so its reply is on screen before this wrapper sees it, and
    fanning it out would print the round twice, once in the conversation and
    once as a reminder."""
    from datetime import datetime, timezone

    async def wrapped(job):
        # Bind the addressee before the turn, exactly as ``turn.send`` does for a
        # typed message: the outlet and sink read this map to stamp ``target`` on
        # the turn's events, and the client demultiplexes on that stamp -- an
        # untagged frame reads as the main conversation's, which is where a wake's
        # reply went until this was here. The sink pops the entry at turn end, so
        # nothing is removed here (same contract as turn.send).
        #
        # Measured 2026-08-26: the round ran, the instance log grew, and the pane
        # showed nothing -- the events reached the client with no target on them.
        agent = getattr(job.payload, "direct_agent", None)
        handle = getattr(job.payload, "direct_handle", None)
        if agent and handle and direct_targets is not None:
            from raven.spine import direct_lane

            lane = direct_lane(f"{job.payload.channel or default_channel}:{job.payload.to or 'direct'}", agent, handle)
            direct_targets[lane] = {"agent": agent, "handle": handle}
        response = await base_on_cron(job)
        resolved_channel = job.payload.channel or default_channel
        # getattr, as the campaign field is read elsewhere: this is the delivery
        # path, and a payload that predates the field must lose the gate, never
        # the reminder.
        addressed_to_instance = bool(
            getattr(job.payload, "direct_agent", None) and getattr(job.payload, "direct_handle", None)
        )
        if response and resolved_channel == LOCAL_CHANNEL and not addressed_to_instance:
            await fanout_cron_delivered(
                emitter,
                job_id=job.id,
                name=job.name,
                text=response,
                fired_at=datetime.now(timezone.utc).isoformat(),
            )
        return response

    return wrapped


__all__ = ["build_cron_callback_spine", "fanout_cron_delivered", "fanout_cron_missed"]
