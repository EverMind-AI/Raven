"""``turn.*`` real handlers.

* ``turn.send`` submits the turn onto the spine (one per ``session_key``) and
  returns ``{turn_id, accepted: True}`` synchronously; streaming output flows
  out via the build_rpc_spine hub/sink as ``SubscriptionEmitter`` notifications.
* ``turn.subscribe`` wraps ``SubscriptionEmitter.register``.
* ``turn.unsubscribe`` wraps ``SubscriptionEmitter.unregister`` (idempotent).
* ``turn.cancel`` cancels the in-flight turn handle and emits the one
  ``error`` event with ``reason="cancelled_by_client"`` (the sink stays silent
  on a cancelled TurnFailed to avoid a double error).

The handlers are exposed at the module level so tests can patch the
``_resolve_model`` seam. ``register_turn_methods`` closes the ``emitter`` and
the build_rpc_spine bundle (``scheduler`` / ``turn_ids`` / ``build_error``) into
single-argument dispatcher handlers.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from loguru import logger
from pydantic import ValidationError

from raven.rpc.connection import claim_conversation, declared_surface
from raven.rpc.errors import RpcError, TurnInProgressError
from raven.rpc.models import (
    TurnCancelParams,
    TurnSendParams,
    TurnSubscribeParams,
    TurnUnsubscribeParams,
)
from raven.rpc.subscriptions import SubscriptionEmitter
from raven.spine import ChatType, Media, Origin, Source, TurnHandle, TurnRequest, direct_lane, session_of
from raven.spine.scheduler import Scheduler, SchedulerDrainingError
from raven.spine.turn import BusyPolicy

if TYPE_CHECKING:
    from raven.rpc.dispatcher import Dispatcher
    from raven.rpc.methods.session import AgentLoopFactory

_TURN_FAILED_CODE = -32099


def _resolve_media(paths: list[str] | None) -> tuple[Media, ...]:
    """Turn the front end's attachment paths into ``Media`` for the spine.

    Resolved with the filesystem tools' own policy rather than against the
    process cwd. A caller sends what it holds, and what it holds is a workspace
    path (``uploads/shot.png``) -- the same spelling every file tool takes, and
    one that resolves to nothing from wherever ``raven serve`` happens to have
    been started. The downstream check is a bare ``is_file()`` that drops a miss
    in silence, so a cwd-relative resolve loses the attachment with no error
    anywhere.

    The mime is left generic on purpose: ``render.build_user_content`` sniffs
    the magic bytes, and the channels' own intake does the same thing here.
    A path that does not resolve, or resolves outside the allowed directory,
    is dropped with a log line -- one bad attachment must not fail the turn.
    """
    if not paths:
        return ()
    from raven.agent.tools.filesystem import resolve_path
    from raven.config import load_config

    try:
        cfg = load_config()
        # Through ``workspace_path``, not the raw field: the declared default is
        # a literal that the property resolves against ``RAVEN_HOME``, and
        # ``fs.upload`` deposits through that same property. Reading the field
        # directly put a second home's attachments under the first home's
        # workspace, where nothing resolved.
        workspace = cfg.workspace_path
        allowed = (workspace,) if cfg.tools.restrict_to_workspace else ()
    except Exception as exc:
        logger.warning("turn.send: cannot resolve the workspace ({}); attachments dropped", exc)
        return ()

    out: list[Media] = []
    for raw in paths:
        if not isinstance(raw, str) or not raw.strip():
            continue
        try:
            resolved = resolve_path(raw.strip(), workspace, allowed)
            if not resolved.is_file():
                logger.warning("turn.send: attachment {} does not resolve to a file", raw)
                continue
        except Exception as exc:
            # Every failure shape lands here on purpose. A path can be refused
            # (PermissionError), embed a null byte or an unknown ~user
            # (ValueError / RuntimeError), or exceed the filesystem's name
            # limit (OSError) -- and each of those escaping would turn one bad
            # attachment into a turn that never runs.
            logger.warning("turn.send: attachment {} rejected: {}", raw, exc)
            continue
        out.append(Media(path=str(resolved), mime="application/octet-stream", kind="file"))
    return tuple(out)


# ---------------------------------------------------------------------------
# Module-level state
# ---------------------------------------------------------------------------

# In-flight turn handles keyed by session_key. One turn per session at a time:
# ``turn.send`` rejects with -32003 when present, ``turn.cancel`` cancels the
# handle. The build_rpc_spine sink clears the slot at each turn's end (via the
# ``clear_active`` callback), so presence here means in-flight.
_active_turns: dict[str, TurnHandle] = {}


def is_turn_active(session_key: str) -> bool:
    """True if a turn is in flight for this session (the sink drops the slot on
    turn end, so presence is liveness)."""
    return session_key in _active_turns


def is_session_busy(session_key: str) -> bool:
    """True if *any* lane of this session has a turn in flight.

    ``is_turn_active`` answers for one lane, which is what ``turn.send`` needs:
    a direct chat is refused only by that instance still answering. The
    session-level guards -- clear, undo, compress, model switch -- mean "is
    anything running here", and a sub-agent answering is running here even
    though it runs on a lane of its own.
    """
    return any(session_of(lane) == session_key for lane in _active_turns)


def clear_active(session_key: str) -> None:
    """Drop a session's active-turn slot. Wired into build_rpc_spine as ``on_turn_end``
    so the slot clears at the end of the turn that owns it (alongside turn_ids)."""
    _active_turns.pop(session_key, None)


# The ``busy: inject`` sends the running turn has not yet merged, per lane and
# then per the id minted for each: (its handle, its text), in arrival order.
# The handle is the one record of that text the surface holds, and it stays
# valid through every state the spine puts an inject in -- waiting in the lane's
# mailbox, merged into the running turn, or fallen back to a turn of its own
# when the host ended first. Kept so ``turn.cancel`` / ``session.interrupt``
# can still reach it once the host turn has released the lane's active slot,
# and so the sink can promote the fallback turn into that slot when it starts
# (reviewed 2026-09-10: an undrained inject ran as a turn nothing could see or
# cancel). Every inject, not the newest: the lane's mailbox keeps them all, and
# two steers after the host's last drain fall back one after the other, each
# needing its own handle when its turn starts (reviewed again the same day).
_pending_injects: dict[str, dict[str, tuple[TurnHandle, str]]] = {}


def promote_pending_inject(lane: str, turn_id: str) -> str | None:
    """A pending inject's turn is starting on ``lane``: make it the lane's active turn.

    Wired into build_rpc_spine as ``on_turn_start``. Answers the injected text
    when ``turn_id`` names the lane's pending inject -- the host turn ended
    before draining it and the spine fell it back to a turn of its own -- and
    ``None`` for any other turn. The caller binds ``turn_ids`` and opens the
    turn on the wire; this binds the active-turn slot the cancel paths read.
    """
    pending = _pending_injects.get(lane, {}).pop(turn_id, None)
    if pending is None:
        return None
    if not _pending_injects.get(lane):
        _pending_injects.pop(lane, None)
    _active_turns[lane] = pending[0]
    return pending[1]


def _cancellable(lane: str) -> tuple[TurnHandle, str | None] | None:
    """The handle a cancel on ``lane`` reaches, and the inject's id when that is what it is.

    The active slot first -- the turn ``turn.send`` bound, or a fallback inject
    the sink promoted. Failing that, the oldest inject still pending: in the
    mailbox, or queued behind a host that ended before the sink saw it start.
    Oldest, because that is the one the spine runs next.
    """
    handle = _active_turns.get(lane)
    if handle is not None:
        return handle, None
    pending = _pending_injects.get(lane)
    if not pending:
        return None
    inject_id, (inject_handle, _text) = next(iter(pending.items()))
    return inject_handle, inject_id


def _forget_inject(lane: str, turn_id: str) -> None:
    """Drop one inject's pending record, and the lane's table when it empties."""
    pending = _pending_injects.get(lane)
    if pending is None:
        return
    pending.pop(turn_id, None)
    if not pending:
        _pending_injects.pop(lane, None)


# ---------------------------------------------------------------------------
# Mockable seams
# ---------------------------------------------------------------------------


def _resolve_model(parsed: TurnSendParams) -> str:
    """Resolve the model id for a turn before spawning AgentLoop.

    Raises ``ModelNotAvailableError`` (-32008) if no provider/model is
    routable. The default impl is a no-op pass-through — AgentLoop owns the
    real model selection. Tests patch this seam to assert -32008 path.
    """
    return "default"


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------


def _target_payload(parsed: TurnSendParams) -> dict[str, str] | None:
    """The turn's addressee as it goes on the wire, or ``None`` for the main agent."""
    return None if parsed.target is None else {"agent": parsed.target.agent, "handle": parsed.target.handle}


