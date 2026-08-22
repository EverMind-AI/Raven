"""``turn.*`` real handlers.

* ``turn.send`` submits the turn onto the spine (one per ``session_key``) and
  returns ``{turn_id, accepted: True}`` synchronously; streaming output flows
  out via the build_tui hub/sink as ``SubscriptionEmitter`` notifications.
* ``turn.subscribe`` wraps ``SubscriptionEmitter.register``.
* ``turn.unsubscribe`` wraps ``SubscriptionEmitter.unregister`` (idempotent).
* ``turn.cancel`` cancels the in-flight turn handle and emits the one
  ``error`` event with ``reason="cancelled_by_client"`` (the sink stays silent
  on a cancelled TurnFailed to avoid a double error).

The handlers are exposed at the module level so tests can patch the
``_resolve_model`` seam. ``register_turn_methods`` closes the ``emitter`` and
the build_tui bundle (``scheduler`` / ``turn_ids`` / ``build_error``) into
single-argument dispatcher handlers.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from loguru import logger
from pydantic import ValidationError

from raven.rpc.errors import RpcError, TurnInProgressError
from raven.rpc.models import (
    TurnCancelParams,
    TurnSendParams,
    TurnSubscribeParams,
    TurnUnsubscribeParams,
)
from raven.rpc.subscriptions import SubscriptionEmitter
from raven.spine import ChatType, Media, Origin, Source, TurnHandle, TurnRequest
from raven.spine.scheduler import Scheduler, SchedulerDrainingError

if TYPE_CHECKING:
    from raven.rpc.dispatcher import Dispatcher

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
    from raven.agent.tools.filesystem import _resolve_path
    from raven.config import load_config

    try:
        cfg = load_config()
        workspace = Path(cfg.agents.defaults.workspace).expanduser()
        allowed = workspace if cfg.tools.restrict_to_workspace else None
    except Exception as exc:
        logger.warning("turn.send: cannot resolve the workspace ({}); attachments dropped", exc)
        return ()

    out: list[Media] = []
    for raw in paths:
        if not isinstance(raw, str) or not raw.strip():
            continue
        try:
            resolved = _resolve_path(raw.strip(), workspace, allowed)
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
# handle. The build_tui sink clears the slot at each turn's end (via the
# ``clear_active`` callback), so presence here means in-flight.
_active_turns: dict[str, TurnHandle] = {}


def is_turn_active(session_key: str) -> bool:
    """True if a turn is in flight for this session (the sink drops the slot on
    turn end, so presence is liveness)."""
    return session_key in _active_turns


def clear_active(session_key: str) -> None:
    """Drop a session's active-turn slot. Wired into build_tui as ``on_turn_end``
    so the slot clears at the sink's turn-end point (alongside turn_ids/usages)."""
    _active_turns.pop(session_key, None)


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


async def _emit_start_then_error(
    emitter: SubscriptionEmitter, session_key: str, turn_id: str, code: int, message: str
) -> None:
    # message.start first so the front-end has a turn to clear, then the error
    # clears it (its onError resets turnId) — same shape the old per-turn task used.
    await emitter.emit(session_key, {"type": "message.start", "payload": {"turn_id": turn_id}})
    await emitter.emit(
        session_key,
        {"type": "error", "payload": {"code": code, "message": message, "reason": "internal"}},
    )


