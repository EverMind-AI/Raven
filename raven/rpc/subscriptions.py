"""SubscriptionEmitter — per-session subscription registry + 16ms coalesce loop.

Per-session subscription registry with a coalesce loop:

- Per-session subscription map (`session_key → [Subscription]`)
- Each subscription owns a bounded asyncio.Queue (capacity 512)
- 16ms coalesce loop merges consecutive `token.delta` events into one frame
- Queue overflow → emit error(code=-32010) + close subscription
- Non-token events pass through preserving order
- A turn in flight is buffered, so a subscription opened mid-turn is handed
  what already streamed before it sees anything live

Owned by the RPC server; passed to `register_turn_methods(dispatcher, emitter)`.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

from loguru import logger

COALESCE_WINDOW_S = 0.016
QUEUE_CAPACITY = 512

# How many events of a turn in flight are held for replay. Well under
# QUEUE_CAPACITY: the whole buffer is pushed into a new subscription's queue at
# once, and a replay that overflows the queue would kill the subscription it
# exists to serve. Consecutive deltas are merged as they are recorded, so this
# counts segments and tool calls, not tokens -- a long turn reaches it only
# after a few hundred calls.
REPLAY_CAPACITY = 400

# Deltas of the same kind concatenate instead of accumulating one event each.
_MERGEABLE = ("token.delta", "thinking.delta")

# Ends a turn: the transcript on disk becomes the record, so the buffer goes.
_TURN_OVER = ("message.complete", "error")


@dataclass
class Subscription:
    sub_id: str
    session_key: str
    queue: asyncio.Queue
    coalesce_task: asyncio.Task | None = None
    closed: bool = False


SendFrame = Callable[[dict[str, Any]], Awaitable[None]]


class SubscriptionEmitter:
    """Routes TurnEvent notifications to per-session subscribers."""

    def __init__(self, send_frame: SendFrame) -> None:
        self._send_frame = send_frame
        self._by_session: dict[str, list[Subscription]] = {}
        self._by_id: dict[str, Subscription] = {}
        # session_key -> events of the turn currently in flight, oldest first.
        self._replay: dict[str, list[dict[str, Any]]] = {}
        self._startup_events: list[dict[str, Any]] = []

    def queue_startup_event(self, event: dict[str, Any]) -> None:
        """Buffer an event produced before any subscription exists.

        Server bring-up (cron start, agent-loop build) precedes the client's
        ``turn.subscribe``, so an ``emit`` at that point is a silent no-op.
        Buffered events are flushed to the first subscription that registers,
        then dropped — they are one-shot startup notices, not a replay log.
        """
        self._startup_events.append(event)

    async def register(self, session_key: str) -> str:
        """Create a subscription, start its coalesce loop, return the sub_id.

        A turn already in flight is replayed into the new queue first. Without
        it, opening the session in a second window (or following a shared link)
        showed an empty transcript until the turn ended: ``session.resume``
        reads what is on disk, and a turn is written only once it finishes.

        Replay and the live stream must not overlap or leave a gap, so the
        buffer is copied and the subscription published with no ``await``
        between them: an ``emit`` can only run at a suspension point, so it
        either lands before the copy (and is in it) or after the publish (and
        is queued behind it).

        Any queued startup events are flushed to this subscription's session
        if it is the first to register (see ``queue_startup_event``).
        """
        sub = Subscription(
            sub_id=uuid4().hex,
            session_key=session_key,
            queue=asyncio.Queue(maxsize=QUEUE_CAPACITY),
        )
        for event in self._replay.get(session_key, ()):
            try:
                sub.queue.put_nowait(event)
            except asyncio.QueueFull:  # pragma: no cover - REPLAY_CAPACITY forbids it
                logger.warning("replay overflowed a fresh subscription for {}", session_key)
                break
        self._by_session.setdefault(session_key, []).append(sub)
        self._by_id[sub.sub_id] = sub
        sub.coalesce_task = asyncio.create_task(self._coalesce_loop(sub))
        if self._startup_events:
            pending, self._startup_events = self._startup_events, []
            for event in pending:
                await self.emit(session_key, event)
        return sub.sub_id

    async def unregister(self, sub_id: str) -> bool:
        """Close the subscription if it exists and is open. Idempotent."""
        sub = self._by_id.get(sub_id)
        if sub is None or sub.closed:
            self._by_id.pop(sub_id, None)
            return False
        self._mark_closed(sub)
        return True

    async def emit(self, session_key: str, event: dict[str, Any]) -> None:
        """Push event to every open subscriber of session_key.

        Overflow → emit error event + close the affected subscription.
        Other subscribers unaffected.

        Recorded before it is dispatched, so a subscription registering during
        this call already finds it in the buffer.
        """
        self._record(session_key, event)
        for sub in list(self._by_session.get(session_key, [])):
            if sub.closed:
                continue
            try:
                sub.queue.put_nowait(event)
            except asyncio.QueueFull:
                await self._close_overflow(sub)

    async def close_session(self, session_key: str) -> None:
        """Close all subscriptions belonging to session_key."""
        self._replay.pop(session_key, None)
        for sub in list(self._by_session.get(session_key, [])):
            await self.unregister(sub.sub_id)

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------

    def _record(self, session_key: str, event: dict[str, Any]) -> None:
        """Keep the turn in flight replayable. Synchronous by contract.

        Anything awaited here would open a window in which ``register`` copies
        a buffer this event has not reached yet while ``emit`` has already
        passed the point that would have queued it live -- the one ordering
        that loses an event outright.
        """
        kind = event.get("type")
        if kind in ("message.start", "turn.started"):
            # A new turn replaces the last one; nothing before it is live.
            # turn.started opens a runtime turn -- the client needs the same
            # in-flight buffer a message.start opens, or a subscriber joining
            # mid-turn sees deltas with no opening boundary and the wrong
            # workspace counter.
            self._replay[session_key] = [event]
            return
        buffered = self._replay.get(session_key)
        if buffered is None:
            return
        if kind in _TURN_OVER:
            # The turn is over and the transcript is on disk, which is what a
            # client joining from here reads. Holding the buffer past this
            # point would replay a finished turn on top of that.
            del self._replay[session_key]
            return
        last = buffered[-1]
        if kind in _MERGEABLE and last.get("type") == kind:
            # One growing event rather than one per token, so the buffer is
            # bounded by how many times the turn changed register, not by how
            # much it said.
            buffered[-1] = {
                "type": kind,
                "payload": {**last.get("payload", {}), "text": last["payload"].get("text", "") + _text_of(event)},
            }
            return
        buffered.append(event)
        if len(buffered) > REPLAY_CAPACITY:
            # Drop from the middle, never the head or the tail: the head opens
            # the turn a client has to render into, and the tail is what is on
            # screen right now.
            del buffered[1]

    def _mark_closed(self, sub: Subscription) -> None:
        """Mark subscription closed, cancel its loop, drop from indexes."""
        sub.closed = True
        if sub.coalesce_task is not None and not sub.coalesce_task.done():
            sub.coalesce_task.cancel()
        self._by_id.pop(sub.sub_id, None)
        bucket = self._by_session.get(sub.session_key)
        if bucket is not None:
            bucket[:] = [s for s in bucket if s.sub_id != sub.sub_id]
            if not bucket:
                del self._by_session[sub.session_key]

    async def _coalesce_loop(self, sub: Subscription) -> None:
        """Per-subscription 16ms window coalesce loop.

        Each iteration:
          1. Block waiting for the first event.
          2. Sleep 16ms — accumulate any further events into a batch.
          3. Drain non-blockingly.
          4. Merge consecutive token.delta events; pass through others in order.
          5. Write each merged event as a JSON-RPC notification.
        """
        try:
            while not sub.closed:
                first = await sub.queue.get()
                batch: list[dict[str, Any]] = [first]
                await asyncio.sleep(COALESCE_WINDOW_S)
                while not sub.queue.empty():
                    batch.append(sub.queue.get_nowait())
                merged = _merge_consecutive_token_deltas(batch)
                for event in merged:
                    if sub.closed:
                        return
                    await self._send_frame(
                        {
                            "jsonrpc": "2.0",
                            "method": "event",
                            "params": {
                                "subscription_id": sub.sub_id,
                                "event": event,
                            },
                        }
                    )
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.exception("subscription coalesce loop crashed sub_id={}", sub.sub_id)

    async def _close_overflow(self, sub: Subscription) -> None:
        """Emit -32016 overflow notification, then close the subscription.

        Code -32016 from the extension range (-32016..-32049). Originally
        spec'd as -32010 in early drafts — collides with the live
        ConfigFieldReadonlyError.
        """
        if sub.closed:
            return
        try:
            await self._send_frame(
                {
                    "jsonrpc": "2.0",
                    "method": "event",
                    "params": {
                        "subscription_id": sub.sub_id,
                        "event": {
                            "type": "error",
                            "payload": {
                                "code": -32016,
                                "message": "subscription_capacity_exceeded",
                                "reason": "internal",
                            },
                        },
                    },
                }
            )
        except Exception:
            logger.exception(
                "failed to send overflow notification sub_id={}",
                sub.sub_id,
            )
        finally:
            self._mark_closed(sub)


def _text_of(event: dict[str, Any]) -> str:
    payload = event.get("payload")
    return str(payload.get("text", "")) if isinstance(payload, dict) else ""


def _merge_consecutive_token_deltas(
    batch: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Collapse runs of `token.delta` events into a single merged frame.

    Non-delta events break the run and pass through unchanged. Preserves
    overall event ordering.

    A run also breaks when the ``target`` changes: the merged frame is rebuilt
    rather than mutated, so a tag that is not carried across is a tag silently
    dropped -- and an untagged delta reads as the main agent's, which is the one
    mistake this field exists to prevent.
    """
    result: list[dict[str, Any]] = []
    pending: dict[str, Any] | None = None
    for event in batch:
        if event.get("type") == "token.delta":
            target = event["payload"].get("target")
            if pending is not None and pending["payload"].get("target") != target:
                result.append(pending)
                pending = None
            if pending is None:
                payload = {"text": event["payload"]["text"]}
                if target is not None:
                    payload["target"] = target
                pending = {"type": "token.delta", "payload": payload}
            else:
                pending["payload"]["text"] += event["payload"]["text"]
        else:
            if pending is not None:
                result.append(pending)
                pending = None
            result.append(event)
    if pending is not None:
        result.append(pending)
    return result


__all__ = [
    "COALESCE_WINDOW_S",
    "QUEUE_CAPACITY",
    "SendFrame",
    "Subscription",
    "SubscriptionEmitter",
]