def _tag(payload: dict[str, Any], target: dict[str, str] | None) -> dict[str, Any]:
    """Add the addressee to an event payload, or leave it exactly as it was.

    Absent rather than null for a main-agent turn, so every payload the wire
    already carried keeps its shape byte for byte and "untagged means the main
    conversation" is true of the frame itself, not only of a convention.
    """
    return payload if target is None else {**payload, "target": target}


async def _emit_start_then_error(
    emitter: SubscriptionEmitter,
    session_key: str,
    turn_id: str,
    code: int,
    message: str,
    target: dict[str, str] | None = None,
) -> None:
    # message.start first so the front-end has a turn to clear, then the error
    # clears it (its onError resets turnId) — same shape the old per-turn task used.
    # Both carry the target: a turn that never ran still belonged to whatever the
    # client addressed, and that is the view whose spinner has to be cleared.
    await emitter.emit(session_key, {"type": "message.start", "payload": _tag({"turn_id": turn_id}, target)})
    await emitter.emit(
        session_key,
        {
            "type": "error",
            # Carries the turn it belongs to: this failure answers a request, and
            # a consumer with no id cannot tell it from a foreign turn's.
            "payload": _tag({"code": code, "message": message, "reason": "internal", "turn_id": turn_id}, target),
        },
    )