async def turn_send(
    params: dict[str, Any],
    *,
    emitter: SubscriptionEmitter | None = None,
    scheduler: Scheduler | None = None,
    turn_ids: dict[str, str] | None = None,
    build_error: RpcError | None = None,
) -> dict[str, Any]:
    """``turn.send`` — submit a turn onto the spine, return ``{turn_id, accepted}``.

    The turn streams out via the build_tui hub/sink (token.delta from the runner,
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

    if scheduler is None:
        # No agent loop wired (build failed / no provider). Surface per-turn as
        # the build error's own code, else -32008. No turn runs.
        if emitter is not None:
            if build_error is not None:
                await _emit_start_then_error(
                    emitter, parsed.session_key, turn_id, build_error.code, build_error.message
                )
            else:
                await _emit_start_then_error(emitter, parsed.session_key, turn_id, -32008, "model_not_available")
        return {"turn_id": turn_id, "accepted": True}

    if is_turn_active(parsed.session_key):
        raise TurnInProgressError(
            f"session {parsed.session_key!r} already has an active turn",
        )

    req = TurnRequest(
        origin=Origin.USER,
        source=Source(
            channel=parsed.channel or "tui",
            chat_id=parsed.chat_id or "default",
            sender_id=parsed.sender_id or "user",
            chat_type=ChatType.DM,
        ),
        text=parsed.content,
        media=_resolve_media(parsed.media),
        # conversation == the front-end subscription key, so the runner's stream
        # and the sink's message.complete reach the right subscription.
        conversation=parsed.session_key,
        # The id this call returns and puts on message.start, so the lane stamps
        # THIS value on the turn's lifecycle events and the client's correlation
        # key survives end to end. Without it the lane mints its own, the sink's
        # ownership check never matches what this bound, and the active-turn slot
        # is never released -- every later send is refused.
        turn_id=turn_id,
    )
    try:
        handle = scheduler.submit(req)
    except SchedulerDrainingError:
        # Server shutting down: surface a turn_failed so the front-end clears its
        # slot; nothing is bound (no leak).
        if emitter is not None:
            await _emit_start_then_error(emitter, parsed.session_key, turn_id, _TURN_FAILED_CODE, "turn_failed")
        return {"turn_id": turn_id, "accepted": True}

    # Bind immediately after submit with no await between (the runner reads
    # turn_ids[session_key] and submit is synchronous, so the worker — scheduled
    # but not yet run — must see the binding). The sink drops both slots at
    # turn end (turn_ids via build_tui, _active_turns via clear_active).
    if turn_ids is not None:
        turn_ids[parsed.session_key] = turn_id
    _active_turns[parsed.session_key] = handle

    if emitter is not None:
        await emitter.emit(parsed.session_key, {"type": "message.start", "payload": {"turn_id": turn_id}})

    return {"turn_id": turn_id, "accepted": True}


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


async def turn_cancel(
    params: dict[str, Any],
    *,
    emitter: SubscriptionEmitter | None = None,
) -> dict[str, Any]:
    """``turn.cancel`` — cancel the in-flight turn + notify subscribers.

    Sequence:
      1. Look up the active turn handle; if absent → ``{cancelled: False}``.
      2. ``handle.cancel()``.
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
    """
    parsed = TurnCancelParams.model_validate(params)

    handle = _active_turns.get(parsed.session_key)
    if handle is None:
        return {"cancelled": False}

    handle.cancel()

    if emitter is not None:
        await emitter.emit(
            parsed.session_key,
            {
                "type": "error",
                "payload": {
                    "code": _TURN_FAILED_CODE,
                    "message": "turn_cancelled",
                    "reason": "cancelled_by_client",
                },
            },
        )

    # Drain so the sink has dropped the active-turn slot before returning.
    # handle.result() returns None on cancellation (does not raise).
    await handle.result()

    return {"cancelled": True}


# ---------------------------------------------------------------------------
# Dispatcher registration
# ---------------------------------------------------------------------------


def register_turn_methods(
    dispatcher: "Dispatcher",
    *,
    emitter: SubscriptionEmitter | None = None,
    scheduler: Scheduler | None = None,
    turn_ids: dict[str, str] | None = None,
    build_error: RpcError | None = None,
) -> None:
    """Register ``turn.{send,subscribe,unsubscribe,cancel}`` on a dispatcher.

    Wraps the four module-level handlers in single-argument closures that
    pre-bind the ``emitter`` and the build_tui spine bundle (``scheduler`` /
    ``turn_ids``) plus the latched ``build_error``, per the dispatcher's
    single-argument handler contract.
    """

    async def _send(params: dict[str, Any]) -> dict[str, Any]:
        return await turn_send(
            params,
            emitter=emitter,
            scheduler=scheduler,
            turn_ids=turn_ids,
            build_error=build_error,
        )

    async def _subscribe(params: dict[str, Any]) -> dict[str, Any]:
        return await turn_subscribe(params, emitter=emitter)

    async def _unsubscribe(params: dict[str, Any]) -> dict[str, Any]:
        return await turn_unsubscribe(params, emitter=emitter)

    async def _cancel(params: dict[str, Any]) -> dict[str, Any]:
        return await turn_cancel(params, emitter=emitter)

    dispatcher.register("turn.send", _send)
    dispatcher.register("turn.subscribe", _subscribe)
    dispatcher.register("turn.unsubscribe", _unsubscribe)
    dispatcher.register("turn.cancel", _cancel)


__all__ = [
    "register_turn_methods",
    "turn_send",
    "turn_subscribe",
    "turn_unsubscribe",
    "turn_cancel",
]