def _name_session(
    parsed: TurnSendParams,
    *,
    agent_loop_factory: "AgentLoopFactory | None",
    emitter: SubscriptionEmitter | None,
) -> bool:
    """Hand this turn's opening line to the session namer, if it is one.

    Returns whether a namer actually started, which is what `turn.send` passes
    back to the caller. Without it a client cannot tell "a title is coming" from
    "no title is coming", and its only way to find out was to hold a placeholder
    until a grace period expired -- so every refusal here, which is decided in
    microseconds, cost the front end its full wait before it showed anything.

    Wrapped in its own try/except for the reason the loop factory is invoked
    defensively everywhere else in this module: naming is a side errand, and no
    failure in it may reach a client that asked for a turn.
    """
    try:
        from raven.config.raven import load_raven_config
        from raven.rpc.methods.session import _safe_invoke_factory
        from raven.rpc.session_naming import name_session_alongside_turn
        from raven.session.resolve import manager_for

        agent_loop = _safe_invoke_factory(agent_loop_factory)
        if agent_loop is None:
            return False
        # One read for both. The extension blocks live in their own model, and
        # `Config` has no attribute for them at all: `config.raven.session_title`
        # raised AttributeError, which the except below turned into silence, and
        # that is exactly how this shipped inert. `RavenConfig.base` is the very
        # object `load_config()` would return, so reading it from here rather
        # than calling that too keeps this path at one config load instead of
        # two -- loader.py notes that this caller reloads on every turn.
        raven_config = load_raven_config()
        config = raven_config.base
        settings = raven_config.session_title
        task = name_session_alongside_turn(
            session_key=parsed.session_key,
            text=parsed.content or "",
            mgr=manager_for(agent_loop, config),
            provider=getattr(agent_loop, "provider", None),
            emitter=emitter,
            enabled=settings.enabled,
            model=settings.model,
            budget=settings.budget,
            min_input_width=settings.min_input_width,
            timeout_seconds=settings.timeout_seconds,
        )
        return task is not None
    except Exception:
        # Warning, not debug. Every *refusal* to name a session is a decision
        # this code makes deliberately and reports at debug; reaching here means
        # the wiring itself is broken, and the first version of this logged that
        # at debug too, so a feature that never once ran looked exactly like one
        # that had nothing to name.
        #
        # `opt(exception=True)`, not `exc_info=True`: loguru has no such kwarg
        # and would file it under `record["extra"]`, which the sinks in
        # cli/_log_file.py do not format -- leaving a warning with no exception
        # type, message or frame in it at all.
        logger.opt(exception=True).warning("turn.send: session naming could not start")
        return False


async def turn_send(
    params: dict[str, Any],
    *,
    emitter: SubscriptionEmitter | None = None,
    scheduler: Scheduler | None = None,
    turn_ids: dict[str, str] | None = None,
    direct_targets: dict[str, dict[str, str]] | None = None,
    build_error: RpcError | None = None,
    default_channel: str = "tui",
    agent_loop_factory: "AgentLoopFactory | None" = None,
) -> dict[str, Any]:
    """``turn.send`` — submit a turn onto the spine, return ``{turn_id, accepted}``.

    The turn streams out via the build_rpc_spine hub/sink (token.delta from the runner,
    message.complete / error from the sink). message.start is emitted here since
    this owns the turn_id.

    Errors:
      -32003 (TurnInProgressError) — session already has an active turn.
      -32008 (ModelNotAvailableError) — no provider/model routable.
    """
    try:
        parsed = TurnSendParams.model_validate(params)
    except ValidationError as exc:
        # Re-raise as-is; dispatcher will catch and emit -32603 internal_error.
        raise exc

    # Fail-fast: model availability before the active-turn slot, so a -32008
    # reject does not lock the session out of subsequent sends.
    _resolve_model(parsed)

    turn_id = uuid4().hex
    target = _target_payload(parsed)

    if scheduler is None:
        # No agent loop wired (build failed / no provider). Surface per-turn as
        # the build error's own code, else -32008. No turn runs.
        if emitter is not None:
            if build_error is not None:
                await _emit_start_then_error(
                    emitter, parsed.session_key, turn_id, build_error.code, build_error.message, target
                )
            else:
                await _emit_start_then_error(
                    emitter, parsed.session_key, turn_id, -32008, "model_not_available", target
                )
        return {"turn_id": turn_id, "accepted": True, "naming": False}

    # Per lane, not per session: a direct chat runs on its instance's own lane
    # (see ``direct_lane``), so it is refused only by *that instance* still
    # answering -- the main agent's turn and every other instance's are
    # concurrent with it. Two turns to one instance would serialise on
    # ``hold_handle`` anyway; refusing is what keeps them from queueing behind a
    # wait with no bound.
    lane = (
        direct_lane(parsed.session_key, parsed.target.agent, parsed.target.handle)
        if parsed.target is not None
        else parsed.session_key
    )

    if parsed.busy == "inject" and (is_turn_active(lane) or _lane_in_flight(scheduler, lane)):
        # Two records of "a turn is running here", and both count: this map
        # knows the turns ``turn.send`` started, the scheduler knows every turn
        # on the lane -- an armed wake is submitted to it directly
        # (``make_on_session_wake``) and never passes through here (reviewed
        # 2026-09-09: a steer during a running wake queued a second turn).
        return await _inject_into_running(parsed, lane, scheduler=scheduler, emitter=emitter)
    if is_turn_active(lane):
        raise TurnInProgressError(
            f"session {parsed.session_key!r} already has an active turn",
        )

    req = TurnRequest(
        origin=Origin.USER,
        source=Source(
            channel=parsed.channel or default_channel,
            chat_id=parsed.chat_id or "default",
            sender_id=parsed.sender_id or "user",
            chat_type=ChatType.DM,
            # What this connection called itself in system.hello, or None. The
            # turn runs on the spine's own task, out of reach of the
            # connection's contextvars, so the fact has to ride the request.
            surface=declared_surface(),
        ),
        text=parsed.content,
        media=_resolve_media(parsed.media),
        # conversation == the lane. For the main agent that is the session key,
        # which is also the front-end subscription key; for a direct chat it is
        # that instance's lane, and ``RpcOutlet`` maps it back to the session so
        # the client's one subscription still receives it.
        conversation=lane,
        # When set, the turn skips the model entirely and runs one direct-chat
        # turn against that instance (see AgentLoop.run_turn).
        direct_target=(parsed.target.agent, parsed.target.handle) if parsed.target is not None else None,
        # The id this call returns and puts on message.start, so the lane stamps
        # THIS value on the turn's lifecycle events and the client's correlation
        # key survives end to end.
        turn_id=turn_id,
    )
    try:
        handle = scheduler.submit(req)
    except SchedulerDrainingError:
        # Server shutting down: surface a turn_failed so the front-end clears its
        # slot; nothing is bound (no leak).
        if emitter is not None:
            await _emit_start_then_error(emitter, parsed.session_key, turn_id, _TURN_FAILED_CODE, "turn_failed", target)
        return {"turn_id": turn_id, "accepted": True, "naming": False}

    # Bind immediately after submit with no await between (submit is synchronous,
    # so the worker — scheduled but not yet run — must see the binding). This map
    # is no longer where the sink gets the id to stamp (the turn carries its own):
    # it records WHICH turn owns this lane's client-facing slots, so a turn the
    # runtime submitted onto the same lane cannot release them out from under a
    # client turn still queued behind it. The sink drops both slots at the owning
    # turn's end (turn_ids via build_rpc_spine, _active_turns via clear_active).
    if turn_ids is not None:
        turn_ids[lane] = turn_id
    # Bound the same way and dropped by the same sink, so the two cannot fall out
    # of step. Popped rather than left when there is no target: a stale entry
    # from an earlier turn would tag the main agent's stream as a sub-agent's.
    if direct_targets is not None:
        if target is None:
            direct_targets.pop(lane, None)
        else:
            direct_targets[lane] = target
    _active_turns[lane] = handle
    # The surface that sent the turn is the one a mid-turn question belongs to.
    # Without this the ask_user notification is broadcast to every socket on the
    # gateway, so a question asked inside one terminal's session also interrupts
    # the browser page and any other attached terminal.
    claim_conversation(lane)

    if emitter is not None:
        # The question rides the event that opens the turn so a client which
        # did not send it can still draw it: the user entry is written to the
        # transcript only at turn end, so until then this is the only place a
        # second window can learn what was asked.
        await emitter.emit(
            parsed.session_key,
            {"type": "message.start", "payload": _tag({"turn_id": turn_id, "content": parsed.content}, target)},
        )

    # After the submit, so a turn that was never accepted does not name a session
    # that has nothing in it; and only for the main conversation, since a direct
    # chat's opening line names its instance's lane, not this session. Returns
    # immediately -- the call it may start runs on its own task.
    naming = False
    if parsed.target is None:
        naming = _name_session(parsed, agent_loop_factory=agent_loop_factory, emitter=emitter)

    return {"turn_id": turn_id, "accepted": True, "naming": naming}


def _lane_in_flight(scheduler: Scheduler, lane: str) -> bool:
    """Whether the scheduler has a turn running on ``lane``, whoever started it."""
    probe = getattr(scheduler, "has_inflight", None)
    return bool(probe(lane)) if callable(probe) else False


async def _inject_into_running(
    parsed: TurnSendParams,
    lane: str,
    *,
    scheduler: Scheduler,
    emitter: SubscriptionEmitter | None,
) -> dict[str, Any]:
    """Hand ``parsed.content`` to the turn already running on ``lane``.

    ``BusyPolicy.INJECT``: the lane holds the text for the running turn's
    worker to merge at its next tool-loop gap, and falls it back to a turn of
    its own if that turn ends first (spine ``Lane.submit``). Nothing is bound
    here -- the running turn owns the lane's slots -- but the handle is kept in
    ``_pending_injects`` under an id minted for the text, so a cancel can still
    reach it after the host has released those slots, and so the sink can
    promote the fallback turn into them when it starts. The id answered is that
    one: it is what the fallback turn's events will carry.
    """
    turn_id = uuid4().hex
    target = _target_payload(parsed)
    req = TurnRequest(
        origin=Origin.USER,
        source=Source(
            channel=parsed.channel or "tui",
            chat_id=parsed.chat_id or "default",
            sender_id=parsed.sender_id or "user",
            chat_type=ChatType.DM,
            surface=declared_surface(),
        ),
        text=parsed.content,
        media=_resolve_media(parsed.media),
        conversation=lane,
        direct_target=(parsed.target.agent, parsed.target.handle) if parsed.target is not None else None,
        busy=BusyPolicy.INJECT,
        turn_id=turn_id,
    )
    try:
        handle = scheduler.submit(req)
    except SchedulerDrainingError:
        # The same exit the ordinary send takes: the front end is told the turn
        # failed so it can clear its slot, rather than an internal error.
        if emitter is not None:
            await _emit_start_then_error(emitter, parsed.session_key, turn_id, _TURN_FAILED_CODE, "turn_failed", target)
        return {"turn_id": turn_id, "accepted": True, "naming": False}
    _pending_injects.setdefault(lane, {})[turn_id] = (handle, parsed.content)

    async def _forget_when_done() -> None:
        # Merged, ran, or cancelled: the future resolves on every exit.
        await handle.result()
        _forget_inject(lane, turn_id)

    asyncio.get_running_loop().create_task(_forget_when_done())
    return {"turn_id": turn_id, "accepted": True, "naming": False}


async def turn_subscribe(
    params: dict[str, Any],
    *,
    emitter: SubscriptionEmitter | None = None,
) -> dict[str, Any]:
    """``turn.subscribe`` — open a subscription, return ``{subscription_id}``."""
    parsed = TurnSubscribeParams.model_validate(params)
    if emitter is None:
        raise RuntimeError(
            "turn.subscribe requires a SubscriptionEmitter; register_turn_methods must be called with emitter=...",
        )
    sub_id = await emitter.register(parsed.session_key)
    return {"subscription_id": sub_id}


async def turn_unsubscribe(
    params: dict[str, Any],
    *,
    emitter: SubscriptionEmitter | None = None,
) -> dict[str, Any]:
    """``turn.unsubscribe`` — close a subscription (idempotent)."""
    parsed = TurnUnsubscribeParams.model_validate(params)
    if emitter is None:
        raise RuntimeError(
            "turn.unsubscribe requires a SubscriptionEmitter; register_turn_methods must be called with emitter=...",
        )
    unsubscribed = await emitter.unregister(parsed.subscription_id)
    return {"unsubscribed": unsubscribed}


def _cancel_payload(turn_id: str) -> dict[str, Any]:
    """The cancelled-turn error's payload, carrying its turn when one is bound.

    The lane's bound turn is the right source here and only here: the client
    cancels its own session, so the turn ``turn.send`` bound to this lane is the
    turn being cancelled. The key is omitted rather than sent empty, so that
    "absent" has one representation on the wire -- a consumer that correlates a
    request to a turn treats both the same way, as not its own.
    """
    payload: dict[str, Any] = {
        "code": _TURN_FAILED_CODE,
        "message": "turn_cancelled",
        "reason": "cancelled_by_client",
    }
    if turn_id:
        payload["turn_id"] = turn_id
    return payload


async def turn_cancel(
    params: dict[str, Any],
    *,
    emitter: SubscriptionEmitter | None = None,
    direct_targets: dict[str, dict[str, str]] | None = None,
    turn_ids: dict[str, str] | None = None,
) -> dict[str, Any]:
    """``turn.cancel`` — cancel the in-flight turn + notify subscribers.

    Sequence:
      1. Look up the active turn handle; if absent → ``{cancelled: False}``.
      2. ``await handle.cancel()``.
      3. ``emitter.emit(session_key, error(reason="cancelled_by_client"))`` — the
         client resets its UI off this event. This is the ONLY cancelled-turn
         error; the sink stays silent on a cancelled TurnFailed (avoiding a
         double error), so this emit is the one signal that clears the front-end
         turn slot — it must always fire.
      4. Await the handle so the turn is provably unwound (the sink's TurnFailed
         handler drops the active-turn slot) before returning, so the next
         ``turn.send`` cannot race a half-unwound turn into a phantom -32003.
      5. Return ``{cancelled: True}``.

    The subscription is SESSION-scoped, not turn-scoped: a per-turn cancel ends
    only the turn and MUST leave the session's subscriptions open so the next
    turn's events still reach the client.

    ``target`` names which lane to cancel and must be resolved the same way
    ``turn.send`` resolves the lane it binds, or the lookup misses every direct
    turn: those are registered under ``direct_lane(...)``, never under the bare
    session key. The emit still goes to the session, which is what the client
    subscribes to; the addressee rides the payload.
    """
    parsed = TurnCancelParams.model_validate(params)

    lane = (
        direct_lane(parsed.session_key, parsed.target.agent, parsed.target.handle)
        if parsed.target is not None
        else parsed.session_key
    )

    reach = _cancellable(lane)
    if reach is None:
        return {"cancelled": False}
    handle, inject_id = reach

    await handle.cancel()

    if emitter is not None:
        # Tagged from the live map rather than from ``parsed.target``: the map is
        # what turn.send bound for this lane, so the tag on this error is byte
        # for byte the one the cancelled turn's own events carried -- which is
        # what lets the client clear the view it was streaming into. A pending
        # inject was bound to nothing; its error names the id its send answered.
        await emitter.emit(
            parsed.session_key,
            {
                "type": "error",
                "payload": _tag(
                    _cancel_payload(inject_id or (turn_ids or {}).get(lane, "")),
                    (direct_targets or {}).get(lane),
                ),
            },
        )

    # Drain so the sink has dropped the active-turn slot before returning.
    # handle.result() returns None on cancellation (does not raise).
    await handle.result()
    if inject_id is not None:
        _forget_inject(lane, inject_id)

    return {"cancelled": True}


async def session_interrupt(params: dict[str, Any]) -> dict[str, Any]:
    """``session.interrupt`` -- Ctrl+C on the pre-typed-chat path.

    ui-tui's ``turnController.interruptTurn`` fires this when no
    ``ChatStreamHandle`` is attached, then finalizes the transcript itself. So
    unlike ``turn.cancel`` this emits nothing: the client has already drawn the
    interrupted state, and a second error frame would double-report it.

    ``ok`` says a live turn was cancelled. False when there was none, which
    happens routinely -- Ctrl+C on an idle prompt -- and is not an error.
    """
    session_key = str(params.get("session_id") or "").strip()
    reach = _cancellable(session_key) if session_key else None
    handle = reach[0] if reach is not None else None
    if handle is None:
        return {"ok": False}
    await handle.cancel()
    await handle.result()
    return {"ok": True}


# ---------------------------------------------------------------------------
# Dispatcher registration
# ---------------------------------------------------------------------------


def register_session_interrupt_method(dispatcher: "Dispatcher") -> None:
    """Register ``session.interrupt``.

    Kept out of :func:`register_turn_methods` because that group is skipped
    when the caller owns no emitter, and this handler needs none -- gating it
    the same way would put the legacy Ctrl+C path back on -32601 in exactly the
    configurations that still use it.
    """
    dispatcher.register("session.interrupt", session_interrupt)


def register_turn_methods(
    dispatcher: "Dispatcher",
    *,
    emitter: SubscriptionEmitter | None = None,
    scheduler: Scheduler | None = None,
    turn_ids: dict[str, str] | None = None,
    direct_targets: dict[str, dict[str, str]] | None = None,
    build_error: RpcError | None = None,
    default_channel: str = "tui",
    agent_loop_factory: "AgentLoopFactory | None" = None,
) -> None:
    """Register ``turn.{send,subscribe,unsubscribe,cancel}`` on a dispatcher.

    Wraps the four module-level handlers in single-argument closures that
    pre-bind the ``emitter`` and the build_rpc_spine spine bundle (``scheduler`` /
    ``turn_ids``) plus the latched ``build_error``, per the dispatcher's
    single-argument handler contract.

    ``default_channel`` is the ``source.channel`` stamped on a turn when the
    client omits one — and it MUST match the channel the outlet was registered
    under (the hub routes deliverables by ``source.channel``), or the reply is
    dropped. Defaults to ``"tui"``.
    """

    async def _send(params: dict[str, Any]) -> dict[str, Any]:
        return await turn_send(
            params,
            emitter=emitter,
            scheduler=scheduler,
            turn_ids=turn_ids,
            direct_targets=direct_targets,
            build_error=build_error,
            agent_loop_factory=agent_loop_factory,
            default_channel=default_channel,
        )

    async def _subscribe(params: dict[str, Any]) -> dict[str, Any]:
        return await turn_subscribe(params, emitter=emitter)

    async def _unsubscribe(params: dict[str, Any]) -> dict[str, Any]:
        return await turn_unsubscribe(params, emitter=emitter)

    async def _cancel(params: dict[str, Any]) -> dict[str, Any]:
        return await turn_cancel(params, emitter=emitter, direct_targets=direct_targets, turn_ids=turn_ids)

    dispatcher.register("turn.send", _send)
    dispatcher.register("turn.subscribe", _subscribe)
    dispatcher.register("turn.unsubscribe", _unsubscribe)
    dispatcher.register("turn.cancel", _cancel)


__all__ = [
    "register_turn_methods",
    "register_session_interrupt_method",
    "turn_send",
    "turn_subscribe",
    "turn_unsubscribe",
    "turn_cancel",
    "session_interrupt",
]
